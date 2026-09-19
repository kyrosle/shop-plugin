import { test, expect } from "bun:test";
import { mkdtempSync, mkdirSync, writeFileSync, readFileSync, existsSync, rmSync, realpathSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { spawnSync } from "node:child_process";
import { getKeybindings, visibleWidth } from "@earendil-works/pi-tui";
import { ModelPicker } from "../extensions/model-picker.ts";
import { ConfigPublisher, SESSION_CONFIG, candidatePath, configCall, configContext, configRequest,
  hash, publishCandidate, removeCandidate, sessionSettings, type ConfigView, type Profiles, SEATS } from "../extensions/configuration.ts";
import { editConfiguration, settingsPanel, validateModels } from "../extensions/settings-ui.ts";

const model = { provider: "p", id: "model", api: "openai-completions", reasoning: true } as any;
const entries = (root: string, overrides: any, id = "entry") => [{ type: "custom", customType: SESSION_CONFIG,
  id, data: { version: 1, project_root: root, overrides } }] as any;
const profiles = (profile: any = {}): Profiles => Object.fromEntries(SEATS.map(s => [s, { ...profile }])) as Profiles;

test("session settings follow current branch, root and empty reset; unknown versions refuse", () => {
  const first = entries("/repo", { models: { lead: "p/model" } });
  expect(sessionSettings(first, "/repo").overrides.models?.lead).toBe("p/model");
  expect(sessionSettings(first, "/elsewhere").overrides).toEqual({});
  const reset = [...first, ...entries("/repo", {}, "reset")];
  expect(sessionSettings(reset, "/repo").overrides).toEqual({});
  expect(sessionSettings(first, "/repo").marker).toStartWith("entry:");
  expect(sessionSettings([], "/repo").overrides).toEqual({});
  expect(() => sessionSettings([{ ...first[0], data: { version: 2 } }], "/repo")).toThrow("version");
});

async function fixture(fn: (f: any) => Promise<void>) {
  const root = realpathSync(mkdtempSync(join(tmpdir(), "shop-config-")));
  const old = { ...process.env };
  const bridge = { protocol: 1, core_root: resolve("."), state_dir: join(root, "state"), config_dir: join(root, "config") };
  mkdirSync(bridge.config_dir); mkdirSync(bridge.state_dir);
  const env = { LC_ALL: "zh_CN.UTF-8", SHOP_LOCATOR: join(root, "bridge.json"), SHOP_CONFIG_DIR: bridge.config_dir, SHOP_STATE_DIR: bridge.state_dir,
    HERDR_ENV: "1", HERDR_SOCKET_PATH: "/test/socket", HERDR_TAB_ID: "tab", HERDR_PANE_ID: "pane" };
  writeFileSync(env.SHOP_LOCATOR, JSON.stringify(bridge));
  Object.assign(process.env, env);
  let branch: any[] = [], id = "session", trusted = true;
  const calls: any[] = [], notices: string[] = [], saved: any[] = [];
  const ctx: any = { cwd: root, mode: "tui", hasUI: true, isIdle: () => true,
    isProjectTrusted: () => trusted,
    sessionManager: { getSessionId: () => id, getBranch: () => branch },
    modelRegistry: { getAvailable: () => [model] },
    ui: { notify: (text: string) => notices.push(text), confirm: async () => true,
      custom: async () => ({ type: "cancel" }), select: async () => undefined } };
  const pi: any = {
    exec: async (command: string, args: string[]) => {
      const request = JSON.parse(args[args.indexOf("--request") + 1]);
      calls.push(request);
      if (request.action === "identify") return { code: 0, stdout: JSON.stringify({ terminal: "terminal" }) };
      const result = spawnSync(command, args, { env: process.env, encoding: "utf8" });
      return { code: result.status, stdout: result.stdout, stderr: result.stderr };
    },
    appendEntry: (customType: string, data: any) => {
      const entry = { type: "custom", customType, id: `saved-${saved.length}`, data };
      saved.push(entry); branch = [...branch, entry];
    },
  };
  const publisher = new ConfigPublisher(pi);
  try {
    await fn({ root, bridge, env, pi, ctx, calls, notices, saved, publisher,
      branch: (next: any[]) => { branch = next; }, session: (next: string) => { id = next; },
      trust: (next: boolean) => { trusted = next; } });
  } finally {
    publisher.stop();
    for (const key of Object.keys(process.env)) if (!(key in old)) delete process.env[key];
    Object.assign(process.env, old); rmSync(root, { recursive: true, force: true });
  }
}

function modelDialogs(f: any, steps: any[], pick = "p/model") {
  f.ctx.ui.select = async () => { throw new Error("Model picker must not use a plain select dialog"); };
  f.ctx.ui.custom = async (factory: any) => {
    let result: any;
    const component = factory({ requestRender() {}, terminal: { rows: 24 } },
      { fg: (_: string, text: string) => text }, getKeybindings(), (value: any) => { result = value; });
    if (component instanceof ModelPicker) {
      component.handleInput(pick);
      component.handleInput("\r");
      return result;
    }
    return steps.shift();
  };
}

test("directory trust and registered main root never follow an auxiliary worktree", async () => fixture(async f => {
  const auxiliary = join(f.root, "auxiliary"); mkdirSync(auxiliary);
  const runtime = join(f.bridge.state_dir, "runtime"); mkdirSync(runtime);
  writeFileSync(join(runtime, hash("/test/socket:tab").slice(0, 12) + ".json"), JSON.stringify({
    tab: "tab", cwd: f.root, architect: { pane: "architect" },
  }));
  f.ctx.cwd = auxiliary;
  expect(configContext(f.bridge, f.ctx)).toEqual({ root: f.root, trusted: false, architect: false, existingShop: true });
}));

test("Python configuration service is consumed by Pi; sparse session save is not a message", async () => fixture(async f => {
  modelDialogs(f, [{ type: "edit", seat: "lead", field: "model" }, { type: "save" }]);
  await editConfiguration(f.pi, f.ctx, f.publisher);
  expect(f.saved).toHaveLength(1);
  expect(f.saved[0].customType).toBe(SESSION_CONFIG);
  expect(f.saved[0].data.overrides).toEqual({ models: { seats: { lead: { model: "p/model" } } } });
  expect(existsSync(join(f.bridge.config_dir, "settings.json"))).toBe(false);
  expect(existsSync(join(f.root, ".pi"))).toBe(false);
  expect(f.notices.join(" ")).toContain("现有工位未修改");
}));

test("model picker cancel and inherit never change native Pi model or defaults", async () => fixture(async f => {
  f.pi.setModel = () => { throw new Error("Must not switch Architect model"); };
  modelDialogs(f, [{ type: "edit", seat: "lead", field: "model" }, { type: "cancel" }], "\x1b");
  await editConfiguration(f.pi, f.ctx, f.publisher);
  expect(f.saved).toHaveLength(0);
  f.branch(entries(f.root, { models: { seats: { lead: { model: "p/model" } } } }));
  modelDialogs(f, [{ type: "edit", seat: "lead", field: "model" }, { type: "save" }], "\x1b[A");
  await editConfiguration(f.pi, f.ctx, f.publisher);
  expect(f.saved[0].data.overrides).toEqual({});
}));

test("model removed from catalogue during picker is rejected before draft persistence", async () => fixture(async f => {
  modelDialogs(f, [{ type: "edit", seat: "lead", field: "model" }, { type: "save" }]);
  const custom = f.ctx.ui.custom;
  f.ctx.ui.custom = async (...args: any[]) => {
    const result = await custom(...args);
    if (typeof result === "string" && result.startsWith("model:")) f.ctx.modelRegistry.getAvailable = () => [];
    return result;
  };
  await expect(editConfiguration(f.pi, f.ctx, f.publisher)).rejects.toThrow("不可用");
  expect(f.saved).toHaveLength(0);
}));

test("scope switching retains drafts; untrusted directory is skipped", async () => fixture(async f => {
  f.trust(false);
  modelDialogs(f, [{ type: "edit", seat: "worker", field: "model" }, { type: "scope" }, { type: "scope" }, { type: "save" }]);
  await editConfiguration(f.pi, f.ctx, f.publisher);
  expect(f.saved[0].data.overrides.models.seats.worker.model).toBe("p/model");
  expect(existsSync(join(f.root, ".pi"))).toBe(false);
}));

test("global and directory saves use CAS and preserve advanced fields", async () => fixture(async f => {
  writeFileSync(join(f.bridge.config_dir, "settings.json"), JSON.stringify({ version: 1, advanced: { keep: true } }));
  modelDialogs(f, [{ type: "scope" }, { type: "edit", seat: "lead", field: "model" }, { type: "save" }]);
  await editConfiguration(f.pi, f.ctx, f.publisher);
  let saved = JSON.parse(readFileSync(join(f.bridge.config_dir, "settings.json"), "utf8"));
  expect(saved.advanced).toEqual({ keep: true });
  expect(saved.models.seats.lead.model).toBe("p/model");
  modelDialogs(f, [{ type: "scope" }, { type: "scope" }, { type: "edit", seat: "worker", field: "model" }, { type: "save" }]);
  await editConfiguration(f.pi, f.ctx, f.publisher);
  saved = JSON.parse(readFileSync(join(f.root, ".pi/shop.json"), "utf8"));
  expect(saved.models.seats.worker.model).toBe("p/model");
  expect(saved.models.seats.lead).toBeUndefined();
  expect(f.saved).toHaveLength(0);
}));

test("session reset appends empty custom overrides; legacy migration is explicit", async () => fixture(async f => {
  f.branch(entries(f.root, { models: { worker: "p/model" } }));
  f.ctx.ui.custom = async () => ({ type: "reset" });
  await editConfiguration(f.pi, f.ctx, f.publisher);
  expect(f.saved[0].data.overrides).toEqual({});
  const legacy = join(f.bridge.config_dir, "models.json");
  writeFileSync(legacy, JSON.stringify({ lead: "p/model", worker: "p/model" }));
  const original = readFileSync(legacy, "utf8");
  f.ctx.ui.confirm = async () => false;
  await editConfiguration(f.pi, f.ctx, f.publisher, "migrate");
  expect(existsSync(join(f.bridge.config_dir, "settings.json"))).toBe(false);
  f.ctx.ui.confirm = async () => true;
  await editConfiguration(f.pi, f.ctx, f.publisher, "migrate");
  expect(readFileSync(legacy, "utf8")).toBe(original);
  expect(JSON.parse(readFileSync(join(f.bridge.config_dir, "settings.json"), "utf8")).models.worker).toBe("p/model");
}));

test("cancel and reset-declined do not persist anything", async () => fixture(async f => {
  await editConfiguration(f.pi, f.ctx, f.publisher);
  expect(f.saved).toHaveLength(0);
  let n = 0;
  f.ctx.ui.custom = async () => n++ === 0 ? { type: "reset" } : { type: "cancel" };
  f.ctx.ui.confirm = async () => false;
  await editConfiguration(f.pi, f.ctx, f.publisher);
  expect(f.saved).toHaveLength(0);
  expect(f.calls.filter((r: any) => r.action === "save")).toHaveLength(0);
}));

test("session change or branch setting change during confirmation refuses append", async () => fixture(async f => {
  f.ctx.ui.custom = async () => ({ type: "reset" });
  f.ctx.ui.confirm = async () => { f.session("other"); return true; };
  await expect(editConfiguration(f.pi, f.ctx, f.publisher)).rejects.toThrow("会话/回合已变化");
  expect(f.saved).toHaveLength(0);
  f.ctx.ui.confirm = async () => { f.branch(entries(f.root, {}, "changed")); return true; };
  await expect(editConfiguration(f.pi, f.ctx, f.publisher)).rejects.toThrow("分支设置");
  expect(f.saved).toHaveLength(0);
}));

test("parent file conflict after confirmation refuses session persistence", async () => fixture(async f => {
  f.ctx.ui.custom = async () => ({ type: "reset" });
  f.ctx.ui.confirm = async () => {
    writeFileSync(join(f.bridge.config_dir, "settings.json"), JSON.stringify({ version: 1, models: { lead: "p/model" } }));
    return true;
  };
  await expect(editConfiguration(f.pi, f.ctx, f.publisher)).rejects.toThrow("Configuration changed");
  expect(f.saved).toHaveLength(0);
}));

test("model availability and thinking capability fail before save", async () => fixture(async f => {
  expect(() => validateModels(f.ctx, profiles({ model: "unknown/model" }))).toThrow("不可用");
  expect(() => validateModels(f.ctx, profiles({ model: "p/model", thinking: "max" }))).toThrow("不支持");
  validateModels(f.ctx, profiles({ model: "p/model", thinking: "high" }));
  validateModels(f.ctx, profiles({ model: "p/model", thinking: null }));
  f.ctx.mode = "rpc";
  await editConfiguration(f.pi, f.ctx, f.publisher);
  expect(f.calls).toHaveLength(0);
  expect(f.notices.join(" ")).toContain("TUI");
}));

for (const language of ["en", "zh-CN"]) test(`${language} settings actions and narrow-screen bounds`, async () => fixture(async f => {
  writeFileSync(join(f.bridge.config_dir, "language.json"), JSON.stringify({ version: 1, language }));
  const view = await configCall<ConfigView>(f.pi, f.bridge, { action: "load", ...configRequest(configContext(f.bridge, f.ctx), {}) });
  const results: any[] = [];
  const panel = settingsPanel("session", "session:test", view.layers.session.profiles, view, r => results.push(r), () => {}, ["global", "session"], true,
    { label: t => t, value: t => t, description: t => t, hint: t => t, cursor: "›" });
  expect(panel.render(120).join(" ")).toContain(language === "en" ? "Shop settings" : "Shop 设置");
  expect(panel.render(120)[0]).toStartWith("╭");
  expect(panel.render(120).at(-1)).toEndWith("╯");
  expect(panel.render(120).join(" ")).toContain(language === "en" ? "Scope:" : "作用域：");
  for (const width of [4, 12, 40, 80]) for (const line of panel.render(width)) expect(visibleWidth(line)).toBeLessThanOrEqual(width);
  for (const input of ["\t", "s", "r", "\x1b"]) panel.handleInput!(input);
  expect(results.map(r => r.type)).toEqual(["scope", "save", "reset", "cancel"]);
}));

test("candidate publisher invalidates old session; cleanup never deletes replacement", async () => fixture(async f => {
  await f.publisher.start(f.ctx);
  const path = candidatePath(f.bridge);
  const first = JSON.parse(readFileSync(path, "utf8"));
  expect(first.session_id).toBe("session");
  f.session("new-session");
  f.branch(entries(f.root, { models: { worker: "p/model" } }));
  await f.publisher.start(f.ctx);
  const next = JSON.parse(readFileSync(path, "utf8"));
  expect(next.session_id).toBe("new-session");
  expect(next.instance).not.toBe(first.instance);
  expect(next.overrides.models.worker).toBe("p/model");
  expect(() => publishCandidate(path, { ...next, instance: "competitor" })).toThrow("Another live Pi instance");
  removeCandidate(path, first.instance);
  expect(existsSync(path)).toBe(true);
  f.publisher.stop();
  expect(existsSync(path)).toBe(false);
}));

test("shutdown during async identity lookup cannot republish an obsolete candidate", async () => fixture(async f => {
  let release!: (value: any) => void;
  f.pi.exec = () => new Promise(resolve => { release = resolve; });
  const pending = f.publisher.start(f.ctx);
  f.publisher.stop();
  release({ code: 0, stdout: JSON.stringify({ terminal: "terminal" }) });
  await pending;
  expect(existsSync(candidatePath(f.bridge))).toBe(false);
}));

test("candidate generated by TS is consumed by Python with session precedence", async () => fixture(async f => {
  writeFileSync(join(f.bridge.config_dir, "settings.json"), JSON.stringify({ version: 1, models: { defaults: { model: "p/global", thinking: "high" } } }));
  f.branch(entries(f.root, { models: { seats: { "worker-2": { model: "p/session", thinking: "low" } } } }));
  await f.publisher.start(f.ctx);
  const source = `import json, configuration as c\napi=lambda *a: {'agent': {'agent':'pi','terminal_id':'terminal','tab_id':'tab'}}\nprint(json.dumps(c.startup_profiles(api, {'pane_id':'pane','tab_id':'tab'}, ${JSON.stringify(f.root)}, '/test/socket')))`;
  const result = spawnSync("python3", ["-c", source], { env: { ...process.env, PYTHONPATH: "core" }, encoding: "utf8" });
  expect(result.status).toBe(0);
  const [resolved, provenance] = JSON.parse(result.stdout);
  expect(resolved["worker-2"]).toEqual({ model: "p/session", thinking: "low" });
  expect(provenance.session_id).toBe("session");
  // Large candidates fail before replacing a good record.
  const path = candidatePath(f.bridge), original = readFileSync(path, "utf8");
  expect(() => publishCandidate(path, { text: "x".repeat(65537) })).toThrow("64 KiB");
  expect(readFileSync(path, "utf8")).toBe(original);
}));
