"""Bounded foreground supervision: snapshots and explicit pause, never auto-scale/kill."""
import datetime as dt
from pathlib import Path
import time
import contracts as ct
import coordination as co

import events as events_module
import identity
import snapshot as snapshot_module


EXECUTION_ROLES = ('auxiliary_lead', 'worker')


def snapshot(api, state, facts=None):
    """Bounded read-only view for patrol: execution members only.

    The shape and every patrol-relevant path come from the single snapshot
    producer (`core/snapshot.py`), so status, patrol, the Pi commands and the
    plugin action can never drift apart again.
    """
    co.binding(state)
    tickets = ct.tickets(state['cwd'], state['run_id'])
    return snapshot_module.build(state, tickets, api=api, repo=state['cwd'], run_id=state['run_id'],
                                 roles=EXECUTION_ROLES, scope_label='execution-members', facts=facts)


def patrol(api, state_path, caller, seconds=0):
    if not 0 <= seconds <= 120:
        raise RuntimeError('--seconds must be 0..120')
    # No shop/repo locks held while waiting: checkpoints and dynamic membership stay writable.
    initial = ct.load(state_path)
    co.caller_control(api, initial, caller, primary_only=True)
    co.binding(initial)
    identity = (initial['shop_id'], initial['run_id'])
    deadline = time.monotonic() + seconds
    first = None
    while True:
        state = ct.load(state_path)  # Refresh membership every iteration, no captured stale list.
        if (state.get('shop_id'), state.get('run_id')) != identity:
            raise RuntimeError('Shop/run changed during patrol; stop and inspect')
        if state.get('phase') != 'ready':
            return {'reason': 'membership_operation_in_progress', 'phase': state.get('phase'), 'run_id': identity[1]}
        # Event facts annotate the view; the live read inside snapshot() stays authoritative.
        report = snapshot(api, state, facts=events_module.read_facts_for_state(state_path))
        # Ignore checkpoint age alone when deciding whether an event occurred.
        signature = repr([(r['name'], r['pane'], r['status'], [(t['ticket_id'], t['attempt'], t['status'],
                      t['delivery'], t.get('checkpoint', {}).get('progress'), t['inspect_output']) for t in r['tickets']])
                      for r in report['members']])
        if first is None:
            first = signature
        needs_attention = any(t['inspect_output'] or t['status'] == 'review' for r in report['members'] for t in r['tickets'])
        if seconds == 0 or signature != first or needs_attention or time.monotonic() >= deadline:
            report['reason'] = 'attention' if needs_attention else 'changed' if signature != first else 'snapshot' if seconds == 0 else 'interval_elapsed'
            return report
        time.sleep(min(5, max(0, deadline - time.monotonic())))


def pause(api, state, caller, target, reason):
    co.caller_control(api, state, caller, primary_only=True)
    co.binding(state)
    if not reason or not reason.strip():
        raise RuntimeError('--reason required (observed evidence, not merely slow output)')
    item = next((m for m in [*state.get('extra_leads', []), *state['workers']] if m['name'] == target), None)
    if not item or item['pane'] == caller['pane_id']:
        raise RuntimeError('Only registered execution members may be paused, never self/Architect/primary Lead')
    # Fail closed: only a working occupant with matching name/pane/terminal/status is pausable.
    identity.resolve(api, item, state['tab'], allowed_status=('working',),
                     where='pause target ' + str(target))
    audit = {'target': target, 'pane': item['pane'], 'reason': reason, 'requested_at': ct.stamp(), 'status': 'request_pending'}
    # One record per member, not indefinite logs; authoritative ticket/checkpoint history remains separate.
    p = ct.run_path(state['cwd'], state['run_id']) / 'evidence' / (ct.ident(target) + '.pause.json')
    ct.atomic(p, audit)
    try:
        api('agent', 'send-keys', target, 'esc')
        audit['status'] = 'esc_sent_not_confirmed_stopped'
    except Exception as e:
        audit.update(status='uncertain', error=str(e))
        ct.atomic(p, audit)
        raise
    ct.atomic(p, audit)
    return dict(audit, evidence=str(p), next='Check agent state AND shell/background jobs before retry; no reassignment, reset or close performed.')
