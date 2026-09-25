import { test, expect } from "bun:test";
import { mkdtempSync, mkdirSync, writeFileSync, readFileSync, existsSync, rmSync, realpathSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { spawnSync } from "node:child_process";
import { getKeybindings, visibleWidth } from "@earendil-works/pi-tui";
import { ModelPicker } from "../extensions/model-picker.ts";
import { SESSION_CONFIG, configCall, configContext, configRequest, effectiveProfiles,
  sessionSettings, type ConfigView, type Profiles, SEATS } from "../extensions/configuration.ts";
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
  const bridge = { state_dir: join(root, "state"), config_dir: join(root, "config") };
  mkdirSync(bridge.config_dir); mkdirSync(bridge.state_dir);
  const env = { LC_ALL: "zh_CN.UTF-8", SHOP_CONFIG_DIR: bridge.config_dir, SHOP_STATE_DIR: bridge.state_dir };
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
      const result = spawnSync(command, args, { env: process.env, encoding: "utf8" });
      return { code: result.status, stdout: result.stdout, stderr: result.stderr };
    },
    appendEntry: (customType: string, data: any) => {
      const entry = { type: "custom", customType, id: `saved-${saved.length}`, data };
      saved.push(entry); branch = [...branch, entry];
    },
  };
  try {
    await fn({ root, bridge, env, pi, ctx, calls, notices, saved,
      branch: (next: any[]) => { branch = next; }, session: (next: string) => { id = next; },
      trust: (next: boolean) => { trusted = next; } });
  } finally {
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

test("config context is the working directory and its Pi trust decision", async () => fixture(async f => {
  expect(configContext(f.ctx)).toEqual({ root: f.root, trusted: true });
  f.trust(false);
  expect(configContext(f.ctx)).toEqual({ root: f.root, trusted: false });
}));

test("effective profiles merge global, project and this session for the next /shop-go", async () => fixture(async f => {
  writeFileSync(join(f.bridge.config_dir, "settings.json"), JSON.stringify({ version: 1, models: {
    defaults: { model: "p/model", thinking: "high" }, worker: { thinking: "low" } } }));
  mkdirSync(join(f.root, ".pi"));
  writeFileSync(join(f.root, ".pi/shop.json"), JSON.stringify({ version: 1, models: { lead: { model: "p/project" } } }));
  f.branch(entries(f.root, { models: { seats: { worker: { model: "p/session" } } } }));
  const profiles = await effectiveProfiles(f.pi, f.ctx);
  expect(profiles.lead).toEqual({ model: "p/project", thinking: "high" });
  expect(profiles.worker).toEqual({ model: "p/session", thinking: "low" });
}));

test("Python configuration service is consumed by Pi; sparse session save is not a message", async () => fixture(async f => {
  modelDialogs(f, [{ type: "edit", seat: "lead", field: "model" }, { type: "save" }]);
  await editConfiguration(f.pi, f.ctx);
  expect(f.saved).toHaveLength(1);
  expect(f.saved[0].customType).toBe(SESSION_CONFIG);
  expect(f.saved[0].data.overrides).toEqual({ models: { seats: { lead: { model: "p/model" } } } });
  expect(existsSync(join(f.bridge.config_dir, "settings.json"))).toBe(false);
  expect(existsSync(join(f.root, ".pi"))).toBe(false);
  expect(f.notices.join(" ")).toContain("/shop-go");
}));

test("model picker cancel and inherit never change native Pi model or defaults", async () => fixture(async f => {
  f.pi.setModel = () => { throw new Error("Must not switch Architect model"); };
  modelDialogs(f, [{ type: "edit", seat: "lead", field: "model" }, { type: "cancel" }], "\x1b");
  await editConfiguration(f.pi, f.ctx);
  expect(f.saved).toHaveLength(0);
  f.branch(entries(f.root, { models: { seats: { lead: { model: "p/model" } } } }));
  modelDialogs(f, [{ type: "edit", seat: "lead", field: "model" }, { type: "save" }], "\x1b[A");
  await editConfiguration(f.pi, f.ctx);
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
  await expect(editConfiguration(f.pi, f.ctx)).rejects.toThrow("不可用");
  expect(f.saved).toHaveLength(0);
}));

test("scope switching retains drafts; untrusted directory is skipped", async () => fixture(async f => {
  f.trust(false);
  modelDialogs(f, [{ type: "edit", seat: "worker", field: "model" }, { type: "scope" }, { type: "scope" }, { type: "save" }]);
  await editConfiguration(f.pi, f.ctx);
  expect(f.saved[0].data.overrides.models.seats.worker.model).toBe("p/model");
  expect(existsSync(join(f.root, ".pi"))).toBe(false);
}));

test("global and directory saves use CAS and preserve advanced fields", async () => fixture(async f => {
  writeFileSync(join(f.bridge.config_dir, "settings.json"), JSON.stringify({ version: 1, advanced: { keep: true } }));
  modelDialogs(f, [{ type: "scope" }, { type: "edit", seat: "lead", field: "model" }, { type: "save" }]);
  await editConfiguration(f.pi, f.ctx);
  let saved = JSON.parse(readFileSync(join(f.bridge.config_dir, "settings.json"), "utf8"));
  expect(saved.advanced).toEqual({ keep: true });
  expect(saved.models.seats.lead.model).toBe("p/model");
  modelDialogs(f, [{ type: "scope" }, { type: "scope" }, { type: "edit", seat: "worker", field: "model" }, { type: "save" }]);
  await editConfiguration(f.pi, f.ctx);
  saved = JSON.parse(readFileSync(join(f.root, ".pi/shop.json"), "utf8"));
  expect(saved.models.seats.worker.model).toBe("p/model");
  expect(saved.models.seats.lead).toBeUndefined();
  expect(f.saved).toHaveLength(0);
}));

test("session reset appends empty custom overrides; legacy migration is explicit", async () => fixture(async f => {
  f.branch(entries(f.root, { models: { worker: "p/model" } }));
  f.ctx.ui.custom = async () => ({ type: "reset" });
  await editConfiguration(f.pi, f.ctx);
  expect(f.saved[0].data.overrides).toEqual({});
  const legacy = join(f.bridge.config_dir, "models.json");
  writeFileSync(legacy, JSON.stringify({ lead: "p/model", worker: "p/model" }));
  const original = readFileSync(legacy, "utf8");
  f.ctx.ui.confirm = async () => false;
  await editConfiguration(f.pi, f.ctx, "migrate");
  expect(existsSync(join(f.bridge.config_dir, "settings.json"))).toBe(false);
  f.ctx.ui.confirm = async () => true;
  await editConfiguration(f.pi, f.ctx, "migrate");
  expect(readFileSync(legacy, "utf8")).toBe(original);
  expect(JSON.parse(readFileSync(join(f.bridge.config_dir, "settings.json"), "utf8")).models.worker).toBe("p/model");
}));

test("cancel and reset-declined do not persist anything", async () => fixture(async f => {
  await editConfiguration(f.pi, f.ctx);
  expect(f.saved).toHaveLength(0);
  let n = 0;
  f.ctx.ui.custom = async () => n++ === 0 ? { type: "reset" } : { type: "cancel" };
  f.ctx.ui.confirm = async () => false;
  await editConfiguration(f.pi, f.ctx);
  expect(f.saved).toHaveLength(0);
  expect(f.calls.filter((r: any) => r.action === "save")).toHaveLength(0);
}));

test("session change or branch setting change during confirmation refuses append", async () => fixture(async f => {
  f.ctx.ui.custom = async () => ({ type: "reset" });
  f.ctx.ui.confirm = async () => { f.session("other"); return true; };
  await expect(editConfiguration(f.pi, f.ctx)).rejects.toThrow("会话/回合已变化");
  expect(f.saved).toHaveLength(0);
  f.ctx.ui.confirm = async () => { f.branch(entries(f.root, {}, "changed")); return true; };
  await expect(editConfiguration(f.pi, f.ctx)).rejects.toThrow("分支设置");
  expect(f.saved).toHaveLength(0);
}));

test("parent file conflict after confirmation refuses session persistence", async () => fixture(async f => {
  f.ctx.ui.custom = async () => ({ type: "reset" });
  f.ctx.ui.confirm = async () => {
    writeFileSync(join(f.bridge.config_dir, "settings.json"), JSON.stringify({ version: 1, models: { lead: "p/model" } }));
    return true;
  };
  await expect(editConfiguration(f.pi, f.ctx)).rejects.toThrow("Configuration changed");
  expect(f.saved).toHaveLength(0);
}));

test("model availability and thinking capability fail before save", async () => fixture(async f => {
  expect(() => validateModels(f.ctx, profiles({ model: "unknown/model" }))).toThrow("不可用");
  expect(() => validateModels(f.ctx, profiles({ model: "p/model", thinking: "max" }))).toThrow("不支持");
  validateModels(f.ctx, profiles({ model: "p/model", thinking: "high" }));
  validateModels(f.ctx, profiles({ model: "p/model", thinking: null }));
  f.ctx.mode = "rpc";
  await editConfiguration(f.pi, f.ctx);
  expect(f.calls).toHaveLength(0);
  expect(f.notices.join(" ")).toContain("TUI");
}));

for (const language of ["en", "zh-CN"]) test(`${language} settings actions and narrow-screen bounds`, async () => fixture(async f => {
  writeFileSync(join(f.bridge.config_dir, "language.json"), JSON.stringify({ version: 1, language }));
  const view = await configCall<ConfigView>(f.pi, { action: "load", ...configRequest(configContext(f.ctx), {}) });
  const results: any[] = [];
  const panel = settingsPanel("session", "session:test", view.layers.session.profiles, view, r => results.push(r), () => {}, ["global", "session"],
    { label: t => t, value: t => t, description: t => t, hint: t => t, cursor: "›" });
  expect(panel.render(120).join(" ")).toContain(language === "en" ? "Shop settings" : "Shop 设置");
  expect(panel.render(120)[0]).toStartWith("╭");
  expect(panel.render(120).at(-1)).toEndWith("╯");
  expect(panel.render(120).join(" ")).toContain(language === "en" ? "Scope:" : "作用域：");
  for (const width of [4, 12, 40, 80]) for (const line of panel.render(width)) expect(visibleWidth(line)).toBeLessThanOrEqual(width);
  for (const input of ["\t", "s", "r", "\x1b"]) panel.handleInput!(input);
  expect(results.map(r => r.type)).toEqual(["scope", "save", "reset", "cancel"]);
}));

for (const language of ["en", "zh-CN"]) test(`${language} Worker purpose labels preserve seat IDs and draft values`, async () => fixture(async f => {
  writeFileSync(join(f.bridge.config_dir, "language.json"), JSON.stringify({ version: 1, language }));
  const view = await configCall<ConfigView>(f.pi, { action: "load", ...configRequest(configContext(f.ctx), {}) });
  const draft = view.layers.session.profiles;
  const before = JSON.stringify(draft);
  const labels = language === "en" ? ["Fast Worker (low cost)", "Steady Worker (reliable)"]
    : ["快速 Worker（低成本）", "稳健 Worker（重可靠性）"];
  const descriptions = language === "en" ? ["Routine tasks", "Complex implementation"] : ["日常小任务", "复杂实现"];
  for (const [index, seat] of ["worker", "worker-2"].entries()) for (const field of ["model", "thinking"]) {
    const results: any[] = [];
    const panel = settingsPanel("session", "session:test", draft, view, r => results.push(r), () => {}, ["session"],
      { label: t => t, value: t => t, description: t => t, hint: t => t, cursor: "›" }, `${seat}:${field}`);
    const text = panel.render(120).join("\n");
    for (const label of labels) expect(text).toContain(label);
    expect(text).toContain(descriptions[index]);
    expect(text).not.toMatch(/Worker [12]/);
    for (const width of [4, 12, 40, 80]) for (const line of panel.render(width)) expect(visibleWidth(line)).toBeLessThanOrEqual(width);
    panel.handleInput!("\r");
    expect(results).toEqual([{ type: "edit", seat, field }]);
  }
  expect(JSON.stringify(draft)).toBe(before);
  expect(f.saved).toHaveLength(0);
}));
