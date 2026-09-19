// Shop built-in transport contract harness.
// Starts the real broker (node/bun strip-types launch, no tsx/npx/network) in an
// isolated temp transport dir and drives raw net clients over the versioned Shop transport
// wire contract. No Pi, no Herdr, no active workstation state is touched.
import { afterEach, expect, test } from "bun:test";
import { spawn, type ChildProcess } from "node:child_process";
import { createHash } from "node:crypto";
import { existsSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, rmSync, statSync, writeFileSync } from "node:fs";
import { Socket, createServer, type Server } from "node:net";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { createMessageReader, writeMessage } from "../transport/shared/framing.ts";
import { getBrokerLaunchSpec, spawnBrokerIfNeeded } from "../transport/client/spawn.ts";
import {
  SHOP_TRANSPORT_PROTOCOL_NAME,
  SHOP_TRANSPORT_PROTOCOL_VERSION,
  getBrokerPidPath,
  getBrokerSocketPath,
} from "../transport/shared/paths.ts";
import { payloadFingerprint } from "../transport/shared/protocol.ts";

interface RawMessage {
  type: string;
  [key: string]: unknown;
}

class RawClient {
  private messages: RawMessage[] = [];
  private waiters: Array<{ predicate: (message: RawMessage) => boolean; resolve: (message: RawMessage) => void }> = [];

  private constructor(readonly socket: Socket, readonly socketPath: string) {}

  static async connect(socketPath: string): Promise<RawClient> {
    const socket = await new Promise<Socket>((resolve, reject) => {
      const candidate = new Socket();
      candidate.once("error", reject);
      candidate.connect(socketPath, () => resolve(candidate));
    });
    const client = new RawClient(socket, socketPath);
    socket.on("data", createMessageReader(
      (raw) => {
        const message = raw as RawMessage;
        const index = client.waiters.findIndex((waiter) => waiter.predicate(message));
        if (index >= 0) {
          const [waiter] = client.waiters.splice(index, 1);
          waiter.resolve(message);
        } else {
          client.messages.push(message);
        }
      },
      () => { /* harness: framing errors surface as timeouts */ },
    ));
    return client;
  }

  send(message: unknown): void {
    writeMessage(this.socket, message);
  }

  async next(predicate: (message: RawMessage) => boolean = () => true, timeoutMs = 3_000): Promise<RawMessage> {
    const index = this.messages.findIndex(predicate);
    if (index >= 0) return this.messages.splice(index, 1)[0];
    return await new Promise<RawMessage>((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error("timed out waiting for transport message")), timeoutMs);
      this.waiters.push({ predicate, resolve: (message) => { clearTimeout(timer); resolve(message); } });
    });
  }

  async nextType(type: string, timeoutMs = 3_000): Promise<RawMessage> {
    return await this.next((message) => message.type === type, timeoutMs);
  }

  close(): void {
    this.socket.destroy();
  }
}

interface Harness {
  root: string;
  transportDir: string;
  socketPath: string;
  child: ChildProcess;
  stop: () => Promise<void>;
}

const harnesses: Harness[] = [];

function makeRoot(): string {
  return mkdtempSync(join(tmpdir(), "shop-transport-"));
}

function brokerEnv(transportDir: string, idleExitMs = 60_000): NodeJS.ProcessEnv {
  return {
    PI_SHOP_TRANSPORT_DIR: transportDir,
    PI_SHOP_TRANSPORT_IDLE_EXIT_MS: String(idleExitMs),
    PATH: process.env.PATH ?? "/usr/bin:/bin",
  };
}

async function waitForHealthy(transportDir: string, timeoutMs = 10_000): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const { isBrokerHealthy } = await import("../transport/client/spawn.ts");
    if (await isBrokerHealthy({ transportDir, env: brokerEnv(transportDir) })) return;
    await new Promise((resolve) => setTimeout(resolve, 50));
  }
  throw new Error("broker never became healthy");
}

async function startBroker(idleExitMs = 60_000): Promise<Harness> {
  const root = makeRoot();
  const transportDir = join(root, "transport");
  const env = brokerEnv(transportDir, idleExitMs);
  const spec = getBrokerLaunchSpec();
  const child = spawn(spec.command, spec.args, { env, stdio: ["ignore", "pipe", "pipe"] });
  const errors: string[] = [];
  child.stderr?.on("data", (chunk: Buffer) => errors.push(chunk.toString("utf8")));
  await waitForHealthy(transportDir).catch((error) => {
    throw new Error(`broker failed to start: ${error} ${errors.join("")}`);
  });
  const harness: Harness = {
    root, transportDir, socketPath: getBrokerSocketPath("darwin", transportDir), child,
    stop: async () => {
      child.kill("SIGTERM");
      await new Promise((resolve) => setTimeout(resolve, 100));
      if (!child.killed) child.kill("SIGKILL");
      rmSync(root, { recursive: true, force: true });
    },
  };
  harnesses.push(harness);
  return harness;
}

afterEach(async () => {
  while (harnesses.length) {
    const harness = harnesses.pop();
    if (harness) await harness.stop();
  }
});

function helloFor(member: string, launch: string, overrides: Record<string, unknown> = {}): RawMessage {
  return {
    type: "hello",
    protocol: SHOP_TRANSPORT_PROTOCOL_NAME,
    version: SHOP_TRANSPORT_PROTOCOL_VERSION,
    shop_id: "shop-1",
    run_id: "run-1",
    member_id: member,
    launch_id: launch,
    session_id: `${member}-session`,
    terminal_id: `${member}-terminal`,
    pane_id: `${member}-pane`,
    ...overrides,
  };
}

async function register(client: RawClient, member: string, launch: string, overrides: Record<string, unknown> = {}) {
  client.send(helloFor(member, launch, overrides));
  const reply = await client.next((message) => message.type === "hello_ok" || message.type === "hello_rejected");
  return reply;
}

function buildSend(options: {
  messageId: string;
  from: { member_id: string; launch_id: string; session_id: string; endpoint_epoch: string };
  to: { member_id: string; launch_id: string; endpoint_epoch: string };
  body?: string;
  replyTo?: string | null;
  kind?: string;
  extra?: Record<string, unknown>;
}) {
  const payload = {
    schema: "shop-transport-v1",
    message_id: options.messageId,
    shop_id: "shop-1",
    run_id: "run-1",
    sender: { member_id: options.from.member_id, launch_id: options.from.launch_id },
    recipient: { member_id: options.to.member_id, launch_id: options.to.launch_id },
    kind: options.kind ?? "note",
    reply_to: options.replyTo ?? null,
    body: { text: options.body ?? "hello" },
    body_sha256: "irrelevant-to-transport",
  };
  return {
    type: "send",
    message_id: options.messageId,
    from: options.from,
    to: options.to,
    kind: options.kind ?? "note",
    reply_to: options.replyTo ?? null,
    payload,
    payload_sha256: payloadFingerprint(payload),
    created_at: Date.now(),
    ...(options.extra ?? {}),
  };
}

function fromOf(helloOk: RawMessage, hello: RawMessage) {
  return {
    member_id: hello.member_id as string,
    launch_id: hello.launch_id as string,
    session_id: hello.session_id as string,
    endpoint_epoch: helloOk.endpoint_epoch as string,
  };
}

test("exact endpoint triple delivers, receipt routes back, no files outside the transport root", async () => {
  const harness = await startBroker();
  const alice = await RawClient.connect(harness.socketPath);
  const bob = await RawClient.connect(harness.socketPath);
  const aliceHello = helloFor("alice", "alice-launch-1");
  const bobHello = helloFor("bob", "bob-launch-1");
  const aliceOk = await register(alice, "alice", "alice-launch-1");
  const bobOk = await register(bob, "bob", "bob-launch-1");
  expect(aliceOk.type).toBe("hello_ok");
  expect(bobOk.type).toBe("hello_ok");
  expect(aliceOk.broker_epoch).toBe(bobOk.broker_epoch);

  alice.send(buildSend({
    messageId: "m-1", from: fromOf(aliceOk, aliceHello),
    to: { member_id: "bob", launch_id: "bob-launch-1", endpoint_epoch: bobOk.endpoint_epoch as string },
  }));
  const delivered = await alice.nextType("delivered");
  expect(delivered.message_id).toBe("m-1");
  expect(delivered.delivery).toBe("socket_delivered");

  const inbound = await bob.nextType("send");
  expect(inbound.message_id).toBe("m-1");
  expect((inbound.to as { launch_id: string }).launch_id).toBe("bob-launch-1");
  bob.send({ type: "receipt", message_id: "m-1", from_endpoint_epoch: bobOk.endpoint_epoch,
    status: "injected", at: Date.now() });
  const receipt = await alice.nextType("receipt");
  expect(receipt.status).toBe("injected");

  // Only transport files exist, and nothing was created outside the tmp root.
  const transportEntries = readdirSync(harness.transportDir).sort();
  expect(transportEntries).toContain("transport.sock");
  expect(transportEntries).toContain("broker.pid");
  const parentEntries = readdirSync(harness.root);
  expect(parentEntries).toEqual(["transport"]);
  expect(statSync(harness.transportDir).mode & 0o777).toBe(0o700);
  expect(statSync(getBrokerPidPath(harness.transportDir)).mode & 0o777).toBe(0o600);
  alice.close(); bob.close();
});

test("wrong member, wrong launch and stale epoch are refused without redirect", async () => {
  const harness = await startBroker();
  const alice = await RawClient.connect(harness.socketPath);
  const bob = await RawClient.connect(harness.socketPath);
  const aliceHello = helloFor("alice", "alice-launch-1");
  const aliceOk = await register(alice, "alice", "alice-launch-1");
  const bobOk = await register(bob, "bob", "bob-launch-1");
  const from = fromOf(aliceOk, aliceHello);

  const cases: Array<{ label: string; target: { member_id: string; launch_id: string; endpoint_epoch: string } }> = [
    { label: "wrong-member", target: { member_id: "carol", launch_id: "bob-launch-1", endpoint_epoch: bobOk.endpoint_epoch as string } },
    { label: "wrong-launch", target: { member_id: "bob", launch_id: "bob-launch-2", endpoint_epoch: bobOk.endpoint_epoch as string } },
    { label: "stale-epoch", target: { member_id: "bob", launch_id: "bob-launch-1", endpoint_epoch: "00000000-0000-0000-0000-000000000000" } },
  ];
  for (const { label, target } of cases) {
    alice.send(buildSend({ messageId: `m-${label}`, from, to: target }));
    const error = await alice.nextType("error");
    expect(error.code).toBe(label === "stale-epoch" ? "E_TARGET_STALE_EPOCH" : "E_TARGET_NOT_FOUND");
  }
  // Bob received nothing at all.
  await expect(bob.nextType("send", 300)).rejects.toThrow();
  alice.close(); bob.close();
});

test("self target and sender identity mismatch are refused", async () => {
  const harness = await startBroker();
  const alice = await RawClient.connect(harness.socketPath);
  const aliceHello = helloFor("alice", "alice-launch-1");
  const aliceOk = await register(alice, "alice", "alice-launch-1");
  const from = fromOf(aliceOk, aliceHello);
  alice.send(buildSend({
    messageId: "m-self", from,
    to: { member_id: "alice", launch_id: "alice-launch-1", endpoint_epoch: aliceOk.endpoint_epoch as string },
  }));
  expect((await alice.nextType("error")).code).toBe("E_SELF_TARGET");

  alice.send(buildSend({
    messageId: "m-forged",
    from: { ...from, member_id: "mallory" },
    to: { member_id: "bob", launch_id: "bob-launch-1", endpoint_epoch: "x" },
  }));
  expect((await alice.nextType("error")).code).toBe("E_RUN_MISMATCH");
  alice.close();
});

test("duplicate live registration is rejected and keeps the incumbent", async () => {
  const harness = await startBroker();
  const first = await RawClient.connect(harness.socketPath);
  const second = await RawClient.connect(harness.socketPath);
  const firstOk = await register(first, "bob", "bob-launch-1");
  expect(firstOk.type).toBe("hello_ok");
  const secondReply = await register(second, "bob", "bob-launch-1");
  expect(secondReply.type).toBe("hello_rejected");
  expect(secondReply.code).toBe("E_TARGET_DUPLICATE_REGISTRATION");
  first.close(); second.close();
});

test("same message id replays the recorded result; different body is a conflict", async () => {
  const harness = await startBroker();
  const alice = await RawClient.connect(harness.socketPath);
  const bob = await RawClient.connect(harness.socketPath);
  const aliceHello = helloFor("alice", "alice-launch-1");
  const aliceOk = await register(alice, "alice", "alice-launch-1");
  const bobOk = await register(bob, "bob", "bob-launch-1");
  const from = fromOf(aliceOk, aliceHello);
  const to = { member_id: "bob", launch_id: "bob-launch-1", endpoint_epoch: bobOk.endpoint_epoch as string };

  alice.send(buildSend({ messageId: "m-replay", from, to, body: "first" }));
  await alice.nextType("delivered");
  const inbound = await bob.nextType("send");
  expect(inbound.message_id).toBe("m-replay");

  alice.send(buildSend({ messageId: "m-replay", from, to, body: "first" }));
  const replay = await alice.nextType("delivered");
  expect(replay.replayed).toBe(true);
  await expect(bob.nextType("send", 300)).rejects.toThrow();

  alice.send(buildSend({ messageId: "m-replay", from, to, body: "second" }));
  expect((await alice.nextType("error")).code).toBe("E_MESSAGE_ID_REUSE");
  alice.close(); bob.close();
});

test("malformed, unknown-field and oversized payloads fail closed", async () => {
  const harness = await startBroker();
  const alice = await RawClient.connect(harness.socketPath);
  const aliceHello = helloFor("alice", "alice-launch-1");
  const aliceOk = await register(alice, "alice", "alice-launch-1");
  const from = fromOf(aliceOk, aliceHello);
  const to = { member_id: "bob", launch_id: "bob-launch-1", endpoint_epoch: "epoch" };

  alice.send({ type: "send", message_id: "m-bad" });
  expect((await alice.nextType("error")).code).toBe("E_MALFORMED");

  const extra = buildSend({ messageId: "m-extra", from, to, extra: { unknown_field: 1 } });
  alice.send(extra);
  expect((await alice.nextType("error")).code).toBe("E_MALFORMED");

  const huge = buildSend({ messageId: "m-huge", from, to, body: "x".repeat(40 * 1024) });
  const payload = huge.payload as Record<string, unknown>;
  (payload.body as Record<string, unknown>).text = "x".repeat(40 * 1024);
  huge.payload_sha256 = payloadFingerprint(payload);
  alice.send(huge);
  expect((await alice.nextType("error")).code).toBe("E_PAYLOAD_TOO_LARGE");
  alice.close();
});

test("rate limit burst is refused with retryable error and the broker stays responsive", async () => {
  const harness = await startBroker();
  const alice = await RawClient.connect(harness.socketPath);
  const aliceHello = helloFor("alice", "alice-launch-1");
  const aliceOk = await register(alice, "alice", "alice-launch-1");
  const from = fromOf(aliceOk, aliceHello);
  const to = { member_id: "bob", launch_id: "bob-launch-1", endpoint_epoch: "epoch" };
  for (let index = 0; index < 260; index += 1) {
    alice.send(buildSend({ messageId: `m-rate-${index}`, from, to }));
  }
  let limited = false;
  for (let index = 0; index < 260; index += 1) {
    const message = await alice.next((candidate) => candidate.type === "error" || candidate.type === "delivery_failed");
    if (message.type === "error" && message.code === "E_RATE_LIMITED") {
      expect(message.retryable).toBe(true);
      limited = true;
      break;
    }
  }
  expect(limited).toBe(true);
  // Still responsive afterwards.
  alice.send({ type: "health_check" });
  expect((await alice.nextType("health_ok")).protocol).toBe(SHOP_TRANSPORT_PROTOCOL_NAME);
  alice.close();
});

test("cancel works before injection and is refused after it; reply_to must be explicit and known", async () => {
  const harness = await startBroker();
  const alice = await RawClient.connect(harness.socketPath);
  const bob = await RawClient.connect(harness.socketPath);
  const aliceHello = helloFor("alice", "alice-launch-1");
  const aliceOk = await register(alice, "alice", "alice-launch-1");
  const bobOk = await register(bob, "bob", "bob-launch-1");
  const from = fromOf(aliceOk, aliceHello);
  const to = { member_id: "bob", launch_id: "bob-launch-1", endpoint_epoch: bobOk.endpoint_epoch as string };

  alice.send(buildSend({ messageId: "m-cancel", from, to }));
  await alice.nextType("delivered");
  await bob.nextType("send");
  alice.send({ type: "cancel", message_id: "m-cancel" });
  const cancelOk = await alice.nextType("cancel_result");
  expect(cancelOk.ok).toBe(true);
  expect((await bob.nextType("message_control")).action).toBe("cancelled");

  alice.send(buildSend({ messageId: "m-injected", from, to }));
  await alice.nextType("delivered");
  await bob.nextType("send");
  bob.send({ type: "receipt", message_id: "m-injected", from_endpoint_epoch: bobOk.endpoint_epoch,
    status: "injected", at: Date.now() });
  await alice.nextType("receipt");
  alice.send({ type: "cancel", message_id: "m-injected" });
  const tooLate = await alice.nextType("cancel_result");
  expect(tooLate.ok).toBe(false);
  expect(tooLate.code).toBe("E_CANCEL_TOO_LATE");
  expect(tooLate.outcome_known).toBe(false);

  alice.send(buildSend({ messageId: "m-reply", from, to, replyTo: "m-does-not-exist" }));
  expect((await alice.nextType("error")).code).toBe("E_UNKNOWN_MESSAGE");

  alice.send(buildSend({ messageId: "m-reply-ok", from, to, replyTo: "m-cancel" }));
  expect((await alice.nextType("delivered")).message_id).toBe("m-reply-ok");
  alice.close(); bob.close();
});

test("protocol version mismatch is rejected and never downgraded", async () => {
  const harness = await startBroker();
  const client = await RawClient.connect(harness.socketPath);
  const reply = await register(client, "alice", "alice-launch-1", { version: 99 });
  expect(reply.type).toBe("hello_rejected");
  expect(reply.code).toBe("E_VERSION_UNSUPPORTED");
  client.close();
});

test("a foreign protocol socket is never adopted or unlinked", async () => {
  const root = makeRoot();
  const transportDir = join(root, "transport");
  const socketPath = getBrokerSocketPath("darwin", transportDir);
  const { mkdirSync } = await import("node:fs");
  mkdirSync(transportDir, { recursive: true });
  const decoy: Server = createServer((socket) => {
    socket.on("data", () => writeMessage(socket, { type: "health_ok", protocol: "pi-intercom", version: 1 }));
  });
  await new Promise<void>((resolve) => decoy.listen(socketPath, resolve));
  try {
    const result = await spawnBrokerIfNeeded({ transportDir, env: brokerEnv(transportDir) })
      .then(() => "spawned").catch((error) => (error as { code?: string }).code ?? String(error));
    expect(result).toBe("E_FOREIGN_SOCKET");
    expect(existsSync(socketPath)).toBe(true);
  } finally {
    await new Promise<void>((resolve) => decoy.close(() => resolve()));
    rmSync(root, { recursive: true, force: true });
  }
});

test("killing the broker leaves stale files that the next launch replaces with a new broker epoch", async () => {
  const first = await startBroker(60_000);
  const firstHello = await (async () => {
    const client = await RawClient.connect(first.socketPath);
    const ok = await register(client, "alice", "alice-launch-1");
    client.close();
    return ok;
  })();
  first.child.kill("SIGKILL");
  await new Promise((resolve) => setTimeout(resolve, 200));
  expect(existsSync(first.socketPath)).toBe(true);

  const spawned = await spawnBrokerIfNeeded({ transportDir: first.transportDir, env: brokerEnv(first.transportDir) });
  expect(spawned.spawned).toBe(true);
  await waitForHealthy(first.transportDir);
  const client = await RawClient.connect(first.socketPath);
  const ok = await register(client, "alice", "alice-launch-1");
  expect(ok.type).toBe("hello_ok");
  expect(ok.broker_epoch).not.toBe(firstHello.broker_epoch);
  client.close();
});

test("idle broker exits by itself and cleans up its runtime files", async () => {
  const harness = await startBroker(400);
  const socketPath = harness.socketPath;
  const pidPath = getBrokerPidPath(harness.transportDir);
  const deadline = Date.now() + 8_000;
  while (Date.now() < deadline && (existsSync(socketPath) || existsSync(pidPath))) {
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  expect(existsSync(socketPath)).toBe(false);
  expect(existsSync(pidPath)).toBe(false);
});

test("broker launch resolves no external packages and needs no network", async () => {
  const spec = getBrokerLaunchSpec();
  expect(spec.command).toBe(process.execPath);
  expect(spec.args).toContain("--experimental-strip-types");
  expect(spec.args.some((arg) => /tsx|npx|npm|yarn|pnpm/.test(arg))).toBe(false);
  const root = makeRoot();
  const transportDir = join(root, "transport");
  const env = { PI_SHOP_TRANSPORT_DIR: transportDir, PATH: "/usr/bin:/bin",
    PI_SHOP_TRANSPORT_IDLE_EXIT_MS: "60000" };
  const child = spawn(spec.command, spec.args, { env, stdio: ["ignore", "pipe", "pipe"] });
  const harness: Harness = {
    root, transportDir, socketPath: getBrokerSocketPath("darwin", transportDir), child,
    stop: async () => {
      child.kill("SIGTERM");
      await new Promise((resolve) => setTimeout(resolve, 100));
      rmSync(root, { recursive: true, force: true });
    },
  };
  harnesses.push(harness);
  await waitForHealthy(transportDir);
  expect(existsSync(join(transportDir, "broker.pid"))).toBe(true);
});

test("a foreign upstream-style broker socket elsewhere is never contacted", async () => {
  const root = makeRoot();
  const upstreamDir = join(root, "upstream");
  writeFileSync(join(root, "keep"), "keep");
  const { mkdirSync } = await import("node:fs");
  mkdirSync(upstreamDir, { recursive: true });
  const upstreamSocket = join(upstreamDir, "broker.sock");
  let upstreamConnections = 0;
  const decoy: Server = createServer((socket) => {
    upstreamConnections += 1;
    socket.on("data", () => writeMessage(socket, { type: "health_ok", protocol: "pi-intercom", version: 1 }));
  });
  await new Promise<void>((resolve) => decoy.listen(upstreamSocket, resolve));
  const transportDir = join(root, "transport");
  try {
    const spec = getBrokerLaunchSpec();
    const child = spawn(spec.command, spec.args, {
      env: { PI_SHOP_TRANSPORT_DIR: transportDir, PATH: "/usr/bin:/bin", PI_SHOP_TRANSPORT_IDLE_EXIT_MS: "60000" },
      stdio: ["ignore", "pipe", "pipe"],
    });
    harnesses.push({ root, transportDir, socketPath: getBrokerSocketPath("darwin", transportDir), child,
      stop: async () => { child.kill("SIGTERM"); await new Promise((r) => setTimeout(r, 100));
        decoy.close(); rmSync(root, { recursive: true, force: true }); } });
    await waitForHealthy(transportDir);
    const client = await RawClient.connect(getBrokerSocketPath("darwin", transportDir));
    expect((await register(client, "alice", "alice-launch-1")).type).toBe("hello_ok");
    client.close();
    expect(upstreamConnections).toBe(0);
    expect(existsSync(upstreamSocket)).toBe(true);
  } finally {
    if (decoy.listening) await new Promise<void>((resolve) => decoy.close(() => resolve()));
  }
});

test("a rejected frame never reaches the recipient socket (pre-injection gate)", async () => {
  const harness = await startBroker();
  const alice = await RawClient.connect(harness.socketPath);
  const bob = await RawClient.connect(harness.socketPath);
  const aliceHello = helloFor("alice", "alice-launch-1");
  const aliceOk = await register(alice, "alice", "alice-launch-1");
  await register(bob, "bob", "bob-launch-1");
  alice.send(buildSend({
    messageId: "m-rejected", from: fromOf(aliceOk, aliceHello),
    to: { member_id: "bob", launch_id: "bob-launch-2", endpoint_epoch: "x" },
  }));
  expect((await alice.nextType("error")).code).toBe("E_TARGET_NOT_FOUND");
  await expect(bob.nextType("send", 300)).rejects.toThrow();
  alice.close(); bob.close();
});

test("extension wiring resolves member identity by pane and never guesses", async () => {
  const { findMember, readShopState, statePathFor } = await import("../extensions/transport.ts");
  const state = {
    shop_id: "s1", run_id: "r1", tab: "t1", phase: "ready",
    architect: { name: "arch", pane: "p1", launch_id: "l1", terminal_id: "t1" },
    lead: { name: "lead", pane: "p2", launch_id: "l2", terminal_id: "t2" },
    workers: [{ name: "wa", pane: "p3", launch_id: "l3", terminal_id: "t3" },
      { name: "wb", pane: "p4", launch_id: "l4", terminal_id: "t4" }],
  };
  expect(findMember(state, "p4")).toEqual({ member: state.workers[1], role: "worker" });
  expect(findMember(state, "p2")?.role).toBe("lead");
  expect(findMember(state, "p1")?.role).toBe("architect");
  expect(findMember(state, "p9")).toBeUndefined();
  expect(findMember(state, undefined)).toBeUndefined();

  const { mkdtempSync: mk, writeFileSync: wr, rmSync: rm } = await import("node:fs");
  const dir = mk(join(tmpdir(), "shop-ext-"));
  try {
    const good = join(dir, "state.json");
    wr(good, JSON.stringify(state));
    expect(readShopState(good)?.run_id).toBe("r1");
    const bad = join(dir, "bad.json");
    wr(bad, "{not json");
    expect(readShopState(bad)).toBeUndefined();
    expect(readShopState(join(dir, "missing.json"))).toBeUndefined();
  } finally {
    rm(dir, { recursive: true, force: true });
  }

  const bridge = { protocol: 1, core_root: "/core", state_dir: "/state", config_dir: "/config" };
  const env = { HERDR_SOCKET_PATH: "/socket", HERDR_TAB_ID: "t1" } as NodeJS.ProcessEnv;
  const expectedKey = createHash("sha256").update("/socket:t1").digest("hex").slice(0, 12);
  expect(statePathFor(bridge, env)).toBe(`/state/runtime/${expectedKey}.json`);
  expect(statePathFor(bridge, {} as NodeJS.ProcessEnv)).toBeUndefined();
});

test("a stale socket file from a dead broker is replaced without killing anything", async () => {
  const root = makeRoot();
  const transportDir = join(root, "transport");
  const { mkdirSync } = await import("node:fs");
  mkdirSync(transportDir, { recursive: true });
  const socketPath = getBrokerSocketPath("darwin", transportDir);
  writeFileSync(socketPath, "stale-socket-placeholder");
  try {
    const spawned = await spawnBrokerIfNeeded({ transportDir, env: brokerEnv(transportDir) });
    expect(spawned.spawned).toBe(true);
    await waitForHealthy(transportDir);
    const client = await RawClient.connect(socketPath);
    expect((await register(client, "alice", "alice-launch-1")).type).toBe("hello_ok");
    client.close();
  } finally {
    const childPid = Number(readFileSync(getBrokerPidPath(transportDir), "utf8").trim());
    try { process.kill(childPid, "SIGTERM"); } catch { /* already gone */ }
    rmSync(root, { recursive: true, force: true });
  }
});

// --- Attempt-2 focused regressions: scratch cleanup, unknown injection, lifecycle ---

interface FakeReceipt { message_id: string; from_endpoint_epoch: string; status: string; detail?: string }

function fakeClient(overrides: Record<string, unknown> = {}) {
  const receipts: FakeReceipt[] = [];
  return {
    receipts,
    endpoint_epoch: "epoch-1",
    broker_epoch: "broker-1",
    connected: true,
    sendReceipt: (receipt: FakeReceipt) => { receipts.push(receipt); },
    ...overrides,
  };
}

function decisionBody(overrides: Record<string, unknown> = {}) {
  return {
    decision: "inject", message_id: "m-clean", state: "received", body: "hello",
    kind: "note", reply_to: null, sender: { member_id: "lead", launch_id: "lead-launch-1" },
    ...overrides,
  };
}

test("inbound scratch files are removed on success, duplicate, reject and failure paths", async () => {
  const { handleInboundMessage } = await import("../extensions/transport.ts");
  const root = makeRoot();
  const scratchDir = join(root, "scratch");
  mkdirSync(scratchDir, { recursive: true });
  const { readdirSync: rd } = await import("node:fs");
  const scenarios: Array<{ label: string; outcome: "injected" | "duplicate" | "rejected" | "unknown"; firstStatus: string;
    exec: (args: string[]) => Promise<unknown>; sendMessage?: () => void }> = [
    { label: "success", outcome: "injected", firstStatus: "receiver_received",
      exec: async () => decisionBody({ message_id: "m-ok" }) },
    { label: "duplicate", outcome: "duplicate", firstStatus: "receiver_received",
      exec: async () => decisionBody({ decision: "duplicate", state: "received", message_id: "m-dup" }) },
    { label: "reject", outcome: "rejected", firstStatus: "rejected",
      exec: async () => decisionBody({ decision: "reject", code: "E_MALFORMED", detail: "bad shape", message_id: "m-rej" }) },
    { label: "verification-failure", outcome: "rejected", firstStatus: "rejected",
      exec: async () => { throw new Error("python unavailable"); } },
    { label: "injection-failure", outcome: "unknown", firstStatus: "receiver_received",
      exec: async () => decisionBody({ message_id: "m-inj" }),
      sendMessage: () => { throw new Error("sendMessage refused"); } },
  ];
  try {
    for (const scenario of scenarios) {
      const calls: string[][] = [];
      const client = fakeClient();
      const outcome = await handleInboundMessage({
        client: client as never,
        runId: "r1",
        statePath: join(root, "state.json"),
        selfName: "worker",
        selfPane: "p3",
        exec: async (args: string[]) => {
          calls.push(args);
          return await scenario.exec(args) as never;
        },
        sendMessage: scenario.sendMessage ?? (() => { /* delivered */ }),
        idle: () => true,
        scratchDir,
      }, { message_id: "m-" + scenario.label, payload: { schema: "shop-transport-v1", message_id: "m-" + scenario.label } });
      expect(outcome).toBe(scenario.outcome);
      expect(rd(scratchDir)).toEqual([]);
      expect(client.receipts.length).toBeGreaterThan(0);
      expect(client.receipts[0].status).toBe(scenario.firstStatus);
    }
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

test("injection failure is durable unknown and never claims a business rejection", async () => {
  const { handleInboundMessage } = await import("../extensions/transport.ts");
  const root = makeRoot();
  const calls: string[][] = [];
  const client = fakeClient();
  try {
    const outcome = await handleInboundMessage({
      client: client as never,
      runId: "r1",
      statePath: join(root, "state.json"),
      selfName: "worker",
      selfPane: "p3",
      exec: async (args: string[]) => {
        calls.push(args);
        if (args[0] === "receive") return decisionBody({ message_id: "m-unknown" }) as never;
        return { decision: "inject", message_id: "m-unknown", state: "unknown" } as never;
      },
      sendMessage: () => { throw new Error("mid-flight failure"); },
      idle: () => false,
      scratchDir: root,
    }, { message_id: "m-unknown", payload: { schema: "shop-transport-v1" } });
    expect(outcome).toBe("unknown");
    const statuses = client.receipts.map((receipt) => receipt.status);
    expect(statuses).toEqual(["receiver_received", "receiver_received"]);
    expect(statuses).not.toContain("rejected");
    expect(statuses).not.toContain("injected");
    // The unknown detail must not assert either outcome, only that no retry happens.
    expect(client.receipts[1].detail).toBe("injection outcome unknown; no automatic retry");
    expect(client.receipts[1].detail).not.toContain("not injected");
    expect(client.receipts[1].detail).not.toContain("injected");
    const receiptCall = calls.find((args) => args[0] === "receipt");
    expect(receiptCall).toBeDefined();
    expect(receiptCall?.[receiptCall.indexOf("--status") + 1]).toBe("unknown");
    // Busy context must steer, never abort.
    expect(calls.some((args) => args[0] === "receive")).toBe(true);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
});

function lifecycleFixture(root: string) {
  const mk = mkdirSync;
  const wr = writeFileSync;
  const stateDir = join(root, "state");
  const runtime = join(stateDir, "runtime");
  mk(runtime, { recursive: true });
  const env = {
    HERDR_ENV: "1", HERDR_PANE_ID: "w1:p3", HERDR_TAB_ID: "w1:t1",
    HERDR_SOCKET_PATH: "/tmp/socket", SHOP_STATE_DIR: stateDir,
    SHOP_LOCATOR: join(root, "bridge.json"),
  } as unknown as NodeJS.ProcessEnv;
  const locator = env.SHOP_LOCATOR as string;
  wr(locator, JSON.stringify({
    protocol: 1, core_root: root, state_dir: stateDir, config_dir: join(root, "config"),
  }));
  const key = createHash("sha256").update("/tmp/socket:w1:t1").digest("hex").slice(0, 12);
  wr(join(runtime, `${key}.json`), JSON.stringify({
    shop_id: "s1", run_id: "r1", tab: "w1:t1", phase: "ready", cwd: root,
    lead: { name: "lead", pane: "w1:p2", launch_id: "l1", terminal_id: "t1" },
    workers: [{ name: "worker", pane: "w1:p3", launch_id: "w1", terminal_id: "t3" }],
  }));
  return { env, locator };
}

test("repeated session_start is idempotent and stop/start never orphans a client", async () => {
  const { startShopTransport, stopShopTransport } = await import("../extensions/transport.ts");
  const root = makeRoot();
  const { env, locator } = lifecycleFixture(root);
  const clients: Array<{ connects: number; disconnects: number; connect: () => Promise<void>;
    disconnect: () => Promise<void>; onMessage: () => void; onMessageControl: () => void;
    sendReceipt: () => void; endpoint_epoch: string | null; broker_epoch: string | null; connected: boolean }> = [];
  const createClient = () => {
    const client = {
      connects: 0, disconnects: 0,
      connect: async () => { client.connects += 1; },
      disconnect: async () => { client.disconnects += 1; },
      onMessage: () => { /* handler unused in this test */ },
      onMessageControl: () => { /* handler unused in this test */ },
      sendReceipt: () => { /* no receipts expected */ },
      endpoint_epoch: "epoch", broker_epoch: "broker", connected: true,
    };
    clients.push(client);
    return client;
  };
  const ctx = { isIdle: () => true, hasUI: false, ui: { setStatus: () => {}, notify: () => {} },
    sessionManager: { getSessionFile: () => "/tmp/session.jsonl" } };
  const previousLocator = process.env.SHOP_LOCATOR;
  process.env.SHOP_LOCATOR = locator;
  try {
    await stopShopTransport();
    const first = await startShopTransport({} as never, ctx as never, { env, createClient: createClient as never });
    expect(first.started).toBe(true);
    const second = await startShopTransport({} as never, ctx as never, { env, createClient: createClient as never });
    expect(second).toEqual({ started: false, reason: "already-connected" });
    expect(clients.length).toBe(1);
    expect(clients[0].connects).toBe(1);
    expect(clients[0].disconnects).toBe(0);

    await stopShopTransport();
    expect(clients[0].disconnects).toBe(1);

    const third = await startShopTransport({} as never, ctx as never, { env, createClient: createClient as never });
    expect(third.started).toBe(true);
    expect(clients.length).toBe(2);

    // Start is in flight, then a shutdown arrives: the late client must be stopped.
    let release: () => void = () => {};
    const gate = new Promise<void>((resolve) => { release = resolve; });
    const raced = createClient();
    raced.connect = async () => { raced.connects += 1; await gate; };
    await stopShopTransport();
    const pendingStart = startShopTransport({} as never, ctx as never, {
      env, createClient: (() => raced) as never,
    });
    await stopShopTransport();
    release();
    const racedResult = await pendingStart;
    expect(racedResult).toEqual({ started: false, reason: "superseded" });
    expect(raced.disconnects).toBe(1);
    expect(clients[clients.length - 1]).toBe(raced);
  } finally {
    await stopShopTransport();
    if (previousLocator === undefined) delete process.env.SHOP_LOCATOR;
    else process.env.SHOP_LOCATOR = previousLocator;
    rmSync(root, { recursive: true, force: true });
  }
});
