# Language

[简体中文](../zh-CN/LANGUAGE.md) · [README](../../README.md)

Shop supports English (`en`) and Simplified Chinese (`zh-CN`).

## Selection

In Herdr Pi:

```text
/shop-language
/shop-language en
/shop-language zh-CN
/shop-language auto
```

No argument opens a bilingual selector. Esc cancels without writing. An explicit argument saves that preference.
A configured bridge is required to save from Pi. Command descriptions and Herdr manifest action titles are bilingual static text; panel contents and notifications use the selected language. Native host buttons/key hints remain controlled by Pi/Herdr.

Without launching Pi or opening Shop:

```sh
<package>/bin/herdr-shop language
<package>/bin/herdr-shop language zh-CN
<package>/bin/herdr-shop language en
<package>/bin/herdr-shop language auto
```

CLI without an argument returns the saved/effective language, file path and revision as JSON. It does not probe Herdr, launch agents or create run/runtime state.

Resolution:

1. Explicit saved `en` or `zh-CN`.
2. For `auto`, first non-empty `LC_ALL`, then `LC_MESSAGES`, then `LANG`.
3. A `zh` language prefix selects Simplified Chinese, including zh_CN/zh-TW variants. All other values, including `C`, `POSIX` and unsupported languages, use English.

## Storage and boundaries

Personal preference: `<bridge.config_dir>/language.json`. With no bridge, the standalone CLI uses `~/.config/shop-workstation/language.json`.

```json
{"version": 1, "language": "auto"}
```

This is not a project/session model layer. It never changes `settings.json`, model profiles, startup candidates, tickets, transport or running agents.
Pi/CLI share the file. Writes use a dedicated lock, content-hash comparison and atomic 0600 replacement. A competing save causes refusal, not silent overwrite. Inspect and reopen after a conflict; no automatic retry.
Invalid/oversized preferences fall back to system detection for display only. Explicit inspection/saves report the error and do not overwrite the bad file. Missing translations fall back to English.
Existing dialogs retain their already-rendered choices; reopen panels after changing language. Other Pi processes read the preference on subsequent display operations. No process restart or model call is needed.

## What stays unchanged

- Command names/flags, action IDs, JSON keys, schemas, status values, error codes, paths and identifiers.
- User input, model/provider IDs, original errors, historical evidence and JSON fact views. A localized explanation can surround an unchanged raw error.
- Agent reply language: follow the user's request. Display language is not permission to translate task text, source code or evidence.

Menus return stable action IDs, never translated text as business operations. English/Chinese confirmations have the same safety boundaries. Cancellation remains cancellation in either language.

## Documents and validation

[English README](../../README.md) and [Chinese README](../../README.zh-CN.md) link to paired guides under `docs/en/` and `docs/zh-CN/`.
Runtime role prompts refer to the canonical English workflow; they do not switch policy with display language. Licenses and original third-party attributions are not rewritten as translations.

Offline tests check catalogue/placeholder parity, fallback, shared Python/TypeScript preferences, concurrent-save refusal, both-language action routing, narrow-screen rendering, links and package contents.
Real Pi/Herdr rendering, input methods and host UI behavior still require isolated live validation. Offline tests do not constitute that acceptance.
