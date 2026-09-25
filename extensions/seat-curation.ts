// Curate the current Pi context into a standalone child session for the next seat.
// The child holds only a deterministic curator checkpoint plus a brief; no raw
// messages are copied, so no in-flight tool call can reach another provider
// request unpaired. The parent session is never switched or modified.
import { existsSync, writeFileSync } from "node:fs";
import { getAgentDir, SessionManager, type ExtensionCommandContext, type ExtensionContext } from "@earendil-works/pi-coding-agent";
import { analyzePartition } from "pi-context-curator/src/analyzer";
import { compileCheckpoint } from "pi-context-curator/src/compiler";
import { loadConfig } from "pi-context-curator/src/config";
import { prepareSource } from "pi-context-curator/src/source";
import { validateCoverage } from "pi-context-curator/src/validation";
import type { CuratorConfig, CuratorNode } from "pi-context-curator/src/types";

export type Analyze = typeof analyzePartition;

export type CurationRequest = {
  focus: string;
  instruction: string;
  analyzerModel: string;
  sessionDir: string;
  name: string;
  brief: string;
  signal?: AbortSignal;
};

export type AnalyzerUsage = { calls: number; input: number; output: number; cacheRead: number; cost: number };

export type CurationResult = {
  file: string;
  curated: boolean;
  analyzer: AnalyzerUsage;
  reason?: string;
  sourceTokens?: number;
  checkpointTokens?: number;
  blocks?: Array<{ title: string; mode: string; risk: string }>;
};

// Curate everything except the newest message; the tail is dropped, not copied.
const RAW_TAIL_TOKENS = 1;

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

export async function curateToChildSession(ctx: ExtensionContext, request: CurationRequest,
  analyze: Analyze = analyzePartition): Promise<CurationResult> {
  const config: CuratorConfig = {
    ...loadConfig(getAgentDir(), ctx.cwd),
    analyzerModel: request.analyzerModel, thinkingLevel: "low", language: "en", rawTailTokens: RAW_TAIL_TOKENS,
  };
  const analyzer: AnalyzerUsage = { calls: 0, input: 0, output: 0, cacheRead: 0, cost: 0 };
  let checkpoint: string | undefined;
  let result: Omit<CurationResult, "file" | "analyzer">;
  let prepared: ReturnType<typeof prepareSource> | undefined;
  try {
    prepared = prepareSource(ctx as ExtensionCommandContext, request.focus, RAW_TAIL_TOKENS, "en", config.maxBlocksPerSplit);
  } catch (error) {
    // Too little history to curate: the brief alone carries the handoff.
    result = { curated: false, reason: String(error instanceof Error ? error.message : error) };
  }
  if (prepared) {
    const nodes = await analyze(metered(ctx, analyzer), prepared.units, request.focus, config, request.signal ?? new AbortController().signal,
      undefined, undefined, request.instruction);
    const coverage = validateCoverage(nodes, prepared.units.map(unit => unit.id));
    if (!coverage.ok) throw new Error("Curation coverage failed: " + JSON.stringify(coverage).slice(0, 500));
    const compiled = compileCheckpoint({ ...prepared.snapshot, curationInstruction: request.instruction },
      nodes, prepared.unitById, false, "en");
    checkpoint = compiled.text;
    result = {
      curated: true,
      sourceTokens: prepared.units.reduce((sum, unit) => sum + unit.tokens, 0),
      checkpointTokens: compiled.estimatedTokens,
      blocks: leaves(nodes).map(node => ({ title: node.title, mode: node.mode, risk: node.risk })),
    };
  }
  const parent = ctx.sessionManager.getSessionFile();
  const child = SessionManager.create(ctx.cwd, request.sessionDir, parent ? { parentSession: parent } : undefined);
  child.appendSessionInfo(request.name);
  if (checkpoint) child.appendCustomMessageEntry("shop-seat-context", checkpoint, true, { blocks: result!.blocks });
  child.appendCustomMessageEntry("shop-seat-brief", request.brief, true);
  const file = child.getSessionFile();
  if (!file) throw new Error("Child session has no file path");
  // Pi defers the first write until an assistant message exists; persist now
  // so the new seat can open it with --session.
  if (!existsSync(file)) {
    writeFileSync(file, [child.getHeader(), ...child.getEntries()].map(entry => JSON.stringify(entry)).join("\n") + "\n",
      { mode: 0o600 });
  }
  return { file, ...result!, analyzer };
}
