// Shop built-in transport client surface (new file).
// Exposes the versioned Shop transport contract client to Pi Shop wiring; no Pi SDK import
// here so the transport stays host-agnostic. See transport/NOTICE.md.
export {
  ShopTransportClient,
  type ClientIdentity,
  type ClientOptions,
  type SendOutcome,
  type SendRequest,
} from "./client.ts";
export {
  getBrokerLaunchSpec,
  getBrokerPath,
  isBrokerHealthy,
  probeBrokerSocket,
  spawnBrokerIfNeeded,
  waitForBroker,
  type BrokerLaunchSpec,
  type SpawnOptions,
} from "./spawn.ts";
export {
  MAX_FRAME_BYTES,
  MAX_ENVELOPE_BYTES,
  MAX_PAYLOAD_BYTES,
  SHOP_TRANSPORT_PROTOCOL_NAME,
  SHOP_TRANSPORT_PROTOCOL_VERSION,
  TRANSPORT_FEATURES,
  TransportError,
  canonicalJson,
  parseBrokerMessage,
  parseClientMessage,
  payloadFingerprint,
  sha256Hex,
} from "../shared/protocol.ts";
export {
  getBrokerConnectTarget,
  getBrokerPidPath,
  getBrokerSocketPath,
  getBrokerSpawnLockPath,
  getShopStateRootPath,
  getShopTransportDirPath,
} from "../shared/paths.ts";
export type {
  BrokerMessage,
  ClientMessage,
  DeliveryRecord,
  LiveEndpoint,
  MessageKind,
  ReceiptMessage,
  ReceiptStatus,
  SendMessage,
  SendTargetRef,
  TransportErrorCode,
} from "../shared/types.ts";
