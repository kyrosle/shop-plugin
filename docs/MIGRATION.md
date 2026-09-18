# Legacy migration policy

This alpha does not migrate or replace installed workstation scripts.
Keep legacy shortcuts and Pi extension enabled until choosing a controlled cutover.
Never enable both old and new mode extensions for same Pi session.

Before cutover:
1. Inventory legacy shops, active bindings, outstanding tickets and surviving processes.
2. Finish or explicitly block tasks; verify writers stopped. Do not infer stop from closed pane.
3. Back up legacy config/runtime/roles and Herdr keybinding config outside Git repository.
4. Verify new package tests and doctor in isolated state directory.
5. Configure new bridge and model IDs. Disable old mode extension, install new package and reload target Pi.
6. Replace only old U / Shift+U entries with plugin_action entries. Preserve unrelated keybindings.
7. Start a fresh test shop. Do not copy old ready registrations into new runtime.

Rollback: stop new work safely; restore previous keybinding/extension configuration and reload Pi.
Keep both generations of state/session files for evidence. Never reset/stash/delete project worktrees.
Moving an active run or sessions between generations requires a future explicit migration command, not manual JSON edits.

Package removal/unlink only removes registration/code. Retained project `.shop`, bridge/config/state and sessions
need separate user-authorized cleanup after preview.

## Packaged install, cutover and rollback checklist

Compatibility policy: package version and `herdr-plugin.toml` version must match
exactly (`0.1.0-alpha.1` today); the transport protocol is `pi-shop-transport`
v1/schema 1 and the snapshot schema is `shop.snapshot/v1`. A candidate whose
protocol is incompatible is refused, never silently downgraded or rewritten.

Before cutover:
- Inventory active shops/bindings/tickets/processes, Pi extension load sources,
  the Herdr plugin link, keybindings, the Shop transport broker/socket/lock and
  any original `pi-intercom` instance.
- Refuse cutover on an active run, an unknown writer or a partial operation;
  active-run migration is unsupported and must stay manual.
- Preview every config/file change and back up bridge, model/role config, Herdr
  keybindings, prior plugin references and version metadata outside the repo.
- Never modify installed `pi-intercom`, take over its socket, treat an unlink as
  a stopped process, or enable two Shop role extensions at once.
- Install the exact tested tag/commit into an isolated profile first, run
  `doctor`, `npm test`, `npm run typecheck` and `npm pack --dry-run` there.

Cutover:
- Disable the old extension explicitly, install the candidate, configure the
  bridge, replace only the conflicting U/Shift+U entries, reload the chosen Pi,
  then start a fresh unbound test shop.

Rollback:
- Stop new work safely; refuse while bound/active/unknown.
- Restore the backed-up plugin refs, keybindings, bridge and config, then reload
  explicitly.
- Confirm the candidate broker/process stopped through real socket/process
  evidence; uninstall is not proof.
- Restore only a protocol-compatible prior version; keep at least two
  generations of state/session/evidence and never delete worktrees or
  credentials.

Shutdown integration: `Shift+U` now runs the independent user-action shutdown
(`core/shutdown.py` via `core/plugin.py`). It refuses while a run is bound, so
the documented order stays: settle tickets → explicit unbind → user shutdown.
`core/shop.py shutdown --preview` is read-only diagnosis; execution needs Herdr
plugin-action context.
