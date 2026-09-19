# Pi + Herdr Shop

English · [简体中文](README.zh-CN.md)

**Turn your current Pi into a visible, configurable collaboration workstation with recorded delivery.**
Keep talking to Architect in your original Pi. A primary Lead breaks down work, dispatches tickets and reviews results; Workers execute specific tasks. Each member is an independent, visible Pi in Herdr, not a clone of your conversation.

> **Local alpha.** Offline regression, packaging and installation checks exist; the complete real-model collaboration loop still needs controlled acceptance. This is not an unattended scheduler, nor a guarantee of faster, cheaper or automatic task completion.

[Install](#installation) · [Configure](#configuration) · [Use](#usage) · [What to expect](#what-to-expect) · [Reset](#failures-and-reset) · [Update](#updates-and-retained-data)

## Roles and models: what does Shop configure?

| Role | Responsibility | Model source | When started |
| --- | --- | --- | --- |
| **Architect** | Talk with you, clarify objectives, delegate to primary Lead, summarize delivery | **Your current Pi model and thinking level** | Original Pi retained, not restarted |
| **Primary Lead** | Sole ticket dispatcher, coordinator and reviewer | Primary Lead profile in `/shop-config` | On setup |
| **Auxiliary Lead** | Additional execution or review, not a second dispatcher | Auxiliary Lead profile in `/shop-config` | On demand |
| **Fast Worker (low cost)** | Routine tasks, batch edits, fast execution | Model and thinking level you choose for this purpose | On setup |
| **Steady Worker (reliable)** | Complex implementation, difficult fixes, critical changes | Model and thinking level you choose for this purpose | On demand |

**Shop does not automatically change Architect's model, thinking level or session.** Use Pi's native `/model` and `/thinking` in Architect. `/shop-config` configures execution seats only.

Both Workers use the same kind of role implementation with independently configurable models. Names describe intended use, **not guarantees about model pricing/quality or automatic task-to-model routing**. Internal IDs remain `worker` / `worker-2`; existing profiles need no migration for these display names.

Initial setup is **Architect + primary Lead + Fast Worker**. Capacity is **2 Leads + 2 Workers**, plus original Architect. Auxiliary seats are not all started at setup; expansion is bounded and on demand, not an unlimited background autoscaler.

## What to expect

Illustrative initial layout—not a live screenshot:

```text
┌────────────────────────┬────────────────────────┐
│                        │ Primary Lead           │
│ Architect              │ Plan, dispatch, review │
│ Your original Pi       ├────────────────────────┤
│ Original model/session │ Fast Worker            │
│                        │ Execute specific tasks │
└────────────────────────┴────────────────────────┘
       Add Auxiliary Lead / Steady Worker as needed
```

| What you get | What it does not mean |
| --- | --- |
| Visible independent members, per-seat model profiles | Cloning Architect's history or forcing one model on every member |
| Purpose-labeled profiles for your own cost/capability choices | Automatic benchmarking or guaranteed savings/reliability |
| Tickets, checkpoints, results and review records | Treating `idle` or message delivery as task completion |
| Status, explicit handoffs, development preparation and delivery actions | Creating worktrees, integrating or pushing without confirmation |
| Ordinary Pi interaction outside explicit delegation | Forwarding every message to Lead once panes are open |

Business flow: **clarify objective → create and bind a run → primary Lead dispatches → executor delivers → primary Lead reviews → Architect summarizes**. Roles and tools cooperate to follow this process; it is not a fixed automatic pipeline.

Expected delivery includes findings or a change summary, applicable check commands and result evidence, review conclusions, remaining work and risks. Primary Lead saves the run's `SUMMARY.md` / `REVIEW.md`; Architect checks them before reporting to you. “Dispatched” or an idle pane is not delivery.

## Installation

### 1. Prerequisites

- Herdr **0.9.0**; the adapter pins protocol **22 / schema 1**. CLI and running server must be compatible.
- Pi **0.85.1-compatible API**, Node **>=22.19.0**, Python **>=3.9**, Git. Bun is needed only for development tests.
- Configure Pi providers, credentials and available models first. Shop ships no credentials or personal model selections.
- Keep `@ogulcancelik/pi-herdr` available for general agent inspection, waiting and control.
- Only macOS has been tested. Linux is a POSIX implementation target without live acceptance; Windows is unsupported.

Plugins run with your OS user permissions, not in a sandbox. Review source and back up Pi settings, Herdr plugins/keybindings, Shop bridge, configuration and registrations. Do not switch code while execution members are working or setup mutations remain in progress.

### 2. Install both entrypoints at one Git commit

**Both entrypoints are required.** Pi provides commands and configuration UI; Herdr provides shortcuts, pane lifecycle actions and events. Installing one does not install the other.

This example pins an alpha code baseline containing `/shop-reset`; it does not follow `main`. For upgrades, substitute a reviewed full SHA and use exactly the same value in both commands:

```sh
SHOP_REF='e7ec14cdf190ec2cedec505c5e0e55e38ad2fd69'
pi install "git:github.com/kyrosle/shop-plugin@$SHOP_REF"
herdr plugin install kyrosle/shop-plugin --ref "$SHOP_REF" --yes
```

Installation updates code/package references only: no Lead/Worker starts or dispatch. Do not load both the old manually installed `herdr-shop-mode` extension and this extension.

### 3. First installation: configure the shared bridge

Use the **Herdr-managed checkout** as the authoritative Python core. Pi calls that core through the bridge. Read the actual checkout path from the default Herdr registry rather than guessing its directory suffix:

```sh
SHOP_CORE="$(python3 - <<'PY'
import json
from pathlib import Path
plugins = json.loads((Path.home() / '.config/herdr/plugins.json').read_text())
matches = [p for p in plugins if p['plugin_id'] == 'shop.workstation']
assert len(matches) == 1, 'Expected exactly one Shop plugin registration'
print(matches[0]['plugin_root'])
PY
)"
SHOP_CONFIG="$(herdr plugin config-dir shop.workstation)"
SHOP_STATE="$HOME/.local/state/shop-workstation"

# Preview first. Inspect paths before separately running --apply.
SHOP_CONFIG_DIR="$SHOP_CONFIG" SHOP_STATE_DIR="$SHOP_STATE" \
  python3 "$SHOP_CORE/core/plugin.py" configure

# First configuration only, after approving the preview.
SHOP_CONFIG_DIR="$SHOP_CONFIG" SHOP_STATE_DIR="$SHOP_STATE" \
  python3 "$SHOP_CORE/core/plugin.py" configure --apply
```

Default bridge: `~/.config/shop-workstation/bridge.json`. **Do not rerun `configure --apply` for an ordinary upgrade with an existing bridge, or delete the bridge to bypass its guard.** See the [full installation guide](docs/en/INSTALLATION.md) for custom Herdr profiles and `SHOP_LOCATOR`.

### 4. Configure Herdr shortcuts

Back up Herdr's `config.toml`, then add or replace these entries; do not append duplicates:

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
description = "Shop shutdown preview and checks"
```

After editing:

```sh
herdr config check
herdr server reload-config
```

With the default prefix, press and release Ctrl+B, then U to open; Shift+U requests safe shutdown. For remote use, install on the host running the panes and configure key forwarding in the Herdr client separately.

### 5. Load and verify

In the Herdr Pi tab you intend to test:

```text
/reload
/shop-language en
/shop-config
```

Language choices are `en`, `zh-CN`, `auto`, or run `/shop-language` to choose interactively. Display language does not determine agent reply language.

In the shell used above, verify without starting members:

```sh
git -C "$HOME/.pi/agent/git/github.com/kyrosle/shop-plugin" rev-parse HEAD
git -C "$SHOP_CORE" rev-parse HEAD
python3 "$SHOP_CORE/core/plugin.py" doctor
```

Both HEADs should equal `SHOP_REF`. Doctor checks the environment, not provider inference or task collaboration.

## Configuration

`/shop-config` opens the **Session** scope by default. Press Tab to choose **Global** for reusable defaults for future Shops.

- Tab switches Global / trusted Project / Session; ↑↓ selects a field, Enter edits.
- Choose models and thinking levels for Primary Lead, Auxiliary Lead, Fast Worker and Steady Worker. Multiple seats may share a model.
- The model picker supports fuzzy search across ID, name and provider, with a bounded viewport of up to 10 rows and scrolling/page navigation.
- “Inherit parent” clears an override. Thinking choices follow the selected model's capabilities.
- S previews and confirms a save; R previews clearing model overrides in this scope; Esc cancels. **This R is not `/shop-reset`.**
- Incomplete drafts can be saved, but all four execution seats must resolve to a model before setup, including auxiliary seats not yet started.

Precedence: **Session > trusted Project > Global > built-in defaults**; within each layer, **seat > role > defaults**. No model brand is built in.

### When do model changes take effect?

| Action | Effect |
| --- | --- |
| Save `/shop-config` | Future new Shops; no hot-switch of existing members or change to Pi defaults |
| Expand or restore the current Shop | Uses that Shop's pinned setup-time model snapshot |
| Request an idle-seat profile in `/shop-ui` | Receiving user confirms separately; may optionally update that Shop's target-seat snapshot |
| Architect's `/model` or `/thinking` | You change Architect directly, outside remote seat requests |

Existing `models.json` can be previewed and imported with `/shop-config migrate`; no automatic overwrite. See [configuration and inheritance](docs/en/MODELS.md).

## Usage

### 1. Open seats, without automatically dispatching work

Use a Herdr Pi in the project directory, in an unzoomed tab containing only that Pi. Terminal area must be at least **140 columns × 40 rows**. Save configuration, then press **Ctrl+B → U**.

Architect stays; primary Lead and Fast Worker start fresh Pi sessions. Add auxiliary seats as needed. Additional writers require distinct, clean, registered worktrees. Shop does not automatically stash, reset or commit your changes.

### 2. Explicitly delegate one task

In **Architect**, for example:

```text
/shop Investigate login failures. Read-only first: provide reproduction steps, root cause and a proposed fix.
```

Or explicitly authorize changes:

```text
/shop Fix login timeouts. Limit changes to auth and its tests; provide test evidence and do not push automatically.
```

`/shop` activates delegation for **this request only**. Ordinary questions, chat and edit requests remain local to the current Pi. Use `/shop Additional requirement: …` to send a follow-up to the workstation.

The Shop must be ready. For a clear new task in an unbound Shop, Architect's workflow creates an independent run, writes the necessary objective/plan, binds it explicitly and hands it to primary Lead. A different task already bound to the Shop requires coordination first. Opening panes does not create or automatically choose a run.

The default workflow requires Architect to wait, check delivery and summarize—not stop at “dispatched.” Background execution must be requested separately and has no automatic wake-up or continuous-patrol guarantee. See [task workflow](docs/en/WORKFLOW.md).

### 3. Inspect, intervene and deliver

| Command / action | Purpose |
| --- | --- |
| `/shop-status` | Current Shop status summary |
| `/shop-ui` | Status/identity, handoffs/receipts, development preparation, delivery/integration, idle-seat profiles, interventions, diagnostics |
| `/shop-config` | Execution models and thinking levels for future Shops |
| `/shop-language` | English, Chinese or system detection |
| `/shop-reset` | Preview and confirm archival of this tab's early failed setup registration |
| Ctrl+B → Shift+U | Request identity/task-checked shutdown, keeping Architect |

Viewing the workbench does not dispatch; business changes additionally need an explicit run binding. Message delivery ≠ handoff acceptance ≠ ticket acceptance. Integration supports confirmed fast-forward only, with no automatic push, run closure or resource release. See [workbench guide](docs/en/WORKBENCH.md).

Before shutdown, retain delivery, settle tickets, verify stopped writers and explicitly unbind. **`idle` does not prove background work stopped.** The current Herdr protocol lacks background-process visibility; shutdown can refuse with `background_state_unknown`. Do not delete registrations or force-close panes to bypass it.

## Failures and reset

Do not repeatedly press U after setup fails: a mutation may have succeeded even though its response could not be parsed.

In the **affected tab**, run:

```text
/shop-reset
```

It previews the original error and identity differences before confirmation. Eligibility is limited to failure before member splitting, no members or task binding, and the original Pi pane/terminal still matching. A lost display name may be acknowledged after strict checks. Existing members, extra panes, replaced terminals or unknown identity block reset.

Success archives exact registration bytes before removing this tab's active record. **No Pi closes, configuration resets, task/worktree deletion, other-tab changes or automatic setup.** Start manually with U when ready.

`/shop-reset` is not the legacy member-closing CLI `reset`, nor a global “force clear cache.” Other failures need [safe recovery](docs/en/MIGRATION.md#reset-an-early-failed-setup).

## Updates and retained data

1. Stop ongoing Shop mutations; inspect execution members, tasks and writers. Back up configuration, registrations and previous installation refs.
2. Install both entrypoints at the same new full SHA, verify HEADs and bridge target, then `/reload` the Pi session being tested.
3. Preserve existing bridge and model/language settings. A code upgrade is not a workstation reset or automatic active-task migration.
4. If retaining a verified inactive failed registration, explicitly choose a code-only upgrade and handle recovery separately. Another tab's old registration is not permission to clean it. See [updates and rollback](docs/en/INSTALLATION.md#updates-and-rollback).

Data is grouped by responsibility, not squeezed into one file:

| Data | Default location / rule |
| --- | --- |
| Shared code locator | `~/.config/shop-workstation/bridge.json` |
| Global models / personal language | `<bridge.config_dir>/settings.json`, `language.json` |
| Project / session settings | Project `.pi/shop.json` / custom entry on the current Pi branch |
| Registrations, member sessions, runtime evidence | `<bridge.state_dir>/`; registrations in `runtime/`, scoped by socket + tab |
| Early failed-registration archives | `<bridge.state_dir>/reset-archive/` |
| Runs, tickets, bindings, delivery evidence | Project `.shop/` |

With the installation commands above, state lives at `~/.local/state/shop-workstation`; Herdr's `plugin config-dir` selects configuration storage. Mutable data stays outside both checkouts. Uninstalling code does not authorize deletion of tasks, sessions or worktrees.

## Development, validation and licensing

```sh
npm ci --ignore-scripts
TEST_ROOT="$(mktemp -d)"
SHOP_LOCATOR="$TEST_ROOT/bridge.json" SHOP_CONFIG_DIR="$TEST_ROOT/config" \
  SHOP_STATE_DIR="$TEST_ROOT/state" npm test
npm run typecheck
npm pack --dry-run
```

For development, explicitly use `herdr plugin link /absolute/path/to/shop-plugin` and `pi install /absolute/path/to/shop-plugin`. Use a development bridge only in an isolated, inactive environment; do not overwrite an existing installation or enable duplicate role extensions.

Offline tests cover configuration, identity, transport, ticketing, recovery and UI substitutes. They do not establish actual model understanding, collaboration quality or all host interactions. Use a dedicated tab/isolated project for live testing, not an active business workstation. No permanent scheduler, automatic retry or unconditional force-close is provided.

`private: true` prevents accidental npm publication, not Git installation. Runtime state, sessions, credentials and worktrees are excluded from the package. The project license remains an owner decision; see [LICENSE](LICENSE), [THIRD_PARTY.md](THIRD_PARTY.md) and [LICENSE.pi-intercom](LICENSE.pi-intercom). Third-party licenses do not automatically license the entire project.

Further reading: [installation](docs/en/INSTALLATION.md) · [configuration](docs/en/MODELS.md) · [workbench](docs/en/WORKBENCH.md) · [task workflow](docs/en/WORKFLOW.md) · [architecture](docs/en/ARCHITECTURE.md) · [language](docs/en/LANGUAGE.md) · [migration and recovery](docs/en/MIGRATION.md)
