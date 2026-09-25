import type { ExtensionAPI, ExtensionContext, SessionEntry } from "@earendil-works/pi-coding-agent";
import { CONFIG_DIR_NAME } from "@earendil-works/pi-coding-agent";
import { createHash } from "node:crypto";
import { readFileSync, realpathSync, statSync } from "node:fs";
import { join } from "node:path";
import { shopPaths, type ShopPaths } from "./paths.js";

export const SESSION_CONFIG = "shop-settings";
export const SEATS = ["lead", "worker", "worker-2"] as const;
export type Seat = typeof SEATS[number];
export type Thinking = "off" | "minimal" | "low" | "medium" | "high" | "xhigh" | "max";
export type Profile = { model?: string; thinking?: Thinking | null };
export type Profiles = Record<Seat, Profile>;
export type Overrides = { models?: { defaults?: Profile; lead?: Profile | string; worker?: Profile | string; seats?: Partial<Profiles> } };
export type Scope = "global" | "project" | "session";
export type Layer = { profiles: Profiles; parent: Profiles; sources: Record<Seat, Partial<Record<keyof Profile, string>>>; overrides: Overrides };
export type ConfigView = {
  root: string; trusted: boolean; legacy: boolean;
  paths: Record<"global" | "project" | "legacy", string>;
  revisions: Record<string, string>; layers: Record<Scope, Layer>;
};
export type ConfigContext = { root: string; trusted: boolean };
export const hash = (text: string) => createHash("sha256").update(text).digest("hex");

export function readBounded(path: string): any {
  if (statSync(path).size > 65536) throw new Error("Shop config/state exceeds 64 KiB");
  return JSON.parse(readFileSync(path, "utf8"));
}

export function configContext(ctx: ExtensionContext): ConfigContext {
  const root = realpathSync(ctx.cwd);
  return { root, trusted: ctx.isProjectTrusted() };
}

export function sessionSettings(entries: SessionEntry[], root: string): { overrides: Overrides; marker: string } {
  for (let i = entries.length - 1; i >= 0; i--) {
    const entry = entries[i];
    if (entry.type !== "custom" || entry.customType !== SESSION_CONFIG) continue;
    const data = entry.data as { version?: number; project_root?: string; overrides?: Overrides } | undefined;
    if (data?.version !== 1) throw new Error("Unsupported Shop session settings version");
    if (data.project_root !== root) continue;
    if (!data.overrides || typeof data.overrides !== "object" || Array.isArray(data.overrides))
      throw new Error("Invalid Shop session overrides");
    return { overrides: data.overrides, marker: entry.id + ":" + hash(JSON.stringify(data.overrides)) };
  }
  return { overrides: {}, marker: "none" };
}

export async function configCall<T>(pi: ExtensionAPI, request: Record<string, unknown>, paths: ShopPaths = shopPaths()): Promise<T> {
  const json = JSON.stringify({ ...request, expected_config_dir: paths.config_dir, expected_state_dir: paths.state_dir });
  if (Buffer.byteLength(json) > 65536) throw new Error("Shop configuration request exceeds 64 KiB");
  const result = await pi.exec("python3", [join(paths.core_root, "core/configuration.py"), "--request", json], { timeout: 15000 });
  if (result.code !== 0 || result.killed) throw new Error((result.stderr || "Shop configuration operation failed").slice(0, 2000));
  if (Buffer.byteLength(result.stdout) > 65536) throw new Error("Shop configuration response exceeds 64 KiB");
  return JSON.parse(result.stdout) as T;
}

export function configRequest(context: ConfigContext, overrides: Overrides): Record<string, unknown> {
  return { root: context.root, trusted: context.trusted, project_dir: CONFIG_DIR_NAME, session: overrides };
}

/** Effective per-seat profiles (global < project < this session) as the next /shop-go will use them. */
export async function effectiveProfiles(pi: ExtensionAPI, ctx: ExtensionContext): Promise<Profiles> {
  const context = configContext(ctx);
  const session = sessionSettings(ctx.sessionManager.getBranch(), context.root);
  const view = await configCall<ConfigView>(pi, { ...configRequest(context, session.overrides), action: "load" });
  return view.layers.session.profiles;
}
