import { afterEach, beforeEach, expect, test } from "bun:test";
import { mkdtempSync, mkdirSync, writeFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { createHash } from "node:crypto";
import { t, type Language } from "../extensions/i18n.ts";
import { visibleWidth } from "@earendil-works/pi-tui";
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
  Object.assign(process.env, { LC_ALL: "en_US.UTF-8", HERDR_ENV: "1", HERDR_PANE_ID: "p1", HERDR_TAB_ID: "t1",
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
    ui: { setStatus() {}, notify() {}, select: async (_title: string, choices: string[]) => choices.at(-1) } };
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

const routes = [
  { menu: "Status and identity", choices: [], inputs: [], actions: [] },
  { menu: "Propose handoff", choices: ["lead"], inputs: ["目标 original", "scope", "checks", ""], actions: ["handoff-propose"] },
  { menu: "Development preparation", choices: [], inputs: ["/tmp/new-worktree", "new-branch", ""], actions: ["development-preview", "development-create"] },
  { menu: "Delivery and integration", choices: ["Preview integration"], inputs: ["T001"], actions: ["delivery", "integration-preview", "integrate"] },
  { menu: "Idle-seat configuration", choices: ["lead", "p/model", "high"], inputs: [], actions: ["profile-request"] },
  { menu: "Task intervention", choices: ["Request pause (pause)"], inputs: ["原因 original", "T001"], actions: ["intervention-request"] },
  { menu: "Inbox and receipts", choices: ["h1 · handoff · proposed", "Accept (accept)"], inputs: ["说明 original"], actions: ["handoff-transition"] },
  { menu: "Diagnostics and redacted export", choices: [], inputs: [], actions: ["diagnostics"], confirm: false },
  { menu: "Propose handoff", choices: ["lead"], inputs: ["objective", "scope", "checks", ""], actions: [], confirm: false },
  { menu: "Development preparation", choices: [], inputs: ["/tmp/new-worktree", "new-branch", ""], actions: ["development-preview"], confirm: false },
  { menu: "Delivery and integration", choices: ["Preview integration"], inputs: ["T001"], actions: ["delivery", "integration-preview"], confirm: false },
];
for (const language of ["en", "zh-CN"] as Language[]) for (const route of routes) {
  test(`${language} UI ${route.menu} (${route.confirm === false ? "cancel" : "confirm"}) uses canonical operations`, async () => {
    mkdirSync(join(root, "config"));
    writeFileSync(join(root, "config/language.json"), JSON.stringify({ version: 1, language }));
    const choices = [route.menu, ...route.choices, "Exit"].map(key => t(key, [], language));
    const inputs = [...route.inputs];
    const requests: any[] = [];
    pi.exec = async (_binary: string, args: string[]) => {
      const request = JSON.parse(args[args.indexOf("--request") + 1]);
      requests.push(request);
      const data = request.action === "view" ? { actor: "architect", snapshot: { shop: state, members: [{ name: "lead" }] },
        records: [{ id: "h1", kind: "handoff", status: "proposed", recipient: { name: "architect" } }], handoffs: [], models: {} }
        : { id: "plan-1", target_commit: "commit-original", status: "requested" };
      return { code: 0, stdout: JSON.stringify(data), stderr: "" };
    };
    ctx.modelRegistry = { getAvailable: () => [{ provider: "p", id: "model", reasoning: true }] };
    const notices: string[] = [];
    ctx.ui.notify = (text: string) => notices.push(text);
    ctx.ui.select = async (_title: string, options: string[]) => {
      const choice = choices.shift();
      if (choice === undefined) throw new Error("Unexpected extra menu");
      expect(options).toContain(choice);
      return choice;
    };
    ctx.ui.input = async () => inputs.shift();
    ctx.ui.confirm = async () => route.confirm !== false;
    ctx.ui.custom = async (factory: any) => {
      const panel = factory({ terminal: { rows: 24 }, requestRender() {} }, { fg: (_style: string, text: string) => text }, {}, () => {});
      for (const width of [4, 12, 40]) for (const line of panel.render(width)) expect(visibleWidth(line)).toBeLessThanOrEqual(width);
      panel.handleInput("\x1b");
    };
    await workbenchUI(pi, ctx);
    expect(choices).toHaveLength(0);
    expect(inputs).toHaveLength(0);
    expect(notices).toEqual([]);
    const operations = requests.filter(r => r.action !== "view");
    expect(operations.map(r => r.action)).toEqual(route.actions);
    for (const request of operations) {
      expect(request.expected).toEqual({ shop_id: "s1", run_id: "r1", actor: "architect", launch_id: "a1", session_id: "session1" });
      if (request.action === "handoff-propose") expect(request.objective).toBe("目标 original");
      if (request.action === "handoff-transition") expect(request.transition).toBe("accept");
      if (request.action === "intervention-request") { expect(request.kind).toBe("pause"); expect(request.text).toBe("原因 original"); }
      if (request.action === "integrate") expect(request.writers_stopped).toBe(true);
      if (request.action === "profile-request") expect(request.profile).toEqual({ model: "p/model", thinking: "high" });
    }
  });
}
