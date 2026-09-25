# Shop settings: global, project and session

[简体中文](../zh-CN/MODELS.md) · [README](../../README.md)

`/shop-config` chooses the models and thinking levels of the seats Shop starts. Architect is not a Shop seat: it keeps your Pi model; use `/model` and `/thinking`. Interface language is separate: see [language](LANGUAGE.md).

## Seats

| Seat | Used for |
| --- | --- |
| **Lead** (`lead`) | The single Lead of a run: splits PLAN, dispatches, reviews, reports |
| **Fast Worker** (`worker`) | Every Worker by default: routine tasks, batch edits, fast execution |
| **Steady Worker** (`worker-2`) | A Worker the Lead starts with `profile: "steady"` for complex, risky or critical tasks |

The names describe intended use, not guarantees about price or quality; choose each model yourself. The curator analyzer that curates large handoffs uses the Fast Worker model.

## The panel

Run `/shop-config` in Pi (TUI required). It does not call models, start seats or open panes.

- It opens on the **Session** scope; Tab switches Global / Project / Session. Untrusted projects cannot use the Project scope.
- ↑↓ selects a field; Enter opens the model picker (search, fuzzy match, fixed 10-row viewport) or the thinking-level list. Choosing only changes the draft.
- "Inherit parent" removes an override; "Pi default (explicit)" writes `thinking=null`.
- S previews old/new overrides before confirmation; R previews clearing this scope; Esc cancels. A save writes only the current scope.
- Models come from the current Pi's authenticated catalogue; thinking choices follow each model's capabilities.

Saving applies to the **next** `/shop-go`. Seats that are already running keep their models.

## Layers and files

| Scope | Storage |
| --- | --- |
| Global | `~/.config/shop-workstation/settings.json` (or `$SHOP_CONFIG_DIR`) |
| Trusted project | `<cwd>/.pi/shop.json` |
| Session | `shop-settings` custom entry on the current Pi branch (not an LLM message) |

Precedence: **session > project > global**. Inside a layer: **seat > role > defaults**, so `worker` settings also reach `worker-2` unless `worker-2` overrides them. The panel saves only fields that differ from the parent.

```json
{
  "version": 1,
  "models": {
    "defaults": { "thinking": "medium" },
    "lead": { "model": "provider/mid-model", "thinking": "high" },
    "worker": { "model": "provider/fast-model", "thinking": "low" },
    "seats": { "worker-2": { "model": "provider/strong-model", "thinking": "high" } }
  }
}
```

Seats: `lead`, `worker`, `worker-2`. A `lead-2` entry from the earlier alpha is read and ignored, and disappears on the next save. Thinking: `off / minimal / low / medium / high / xhigh / max / null`.

## How a run uses the settings

When `/shop-go` starts a Lead, Architect resolves the effective settings (global + project + its session) once and writes them to `<run>/profiles.json`. The Lead and every Worker of that run use that file, so a session override made in Architect applies to the whole run, and editing settings mid-run does not change it.

A run fails to start with a clear message if the Lead, Fast Worker or Steady Worker has no model.

## Legacy migration

If `settings.json` is absent, an older `models.json` in the same directory is read. `/shop-config migrate` previews and writes a new `settings.json`, keeping `models.json` as a backup.

## Concurrency

Settings are capped at 64 KiB. Saves use a configuration lock and content-hash compare-and-swap across parent layers. A concurrent change refuses the save and asks you to reopen the panel. Files are replaced atomically with mode 0600.
