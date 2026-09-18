// Shop built-in transport: broker launch, liveness and spawn locking.
// Written for this package against the versioned Shop transport contract; the liveness
// probe / spawn-lock / health-handshake patterns derive from pi-intercom 0.13.0
// broker/spawn.ts (MIT, sha256
// 63b22e961bf23c5ead19084be6defe4a8c63ee4e614eebf2cb79897b16bfd673,
// Copyright (c) 2026 Nico Bailon). The tsx/npx launch path, Windows VBS
// launcher and intercom paths were removed, not ported. See transport/NOTICE.md.
import { spawn } from "node:child_process";
import { existsSync, openSync, closeSync, readFileSync, unlinkSync, writeFileSync } from "node:fs";
import { connect, type Socket } from "node:net";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { createMessageReader, writeMessage } from "../shared/framing.ts";
import {
  SHOP_TRANSPORT_PROTOCOL_NAME,
  SHOP_TRANSPORT_PROTOCOL_VERSION,
  ensureShopTransportDir,
  getBrokerConnectTarget,
  getBrokerPidPath,
  getBrokerSpawnLockPath,
  getShopTransportDirPath,
  type BrokerConnectTarget,
} from "../shared/paths.ts";
import { TransportError } from "../shared/protocol.ts";

const SPAWN_LOCK_STALE_MS = 30_000;
const STARTUP_STDERR_LIMIT = 4_000;
const HEALTH_TIMEOUT_MS = 10_000;

export interface BrokerLaunchSpec {
  command: string;
  args: string[];
  brokerPath: string;
}

export interface SpawnOptions {
  env?: NodeJS.ProcessEnv;
  transportDir?: string;
  healthTimeoutMs?: number;
  startupStderrLimit?: number;
  platform?: NodeJS.Platform;
}

/** Broker entry shipped inside this package. No tsx, npx, npm or network. */
export function getBrokerPath(): string {
  const here = dirname(fileURLToPath(import.meta.url));
  return join(here, "..", "broker", "broker.ts");
}

export function getBrokerLaunchSpec(): BrokerLaunchSpec {
  const brokerPath = getBrokerPath();
  return { command: process.execPath, args: ["--experimental-strip-types", brokerPath], brokerPath };
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function connectToTarget(target: BrokerConnectTarget): Socket {
  return typeof target === "string" ? connect(target) : connect(target.port, target.host);
}

/** Resolve whether the socket is absent, live Shop transport, or foreign. */
export async function probeBrokerSocket(
  target: BrokerConnectTarget,
  timeoutMs = 1_000,
): Promise<"absent" | "shop_live" | "foreign"> {
  if (typeof target === "string" && !existsSync(target)) return "absent";
  return await new Promise((resolve) => {
    const socket = connectToTarget(target);
    let settled = false;
    const finish = (value: "absent" | "shop_live" | "foreign") => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      socket.destroy();
      resolve(value);
    };
    const timer = setTimeout(() => finish("foreign"), timeoutMs);
    timer.unref?.();
    socket.on("error", () => finish("absent"));
    socket.on("connect", () => writeMessage(socket, { type: "health_check" }));
    socket.on("data", createMessageReader(
      (message) => {
        const raw = message as Record<string, unknown> | null;
        if (raw && raw.type === "health_ok"
          && raw.protocol === SHOP_TRANSPORT_PROTOCOL_NAME
          && raw.version === SHOP_TRANSPORT_PROTOCOL_VERSION) {
          finish("shop_live");
        } else {
          finish("foreign");
        }
      },
      () => finish("foreign"),
    ));
    socket.on("close", () => finish("absent"));
  });
}

function acquireSpawnLock(lockPath: string): boolean {
  try {
    const fd = openSync(lockPath, "wx", 0o600);
    writeFileSync(fd, JSON.stringify({ pid: process.pid, at: Date.now() }));
    closeSync(fd);
    return true;
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== "EEXIST") throw error;
  }
  // Stale lock: owner dead or older than the startup window.
  try {
    const data = JSON.parse(readFileSync(lockPath, "utf8")) as { pid?: number; at?: number };
    const stale = (typeof data.at === "number" && Date.now() - data.at > SPAWN_LOCK_STALE_MS)
      || (typeof data.pid !== "number" || !isProcessAlive(data.pid));
    if (stale) {
      unlinkSync(lockPath);
      return acquireSpawnLock(lockPath);
    }
  } catch {
    try { unlinkSync(lockPath); } catch { /* ignore */ }
  }
  return false;
}

function isProcessAlive(pid: number): boolean {
  try {
    process.kill(pid, 0);
    return true;
  } catch (error) {
    return (error as NodeJS.ErrnoException).code !== "ESRCH";
  }
}

function releaseSpawnLock(lockPath: string): void {
  try { unlinkSync(lockPath); } catch { /* ignore */ }
}

export async function isBrokerHealthy(options: SpawnOptions = {}): Promise<boolean> {
  const target = getBrokerConnectTarget(options.platform, options.env, options.transportDir);
  return await probeBrokerSocket(target) === "shop_live";
}

/**
 * Ensure one live Shop transport broker owns the Shop-private socket.
 * Reuses a live broker, refuses a foreign socket, and never kills unknown PIDs.
 */
export async function spawnBrokerIfNeeded(options: SpawnOptions = {}): Promise<{ spawned: boolean; pid?: number }> {
  const transportDir = options.transportDir ?? getShopTransportDirPath(options.env);
  ensureShopTransportDir(transportDir, options.platform);
  const target = getBrokerConnectTarget(options.platform, options.env, transportDir);
  const first = await probeBrokerSocket(target);
  if (first === "shop_live") return { spawned: false };
  if (first === "foreign") {
    throw new TransportError("E_FOREIGN_SOCKET",
      "shop transport socket path is owned by another protocol; refusing to touch it");
  }

  const lockPath = getBrokerSpawnLockPath(transportDir);
  if (!acquireSpawnLock(lockPath)) {
    // Another launcher is starting the broker; wait for it instead of racing.
    await waitForBroker(options);
    return { spawned: false };
  }
  try {
    const recheck = await probeBrokerSocket(target);
    if (recheck === "shop_live") return { spawned: false };
    if (recheck === "foreign") {
      throw new TransportError("E_FOREIGN_SOCKET",
        "shop transport socket path is owned by another protocol; refusing to touch it");
    }
    const pidPath = getBrokerPidPath(transportDir);
    const spec = getBrokerLaunchSpec();
    let stderr = "";
    const child = spawn(spec.command, spec.args, {
      cwd: dirname(spec.brokerPath),
      env: { ...process.env, ...options.env },
      stdio: ["ignore", "ignore", "pipe"],
      detached: true,
    });
    child.stderr?.on("data", (chunk: Buffer) => {
      if (stderr.length < (options.startupStderrLimit ?? STARTUP_STDERR_LIMIT)) {
        stderr += chunk.toString("utf8").slice(0, (options.startupStderrLimit ?? STARTUP_STDERR_LIMIT) - stderr.length);
      }
    });
    child.unref();
    try {
      await waitForBroker(options);
    } catch (error) {
      throw new TransportError("E_INTERNAL",
        `broker did not become healthy: ${stderr.trim() || (error as Error).message}`, { outcomeKnown: false });
    }
    const pid = existsSync(pidPath) ? Number.parseInt(readFileSync(pidPath, "utf8").trim(), 10) : undefined;
    return { spawned: true, ...(Number.isSafeInteger(pid) ? { pid: pid as number } : {}) };
  } finally {
    releaseSpawnLock(lockPath);
  }
}

export async function waitForBroker(options: SpawnOptions = {}): Promise<void> {
  const deadline = Date.now() + (options.healthTimeoutMs ?? HEALTH_TIMEOUT_MS);
  while (Date.now() < deadline) {
    if (await isBrokerHealthy(options)) return;
    await sleep(50);
  }
  throw new Error("timed out waiting for shop transport broker health");
}
