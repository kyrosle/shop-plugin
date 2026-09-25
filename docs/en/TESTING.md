# Real-host regression gate

[中文](../zh-CN/TESTING.md)

Offline Python/Bun tests remain necessary. They do not prove that Herdr's actual responses, Pi's TUI or the plugin lifecycle work together. `tests/host/run.py` adds an opt-in, repeatable **real Herdr + real Pi** smoke gate. It is not yet a full ticket-delivery end-to-end suite.

## Run from a source checkout

Prerequisites: macOS, Herdr **>= 0.9.0** (tested on 0.9.1), Pi, Node and Python 3 already installed on PATH; repository dependencies installed with `npm ci --ignore-scripts`. The runner never installs or upgrades host binaries. Herdr compatibility is decided by the adapter's protocol/schema probe; the actual Pi version is recorded and exercised rather than assumed compatible.

```sh
npm run test:host                       # plan/preflight only; starts nothing
npm run test:host -- --run              # all scenarios; actual isolated processes
npm run test:host -- --run --scenario startup
npm run test:host -- --run --scenario reset
npm run test:host -- --run --scenario lifecycle
```

Only `--run` authorizes process launch. Missing prerequisites are errors, not silent skips. `startup` and `reset` are diagnostic subsets, **not release acceptance**. The default `all` must pass before claiming that the host lifecycle is verified.

## Isolation and authority

Each invocation creates a fresh private `/tmp/shop-host-*` directory (canonicalized to `/private/tmp` on macOS), with its own:

- Herdr config, Unix socket, headless server, plugin registry and PTYs;
- HOME, XDG directories, Pi agent directory, empty auth file and member sessions;
- Shop bridge, model/language configuration and state;
- throwaway Git project, fixture extension and logs.

The child environment is allowlisted. User keys, proxies, agent context, shell startup overrides and existing Shop/Herdr locators are not copied. A no-network fixture provider supplies thirteen catalog entries for TUI checks. Its local response implementation never calls a remote endpoint; these scenarios require **zero provider invocations**, including fixture invocations. Native inference tools and unrelated extensions/skills/context files are disabled.

The server endpoint must not exist and must report not running before launch. Fresh server workspace/plugin inventories must be empty. The runner never adopts an existing server or queries your other tabs. Fault injection changes one successful **real pane-rename response**, after recording the original, to reproduce a successful write followed by adapter parsing failure. It does not fake successful host operations or relax shutdown safety.

This is config/process isolation, **not an OS security sandbox**. Run only trusted checkout code. No user installation, model credentials, live workstation registration or business repository is copied or changed.

## Coverage and limits

| Stage | Checks |
| --- | --- |
| Startup | Native plugin link; actual Pi TUI and Shop command registration; private endpoint and fixture model |
| Configuration | Cancel leaves config unchanged; scope switching; 13-model catalog, bounded scrolling, search; confirmed model save; actual Lead launch uses that profile |
| Architect preservation | Shop saves leave Pi defaults untouched; reload retains Architect session and model |
| Language/reload | Native slash commands persist language in private config; reload lifecycle fires |
| Reset | Real mutation + deliberately invalid reply yields partial registration; native `/shop-reset` cancel preserves bytes; confirmation archives exact bytes and retains pane/config |
| Setup | Native Herdr plugin action starts real Lead/Worker Pi processes; registered/live identities and separate sessions match |
| Shutdown refusal | Start a real bounded child in a separate POSIX session without a tty; idle Pi must still be blocked, with registration and all members retained |
| Shutdown success | Stop only the fixture child, then require a ready plan, execution pane/process exit, preserved Architect and receipt; repeated close must leave registration, receipt and topology unchanged |
| Cleanup | Only directly owned test server is terminated; observed test Pi process exit is verified; logs stay available |

**Shutdown evidence:** Herdr protocol 22 still lacks a background list. Shop combines native identity/foreground facts with the local Unix socket's kernel peer PID and two stable OS metadata snapshots. Only the verified direct Pi process is exempted; additional descendants, session/tty/group processes block. Changed process incarnations invalidate the plan. After close, original shell/Pi exit must be verified too. No full command arguments or environment variables are queried. `background-blocked-plan.json` and `shutdown-plan.json` preserve negative and positive evidence. Never inject `background_proven=True` or bypass missing evidence to make the lane pass.

This proves only currently observable **pane-scoped** quiescence, not absence of every repository writer. A service that has fully daemonized, been reparented and escaped the pane's session/tty/tree is outside this scope. Snapshot checks are not an atomic process freeze. Shutdown never certifies such external services stopped, finishes a run or replaces explicit writer verification for worktree integration. Remote/unverified peers and unreadable/unstable metadata remain fail-closed. The local snapshot fallback is currently enabled only on macOS. Linux must remain `background_state_unknown` until its proc visibility/namespace behavior and a real-host lane are validated.

The current native CLI rejects a repeated close after registration removal (nonzero exit), rather than returning `already_closed`. The report records this outcome explicitly; the repeat check proves non-mutation only, not a successful acknowledgement.

Bound-run broker traffic, dispatch → acceptance → delivery, worktree integration, real-model collaboration quality and visual screenshot comparison are **not covered yet**. This lane is the host-lifecycle foundation, not evidence of a complete model collaboration loop.

### Offline lifecycle and production-client checks

`tests/test_lifecycle.py` exercises real core/adapter/candidate-file logic with substituted native replies and OS process metadata: session/lease/process-incarnation fencing, moved/busy members, unused orphan archival, private exact-byte backups, backup failure and changed preimages. `test_setup_adapter.py` also covers safe archival of an early Architect-only failure; unknown split/start outcomes retain partial evidence. Workbench business tests substitute the verified-owner gate; they do not independently prove live lifecycle ownership.

Bun command tests assert no model request on failed preflight or session drift. `transport_harness.test.ts` connects the **production ShopTransportClient** to a real isolated broker and exchanges a message/receipt without Pi, Herdr or model calls. This catches missing client hello writes that raw socket tests cannot catch. Socket delivery/injection is not business acceptance. These are offline checks, not real-host acceptance of session replacement, orphan cleanup or Grok RPC process ancestry; those need separately authorized host runs.

## Live provider lane (opt-in, paid)

The no-inference scenarios above never use credentials. `live-smoke` and `live-delegation` are the only paid path and need an explicit model:

```sh
python3 tests/host/run.py --run --scenario live-smoke \
  --pi-bin "$(which pi)" \
  --live-model opencode-go/deepseek-v4.1-flash \
  --live-thinking architect=max,lead=high,worker=low \
  --live-budget-usd 0.30
```

- `live-smoke`: Architect, Lead and Worker each complete one real turn; models and thinking levels are checked per seat.
- `live-delegation`: additionally sends `/shop` with a small analysis task that must go through the Worker, and waits (`--live-timeout`, default 900 s) until a Worker ticket is `accepted` and the run has `SUMMARY.md`.
- Only the chosen provider's **API-key** entry is copied from `~/.pi/agent/auth.json`; OAuth credentials are refused because a test refresh could rotate the user's login. The copy is deleted after the run, success or failure; reports never contain it.
- Every assistant message's usage is recorded in `live-usage.jsonl`. Exceeding `--live-budget-usd` (default 0.30, max 5) or `--live-max-calls` (default 80) aborts the run and stops the owned server.
- Live seats run with Pi's built-in tools enabled, so models can execute commands. They load a private copy of the checkout under the test root (`node_modules` is symlinked), so Shop paths never point into your working tree. Isolation is still configuration/process isolation, not an OS sandbox.
- `npm run` prefers the repository's Pi copy; pass `--pi-bin` when the live model needs a newer Pi registry.

## Evidence and cleanup

Every run prints `Artifacts: <private directory>`. `report.json` contains per-stage outcomes, versions, coverage boundary, provider-call count and cleanup result. Additional evidence includes `commands.jsonl`, `server.log`, member observations, private native plugin logs, reset archives and shutdown plans. Artifacts contain machine paths and terminal text; inspect before sharing.

Failures and interrupts still attempt owned-resource cleanup. No global `herdr server stop`, broad `pkill`, implicit reset or operation retry is used. A cleanup failure also fails the gate; inspect the reported owned server PID, private socket and remaining fixture PIDs before any manual intervention. SIGKILL/machine failure cannot guarantee cleanup. Artifacts are deliberately retained; remove only the exact directory from that invocation after inspection.

## Automation

`npm test` includes offline runner safety tests and still starts no real host. The separate **Real host gate** GitHub workflow is manual (`workflow_dispatch`), uses a dedicated self-hosted macOS runner labeled `shop-host-tests`, and requires host binaries already installed. It runs the full gate, propagates its exit code and uploads only that invocation's artifacts, even on failure. Configure a disposable/trusted runner; never execute untrusted PR code there.

Ordinary push/PR CI remains offline. An unconfigured or unexecuted real-host workflow means **NOT_RUN**, not passed. Full real-host acceptance requires both the actual background refusal and idle-close scenarios, not merely green offline CI.
