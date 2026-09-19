# Shop settings: global, project and session

[简体中文](../zh-CN/MODELS.md) · [README](../../README.md)

`/shop-config` manages configuration layers, inheritance, saves and migration.
Explicit changes to an idle running seat are separate: see [workbench](WORKBENCH.md).
Interface language is a personal preference, not a model layer: see [language](LANGUAGE.md).

## Pi settings entrypoint

Run `/shop-config` inside Herdr Pi. Requires a configured bridge and Pi TUI; it does not call models, dispatch work or open panes.

- Opens the **Session** scope; Tab switches Global / Project / Session.
- Settings use a titled, bordered panel with scope tabs and a shortcut footer. ↑↓ selects a field; Enter opens the model or thinking-level selector.
- Model selection has a search input, fuzzy matching across model ID/name/provider, and a fixed viewport of up to 10 rows (smaller on short terminals). ↑↓, PageUp/PageDown and fullscreen mouse wheel scroll results; the selected model name appears below. Enter chooses, Esc/Ctrl+C returns without choosing.
- Clear model search to access “Inherit parent”. Selection only changes the Shop draft: it does not switch Architect's model, save Pi defaults, refresh providers or call a model. Save/confirmation remains a separate step.
- Models come from the current Pi's authenticated, available catalogue; thinking choices follow model capabilities.
- “Inherit parent” removes an override; “Pi default (explicit)” writes `thinking=null`, overriding an explicit parent level.
- S previews old/new overrides before confirmation; R previews clearing this scope's model group; Esc cancels.
- Switching scopes retains drafts, but a save writes only the current scope.
- Each field shows its source, target path/session and the new-Shop-only effect.
- Untrusted projects cannot use the Project scope. Non-Architect seats can edit preferences, but their session settings do not change the current Shop.

Architect keeps its native Pi model/session; use `/model` and `/thinking`.
Saving does not remotely switch running members, restart Pi or change Pi's global defaults.
The panel exposes the model group only; no setting bypasses identity or stopped-writer checks.

## Layers and files

| Scope | Storage |
| --- | --- |
| Global | `<bridge.config_dir>/settings.json` |
| Trusted project | `<Architect project root>/.pi/shop.json` |
| Session | `shop-settings` custom entry on the current Pi branch |
| Execution snapshot | `model_profiles` in Shop runtime registration; not a dynamic inheritance layer |

Without a Shop, the project root is the current Pi cwd. With registration, it is the registered cwd, never an auxiliary worktree.
Pi distributions use their public `CONFIG_DIR_NAME` instead of hard-coding `.pi`. Trust in a different cwd is not borrowed for the registered root.
Session entries carry `project_root`; they do not cross roots or require scanning session JSONL. Custom entries are not LLM messages.

Field precedence: **session > project > global > built-in**.
Within each layer: **seat > role > defaults**. Thus a session worker-role setting can override the same field in a global worker-2 seat setting.
The UI saves only seat fields differing from the parent. Unspecified fields inherit; saving/resetting preserves advanced groups outside models.
Built-ins specify no model brand; absent thinking is left to Pi.

Replace provider/model placeholders:

```json
{
  "version": 1,
  "models": {
    "defaults": { "thinking": "medium" },
    "lead": { "model": "provider/model-a", "thinking": "high" },
    "worker": { "model": "provider/model-b" },
    "seats": {
      "lead-2": { "model": "provider/model-c", "thinking": "high" },
      "worker-2": { "model": "provider/model-d", "thinking": "low" }
    }
  }
}
```

Seats: `lead`, `lead-2`, `worker`, `worker-2`. Model strings: at most 512 characters.
Thinking: `off / minimal / low / medium / high / xhigh / max / null`.
Keep model and thinking separate instead of appending thinking shorthand to model IDs.
Partial configuration can be saved, but **all four seats must resolve to models at setup**, including auxiliary seats not yet started.
Python validates syntax; the UI also validates current availability/capability. Another Pi may have different credentials/catalogue; it validates its actual launch.

## Saving is not changing running members

```text
Built-in → Global → Trusted project → Architect session
                                      ↓ new Shop
                               pinned model_profiles
                                      ↓ member launch
                                  launch_profile
```

- Layer saves affect new Shops only.
- Expansion/recovery uses the existing Shop's pinned snapshot, not freshly edited settings files.
- `launch_profile` records requested launch parameters, not a live observation. Manual Pi model changes do not rewrite it.
- `/new`, `/resume`, `/tree` and reload do not modify existing Shops or restart members.
- `/shop-ui` → Idle-seat configuration is a separate explicit request. The receiving user confirms; public Pi APIs apply it. It can affect only the current session or also that seat's Shop snapshot for future launches. It does not change configuration layers or historical launch parameters. See [workbench](WORKBENCH.md#single-seat-model-requests).
- Architect remains on native `/model` and `/thinking` and does not accept such requests.

## How setup shortcuts obtain session settings

The Pi extension publishes bounded candidates to `<state_dir>/config-candidates/` every five seconds. They contain configuration and instance metadata, not chats, credentials or full session paths.
Candidates bind socket/tab/pane/terminal, session, PID, extension instance, project root, trust and session overrides.

Setup checks the original Architect's candidate, identity, live process and 20-second freshness; rereads disk layers; then pins resolved settings and provenance.
Missing/invalid candidates cause refusal, never silent global-only fallback. Use `/reload` or `/shop-config` in the original Architect before setup.
Shutdown/reload/session switching removes only this instance's candidate. Crashed-process records do not authorize takeover by age or name.
This is a same-user coordination protocol, not a malicious-code sandbox or an atomic freeze on manual input in another pane.

## Legacy migration

`<config_dir>/models.json` remains a read-only compatibility source only when `settings.json` is absent. The panel shows legacy status.
Run `/shop-config migrate` and confirm the preview:

- Creates a new settings.json, never overwrites an existing modern file.
- Keeps models.json as backup; after migration it is no longer read or dual-written.
- Does not change current Shops, tickets, sessions or worktrees.
- A direct Global save with a legacy file asks for migration first; explicit Session/Project overrides remain available.

Legacy registrations lacking `model_profiles` pin global compatibility settings on the next explicit launch/recovery; they never pretend to have recovered old Architect session settings.

## Explicit file entrypoint

For a **new** Shop only; explicitly bypasses Project/Session layers:

```sh
/absolute/package/bin/herdr-shop setup --models-file /absolute/models.json --dry-run
/absolute/package/bin/herdr-shop setup --models-file /absolute/models.json
```

The file uses the legacy model-group shape in `config/models.example.json`, not a settings.json wrapper with version/models.
It is not merged with default configuration. Existing Shops and non-setup commands reject `--models-file`.

## Concurrency and validation

Configuration is capped at 64 KiB. Cooperating writers use a configuration lock and content-hash CAS, including parent layers.
Concurrent changes refuse the save and require reopening; files are atomically replaced with mode 0600.
Previews/session saves may create a global configuration lock, never a lock in the business worktree.
Raw shells and external editors do not participate in that lock; this is not an OS sandbox.

Offline tests cover precedence, inheritance/reset, CAS, migration, UI/branch lifecycle, candidate identity and cross-language consumption.
Real Pi/Herdr shortcuts, provider permissions and manual session-switch races need separate controlled validation.
