import { expect, test } from "bun:test";
import { mkdtempSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { SessionManager } from "@earendil-works/pi-coding-agent";
import { curateToChildSession, type Analyze } from "../extensions/seat-curation.ts";

const user = (text: string) => ({ role: "user" as const, content: [{ type: "text" as const, text }], timestamp: Date.now() });
const assistant = (text: string) => ({
  role: "assistant" as const, content: [{ type: "text" as const, text }], api: "openai-completions", provider: "p", model: "m",
  usage: { input: 1, output: 1, cacheRead: 0, cacheWrite: 0, totalTokens: 2, cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } },
  stopReason: "stop" as const, timestamp: Date.now(),
});

function context(messages: Array<ReturnType<typeof user> | ReturnType<typeof assistant>>) {
  const cwd = mkdtempSync(join(tmpdir(), "seat-curation-"));
  const sessionManager = SessionManager.inMemory(cwd);
  for (const message of messages) sessionManager.appendMessage(message as any);
  return { cwd, ctx: { cwd, sessionManager, getContextUsage: () => undefined, modelRegistry: {} } as any };
}

const request = (cwd: string) => ({
  focus: "Lead: dispatch", instruction: "keep constraints", analyzerModel: "p/m",
  sessionDir: join(cwd, "sessions"), name: "Shop Lead", brief: "# Brief\nSPEC and PLAN here",
});

function entries(file: string) {
  return readFileSync(file, "utf8").trim().split("\n").map(line => JSON.parse(line));
}

test("curated child holds only the checkpoint and brief, honoring exact/drop, without touching the parent", async () => {
  const { cwd, ctx } = context([
    user("Constraint: output must be JSON, never YAML."), assistant("Agreed, JSON only."),
    user("Alternative B: use YAML files."), assistant("Rejected B; YAML is out."),
    user("Write SPEC and PLAN."), assistant("SPEC and PLAN written."),
  ]);
  const parentBefore = ctx.sessionManager.getEntries().length;
  let seen: { focus?: string; instruction?: string; model?: string } = {};
  const analyze: Analyze = async (_ctx, units, focus, config, _signal, _title, _progress, instruction) => {
    seen = { focus, instruction, model: config.analyzerModel };
    const keep = units.filter(unit => unit.text.includes("Constraint")).map(unit => unit.id);
    const node = (id: string, ids: string[], mode: "exact" | "drop") => ({
      id, title: id, summary: id + " summary", sourceUnitIds: ids, recommendedMode: mode, mode, risk: "low" as const,
      rationale: "", dependencies: [], verbatimEvidence: [],
    });
    return [node("constraint", keep, "exact"), node("rest", units.map(u => u.id).filter(id => !keep.includes(id)), "drop")];
  };
  const result = await curateToChildSession(ctx, request(cwd), analyze);
  expect(result.analyzer.calls).toBe(0);
  expect(seen).toEqual({ focus: "Lead: dispatch", instruction: "keep constraints", model: "p/m" });
  expect(result.curated).toBe(true);
  expect(result.blocks?.map(block => block.mode)).toEqual(["exact", "drop"]);
  const written = entries(result.file);
  expect(written[0].type).toBe("session");
  const custom = written.filter(entry => entry.type === "custom_message");
  expect(custom.map(entry => entry.customType)).toEqual(["shop-seat-context", "shop-seat-brief"]);
  expect(custom[0].content).toContain("output must be JSON, never YAML");
  expect(custom[0].content).not.toContain("Alternative B");
  expect(written.some(entry => entry.type === "message")).toBe(false);
  expect(ctx.sessionManager.getEntries().length).toBe(parentBefore);
  // The new seat opens this file with --session: it must load as context.
  const reopened = SessionManager.open(result.file);
  expect(reopened.buildContextEntries().filter(entry => entry.type === "custom_message").length).toBe(2);
});

test("too-short history falls back to the brief alone", async () => {
  const { cwd, ctx } = context([user("hi")]);
  const analyze: Analyze = async () => { throw new Error("analyzer must not run"); };
  const result = await curateToChildSession(ctx, request(cwd), analyze);
  expect(result.curated).toBe(false);
  const custom = entries(result.file).filter(entry => entry.type === "custom_message");
  expect(custom.map(entry => entry.customType)).toEqual(["shop-seat-brief"]);
});

test("coverage failure refuses to write a lossy child", async () => {
  const { cwd, ctx } = context([user("a"), assistant("b"), user("c"), assistant("d")]);
  const analyze: Analyze = async () => [];
  await expect(curateToChildSession(ctx, request(cwd), analyze)).rejects.toThrow("coverage");
});

test("analyzer completions are metered through the wrapped model registry", async () => {
  const { cwd, ctx } = context([user("a1"), assistant("b1"), user("c1"), assistant("d1")]);
  ctx.modelRegistry = { complete: async () => ({ usage: { input: 100, output: 20, cacheRead: 5, cost: { total: 0.002 } } }) };
  const analyze: Analyze = async (meteredCtx, units) => {
    await meteredCtx.modelRegistry.complete({} as any, {} as any, {} as any);
    await meteredCtx.modelRegistry.complete({} as any, {} as any, {} as any);
    return [{ id: "all", title: "all", summary: "s", sourceUnitIds: units.map(u => u.id), recommendedMode: "summary",
      mode: "summary", risk: "low", rationale: "", dependencies: [], verbatimEvidence: [] }];
  };
  const result = await curateToChildSession(ctx, request(cwd), analyze);
  expect(result.analyzer).toEqual({ calls: 2, input: 200, output: 40, cacheRead: 10, cost: 0.004 });
});
