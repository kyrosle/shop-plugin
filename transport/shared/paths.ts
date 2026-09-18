// Ported from pi-intercom 0.13.0 (MIT), npm tarball sha256
// 1d89bd31ca63cccd82a5c950cc043bca99e17e4664df7fd99f16859a0442279f
// Upstream ref: npm gitHead 199279ae861bf53ce014809fb2a03337538ae13e
// Original: broker/paths.ts (sha256 a6d5387de9fae0d206987e2ba5041d544ba1d6c02495b6ac841215f4a618e3d2)
// Copyright (c) 2026 Nico Bailon. Modified for Shop built-in transport:
// every path/name/env re-scoped to PI_SHOP_TRANSPORT_DIR + pi-shop-transport,
// getIntercomDirPath removed, Shop state root used instead of PI_CODING_AGENT_DIR.
// See transport/NOTICE.md.
import { chmodSync, mkdirSync, readFileSync } from "node:fs";
import { homedir } from "node:os";
import { isAbsolute, join, resolve } from "node:path";

export const SHOP_TRANSPORT_DIR_MODE = 0o700;
export const SHOP_TRANSPORT_FILE_MODE = 0o600;
export const SHOP_TRANSPORT_TCP_HOST = "127.0.0.1";
export const SHOP_TRANSPORT_PROTOCOL_NAME = "pi-shop-transport";
export const SHOP_TRANSPORT_PROTOCOL_VERSION = 1;

export interface BrokerTcpEndpoint {
  transport: "tcp";
  host: string;
  port: number;
  stateId?: string;
}

export type BrokerConnectTarget = string | BrokerTcpEndpoint;

function sanitizePipeSegment(value: string): string {
  return value
    .replace(/[^a-zA-Z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .toLowerCase() || "default";
}

/** Shop state root: the same root `shop-run`/`herdr-shop` already use. */
export function getShopStateRootPath(
  env: NodeJS.ProcessEnv = process.env,
  homeDir: string = homedir(),
  cwd: string = process.cwd(),
): string {
  const configured = (env.SHOP_STATE_DIR ?? env.HERDR_PLUGIN_STATE_DIR)?.trim();
  if (!configured) return join(homeDir, ".local/state/shop-workstation");
  return isAbsolute(configured) ? configured : resolve(cwd, configured);
}

/** Transport root: PI_SHOP_TRANSPORT_DIR wins, else <shop state root>/transport. */
export function getShopTransportDirPath(
  env: NodeJS.ProcessEnv = process.env,
  homeDir: string = homedir(),
  cwd: string = process.cwd(),
): string {
  const configured = env.PI_SHOP_TRANSPORT_DIR?.trim();
  if (configured) return isAbsolute(configured) ? configured : resolve(cwd, configured);
  return join(getShopStateRootPath(env, homeDir, cwd), "transport");
}

export function shouldUseWindowsTcpTransport(
  platform: NodeJS.Platform = process.platform,
  env: NodeJS.ProcessEnv = process.env,
): boolean {
  if (platform !== "win32") return false;
  return env.PI_SHOP_TRANSPORT_TRANSPORT?.trim().toLowerCase() === "tcp";
}

export function getBrokerPortFilePath(transportDir: string = getShopTransportDirPath()): string {
  return join(transportDir, "broker.port.json");
}

export function getBrokerPidPath(transportDir: string = getShopTransportDirPath()): string {
  return join(transportDir, "broker.pid");
}

export function getBrokerSpawnLockPath(transportDir: string = getShopTransportDirPath()): string {
  return join(transportDir, "broker.spawn.lock");
}

export function getBrokerStateDirPath(transportDir: string = getShopTransportDirPath()): string {
  return join(transportDir, "state");
}

export function getBrokerSocketPath(
  platform: NodeJS.Platform = process.platform,
  transportDir: string = getShopTransportDirPath(),
): string {
  if (platform === "win32") return `\\\\.\\pipe\\pi-shop-transport-${sanitizePipeSegment(transportDir)}`;
  return join(transportDir, "transport.sock");
}

export function getBrokerConnectTarget(
  platform: NodeJS.Platform = process.platform,
  env: NodeJS.ProcessEnv = process.env,
  transportDir: string = getShopTransportDirPath(env),
): BrokerConnectTarget {
  if (shouldUseWindowsTcpTransport(platform, env)) {
    const endpointFile = getBrokerPortFilePath(transportDir);
    const parsed: unknown = JSON.parse(readFileSync(endpointFile, "utf-8"));
    if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
      throw new Error(`Invalid shop transport TCP endpoint at ${endpointFile}: expected a JSON object`);
    }
    const endpoint = parsed as Record<string, unknown>;
    if (
      endpoint.transport !== "tcp"
      || endpoint.host !== SHOP_TRANSPORT_TCP_HOST
      || typeof endpoint.port !== "number"
      || !Number.isSafeInteger(endpoint.port)
      || endpoint.port <= 0
      || endpoint.port > 65535
      || typeof endpoint.stateId !== "string"
      || endpoint.stateId.length === 0
    ) {
      throw new Error(`Invalid shop transport TCP endpoint at ${endpointFile}`);
    }
    return { transport: "tcp", host: endpoint.host, port: endpoint.port, stateId: endpoint.stateId };
  }
  return getBrokerSocketPath(platform, transportDir);
}

export function getBrokerListenTarget(
  platform: NodeJS.Platform = process.platform,
  env: NodeJS.ProcessEnv = process.env,
  transportDir: string = getShopTransportDirPath(env),
): BrokerConnectTarget {
  if (shouldUseWindowsTcpTransport(platform, env)) {
    return { transport: "tcp", host: SHOP_TRANSPORT_TCP_HOST, port: 0 };
  }
  return getBrokerSocketPath(platform, transportDir);
}

export function ensureShopTransportDir(
  transportDir: string = getShopTransportDirPath(),
  platform: NodeJS.Platform = process.platform,
): void {
  mkdirSync(transportDir, { recursive: true, mode: SHOP_TRANSPORT_DIR_MODE });
  if (platform !== "win32") chmodSync(transportDir, SHOP_TRANSPORT_DIR_MODE);
}

export function restrictShopTransportFile(
  filePath: string,
  platform: NodeJS.Platform = process.platform,
): void {
  if (platform !== "win32") chmodSync(filePath, SHOP_TRANSPORT_FILE_MODE);
}
