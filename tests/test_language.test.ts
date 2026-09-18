import { afterEach, beforeEach, expect, test } from "bun:test";
import { mkdtempSync, mkdirSync, writeFileSync, readFileSync, rmSync, existsSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { spawnSync } from "node:child_process";
import ts from "typescript";
import { catalogs, detectLanguage, readPreference, resolveLanguage, selectAction, t } from "../extensions/i18n.ts";
import { registerLanguageCommand } from "../extensions/language-ui.ts";
import { summarizeSnapshot } from "../extensions/state.ts";

let old: NodeJS.ProcessEnv, root: string, path: string;
beforeEach(() => {
  old = { ...process.env };
  root = mkdtempSync(join(tmpdir(), "shop-language-"));
  mkdirSync(join(root, "config"));
  path = join(root, "config/language.json");
  process.env.SHOP_LOCATOR = join(root, "bridge.json");
  process.env.LC_ALL = "en_US.UTF-8";
  writeFileSync(process.env.SHOP_LOCATOR, JSON.stringify({ protocol: 1, core_root: resolve("."),
    config_dir: join(root, "config"), state_dir: join(root, "state") }));
});
afterEach(() => {
  for (const key of Object.keys(process.env)) if (!(key in old)) delete process.env[key];
  Object.assign(process.env, old);
  rmSync(root, { recursive: true, force: true });
});
function preference(language: string) { writeFileSync(path, JSON.stringify({ version: 1, language })); }
function commandFixture() {
  let command: any, id = "session";
  const notices: string[] = [], calls: string[][] = [];
  const pi: any = {
    registerCommand(name: string, value: any) { expect(name).toBe("shop-language"); command = value; },
    async exec(binary: string, args: string[]) {
      calls.push(args);
      const result = spawnSync(binary, args, { encoding: "utf8", env: process.env });
      return { code: result.status, stdout: result.stdout, stderr: result.stderr, killed: false };
    },
  };
  const ctx: any = { hasUI: true, sessionManager: { getSessionId: () => id },
    ui: { select: async () => undefined, notify: (text: string) => notices.push(text) } };
  registerLanguageCommand(pi);
  return { command, ctx, notices, calls, changeSession() { id = "replaced"; } };
}

test("TS/Python locale detection agree including POSIX and Chinese variants", () => {
  const cases = [{}, { LANG: "zh_CN.UTF-8" }, { LANG: "zh-TW" }, { LANG: "ZH_hans" },
    { LANG: "zh_CN", LC_MESSAGES: "en_GB" }, { LC_MESSAGES: "zh_CN", LC_ALL: "" },
    { LC_ALL: "C", LANG: "zh_CN" }, { LANG: "fr_FR" }, { LANG: "zhgarbage" }];
  const result = spawnSync("python3", ["-c", "import language,json,sys; print(json.dumps([language.detect_language(e) for e in json.loads(sys.argv[1])]))", JSON.stringify(cases)],
    { env: { ...process.env, PYTHONPATH: "core" }, encoding: "utf8" });
  expect(result.status).toBe(0);
  expect(cases.map(env => detectLanguage(env))).toEqual(JSON.parse(result.stdout));
});

test("catalogues have identical keys/placeholders; fallback and values are not recursive", () => {
  expect(Object.keys(catalogs.en).sort()).toEqual(Object.keys(catalogs["zh-CN"]).sort());
  for (const key of Object.keys(catalogs.en)) {
    expect(catalogs.en[key]).toBe(key);
    expect(catalogs["zh-CN"][key].length).toBeGreaterThan(0);
    expect((catalogs.en[key].match(/\{\d+\}|%s/g) ?? []).sort()).toEqual((catalogs["zh-CN"][key].match(/\{\d+\}|%s/g) ?? []).sort());
  }
  expect(t("Untranslated English fallback", [], "zh-CN")).toBe("Untranslated English fallback");
  expect(t("Inherit parent: {0}", ["user {1} /path 中文"], "zh-CN")).toBe("继承父层：user {1} /path 中文");
});

test("explicit preference wins and Python revisions match TS byte-for-byte", () => {
  const f = commandFixture();
  expect(readPreference().revision).toBe("missing");
  expect(resolveLanguage()).toBe("en");
  preference("zh-CN");
  expect(resolveLanguage()).toBe("zh-CN");
  const result = spawnSync("python3", ["core/language.py"], { env: process.env, encoding: "utf8" });
  expect(result.status).toBe(0);
  expect(JSON.parse(result.stdout)).toEqual({ ...readPreference(), effective: "zh-CN" });
  expect(f.calls).toEqual([]);
});

test("invalid language data falls back only for display; explicit reads report errors", () => {
  for (const value of ["{", "null", "[]", '{"version":true,"language":"en"}', '{"version":1,"language":"fr"}', "x".repeat(4097)]) {
    writeFileSync(path, value);
    expect(() => readPreference()).toThrow();
    expect(resolveLanguage()).toBe("en");
    expect(readFileSync(path, "utf8")).toBe(value);
  }
});

test("language command persists through Python without models, transport, sessions or runtime mutations", async () => {
  const f = commandFixture();
  for (const language of ["zh-CN", "en", "auto"] as const) {
    await f.command.handler(language, f.ctx);
    expect(readPreference().language).toBe(language);
  }
  expect(f.notices).toHaveLength(3);
  expect(f.notices[0]).toContain("语言已保存");
  expect(f.notices[1]).toContain("Language saved");
  expect(f.calls.every(args => args[0].endsWith("core/language.py"))).toBe(true);
  expect(existsSync(join(root, "state"))).toBe(false);
  expect(existsSync(join(root, "config/settings.json"))).toBe(false);
});

test("cancel, unsupported values, replaced sessions and conflicting saves never overwrite", async () => {
  const f = commandFixture();
  await f.command.handler("", f.ctx);
  await f.command.handler("fr", f.ctx);
  expect(f.calls).toEqual([]);
  expect(existsSync(path)).toBe(false);
  f.ctx.ui.select = async () => { f.changeSession(); return "English"; };
  await f.command.handler("", f.ctx);
  expect(f.calls).toEqual([]);
  expect(f.notices.at(-1)).toContain("Session or bridge changed");
  f.ctx.ui.select = async () => { preference("zh-CN"); return "English"; };
  await f.command.handler("", f.ctx);
  expect(readPreference().language).toBe("zh-CN");
  expect(f.notices.at(-1)).toContain("changed");
});

test("selection uses captured labels and stable IDs even if language changes while open", async () => {
  for (const language of ["en", "zh-CN"] as const) {
    const label = t("Exit", [], language);
    const ctx: any = { ui: { select: async (_title: string, choices: string[]) => {
      preference(language === "en" ? "zh-CN" : "en"); return choices[0];
    } } };
    expect(await selectAction(ctx, "menu", [["exit", label]])).toBe("exit");
    ctx.ui.select = async () => "unknown choice";
    expect(await selectAction(ctx, "menu", [["exit", label]])).toBeUndefined();
    await expect(selectAction(ctx, "menu", [["a", "same"], ["b", "same"]])).rejects.toThrow("Duplicate");
  }
});

test("localized snapshot keeps original status, IDs, user text and error detail", () => {
  const data = { schema: "shop.snapshot/v1", shop: { phase: "ready", run_id: "run-original" },
    members: [], attention: [{ severity: "warn", code: "E_RAW", detail: "Keep 原文 {0}" }], unknowns: ["original unknown"], events: {} };
  const original = JSON.stringify(data);
  for (const language of ["en", "zh-CN"] as const) {
    const result = summarizeSnapshot(original, 4000, language);
    expect(result).toContain("ready"); expect(result).toContain("run-original");
    expect(result).toContain("E_RAW"); expect(result).toContain("Keep 原文 {0}");
    expect(result).toContain("original unknown");
    expect(summarizeSnapshot(original, 24, language).length).toBeLessThanOrEqual(24);
  }
  expect(summarizeSnapshot(original, 4000, "zh-CN")).toContain("成员");
  expect(JSON.stringify(data)).toBe(original);
});

test("all literal UI translation calls have shared catalogue entries", () => {
  for (const name of ["index", "language-ui", "settings-ui", "workbench-ui", "state", "transport"]) {
    const file = resolve(`extensions/${name}.ts`), source = readFileSync(file, "utf8");
    const tree = ts.createSourceFile(file, source, ts.ScriptTarget.Latest, true);
    function walk(node: ts.Node) {
      if (ts.isCallExpression(node) && ts.isIdentifier(node.expression) && node.expression.text === "t"
          && node.arguments.length && ts.isStringLiteral(node.arguments[0])) {
        expect(Object.hasOwn(catalogs.en, node.arguments[0].text)).toBe(true);
      }
      ts.forEachChild(node, walk);
    }
    walk(tree);
  }
});
