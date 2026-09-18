import type { ExtensionContext } from "@earendil-works/pi-coding-agent";
import { createHash } from "node:crypto";
import { readFileSync, statSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import { readBridge } from "./bridge.js";

export type Language = "en" | "zh-CN";
export type LanguagePreference = Language | "auto";
export const LANGUAGES: readonly LanguagePreference[] = ["auto", "zh-CN", "en"];
type Catalog = Record<string, string>;
export const catalogs: Record<Language, Catalog> = {
  en: JSON.parse(readFileSync(new URL("../locales/en.json", import.meta.url), "utf8")),
  "zh-CN": JSON.parse(readFileSync(new URL("../locales/zh-CN.json", import.meta.url), "utf8")),
};
export function detectLanguage(env: NodeJS.ProcessEnv): Language {
  const value = env.LC_ALL || env.LC_MESSAGES || env.LANG || "";
  return /^zh(?:[-_.@]|$)/i.test(value) ? "zh-CN" : "en";
}
export function preferencePath(): string {
  return join(readBridge()?.config_dir ?? join(homedir(), ".config/shop-workstation"), "language.json");
}
export function readPreference(path = preferencePath()): { language: LanguagePreference; revision: string; path: string } {
  let raw: Buffer;
  try {
    const stat = statSync(path);
    if (!stat.isFile()) throw new Error("Language preference must be a regular file");
    if (stat.size > 4096) throw new Error("Language preference exceeds 4 KiB");
    raw = readFileSync(path);
    if (raw.length > 4096) throw new Error("Language preference exceeds 4 KiB");
  } catch (error: any) {
    if (error.code === "ENOENT") return { language: "auto", revision: "missing", path };
    throw error;
  }
  const value = JSON.parse(raw.toString("utf8"));
  if (!value || value.version !== 1 || !LANGUAGES.includes(value.language)
      || Object.keys(value).some(k => !["version", "language"].includes(k)))
    throw new Error("Invalid language preference; inspect language.json before saving");
  return { language: value.language, revision: createHash("sha256").update(raw).digest("hex"), path };
}
export function resolveLanguage(): Language {
  // Bad display preferences must not change authorization or disable diagnostics.
  // Explicit inspection/saves still report errors and refuse to overwrite bad data.
  try {
    const preference = readPreference().language;
    if (preference !== "auto") return preference;
  } catch { /* auto fallback; never rewrite invalid preferences */ }
  return detectLanguage(process.env);
}
/** Only package-owned message keys enter this function. Values are opaque and never retranslated. */
export function t(key: string, values: readonly unknown[] = [], language = resolveLanguage()): string {
  const text = (Object.hasOwn(catalogs[language], key) && catalogs[language][key])
    || (Object.hasOwn(catalogs.en, key) && catalogs.en[key]) || key;
  return text.replace(/\{(\d+)\}/g, (slot, index) => Number(index) < values.length ? String(values[Number(index)]) : slot);
}
/** Capture labels before awaiting; return stable IDs even if another Pi changes language meanwhile. */
export async function selectAction<T extends string>(ctx: Pick<ExtensionContext, "ui">, title: string,
  choices: ReadonlyArray<readonly [T, string]>, options?: { signal?: AbortSignal }): Promise<T | undefined> {
  const labels = choices.map(([, label]) => label);
  if (new Set(labels).size !== labels.length) throw new Error("Duplicate selection labels");
  const selected = await ctx.ui.select(title, labels, options);
  if (selected === undefined) return undefined;
  return choices[labels.indexOf(selected)]?.[0];
}
