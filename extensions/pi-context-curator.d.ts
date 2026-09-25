// Types for the pi-context-curator source modules Shop imports (pinned git
// dependency; its Bundler-style source is not type-checked under NodeNext).
declare module "pi-context-curator/src/types" {
  export type RetentionMode = "summary" | "exact" | "drop";
  export interface SourceUnit { id: string; entryIds: string[]; text: string; tokens: number; hash: string }
  export interface CuratorNode {
    id: string; title: string; displayTitle?: string; summary: string; sourceUnitIds: string[];
    recommendedMode: RetentionMode; mode: RetentionMode; risk: "low" | "medium" | "high"; rationale: string;
    dependencies: string[]; verbatimEvidence: string[]; children?: CuratorNode[];
  }
  export interface CuratorSnapshot { sessionId: string; focus: string; curationInstruction?: string; [key: string]: unknown }
  export interface CuratorConfig {
    analyzerModel: string; thinkingLevel: string; language: "zh" | "en"; rawTailTokens: number;
    maxBlocksPerSplit: 2 | 3; [key: string]: unknown;
  }
  export interface PreparedSource {
    snapshot: CuratorSnapshot; units: SourceUnit[]; unitById: Map<string, SourceUnit>; firstKeptEntryId: string;
    prefixEntries: unknown[]; rawTailEntries: unknown[];
  }
}
declare module "pi-context-curator/src/source" {
  import type { ExtensionCommandContext } from "@earendil-works/pi-coding-agent";
  import type { PreparedSource } from "pi-context-curator/src/types";
  export function prepareSource(ctx: ExtensionCommandContext, focus: string, rawTailTokens: number,
    language?: "zh" | "en", maxBlocksPerSplit?: 2 | 3): PreparedSource;
}
declare module "pi-context-curator/src/analyzer" {
  import type { ExtensionContext } from "@earendil-works/pi-coding-agent";
  import type { CuratorConfig, CuratorNode, SourceUnit } from "pi-context-curator/src/types";
  export function analyzePartition(ctx: ExtensionContext, units: SourceUnit[], focus: string, config: CuratorConfig,
    signal: AbortSignal, parentTitle?: string, onProgress?: (progress: unknown) => void,
    curationInstruction?: string): Promise<CuratorNode[]>;
}
declare module "pi-context-curator/src/compiler" {
  import type { CuratorNode, CuratorSnapshot, SourceUnit } from "pi-context-curator/src/types";
  export function compileCheckpoint(snapshot: CuratorSnapshot, nodes: CuratorNode[], unitById: Map<string, SourceUnit>,
    includeArchiveIndex: boolean, language?: "zh" | "en"): { text: string; estimatedTokens: number };
}
declare module "pi-context-curator/src/config" {
  import type { CuratorConfig } from "pi-context-curator/src/types";
  export function loadConfig(agentDir: string, cwd: string, includeProjectConfig?: boolean): CuratorConfig;
}
declare module "pi-context-curator/src/validation" {
  import type { CuratorNode } from "pi-context-curator/src/types";
  export function validateCoverage(nodes: CuratorNode[], expectedIds: string[]): { ok: boolean; [key: string]: unknown };
}
