// Shop built-in transport broker (standalone, host-agnostic).
// Written for this package against the versioned Shop transport contract; the session
// registry / delivery-record / receipt-routing / idle-shutdown / pid+socket
// lifecycle patterns derive from pi-intercom 0.13.0 broker/broker.ts (MIT,
// sha256 65d9ec1dbed5368ea952a1c7c3f978c83b44cd092e1922509085aa070f06829b,
// Copyright (c) 2026 Nico Bailon). Mailbox/disconnected-redelivery, ask edges,
// namespace-owner/extension-bus, `sameCwd` fallback and supersede were removed,
// not ported. No @earendil-works/pi-coding-agent import. See transport/NOTICE.md.
import { randomUUID } from "node:crypto";
import { existsSync, mkdirSync, readFileSync, unlinkSync, writeFileSync } from "node:fs";
import { createServer, connect, type Server, type Socket } from "node:net";
import { createMessageReader, writeMessage } from "../shared/framing.ts";
import {
  SHOP_TRANSPORT_PROTOCOL_NAME,
  SHOP_TRANSPORT_PROTOCOL_VERSION,
  ensureShopTransportDir,
  getBrokerPidPath,
  getBrokerPortFilePath,
  getBrokerSocketPath,
  getShopTransportDirPath,
  restrictShopTransportFile,
  shouldUseWindowsTcpTransport,
  type BrokerConnectTarget,
} from "../shared/paths.ts";
import {
  TRANSPORT_FEATURES,
  TransportError,
  parseCancelRequest,
  parseClientMessage,
  parseHealthCheck,
  parseHello,
  parseReceipt,
  parseSend,
} from "../shared/protocol.ts";
import { assertNoLiveBrokerProcess } from "../shared/runtime-claim.ts";
import type {
  ClientMessage,
  DeliveryRecord,
  ErrorMessage,
  HelloMessage,
  LiveEndpoint,
  MessageControlMessage,
  ReceiptMessage,
  SendMessage,
  TransportErrorCode,
} from "../shared/types.ts";

export interface BrokerOptions {
  transportDir?: string;
  idleExitMs?: number;
  registrationTimeoutMs?: number;
  unregisteredLimit?: number;
  sessionLimit?: number;
  deliveryRecordLimit?: number;
  deliveryRecordTtlMs?: number;
  rateBurst?: number;
  ratePerSecond?: number;
  env?: NodeJS.ProcessEnv;
  platform?: NodeJS.Platform;
  now?: () => number;
  quiet?: boolean;
}

interface Connection {
  id: number;
  socket: Socket;
  endpoint: LiveEndpoint | null;
  tokens: number;
  lastRefill: number;
  registeredAt: number;
}

export interface BrokerStartResult {
  socketPath: string;
  pid: number;
  brokerEpoch: string;
  tcp?: { host: string; port: number };
}

export type SocketProbe = "absent" | "shop_live" | "foreign";

/** True when the reply identifies a live broker speaking this exact protocol. */
export function isShopHealthReply(message: unknown): boolean {
  if (typeof message !== "object" || message === null) return false;
  const raw = message as Record<string, unknown>;
  return raw.type === "health_ok"
    && raw.protocol === SHOP_TRANSPORT_PROTOCOL_NAME
    && raw.version === SHOP_TRANSPORT_PROTOCOL_VERSION;
}

function endpointKey(memberId: string, launchId: string): string {
  return `${memberId}\u0000${launchId}`;
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

export class ShopTransportBroker {
  private readonly options: Required<Pick<BrokerOptions, "idleExitMs" | "registrationTimeoutMs"
  | "unregisteredLimit" | "sessionLimit" | "deliveryRecordLimit" | "deliveryRecordTtlMs"
  | "rateBurst" | "ratePerSecond">> & BrokerOptions;
  private readonly transportDir: string;
  private readonly brokerEpoch = randomUUID();
  private server: Server | null = null;
  private connections = new Set<Connection>();
  private byEndpointKey = new Map<string, Connection>();
  private byEpoch = new Map<string, Connection>();
  private deliveries = new Map<string, DeliveryRecord>();
  private replyRoutes = new Map<string, { fromMemberId: string; toMemberId: string; at: number }>();
  private nextConnectionId = 1;
  private idleTimer: NodeJS.Timeout | null = null;
  private shuttingDown = false;
  private stopped: Promise<void> | null = null;

  constructor(options: BrokerOptions = {}) {
    this.options = {
      idleExitMs: 5_000,
      registrationTimeoutMs: 1_000,
      unregisteredLimit: 32,
      sessionLimit: 128,
      deliveryRecordLimit: 4096,
      deliveryRecordTtlMs: 60 * 60 * 1000,
      rateBurst: 240,
      ratePerSecond: 120,
      ...options,
    };
    this.transportDir = this.options.transportDir ?? getShopTransportDirPath(this.options.env);
  }

  get epoch(): string {
    return this.brokerEpoch;
  }

  private now(): number {
    return this.options.now ? this.options.now() : Date.now();
  }

  private log(...args: unknown[]): void {
    if (!this.options.quiet) console.error("[shop-transport]", ...args);
  }

  private listenTarget(): BrokerConnectTarget {
    return this.options.platform === "win32" && shouldUseWindowsTcpTransport(this.options.platform, this.options.env)
      ? { transport: "tcp", host: "127.0.0.1", port: 0 }
      : getBrokerSocketPath(this.options.platform, this.transportDir);
  }

  /** Probe the socket path without adopting a foreign broker. */
  static async probeSocket(
    target: BrokerConnectTarget,
    timeoutMs = 500,
  ): Promise<SocketProbe> {
    if (typeof target === "string" && !existsSync(target)) return "absent";
    return await new Promise<SocketProbe>((resolve) => {
      const socket = typeof target === "string" ? connect(target) : connect(target.port, target.host);
      let settled = false;
      const finish = (value: SocketProbe) => {
        if (settled) return;
        settled = true;
        socket.destroy();
        resolve(value);
      };
      const timer = setTimeout(() => finish("foreign"), timeoutMs);
      timer.unref?.();
      socket.on("error", () => { clearTimeout(timer); finish("absent"); });
      socket.on("connect", () => {
        writeMessage(socket, { type: "health_check" });
      });
      socket.on("data", createMessageReader(
        (message) => {
          clearTimeout(timer);
          if (isShopHealthReply(message)) finish("shop_live");
          else finish("foreign");
        },
        () => { clearTimeout(timer); finish("foreign"); },
      ));
      socket.on("close", () => { clearTimeout(timer); finish("absent"); });
    });
  }

  async start(): Promise<BrokerStartResult> {
    ensureShopTransportDir(this.transportDir, this.options.platform);
    assertNoLiveBrokerProcess(getBrokerPidPath(this.transportDir));
    const target = this.listenTarget();
    const probe = await ShopTransportBroker.probeSocket(target);
    if (probe === "shop_live") {
      throw new TransportError("E_INTERNAL", "a live shop transport broker already owns this socket");
    }
    if (probe === "foreign") {
      throw new TransportError("E_FOREIGN_SOCKET",
        `socket ${typeof target === "string" ? target : target.port} is owned by another protocol; refusing to touch it`);
    }
    if (typeof target === "string" && existsSync(target)) {
      try { unlinkSync(target); } catch { /* stale socket removal is best effort */ }
    }

    this.server = createServer((socket) => this.handleConnection(socket));
    await new Promise<void>((resolve, reject) => {
      const server = this.server as Server;
      server.once("error", reject);
      if (typeof target === "string") {
        server.listen(target, () => { server.off("error", reject); resolve(); });
      } else {
        server.listen(target.port, target.host, () => { server.off("error", reject); resolve(); });
      }
    });

    const result: BrokerStartResult = {
      socketPath: typeof target === "string" ? target : "tcp",
      pid: process.pid,
      brokerEpoch: this.brokerEpoch,
    };
    if (typeof target !== "string") {
      result.tcp = { host: target.host, port: (this.server.address() as { port: number }).port };
      const portFile = getBrokerPortFilePath(this.transportDir);
      writeFileSync(portFile, JSON.stringify({
        transport: "tcp", host: target.host,
        port: result.tcp.port, stateId: randomUUID(),
      }), { mode: 0o600 });
      restrictShopTransportFile(portFile, this.options.platform);
    }
    const pidFile = getBrokerPidPath(this.transportDir);
    writeFileSync(pidFile, `${process.pid}\n`, { mode: 0o600 });
    restrictShopTransportFile(pidFile, this.options.platform);
    this.log(`broker ${process.pid} listening (${result.socketPath}) epoch ${this.brokerEpoch}`);
    return result;
  }

  private handleConnection(socket: Socket): void {
    const connection: Connection = {
      id: this.nextConnectionId++,
      socket,
      endpoint: null,
      tokens: this.options.rateBurst,
      lastRefill: this.now(),
      registeredAt: this.now(),
    };
    this.connections.add(connection);
    this.cancelIdleShutdown();
    this.evictUnregistered();

    const registrationTimer = setTimeout(() => {
      if (!connection.endpoint) {
        this.sendError(connection, new TransportError("E_NOT_REGISTERED", "hello not received in time"));
        this.closeConnection(connection);
      }
    }, this.options.registrationTimeoutMs);
    registrationTimer.unref?.();

    socket.on("data", createMessageReader(
      (raw) => this.handleMessage(connection, raw),
      (error) => {
        this.sendError(connection, new TransportError("E_MALFORMED", error.message));
        this.closeConnection(connection);
      },
    ));
    socket.on("error", () => this.closeConnection(connection));
    socket.on("close", () => this.closeConnection(connection));
  }

  private handleMessage(connection: Connection, raw: unknown): void {
    const rawType = (typeof raw === "object" && raw !== null)
      ? (raw as Record<string, unknown>).type : undefined;
    // Liveness probes bypass the rate limiter: a client must always be able to
    // tell a live broker from a stale socket, even while sending at the limit.
    if (rawType !== "health_check" && !this.consumeToken(connection)) {
      this.sendError(connection, new TransportError("E_RATE_LIMITED", "per-connection rate limit exceeded",
        { retryable: true }));
      return;
    }

    let message: ClientMessage;
    try {
      if (rawType === "health_check") {
        // Liveness probe: answered before registration so a client can tell a
        // live Shop broker from a stale socket or a foreign protocol.
        const health = parseHealthCheck(raw);
        this.send(connection, {
          type: "health_ok", protocol: SHOP_TRANSPORT_PROTOCOL_NAME,
          version: SHOP_TRANSPORT_PROTOCOL_VERSION, broker_epoch: this.brokerEpoch,
          pid: process.pid, ...(health.request_id ? { request_id: health.request_id } : {}),
        });
        return;
      }
      message = parseClientMessage(raw);
    } catch (error) {
      const failure = error instanceof TransportError ? error : new TransportError("E_MALFORMED", String(error));
      if (rawType === "hello") {
        // A hello that fails protocol/version/schema checks is answered as a
        // rejection, never as a generic error, and the connection closes.
        this.rejectHello(connection, failure.code, failure.detail);
        return;
      }
      this.sendError(connection, failure);
      if (failure.code === "E_PROTOCOL_UNSUPPORTED" || failure.code === "E_VERSION_UNSUPPORTED") {
        this.closeConnection(connection);
      }
      return;
    }

    if (message.type === "hello") return this.handleHello(connection, message);
    if (!connection.endpoint) {
      this.sendError(connection, new TransportError("E_NOT_REGISTERED", "message received before hello_ok"));
      return;
    }
    switch (message.type) {
      case "send": return this.handleSend(connection, message);
      case "receipt": return this.handleReceipt(connection, message);
      case "cancel": return this.handleCancel(connection, message);
      default: return;
    }
  }

  private handleHello(connection: Connection, hello: HelloMessage): void {
    if (connection.endpoint) {
      this.sendError(connection, new TransportError("E_MALFORMED", "duplicate hello on one connection"));
      return;
    }
    const complete = [hello.shop_id, hello.run_id, hello.member_id, hello.launch_id,
      hello.session_id, hello.terminal_id, hello.pane_id];
    if (complete.some((value) => typeof value !== "string" || value.trim().length === 0)) {
      this.rejectHello(connection, "E_IDENTITY_INCOMPLETE", "identity fields must be nonempty");
      return;
    }
    if (this.connections.size > this.options.sessionLimit && !connection.endpoint) {
      this.rejectHello(connection, "E_INTERNAL", `session limit ${this.options.sessionLimit} reached`);
      return;
    }
    const key = endpointKey(hello.member_id, hello.launch_id);
    const incumbent = this.byEndpointKey.get(key);
    if (incumbent && incumbent !== connection) {
      // Never pick by order: keep the registered occupant, reject the newcomer.
      this.rejectHello(connection, "E_TARGET_DUPLICATE_REGISTRATION",
        `member ${hello.member_id} launch ${hello.launch_id} is already registered`);
      return;
    }
    const endpoint: LiveEndpoint = {
      member_id: hello.member_id,
      launch_id: hello.launch_id,
      session_id: hello.session_id,
      terminal_id: hello.terminal_id,
      pane_id: hello.pane_id,
      shop_id: hello.shop_id,
      run_id: hello.run_id,
      endpoint_epoch: randomUUID(),
      connected_at: this.now(),
    };
    connection.endpoint = endpoint;
    this.byEndpointKey.set(key, connection);
    this.byEpoch.set(endpoint.endpoint_epoch, connection);
    this.send(connection, {
      type: "hello_ok",
      protocol: SHOP_TRANSPORT_PROTOCOL_NAME,
      version: SHOP_TRANSPORT_PROTOCOL_VERSION,
      endpoint_epoch: endpoint.endpoint_epoch,
      broker_epoch: this.brokerEpoch,
      features: [...TRANSPORT_FEATURES],
    });
  }

  private rejectHello(connection: Connection, code: TransportErrorCode, detail: string): void {
    this.send(connection, { type: "hello_rejected", code, detail });
    this.closeConnection(connection);
  }

  private handleSend(connection: Connection, message: SendMessage): void {
    const endpoint = connection.endpoint as LiveEndpoint;
    try {
      this.assertSenderIdentity(endpoint, message);
      if (message.to.member_id === endpoint.member_id && message.to.launch_id === endpoint.launch_id) {
        throw new TransportError("E_SELF_TARGET", "sender cannot target itself");
      }
      const record = this.deliveries.get(message.message_id);
      if (record) {
        if (record.payload_sha256 !== message.payload_sha256) {
          throw new TransportError("E_MESSAGE_ID_REUSE",
            `message_id ${message.message_id} was already used with different content`);
        }
        // Same id + same fingerprint: replay the recorded transport result only.
        this.send(connection, {
          type: "delivered", message_id: record.message_id, to_endpoint_epoch: record.to_endpoint_epoch,
          delivery: "socket_delivered", at: record.at, replayed: true,
        });
        if (record.receipt_status) {
          this.send(connection, {
            type: "receipt", message_id: record.message_id, from_endpoint_epoch: record.to_endpoint_epoch,
            status: record.receipt_status, at: this.now(), detail: "replayed",
          });
        }
        return;
      }
      if (message.reply_to !== null) {
        const route = this.replyRoutes.get(message.reply_to);
        if (!route || route.fromMemberId !== endpoint.member_id || route.toMemberId !== message.to.member_id) {
          throw new TransportError("E_UNKNOWN_MESSAGE",
            `reply_to ${message.reply_to} is not a message delivered from this sender to that recipient`);
        }
      }
      const targetKey = endpointKey(message.to.member_id, message.to.launch_id);
      const target = this.byEndpointKey.get(targetKey);
      if (!target || !target.endpoint) {
        throw new TransportError("E_TARGET_NOT_FOUND",
          `no live endpoint for ${message.to.member_id}/${message.to.launch_id}`);
      }
      if (target.endpoint.endpoint_epoch !== message.to.endpoint_epoch) {
        throw new TransportError("E_TARGET_STALE_EPOCH",
          `endpoint epoch for ${message.to.member_id}/${message.to.launch_id} was superseded`);
      }
      const at = this.now();
      // `queued` is intentionally never produced: this version has no mailbox,
      // so a write to a live socket is the only delivery evidence; it does not prove injection.
      try {
        this.send(target, { ...message, created_at: message.created_at });
      } catch (error) {
        this.send(connection, {
          type: "delivery_failed", message_id: message.message_id, code: "E_INTERNAL",
          retryable: true, outcome_known: false,
          detail: `receiver write failed: ${error instanceof Error ? error.message : String(error)}`,
        });
        return;
      }
      this.deliveries.set(message.message_id, {
        message_id: message.message_id,
        payload_sha256: message.payload_sha256,
        from_member_id: endpoint.member_id,
        from_endpoint_epoch: endpoint.endpoint_epoch,
        to_member_id: message.to.member_id,
        to_launch_id: message.to.launch_id,
        to_endpoint_epoch: message.to.endpoint_epoch,
        at,
        injected: false,
      });
      this.replyRoutes.set(message.message_id, {
        fromMemberId: endpoint.member_id, toMemberId: message.to.member_id, at,
      });
      this.pruneRecords();
      this.send(connection, {
        type: "delivered", message_id: message.message_id,
        to_endpoint_epoch: message.to.endpoint_epoch, delivery: "socket_delivered", at,
      });
    } catch (error) {
      const failure = error instanceof TransportError ? error : new TransportError("E_INTERNAL", String(error), { outcomeKnown: false });
      this.sendError(connection, failure, "send", message.message_id);
    }
  }

  private assertSenderIdentity(endpoint: LiveEndpoint, message: SendMessage): void {
    if (message.from.member_id !== endpoint.member_id
      || message.from.launch_id !== endpoint.launch_id
      || message.from.session_id !== endpoint.session_id
      || message.from.endpoint_epoch !== endpoint.endpoint_epoch) {
      throw new TransportError("E_RUN_MISMATCH", "send.from does not match this connection identity");
    }
    const payload = message.payload;
    const shopId = payload.shop_id;
    const runId = payload.run_id;
    if (typeof shopId === "string" && shopId !== endpoint.shop_id) {
      throw new TransportError("E_SHOP_MISMATCH", "payload shop_id does not match connection");
    }
    if (typeof runId === "string" && runId !== endpoint.run_id) {
      throw new TransportError("E_RUN_MISMATCH", "payload run_id does not match connection");
    }
  }

  private handleReceipt(connection: Connection, message: ReceiptMessage): void {
    const endpoint = connection.endpoint as LiveEndpoint;
    try {
      if (message.from_endpoint_epoch !== endpoint.endpoint_epoch) {
        throw new TransportError("E_TARGET_STALE_EPOCH", "receipt endpoint epoch does not match this connection");
      }
      const record = this.deliveries.get(message.message_id);
      if (!record) throw new TransportError("E_UNKNOWN_MESSAGE", `unknown message_id ${message.message_id}`);
      if (record.to_member_id !== endpoint.member_id) {
        throw new TransportError("E_RUN_MISMATCH", "receipt sender is not the recorded recipient");
      }
      record.receipt_status = message.status;
      if (message.status === "injected") record.injected = true;
      const sender = record.from_endpoint_epoch ? this.byEpoch.get(record.from_endpoint_epoch) : undefined;
      if (sender && sender.endpoint) {
        this.send(sender, {
          type: "receipt", message_id: message.message_id,
          from_endpoint_epoch: endpoint.endpoint_epoch, status: message.status,
          at: message.at, ...(message.detail ? { detail: message.detail } : {}),
        });
      }
    } catch (error) {
      const failure = error instanceof TransportError ? error : new TransportError("E_INTERNAL", String(error));
      this.sendError(connection, failure, "receipt");
    }
  }

  private handleCancel(connection: Connection, message: { message_id: string }): void {
    const endpoint = connection.endpoint as LiveEndpoint;
    try {
      const record = this.deliveries.get(message.message_id);
      if (!record || record.from_member_id !== endpoint.member_id) {
        throw new TransportError("E_UNKNOWN_MESSAGE", `cancel for unknown message_id ${message.message_id}`);
      }
      if (record.receipt_status === "injected" || record.injected) {
        this.send(connection, {
          type: "cancel_result", message_id: message.message_id, ok: false,
          code: "E_CANCEL_TOO_LATE", detail: "message was already injected", outcome_known: false,
        });
        return;
      }
      const target = this.byEpoch.get(record.to_endpoint_epoch);
      if (!target || !target.endpoint) {
        this.send(connection, {
          type: "cancel_result", message_id: message.message_id, ok: false,
          code: "E_UNKNOWN_MESSAGE", detail: "target endpoint is no longer live", outcome_known: true,
        });
        return;
      }
      const control: MessageControlMessage = {
        type: "message_control", message_id: message.message_id, action: "cancelled", at: this.now(),
      };
      this.send(target, control);
      this.send(connection, { type: "cancel_result", message_id: message.message_id, ok: true });
    } catch (error) {
      const failure = error instanceof TransportError ? error : new TransportError("E_INTERNAL", String(error));
      this.sendError(connection, failure, "cancel");
    }
  }

  private sendError(connection: Connection, error: TransportError, requestType?: string, messageId?: string): void {
    const payload: ErrorMessage = error.toErrorMessage(requestType);
    if (messageId) payload.detail = `${payload.detail} (message_id ${messageId})`;
    this.send(connection, payload);
  }

  private send(connection: Connection, message: unknown): void {
    if (connection.socket.destroyed) return;
    try {
      writeMessage(connection.socket, message);
    } catch (error) {
      this.log("write failed", error instanceof Error ? error.message : error);
    }
  }

  private consumeToken(connection: Connection): boolean {
    const now = this.now();
    const elapsed = Math.max(0, now - connection.lastRefill);
    connection.lastRefill = now;
    connection.tokens = Math.min(this.options.rateBurst,
      connection.tokens + (elapsed / 1000) * this.options.ratePerSecond);
    if (connection.tokens < 1) return false;
    connection.tokens -= 1;
    return true;
  }

  private evictUnregistered(): void {
    const unregistered = [...this.connections].filter((connection) => !connection.endpoint);
    while (unregistered.length > this.options.unregisteredLimit) {
      const oldest = unregistered.shift();
      if (oldest) this.closeConnection(oldest);
    }
  }

  private closeConnection(connection: Connection): void {
    if (!this.connections.has(connection)) return;
    this.connections.delete(connection);
    if (connection.endpoint) {
      const key = endpointKey(connection.endpoint.member_id, connection.endpoint.launch_id);
      if (this.byEndpointKey.get(key) === connection) this.byEndpointKey.delete(key);
      this.byEpoch.delete(connection.endpoint.endpoint_epoch);
      connection.endpoint = null;
    }
    if (!connection.socket.destroyed) connection.socket.destroy();
    if (this.connections.size === 0 && !this.shuttingDown) this.scheduleIdleShutdown();
  }

  private pruneRecords(): void {
    const cutoff = this.now() - this.options.deliveryRecordTtlMs;
    for (const [messageId, record] of this.deliveries) {
      if (record.at < cutoff) {
        this.deliveries.delete(messageId);
        this.replyRoutes.delete(messageId);
      }
    }
    while (this.deliveries.size > this.options.deliveryRecordLimit) {
      const oldest = [...this.deliveries.values()].sort((a, b) => a.at - b.at)[0];
      if (!oldest) break;
      this.deliveries.delete(oldest.message_id);
      this.replyRoutes.delete(oldest.message_id);
    }
  }

  private scheduleIdleShutdown(): void {
    if (this.idleTimer) return;
    this.idleTimer = setTimeout(() => {
      this.idleTimer = null;
      if (this.connections.size === 0) void this.shutdown("idle");
    }, this.options.idleExitMs);
    this.idleTimer.unref?.();
  }

  private cancelIdleShutdown(): void {
    if (this.idleTimer) {
      clearTimeout(this.idleTimer);
      this.idleTimer = null;
    }
  }

  async shutdown(reason = "signal"): Promise<void> {
    if (this.stopped) return this.stopped;
    this.shuttingDown = true;
    this.cancelIdleShutdown();
    this.stopped = (async () => {
      for (const connection of [...this.connections]) this.closeConnection(connection);
      if (this.server) {
        await new Promise<void>((resolve) => this.server?.close(() => resolve()));
        this.server = null;
      }
      for (const path of [typeof this.listenTarget() === "string" ? this.listenTarget() as string : null,
        getBrokerPidPath(this.transportDir), getBrokerPortFilePath(this.transportDir)]) {
        if (path && existsSync(path)) {
          try { unlinkSync(path); } catch { /* best effort */ }
        }
      }
      mkdirSync(this.transportDir, { recursive: true, mode: 0o700 });
      this.log(`broker stopped (${reason})`);
    })();
    return this.stopped;
  }
}

async function main(): Promise<void> {
  const broker = new ShopTransportBroker({
    transportDir: getShopTransportDirPath(),
    idleExitMs: Number(process.env.PI_SHOP_TRANSPORT_IDLE_EXIT_MS ?? 5_000),
  });
  const result = await broker.start();
  process.stdout.write(JSON.stringify(result) + "\n");
  const stop = (signal: NodeJS.Signals) => {
    void broker.shutdown(signal).then(() => process.exit(0));
  };
  process.on("SIGTERM", () => stop("SIGTERM"));
  process.on("SIGINT", () => stop("SIGINT"));
}

const invokedDirectly = process.argv[1] !== undefined
  && /broker\.(ts|js)$/.test(process.argv[1]);
if (invokedDirectly) {
  main().catch((error) => {
    const failure = error instanceof TransportError ? error : new TransportError("E_INTERNAL", String(error), { outcomeKnown: false });
    process.stdout.write(JSON.stringify({ error: failure.code, detail: failure.detail }) + "\n");
    process.exit(1);
  });
}

export { sleep };
