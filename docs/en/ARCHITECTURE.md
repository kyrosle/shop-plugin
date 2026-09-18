# Architecture

[简体中文](../zh-CN/ARCHITECTURE.md) · [README](../../README.md)

Shop has two plugin entrypoints and one authoritative Python core. Herdr manages visible
Pi processes; Pi supplies role guidance, tools and interactive controls. Neither entrypoint
is a permanent task scheduler.

## Components

| Layer | Responsibility |
| --- | --- |
| `herdr-plugin.toml`, `core/plugin.py` | Herdr actions, event hooks, notifications and user-action shutdown host |
| `extensions/index.ts`, `state.ts` | Explicit `/shop` delegation, scoped tools and compact status rendering |
| `extensions/settings-ui.ts`, `configuration.ts` | Configuration UI, Pi custom entries and short-lived startup candidates |
| `extensions/workbench-ui.ts` | Read-only dashboard and explicitly confirmed business actions |
| `extensions/i18n.ts`, `language-ui.ts`, `core/language.py`, `locales/` | Shared English/Chinese catalogues, personal language preference and stable selection IDs |
| `extensions/transport.ts`, `endpoints.ts` | Pi transport lifecycle, ephemeral endpoint advertisements and validated message injection |
| `core/shop.py`, `run.py`, `coordination.py` | Seat setup, run binding, explicit dispatch and ticket coordination |
| `core/herdr.py`, `identity.py` | Typed Herdr adapter and fail-closed member authorization |
| `core/contracts.py` | Canonical tickets, attempt fences, checkpoints, results and acceptance |
| `core/configuration.py`, `settings.py` | Configuration parsing, precedence, sparse overrides and guarded writes |
| `core/workbench.py`, `development.py` | Handoffs, model requests, interventions and previewed Git operations |
| `core/snapshot.py`, `events.py`, `supervision.py` | Shared status document, bounded event facts and finite patrol |
| `core/transport.py`, `handoff.py`, `transport_cli.py` | Durable message deduplication, submission/receipt records and business handoff state |
| `core/repair.py`, `shutdown.py` | Explicit recovery and journaled, identity-checked shutdown |
| `transport/` | Host-independent broker, client and strict transport protocol |

Every Python Herdr invocation goes through `core/herdr.py`. It probes compatible binary
and protocol versions, validates pane/agent schemas and applies bounded timeouts. A pane
record does not establish an agent name; authorization checks the agent endpoint.

## State and authority

- A bridge outside the installation identifies the shared `core_root`, `state_dir` and `config_dir`.
- Machine-local state records Shop membership, launch identity, sessions and operation journals.
- Project `.shop/` holds authoritative run bindings, tickets and evidence.
- A Shop has at most one bound run; a run has one workstation owner. A changed `current.json`
  pointer never retargets an existing assignment.
- Primary Lead owns dispatch and ticket coordination. Only the assigned occupant may publish
  its checkpoint/result, and only the current attempt is accepted.
- Runtime state, credentials, conversation histories and user configuration are not package content.

Locks and identity checks coordinate cooperating Shop callers. Plugins run as the operating-system
user, not in a security sandbox; they cannot prevent arbitrary shell writes or atomically freeze
manual input in another pane.

## Identity and observations

Persisted phase (`creating`, `ready`, `partial`, etc.) describes an operation, not live health.
A ready registration can contain missing, moved or mismatched members. Missing panes do not
prove their background processes stopped.

Launch, terminal and Pi session identities are distinct. Runtime transport epochs belong to
short-lived endpoint advertisements, not the persistent member record. Advertisements support
exact-target discovery; the broker validates epochs when sending. Unknown or stale identity is
not permission to retarget, restart or replay.

All status surfaces use `shop.snapshot/v1` from `core/snapshot.py`:

- `desired`: registered member identity and location;
- `observed`: live facts with optional event annotations;
- tickets: status, owner, attempt, delivery, dependencies and bounded checkpoints;
- transport and handoff: separate technical and business evidence;
- attention: inspection suggestions, not automatic stuck/dead/done decisions.

Live reads take precedence over events. Events are non-authoritative and never dispatch,
accept, pause, close, focus or retry work. The hooks accept the pinned shapes for
`pane.agent_status_changed`, `pane.closed` and `pane.exited`; malformed or foreign events
are ignored. Facts are capped at 2,000 lines / 512 KiB per Shop.

Snapshot output is bounded and allowlisted. Prompts, transcripts, environment contents and
message bodies are excluded. Free text is sanitized, but local status is not an anonymous
export: it can contain relevant local paths and identifiers. Use the workbench's dedicated
allowlisted diagnostic export before sharing diagnostics.

## Message and handoff path

```text
Explicit Pi tool / workbench action
  → Python authorization and durable preparation
  → calling Pi transport client
  → Shop-private broker
  → recipient identity validation and durable deduplication
  → Pi public message API
  → explicit recipient business receipt
```

`herdr-shop message/dispatch` prepares records only; actual sending uses the Pi tools.
There is no external intercom routing or Herdr prompt fallback for these messages.
The broker carries bounded envelopes; it does not assign tasks or decide acceptance.

The receiver records receipt before injection. A crash or session change in that window
leaves an unresolved outcome and never causes automatic reinjection. Wire identity and
business identity must agree. Busy delivery uses Pi steering rather than aborting a turn.

Transport delivery/injection is not handoff acceptance. Handoff delivery is not ticket
acceptance. Result files and primary Lead review remain the task-completion authority.
See [workflow](WORKFLOW.md) and [workbench](WORKBENCH.md).

## Configuration and runtime changes

Configuration precedence is session > trusted project > global > built-in, with
seat > role > defaults within each scope. Python owns validation, sparse overrides,
content-hash compare-and-swap and atomic file writes. Pi owns model capability checks
and current-branch custom entries. See [configuration](MODELS.md).

New shops pin `model_profiles`; `launch_profile` records requested startup arguments,
not live observations. Saving configuration does not change existing sessions.
A separate idle-seat request requires confirmation in the receiving Pi. Applying or
unknown profile changes block dispatch until completion or explicit inspection.
Architect remains on native `/model` and `/thinking` controls.

Display language uses a separate `language.json`, never these configuration layers or
Shop snapshots. Translations affect labels only; protocol, raw evidence and user content
stay unchanged. See [language](LANGUAGE.md).

## Git operations and intervention

Development preparation creates a new isolated branch/worktree only after preview and
confirmation. Integration supports explicit fast-forward only. Plans bind to repository
facts and expire; mutation rechecks those facts. Failure preserves partial state without
automatic stash, reset, cleanup, rollback or push.

Scope revision, pause request, Esc delivery, verified stoppage and ticket cancellation are
separate events. Existing attempts and evidence are preserved. A user assertion that
background writers stopped is recorded as an assertion, not fabricated process evidence.

## Shutdown and recovery

Shutdown execution belongs to the independent Herdr user-action host. Agent tools may
inspect a preview but do not gain a force-close capability. A plan includes identity,
binding, process, transport, handoff and layout facts, an expiry and a state digest.
Execution revalidates the plan and refuses drift.

A bound run, active or unknown member, identity mismatch, missing process facts or
unresolved transport/handoff blocks shutdown. Herdr protocol 22 exposes foreground
processes but not a complete background-job list: `background_state_unknown` remains
a blocker, not a claim of safety.

Execution journals archive, per-target close, verified absence, final checks and receipt
before removing registration. Architect is retained. Failure preserves `shutdown_partial`
and the remaining roster. Recovery plans are read-only; restore is a separate explicit action.
Neither operation silently unbinds runs, accepts tickets or deletes code, worktrees or sessions.
See [migration and rollback](MIGRATION.md).

## Validation limits

Offline tests use temporary Git repositories, a local broker and Pi/Herdr substitutes.
They validate mechanisms and contracts, not real model understanding or output quality.
Actual providers, Pi UI behavior, Herdr lifecycle and background-writer safety require
controlled validation in an isolated environment before use with active projects.

Third-party origins and licenses are documented in [third-party notices](../../THIRD_PARTY.md) and
[transport attribution](../../transport/NOTICE.md); source attribution must remain intact.
