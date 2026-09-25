import { expect, test } from "bun:test";
import { mkdtempSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { SessionManager } from "@earendil-works/pi-coding-agent";
import {
  BUDGET_FRACTION, RAW_LIMIT_TOKENS, chooseMode, completeMessages, handoffToChildSession, type Analyze,
} from "../extensions/seat-curation.ts";

const now = Date.now();
const user = (text: string) => ({ role: "user", content: [{ type: "text", text }], timestamp: now });
const usage = { input: 1, output: 1, cacheRead: 0, cacheWrite: 0, totalTokens: 2, cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } };
const assistant = (text: string) => ({ role: "assistant", content: [{ type: "text", text }], api: "openai-completions",
  provider: "p", model: "m", usage, stopReason: "stop", timestamp: now });
const toolCall = (id: string) => ({ role: "assistant", content: [{ type: "toolCall", id, name: "read", arguments: {} }],
  api: "openai-completions", provider: "p", model: "m", usage, stopReason: "toolUse", timestamp: now });
const toolResult = (id: string) => ({ role: "toolResult", toolCallId: id, toolName: "read",
  content: [{ type: "text", text: "result " + id }], isError: false, timestamp: now });

function context(messages: object[], window = 1_000_000) {
  const cwd = mkdtempSync(join(tmpdir(), "seat-handoff-"));
  const sessionManager = SessionManager.inMemory(cwd);
  for (const message of messages) sessionManager.appendMessage(message as any);
  const modelRegistry = { find: () => ({ contextWindow: window }), complete: async () => ({ usage: {} }) };
  return { cwd, ctx: { cwd, sessionManager, getContextUsage: () => undefined, modelRegistry } as any };
}

const request = (cwd: string, mode?: "auto" | "raw" | "curate" | "brief") => ({
  focus: "Lead: dispatch", instruction: "keep constraints", analyzerModel: "p/analyzer", receiverModel: "p/lead",
  sessionDir: join(cwd, "sessions"), name: "Shop Lead", ...(mode ? { mode } : {}),
});

const entries = (file: string) => readFileSync(file, "utf8").trim().split("\n").map(line => JSON.parse(line));
const noAnalyzer: Analyze = async () => { throw new Error("analyzer must not run"); };
const conversation = [
  user("Constraint: output must be JSON, never YAML."), assistant("Agreed, JSON only."),
  user("Alternative B: use YAML files."), assistant("Rejected B; YAML is out."),
  user("Write SPEC and PLAN."), assistant("SPEC and PLAN written."),
];

test("mode choice: small passes verbatim, beyond budget must curate, forced modes respected inside budget", () => {
  expect(chooseMode("auto", RAW_LIMIT_TOKENS, 300_000).mode).toBe("raw");
  expect(chooseMode("auto", RAW_LIMIT_TOKENS + 1, 300_000).mode).toBe("curate");
  expect(chooseMode("auto", 400_000, 300_000).mode).toBe("curate");
  expect(chooseMode("raw", 400_000, 300_000).mode).toBe("curate");
  expect(chooseMode("raw", 20_000, 300_000).mode).toBe("raw");
  expect(chooseMode("curate", 100, 300_000).mode).toBe("curate");
  expect(chooseMode("brief", 400_000, 300_000).mode).toBe("brief");
});

test("in-flight tool calls and their orphaned results are never handed over", () => {
  const { ctx } = context([user("go"), toolCall("done"), toolResult("done"), toolCall("inflight")]);
  const roles = completeMessages(ctx).map((message: any) => message.role + (message.toolCallId ? ":" + message.toolCallId : ""));
  expect(roles).toEqual(["user", "assistant", "toolResult:done"]);
});

test("auto hands a small context over verbatim without calling the analyzer, parent untouched", async () => {
  const { cwd, ctx } = context([...conversation, toolCall("spawn")]);
  const parentBefore = ctx.sessionManager.getEntries().length;
  const result = await handoffToChildSession(ctx, request(cwd), noAnalyzer);
  expect(result.mode).toBe("raw");
  expect(result.budgetTokens).toBe(Math.floor(1_000_000 * BUDGET_FRACTION));
  expect(result.analyzer.calls).toBe(0);
  const messages = entries(result.file).filter(entry => entry.type === "message");
  expect(messages.length).toBe(conversation.length);
  expect(JSON.stringify(messages)).toContain("never YAML");
  expect(JSON.stringify(messages)).not.toContain("toolCall");
  expect(ctx.sessionManager.getEntries().length).toBe(parentBefore);
  expect(SessionManager.open(result.file).buildContextEntries().filter(entry => entry.type === "message").length).toBe(conversation.length);
});

test("curate keeps exact blocks, drops the rest, copies no raw messages, and meters analyzer usage", async () => {
  const { cwd, ctx } = context(conversation);
  ctx.modelRegistry.complete = async () => ({ usage: { input: 100, output: 20, cacheRead: 5, cost: { total: 0.002 } } });
  let seen: Record<string, unknown> = {};
  const analyze: Analyze = async (metered, units, focus, config, _signal, _title, _progress, instruction) => {
    seen = { focus, instruction, model: config.analyzerModel };
    await metered.modelRegistry.complete({} as any, {} as any, {} as any);
    const keep = units.filter(unit => unit.text.includes("Constraint")).map(unit => unit.id);
    const node = (id: string, ids: string[], mode: "exact" | "drop") => ({ id, title: id, summary: id, sourceUnitIds: ids,
      recommendedMode: mode, mode, risk: "low" as const, rationale: "", dependencies: [], verbatimEvidence: [] });
    return [node("constraint", keep, "exact"), node("rest", units.map(unit => unit.id).filter(id => !keep.includes(id)), "drop")];
  };
  const result = await handoffToChildSession(ctx, request(cwd, "curate"), analyze);
  expect(seen).toEqual({ focus: "Lead: dispatch", instruction: "keep constraints", model: "p/analyzer" });
  expect(result.mode).toBe("curate");
  expect(result.blocks?.map(block => block.mode)).toEqual(["exact", "drop"]);
  expect(result.analyzer).toEqual({ calls: 1, input: 100, output: 20, cacheRead: 5, cost: 0.002 });
  const written = entries(result.file);
  expect(written.some(entry => entry.type === "message")).toBe(false);
  const checkpoint = written.find(entry => entry.customType === "shop-seat-context");
  expect(checkpoint.content).toContain("output must be JSON, never YAML");
  expect(checkpoint.content).not.toContain("Alternative B");
});

test("brief mode hands over no context at all", async () => {
  const { cwd, ctx } = context(conversation);
  const result = await handoffToChildSession(ctx, request(cwd, "brief"), noAnalyzer);
  expect(result.mode).toBe("brief");
  expect(entries(result.file).map(entry => entry.type)).toEqual(["session", "session_info"]);
});

test("a context beyond the receiver budget is curated even when raw was requested", async () => {
  const { cwd, ctx } = context(conversation, 100);
  const analyze: Analyze = async (_ctx, units) => [{ id: "all", title: "all", summary: "s", sourceUnitIds: units.map(unit => unit.id),
    recommendedMode: "summary", mode: "summary", risk: "low", rationale: "", dependencies: [], verbatimEvidence: [] }];
  const result = await handoffToChildSession(ctx, request(cwd, "raw"), analyze);
  expect(result.mode).toBe("curate");
  expect(result.reason).toContain("receiver budget");
});

test("too short to curate falls back to verbatim inside the budget", async () => {
  const { cwd, ctx } = context([user("hi")]);
  const result = await handoffToChildSession(ctx, request(cwd, "curate"), noAnalyzer);
  expect(result.mode).toBe("raw");
  expect(result.reason).toContain("too short");
});

test("coverage failure refuses to write a lossy child", async () => {
  const { cwd, ctx } = context(conversation);
  await expect(handoffToChildSession(ctx, request(cwd, "curate"), async () => [])).rejects.toThrow("coverage");
});
