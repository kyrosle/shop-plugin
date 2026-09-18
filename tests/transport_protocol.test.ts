// Strict protocol/bounds/framing unit tests for the Shop built-in transport.
import { expect, test } from "bun:test";
import { Buffer } from "node:buffer";
import { Socket } from "node:net";
import { createMessageReader, writeMessage, MAX_FRAME_BYTES } from "../transport/shared/framing.ts";
import {
  MAX_PAYLOAD_BYTES,
  TransportError,
  canonicalJson,
  parseBrokerMessage,
  parseCancelResult,
  parseClientMessage,
  parseErrorMessage,
  parseHello,
  parseHelloOk,
  parseReceipt,
  parseSend,
  payloadFingerprint,
  sha256Hex,
} from "../transport/shared/protocol.ts";
import { SHOP_TRANSPORT_PROTOCOL_NAME, SHOP_TRANSPORT_PROTOCOL_VERSION } from "../transport/shared/paths.ts";

function validHello(overrides: Record<string, unknown> = {}) {
  return {
    type: "hello",
    protocol: SHOP_TRANSPORT_PROTOCOL_NAME,
    version: SHOP_TRANSPORT_PROTOCOL_VERSION,
    shop_id: "shop-1",
    run_id: "run-1",
    member_id: "alice",
    launch_id: "alice-launch-1",
    session_id: "alice-session",
    terminal_id: "alice-terminal",
    pane_id: "alice-pane",
    ...overrides,
  };
}

function validSend(overrides: Record<string, unknown> = {}) {
  const payload = {
    schema: "shop-transport-v1",
    message_id: "m-1",
    body: { text: "hello" },
  };
  return {
    type: "send",
    message_id: "m-1",
    from: { member_id: "alice", launch_id: "alice-launch-1", session_id: "alice-session", endpoint_epoch: "e-1" },
    to: { member_id: "bob", launch_id: "bob-launch-1", endpoint_epoch: "e-2" },
    kind: "note",
    reply_to: null,
    payload,
    payload_sha256: payloadFingerprint(payload),
    created_at: 1,
    ...overrides,
  };
}

function codeOf(fn: () => unknown): string {
  try {
    fn();
  } catch (error) {
    return error instanceof TransportError ? error.code : `unexpected:${String(error)}`;
  }
  return "no-error";
}

test("hello requires the complete identity triple set and rejects unknown fields", () => {
  expect(parseHello(validHello()).member_id).toBe("alice");
  for (const field of ["shop_id", "run_id", "member_id", "launch_id", "session_id", "terminal_id", "pane_id"]) {
    const broken = validHello();
    delete (broken as Record<string, unknown>)[field];
    expect(codeOf(() => parseHello(broken))).toBe("E_MALFORMED");
  }
  expect(codeOf(() => parseHello(validHello({ extra: 1 })))).toBe("E_MALFORMED");
  expect(codeOf(() => parseHello(validHello({ protocol: "pi-intercom" })))).toBe("E_PROTOCOL_UNSUPPORTED");
  expect(codeOf(() => parseHello(validHello({ version: 2 })))).toBe("E_VERSION_UNSUPPORTED");
  expect(codeOf(() => parseHello(validHello({ member_id: "" })))).toBe("E_MALFORMED");
});

test("send enforces shape, hash, kind and bounds", () => {
  expect(parseSend(validSend()).message_id).toBe("m-1");
  expect(codeOf(() => parseSend(validSend({ extra_field: true })))).toBe("E_MALFORMED");
  expect(codeOf(() => parseSend(validSend({ payload_sha256: "0".repeat(64) })))).toBe("E_MALFORMED");
  expect(codeOf(() => parseSend(validSend({ kind: "chat" })))).toBe("E_MALFORMED");
  expect(codeOf(() => parseSend(validSend({ reply_to: 5 })))).toBe("E_MALFORMED");
  const big = { body: { text: "x".repeat(MAX_PAYLOAD_BYTES + 1024) } };
  expect(codeOf(() => parseSend(validSend({ payload: big, payload_sha256: payloadFingerprint(big) }))))
    .toBe("E_PAYLOAD_TOO_LARGE");
});

test("canonical json and sha256 are stable and order-independent", () => {
  expect(canonicalJson({ b: 1, a: [2, { d: 3, c: 4 }] })).toBe('{"a":[2,{"c":4,"d":3}],"b":1}');
  expect(payloadFingerprint({ a: 1, b: 2 })).toBe(payloadFingerprint({ b: 2, a: 1 }));
  expect(sha256Hex("abc")).toBe("ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad");
  expect(payloadFingerprint({ a: 1 })).toMatch(/^[0-9a-f]{64}$/);
});

test("receipt, cancel and broker-side messages are strict", () => {
  expect(parseReceipt({ type: "receipt", message_id: "m", from_endpoint_epoch: "e", status: "injected", at: 1 }).status)
    .toBe("injected");
  expect(codeOf(() => parseReceipt({ type: "receipt", message_id: "m", from_endpoint_epoch: "e", status: "read", at: 1 })))
    .toBe("E_MALFORMED");
  expect(codeOf(() => parseReceipt({ type: "receipt", message_id: "m", from_endpoint_epoch: "e", status: "injected", at: 1, x: 1 })))
    .toBe("E_MALFORMED");
  expect(parseHelloOk({
    type: "hello_ok", protocol: SHOP_TRANSPORT_PROTOCOL_NAME, version: 1,
    endpoint_epoch: "e", broker_epoch: "b", features: ["exact_target"],
  }).features).toEqual(["exact_target"]);
  expect(codeOf(() => parseHelloOk({
    type: "hello_ok", protocol: "pi-intercom", version: 1, endpoint_epoch: "e", broker_epoch: "b", features: [],
  }))).toBe("E_PROTOCOL_UNSUPPORTED");
  expect(parseCancelResult({ type: "cancel_result", message_id: "m", ok: false, code: "E_CANCEL_TOO_LATE" }).ok).toBe(false);
  expect(parseErrorMessage({ type: "error", code: "E_RATE_LIMITED", detail: "slow down", retryable: true }).retryable).toBe(true);
  expect(codeOf(() => parseBrokerMessage({ type: "nope" }))).toBe("E_MALFORMED");
  expect(codeOf(() => parseClientMessage({ type: "nope" }))).toBe("E_MALFORMED");
});

test("TransportError carries code/retryable/outcome_known into the wire error", () => {
  const failure = new TransportError("E_CANCEL_TOO_LATE", "already injected", { outcomeKnown: false });
  expect(failure.retryable).toBe(false);
  expect(failure.toErrorMessage("cancel")).toEqual({
    type: "error", request_type: "cancel", code: "E_CANCEL_TOO_LATE",
    detail: "already injected", retryable: false, outcome_known: false,
  });
});

test("framing reassembles partial reads, splits and batches, and caps frame size", () => {
  const received: unknown[] = [];
  const errors: string[] = [];
  const reader = createMessageReader((message) => received.push(message), (error) => errors.push(error.message));
  const frame = (value: unknown) => {
    const json = Buffer.from(JSON.stringify(value), "utf8");
    const head = Buffer.allocUnsafe(4);
    head.writeUInt32BE(json.length, 0);
    return Buffer.concat([head, json]);
  };
  const first = frame({ a: 1 });
  const second = frame({ b: 2 });
  reader(first.subarray(0, 3));
  reader(Buffer.concat([first.subarray(3), second.subarray(0, 2)]));
  reader(second.subarray(2));
  expect(received).toEqual([{ a: 1 }, { b: 2 }]);

  const oversized = Buffer.allocUnsafe(4);
  oversized.writeUInt32BE(MAX_FRAME_BYTES + 1, 0);
  reader(oversized);
  expect(errors.length).toBe(1);

  const parseErrors: string[] = [];
  const badReader = createMessageReader(() => { throw new Error("handler boom"); }, (error) => parseErrors.push(error.message));
  badReader(frame({ c: 3 }));
  expect(parseErrors[0]).toContain("handler boom");

  const socket = new Socket();
  expect(() => writeMessage(socket, { ping: true })).not.toThrow();
  socket.destroy();
});

test("Windows pipe/TCP naming and mode logic stay unit-covered", async () => {
  const {
    ensureShopTransportDir, getBrokerConnectTarget, getBrokerSocketPath, getBrokerPortFilePath,
    shouldUseWindowsTcpTransport,
  } = await import("../transport/shared/paths.ts");
  const { mkdtempSync, rmSync, statSync } = await import("node:fs");
  const { tmpdir } = await import("node:os");
  const { join } = await import("node:path");

  const pipe = getBrokerSocketPath("win32", "C:\\state\\transport");
  expect(pipe.startsWith("\\\\.\\pipe\\pi-shop-transport-")).toBe(true);
  expect(shouldUseWindowsTcpTransport("win32", { PI_SHOP_TRANSPORT_TRANSPORT: "tcp" })).toBe(true);
  expect(shouldUseWindowsTcpTransport("darwin", { PI_SHOP_TRANSPORT_TRANSPORT: "tcp" })).toBe(false);
  expect(() => getBrokerConnectTarget("win32", { PI_SHOP_TRANSPORT_TRANSPORT: "tcp" }, "/nonexistent-dir"))
    .toThrow();
  expect(getBrokerPortFilePath("/t")).toBe("/t/broker.port.json");

  const dir = mkdtempSync(join(tmpdir(), "shop-paths-"));
  try {
    const nested = join(dir, "transport");
    ensureShopTransportDir(nested, "darwin");
    if (process.platform !== "win32") expect(statSync(nested).mode & 0o777).toBe(0o700);
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
});
