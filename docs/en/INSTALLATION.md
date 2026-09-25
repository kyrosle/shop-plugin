# Install and update through Git

[简体中文](../zh-CN/INSTALLATION.md) · [README](../../README.md)

Shop needs **both plugin entrypoints**. Installing only one is not a complete installation.
These commands install Shop, not the Pi or Herdr applications. Plugins run with your OS user permissions; review the selected source before installing.

## What goes where?

| Component | Responsibility | Installed with |
| --- | --- | --- |
| Pi extension | `/shop-config`, `/shop-ui`, task delegation, configuration candidates and transport | `pi install` |
| Herdr plugin | Open/close actions, pane lifecycle hooks and native shortcuts | `herdr plugin install` |
| Shared Python core | Identity checks, tickets, configuration and runtime state | Included in both checkouts; **bridge selects one authoritative copy** |

Pin both entrypoints to the **same full Git commit**. Matching `0.1.0-alpha.1` package labels alone is insufficient.
For this installation method, choose the **Herdr-managed checkout** as authoritative core. The Pi extension reads the bridge and calls that core; do not configure a second bridge pointing to the Pi checkout.

## 1. Check prerequisites and preserve existing work

- Current adapter requires Herdr **>= 0.9.0** (tested on **0.9.1**), protocol **22**, schema **1**. CLI and running server must agree. A newer release is accepted only if its protocol/schema match and it still declares every response type and payload key the adapter uses; otherwise Shop refuses to start.
- Pi **0.85.1-compatible API**, Node **>=22.19.0**, Python **>=3.9**, Git. Bun is for development tests, not normal installation.
- Pi provider authentication and models must already be available. Keep the general `@ogulcancelik/pi-herdr` tools available; Shop is not a replacement for them.
- Install on the host that runs the Herdr panes. Remote shortcut forwarding needs separate client configuration.

```sh
pi --version
herdr --version
herdr plugin list
pi list
```

Before changing anything, privately back up Pi package settings, Herdr plugin references/keybindings and any existing Shop bridge/configuration/state outside this repository. Inspect existing managed checkouts for local edits: installers may reset or replace them.

An active run, unknown writer or in-flight setup/member mutation blocks upgrade. Do not infer stopped writers from `idle` or a closed pane. A retained failed registration is not itself an ongoing operation or permission to reset another tab. After verifying that its Shop execution/mutations have stopped and no writer remains unknown, the owner may explicitly choose a code-only upgrade that preserves registration bytes, then handle recovery separately in the affected tab. Follow [migration and rollback](MIGRATION.md); never enable old and new role-injection extensions together.

## 2. Install the same reviewed commit twice

Replace the placeholder with a reviewed full commit from this repository's GitHub history. Run both commands with that one value; do not independently resolve a moving branch for each host.

```sh
SHOP_REF='REPLACE_WITH_REVIEWED_FULL_COMMIT'
pi install "git:github.com/kyrosle/shop-plugin@$SHOP_REF"
herdr plugin install kyrosle/shop-plugin --ref "$SHOP_REF" --yes
```

`--yes` accepts Herdr's install confirmation; it does not start a Shop. Pi installs package dependencies automatically. Do not run `npm ci` inside managed installs or patch their files to work around errors.

Herdr's CLI shows the plugin identity and configuration directory, but may omit its code checkout path. Read `plugin_root` from its local registry as below; do not guess the directory suffix or confuse the configuration directory with the code directory.
The default global Pi checkout is `~/.pi/agent/git/github.com/kyrosle/shop-plugin`.

## 3. Configure the shared bridge — first installation only

For the default Herdr profile, read its registry without modifying it. If you use a custom profile/configuration root, use that profile's registry path instead. Preview before applying:

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

SHOP_CONFIG_DIR="$SHOP_CONFIG" SHOP_STATE_DIR="$SHOP_STATE" \
  python3 "$SHOP_CORE/core/plugin.py" configure
# Confirm core_root, config_dir and state_dir, then:
SHOP_CONFIG_DIR="$SHOP_CONFIG" SHOP_STATE_DIR="$SHOP_STATE" \
  python3 "$SHOP_CORE/core/plugin.py" configure --apply
```

Default bridge: `~/.config/shop-workstation/bridge.json`.

| File/directory | Purpose |
| --- | --- |
| `bridge.core_root` | Herdr-managed authoritative code |
| `bridge.config_dir/settings.json` | Shop global model/thinking overrides |
| `bridge.config_dir/language.json` | Personal interface language |
| `bridge.state_dir` | Registrations, candidates, transport and runtime evidence |
| `~/.pi/agent/settings.json` | Pi's own settings and package references, **not** Shop model settings |

Mutable configuration/state must stay outside either code checkout. No provider credentials belong in the bridge or Shop settings.
If you deliberately use `SHOP_LOCATOR`, set it consistently in both hosts. Avoid conflicting `SHOP_CONFIG_DIR`/`SHOP_STATE_DIR` overrides in their environments.

**Existing bridge? Stop before `--apply`.** It deliberately refuses overwrite. On an ordinary upgrade with unchanged paths, retain it. A different `plugin_root` requires an explicit backed-up cutover preserving config/state paths, not deleting the bridge to bypass the check.

## 4. Reload Pi, select models, add shortcuts

In your existing **Pi session inside Herdr**:

```text
/reload
/shop-language en
/shop-config
```

Language is optional (`zh-CN` or `auto` also supported). `/shop-config` starts in Session scope; press Tab to select Global for reusable defaults. Choose models and supported thinking levels for all four seats (`lead`, `lead-2`, `worker`, `worker-2`), then S to preview and confirm. Auxiliary seats need resolved models even if not started yet. See [model settings](MODELS.md).

Architect keeps its existing Pi model/session. Settings do not change running members. Loading/configuring does not call models or open execution panes.

Back up Herdr's `config.toml`; add these entries only if no conflicting bindings exist. Replace conflicting legacy entries instead of appending duplicates:

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

```sh
herdr config check
herdr server reload-config
```

With the default prefix, press Ctrl+B, release, then U to open. **This step starts fresh Lead/Worker Pi processes**; do it only when ready, from one unzoomed Pi pane at least 140 columns × 40 rows. Installation itself does not do this. Use `/shop-ui` afterward.
Ctrl+B then Shift+U requests checked shutdown, not force-close. It can refuse when background-writer safety is unknown.

## 5. Verify without starting members

```sh
git -C "$HOME/.pi/agent/git/github.com/kyrosle/shop-plugin" rev-parse HEAD
git -C "$SHOP_CORE" rev-parse HEAD
herdr plugin list
herdr plugin action list --plugin shop.workstation
python3 "$SHOP_CORE/core/plugin.py" doctor
```

Both HEADs and installed refs must equal `SHOP_REF`; inspect doctor output for errors. Doctor checks environment/server availability, **not every agent payload, provider permission or end-to-end operation**.
After `/reload` or saving `/shop-config`, there should be no configuration-publication warning. For the identity path specifically, use Pi's `!` shell command from that same original Pi session, replacing the path:

```text
!python3 /absolute/plugin_root/core/configuration.py --request '{"action":"identify"}'
```

This is a read-only identity query; an ordinary shell pane is not a Pi identity and cannot substitute for this check. Do not create a second Pi process in the occupied pane just to test it.

## Updates and rollback

1. Stop/settle Shop work safely; inventory bindings, writers and partial operations. Back up the current refs, bridge, configuration and state. Do not upgrade active execution members.
2. Choose the new full commit and repeat **both** install commands. Pinned refs do not follow `main` automatically; plain `pi update` does not coordinate Herdr.
3. Inspect the new Herdr `plugin_root`. Keep an unchanged bridge; if the path changed, perform an explicit cutover with the same config/state directories. Do not rerun fresh-install `configure --apply` over an existing bridge.
4. Verify refs, doctor and the identity query, then `/reload` each affected Pi session and reopen `/shop-config`. Do not overwrite model/language files or append the shortcuts again.
5. Only then authorize a separate live Shop smoke test. Installation checks are not task-delivery acceptance.

Rollback reinstalls the previous **matching pair of refs**, restores compatible bridge/configuration and reloads Pi after writers are verified stopped. Keep state/evidence; uninstall is not proof of stopped processes. See [migration](MIGRATION.md).

## Troubleshooting

| Symptom | Check/action |
| --- | --- |
| `/shop-config` missing | Pi entrypoint installed and enabled? Duplicate project/global package source? Running Pi inside Herdr? Run `/reload`. |
| U does nothing | Herdr plugin enabled, bindings conflict-free, native config reloaded? Inspect plugin logs. |
| `unknown field 'screen_detection_skipped'` | Older Shop adapter rejects an optional Herdr boolean. Update both Git refs to a commit containing the fix, keep bridge/config, then `/reload`. Do not remove identity checks. |
| `Another Shop core owns bridge` | Command used the wrong checkout. Use `bridge.core_root`; do not point the bridge at whichever copy ran last. |
| `Existing bridge: inspect and back up before switching cores; no automatic overwrite` | First-install command used on an existing setup. Preserve bridge or plan an explicit cutover. |
| Configuration candidate missing/expired | In original Architect, `/reload` or save `/shop-config`; inspect preceding identity/configuration error. Do not hand-write candidates or bypass freshness. |
| `background_state_unknown` on close | Safety capability gap, not permission to force-close or delete registration. Inspect foreground/background work and recovery plan. |
