// Shop built-in transport client.
// Written for this package against the versioned Shop transport contract; connect/reconnect,
// framing read loop, request correlation, liveness heartbeat and send-timeout
// patterns derive from pi-intercom 0.13.0 broker/client.ts (MIT, sha256
// c810c80fdc0147dc9d7b636f071b82db7a50383dee6b860a6fe6cc0946f6212d,
// Copyright (c) 2026 Nico Bailon). Name/prefix/cwd target resolution, extension
// bus, cancelAsk, presence and mailbox methods were removed, not ported; `send`
// requires the exact (member_id, launch_id, endpoint_epoch) triple and reports
// `unknown` on post-write loss. See transport/NOTICE.md.
import { EventEmitter } from "node:events";
import { randomUUID } from "node:crypto";
import { connect, type Socket } from "node:net";
import { createMessageReader, writeMessage } from "../shared/framing.ts";
import {
  SHOP_TRANSPORT_PROTOCOL_NAME,
  SHOP_TRANSPORT_PROTOCOL_VERSION,
  getBrokerConnectTarget,
  getShopTransportDirPath,
} from "../shared/paths.ts";
import {
  TransportError,
  payloadFingerprint,
  parseBrokerMessage,
} from "../shared/protocol.ts";
import type {
  CancelResultMessage,
  DeliveredMessage,
  DeliveryKind,
  MessageControlMessage,
  MessageKind,
  ReceiptMessage,
  SendMessage,
  SendTargetRef,
} from "../shared/types.ts";
import { spawnBrokerIfNeeded } from "./spawn.ts";

export interface ClientIdentity {
  shop_id: string;
  run_id: string;
  member_id: string;
  launch_id: string;
  session_id: string;
  terminal_id: string;
  pane_id: string;
  previous_endpoint_epoch?: string;
}

export interface SendRequest {
  message_id?: string;
  to: SendTargetRef;
  kind: MessageKind;
  reply_to: string | null;
  payload: Record<string, unknown>;
}

export type SendOutcomeName = "not_submitted" | "unknown" | "submitted" | "delivered";

export interface SendOutcome {
  outcome: SendOutcomeName;
  message_id: string;
  delivery?: DeliveryKind;
  code?: string;
  detail?: string;
  retryable?: boolean;
  outcome_known?: boolean;
  replayed?: boolean;
}

export interface ClientOptions {
  env?: NodeJS.ProcessEnv;
  transportDir?: string;
  sendTimeoutMs?: number;
  requestTimeoutMs?: number;
  livenessIntervalMs?: number;
  livenessTimeoutMs?: number;
  autoSpawn?: boolean;
}

interface PendingRequest {
  resolve: (value: unknown) => void;
  timer: NodeJS.Timeout;
  messageId: string;
}

export class ShopTransportClient extends EventEmitter {
  private readonly options: ClientOptions;
  private socket: Socket | null = null;
  private endpointEpoch: string | null = null;
  private brokerEpochValue: string | null = null;
  private featureSet = new Set<string>();
  private pendingSends = new Map<string, PendingRequest>();
  private pendingCancels = new Map<string, PendingRequest>();
  private pendingHello: ((value: unknown) => void) | null = null;
  private endpointIdentity: ClientIdentity | null = null;
  private livenessTimer: NodeJS.Timeout | null = null;
  private disconnecting = false;

  constructor(options: ClientOptions = {}) {
    super();
    this.options = options;
  }

  get connected(): boolean {
    return this.socket !== null && this.endpointEpoch !== null;
  }

  get endpoint_epoch(): string | null {
    return this.endpointEpoch;
  }

  get broker_epoch(): string | null {
    return this.brokerEpochValue;
  }

  supportsFeature(feature: string): boolean {
    return this.featureSet.has(feature);
  }

  async connect(identity: ClientIdentity): Promise<void> {
    if (this.connected) return;
    if (this.options.autoSpawn !== false) {
      await spawnBrokerIfNeeded({
        env: this.options.env,
        transportDir: this.options.transportDir ?? getShopTransportDirPath(this.options.env),
      });
    }
    const target = getBrokerConnectTarget(undefined, this.options.env,
      this.options.transportDir ?? getShopTransportDirPath(this.options.env));
    const socket = typeof target === "string" ? connect(target) : connect(target.port, target.host);
    this.socket = socket;
    socket.on("error", () => this.failConnection("socket error"));
    socket.on("close", () => this.failConnection("socket closed"));
    socket.on("data", createMessageReader(
      (message) => this.handleMessage(message),
      (error) => this.failConnection(error.message),
    ));
    await new Promise<void>((resolve, reject) => {
      const timer = setTimeout(() => reject(new TransportError("E_INTERNAL", "broker connect timed out",
        { retryable: true, outcomeKnown: false })), this.options.requestTimeoutMs ?? 5_000);
      timer.unref?.();
      socket.once("connect", () => { clearTimeout(timer); resolve(); });
      socket.once("error", (error) => { clearTimeout(timer); reject(error); });
    });

    const hello = {
      type: "hello" as const,
      protocol: SHOP_TRANSPORT_PROTOCOL_NAME,
      version: SHOP_TRANSPORT_PROTOCOL_VERSION,
      shop_id: identity.shop_id,
      run_id: identity.run_id,
      member_id: identity.member_id,
      launch_id: identity.launch_id,
      session_id: identity.session_id,
      terminal_id: identity.terminal_id,
      pane_id: identity.pane_id,
      ...(identity.previous_endpoint_epoch ? { previous_endpoint_epoch: identity.previous_endpoint_epoch } : {}),
    };
    const reply = await new Promise<unknown>((resolve, reject) => {
      const timer = setTimeout(() => reject(new TransportError("E_INTERNAL", "hello timed out",
        { retryable: true, outcomeKnown: false })), this.options.requestTimeoutMs ?? 5_000);
      timer.unref?.();
      this.pendingHello = (value) => { clearTimeout(timer); resolve(value); };
    });
    const parsed = reply as Record<string, unknown>;
    if (parsed.type === "hello_rejected") {
      this.disconnectSocket();
      throw new TransportError(parsed.code as TransportError["code"],
        String(parsed.detail ?? "hello rejected"));
    }
    if (parsed.type !== "hello_ok") {
      this.disconnectSocket();
      throw new TransportError("E_MALFORMED", `unexpected hello reply ${String(parsed.type)}`);
    }
    this.endpointEpoch = String(parsed.endpoint_epoch);
    this.brokerEpochValue = String(parsed.broker_epoch);
    this.featureSet = new Set((parsed.features as string[]) ?? []);
    this.endpointIdentity = identity;
    this.startLiveness();
  }

  private startLiveness(): void {
    this.stopLiveness();
    const interval = this.options.livenessIntervalMs ?? 30_000;
    if (interval <= 0) return;
    this.livenessTimer = setInterval(() => {
      if (!this.socket || this.socket.destroyed) return;
      try {
        writeMessage(this.socket, { type: "health_check" });
      } catch {
        this.failConnection("liveness write failed");
      }
    }, interval);
    this.livenessTimer.unref?.();
  }

  private stopLiveness(): void {
    if (this.livenessTimer) {
      clearInterval(this.livenessTimer);
      this.livenessTimer = null;
    }
  }

  private failConnection(reason: string): void {
    if (this.disconnecting) return;
    // Post-write loss: every in-flight request becomes `unknown`, never retried.
    for (const [messageId, pending] of this.pendingSends) {
      clearTimeout(pending.timer);
      pending.resolve({
        outcome: "unknown", message_id: messageId, code: "E_INTERNAL",
        detail: `connection lost: ${reason}`, retryable: true, outcome_known: false,
      } satisfies SendOutcome);
    }
    this.pendingSends.clear();
    for (const pending of this.pendingCancels.values()) {
      clearTimeout(pending.timer);
      pending.resolve({ type: "cancel_result", message_id: pending.messageId, ok: false,
        code: "E_INTERNAL", detail: `connection lost: ${reason}`, outcome_known: false });
    }
    this.pendingCancels.clear();
    if (this.pendingHello) {
      const resolve = this.pendingHello;
      this.pendingHello = null;
      resolve({ type: "hello_rejected", code: "E_INTERNAL", detail: `connection lost: ${reason}` });
    }
    this.disconnectSocket();
  }

  private disconnectSocket(): void {
    this.stopLiveness();
    this.endpointEpoch = null;
    this.endpointIdentity = null;
    this.featureSet = new Set();
    const socket = this.socket;
    this.socket = null;
    if (socket && !socket.destroyed) socket.destroy();
  }

  private handleMessage(raw: unknown): void {
    let message;
    try {
      message = parseBrokerMessage(raw);
    } catch (error) {
      this.emit("error", error);
      return;
    }
    switch (message.type) {
      case "hello_ok":
      case "hello_rejected":
        if (this.pendingHello) {
          const resolve = this.pendingHello;
          this.pendingHello = null;
          resolve(message);
        }
        return;
      case "delivered": {
        const pending = this.pendingSends.get(message.message_id);
        if (pending) {
          clearTimeout(pending.timer);
          this.pendingSends.delete(message.message_id);
          pending.resolve(this.deliveredOutcome(message));
        }
        return;
      }
      case "delivery_failed": {
        const pending = this.pendingSends.get(message.message_id);
        if (pending) {
          clearTimeout(pending.timer);
          this.pendingSends.delete(message.message_id);
          pending.resolve({
            outcome: message.outcome_known ? "not_submitted" : "unknown",
            message_id: message.message_id, code: message.code, detail: message.detail,
            retryable: message.retryable, outcome_known: message.outcome_known,
          } satisfies SendOutcome);
        }
        return;
      }
      case "cancel_result": {
        const pending = this.pendingCancels.get(message.message_id);
        if (pending) {
          clearTimeout(pending.timer);
          this.pendingCancels.delete(message.message_id);
          pending.resolve(message);
        } else {
          this.emit("cancel_result", message);
        }
        return;
      }
      case "send":
        this.emit("message", message);
        return;
      case "receipt":
        this.emit("receipt", message);
        return;
      case "message_control":
        this.emit("message_control", message);
        return;
      case "error": {
        if (message.request_type === "cancel") {
          this.emit("error", new TransportError(message.code, message.detail, {
            retryable: message.retryable, outcomeKnown: message.outcome_known ?? true,
          }));
          return;
        }
        // A send that failed before any write is `not_submitted`; anything the
        // broker marks with unknown outcome stays `unknown`.
        const pendingList = [...this.pendingSends.values()];
        const pending = pendingList[0];
        if (pending) {
          clearTimeout(pending.timer);
          this.pendingSends.delete(pending.messageId);
          pending.resolve({
            outcome: (message.outcome_known ?? true) ? "not_submitted" : "unknown",
            message_id: pending.messageId, code: message.code, detail: message.detail,
            retryable: message.retryable, outcome_known: message.outcome_known,
          } satisfies SendOutcome);
        } else {
          this.emit("error", new TransportError(message.code, message.detail, {
            retryable: message.retryable, outcomeKnown: message.outcome_known ?? true,
          }));
        }
        return;
      }
      case "health_ok":
      default:
        return;
    }
  }

  private deliveredOutcome(message: DeliveredMessage): SendOutcome {
    return {
      outcome: "delivered", message_id: message.message_id, delivery: message.delivery,
      replayed: message.replayed === true,
    };
  }

  async send(request: SendRequest, timeoutMs?: number): Promise<SendOutcome> {
    const message = this.buildSendMessage(request);
    if (!message || !this.socket) {
      return { outcome: "not_submitted", message_id: message?.message_id ?? randomUUID(),
        code: "E_NOT_REGISTERED", detail: "client is not connected; nothing was written",
        retryable: true, outcome_known: true };
    }
    const promise = new Promise<SendOutcome>((resolve) => {
      const timer = setTimeout(() => {
        this.pendingSends.delete(message.message_id);
        resolve({
          outcome: "unknown", message_id: message.message_id, code: "E_INTERNAL",
          detail: `no broker response within ${timeoutMs ?? this.options.sendTimeoutMs ?? 10_000}ms`,
          retryable: true, outcome_known: false,
        });
      }, timeoutMs ?? this.options.sendTimeoutMs ?? 10_000);
      timer.unref?.();
      this.pendingSends.set(message.message_id, { resolve: resolve as (value: unknown) => void, timer, messageId: message.message_id });
    });
    try {
      writeMessage(this.socket, message);
    } catch (error) {
      const pending = this.pendingSends.get(message.message_id);
      if (pending) {
        clearTimeout(pending.timer);
        this.pendingSends.delete(message.message_id);
        pending.resolve({ outcome: "unknown", message_id: message.message_id, code: "E_INTERNAL",
          detail: `write failed: ${(error as Error).message}`, retryable: true, outcome_known: false });
      }
    }
    return await promise;
  }

  private buildSendMessage(request: SendRequest): SendMessage | null {
    if (!this.endpointEpoch || !this.endpointIdentity) return null;
    if (request.message_id && !/^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/.test(request.message_id)) {
      throw new Error("invalid message_id");
    }
    const identity = this.endpointIdentity;
    return {
      type: "send",
      message_id: request.message_id ?? randomUUID(),
      from: {
        member_id: identity.member_id,
        launch_id: identity.launch_id,
        session_id: identity.session_id,
        endpoint_epoch: this.endpointEpoch,
      },
      to: request.to,
      kind: request.kind,
      reply_to: request.reply_to,
      payload: request.payload,
      payload_sha256: payloadFingerprint(request.payload),
      created_at: Date.now(),
    };
  }

  async cancel(messageId: string, timeoutMs?: number): Promise<CancelResultMessage> {
    if (!this.socket) {
      return { type: "cancel_result", message_id: messageId, ok: false,
        code: "E_NOT_REGISTERED", detail: "client is not connected", outcome_known: true };
    }
    const promise = new Promise<CancelResultMessage>((resolve) => {
      const timer = setTimeout(() => {
        this.pendingCancels.delete(messageId);
        resolve({ type: "cancel_result", message_id: messageId, ok: false,
          code: "E_INTERNAL", detail: "no broker response", outcome_known: false });
      }, timeoutMs ?? this.options.requestTimeoutMs ?? 10_000);
      timer.unref?.();
      this.pendingCancels.set(messageId, { resolve: resolve as (value: unknown) => void, timer, messageId });
    });
    try {
      writeMessage(this.socket, { type: "cancel", message_id: messageId });
    } catch (error) {
      const pending = this.pendingCancels.get(messageId);
      if (pending) {
        clearTimeout(pending.timer);
        this.pendingCancels.delete(messageId);
        pending.resolve({ type: "cancel_result", message_id: messageId, ok: false,
          code: "E_INTERNAL", detail: String(error), outcome_known: false });
      }
    }
    return await promise;
  }

  sendReceipt(receipt: Omit<ReceiptMessage, "type" | "at"> & { at?: number }): void {
    if (!this.socket) return;
    const payload: ReceiptMessage = {
      type: "receipt",
      message_id: receipt.message_id,
      from_endpoint_epoch: receipt.from_endpoint_epoch,
      status: receipt.status,
      at: receipt.at ?? Date.now(),
      ...(receipt.detail ? { detail: receipt.detail } : {}),
    };
    writeMessage(this.socket, payload);
  }

  onError(handler: (error: Error) => void): () => void {
    this.on("error", handler);
    return () => this.off("error", handler);
  }

  onMessage(handler: (message: SendMessage) => void): () => void {
    this.on("message", handler);
    return () => this.off("message", handler);
  }

  onMessageControl(handler: (message: MessageControlMessage) => void): () => void {
    this.on("message_control", handler);
    return () => this.off("message_control", handler);
  }

  onReceipt(handler: (message: ReceiptMessage) => void): () => void {
    this.on("receipt", handler);
    return () => this.off("receipt", handler);
  }

  async disconnect(): Promise<void> {
    this.disconnecting = true;
    this.failConnection("client disconnect");
    this.disconnecting = false;
  }
}
