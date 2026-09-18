# Shop workbench

[简体中文](../zh-CN/WORKBENCH.md) · [README](../../README.md)

Entry: `/shop-ui` in Herdr Pi. Requires a registered Shop. Viewing does not dispatch, focus or close panes; business changes also require an explicitly bound run.
Choose interface language with `/shop-language`: see [language](LANGUAGE.md).

## Seven areas

| Area | Behavior | What it does not prove/do |
| --- | --- | --- |
| Status and identity | Shared `shop.snapshot/v1`, members/tickets/attempts/checkpoints, requests, configuration and endpoint advertisements | Idle is not completion; an endpoint advertisement is not proof of broker connectivity |
| Handoffs | Bounded objective/scope/acceptance/evidence; exact-recipient business receipts via `shop_handoff` or inbox | delivered/injected is not accepted; handoff deliver is not ticket accept |
| Development preparation | Pin SHA, clean state, branch, path and worktree list; confirm before creating a new branch/worktree | No member launch, ticket creation or commit of dirty changes |
| Delivery and integration | Read current-attempt results; preview/confirm `merge --ff-only`; record final-check evidence | No automatic push, unbind, finish or resource release |
| Idle-seat configuration | Request receiving Pi to confirm locally and apply through public APIs | Sender requested is not recipient applied; no restart or session-file edit |
| Task intervention | Distinguish supplement / scope-change / pause / cancel | Receipt, Esc delivery, actual stoppage and ticket cancellation are separate states |
| Diagnostics and redacted export | Read-only report; separate confirmation writes a local allowlisted JSON | No upload, repair, raw messages/prompts/environment/paths/identity IDs |

## Messages and handoffs

Actual sending uses Pi tools `shop_message`, `shop_dispatch` or the workbench.
The `herdr-shop message/dispatch` CLI **only prepares business records**, returning `transport_request`; it never invokes Herdr prompt.
Prepared is not sent. Do not manually replay the returned envelope.

`shop_dispatch` pins assignment/attempt, creates a handoff and then sends through the calling Pi's built-in transport.
The recipient checks the pinned ticket before `shop_handoff({id, transition: "accept"})`; use `needs_context` or `reject` when appropriate.
Publishing a result and primary Lead's ticket acceptance remain separate steps.

Transport records submission and receipt separately. Missing receipts, timeouts and crash windows remain unknown, with no automatic resend.
Durable deduplication applies to messages, but light notes do not create tickets. Stale launch/session/attempt or mismatched wire/body identity is rejected.
Pi lifecycle and five-second membership checks only connect/refresh endpoints; they do not poll for dispatch, wake models or retry messages.
Registration does not persist transport epochs: null means unknown. Short-lived endpoint advertisements and broker checks supply runtime epochs.

## Development and delivery boundaries

The wizard creates only a **new** branch and a non-existing isolated worktree. It neither takes over directories nor nests them inside another worktree/registered cwd.
Plans expire after five minutes. Execution rereads HEAD, branch, clean state and worktree list; changes require another preview.
Creation failure retains directories, branches and unknown records without cleanup. Then explicitly use existing `add-worker/add-lead --cwd`, ticket creation and `shop_dispatch` procedures.

Integration requires an accepted result, other tickets accepted/cancelled, a clean target, and target HEAD an ancestor of the chosen result commit.
The UI additionally requires personal verification that all foreground/background writers stopped; core rechecks other registered members idle/done and no assigned/review tickets.
**A user attestation is not automatic Herdr process proof.** Its protocol lacks background-process visibility. If uncertain, inspect plans only.
No automatic cherry-pick, conflict handling, rollback or batch integration; failure preserves partial state.

Final checks record a user-supplied command, exit code, target commit, evidence path and SHA-256.
The plugin does not run the check or label it independently verified. HEAD/evidence changes set `current=false`.
Run completion still follows SUMMARY/REVIEW → explicit unbind → shop-run finish. Window closure uses its separate shutdown preflight.

## Single-seat model requests

1. Architect/primary Lead selects a non-Architect seat, model and explicit thinking level.
2. Target must be idle/done with no assigned/review tickets; sender/recipient launch, terminal and Pi session must still match.
3. Request is recorded as requested; the receiving user confirms in their own `/shop-ui` inbox.
4. Receiving Pi rechecks provider/model/thinking capability, then claim → public API → applied/rejected/unknown.
5. Applying/unknown blocks dispatch to that seat. Unknown outcomes require inspection of native current configuration and explicit reconciliation; never automatic retry.

Apply to the current session only, or also update this Shop's target-seat `model_profiles` for future launches.
Do not change `/shop-config` layers or overwrite historical `launch_profile`.
Runtime requests need explicit thinking; Pi default/null remains a startup-configuration option only.
Architect uses native `/model` and `/thinking`, never a remote profile request.

## Scope, pause and cancellation

- **supplement**: built-in note; explicit busy-member steering allowed. No scope/acceptance change.
- **scope-change**: records revision and affected ticket/attempt. Lead acknowledgement activates it; old ready tickets require explicit revise before dispatch, assigned tickets remain unchanged.
- **pause**: starts requested. Lead may explicitly send Esc; result is only `pause_requested`, not stopped or redispatched.
- **cancel**: explicit Lead execution, stopped-writer attestation and original-writer identity/status rechecks. Unresolved dependencies refuse cancellation; code/checkpoint/result remain.
- **retry**: existing `--writer-stopped` and attempt fences still apply. No new automatic retry path.

## Validation scope

Offline tests cover business state machines, real temporary Git repositories, fake Herdr identity, local broker and Pi UI/API substitutes.
Real TUI, provider/model, shortcuts, session changes, expansion/recovery and background-writer safety require separate controlled validation.
Tests do not prove actual AI understanding or collaboration quality. Validate the real provider/Pi/Herdr combination in isolation before replacing an active installation.
