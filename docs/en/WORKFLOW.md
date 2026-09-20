# Shop task workflow

[简体中文](../zh-CN/WORKFLOW.md) · [README](../../README.md)

Commands are deterministic wrappers around files/Herdr, not an async agent framework. JSON is authoritative; Markdown supplies spec, plan, human-readable status/review. Tools cannot prove semantic completion, prevent arbitrary shell writes, or atomically freeze a human typing in another pane.

Reply language follows the user's request, not Shop display language. Do not automatically translate source code, paths or evidence.

## 1. Fixed workstation/run binding

```bash
<package>/bin/shop-run new --independent "任务标题"
# Open workstation with Ctrl+B,U. Then explicitly bind the printed ID:
<package>/bin/herdr-shop bind <run-id>
<package>/bin/herdr-shop status
```

Creation still only arranges seats; it does not automatically select a run. Opening seats does not activate delegation. Only `/shop <task>` activates Architect role for that request; ordinary messages remain local single-agent work, including code edits/tests, with existing-writer conflict checks. `/shop` requires an idle caller and ready current Architect registration; otherwise it reports no submission. It never automatically opens another workstation. Subsequent ordinary messages do not forward to Lead; use `/shop 补充：...` for updates. Cross-project access is not authorization to create another Shop or take over another Pi. Reuse an existing binding only for its task. For a clear new task in an unbound shop, use `new --independent`: it creates a separate run without reading/changing current.json or old runs/bindings. Bind the returned created ID, not current. An unrelated current pointer is not a blocker and needs no forced reuse or Architect-only workaround. Clarify only when changing an already-bound shop or actual task intent is ambiguous. Primary Lead/Architect can bind only a managed active/blocked run. A run has one workstation owner; workstation has at most one run. Other tabs changing current.json never retarget existing assignments. `.shop/bindings.json` is a deletion guard; runtime state contains run_id and unique shop_id. Mismatched/stale bindings fail closed, requiring inspection, not automatic takeover. Repository run lock serializes bind/finish/GC/contracts; shop lock serializes local pane operations.

Legacy open workstations lacking shop_id must be reset/reopened first. Existing unmanaged Markdown runs are not silently converted or considered completed. Main repository must always be passed using `shop-run --repo /absolute/main/repo` from auxiliary worktrees.

## 2. Reproducible development baseline

Development ticket requires exact 40/64-character base SHA, worktree root in same Git common repository, clean worktree (excluding .shop coordination files), HEAD equal to base, nonempty checks. Validate on ticket creation AND dispatch. Script never auto-commits/checks out/copies dirty code. Human must approve how to preserve dirty work first. Safe fallback is analysis, not silently developing against old HEAD. Worktrees are never auto-deleted.

Analysis may explicitly read current dirty source from main repository while process runs in detached worktree. That exception does not apply to development. API dispatch prevents two development tickets from owning same worktree simultaneously. Direct human edits or raw shell writes remain outside this guard.

## 3. Sessions and checkpoints

New Sol/DS launches now save fresh sessions under `<state_dir>/runtime/sessions/<shop-id>/<member-name>-<launch-id>/` via `--session-dir`. No --continue, --fork, --resume or parent transcript replay. Closing pane does not delete session. Bound run gets session-refs/*.json; actual sessions remain machine-local. Previous --no-session instances cannot be retroactively recovered.

Worker publishes checkpoint at each meaningful phase / before long work. It is one atomically replaced checkpoint per attempt, not token-by-token logging. Publishing is explicit, no background timer. On crash, latest saved checkpoint is available; unsaved last segment can still be lost. Pi-native session recovery is optional manual rescue, not normal handoff. Session directories are deliberately NOT deleted by gc: they may be referenced by multiple runs if seats were rebound. After verification, inspect refs and active agents before explicit manual session cleanup.

## 4. Tickets, result validation, retries and single dispatcher

Only primary Lead can mutate ticket coordination via CLI. Only exact assigned occupant (name/pane/terminal ID) can publish result/checkpoint. Herdr identity checks are orchestration guardrails, not OS security permissions. Do not bypass by editing authoritative JSON manually.

Create a JSON draft in run evidence/ or a temporary file, e.g. analysis:

```json
{
  "ticket_id": "T001",
  "owner": "<name from herdr-shop status>",
  "objective": "Analyze current project entry points",
  "kind": "analysis",
  "worktree": "/absolute/source/repo",
  "base_commit": null,
  "scope": ["apps/desktop/src", "package.json"],
  "checks": [],
  "depends_on": []
}
```

Development sets kind=development, exact base_commit, dedicated worktree and required checks such as `bun run test`. Scope is literal relative files/directories, not glob patterns. Changes outside scope are refused by result validation. Analysis cannot report changed code files. Ticket dependencies must exist before dependent ticket creation (prevents cycles through normal API).

From primary Lead:

```bash
shop-run --repo <main> ticket new <run> --file <draft.json>
# Pi tool (not shell): shop_dispatch({"ticket":"T001"})
# herdr-shop dispatch T001 only prepares a record; it does NOT send.
```

Dispatch checks binding, dependency acceptance, registered idle/done owner, unique assignment and baseline. It stores assigned/attempt/occupant identity and prepares a handoff BEFORE the calling Pi sends it through built-in transport. The CLI only prepares records; use the `shop_dispatch` Pi tool for delivery. Transport submission/delivery is not business acceptance or completion. Unknown outcomes require inspection, never blind retry or double notification. Primary Lead may use Herdr wait/get for lifecycle, but reads result files for acceptance. Human intervention in a Worker must be reported to Lead.

Worker draft checkpoint:

```json
{
  "run_id": "<run>", "ticket_id": "T001", "attempt": 1,
  "owner": "<assigned-name>", "base_commit": null,
  "progress": "Read entry points", "next_steps": ["Inspect IPC"]
}
```

```bash
shop-run --repo <main> ticket checkpoint <run> T001 --file <checkpoint-draft.json>
```

Worker result:

```json
{
  "run_id": "<run>", "ticket_id": "T001", "attempt": 1,
  "owner": "<assigned-name>", "base_commit": null,
  "status": "completed", "summary": "Entry points verified; see report",
  "result_commit": null,
  "changed_files": [], "checks": [], "remaining_work": []
}
```

For each executed check: `{ "command": "exact ticket command", "exit_code": 0, "evidence": "/absolute/existing/output-file" }`. Completed development results must cover all required commands, all checks succeed, no remaining_work. result_commit is null when no commit (not auto-commit permission); if provided for completed development it must resolve to commit. Blocked/failed results may report missing/failed checks. Evidence existence/schema do NOT prove genuine tests: Lead independently reviews/reruns. Never use line count as quality gate.

```bash
# Assigned Worker/auxiliary Sol:
shop-run --repo <main> ticket publish <run> T001 --file <result-draft.json>
# Primary Lead after independent review:
shop-run --repo <main> ticket accept <run> T001 --file /absolute/review.md
```

Publish atomically writes immutable T001.a1.result.json then marks ticket review. Interrupted second write is a reconciliation case, never automatic overwrite. States: ready -> assigned -> review -> accepted; blocked/failed are result outcomes awaiting Lead. To abandon use cancel with stopped-writer attestation.

```bash
shop-run --repo <main> ticket retry <run> T001 --writer-stopped
shop-run --repo <main> ticket cancel <run> T001 --writer-stopped
```

Retry requires checking old writer is stopped (live busy/blocked/unknown or moved agent is rejected). It archives old ticket and increments attempt; old result remains immutable, late writes rejected. Retry never sends a prompt; explicit dispatch is separate. Development retry must satisfy baseline again; preserve partial changes and ask human rather than auto-resetting them.

## Primary Lead supervision and dynamic +/- members

Primary Lead loops during its active task using:

```bash
herdr-shop patrol --seconds 60
```

`patrol` defaults to immediate snapshot; seconds is bounded 0..120. It refreshes runtime membership and live agent state roughly every 5 seconds, returning on changes, review/inspection hints, or deadline. No shop/repo lock is held while waiting: result publication, checkpoints, and membership operations remain possible. It is a foreground finite wait, NOT a service or recurring model invocation. Lead must call again after reviewing report. Each Herdr API subprocess has its normal CLI behavior; the window is not a process-kill deadline. API failures become uncertainty, never death declarations.

Snapshot includes current auxiliary Sol/DS, ticket status/attempt/delivery, bounded checkpoint progress and age, dependency readiness, per-role capacity (max2), and idle auxiliary removal candidates. Missing/old checkpoint (>180s) prompts inspection, not automatic intervention. Lead reads ~60 recent-unwrapped lines only when needed. Output is for diagnosis, not a replacement for result files.

Dynamic policy: reuse idle members first; independent ready work + busy members may justify add-lead/add-worker with clean independent worktree. New members appear in next patrol; removed members disappear. Additional Sol is an executor/reviewer, not another scheduler. Accept/cancel all owned tickets and confirm no dependents before removal; avoid churn when more work is queued. Addition requires Architect/primary Lead caller, not arbitrary Worker. Geometry and per-role caps still apply.

```bash
# Only primary Lead; records reason and sends Esc, never auto-kills/closes/reassigns:
herdr-shop pause <registered-executor-name> --reason "Observed wrong source directory ..."
```

Pause requires matching working Pi; blocked/unknown must be inspected manually. Result explicitly says Esc sent, NOT writer stopped. Check lifecycle plus foreground/background jobs before handoff. Do not discard modified files. Then use existing retry --writer-stopped to increase attempt. Primary Lead may change only a ready ticket with:

```bash
shop-run --repo <main> ticket revise <run> <ticket> --file <patch.json>
```

Patch may contain owner/objective/worktree/scope/checks/base_commit; no run/id/attempt/dependency mutation. Development baseline checks still apply. This supports transferring narrowed work to new Sol/DS, but never transfers live assignments. After revise, dispatch explicitly. Old attempts/results remain. If a member is removed before more work arrives, new tickets can use its reused logical name; dispatched occupant and attempt checks still apply.

Lead ending its Pi turn means no further patrol occurs. User cancellation, API failure or Lead crash must be reported/recovered; no promise of invisible background supervision. Architect waits for Lead's high-level result and significant blockers, not every Worker's screen.

## Architect wait contract

Default is synchronous file handoff: Architect sends the run path and objective to primary Lead using `shop_message`, waits through Herdr lifecycle tools, then reads SUMMARY.md/REVIEW.md before the final user summary. Waiting on Herdr is not a continuous model-call loop. Timeout means pending, not completed: get status, continue agent wait if working; never resend original task blindly. blocked/inspection failure must be reported accurately; idle/done without deliverables requires checking/continuation, not a success claim. Only explicit user request for background/non-waiting execution allows an immediate final acknowledgment, and that mode has no automatic Architect wake guarantee. Do not promise a later summary unless actually waiting or an explicit notification mechanism exists.

## 5. Explicit recovery, not permanent scheduler

```bash
herdr-shop resume
```

Writes run RECOVERY.json containing live panes/processes, ticket states and checkpoint paths; zero starts, zero prompts, zero reassignments. Failure to inspect is recorded as uncertainty, not treated as death. Review uncertain dispatch and old writers before deciding retry.

If primary Lead crashed and original pane is now a shell:

```bash
# Architect only
herdr-shop recover-lead --dry-run
herdr-shop recover-lead --apply
```

Requires original Lead pane still exists in same tab, no detected agent, foreground shell only; refuses running/replaced agent. Starts fresh primary Sol in place and sends RECOVERY.json path with instruction to report recovery plan only. Does not close Worker, replay conversations or automatically resume tickets. Closed/moved Lead pane and stale server IDs require manual repair; this command intentionally does not guess replacements.

## Teardown and retention

Auxiliary removal refuses members with any unaccepted/uncancelled ticket. Accept/cancel before closing. Final sequence:

1. Save SUMMARY.md/REVIEW.md; all tickets accepted/cancelled.
2. Stop other writers; `herdr-shop unbind --handoff-complete`.
3. `shop-run finish <run> --handoff-complete`.
4. Ctrl+B, Shift+U may now close idle seats.

Finish, GC and delete refuse bound runs. Reset refuses bound workstation. A binding registry left after crash is intentionally conservative; inspect state and processes before manual reconciliation. Current.json is convenience for humans only, not assignment routing. Existing gc remains preview-first, latest 5 plus 7-day protection, tickets/evidence only. Session refs/summaries persist; physical worktrees and sessions are separate explicit cleanup responsibilities.

## 6. User-action shutdown and recovery plan

Shutdown is a **user action**, not an agent capability. Shift+U runs the
`shop.workstation` close action in an independent `core/plugin.py` process; that
process previews a bounded plan and executes it only with Herdr plugin-action
context. Agents may run read-only previews (`herdr-shop shutdown --preview`,
`herdr-shop recovery`) for diagnosis; they cannot execute a close.

Order of operations (unchanged from §Teardown): settle every ticket → explicit
unbind → user shutdown. A bound run or registry binding blocks shutdown with
`bound_run`/`outstanding_ticket` blockers; shutdown never unbinds, finishes a
run, or accepts a ticket.

Preview is always non-destructive: it resolves the exact socket/tab
registration, checks phase, binding, member identity (member/launch/terminal),
live status, managed-tab layout, unregistered panes, process facts, transport
and handoff uncertainty, and writes a plan (`plan_id`, state digest, close
order, expiry, `0600`) under `<STATE>/shutdown/plans/<shop>/`. Execution
re-runs every check, refuses on any drift, then journals archive →
`shutdown_closing` → per-target verified absence → final verification →
receipt/tombstone → registration removal. The invoking execution pane is closed
last; the Architect is retained and verified.

Fail-closed blockers include: bound run/outstanding tickets, phase
`partial|removing|resetting|shutdown_*`, active/blocked/unknown members, missing/
moved/replaced/duplicate/identity-mismatched members, an unverified Architect,
unregistered panes in the managed tab, unreadable/oversized/invalid state,
stale or expired plans, transport `unknown`/`pending`, open handoffs, foreground
work, observed `background_work`, unavailable process facts, and
`background_state_unknown`. Herdr protocol 22 lacks a background list; local
Unix peer identity and two stable macOS metadata snapshots supplement it for the
pane's descendants/session/tty/group. Missing or changing evidence blocks.
Only a verified direct Pi is exempted, not arbitrary Node processes. Process
incarnation changes invalidate plans; original shell/Pi exit is verified after
pane close. This does not certify fully detached external services or repository
writers stopped; worktree integration still requires its own writer checks. A failure stops as `shutdown_partial` with the remaining
roster and never notifies success. The core planner can return `already_closed`
only when the receipt matches and the closed panes are verified absent;
otherwise it returns `unknown`. The native CLI currently rejects a missing
registration before reaching this receipt check; repeated close does not mutate
state but is not yet a successful `already_closed` acknowledgement.

Recovery: `herdr-shop recovery` (and the `recovery` plugin action) produces one
read-only reconciliation plan for partial, moved, server-restart and
mixed-survivor layouts. It classifies each member and lists display-only
actions; it never restores, reassigns, replays, closes or deletes. Applying a
restore now requires a matching, non-stale recovery plan and stays an explicit
Architect action with its own dry-run and archive. Cutover/rollback rules:
[Migration](MIGRATION.md).
