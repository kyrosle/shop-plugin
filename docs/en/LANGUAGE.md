# Language

[简体中文](../zh-CN/LANGUAGE.md) · [README](../../README.md)

Shop supports English (`en`) and Simplified Chinese (`zh-CN`).

## Selection

In Pi:

```text
/shop-language
/shop-language en
/shop-language zh-CN
/shop-language auto
```

No argument opens a bilingual selector. Esc cancels without writing. An explicit argument saves that preference.
Command descriptions are bilingual static text; panel contents and notifications use the selected language. Native host buttons and key hints remain controlled by Pi/Herdr.

Without launching Pi:

```sh
python3 <package>/core/language.py            # show saved/effective language as JSON
python3 <package>/core/language.py zh-CN      # save a preference
```

The CLI never probes Herdr, starts seats or creates run state.

Resolution:

1. Explicit saved `en` or `zh-CN`.
2. For `auto`, first non-empty `LC_ALL`, then `LC_MESSAGES`, then `LANG`.
3. A `zh` language prefix selects Simplified Chinese, including zh_CN/zh-TW variants. All other values, including `C`, `POSIX` and unsupported languages, use English.

## Storage and boundaries

Personal preference: `~/.config/shop-workstation/language.json` (or `$SHOP_CONFIG_DIR/language.json`).

```json
{"version": 1, "language": "auto"}
```

This is not a project/session model layer. It never changes `settings.json`, run profiles or running seats.
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
Seat role prompts are English and do not change with display language.

Offline tests check catalogue/placeholder parity, fallback, shared Python/TypeScript preferences, concurrent-save refusal, narrow-screen rendering, links and package contents. The real-host lane saves and switches language in a real Pi.
