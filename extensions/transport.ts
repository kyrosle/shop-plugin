// Pi Shop wiring for the built-in transport: session lifecycle, validated
// receive path and Pi public-message injection. The transport itself lives in
// transport/ and is host-agnostic; Python (core/transport.py) owns identity
// verification, durable dedupe and the handoff decision.
//
// Rules enforced here:
//  * nothing starts in the extension factory - only `session_start` connects;
//  * a frame is validated by Shop before it can reach the model;
//  * injection uses `pi.sendMessage` (never `sendUserMessage`), busy = steer;
//  * a crash between the durable `received` write and the injection is reported
//    unknown (durable `unknown`, wire receipt stays at `receiver_received`) and
//    is never auto-re-injected or reported as a business rejection;
//  * the per-frame scratch file is removed on every path (success, duplicate,
//    reject, verification failure, injection failure);
//  * `session_start` is idempotent and a stop/start race cannot orphan a client;
//  * no Herdr prompt fallback and no external intercom path.
import { t } from "./i18n.js";
import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";
import { createHash, randomUUID } from "node:crypto";
import { readFileSync, statSync, unlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { basename, join } from "node:path";
import { readBridge, type Bridge } from "./bridge.js";
import { ShopTransportClient, type ClientIdentity, type SendRequest, type SendOutcome } from "../transport/client/index.ts";
import { publishEndpoint, readEndpoint, removeEndpoint } from "./endpoints.js";
import type { SendMessage, ReceiptMessage } from "../transport/shared/types.ts";

interface ShopMember {
  name: string;
  pane: string;
  launch_id?: string;
  terminal_id?: string;
  session_dir?: string;
}

interface ShopState {
  recovery_required?: boolean;
  lifecycle?: { version?: number; session_id?: string; architect_process?: { pid: number } };
  shop_id?: string;
  run_id?: string;
  tab?: string;
  phase?: string;
  cwd?: string;
  architect?: ShopMember;
  lead?: ShopMember;
  extra_leads?: ShopMember[];
  workers?: ShopMember[];
}

export interface TransportStartResult {
  started: boolean;
  reason?: string;
  member?: string;
}

/** Minimal client surface the receive handler needs (keeps it unit-testable). */
export interface ReceiptSender {
  sendReceipt(receipt: {
    message_id: string;
    from_endpoint_epoch: string;
    status: "receiver_received" | "injected" | "rejected";
    detail?: string;
  }): void;
  endpoint_epoch: string | null;
}

export interface InboundMessage {
  message_id: string;
  payload: Record<string, unknown>;
}

export type InboundOutcome = "injected" | "duplicate" | "rejected" | "unknown";

export interface PythonDecision {
  decision: "inject" | "duplicate" | "reject";
  message_id?: string;
  state?: string;
  code?: string;
  detail?: string;
  body?: string;
  kind?: string;
  reply_to?: string | null;
  sender?: { member_id?: string; launch_id?: string };
  sender_role?: string;
}

export interface ReceiveDeps {
  client: ReceiptSender;
  runId: string;
  statePath: string;
  selfName: string;
  selfPane: string;
  exec: (args: string[]) => Promise<PythonDecision>;
  sendMessage: (text: string, details: Record<string, unknown>,
    options: { triggerTurn?: boolean; deliverAs?: "steer" }) => void;
  idle: () => boolean;
  current?: () => boolean;
  scratchDir?: string;
}

export function statePathFor(bridge: Bridge, env: NodeJS.ProcessEnv): string | undefined {
  if (!env.HERDR_SOCKET_PATH || !env.HERDR_TAB_ID) return undefined;
  const key = createHash("sha256").update(`${env.HERDR_SOCKET_PATH}:${env.HERDR_TAB_ID}`).digest("hex").slice(0, 12);
  return join(bridge.state_dir, "runtime", `${key}.json`);
}

export function readShopState(path: string): ShopState | undefined {
  try {
    if (statSync(path).size > 65536) return undefined;
    return JSON.parse(readFileSync(path, "utf8")) as ShopState;
  } catch {
    return undefined;
  }
}

export function findMember(state: ShopState, paneId: string | undefined): { member: ShopMember; role: string } | undefined {
  if (!paneId) return undefined;
  const candidates: Array<[string, ShopMember | undefined]> = [
    ["architect", state.architect], ["lead", state.lead],
    ...(state.extra_leads ?? []).map((member): [string, ShopMember] => ["auxiliary_lead", member]),
    ...(state.workers ?? []).map((member): [string, ShopMember] => ["worker", member]),
  ];
  const match = candidates.find(([, member]) => member?.pane === paneId);
  return match && match[1] ? { member: match[1], role: match[0] } : undefined;
}

export function sessionIdFor(ctx: ExtensionContext, env: NodeJS.ProcessEnv): string {
  try {
    const id = ctx.sessionManager?.getSessionId?.();
    if (id) return id;
    const file = ctx.sessionManager?.getSessionFile?.();
    if (file) return basename(file);
  } catch {
    /* session metadata unavailable */
  }
  return env.PI_SESSION_ID ?? "";
}

/**
 * Validate one inbound frame through Shop, then deliver it (or not).
 *
 * The scratch envelope file is removed in a `finally` block on every path, so
 * message bodies never survive outside Shop state retention. An injection
 * failure is durable `unknown` + wire `receiver_received`: it must not claim a
 * recipient business rejection.
 */
export async function handleInboundMessage(deps: ReceiveDeps, message: InboundMessage): Promise<InboundOutcome> {
  const receiptClient = deps.client;
  deps = { ...deps, client: {
    get endpoint_epoch() { return receiptClient.endpoint_epoch; },
    sendReceipt(value) { try { receiptClient.sendReceipt(value); } catch { /* connection loss is not injection failure */ } },
  } };
  const scratchDir = deps.scratchDir ?? tmpdir();
  const scratch = join(scratchDir, `shop-transport-${randomUUID()}.json`);
  let decision: PythonDecision | undefined;
  let verificationFailure: unknown;
  try {
    writeFileSync(scratch, JSON.stringify(message.payload), { mode: 0o600 });
    decision = await deps.exec(["receive", "--state", deps.statePath, "--self", deps.selfName,
      "--self-pane", deps.selfPane, "--file", scratch]);
  } catch (error) {
    verificationFailure = error;
  } finally {
    try {
      unlinkSync(scratch);
    } catch {
      /* the file was never created, or is already gone */
    }
  }

  const epoch = deps.client.endpoint_epoch ?? "";
  if (verificationFailure || !decision) {
    // Shop could not verify the frame: nothing was injected and no model entry
    // exists, so a rejection is truthful here.
    deps.client.sendReceipt({
      message_id: message.message_id, from_endpoint_epoch: epoch, status: "rejected",
      detail: `shop verification failed: ${String(verificationFailure).slice(0, 500)}`,
    });
    return "rejected";
  }

  if (decision.decision === "reject") {
    deps.client.sendReceipt({
      message_id: message.message_id, from_endpoint_epoch: epoch, status: "rejected",
      detail: `${decision.code ?? "E_MALFORMED"} ${decision.detail ?? ""}`.trim(),
    });
    return "rejected";
  }
  if (decision.decision === "duplicate") {
    deps.client.sendReceipt({
      message_id: message.message_id, from_endpoint_epoch: epoch, status: "receiver_received",
      detail: `duplicate; recorded state ${decision.state ?? "unknown"}`,
    });
    return "duplicate";
  }

  if (deps.current && !deps.current()) {
    try {
      await deps.exec(["receipt", "--message-id", message.message_id, "--status", "unknown",
        "--detail", "session changed before injection; no replay"]);
    } catch { /* received guard still prevents replay */ }
    return "unknown";
  }

  // `inject`: the durable `received` record already exists. Deliver through the
  // Pi public message API; busy receivers queue (steer), never abort.
  const envelopeSender = (message.payload.sender as { member_id?: string } | undefined)?.member_id;
  const sender = decision.sender?.member_id ?? envelopeSender ?? "unknown";
  const text = `[SHOP TRANSPORT message from ${String(sender)}]\n` +
    `${decision.body ?? ""}\n` +
    `(message_id ${message.message_id}; kind ${decision.kind ?? "note"}; reply_to ${decision.reply_to ?? "none"})`;
  deps.client.sendReceipt({ message_id: message.message_id, from_endpoint_epoch: epoch, status: "receiver_received" });
  try {
    deps.sendMessage(text, {
      message_id: message.message_id, sender, reply_to: decision.reply_to ?? null, run_id: deps.runId,
    }, deps.idle() ? { triggerTurn: true } : { deliverAs: "steer" });
  } catch (error) {
    // Injection outcome is unknown: record durable `unknown` and keep the wire
    // receipt at receiver_received with an explicit unknown detail.
    try {
      await deps.exec(["receipt", "--message-id", message.message_id, "--status", "unknown",
        "--detail", `injection outcome unknown: ${String(error).slice(0, 300)}`]);
    } catch {
      /* the duplicate guard still prevents a second injection */
    }
    deps.client.sendReceipt({
      message_id: message.message_id, from_endpoint_epoch: epoch, status: "receiver_received",
      // `unknown` must not assert either outcome: no claim of injection and no
      // claim that injection was skipped, only that no retry happens.
      detail: "injection outcome unknown; no automatic retry",
    });
    return "unknown";
  }
  deps.client.sendReceipt({ message_id: message.message_id, from_endpoint_epoch: epoch, status: "injected" });
  try {
    await deps.exec(["receipt", "--message-id", message.message_id, "--status", "injected"]);
  } catch {
    /* the wire receipt is the transport evidence; the durable record stays received */
  }
  return "injected";
}

async function callTransportCli(pi: ExtensionAPI, bridge: Bridge, repo: string, runId: string, args: string[]): Promise<PythonDecision> {
  // bin/shop-transport is a POSIX sh wrapper; python3 must run the Python CLI itself.
  const result = await pi.exec("python3", [
    join(bridge.core_root, "core/transport_cli.py"), "--repo", repo, "--run", runId, ...args,
  ], { timeout: 30_000 });
  const text = result.stdout.trim();
  if (result.code !== 0) {
    throw new Error(`shop-transport ${args[0]} failed: ${(result.stderr || text || "no output").slice(0, 2000)}`);
  }
  return JSON.parse(text) as PythonDecision;
}

function transportEnv(bridge: Bridge, env: NodeJS.ProcessEnv): NodeJS.ProcessEnv {
  return { ...env, SHOP_STATE_DIR: env.SHOP_STATE_DIR ?? bridge.state_dir,
    SHOP_CONFIG_DIR: env.SHOP_CONFIG_DIR ?? bridge.config_dir };
}

// One process-wide client. `generation` invalidates an in-flight start when a
// stop (or a newer start) happens first, so no client can be orphaned.
let active: TransportClientLike | null = null;
let generation = 0;
let activeIdentity: ClientIdentity | null = null;
let activeRoot: string | null = null;
let heartbeat: ReturnType<typeof setInterval> | undefined;


export interface TransportClientLike {
  connect(identity: ClientIdentity): Promise<void>;
  disconnect(): Promise<void>;
  onMessage(handler: (message: SendMessage) => void): void;
  onReceipt?(handler: (message: ReceiptMessage) => void): unknown;
  onError?(handler: (error: Error) => void): unknown;
  send?(request: SendRequest): Promise<SendOutcome>;
  onMessageControl(handler: (control: { message_id: string }) => void): void;
  sendReceipt(receipt: {
    message_id: string; from_endpoint_epoch: string;
    status: "receiver_received" | "injected" | "rejected"; detail?: string;
  }): void;
  readonly endpoint_epoch: string | null;
  readonly broker_epoch: string | null;
  readonly connected: boolean;
}

export interface StartOptions {
  env?: NodeJS.ProcessEnv;
  createClient?: (options: { env: NodeJS.ProcessEnv }) => TransportClientLike;
  scratchDir?: string;
}


export async function startShopTransport(pi: ExtensionAPI, ctx: ExtensionContext,
  options: StartOptions = {}): Promise<TransportStartResult> {
  const env = options.env ?? process.env;
  if (env.HERDR_ENV !== "1") return { started: false, reason: "outside-herdr" };
  const bridge = readBridge();
  if (!bridge) return { started: false, reason: "bridge-unconfigured" };
  const statePath = statePathFor(bridge, env);
  if (!statePath) return { started: false, reason: "missing-herdr-context" };
  const state = readShopState(statePath);
  if (!state) return { started: false, reason: "no-workstation-state" };
  if (state.phase !== "ready" || state.recovery_required) return { started: false, reason: "shop-needs-recovery" };
  if (!state.shop_id || !state.run_id) return { started: false, reason: "run-not-bound" };
  const match = findMember(state, env.HERDR_PANE_ID);
  if (!match) return { started: false, reason: "not-a-registered-member" };
  const { member } = match;
  if (!member.launch_id || !member.terminal_id) return { started: false, reason: "member-identity-incomplete" };
  if (!state.cwd || !sessionIdFor(ctx, env)) return { started: false, reason: "session-or-repo-unavailable" };

  const ownsArchitectSession = (current: ShopState | undefined) => match.role !== "architect"
    || (current?.lifecycle?.version === 1 && current.lifecycle.session_id === sessionIdFor(ctx, env)
      && current.lifecycle.architect_process?.pid === process.pid);
  if (!ownsArchitectSession(state)) {
    if (active) await stopShopTransport();
    return { started: false, reason: "architect-session-expired" };
  }
  const identity: ClientIdentity = {
    shop_id: state.shop_id,
    run_id: state.run_id,
    member_id: member.name,
    launch_id: member.launch_id,
    session_id: sessionIdFor(ctx, env),
    terminal_id: member.terminal_id,
    pane_id: member.pane,
  };
  if (active?.connected && JSON.stringify(activeIdentity) === JSON.stringify(identity)) {
    return { started: false, reason: "already-connected" };
  }
  if (active) await stopShopTransport();
  const create = options.createClient ?? ((config: { env: NodeJS.ProcessEnv }) => new ShopTransportClient(config));
  const client = create({ env: transportEnv(bridge, env) });
  const runId = state.run_id;
  const mine = ++generation;
  client.onError?.((error) => {
    if (ctx.hasUI) ctx.ui.setStatus("shop-transport", t("Transport error: {0}", [String(error).slice(0, 120)]));
  });

  client.onMessage((message) => {
    const payload = message.payload;
    const sender = payload.sender as { member_id?: string; launch_id?: string } | undefined;
    const recipient = payload.recipient as { member_id?: string; launch_id?: string } | undefined;
    if (mine !== generation || message.message_id !== payload.message_id ||
        message.from?.member_id !== sender?.member_id || message.from?.launch_id !== sender?.launch_id ||
        message.to?.member_id !== recipient?.member_id || message.to?.launch_id !== recipient?.launch_id ||
        message.to?.endpoint_epoch !== client.endpoint_epoch || payload.kind !== message.kind ||
        payload.reply_to !== message.reply_to) {
      try {
        client.sendReceipt({ message_id: message.message_id, from_endpoint_epoch: client.endpoint_epoch ?? "",
          status: "rejected", detail: "wire/business identity mismatch or obsolete session" });
      } catch { /* obsolete connection; no injection */ }
      return;
    }
    void handleInboundMessage({
      client,
      runId,
      statePath,
      selfName: member.name,
      selfPane: member.pane,
      scratchDir: options.scratchDir,
      exec: (args) => callTransportCli(pi, bridge, state.cwd!, runId, args),
      idle: () => ctx.isIdle(),
      current: () => {
        const current = readShopState(statePath);
        const registered = current && findMember(current, identity.pane_id)?.member;
        return registered?.name === identity.member_id && registered.launch_id === identity.launch_id
          && registered.terminal_id === identity.terminal_id && mine === generation && client.connected && sessionIdFor(ctx, env) === identity.session_id
          && current?.phase === "ready" && !current.recovery_required && current.shop_id === identity.shop_id
          && current.run_id === identity.run_id && ownsArchitectSession(current);
      },
      sendMessage: (text, details, sendOptions) => pi.sendMessage({
        customType: "shop_transport", content: text, display: true, details,
      }, sendOptions),
    }, message).catch((error) => {
      if (ctx.hasUI) ctx.ui.setStatus("shop-transport", t("Receive outcome unknown: {0}", [String(error).slice(0, 100)]));
    });
  });
  client.onMessageControl((control) => {
    if (ctx.hasUI) ctx.ui.notify(t("Shop transport: {0} cancelled before injection", [control.message_id]), "info");
  });

  try {
    await client.connect(identity);
  } catch (error) {
    await client.disconnect();
    if (ctx.hasUI) ctx.ui.setStatus("shop-transport", t("Transport unavailable: {0}", [String(error).slice(0, 120)]));
    return { started: false, reason: `connect-failed: ${String(error).slice(0, 200)}` };
  }
  if (mine !== generation) {
    // A stop (or a newer start) happened while connecting: never orphan this client.
    await client.disconnect();
    return { started: false, reason: "superseded" };
  }
  active = client;
  activeIdentity = identity;
  activeRoot = bridge.state_dir;
  const publish = () => {
    if (client.connected && client.endpoint_epoch && client.broker_epoch) {
      publishEndpoint(bridge.state_dir, identity, client.endpoint_epoch, client.broker_epoch);
    }
  };
  publish();
  heartbeat = setInterval(() => { try { publish(); } catch { /* expiry fails closed */ } }, 5000);
  heartbeat.unref?.();
  client.onReceipt?.((receipt) => {
    void callTransportCli(pi, bridge, state.cwd!, runId, ["outgoing", "--message-id", receipt.message_id,
      "--status", receipt.status, "--state", statePath, "--self", member.name])
      .catch(() => { /* missing persistence never upgrades a receipt */ });
  });
  if (ctx.hasUI) ctx.ui.setStatus("shop-transport", t("Transport ready · {0} · {1}", [identity.member_id, client.endpoint_epoch?.slice(0, 8)]));
  return { started: true, member: identity.member_id };
}

export async function stopShopTransport(): Promise<void> {
  generation += 1;
  const client = active;
  if (heartbeat) clearInterval(heartbeat);
  heartbeat = undefined;
  if (activeRoot && activeIdentity && client?.endpoint_epoch) {
    removeEndpoint(activeRoot, activeIdentity, client.endpoint_epoch);
  }
  active = null;
  activeIdentity = null;
  activeRoot = null;
  if (client) await client.disconnect();
}

export function transportStatus(): { connected: boolean; endpoint_epoch: string | null; broker_epoch: string | null } {
  return {
    connected: active?.connected ?? false,
    endpoint_epoch: active?.endpoint_epoch ?? null,
    broker_epoch: active?.broker_epoch ?? null,
  };
}


/** Reconcile membership only; never retries messages or dispatches work. */
export async function reconcileShopTransport(pi: ExtensionAPI, ctx: ExtensionContext): Promise<void> {
  const bridge = readBridge();
  const path = bridge && statePathFor(bridge, process.env);
  const state = path ? readShopState(path) : undefined;
  if (!state || state.phase !== "ready" || state.recovery_required || !state.run_id ||
      !findMember(state, process.env.HERDR_PANE_ID)) {
    if (active) await stopShopTransport();
    return;
  }
  await startShopTransport(pi, ctx);
}

/** Only built-in transport sends. Python prepares and journals the envelope. */
export async function sendShopEnvelope(pi: ExtensionAPI, ctx: ExtensionContext,
  envelope: Record<string, unknown>, expectedRecipientSession?: string): Promise<SendOutcome> {
  await startShopTransport(pi, ctx);
  const bridge = readBridge();
  const path = bridge && statePathFor(bridge, process.env);
  const state = path ? readShopState(path) : undefined;
  const sender = envelope.sender as { member_id: string; launch_id: string };
  const recipient = envelope.recipient as { member_id: string; launch_id: string };
  if (!bridge || !path || !state?.cwd || state.phase !== "ready" || state.recovery_required
      || !active?.connected || !active.send || !activeIdentity ||
      state.run_id !== envelope.run_id || state.shop_id !== envelope.shop_id ||
      activeIdentity.run_id !== envelope.run_id || activeIdentity.member_id !== sender?.member_id ||
      activeIdentity.launch_id !== sender?.launch_id || activeIdentity.session_id !== sessionIdFor(ctx, process.env)) {
    throw new Error("E_NOT_REGISTERED: no current Shop endpoint; nothing submitted");
  }
  const target = readEndpoint(bridge.state_dir, String(envelope.shop_id), recipient.member_id);
  if (!target || target.run_id !== envelope.run_id || target.launch_id !== recipient.launch_id ||
      target.broker_epoch !== active.broker_epoch ||
      (expectedRecipientSession !== undefined && target.session_id !== expectedRecipientSession)) {
    throw new Error("E_TARGET_STALE_EPOCH: target endpoint absent/stale; nothing submitted");
  }
  const result = await active.send({ message_id: String(envelope.message_id),
    to: { member_id: target.member_id, launch_id: target.launch_id, endpoint_epoch: target.endpoint_epoch },
    kind: envelope.kind as SendRequest["kind"], reply_to: envelope.reply_to as string | null, payload: envelope });
  try {
    await callTransportCli(pi, bridge, state.cwd, String(envelope.run_id), ["outgoing", "--state", path,
      "--self", sender.member_id, "--message-id", result.message_id, "--status", result.outcome]);
  } catch {
    return { ...result, outcome: "unknown", detail: "send completed but outcome journal unavailable; do not resend" };
  }
  return result;
}
