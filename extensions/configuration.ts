import type { ExtensionAPI, ExtensionContext, SessionEntry } from "@earendil-works/pi-coding-agent";
import { CONFIG_DIR_NAME } from "@earendil-works/pi-coding-agent";
import { createHash, randomUUID } from "node:crypto";
import { existsSync, mkdirSync, readFileSync, realpathSync, renameSync, statSync, unlinkSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { readBridge, type Bridge } from "./bridge.js";

export const SESSION_CONFIG = "shop-settings";
export const SEATS = ["lead", "lead-2", "worker", "worker-2"] as const;
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
export type ConfigContext = { root: string; trusted: boolean; architect: boolean; existingShop: boolean };
export const hash = (text: string) => createHash("sha256").update(text).digest("hex");

export function readBounded(path: string): any {
  if (statSync(path).size > 65536) throw new Error("Shop config/state exceeds 64 KiB");
  return JSON.parse(readFileSync(path, "utf8"));
}

export function configContext(bridge: Bridge, ctx: ExtensionContext, env = process.env): ConfigContext {
  const cwd = realpathSync(ctx.cwd);
  let root = cwd, architect = true, existingShop = false;
  if (env.HERDR_SOCKET_PATH && env.HERDR_TAB_ID) {
    const key = hash(`${env.HERDR_SOCKET_PATH}:${env.HERDR_TAB_ID}`).slice(0, 12);
    const path = join(bridge.state_dir, "runtime", `${key}.json`);
    if (existsSync(path)) {
      const state = readBounded(path);
      if (state.tab !== env.HERDR_TAB_ID || typeof state.cwd !== "string" || !state.architect?.pane)
        throw new Error("Shop registration invalid; inspect before editing configuration");
      root = realpathSync(state.cwd);
      architect = state.architect.pane === env.HERDR_PANE_ID;
      existingShop = true;
    }
  }
  return { root, trusted: root === cwd && ctx.isProjectTrusted(), architect, existingShop };
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

export async function configCall<T>(pi: ExtensionAPI, bridge: Bridge, request: Record<string, unknown>): Promise<T> {
  const json = JSON.stringify({ ...request, expected_config_dir: bridge.config_dir, expected_state_dir: bridge.state_dir });
  if (Buffer.byteLength(json) > 65536) throw new Error("Shop configuration request exceeds 64 KiB");
  const result = await pi.exec("python3", [join(bridge.core_root, "core/configuration.py"), "--request", json], { timeout: 15000 });
  if (result.code !== 0 || result.killed) throw new Error((result.stderr || "Shop configuration operation failed").slice(0, 2000));
  if (Buffer.byteLength(result.stdout) > 65536) throw new Error("Shop configuration response exceeds 64 KiB");
  return JSON.parse(result.stdout) as T;
}

export function configRequest(context: ConfigContext, overrides: Overrides): Record<string, unknown> {
  return { root: context.root, trusted: context.trusted, project_dir: CONFIG_DIR_NAME, session: overrides };
}

export function candidatePath(bridge: Bridge, env = process.env): string {
  if (!env.HERDR_SOCKET_PATH || !env.HERDR_TAB_ID || !env.HERDR_PANE_ID) throw new Error("Missing Herdr candidate identity");
  return join(bridge.state_dir, "config-candidates", hash(`${env.HERDR_SOCKET_PATH}:${env.HERDR_TAB_ID}:${env.HERDR_PANE_ID}`).slice(0, 24) + ".json");
}

export function removeCandidate(path: string, instance: string): void {
  try {
    if (readBounded(path).instance === instance) unlinkSync(path);
  } catch (error: any) { if (error.code !== "ENOENT") throw error; }
}

export function publishCandidate(path: string, record: Record<string, unknown>): void {
  const text = JSON.stringify(record);
  if (Buffer.byteLength(text) > 65536) throw new Error("Shop candidate exceeds 64 KiB");
  if (existsSync(path)) {
    const previous = readBounded(path);
    if (previous.instance !== record.instance) {
      if (!Number.isInteger(previous.pid) || previous.pid <= 0) throw new Error("Invalid prior Shop candidate; inspect registration");
      let alive = true;
      try { process.kill(previous.pid, 0); } catch (error: any) {
        if (error.code === "ESRCH") alive = false; else throw error;
      }
      if (alive) throw new Error("Another live Pi instance owns this settings candidate; inspect duplicate extension/session");
    }
  }
  mkdirSync(dirname(path), { recursive: true, mode: 0o700 });
  const temporary = `${path}.${randomUUID()}.tmp`;
  try {
    writeFileSync(temporary, text, { flag: "wx", mode: 0o600 });
    renameSync(temporary, path);
  } finally { if (existsSync(temporary)) unlinkSync(temporary); }
}

/** Session lifecycle only. No model calls, task dispatch, or member control. */
export class ConfigPublisher {
  private generation = 0;
  private timer?: ReturnType<typeof setInterval>;
  private owned?: { path: string; instance: string };
  private controller = new AbortController();
  constructor(private pi: ExtensionAPI) {}
  get signal(): AbortSignal { return this.controller.signal; }

  stop(): void {
    this.generation++;
    this.controller.abort();
    if (this.timer) clearInterval(this.timer);
    this.timer = undefined;
    if (this.owned) {
      try { removeCandidate(this.owned.path, this.owned.instance); } catch { /* Stale candidates expire; never delete another instance. */ }
      this.owned = undefined;
    }
  }

  async start(ctx: ExtensionContext): Promise<void> {
    this.stop();
    this.controller = new AbortController();
    const generation = this.generation;
    try {
      const sessionId = ctx.sessionManager.getSessionId();
      const bridge = readBridge();
      if (!bridge) return;
      const initial = configContext(bridge, ctx);
      if (!initial.architect) return;
      const { terminal } = await configCall<{ terminal: string }>(this.pi, bridge, { action: "identify" });
      if (generation !== this.generation) return;
      const instance = randomUUID(), path = candidatePath(bridge);
      const pulse = () => {
        if (generation !== this.generation) return;
        try {
          if (ctx.sessionManager.getSessionId() !== sessionId) throw new Error("Session changed; refresh Shop settings");
          if (JSON.stringify(readBridge()) !== JSON.stringify(bridge)) throw new Error("Shop bridge changed; reload configuration");
          const context = configContext(bridge, ctx);
          if (!context.architect || context.root !== initial.root) throw new Error("Architect/project identity changed");
          const session = sessionSettings(ctx.sessionManager.getBranch(), context.root);
          publishCandidate(path, { schema: 1, socket: process.env.HERDR_SOCKET_PATH,
            tab: process.env.HERDR_TAB_ID, pane: process.env.HERDR_PANE_ID, terminal,
            session_id: sessionId, instance, pid: process.pid, root: context.root,
            trusted: context.trusted, project_dir: CONFIG_DIR_NAME, overrides: session.overrides,
            settings_entry_id: session.marker, updated_at: Date.now() / 1000 });
          this.owned = { path, instance };
        } catch (error) {
          this.stop();
          if (ctx.hasUI) ctx.ui.notify(`Shop 开工配置不可用：${String(error)}`, "warning");
        }
      };
      pulse();
      if (generation === this.generation) {
        this.timer = setInterval(pulse, 5000);
        this.timer.unref();
      }
    } catch (error) {
      if (generation === this.generation && ctx.hasUI)
        ctx.ui.notify(`Shop 开工配置未发布：${String(error)}。配置保存后可重试 /shop-config。`, "warning");
    }
  }
}
