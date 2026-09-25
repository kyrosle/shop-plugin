# Testing

[中文](../zh-CN/TESTING.md) · [README](../../README.md)

Three levels, from free to paid:

| Level | Command | Proves |
| --- | --- | --- |
| Offline | `npm test`, `npm run typecheck` | Configuration, handoff rules, curation-to-file, seat helpers, runner safety; no host, no model |
| Real host, no inference | `npm run test:host -- --run` | A real Pi in a real Herdr loads Shop; commands register; `/shop-config`, the model picker, language and `/reload` work |
| Real host, real provider | `python3 tests/host/run.py --run --scenario live-… ` | Full seat runs with real models, with correctness checks and cost/time metrics |

Prerequisites: macOS; Herdr ≥ 0.9.0 (tested 0.9.1), Pi, Node and Python 3 on PATH; `npm ci --ignore-scripts`. The runner never installs or upgrades host binaries. Without `--run` it prints a plan and starts nothing. Missing prerequisites are errors, not skips.

## Isolation

Each invocation creates a fresh private `/tmp/shop-host-*` directory with its own Herdr config, socket, headless server and PTYs; its own HOME, XDG and Pi agent directories, sessions and Shop config/state; and a throwaway Git project. The child environment is allowlisted: user keys, proxies and agent context are not copied. The runner refuses to adopt an existing server and never queries your other tabs.

Live runs let models execute commands, so seats load a **private copy of the checkout** under the test root (`node_modules` is symlinked); Shop paths never point into your working tree. This is configuration/process isolation, not an OS sandbox; run only trusted code.

The no-inference lane uses a local fixture provider (thirteen catalogue entries for the picker) and requires **zero provider calls**.

## Live scenarios (opt-in, paid)

```sh
python3 tests/host/run.py --run --scenario live-seats \
  --pi-bin "$(which pi)" \
  --live-model opencode-go/deepseek-v4.1-flash \
  --live-thinking architect=max,lead=high,worker=low \
  --live-budget-usd 0.30
```

| Scenario | Passes when |
| --- | --- |
| `live-seats` | `/shop-go <goal>` (or `--seats-flow two-step`: `/shop-spec`, then a confirmed `/shop-go`) delivers through a Worker, Architect answers with the fixture content, and every seat pane closes with its session kept |
| `live-fidelity` | Real Architect turns set a constraint and reject an alternative that fixed SPEC/PLAN omit; the agreed `report.json` must reach the repository. With `--handoff-mode brief` the expected result is an honest `blocked` report without output. A seat reading Pi session files fails the run |
| `live-parallel` | Two independent Worker results are correct and the Workers overlapped in time |
| `live-failure` | `--failure-case missing`: a missing input ends in `blocked` without fabricated files. `--failure-case lost`: a Worker pane closed mid-task is detected, and the Lead recovers or reports failure. Call counts stay under limits |
| `live-task` | A seeded module with a real bug and a feature request: the project suite, hidden tests held by the runner and the original tests all pass |

Options:

- `--handoff-mode auto|raw|curate|brief` forces the handoff form.
- `--fidelity-size large` reads ~90 KB of background notes first, landing in the middle handoff band.
- `--handoff-budget-tokens N` shrinks the receiver budget to force curation.
- `--live-seat-models architect=p/m,lead=p/m,worker=p/m` sets per-seat models; omitted seats use `--live-model`.
- `--repeat N` (max 20) runs N independent hosts and writes `shop-host-aggregate-*.json` (in `$TMPDIR`) with pass rate, failing steps and min/median/max of cost and time per seat.
- `npm run` prefers the repository's own Pi; pass `--pi-bin` when the live model needs a newer Pi model catalogue.

Credentials and budget:

- Only the chosen providers' **API-key** entries are copied from `~/.pi/agent/auth.json`. OAuth is refused, because a test refresh could rotate your own login.
- The copy is deleted after every run, whether it passed or failed, and reports never contain it.
- Every assistant message's usage is recorded. Curator analyzer calls are metered as their own `curator` seat. Exceeding `--live-budget-usd` (default 0.30, max 5) or `--live-max-calls` (default 80) aborts the run.

## Evidence and cleanup

Every run prints `Artifacts: <private directory>`. `report.json` holds per-step results, versions, live usage, per-seat baseline metrics and scenario verdicts. The run's `project/.shop/seats/` holds SPEC, PLAN, seat records, prompts, sessions and reports. Artifacts contain machine paths and terminal text; inspect them before sharing.

Failures and interrupts still clean up owned resources: the runner stops only the server it started and verifies that its Pi processes exited. To stop a background run, send SIGTERM to the exact Python runner PID so cleanup and the credential scrub run. Artifacts are kept; delete only the exact directory after inspection.

## Automation

`npm test` runs offline in ordinary CI. The manual **Real host gate** workflow runs the no-inference lane on a dedicated self-hosted macOS runner (`shop-host-tests`). Live scenarios are never run in CI.
