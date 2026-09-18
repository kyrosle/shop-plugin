// Ported from pi-intercom 0.13.0 (MIT), npm tarball sha256
// 1d89bd31ca63cccd82a5c950cc043bca99e17e4664df7fd99f16859a0442279f
// Upstream ref: npm gitHead 199279ae861bf53ce014809fb2a03337538ae13e
// Original: broker/runtime-claim.ts (sha256 e2290bba58d88f4b0ba5310c76fc1575d62cea70139358f741ac59777aac9e51)
// Copyright (c) 2026 Nico Bailon. Modified for Shop built-in transport:
// error text re-scoped; logic unchanged (never replace a live PID).
// See transport/NOTICE.md.
import { existsSync, readFileSync } from "node:fs";

export function assertNoLiveBrokerProcess(pidPath: string): void {
  if (!existsSync(pidPath)) return;

  let pid: number;
  try {
    pid = Number.parseInt(readFileSync(pidPath, "utf8").trim(), 10);
  } catch {
    return;
  }
  if (!Number.isSafeInteger(pid) || pid <= 0) return;

  try {
    process.kill(pid, 0);
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ESRCH") return;
    throw error;
  }
  throw new Error(`Refusing to replace live shop transport broker process ${pid}`);
}
