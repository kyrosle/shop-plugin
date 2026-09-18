# Pi + Herdr Shop

English · [简体中文](README.zh-CN.md)

**Local alpha. One repository, two plugin entrypoints, one authoritative Python core.**
Visible independent Pi processes; file-ticket handoff; primary Lead owns dispatch and review.
No Pi subprocess orchestration framework, conversation cloning, daemon scheduler, or generic tool interception.

## Status

Implemented:
- Herdr manifest actions: open, close, status, recovery report, doctor.
- Pi package: explicit per-request `/shop <task>` delegation (ordinary messages stay single-agent), `shop_status`, `shop_patrol`, `shop_dispatch`, `shop_message`, `shop_handoff`; `/shop-status` for manual inspection.
- `/shop-ui`: read-only dashboard, exact-recipient handoff receipts, new-worktree preparation, delivery/ff-only integration plans, idle-seat model requests, scope/pause/cancel controls, and allowlisted diagnostic export. See [workbench guide](docs/en/WORKBENCH.md).
- `/shop-config`: global/trusted-directory/session model and thinking settings; explicit legacy migration, CAS saves, and Pi-to-Herdr configuration candidates. Existing shops retain pinned launch profiles. Controlled live acceptance remains pending.
- Existing ticket/binding/checkpoint/review/retry/cleanup core and regression tests.
- Observed member health across workspaces: present, missing, moved, mismatched, unknown.
- Same-shop envelope validation including sender/recipient launch IDs; schema-1 dispatch records envelope.
- Fresh sessions; explicit restoration when only original Architect remains.

Not yet release-ready:
- Partial/mixed survivor layouts require the read-only recovery plan and an explicit apply; nothing is rebuilt automatically.
- Herdr pane event hooks (`[[events]]` → `core/events.py`) record bounded, non-authoritative facts; `/shop-ui` reads the shared snapshot on explicit refresh.
- `shop_message` / `shop_dispatch` use built-in transport and durable delivery journals; no Herdr prompt fallback. CLI message/dispatch only prepare records. Busy notes require explicit `allow_busy`; transport delivery is not business acceptance.
- Seat launch identity and Pi session identity are separate. Ephemeral endpoint advertisements expire; handoff/profile actions refuse replaced launches/sessions. Session-switch races and live endpoint lifecycle still need controlled acceptance.
- Automatic dynamic role loading remains Architect-only; execution roles use launch-time prompt files.
- No automatic legacy migration. No live end-to-end acceptance of packaged setup/recovery yet.
- Linux declared for POSIX implementation; only macOS tested. Windows unsupported (`fcntl`, shell wrappers).

## Requirements

- Herdr >= 0.9.0, matching CLI/server protocol; Pi API dependencies target 0.85.1-compatible versions.
- Python >= 3.9, Node >= 22.19.0 for Pi, Git. Bun needed for development tests only.
- Configured Pi providers/models. Plugin ships no credentials or hard-coded user model selection.
- Keep `@ogulcancelik/pi-herdr` available for general agent read/wait/control tools.

## Local development installation

Commands below are **explicit setup steps**, not automatic install scripts. Do not switch an active legacy workstation.

```sh
cd /absolute/path/to/shop-plugin
npm ci --ignore-scripts
npm test
npm run typecheck

# Preview first. Refuses to overwrite an existing bridge on apply.
python3 core/plugin.py configure
python3 core/plugin.py configure --apply
```

Default bridge: `~/.config/shop-workstation/bridge.json`.
It identifies authoritative core checkout, config directory and state directory for both plugins.
Set `SHOP_LOCATOR` consistently in both hosts to use another bridge. `SHOP_STATE_DIR` and
`SHOP_CONFIG_DIR` can select paths at configuration time. Do not put mutable state inside package checkout.
When invoked within a Herdr plugin context, configure can use its plugin config/state directories.

After loading the extension, use `/shop-config` in Herdr Pi to configure global, trusted-directory or
session model/thinking overrides. Each execution seat can use a different profile. Existing
`models.json` can be explicitly imported with `/shop-config migrate`; there is no automatic overwrite.
See the [configuration guide](docs/en/MODELS.md) for precedence, inheritance and migration.
Architect stays in the existing Pi session: select its model with `/model` and thinking with `/thinking`.
Shop never resets that session or changes its model automatically.

```sh
# Use compatible Herdr executable, not an old PATH copy.
herdr plugin link /absolute/path/to/shop-plugin
pi install /absolute/path/to/shop-plugin
```

Disable old manually installed `herdr-shop-mode` extension before loading new Pi extension; `/reload` after installation.
Do not enable both role-injection extensions. Configure keyboard entries manually (back up config first):

```toml
[[keys.command]]
key = "prefix+u"
type = "plugin_action"
command = "shop.workstation.open"
description = "Open Shop"

[[keys.command]]
key = "prefix+shift+u"
type = "plugin_action"
command = "shop.workstation.close"
description = "Close idle unbound Shop"
```

Remove/replace conflicting old entries rather than appending duplicates. Remote keybinding forwarding depends on
Herdr client configuration; install/configure on server running panes. Native plugin behavior must be verified remotely.

Use explicit `bin/herdr-shop` and `bin/shop-run` paths from this checkout; no global wrappers are overwritten.
If needed, set `SHOP_HERDR_BIN` to compatible executable. Herdr actions normally use injected `HERDR_BIN_PATH`.

## Language

Use `/shop-language` to select English, 简体中文 or automatic system detection.
Explicit commands: `/shop-language en`, `/shop-language zh-CN`, `/shop-language auto`.
CLI: `bin/herdr-shop language [auto|zh-CN|en]`. Personal language preferences are separate
from model configuration and never modify running Shops. See [language guide](docs/en/LANGUAGE.md).

## Safety and recovery

Plugins execute as your OS user, not in a sandbox. Review code before installation.
File locks/identity checks govern Shop-specific operations, not arbitrary shell commands.

- Ctrl+B, U starts from one unzoomed Pi pane, >=140 columns x 40 rows. New setup requires a fresh Pi configuration candidate; use `/reload` or `/shop-config` in the original Architect if missing. Explicit `setup --models-file ...` remains the non-session CLI alternative.
- Architect retained; primary Lead and Worker start fresh. Maximum 2 Leads and 2 Workers.
- Additional writers require distinct clean registered worktrees. Never auto-stash/reset user changes.
- Manual close does not cancel tickets or prove background writers stopped. `shop_status` reports observed health.
- All execution panes absent: open can rebuild registered seats with state archive; no task prompts or retry.
- Partial/moved/replaced seats: fail closed; inspect, do not delete registration to bypass checks.
- Pause sends Esc only; verify foreground AND background jobs before `retry --writer-stopped`.
- Reports/results, not screen text or idle status, determine acceptance.
- Active run blocks teardown/cleanup; finish tickets and unbind explicitly.
- No automatic model wakeups. Lead patrol only runs during its active turn.

See [workflow](docs/en/WORKFLOW.md), [architecture](docs/en/ARCHITECTURE.md), [migration](docs/en/MIGRATION.md).

## Distribution and licensing

Runtime state, sessions, user configuration, credentials and worktrees are not package content.
`private: true` prevents accidental npm publication; Git-based Pi installation still works.
The project license is pending. Third-party notices in `THIRD_PARTY.md`, `LICENSE.pi-intercom`
and `transport/NOTICE.md` apply to their respective components, not automatically to the entire project.

Both plugin entrypoints must use compatible protocol versions; the bridge identifies the authoritative core.
Before switching versions, stop ongoing mutations and preserve compatible state. Uninstalling code does not delete project data.

## Shutdown, recovery and packaging

User-level shutdown is a **separate authority** from agent control. `Shift+U`
runs the `shop.workstation` `close` action in an independent `core/plugin.py`
process, which previews a bounded plan (`core/shutdown.py`), revalidates it and
only then closes panes. The invoking execution pane is closed last and the
Architect is always retained; execution refuses to run without Herdr
plugin-action context, and no agent tool gains a force-close capability.

Shutdown fails closed (zero closes) on: a bound run or registry binding, an
active/blocked/unknown member, identity drift, missing/moved/replaced/duplicate
members, an unregistered pane in the managed tab, unreadable or oversized state,
a stale/expired plan, transport or handoff uncertainty, foreground work, and
`background_state_unknown` (Herdr protocol 22 exposes no descendant/background
job list, so the plan reports the capability gap instead of assuming stopped).

Execution is journaled: archive → `phase=shutdown_closing` → per-target
verified-absence completion → final layout/Architect/agent verification →
receipt/tombstone → registration removal. A failure leaves `shutdown_partial`
with the remaining roster and no success notification. Repeated close returns
`already_closed` only when a receipt matches and the closed panes are verified
absent.

`core/shutdown.py recover` / the `recovery` plugin action produces one read-only
reconciliation plan for partial, moved, server-restart and mixed-survivor cases
(`present_exact`, `moved_exact`, `missing`, `identity_mismatch`, `replacement`,
`duplicate`, `unknown`). It never restores, reassigns, replays, closes or
deletes anything; every apply path is a separate explicit user action.

Packaging: `package.json` keeps `private: true`, mirrors the manifest version
`0.1.0-alpha.1`, declares `engines.node >= 22.19.0`, and ships an allowlist
(extensions, transport, core, bin, roles, explicit bilingual docs/locales, config, manifest, READMEs,
THIRD_PARTY.md, LICENSE, LICENSE.pi-intercom). `LICENSE` records that the
project license is **pending** — do not publish before the owner decides.
Migration/cutover/rollback: see `docs/en/MIGRATION.md`; architecture: see
`docs/en/ARCHITECTURE.md`.
