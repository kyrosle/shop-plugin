import { afterEach, beforeEach, expect, test } from "bun:test";
import { mkdtempSync, mkdirSync, writeFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { registerResetCommand } from "../extensions/reset-ui.ts";

let root: string, old: NodeJS.ProcessEnv;
beforeEach(() => {
  old = { ...process.env };
  root = mkdtempSync(join(tmpdir(), "shop-reset-ui-"));
  mkdirSync(join(root, "config"));
  process.env.SHOP_LOCATOR = join(root, "bridge.json");
  process.env.LC_ALL = "en_US.UTF-8";
  Object.assign(process.env, { HERDR_ENV: "1", HERDR_SOCKET_PATH: "socket", HERDR_TAB_ID: "test-tab", HERDR_PANE_ID: "p1" });
  writeFileSync(process.env.SHOP_LOCATOR, JSON.stringify({ protocol: 1, core_root: resolve("."),
    config_dir: join(root, "config"), state_dir: join(root, "state") }));
});
afterEach(() => {
  for (const key of Object.keys(process.env)) if (!(key in old)) delete process.env[key];
  Object.assign(process.env, old);
  rmSync(root, { recursive: true, force: true });
});
function fixture() {
  let command: any, session = "session";
  const hooks: Record<string, Function> = {}, calls: any[] = [], notices: string[] = [], confirmations: string[] = [];
  const plan: any = { schema: 1, decision: "ready", token: "f".repeat(64), tab: "test-tab", pane: "p1",
    recorded_name: "s123-architect", observed_name: null, original_error: "original pane rename failure {0}" };
  const pi: any = {
    on(name: string, hook: Function) { hooks[name] = hook; },
    registerCommand(name: string, value: any) { expect(name).toBe("shop-reset"); command = value; },
    async exec(binary: string, args: string[]) {
      expect(binary).toBe("python3"); expect(args[0]).toEndWith("core/reset.py");
      const request = JSON.parse(args[2]); calls.push(request);
      return { code: 0, stdout: JSON.stringify(request.action === "preview" ? plan : {
        schema: 1, decision: "archived", archive: "/fixture/reset-archive/record.json", pane: "p1", tab: "test-tab",
      }), stderr: "", killed: false };
    },
  };
  const ctx: any = { cwd: root, hasUI: true, mode: "tui", isIdle: () => true,
    sessionManager: { getSessionId: () => session },
    ui: { notify: (text: string) => notices.push(text), confirm: async (title: string, text: string) => {
      confirmations.push(title + "\n" + text); return true;
    } } };
  registerResetCommand(pi);
  return { command, pi, ctx, calls, notices, confirmations, hooks, plan, changeSession() { session = "new-session"; } };
}

for (const language of ["en", "zh-CN"]) test(`${language} reset requires preview and confirmation; stable payload only`, async () => {
  writeFileSync(join(root, "config/language.json"), JSON.stringify({ version: 1, language }));
  const f = fixture();
  await f.command.handler("", f.ctx);
  expect(f.calls.map(c => c.action)).toEqual(["preview", "apply"]);
  expect(f.calls[1].confirmed).toBe(true);
  expect(f.calls[1].token).toBe(f.plan.token);
  expect(f.calls.every(c => c.expected_state_dir === join(root, "state"))).toBe(true);
  expect(f.confirmations).toHaveLength(1);
  expect(f.confirmations[0]).toContain("test-tab");
  expect(f.confirmations[0]).toContain("original pane rename failure {0}");
  expect(f.confirmations[0]).toContain(language === "en" ? "Close 0 panes" : "关闭 0 个 pane");
  expect(f.notices.at(-1)).toContain("/fixture/reset-archive/record.json");
});

test("cancel, missing state and blocked plans never apply", async () => {
  const f = fixture();
  f.ctx.ui.confirm = async () => false;
  await f.command.handler("", f.ctx);
  expect(f.calls.map(c => c.action)).toEqual(["preview"]);
  f.plan.decision = "empty";
  await f.command.handler("", f.ctx);
  f.plan.decision = "blocked"; f.plan.reason = "identity_changed";
  await f.command.handler("", f.ctx);
  expect(f.calls.every(c => c.action === "preview")).toBe(true);
  expect(f.notices.at(-1)).toContain("cannot adopt a different agent");
});

test("unsupported arguments, busy turns and non-TUI contexts never call core", async () => {
  const f = fixture();
  await f.command.handler("--force", f.ctx);
  f.ctx.mode = "rpc";
  await f.command.handler("", f.ctx);
  f.ctx.mode = "tui"; f.ctx.isIdle = () => false;
  await f.command.handler("", f.ctx);
  expect(f.calls).toEqual([]);
});

test("session, bridge, tab and lifecycle changes after confirmation stop apply", async () => {
  for (const change of ["session", "bridge", "tab", "tree", "shutdown", "busy"]) {
    const f = fixture();
    const bridgeText = JSON.stringify({ protocol: 1, core_root: resolve("."), config_dir: join(root, "config"), state_dir: join(root, "state") });
    writeFileSync(process.env.SHOP_LOCATOR!, bridgeText);
    process.env.HERDR_TAB_ID = "test-tab";
    f.ctx.ui.confirm = async () => {
      if (change === "session") f.changeSession();
      if (change === "bridge") writeFileSync(process.env.SHOP_LOCATOR!, bridgeText.replace('"protocol":1', '"protocol":2'));
      if (change === "tab") process.env.HERDR_TAB_ID = "other-tab";
      if (change === "tree") f.hooks.session_tree();
      if (change === "shutdown") f.hooks.session_shutdown();
      if (change === "busy") f.ctx.isIdle = () => false;
      return true;
    };
    await f.command.handler("", f.ctx);
    expect(f.calls.map(c => c.action)).toEqual(["preview"]);
    expect(f.notices).toHaveLength(1);
  }
});

test("wrong-tab responses cannot reach confirmation or apply", async () => {
  const f = fixture();
  f.plan.tab = "unrelated-tab";
  await f.command.handler("", f.ctx);
  expect(f.calls.map(c => c.action)).toEqual(["preview"]);
  expect(f.confirmations).toHaveLength(0);
  expect(f.notices.at(-1)).toContain("Invalid reset response");
});

test("long original errors do not bury confirmation safety details", async () => {
  const f = fixture();
  f.plan.original_error = "multi-line error\n".repeat(1000);
  await f.command.handler("", f.ctx);
  expect(f.confirmations[0].length).toBeLessThan(1200);
  expect(f.confirmations[0]).toContain("Close 0 panes");
  expect(f.confirmations[0]).toContain("No automatic setup follows");
});

test("unknown apply outcome is reported once and never retried", async () => {
  const f = fixture(), original = f.pi.exec;
  f.pi.exec = async (...args: any[]) => {
    const result = await original(...args);
    return f.calls.at(-1).action === "apply" ? { ...result, code: 1, stdout: "", stderr: "fixture outcome unknown" } : result;
  };
  await f.command.handler("", f.ctx);
  expect(f.calls.map(c => c.action)).toEqual(["preview", "apply"]);
  expect(f.notices.at(-1)).toContain("outcome unknown");
});

test("a second reset command cannot run while confirmation is open", async () => {
  const f = fixture();
  let release!: (value: boolean) => void;
  f.ctx.ui.confirm = () => new Promise<boolean>(r => { release = r; });
  const first = f.command.handler("", f.ctx);
  await Promise.resolve(); await Promise.resolve();
  await f.command.handler("", f.ctx);
  release(false);
  await first;
  expect(f.calls).toHaveLength(1);
  expect(f.notices.at(-1)).toContain("reset command to finish");
});
