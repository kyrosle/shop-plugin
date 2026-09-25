# Pi + Herdr Shop

English · [简体中文](README.zh-CN.md)

**Think with your strongest model; let cheaper seats do the work.** Shop turns the Pi you are talking to inside Herdr into an Architect. You align with it, it writes a short SPEC and PLAN, and a Lead and Workers carry the work out and report back. Each of them is a separate Pi in its own Herdr pane, running the model you chose for that role.

> **Alpha.** Verified on macOS with an isolated real-Herdr / real-Pi test lane, including paid runs against a real provider (see [testing](docs/en/TESTING.md)). It is not an unattended scheduler.

```text
┌──────────────────────────┬────────────────────────────┐
│ Architect                │ Lead            (new pane) │
│ your Pi, your model      │ splits PLAN, dispatches,   │
│ /shop-go <goal>          │ reviews, reports           │
│  → SPEC.md + PLAN.md     ├────────────────────────────┤
│  ← final answer          │ Worker(s)      (new panes) │
│                          │ one task each, then close  │
└──────────────────────────┴────────────────────────────┘
```

## Roles

| Role | Does | Model |
| --- | --- | --- |
| **Architect** | Talks with you, writes SPEC/PLAN, checks the result, answers you | Your current Pi model and thinking level (Shop never changes it) |
| **Lead** | Turns PLAN into tasks, dispatches Workers, reviews their reports, reports back | `/shop-config` → Lead |
| **Fast Worker** | Executes one task (the default) | `/shop-config` → Fast Worker |
| **Steady Worker** | Executes one complex or critical task when the Lead asks for it | `/shop-config` → Steady Worker |

Seats are **ephemeral**: no pane is opened in advance, each seat starts when it is needed, reports exactly once and closes its own pane. Their sessions are kept.

## Installation

Prerequisites: Herdr **≥ 0.9.0** (tested 0.9.1), Pi (tested 0.86.1), Node **≥ 22.19**, Python **≥ 3.9**, Git. Configure your Pi providers and credentials first; Shop ships none. Only macOS has been tested.

```sh
pi install git:github.com/kyrosle/shop-plugin@<commit>
```

Then `/reload` in Pi. Pi runs `npm install`, which fetches the pinned [pi-context-curator](https://github.com/kyrosle/pi-context-curator) over HTTPS. There is no Herdr plugin and no bridge to configure. Upgrading from the earlier workstation alpha: see [installation](docs/en/INSTALLATION.md).

## Configuration

Run `/shop-config` in Pi to choose the Lead, Fast Worker and Steady Worker models and thinking levels, per session, trusted project or globally. Architect uses Pi's own `/model` and `/thinking`. Details: [configuration](docs/en/MODELS.md). Interface language: `/shop-language` ([language](docs/en/LANGUAGE.md)).

## Usage

In a Pi running inside Herdr:

```text
/shop-go Fix the case-insensitive word count in textstats.py and add unique_words(); keep all tests green.
```

Architect writes `SPEC.md` (decisions, constraints, acceptance) and `PLAN.md`, then the Lead starts automatically. Keep talking to Architect meanwhile. When the Lead finishes, its report arrives as a message, and Architect answers your request first and then notes its check against SPEC.

To review before anything starts, use `/shop-spec <goal>`, edit the files if you like, then `/shop-go`.

## Context handoff

Each seat starts from a child session built from its parent's history. Up to 16k tokens pass verbatim, bracketed as the parent's history, which is cheap with prompt caching and loses nothing. Beyond 30% of the receiving model's window, the history is curated down to that budget. SPEC, PLAN and task are pinned in the seat's system prompt, so the seat's own auto-compaction cannot drop them. Seats may not read other sessions; when information is missing they report `blocked`. See [ephemeral seats](docs/en/SEATS.md).

## What to expect

Measured on the isolated live lane with `deepseek-v4.1-flash` in every seat (medians):

| Workflow | Pass | Cost | Time to answer |
| --- | --- | --- | --- |
| Earlier resident workstation, ticket files (baseline, 5 runs) | 5/5 | $0.021 | 186 s |
| `/shop-go`, same task (3 runs) | 3/3 | $0.009 | 99 s |

Other live checks: a constraint that exists only in the discussion reaches the output (3/3 verbatim, 3/3 curated); parallel Workers overlap; a missing input ends in an honest `blocked`; a Worker killed mid-task is replaced; a small real bug-fix-plus-feature task passes hidden tests. These are small tasks. Model tiering showed no quality gain on them, only higher cost.

## Files and data

| What | Where |
| --- | --- |
| Run: SPEC, PLAN, per-seat records, prompts, sessions, reports | `<project>/.shop/seats/<run>/` (added to `.git/info/exclude`) |
| Global settings, language | `~/.config/shop-workstation/` (`SHOP_CONFIG_DIR`) |
| Project / session settings | `<project>/.pi/shop.json` / an entry on the current Pi branch |

## Limits

- Workers share one checkout: parallel tasks must not touch the same files. There is no worktree isolation, automatic commit or integration.
- A closed seat cannot be resumed; its session is kept for inspection (`pi --session <file>`).
- If Architect is busy when the Lead reports, the report queues like typed input.

## Development

```sh
npm ci --ignore-scripts
npm test                     # Python + Bun, offline
npm run typecheck
npm run test:host -- --run   # real Herdr + real Pi, no inference
```

Paid live scenarios need an explicit model and budget; see [testing](docs/en/TESTING.md).

`private: true` prevents accidental npm publication. The project license is still an owner decision; see [LICENSE](LICENSE).
