// Hand the current Pi context to the next seat as a standalone child session.
// The receiving model decides how: small contexts pass through verbatim (cheap
// with prompt caching, nothing lost); contexts beyond the receiver's budget are
// curated with pi-context-curator down to that budget. The seat brief (SPEC,
// PLAN, task) is not in the session: callers pin it in the system prompt so the
// seat's own auto-compaction can never drop it. The parent session is never
// switched or modified.
import { existsSync, writeFileSync } from "node:fs";
import {
  estimateTokens, getAgentDir, SessionManager, sessionEntryToContextMessages,
  type ExtensionCommandContext, type ExtensionContext,
} from "@earendil-works/pi-coding-agent";
import { analyzePartition } from "pi-context-curator/src/analyzer";
import { compileCheckpoint } from "pi-context-curator/src/compiler";
import { loadConfig } from "pi-context-curator/src/config";
import { prepareSource } from "pi-context-curator/src/source";
import { validateCoverage } from "pi-context-curator/src/validation";
import type { CuratorConfig, CuratorNode } from "pi-context-curator/src/types";

export type Analyze = typeof analyzePartition;
export type HandoffMode = "auto" | "raw" | "curate" | "brief";
export type AnalyzerUsage = { calls: number; input: number; output: number; cacheRead: number; cost: number };

export const HANDOFF_MODES: readonly HandoffMode[] = ["auto", "raw", "curate", "brief"];
/** Below this, verbatim context costs less than any curation call. */
export const RAW_LIMIT_TOKENS = 16_000;
/** Share of the receiver's window a handed-off context may take; the rest is room to work. */
export const BUDGET_FRACTION = 0.3;
/** Between RAW_LIMIT_TOKENS and the budget; pending the S2 fidelity comparison. */
export const MIDDLE_MODE: "raw" | "curate" = "curate";
const FALLBACK_WINDOW = 128_000;

export type HandoffRequest = {
  focus: string;
  instruction: string;
  analyzerModel: string;
  receiverModel: string;
  sessionDir: string;
  name: string;
  mode?: HandoffMode;
  /** Explicit receiver budget; defaults to BUDGET_FRACTION of the receiver's context window. */
  budgetTokens?: number;
  signal?: AbortSignal;
};

export type HandoffResult = {
  file: string;
  mode: "raw" | "curate" | "brief";
  reason: string;
  sourceTokens: number;
  budgetTokens: number;
  analyzer: AnalyzerUsage;
  checkpointTokens?: number;
  overBudget?: boolean;
  blocks?: Array<{ title: string; mode: string; risk: string }>;
};

type Message = ReturnType<typeof sessionEntryToContextMessages>[number];

// Curate everything except the newest message; that tail is dropped, not copied.
const RAW_TAIL_TOKENS = 1;

/** Context messages minus any tool call without a result (e.g. the spawn call in flight) and orphaned results. */
export function completeMessages(ctx: ExtensionContext): Message[] {
  const messages = ctx.sessionManager.buildContextEntries().flatMap(sessionEntryToContextMessages);
  const calls = (message: Message) => message.role === "assistant"
    ? message.content.filter(part => part.type === "toolCall").map(part => (part as { id: string }).id) : [];
  const answered = new Set(messages.filter(message => message.role === "toolResult")
    .map(message => (message as { toolCallId: string }).toolCallId));
  const kept = messages.filter(message => calls(message).every(id => answered.has(id)));
  const called = new Set(kept.flatMap(calls));
  return kept.filter(message => message.role !== "toolResult" || called.has((message as { toolCallId: string }).toolCallId));
}

export function chooseMode(requested: HandoffMode, sourceTokens: number, budgetTokens: number): { mode: HandoffResult["mode"]; reason: string } {
  if (requested === "brief") return { mode: "brief", reason: "forced brief" };
  if (sourceTokens > budgetTokens) return { mode: "curate", reason: `source ${sourceTokens} > receiver budget ${budgetTokens}` };
  if (requested === "raw" || requested === "curate") return { mode: requested, reason: `forced ${requested}` };
  if (sourceTokens <= RAW_LIMIT_TOKENS) return { mode: "raw", reason: `source ${sourceTokens} <= raw limit ${RAW_LIMIT_TOKENS}` };
  return { mode: MIDDLE_MODE, reason: `source ${sourceTokens} between raw limit and budget` };
}

/** Same context, but every analyzer completion's usage is tallied (it bypasses the session ledger). */
function metered(ctx: ExtensionContext, usage: AnalyzerUsage): ExtensionContext {
  const registry = new Proxy(ctx.modelRegistry, { get(target, key) {
    const value = Reflect.get(target, key, target);
    if (key !== "complete") return typeof value === "function" ? value.bind(target) : value;
    return async (...args: unknown[]) => {
      const response = await (value as (...args: unknown[]) => Promise<any>).apply(target, args);
      const used = response?.usage ?? {};
      usage.calls += 1;
      usage.input += used.input ?? 0;
      usage.output += used.output ?? 0;
      usage.cacheRead += used.cacheRead ?? 0;
      usage.cost += used.cost?.total ?? 0;
      return response;
    };
  } });
  return new Proxy(ctx, { get(target, key) {
    if (key === "modelRegistry") return registry;
    const value = Reflect.get(target, key, target);
    return typeof value === "function" ? value.bind(target) : value;
  } });
}

function leaves(nodes: CuratorNode[]): CuratorNode[] {
  return nodes.flatMap(node => node.children?.length ? leaves(node.children) : [node]);
}

function receiverWindow(ctx: ExtensionContext, selector: string): number {
  const slash = selector.indexOf("/");
  const model = slash > 0 ? ctx.modelRegistry.find(selector.slice(0, slash), selector.slice(slash + 1)) : undefined;
  return model?.contextWindow || FALLBACK_WINDOW;
}

function appendVerbatim(child: SessionManager, messages: Message[]): void {
  for (const message of messages) {
    const value = message as any;
    if (value.role === "custom") child.appendCustomMessageEntry(value.customType, value.content, value.display ?? true, value.details);
    else if (value.role === "compactionSummary" || value.role === "branchSummary") {
      child.appendCustomMessageEntry("shop-seat-summary", value.summary, true);
    } else child.appendMessage(value);
  }
}

async function curate(ctx: ExtensionContext, request: HandoffRequest, budgetTokens: number, analyzer: AnalyzerUsage,
  analyze: Analyze): Promise<{ text: string; details: Partial<HandoffResult> } | undefined> {
  const config: CuratorConfig = {
    ...loadConfig(getAgentDir(), ctx.cwd), analyzerModel: request.analyzerModel, thinkingLevel: "low", language: "en",
    rawTailTokens: RAW_TAIL_TOKENS, targetCheckpointTokens: budgetTokens,
  };
  let prepared: ReturnType<typeof prepareSource>;
  try {
    prepared = prepareSource(ctx as ExtensionCommandContext, request.focus, RAW_TAIL_TOKENS, "en", config.maxBlocksPerSplit);
  } catch {
    return undefined; // Too little history to split; the caller passes it verbatim.
  }
  const nodes = await analyze(metered(ctx, analyzer), prepared.units, request.focus, config,
    request.signal ?? new AbortController().signal, undefined, undefined, request.instruction);
  const coverage = validateCoverage(nodes, prepared.units.map(unit => unit.id));
  if (!coverage.ok) throw new Error("Curation coverage failed: " + JSON.stringify(coverage).slice(0, 500));
  const compiled = compileCheckpoint({ ...prepared.snapshot, curationInstruction: request.instruction },
    nodes, prepared.unitById, false, "en");
  return { text: compiled.text, details: {
    checkpointTokens: compiled.estimatedTokens, overBudget: compiled.estimatedTokens > budgetTokens,
    blocks: leaves(nodes).map(node => ({ title: node.title, mode: node.mode, risk: node.risk })),
  } };
}

export async function handoffToChildSession(ctx: ExtensionContext, request: HandoffRequest,
  analyze: Analyze = analyzePartition): Promise<HandoffResult> {
  const messages = completeMessages(ctx);
  const sourceTokens = messages.reduce((sum, message) => sum + estimateTokens(message), 0);
  const budgetTokens = request.budgetTokens ?? Math.floor(receiverWindow(ctx, request.receiverModel) * BUDGET_FRACTION);
  let { mode, reason } = chooseMode(request.mode ?? "auto", sourceTokens, budgetTokens);
  const analyzer: AnalyzerUsage = { calls: 0, input: 0, output: 0, cacheRead: 0, cost: 0 };
  let curated: Awaited<ReturnType<typeof curate>>;
  if (mode === "curate") {
    curated = await curate(ctx, request, budgetTokens, analyzer, analyze);
    if (!curated) {
      if (sourceTokens > budgetTokens) throw new Error("Context exceeds the receiver budget but cannot be curated");
      mode = "raw";
      reason += "; too short to curate, passed verbatim";
    }
  }
  const parent = ctx.sessionManager.getSessionFile();
  const child = SessionManager.create(ctx.cwd, request.sessionDir, parent ? { parentSession: parent } : undefined);
  child.appendSessionInfo(request.name);
  if (mode === "raw") appendVerbatim(child, messages);
  if (mode === "curate" && curated) child.appendCustomMessageEntry("shop-seat-context", curated.text, true, { blocks: curated.details.blocks });
  const file = child.getSessionFile();
  if (!file) throw new Error("Child session has no file path");
  // Pi defers the first write until an assistant message exists; persist now
  // so the new seat can open it with --session.
  if (!existsSync(file)) {
    writeFileSync(file, [child.getHeader(), ...child.getEntries()].map(entry => JSON.stringify(entry)).join("\n") + "\n",
      { mode: 0o600 });
  }
  return { file, mode, reason, sourceTokens, budgetTokens, analyzer, ...(curated?.details ?? {}) };
}
