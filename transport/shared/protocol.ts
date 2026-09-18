// Shop built-in transport protocol: strict envelope parsing and bounds.
// New file; replaces pi-intercom 0.13.0 broker/protocol.ts (sha256
// 4f97b280ff3e4abc5f622db5e039b57ecda2aba4b93425b20a1aea3f4fec2943).
// Shape-validator approach derived from upstream (MIT, Copyright (c) 2026 Nico
// Bailon); the Shop envelope, unknown-field rejection, bounds and error-code
// table follow the versioned Shop transport contract. See transport/NOTICE.md.
import { createHash } from "node:crypto";
import { MAX_FRAME_BYTES } from "./framing.ts";
import { SHOP_TRANSPORT_PROTOCOL_NAME, SHOP_TRANSPORT_PROTOCOL_VERSION } from "./paths.ts";
import type {
  BrokerMessage,
  CancelRequestMessage,
  CancelResultMessage,
  ClientMessage,
  DeliveredMessage,
  DeliveryFailedMessage,
  EndpointRef,
  ErrorMessage,
  HealthCheckMessage,
  HealthOkMessage,
  HelloMessage,
  HelloOkMessage,
  HelloRejectedMessage,
  MessageControlMessage,
  ReceiptMessage,
  ReceiptStatus,
  SendMessage,
  SendTargetRef,
  TransportErrorCode,
} from "./types.ts";

export { MAX_FRAME_BYTES, SHOP_TRANSPORT_PROTOCOL_NAME, SHOP_TRANSPORT_PROTOCOL_VERSION };
export const MAX_PAYLOAD_BYTES = 32 * 1024;
export const MAX_ENVELOPE_BYTES = 64 * 1024;

export const TRANSPORT_FEATURES = ["exact_target", "receipt", "cancel", "unknown_outcome"] as const;

const RECEIPT_STATUSES: ReadonlySet<string> = new Set(["receiver_received", "injected", "rejected"]);
const MESSAGE_KINDS: ReadonlySet<string> = new Set(["note", "task_notice", "handoff"]);

export class TransportError extends Error {
  readonly code: TransportErrorCode;
  readonly retryable: boolean;
  readonly outcomeKnown: boolean;
  readonly detail: string;

  constructor(code: TransportErrorCode, detail: string, options: {
    retryable?: boolean;
    outcomeKnown?: boolean;
  } = {}) {
    super(`${code}: ${detail}`);
    this.name = "TransportError";
    this.code = code;
    this.detail = detail;
    this.retryable = options.retryable ?? false;
    this.outcomeKnown = options.outcomeKnown ?? true;
  }

  toErrorMessage(requestType?: string): ErrorMessage {
    return {
      type: "error",
      ...(requestType ? { request_type: requestType } : {}),
      code: this.code,
      detail: this.detail,
      retryable: this.retryable,
      outcome_known: this.outcomeKnown,
    };
  }
}

export function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/**
 * Strict object check: every present key must be allowed, required keys must be
 * present, and value types are checked by the caller. Unknown fields are
 * rejected instead of dropped (`E_MALFORMED`), which is a deliberate change
 * from the upstream lenient validators.
 */
export function requireShape(
  value: unknown,
  where: string,
  allowed: readonly string[],
  required: readonly string[],
): Record<string, unknown> {
  if (!isRecord(value)) throw new TransportError("E_MALFORMED", `${where} must be an object`);
  for (const key of Object.keys(value)) {
    if (!allowed.includes(key)) throw new TransportError("E_MALFORMED", `${where} has unknown field ${key}`);
  }
  for (const key of required) {
    if (!(key in value)) throw new TransportError("E_MALFORMED", `${where} is missing ${key}`);
  }
  return value;
}

function requireString(value: Record<string, unknown>, where: string, key: string, options: { allowEmpty?: boolean } = {}): string {
  const field = value[key];
  if (typeof field !== "string" || (!options.allowEmpty && field.trim().length === 0)) {
    throw new TransportError("E_MALFORMED", `${where}.${key} must be a nonempty string`);
  }
  return field;
}

function requireNumber(value: Record<string, unknown>, where: string, key: string): number {
  const field = value[key];
  if (typeof field !== "number" || !Number.isFinite(field)) {
    throw new TransportError("E_MALFORMED", `${where}.${key} must be a finite number`);
  }
  return field;
}

function requireBoolean(value: Record<string, unknown>, where: string, key: string): boolean {
  const field = value[key];
  if (typeof field !== "boolean") throw new TransportError("E_MALFORMED", `${where}.${key} must be a boolean`);
  return field;
}

export function canonicalJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  if (isRecord(value)) {
    const keys = Object.keys(value).sort();
    return `{${keys.map((key) => `${JSON.stringify(key)}:${canonicalJson(value[key])}`).join(",")}}`;
  }
  return JSON.stringify(value ?? null);
}

export function sha256Hex(value: string): string {
  return createHash("sha256").update(value, "utf8").digest("hex");
}

export function payloadFingerprint(payload: Record<string, unknown>): string {
  return sha256Hex(canonicalJson(payload));
}

function serializedBytes(value: unknown): number {
  return Buffer.byteLength(JSON.stringify(value), "utf8");
}

export function assertPayloadBounds(payload: Record<string, unknown>, message: SendMessage): void {
  const payloadBytes = serializedBytes(payload);
  if (payloadBytes > MAX_PAYLOAD_BYTES) {
    throw new TransportError("E_PAYLOAD_TOO_LARGE",
      `payload is ${payloadBytes} bytes, limit ${MAX_PAYLOAD_BYTES}`);
  }
  const envelopeBytes = serializedBytes(message);
  if (envelopeBytes > MAX_ENVELOPE_BYTES) {
    throw new TransportError("E_PAYLOAD_TOO_LARGE",
      `envelope is ${envelopeBytes} bytes, limit ${MAX_ENVELOPE_BYTES}`);
  }
  if (envelopeBytes > MAX_FRAME_BYTES) {
    throw new TransportError("E_FRAME_TOO_LARGE", `envelope exceeds frame limit ${MAX_FRAME_BYTES}`);
  }
}

const HELLO_KEYS = ["type", "protocol", "version", "shop_id", "run_id", "member_id",
  "launch_id", "session_id", "terminal_id", "pane_id", "previous_endpoint_epoch"] as const;
const HELLO_REQUIRED = ["type", "protocol", "version", "shop_id", "run_id", "member_id",
  "launch_id", "session_id", "terminal_id", "pane_id"] as const;

export function parseHello(value: unknown): HelloMessage {
  const raw = requireShape(value, "hello", HELLO_KEYS, HELLO_REQUIRED);
  const message: HelloMessage = {
    type: "hello",
    protocol: requireString(raw, "hello", "protocol", { allowEmpty: true }),
    version: requireNumber(raw, "hello", "version"),
    shop_id: requireString(raw, "hello", "shop_id"),
    run_id: requireString(raw, "hello", "run_id"),
    member_id: requireString(raw, "hello", "member_id"),
    launch_id: requireString(raw, "hello", "launch_id"),
    session_id: requireString(raw, "hello", "session_id"),
    terminal_id: requireString(raw, "hello", "terminal_id"),
    pane_id: requireString(raw, "hello", "pane_id"),
  };
  if (raw.previous_endpoint_epoch !== undefined) {
    message.previous_endpoint_epoch = requireString(raw, "hello", "previous_endpoint_epoch");
  }
  if (message.protocol !== SHOP_TRANSPORT_PROTOCOL_NAME) {
    throw new TransportError("E_PROTOCOL_UNSUPPORTED", `protocol ${message.protocol}`);
  }
  if (message.version !== SHOP_TRANSPORT_PROTOCOL_VERSION) {
    throw new TransportError("E_VERSION_UNSUPPORTED", `version ${message.version}`);
  }
  return message;
}

function parseEndpointRef(value: unknown, where: string): EndpointRef {
  const raw = requireShape(value, where, ["member_id", "launch_id", "session_id", "endpoint_epoch"],
    ["member_id", "launch_id", "session_id", "endpoint_epoch"]);
  return {
    member_id: requireString(raw, where, "member_id"),
    launch_id: requireString(raw, where, "launch_id"),
    session_id: requireString(raw, where, "session_id"),
    endpoint_epoch: requireString(raw, where, "endpoint_epoch"),
  };
}

function parseTargetRef(value: unknown, where: string): SendTargetRef {
  const raw = requireShape(value, where, ["member_id", "launch_id", "endpoint_epoch"],
    ["member_id", "launch_id", "endpoint_epoch"]);
  return {
    member_id: requireString(raw, where, "member_id"),
    launch_id: requireString(raw, where, "launch_id"),
    endpoint_epoch: requireString(raw, where, "endpoint_epoch"),
  };
}

const SEND_KEYS = ["type", "message_id", "from", "to", "kind", "reply_to", "payload",
  "payload_sha256", "created_at"] as const;

export function parseSend(value: unknown): SendMessage {
  const raw = requireShape(value, "send", SEND_KEYS, SEND_KEYS);
  const kind = requireString(raw, "send", "kind");
  if (!MESSAGE_KINDS.has(kind)) throw new TransportError("E_MALFORMED", `send.kind ${kind} is unknown`);
  if (!isRecord(raw.payload)) throw new TransportError("E_MALFORMED", "send.payload must be an object");
  if (raw.reply_to !== null && typeof raw.reply_to !== "string") {
    throw new TransportError("E_MALFORMED", "send.reply_to must be a string or null");
  }
  const message: SendMessage = {
    type: "send",
    message_id: requireString(raw, "send", "message_id"),
    from: parseEndpointRef(raw.from, "send.from"),
    to: parseTargetRef(raw.to, "send.to"),
    kind: kind as SendMessage["kind"],
    reply_to: raw.reply_to as string | null,
    payload: raw.payload,
    payload_sha256: requireString(raw, "send", "payload_sha256"),
    created_at: requireNumber(raw, "send", "created_at"),
  };
  const fingerprint = payloadFingerprint(message.payload);
  if (fingerprint !== message.payload_sha256) {
    throw new TransportError("E_MALFORMED",
      `send.payload_sha256 does not match payload (expected ${fingerprint})`);
  }
  assertPayloadBounds(message.payload, message);
  return message;
}

export function parseReceipt(value: unknown): ReceiptMessage {
  const raw = requireShape(value, "receipt",
    ["type", "message_id", "from_endpoint_epoch", "status", "detail", "at"],
    ["type", "message_id", "from_endpoint_epoch", "status", "at"]);
  const status = requireString(raw, "receipt", "status");
  if (!RECEIPT_STATUSES.has(status)) throw new TransportError("E_MALFORMED", `receipt.status ${status} is unknown`);
  const message: ReceiptMessage = {
    type: "receipt",
    message_id: requireString(raw, "receipt", "message_id"),
    from_endpoint_epoch: requireString(raw, "receipt", "from_endpoint_epoch"),
    status: status as ReceiptStatus,
    at: requireNumber(raw, "receipt", "at"),
  };
  if (raw.detail !== undefined) message.detail = requireString(raw, "receipt", "detail", { allowEmpty: true });
  return message;
}

export function parseCancelRequest(value: unknown): CancelRequestMessage {
  const raw = requireShape(value, "cancel", ["type", "message_id", "request_id"],
    ["type", "message_id"]);
  const message: CancelRequestMessage = {
    type: "cancel",
    message_id: requireString(raw, "cancel", "message_id"),
  };
  if (raw.request_id !== undefined) message.request_id = requireString(raw, "cancel", "request_id");
  return message;
}

export function parseHealthCheck(value: unknown): HealthCheckMessage {
  const raw = requireShape(value, "health_check", ["type", "request_id"], ["type"]);
  const message: HealthCheckMessage = { type: "health_check" };
  if (raw.request_id !== undefined) message.request_id = requireString(raw, "health_check", "request_id");
  return message;
}

export function parseClientMessage(value: unknown): ClientMessage {
  if (!isRecord(value)) throw new TransportError("E_MALFORMED", "message must be an object");
  switch (value.type) {
    case "hello": return parseHello(value);
    case "send": return parseSend(value);
    case "receipt": return parseReceipt(value);
    case "cancel": return parseCancelRequest(value);
    case "health_check": return parseHealthCheck(value);
    default:
      throw new TransportError("E_MALFORMED", `unknown client message type ${String(value.type)}`);
  }
}

export function parseHelloOk(value: unknown): HelloOkMessage {
  const raw = requireShape(value, "hello_ok",
    ["type", "protocol", "version", "endpoint_epoch", "broker_epoch", "features"],
    ["type", "protocol", "version", "endpoint_epoch", "broker_epoch", "features"]);
  if (raw.protocol !== SHOP_TRANSPORT_PROTOCOL_NAME) {
    throw new TransportError("E_PROTOCOL_UNSUPPORTED", `hello_ok protocol ${String(raw.protocol)}`);
  }
  if (!Array.isArray(raw.features) || raw.features.some((item) => typeof item !== "string")) {
    throw new TransportError("E_MALFORMED", "hello_ok.features must be a string array");
  }
  return {
    type: "hello_ok",
    protocol: SHOP_TRANSPORT_PROTOCOL_NAME,
    version: requireNumber(raw, "hello_ok", "version"),
    endpoint_epoch: requireString(raw, "hello_ok", "endpoint_epoch"),
    broker_epoch: requireString(raw, "hello_ok", "broker_epoch"),
    features: raw.features as string[],
  };
}

export function parseDelivered(value: unknown): DeliveredMessage {
  const raw = requireShape(value, "delivered",
    ["type", "message_id", "to_endpoint_epoch", "delivery", "at", "replayed"],
    ["type", "message_id", "to_endpoint_epoch", "delivery", "at"]);
  const delivery = requireString(raw, "delivered", "delivery");
  if (delivery !== "socket_delivered" && delivery !== "queued") {
    throw new TransportError("E_MALFORMED", `delivered.delivery ${delivery} is unknown`);
  }
  const message: DeliveredMessage = {
    type: "delivered",
    message_id: requireString(raw, "delivered", "message_id"),
    to_endpoint_epoch: requireString(raw, "delivered", "to_endpoint_epoch"),
    delivery,
    at: requireNumber(raw, "delivered", "at"),
  };
  if (raw.replayed !== undefined) message.replayed = requireBoolean(raw, "delivered", "replayed");
  return message;
}

export function parseDeliveryFailed(value: unknown): DeliveryFailedMessage {
  const raw = requireShape(value, "delivery_failed",
    ["type", "message_id", "code", "retryable", "outcome_known", "detail"],
    ["type", "message_id", "code", "retryable", "outcome_known"]);
  const message: DeliveryFailedMessage = {
    type: "delivery_failed",
    message_id: requireString(raw, "delivery_failed", "message_id"),
    code: requireString(raw, "delivery_failed", "code") as DeliveryFailedMessage["code"],
    retryable: requireBoolean(raw, "delivery_failed", "retryable"),
    outcome_known: requireBoolean(raw, "delivery_failed", "outcome_known"),
  };
  if (raw.detail !== undefined) message.detail = requireString(raw, "delivery_failed", "detail", { allowEmpty: true });
  return message;
}

export function parseMessageControl(value: unknown): MessageControlMessage {
  const raw = requireShape(value, "message_control", ["type", "message_id", "action", "at"],
    ["type", "message_id", "action", "at"]);
  if (raw.action !== "cancelled") throw new TransportError("E_MALFORMED", `message_control.action ${String(raw.action)}`);
  return {
    type: "message_control",
    message_id: requireString(raw, "message_control", "message_id"),
    action: "cancelled",
    at: requireNumber(raw, "message_control", "at"),
  };
}

export function parseCancelResult(value: unknown): CancelResultMessage {
  const raw = requireShape(value, "cancel_result", ["type", "message_id", "ok", "code", "detail", "outcome_known"],
    ["type", "message_id", "ok"]);
  const message: CancelResultMessage = {
    type: "cancel_result",
    message_id: requireString(raw, "cancel_result", "message_id"),
    ok: requireBoolean(raw, "cancel_result", "ok"),
  };
  if (raw.code !== undefined) message.code = requireString(raw, "cancel_result", "code") as CancelResultMessage["code"];
  if (raw.detail !== undefined) message.detail = requireString(raw, "cancel_result", "detail", { allowEmpty: true });
  if (raw.outcome_known !== undefined) message.outcome_known = requireBoolean(raw, "cancel_result", "outcome_known");
  return message;
}

export function parseHelloRejected(value: unknown): HelloRejectedMessage {
  const raw = requireShape(value, "hello_rejected", ["type", "code", "detail"], ["type", "code", "detail"]);
  return {
    type: "hello_rejected",
    code: requireString(raw, "hello_rejected", "code") as HelloRejectedMessage["code"],
    detail: requireString(raw, "hello_rejected", "detail", { allowEmpty: true }),
  };
}

export function parseErrorMessage(value: unknown): ErrorMessage {
  const raw = requireShape(value, "error",
    ["type", "request_type", "code", "detail", "retryable", "outcome_known"],
    ["type", "code", "detail", "retryable"]);
  const message: ErrorMessage = {
    type: "error",
    code: requireString(raw, "error", "code") as ErrorMessage["code"],
    detail: requireString(raw, "error", "detail", { allowEmpty: true }),
    retryable: requireBoolean(raw, "error", "retryable"),
  };
  if (raw.request_type !== undefined) message.request_type = requireString(raw, "error", "request_type");
  if (raw.outcome_known !== undefined) message.outcome_known = requireBoolean(raw, "error", "outcome_known");
  return message;
}

export function parseHealthOk(value: unknown): HealthOkMessage {
  const raw = requireShape(value, "health_ok", ["type", "protocol", "version", "broker_epoch", "pid", "request_id"],
    ["type", "protocol", "version", "broker_epoch", "pid"]);
  return {
    type: "health_ok",
    protocol: requireString(raw, "health_ok", "protocol", { allowEmpty: true }),
    version: requireNumber(raw, "health_ok", "version"),
    broker_epoch: requireString(raw, "health_ok", "broker_epoch"),
    pid: requireNumber(raw, "health_ok", "pid"),
  };
}

export function parseBrokerMessage(value: unknown): BrokerMessage {
  if (!isRecord(value)) throw new TransportError("E_MALFORMED", "message must be an object");
  switch (value.type) {
    case "hello_ok": return parseHelloOk(value);
    case "hello_rejected": return parseHelloRejected(value);
    case "send": return parseSend(value);
    case "delivered": return parseDelivered(value);
    case "delivery_failed": return parseDeliveryFailed(value);
    case "receipt": return parseReceipt(value);
    case "message_control": return parseMessageControl(value);
    case "cancel_result": return parseCancelResult(value);
    case "error": return parseErrorMessage(value);
    case "health_ok": return parseHealthOk(value);
    default:
      throw new TransportError("E_MALFORMED", `unknown broker message type ${String(value.type)}`);
  }
}
