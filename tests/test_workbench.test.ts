import { afterEach, beforeEach, expect, test } from "bun:test";
import { mkdtempSync, mkdirSync, writeFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { createHash } from "node:crypto";
import { workbenchClient, workbenchUI, submitPrepared } from "../extensions/workbench-ui.ts";
import { startShopTransport, stopShopTransport, sendShopEnvelope, handleInboundMessage } from "../extensions/transport.ts";
import { publishEndpoint, readEndpoint, endpointPath } from "../extensions/endpoints.ts";

let root: string;
let original: NodeJS.ProcessEnv;
let statePath: string;
let state: any;
let calls: any[];
let ctx: any;
let pi: any;
let sid: string;

beforeEach(() => {
  original = { ...process.env };
  root = mkdtempSync(join(tmpdir(), "shop-workbench-ui-"));
  Object.assign(process.env, { HERDR_ENV: "1", HERDR_PANE_ID: "p1", HERDR_TAB_ID: "t1",
    HERDR_SOCKET_PATH: "/tmp/shop-test-only", SHOP_LOCATOR: join(root, "bridge.json") });
  writeFileSync(process.env.SHOP_LOCATOR!, JSON.stringify({ protocol: 1, core_root: join(root, "package"),
    state_dir: root, config_dir: join(root, "config") }));
  const key = createHash("sha256").update("/tmp/shop-test-only:t1").digest("hex").slice(0, 12);
  mkdirSync(join(root, "runtime"));
  statePath = join(root, "runtime", `${key}.json`);
  state = { shop_id: "s1", run_id: "r1", phase: "ready", cwd: join(root, "main-repo"), tab: "t1",
    architect: { name: "architect", pane: "p1", launch_id: "a1", terminal_id: "term1" },
    lead: { name: "lead", pane: "p2", launch_id: "l1", terminal_id: "term2" }, workers: [] };
  writeFileSync(statePath, JSON.stringify(state));
  calls = [];
  sid = "session1";
  ctx = { hasUI: true, isIdle: () => true, sessionManager: { getSessionId: () => sid },
    ui: { setStatus() {}, notify() {}, select: async () => "退出" } };
  pi = { exec: async (_command: string, args: string[]) => {
    calls.push(args);
    return { code: 0, stdout: JSON.stringify({ actor: "architect", snapshot: { shop: state, members: [] },
      records: [], handoffs: [], models: {} }), stderr: "" };
  } };
});
afterEach(async () => {
  await stopShopTransport();
  for (const key of Object.keys(process.env)) if (!(key in original)) delete process.env[key];
  Object.assign(process.env, original);
  rmSync(root, { recursive: true, force: true });
});

test("dashboard open/escape performs only a read, no dispatch/model/Herdr control", async () => {
  await workbenchUI(pi, ctx);
  expect(calls.length).toBe(1);
  const request = JSON.parse(calls[0][calls[0].indexOf("--request") + 1]);
  expect(request.action).toBe("view");
  expect(request.expected.session_id).toBe("session1");
});

test("UI callback fences session/run changes before subprocess mutation", async () => {
  const call = workbenchClient(pi, ctx);
  sid = "new-session";
  await expect(call({ action: "profile-request" })).rejects.toThrow("changed");
  expect(calls.length).toBe(0);
  sid = "session1";
  state.run_id = "other";
  writeFileSync(statePath, JSON.stringify(state));
  await expect(call({ action: "development-create" })).rejects.toThrow("changed");
  expect(calls.length).toBe(0);
});

test("structured core errors expose code and actionable next step", async () => {
  pi.exec = async () => ({ code: 1, stdout: "", stderr: JSON.stringify({ code: "E_STALE_PLAN", reason: "HEAD changed", next_action: "Refresh preview" }) });
  await expect(workbenchClient(pi, ctx)({ action: "view" })).rejects.toThrow("Refresh preview");
});

test("non-message operations never trigger transport", async () => {
  expect(await submitPrepared(pi, ctx, { status: "requested" })).toEqual({ status: "requested" });
  expect(calls).toEqual([]);
});

test("endpoint discovery fails closed on expiry and wrong identity", () => {
  const identity = { shop_id: "s1", run_id: "r1", member_id: "lead", launch_id: "l1", session_id: "lead-session",
    terminal_id: "term2", pane_id: "p2" };
  publishEndpoint(root, identity, "epoch2", "broker");
  expect(readEndpoint(root, "s1", "lead")?.endpoint_epoch).toBe("epoch2");
  writeFileSync(endpointPath(root, "s1", "lead"), JSON.stringify({ ...identity, expires_at: 0 }));
  expect(readEndpoint(root, "s1", "lead")).toBeUndefined();
});

test("Pi sender uses pinned message ID, exact target epoch and MAIN repo for receipts", async () => {
  let sent: any;
  const client = { connected: true, endpoint_epoch: "source-epoch", broker_epoch: "broker",
    connect: async () => {}, disconnect: async () => {}, onMessage() {}, onMessageControl() {}, sendReceipt() {},
    send: async (request: any) => { sent = request; return { outcome: "delivered", message_id: request.message_id }; } };
  await startShopTransport(pi, ctx, { createClient: (() => client) as never });
  publishEndpoint(root, { shop_id: "s1", run_id: "r1", member_id: "lead", launch_id: "l1", session_id: "lead-session",
    terminal_id: "term2", pane_id: "p2" }, "epoch2", "broker");
  const envelope = { schema: "shop-transport-v1", message_id: "pinned-id", shop_id: "s1", run_id: "r1",
    sender: { member_id: "architect", launch_id: "a1" }, recipient: { member_id: "lead", launch_id: "l1" },
    kind: "note", reply_to: null, body: { text: "hello" }, body_sha256: createHash("sha256").update("hello").digest("hex") };
  const outcome = await sendShopEnvelope(pi, ctx, envelope);
  expect(outcome.outcome).toBe("delivered");
  expect(sent.message_id).toBe("pinned-id");
  expect(sent.to).toEqual({ member_id: "lead", launch_id: "l1", endpoint_epoch: "epoch2" });
  expect(calls[0][calls[0].indexOf("--repo") + 1]).toBe(state.cwd);
  expect(calls[0][0]).toBe(join(root, "package/bin/shop-transport"));
  publishEndpoint(root, { shop_id: "s1", run_id: "r1", member_id: "lead", launch_id: "replacement", session_id: "new",
    terminal_id: "term2", pane_id: "p2" }, "epoch3", "broker");
  await expect(sendShopEnvelope(pi, ctx, envelope)).rejects.toThrow("E_TARGET_STALE_EPOCH");
  expect(calls.length).toBe(1);
});

test("receiver session change during validation preserves unknown without injection", async () => {
  let current = true;
  let injected = 0;
  const outcome = await handleInboundMessage({ runId: "r1", statePath, selfName: "architect", selfPane: "p1", scratchDir: root,
    client: { endpoint_epoch: "e1", sendReceipt() {} }, idle: () => true, current: () => current,
    sendMessage() { injected++; }, exec: async (args) => {
      calls.push(args);
      if (args[0] === "receive") { current = false; return { decision: "inject", body: "old-session text" }; }
      return { decision: "duplicate" };
    } }, { message_id: "in-flight", payload: {} });
  expect(outcome).toBe("unknown");
  expect(injected).toBe(0);
  expect(calls[1]).toContain("unknown");
});

test("wire/body sender mismatch rejected before Python or model injection", async () => {
  let receive: (message: any) => void = () => {};
  const receipts: any[] = [];
  const client = { connected: true, endpoint_epoch: "source-epoch", broker_epoch: "broker",
    connect: async () => {}, disconnect: async () => {}, onMessage(handler: any) { receive = handler; },
    onMessageControl() {}, sendReceipt(receipt: any) { receipts.push(receipt); } };
  await startShopTransport(pi, ctx, { createClient: (() => client) as never });
  receive({ message_id: "m1", from: { member_id: "spoof", launch_id: "l1" },
    to: { member_id: "architect", launch_id: "a1", endpoint_epoch: "source-epoch" }, kind: "note", reply_to: null,
    payload: { message_id: "m1", kind: "note", reply_to: null, sender: { member_id: "lead", launch_id: "l1" },
      recipient: { member_id: "architect", launch_id: "a1" }, body: { text: "must not inject" } } });
  expect(receipts[0].status).toBe("rejected");
  expect(calls).toEqual([]);
});
