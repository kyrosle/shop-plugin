# NOTICE — Shop built-in transport port

The files under `transport/` are derived from **pi-intercom 0.13.0** (MIT),
Copyright (c) 2026 Nico Bailon. The upstream license is shipped unchanged as
`LICENSE.pi-intercom` (sha256
`2d20dfacd9742706e564470dc77438608a1e54b0ed46959f080709389209093c`).

## Frozen upstream identity

| Item | Value |
| --- | --- |
| Package / version | `pi-intercom@0.13.0` |
| Tarball | `https://registry.npmjs.org/pi-intercom/-/pi-intercom-0.13.0.tgz` |
| Tarball sha256 | `1d89bd31ca63cccd82a5c950cc043bca99e17e4664df7fd99f16859a0442279f` |
| Tarball sha512 | `sha512-+QjKJRAEhrgQZj4+M9OW/8unRLvCzeCp0K66lZmbS5/me0fsClXRtANBgM6mY+EoX+Fcd+qBE8dTThp8+ND//g==` |
| Registry sha1 | `1108b84a365cb66ce85cb8e469bab603b4a228a3` |
| Upstream ref (npm gitHead) | `199279ae861bf53ce014809fb2a03337538ae13e` |
| License file sha256 | `2d20dfacd9742706e564470dc77438608a1e54b0ed46959f080709389209093c` |

## Origin and modification per shipped file

| Shop file | Upstream origin (sha256) | Modification |
| --- | --- | --- |
| `shared/framing.ts` | `broker/framing.ts` (`fde128944099fa0ce9a808ca571df0736f22447eea149ab08eef8711882bf0d5`) | algorithm kept; error text re-scoped; 1 MiB frame cap kept |
| `shared/paths.ts` | `broker/paths.ts` (`a6d5387de9fae0d206987e2ba5041d544ba1d6c02495b6ac841215f4a618e3d2`) | every path/env re-scoped to `PI_SHOP_TRANSPORT_DIR`; protocol `pi-shop-transport` v1; `PI_CODING_AGENT_DIR`/intercom dir removed |
| `shared/runtime-claim.ts` | `broker/runtime-claim.ts` (`e2290bba58d88f4b0ba5310c76fc1575d62cea70139358f741ac59777aac9e51`) | error text re-scoped; logic unchanged |
| `shared/protocol.ts` | replaces `broker/protocol.ts` (`4f97b280ff3e4abc5f622db5e039b57ecda2aba4b93425b20a1aea3f4fec2943`) | new strict envelope/unknown-field/bounds/code-table implementation written for this package; upstream shape-validator style only |
| `shared/types.ts` | replaces `types.ts` (`7796cf45d8608956601c7ec14322a94fa9448a390c7239d84e24d43de1ba6d5f`) | trimmed to transport types; endpoint/broker epochs added; attachments/extension capabilities removed |
| `broker/broker.ts` | `broker/broker.ts` (`65d9ec1dbed5368ea952a1c7c3f978c83b44cd092e1922509085aa070f06829b`) | rewritten to the versioned Shop transport contract: exact-triple targeting, identity handshake, receipts, cancel, rate limit, idle shutdown; mailbox/disconnected redelivery, ask edges, namespace bus, `sameCwd` fallback, supersede and extension-state manager removed, not ported |
| `client/client.ts` | `broker/client.ts` (`c810c80fdc0147dc9d7b636f071b82db7a50383dee6b860a6fe6cc0946f6212d`) | connect/hello/liveness/correlation patterns kept; name/prefix/cwd resolution, extension bus, cancelAsk, presence and mailbox methods removed; `send` requires the exact triple and reports `unknown` on post-write loss |
| `client/spawn.ts` | `broker/spawn.ts` (`63b22e961bf23c5ead19084be6defe4a8c63ee4e614eebf2cb79897b16bfd673`) | liveness probe/spawn lock/health handshake kept; `tsx`/`npx`/VBS launch removed and replaced by `process.execPath --experimental-strip-types` on the packaged broker entry |
| `client/index.ts` | new | Shop client surface for the Pi wiring; no upstream counterpart |

## Upstream behaviour deliberately not ported

`index.ts` (Pi extension/tool/UI layer, inbound turn triggering, outbox), the
extension-bus/namespace plane (`extension-api.ts`, `broker/extension-state.ts`),
`project-agent.ts` (pane creation), `cwd.ts`, `reply-tracker.ts` (ask inference
and `pending-asks/`), `format-context.ts`, `ui/*`, the packaged skill and the
`supersedes`/`retryOf` control messages. Mailbox redelivery and disconnected
session reconnection are absent by design: this version only reconnects the
connection and never replays a business message.

Shop additions written for this package (no upstream origin): `core/transport.py`
(durable dedupe, identity verification, crash-window states),
`core/handoff.py` (handoff state machine), `core/transport_cli.py`,
`bin/shop-transport`, `extensions/transport.ts` (Pi wiring and validated
`pi.sendMessage` injection).
