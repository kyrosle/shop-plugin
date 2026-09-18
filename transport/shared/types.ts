// Shop built-in transport wire types (new file; replaces pi-intercom 0.13.0
// broker/protocol.ts sha256 4f97b280ff3e4abc5f622db5e039b57ecda2aba4b93425b20a1aea3f4fec2943).
// Written for this package against the versioned Shop transport contract: exact endpoint
// triple, endpoint/broker epochs, bounded payload, explicit reply_to/cancel.
// Shape-checking approach derived from upstream (MIT, Copyright (c) 2026 Nico
// Bailon); no upstream runtime shape was reused verbatim. See transport/NOTICE.md.

export type MessageKind = "note" | "task_notice" | "handoff";
export type ReceiptStatus = "receiver_received" | "injected" | "rejected";
export type DeliveryKind = "socket_delivered" | "queued";

export type TransportErrorCode =
  | "E_PROTOCOL_UNSUPPORTED"
  | "E_VERSION_UNSUPPORTED"
  | "E_IDENTITY_INCOMPLETE"
  | "E_SHOP_MISMATCH"
  | "E_RUN_MISMATCH"
  | "E_TARGET_NOT_FOUND"
  | "E_TARGET_STALE_EPOCH"
  | "E_TARGET_DUPLICATE_REGISTRATION"
  | "E_SELF_TARGET"
  | "E_MESSAGE_ID_REUSE"
  | "E_UNKNOWN_MESSAGE"
  | "E_CANCEL_TOO_LATE"
  | "E_RATE_LIMITED"
  | "E_PAYLOAD_TOO_LARGE"
  | "E_FRAME_TOO_LARGE"
  | "E_MALFORMED"
  | "E_NOT_REGISTERED"
  | "E_FOREIGN_SOCKET"
  | "E_INTERNAL";

export interface EndpointRef {
  member_id: string;
  launch_id: string;
  session_id: string;
  endpoint_epoch: string;
}

export interface SendTargetRef {
  member_id: string;
  launch_id: string;
  endpoint_epoch: string;
}

export interface HelloMessage {
  type: "hello";
  protocol: string;
  version: number;
  shop_id: string;
  run_id: string;
  member_id: string;
  launch_id: string;
  session_id: string;
  terminal_id: string;
  pane_id: string;
  previous_endpoint_epoch?: string;
}

export interface HelloOkMessage {
  type: "hello_ok";
  protocol: string;
  version: number;
  endpoint_epoch: string;
  broker_epoch: string;
  features: string[];
}

export interface HelloRejectedMessage {
  type: "hello_rejected";
  code: TransportErrorCode;
  detail: string;
}

export interface SendMessage {
  type: "send";
  message_id: string;
  from: EndpointRef;
  to: SendTargetRef;
  kind: MessageKind;
  reply_to: string | null;
  payload: Record<string, unknown>;
  payload_sha256: string;
  created_at: number;
}

export interface DeliveredMessage {
  type: "delivered";
  message_id: string;
  to_endpoint_epoch: string;
  delivery: DeliveryKind;
  at: number;
  replayed?: boolean;
}

export interface DeliveryFailedMessage {
  type: "delivery_failed";
  message_id: string;
  code: TransportErrorCode;
  retryable: boolean;
  outcome_known: boolean;
  detail?: string;
}

export interface ReceiptMessage {
  type: "receipt";
  message_id: string;
  from_endpoint_epoch: string;
  status: ReceiptStatus;
  detail?: string;
  at: number;
}

export interface MessageControlMessage {
  type: "message_control";
  message_id: string;
  action: "cancelled";
  at: number;
}

export interface CancelRequestMessage {
  type: "cancel";
  message_id: string;
  request_id?: string;
}

export interface CancelResultMessage {
  type: "cancel_result";
  message_id: string;
  ok: boolean;
  code?: TransportErrorCode;
  detail?: string;
  outcome_known?: boolean;
}

export interface ErrorMessage {
  type: "error";
  request_type?: string;
  code: TransportErrorCode;
  detail: string;
  retryable: boolean;
  outcome_known?: boolean;
}

export interface HealthCheckMessage {
  type: "health_check";
  request_id?: string;
}

export interface HealthOkMessage {
  type: "health_ok";
  protocol: string;
  version: number;
  broker_epoch: string;
  pid: number;
  request_id?: string;
}

export type ClientMessage = HelloMessage | SendMessage | ReceiptMessage
  | CancelRequestMessage | HealthCheckMessage;
export type BrokerMessage = HelloOkMessage | HelloRejectedMessage | SendMessage
  | DeliveredMessage | DeliveryFailedMessage | ReceiptMessage | MessageControlMessage
  | CancelResultMessage | ErrorMessage | HealthOkMessage;

export interface LiveEndpoint {
  member_id: string;
  launch_id: string;
  session_id: string;
  terminal_id: string;
  pane_id: string;
  shop_id: string;
  run_id: string;
  endpoint_epoch: string;
  connected_at: number;
}

export interface DeliveryRecord {
  message_id: string;
  payload_sha256: string;
  from_member_id: string;
  from_endpoint_epoch: string;
  to_member_id: string;
  to_launch_id: string;
  to_endpoint_epoch: string;
  at: number;
  receipt_status?: ReceiptStatus;
  injected: boolean;
}
