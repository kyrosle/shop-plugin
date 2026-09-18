// Ephemeral endpoint discovery; broker remains authority for epoch validation.
import { createHash } from "node:crypto";
import { mkdirSync, readFileSync, renameSync, statSync, unlinkSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import type { ClientIdentity } from "../transport/client/index.ts";

export interface Endpoint extends ClientIdentity {
  endpoint_epoch: string;
  broker_epoch: string;
  expires_at: number;
}

export function endpointPath(root: string, shop: string, member: string): string {
  const key = createHash("sha256").update(JSON.stringify([shop, member])).digest("hex");
  return join(root, "endpoints", `${key}.json`);
}

export function readEndpoint(root: string, shop: string, member: string): Endpoint | undefined {
  try {
    const path = endpointPath(root, shop, member);
    if (statSync(path).size > 8192) return undefined;
    const value = JSON.parse(readFileSync(path, "utf8")) as Endpoint;
    if (value.shop_id !== shop || value.member_id !== member || !value.endpoint_epoch ||
        !value.broker_epoch || !Number.isFinite(value.expires_at) || value.expires_at < Date.now()) return undefined;
    return value;
  } catch { return undefined; }
}

export function publishEndpoint(root: string, identity: ClientIdentity, epoch: string, broker: string): void {
  mkdirSync(join(root, "endpoints"), { recursive: true, mode: 0o700 });
  const path = endpointPath(root, identity.shop_id, identity.member_id);
  const tmp = `${path}.${process.pid}.${epoch}.tmp`;
  try {
    writeFileSync(tmp, JSON.stringify({ ...identity, endpoint_epoch: epoch, broker_epoch: broker,
      expires_at: Date.now() + 20_000 }), { mode: 0o600 });
    renameSync(tmp, path);
  } finally { try { unlinkSync(tmp); } catch { /* renamed or absent */ } }
}

export function removeEndpoint(root: string, identity: ClientIdentity, epoch: string): void {
  const endpoint = readEndpoint(root, identity.shop_id, identity.member_id);
  if (endpoint?.endpoint_epoch !== epoch) return;
  try { unlinkSync(endpointPath(root, identity.shop_id, identity.member_id)); } catch { /* absent */ }
}
