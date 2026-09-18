import { selectAction, t } from "./i18n.js";
import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";
import type { Thinking as ThinkingLevel } from "./configuration.js";
import { getSupportedThinkingLevels } from "@earendil-works/pi-ai";
import { matchesKey, truncateToWidth, wrapTextWithAnsi } from "@earendil-works/pi-tui";
import { mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { randomUUID } from "node:crypto";
import { readBridge } from "./bridge.js";
import { findMember, readShopState, sendShopEnvelope, sessionIdFor, statePathFor, transportStatus } from "./transport.js";

type Json = Record<string, unknown>;
interface RequestRecord {
  id: string; kind: string; status: string;
  sender: { name: string }; recipient?: { name: string };
  payload: { profile?: { model: string; thinking?: ThinkingLevel | null }; [key: string]: unknown };
}
interface View {
  actor: string; snapshot: { shop: Json; members: Array<{ name: string; role: string; [key: string]: unknown }> };
  records: RequestRecord[]; handoffs: Json[]; models: Json;
}

export function workbenchClient(pi: ExtensionAPI, ctx: ExtensionContext) {
  const bridge = readBridge();
  const path = bridge && statePathFor(bridge, process.env);
  if (!bridge || !path) throw new Error(t("Shop bridge/Herdr context unavailable"));
  const token = () => {
    const state = readShopState(path);
    const actor = state && findMember(state, process.env.HERDR_PANE_ID)?.member;
    if (!state || !actor) throw new Error(t("Shop membership unavailable"));
    return { shop_id: state.shop_id, run_id: state.run_id, actor: actor.name, launch_id: actor.launch_id,
      session_id: sessionIdFor(ctx, process.env) };
  };
  const expected = token();
  return async <T = Json>(data: Json): Promise<T> => {
    if (JSON.stringify(token()) !== JSON.stringify(expected)) throw new Error(t("Shop/run/session changed; reopen /shop-ui"));
    const result = await pi.exec("python3", [join(bridge.core_root, "core/workbench.py"), "--state", path,
      "--request", JSON.stringify({ ...data, expected })], { timeout: 90_000 });
    if (JSON.stringify(token()) !== JSON.stringify(expected)) throw new Error(t("Shop/run/session changed during operation; inspect recorded outcome, do not replay"));
    if (result.code || result.killed) {
      let message = (result.stderr || result.stdout).slice(0, 4000);
      try {
        const error = JSON.parse(message) as { code: string; reason: string; next_action: string };
        message = t("{0}: {1}\nNext step: {2}", [error.code, error.reason, error.next_action]);
      } catch { /* legacy/launcher error stays bounded */ }
      throw new Error(message);
    }
    if (result.stdout.length > 512_000) throw new Error(t("Workbench response exceeds display bound"));
    return JSON.parse(result.stdout) as T;
  };
}

/** Bounded, read-only pager. No editor, model calls, files or repair actions. */
export async function showFacts(ctx: ExtensionContext, title: string, value: unknown): Promise<void> {
  const text = JSON.stringify(value, null, 2).slice(0, 64_000);
  await ctx.ui.custom<void>((tui, theme, _keys, done) => {
    let scroll = 0;
    let count = 0;
    return {
      render(width: number) {
        const rows = Math.max(3, Math.min(30, tui.terminal.rows - 5));
        const lines = text.split("\n").flatMap(line => wrapTextWithAnsi(line, Math.max(1, width - 2)));
        count = Math.max(0, lines.length - rows);
        scroll = Math.min(scroll, count);
        return [truncateToWidth(theme.fg("accent", title), width), ...lines.slice(scroll, scroll + rows),
          truncateToWidth(t("↑↓ / PgUp PgDn · Esc back · {0}/{1}", [scroll + 1, lines.length]), width)];
      },
      handleInput(data: string) {
        if (matchesKey(data, "escape") || matchesKey(data, "enter")) { done(); return; }
        if (matchesKey(data, "down")) scroll = Math.min(count, scroll + 1);
        if (matchesKey(data, "up")) scroll = Math.max(0, scroll - 1);
        if (matchesKey(data, "pageDown")) scroll = Math.min(count, scroll + 20);
        if (matchesKey(data, "pageUp")) scroll = Math.max(0, scroll - 20);
        tui.requestRender();
      },
      invalidate() {},
    };
  });
}

export async function submitPrepared(pi: ExtensionAPI, ctx: ExtensionContext, prepared: Json): Promise<Json> {
  const envelope = prepared.transport_request as Json | undefined;
  if (!envelope) return prepared;
  try {
    const outcome = await sendShopEnvelope(pi, ctx, envelope, prepared.transport_session as string | undefined);
    return { ...prepared, transport_request: undefined, transport: outcome,
      note: "Transport receipt is not business acceptance. No automatic resend." };
  } catch (error) {
    return { ...prepared, transport_request: undefined, transport: { outcome: "unknown", detail: String(error) },
      note: "Prepared record retained. Inspect before retry; no fallback or automatic resend." };
  }
}

export async function workbenchUI(pi: ExtensionAPI, ctx: ExtensionContext): Promise<void> {
  if (!ctx.hasUI) throw new Error(t("/shop-ui requires interactive Pi UI"));
  const call = workbenchClient(pi, ctx);
  const chooseMember = async (view: View) => ctx.ui.select(t("Select exact recipient"), view.snapshot.members
    .filter(m => m.name !== view.actor).map(m => m.name));
  const input = (label: string) => ctx.ui.input(label);
  while (true) {
    const view = await call<View>({ action: "view" });
    const choice = await selectAction(ctx, `Shop · ${view.snapshot.shop.run_id ?? "unbound"} · ${view.actor}`, [
      ["status", t("Status and identity")], ["handoff", t("Propose handoff")], ["inbox", t("Inbox and receipts")],
      ["development", t("Development preparation")], ["delivery", t("Delivery and integration")], ["profile", t("Idle-seat configuration")],
      ["intervention", t("Task intervention")], ["diagnostics", t("Diagnostics and redacted export")], ["exit", t("Exit")],
    ]);
    if (!choice || choice === "exit") return;
    try {
      if (choice === "status") {
        await showFacts(ctx, t("Read-only snapshot · configuration is not a runtime observation"), { ...view, local_transport: transportStatus() });
      } else if (choice === "handoff") {
        const recipient = await chooseMember(view); if (!recipient) continue;
        const objective = await input(t("Objective (required)")); if (!objective) continue;
        const scope = await input(t("Scope/constraints (required)")); if (!scope) continue;
        const acceptance = await input(t("Acceptance criteria (required)")); if (!acceptance) continue;
        const evidence = await input(t("Evidence path (optional)")); if (evidence === undefined) continue;
        if (!await ctx.ui.confirm(t("Submit handoff proposal?"), t("{0}\n{1}\nOnly proposed; not recipient acceptance or task completion.", [recipient, objective]))) continue;
        await showFacts(ctx, t("Handoff proposal result"), await submitPrepared(pi, ctx,
          await call({ action: "handoff-propose", recipient, objective, scope, acceptance, evidence })));
      } else if (choice === "inbox") {
        const records = view.records.filter(r => r.recipient?.name === view.actor);
        const selected = await selectAction(ctx, t("Inbox (business acceptance ≠ transport delivery)"), [["all", t("View all receipts")],
          ...records.map(r => ["record:" + r.id, `${r.id} · ${r.kind} · ${r.status}`] as const)]);
        if (!selected) continue;
        if (selected === "all") { await showFacts(ctx, t("Receipts"), { records: view.records, handoffs: view.handoffs }); continue; }
        const record = records.find(r => selected === "record:" + r.id);
        if (!record) continue;
        await showFacts(ctx, t("Request details"), record);
        if (record.kind === "handoff") {
          const transition = await selectAction(ctx, t("Explicit business decision"), [
            ["accept", t("Accept (accept)")], ["needs_context", t("Need context (needs_context)")],
            ["reject", t("Reject (reject)")], ["deliver", t("Deliver (deliver)")], ["block", t("Blocked (block)")],
          ]);
          if (!transition) continue;
          const detail = await input(t("Explanation/delivery evidence")); if (detail === undefined) continue;
          if (!await ctx.ui.confirm(t("Confirm business receipt?"), t("{0}\n{1}\nDoes not automatically accept the ticket.", [record.id, transition]))) continue;
          await showFacts(ctx, t("Receipts"), await call({ action: "handoff-transition", id: record.id, transition, detail }));
        } else if (record.kind === "profile") {
          if (["applying", "unknown"].includes(record.status)) {
            if (!ctx.isIdle() || !ctx.model) throw new Error(t("Idle Pi with observable model required for reconciliation"));
            const observed = { model: `${ctx.model.provider}/${ctx.model.id}`, thinking: pi.getThinkingLevel() };
            if (!await ctx.ui.confirm(t("Confirm current runtime configuration and unblock dispatch?"), JSON.stringify(observed))) continue;
            if (!ctx.isIdle()) throw new Error(t("Pi became busy; no reconciliation"));
            await showFacts(ctx, t("Manual reconciliation receipt"), await call({ action: "profile-resolve", id: record.id,
              session_id: sessionIdFor(ctx, process.env), observed }));
            continue;
          }
          const profile = record.payload.profile!;
          const model = ctx.modelRegistry.getAvailable().find(m => `${m.provider}/${m.id}` === profile.model);
          if (!model || (profile.thinking != null && !getSupportedThinkingLevels(model).includes(profile.thinking))) {
            throw new Error(t("Receiver model/provider/thinking unavailable; request not applied"));
          }
          if (!ctx.isIdle()) throw new Error(t("Current Pi busy; no model change"));
          if (!await ctx.ui.confirm(t("Apply configuration in this idle Pi?"), JSON.stringify(record.payload))) continue;
          if (!ctx.isIdle()) throw new Error(t("Pi became busy; reopen later"));
          const session_id = sessionIdFor(ctx, process.env);
          await call({ action: "profile-claim", id: record.id, session_id });
          let status = "unknown";
          let detail = "";
          try {
            if (!ctx.isIdle()) throw new Error(t("Pi became busy after claim; inspect request before retry"));
            if (!await pi.setModel(model)) { status = "rejected"; detail = "Pi rejected model selection"; }
            else {
              if (!ctx.isIdle()) throw new Error(t("Pi became busy during model application; outcome unknown"));
              if (profile.thinking != null) pi.setThinkingLevel(profile.thinking);
              if (profile.thinking != null && pi.getThinkingLevel() !== profile.thinking) throw new Error(t("Pi thinking differs from request"));
              status = "applied";
            }
          } catch (error) { detail = String(error); }
          await showFacts(ctx, t("Configuration receipt"), await call({ action: "profile-result", id: record.id, session_id, status, detail }));
        } else if (["scope-change", "pause", "cancel"].includes(record.kind)) {
          if (record.kind === "scope-change") {
            if (await ctx.ui.confirm(t("Accept scope revision?"), t("Activate the new revision. Old ready tickets require explicit revise; assigned tickets are not rewritten."))) {
              await showFacts(ctx, t("Scope revision receipt"), await call({ action: "intervention-acknowledge", id: record.id }));
            }
          } else {
            const operation = await selectAction(ctx, t("Primary Lead intervention"), [["acknowledge", t("Acknowledge receipt (does not stop work)")], ["execute", t("Execute confirmed request")]]);
            if (operation === "acknowledge") {
              await showFacts(ctx, t("Intervention receipt"), await call({ action: "intervention-acknowledge", id: record.id }));
            } else if (operation === "execute") {
              if (record.kind === "cancel" && !await ctx.ui.confirm(t("Confirm all previous writers have stopped?"), t("Check foreground and background writers. Idle does not prove background writers stopped. Cancel if uncertain."))) continue;
              if (!await ctx.ui.confirm(t("Execute intervention?"), record.kind === "pause"
                ? t("Send Esc only to the original executor; do not mark stopped or redispatch.")
                : t("Cancel the listed current attempts; preserve code/checkpoint/result. Unresolved dependencies cause refusal."))) continue;
              await showFacts(ctx, t("Intervention execution result"), await call({ action: "intervention-execute", id: record.id,
                writers_stopped: record.kind === "cancel" }));
            }
          }
        }
      } else if (choice === "development") {
        const path = await input(t("New worktree absolute path (must not exist)")); if (!path) continue;
        const branch = await input(t("New branch name (must not exist)")); if (!branch) continue;
        const base = await input(t("Base commit/ref (empty for HEAD)")); if (base === undefined) continue;
        const plan = await call({ action: "development-preview", path, branch, base: base || "HEAD" });
        await showFacts(ctx, t("Development preflight · Git unchanged"), plan);
        if (await ctx.ui.confirm(t("Create the previewed branch and worktree?"), t("{0}\n{1}\nNo commit, dispatch, overwrite or cleanup of existing files.", [path, branch]))) {
          await showFacts(ctx, t("Creation result"), await call({ action: "development-create", id: plan.id }));
        }
      } else if (choice === "delivery") {
        const report = await call({ action: "delivery" });
        await showFacts(ctx, t("Delivery checklist"), report);
        const operation = await selectAction(ctx, t("Next delivery step"), [["view", t("View only")], ["integrate", t("Preview integration")], ["check", t("Record final-check evidence")]]);
        if (!operation || operation === "view") continue;
        if (operation === "check") {
          const command = await input(t("Already executed check command/manual verification name")); if (!command) continue;
          const code = await input(t("Exit code (integer)")); if (code === undefined || !/^-?\d+$/.test(code)) continue;
          const evidence = await input(t("Check evidence absolute path (≤1 MiB)")); if (!evidence) continue;
          if (!await ctx.ui.confirm(t("Record user-attested verification evidence?"), t("HEAD {0}\n{1}\nThe plugin does not run the command. Records the file hash; HEAD/file changes invalidate the evidence.", [report.target_commit, command]))) continue;
          await showFacts(ctx, t("Final-check evidence"), await call({ action: "final-check", target_commit: report.target_commit,
            command, exit_code: Number(code), evidence }));
          continue;
        }
        const ticket = await input(t("Optional accepted ticket ID to integrate; empty for view only")); if (!ticket) continue;
        const plan = await call({ action: "integration-preview", ticket });
        await showFacts(ctx, t("Integration plan · ff-only · Git unchanged"), plan);
        if (!await ctx.ui.confirm(t("Have all foreground/background writers stopped?"), t("Idle is not proof that background writers stopped. Verify personally; cancel if uncertain."))) continue;
        if (!await ctx.ui.confirm(t("Execute the previewed Git fast-forward?"), t("This changes the target branch and files. No automatic push, run closure or cleanup; failure preserves partial state."))) continue;
        await showFacts(ctx, t("Integration result"), await call({ action: "integrate", id: plan.id, writers_stopped: true }));
      } else if (choice === "profile") {
        const recipient = await chooseMember(view); if (!recipient) continue;
        const model = await ctx.ui.select(t("Select requested model (recipient validates again)"), ctx.modelRegistry.getAvailable().map(m => `${m.provider}/${m.id}`));
        if (!model) continue;
        const selected = ctx.modelRegistry.getAvailable().find(m => `${m.provider}/${m.id}` === model)!;
        const thinking = await ctx.ui.select(t("Runtime changes require an explicit thinking level"), getSupportedThinkingLevels(selected)); if (!thinking) continue;
        const persist = await ctx.ui.confirm(t("Persist to this Shop's future launch configuration?"), t("Target seat only. No = current session only; global/project configuration stays unchanged."));
        if (!await ctx.ui.confirm(t("Request configuration change in the target Pi?"), t("{0}\n{1}\n{2}\nThe recipient must confirm in /shop-ui inbox. Architect is excluded.", [recipient, model, thinking]))) continue;
        await showFacts(ctx, t("Configuration requested (not yet applied)"), await call({ action: "profile-request", recipient,
          profile: { model, thinking }, persist }));
      } else if (choice === "intervention") {
        const kind = await selectAction(ctx, t("Choose supplement / scope change / pause / cancel"), [
          ["supplement", t("Supplement (supplement)")], ["scope-change", t("Scope change (scope-change)")],
          ["pause", t("Request pause (pause)")], ["cancel", t("Request cancellation (cancel)")],
        ]);
        if (!kind) continue;
        const text = await input(t("Supplement text or change reason")); if (!text) continue;
        if (kind === "supplement") {
          const recipient = await chooseMember(view); if (!recipient) continue;
          if (!await ctx.ui.confirm(t("Send supplement?"), t("{0}\n{1}\nSteer when busy; no new task or acceptance-criteria changes.", [recipient, text]))) continue;
          await showFacts(ctx, t("Supplement delivery result"), await submitPrepared(pi, ctx,
            await call({ action: "note", recipient, text })));
          continue;
        }
        const tickets = await input(t("Affected ticket IDs, comma-separated (optional)")); if (tickets === undefined) continue;
        if (!await ctx.ui.confirm(t("Create intervention request?"), t("{0}\n{1}\nRequested is not stopped/cancelled. Lead must acknowledge and execute the authorized action.", [kind, text]))) continue;
        await showFacts(ctx, t("Intervention request"), await call({ action: "intervention-request", kind, text,
          tickets: tickets.split(",").map(s => s.trim()).filter(Boolean) }));
      } else if (choice === "diagnostics") {
        const report = await call({ action: "diagnostics" });
        await showFacts(ctx, t("Redacted diagnostics (no paths, IDs, prompts, environment or raw errors)"), report);
        if (!await ctx.ui.confirm(t("Export the redacted JSON above?"), t("Write only to the local Shop diagnostics directory; no upload."))) continue;
        const root = readBridge()!.state_dir;
        mkdirSync(join(root, "diagnostics"), { recursive: true, mode: 0o700 });
        const file = join(root, "diagnostics", `${randomUUID()}.json`);
        writeFileSync(file, JSON.stringify(report, null, 2), { mode: 0o600, flag: "wx" });
        ctx.ui.notify(file, "info");
      }
    } catch (error) { ctx.ui.notify(String(error), "error"); }
  }
}
