// Ported from pi-intercom 0.13.0 (MIT), npm tarball sha256
// 1d89bd31ca63cccd82a5c950cc043bca99e17e4664df7fd99f16859a0442279f
// Upstream ref: npm gitHead 199279ae861bf53ce014809fb2a03337538ae13e
// Original: broker/framing.ts (sha256 fde128944099fa0ce9a808ca571df0736f22447eea149ab08eef8711882bf0d5)
// Copyright (c) 2026 Nico Bailon. Modified for Shop built-in transport:
// error text re-scoped from intercom to Shop transport; algorithm unchanged.
// See transport/NOTICE.md.
import type { Socket } from "node:net";

export const MAX_FRAME_BYTES = 1024 * 1024;

/**
 * Write a length-prefixed message to a socket.
 * Format: 4-byte big-endian length + JSON payload.
 */
export function writeMessage(socket: Socket, msg: unknown): void {
  const json = JSON.stringify(msg);
  const payloadLength = Buffer.byteLength(json, "utf-8");
  const frame = Buffer.allocUnsafe(4 + payloadLength);
  frame.writeUInt32BE(payloadLength, 0);
  frame.write(json, 4, payloadLength, "utf-8");
  socket.write(frame);
}

/**
 * Create a message reader that handles partial reads.
 * Calls onMessage for each complete message; framing/parse/handler errors go to
 * onError so the caller can close the socket (fail closed, never throw upward).
 */
export function createMessageReader(
  onMessage: (msg: unknown) => void,
  onError: (error: Error) => void,
  maxFrameBytes = MAX_FRAME_BYTES,
) {
  const header = Buffer.allocUnsafe(4);
  let headerBytes = 0;
  let payload: Buffer | null = null;
  let payloadBytes = 0;
  let payloadLength = 0;

  function reportMessage(framePayload: Buffer): boolean {
    let msg: unknown;
    try {
      msg = JSON.parse(framePayload.toString("utf-8"));
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      onError(new Error(`Failed to parse shop transport message: ${message}`, { cause: error }));
      return false;
    }
    try {
      onMessage(msg);
      return true;
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      onError(new Error(`Failed to handle shop transport message: ${message}`, { cause: error }));
      return false;
    }
  }

  return (data: Buffer) => {
    let offset = 0;
    while (offset < data.length) {
      if (headerBytes < 4) {
        const bytes = Math.min(4 - headerBytes, data.length - offset);
        data.copy(header, headerBytes, offset, offset + bytes);
        headerBytes += bytes;
        offset += bytes;
        if (headerBytes < 4) return;
        payloadLength = header.readUInt32BE(0);
        if (payloadLength > maxFrameBytes) {
          headerBytes = 0;
          onError(new Error(`Shop transport frame length ${payloadLength} exceeds maximum ${maxFrameBytes} bytes`));
          return;
        }
      }

      // Fast path: whole payload already present, parse in place.
      if (payloadBytes === 0 && data.length - offset >= payloadLength) {
        const framePayload = data.subarray(offset, offset + payloadLength);
        offset += payloadLength;
        headerBytes = 0;
        payload = null;
        payloadLength = 0;
        if (!reportMessage(framePayload)) return;
        continue;
      }

      if (payload === null || payload.length !== payloadLength) {
        payload = Buffer.allocUnsafe(payloadLength);
      }
      const bytes = Math.min(payloadLength - payloadBytes, data.length - offset);
      data.copy(payload, payloadBytes, offset, offset + bytes);
      payloadBytes += bytes;
      offset += bytes;

      if (payloadBytes < payloadLength) return;

      const framePayload = payload;
      headerBytes = 0;
      payload = null;
      payloadBytes = 0;
      payloadLength = 0;
      if (!reportMessage(framePayload)) return;
    }
  };
}
