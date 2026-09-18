import { readFileSync } from "node:fs";
import { homedir } from "node:os";
import { isAbsolute, join } from "node:path";

export type Bridge = { protocol: number; core_root: string; state_dir: string; config_dir: string };
export function readBridge(): Bridge | undefined {
  const path = process.env.SHOP_LOCATOR ?? join(homedir(), ".config/shop-workstation/bridge.json");
  try {
    const data = JSON.parse(readFileSync(path, "utf8"));
    if (data.protocol !== 1 || ![data.core_root, data.state_dir, data.config_dir].every(x => typeof x === "string" && isAbsolute(x)))
      throw new Error("Invalid Shop bridge or incompatible protocol");
    return data;
  } catch (error: any) {
    if (error.code === "ENOENT") return undefined;
    throw error;
  }
}
