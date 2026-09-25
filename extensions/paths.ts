import { homedir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

export type ShopPaths = { core_root: string; config_dir: string; state_dir: string };

/** The Pi package is the only entrypoint: core lives next to this file; config/state come from env or defaults. */
export function shopPaths(env: NodeJS.ProcessEnv = process.env): ShopPaths {
  return {
    core_root: resolve(dirname(fileURLToPath(import.meta.url)), ".."),
    config_dir: env.SHOP_CONFIG_DIR || join(homedir(), ".config/shop-workstation"),
    state_dir: env.SHOP_STATE_DIR || join(homedir(), ".local/state/shop-workstation"),
  };
}
