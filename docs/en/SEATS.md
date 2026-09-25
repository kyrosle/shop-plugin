# Ephemeral seats

[中文](../zh-CN/SEATS.md) · [README](../../README.md)

How Shop works. No panes are opened in advance and there are no ticket files to write. Your Pi (Architect) writes a short SPEC and PLAN; each downstream seat then starts in a new Herdr pane from a child session built from its parent's context, reports once through a tool, and closes its own pane.

```text
Architect (your Pi, your model)
  /shop-go <goal> → SPEC.md + PLAN.md → context handoff
      ▼
Lead (new pane, Lead model)       splits PLAN into tasks, dispatches, reviews
  shop_spawn_worker → context handoff
      ▼
Workers (new panes, Worker model; independent tasks run in parallel)
  shop_report → pane closes
      ▼
Lead shop_report → arrives in Architect as a message → Architect checks it against SPEC
```

## Requirements

Install the Pi package (see [installation](INSTALLATION.md)) and choose Lead and Worker models with `/shop-config` ([configuration](MODELS.md)). Architect keeps your current Pi model. The Pi must run inside Herdr.

## Commands

| Command | What happens |
| --- | --- |
| `/shop-go <goal>` | Architect writes a lean `SPEC.md` (decisions, constraints, acceptance) and `PLAN.md`, then the Lead starts automatically when that turn ends |
| `/shop-spec <goal>` | Only writes `SPEC.md` and `PLAN.md`, so you can review or edit them first |
| `/shop-go` or `/shop-go <run-dir>` | Starts the Lead for a reviewed run (the latest `/shop-spec` run by default), after a confirmation |

Keep talking to Architect while seats work; the Lead's report arrives as a message and Architect verifies it against SPEC.

Seat tools: the Lead has `shop_spawn_worker` (with `profile: "fast"` by default, or `"steady"` for complex or critical tasks), `shop_wait_workers` (waits without model calls and reports Worker panes that closed without a report as `lost`) and `shop_report`; Workers have `shop_report`. Seats load only these Shop tools.

## How context is handed over

- The handed-over context is the parent's complete message history; a tool call without a result (such as the spawn call in flight) and orphaned results are removed.
- The receiver decides the form. Up to **16k tokens** passes **verbatim**: with prompt caching this is cheap and nothing is lost. Beyond **30% of the receiving model's context window** it **must be curated** with [pi-context-curator](https://github.com/kyrosle/pi-context-curator) down to that budget. In between it is curated for now.
- The brief (SPEC and PLAN for the Lead; the task plus SPEC for a Worker) is placed in the seat's appended system prompt, so the seat's own auto-compaction cannot drop it.
- The curator analyzer uses the Worker model at low thinking. Its calls are metered separately.
- Seats are told to use only what they were handed. They must not read Pi session files or other seats' records; if something is missing they report `blocked` and say what.
- `SHOP_HANDOFF_MODE=raw|curate|brief` forces a form for experiments. Contexts beyond the budget are still curated.

## Files

Everything for a run lives in `.shop/seats/<run>/` in the project. Creating a run adds `/.shop/` to the repository-local `.git/info/exclude` (never committed) if it is not listed, so run records do not show up in `git status`:

| Path | Content |
| --- | --- |
| `SPEC.md`, `PLAN.md` | What Architect decided |
| `profiles.json` | Lead, Fast Worker and Steady Worker models resolved once for the whole run |
| `seats/<id>.json` | Pane, model, Worker profile, handoff decision (form, reason, token counts, analyzer usage) |
| `prompts/<id>.md` | The seat's appended system prompt (role + brief) |
| `sessions/*.jsonl` | Every seat's session, kept after its pane closes; inspect with `pi --session <file>` |
| `reports/<id>.json` | Each seat's single report |

## Limits

- Alpha. Tested on macOS with the isolated live host lane (see [testing](TESTING.md)).
- Workers share one checkout: parallel tasks must not touch the same files. There is no worktree isolation, automatic commit or integration.
- A seat that closed cannot be resumed; its session is kept for inspection.
- If Architect is busy when the Lead reports, the report is queued like any typed message.
