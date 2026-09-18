#!/usr/bin/env python3
"""User-action safe shutdown: preview, revalidation, journaled execution, recovery.

Authority model: a Herdr *user action* (Shift+U on the plugin action)
is a separate authority from agent control. This module never relaxes
``coordination.caller_control`` and never impersonates Architect/Lead: it records
``authority="herdr_user_action"``, the action id and the invocation pane, and the
entry point refuses execution without plugin-action context. Same-user code is not
prevented by this check; it is an orchestration guard, not an OS security boundary.

Safety rules enforced here (fail closed):

* binding blocks: a bound run (or registry binding) is never unbound or finished;
* any active/unknown/blocked member, identity drift, missing/moved/replaced/duplicate
  member, extra or unregistered pane, unreadable/oversized state, stale plan or
  transport/handoff uncertainty blocks with zero closes;
* process facts must be available; if Herdr cannot prove background jobs stopped the
  plan reports ``background_state_unknown`` instead of claiming safe closure;
* every mutation is journaled (archive -> phase ``shutdown_closing`` -> per-target
  verified completion -> tombstone) and a failure leaves ``shutdown_partial``;
* nothing here deletes tickets, worktrees, sessions, credentials or unknown panes,
  and nothing kills a process or sends keys.
"""
import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import secrets

import contracts as ct
import herdr
import identity

PLAN_SCHEMA = 1
RECEIPT_SCHEMA = 1
RECOVERY_PLAN_SCHEMA = 1
PLAN_TTL_SECONDS = 180
RECOVERY_PLAN_TTL_SECONDS = 900
MAX_STATE_BYTES = 65536
AUTHORITY = 'herdr_user_action'
DECISIONS = ('ready', 'blocked', 'already_closed', 'recovery_required', 'unknown')
# Auxiliary leads first, then non-primary workers, primary worker, lead, caller last.
ROLE_ORDER = {'auxiliary_lead': 0, 'worker': 1, 'lead': 2}
UNMANAGED_PHASES = ('creating', 'partial', 'removing', 'resetting', 'restoring',
                    'shutdown_closing', 'shutdown_partial')


class ShutdownRefused(RuntimeError):
    """Fail-closed refusal: nothing was closed and nothing may be closed."""

    def __init__(self, code, detail):
        super().__init__(code + ': ' + detail)
        self.code = code
        self.detail = detail


def stamp(now=None):
    return (now or dt.datetime.now(dt.timezone.utc)).isoformat()


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def state_revision(state):
    """Stable digest of the mutable safety-relevant state fields."""
    relevant = {key: state.get(key) for key in
                ('shop_id', 'run_id', 'phase', 'tab', 'cwd', 'architect', 'lead', 'extra_leads', 'workers')}
    return hashlib.sha256(canonical(relevant).encode('utf-8')).hexdigest()


def shutdown_dir(state_dir):
    return Path(state_dir) / 'shutdown'


def plan_file(state_dir, shop_id, plan_id):
    safe_shop = _safe(shop_id)
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}', str(plan_id)):
        raise ShutdownRefused('plan_invalid', 'unsafe plan id')
    return shutdown_dir(state_dir) / 'plans' / safe_shop / (plan_id + '.json')


def archive_dir(state_dir, shop_id):
    return shutdown_dir(state_dir) / 'archives' / _safe(shop_id)


def recovery_dir(state_dir, shop_id):
    return shutdown_dir(state_dir) / 'recovery' / _safe(shop_id)


def recovery_plan_file(state_dir, shop_id, plan_id):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}', str(plan_id)):
        raise ShutdownRefused('plan_invalid', 'unsafe recovery plan id')
    return recovery_dir(state_dir, shop_id) / (plan_id + '.json')


def plan_digest(plan):
    """Integrity digest of a plan body (tamper detection inside the same OS user).

    This is not an authenticity boundary: any same-user process could recompute the
    digest. It exists so a hand-built or edited plan cannot silently pass the checks.
    """
    body = {key: value for key, value in plan.items() if key != 'plan_digest'}
    return hashlib.sha256(canonical(body).encode('utf-8')).hexdigest()


def verify_recovery_plan(state, plan, state_dir, now=None):
    """Enforce a persisted, digest-matching, non-expired, approving recovery plan."""
    now = now or dt.datetime.now(dt.timezone.utc)
    if not isinstance(plan, dict) or plan.get('schema') != RECOVERY_PLAN_SCHEMA:
        raise ShutdownRefused('plan_invalid', 'recovery plan schema missing or unsupported')
    plan_id = plan.get('plan_id')
    if not plan_id:
        raise ShutdownRefused('plan_invalid', 'recovery plan has no plan_id')
    if not plan.get('plan_digest'):
        raise ShutdownRefused('plan_invalid', 'recovery plan has no digest')
    # Existence first: a hand-built dict is simply not a plan this core produced.
    stored_path = recovery_plan_file(state_dir, plan.get('shop_id') or 'unknown', plan_id)
    if not stored_path.is_file():
        raise ShutdownRefused('plan_not_persisted',
                              'recovery plan was not produced by this core (no stored plan file)')
    if plan.get('plan_digest') != plan_digest(plan):
        raise ShutdownRefused('plan_tampered', 'recovery plan digest does not match its body')
    try:
        stored = json.loads(stored_path.read_text())
    except (OSError, ValueError) as error:
        raise ShutdownRefused('plan_unreadable', 'stored recovery plan unreadable: ' + str(error)[:200])
    if plan_digest(stored) != plan.get('plan_digest'):
        raise ShutdownRefused('plan_tampered', 'stored recovery plan differs from the supplied plan')
    if plan.get('shop_id') != state.get('shop_id'):
        raise ShutdownRefused('plan_mismatch', 'recovery plan belongs to another workstation')
    if plan.get('state_revision') != state_revision(state):
        raise ShutdownRefused('plan_stale', 'recovery plan was produced for a different state revision')
    try:
        expires = dt.datetime.fromisoformat(str(plan.get('expires_at')))
    except ValueError:
        raise ShutdownRefused('plan_invalid', 'recovery plan has no valid expiry')
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=dt.timezone.utc)
    if now > expires:
        raise ShutdownRefused('plan_expired', 'recovery plan expired at ' + str(plan.get('expires_at')))
    if plan.get('decision') not in ('recovery_required', 'no_action_required'):
        raise ShutdownRefused('plan_not_approving',
                              'recovery plan decision %r does not authorize an apply' % plan.get('decision'))
    return stored_path


def load_recovery_plan(state_dir, shop_id, plan_id):
    path = recovery_plan_file(state_dir, shop_id, plan_id)
    if not path.is_file():
        raise ShutdownRefused('plan_invalid', 'no stored recovery plan for id ' + str(plan_id))
    return json.loads(path.read_text())


def receipt_file(state_dir, shop_id):
    return shutdown_dir(state_dir) / 'receipts' / (_safe(shop_id) + '.json')


def _safe(value):
    text = re.sub(r'[^A-Za-z0-9_.-]', '-', str(value or 'unknown'))[:64]
    return text or 'unknown'


def read_state(state_path):
    """Bounded, validated state read. Failures are refusals, never 'closed'."""
    path = Path(state_path)
    try:
        if path.is_symlink():
            raise ShutdownRefused('state_unreadable', 'state file is a symlink')
        if not path.is_file():
            raise ShutdownRefused('state_absent', 'no registered shop state at ' + str(path))
        if path.stat().st_size > MAX_STATE_BYTES:
            raise ShutdownRefused('state_oversized', 'state file exceeds %d bytes' % MAX_STATE_BYTES)
        data = json.loads(path.read_text())
    except ShutdownRefused:
        raise
    except (OSError, ValueError) as error:
        raise ShutdownRefused('state_unreadable', 'state file unreadable: ' + str(error)[:200])
    if not isinstance(data, dict) or not data.get('shop_id') or not data.get('architect'):
        raise ShutdownRefused('state_invalid', 'state lacks shop_id/architect')
    if data.get('phase') not in ('ready',) + UNMANAGED_PHASES:
        raise ShutdownRefused('state_invalid', 'unknown phase ' + repr(data.get('phase')))
    return data


def process_facts(api, pane_id):
    """Process evidence for one pane. Background proof is explicit, never assumed."""
    try:
        info = herdr.process_info_record(api('pane', 'process-info', '--pane', pane_id)['process_info'],
                                         'shutdown process-info')
    except Exception as error:
        return {'pane_id': pane_id, 'available': False, 'error': str(error)[:200],
                'background_proven': False, 'extra_foreground': [], 'shell_pid': None}
    foreground = list(info.get('foreground_processes') or [])
    shell_pid = info.get('shell_pid')
    extra = [item for item in foreground if item.get('pid') != shell_pid]
    # Herdr protocol 22 exposes foreground processes only; there is no descendant or
    # background job list, so background proof cannot be derived and stays false.
    return {'pane_id': pane_id, 'available': True, 'shell_pid': shell_pid,
            'foreground': foreground, 'extra_foreground': extra,
            'background_proven': bool(info.get('descendants')),
            'source': 'herdr_pane_process_info'}


def _binding_blocker(state):
    rid = state.get('run_id')
    if not rid:
        return None
    blockers = [{'code': 'bound_run', 'owner': 'lead',
                 'detail': 'run %s is still bound to this workstation' % rid,
                 'next': 'accept/cancel tickets, then use the explicit unbind command'}]
    try:
        registry = ct.bindings(state.get('cwd'))
    except Exception as error:
        blockers.append({'code': 'binding_unreadable', 'owner': 'lead',
                         'detail': str(error)[:200], 'next': 'inspect .shop/bindings.json manually'})
        return blockers
    record = registry.get(rid)
    if not record:
        blockers.append({'code': 'binding_missing', 'owner': 'lead',
                         'detail': 'bound run has no registry record', 'next': 'manual reconciliation'})
    elif record.get('shop_id') != state.get('shop_id'):
        blockers.append({'code': 'binding_mismatch', 'owner': 'lead',
                         'detail': 'binding shop_id differs from this workstation',
                         'next': 'manual reconciliation; never rewrite the registry'})
    outstanding = []
    try:
        outstanding = [ticket for ticket in ct.tickets(state.get('cwd'), rid)
                       if ticket.get('status') not in ('accepted', 'cancelled')]
    except Exception:
        pass
    for ticket in outstanding[:8]:
        blockers.append({'code': 'outstanding_ticket', 'owner': ticket.get('owner'),
                         'detail': 'ticket %s (%s) is not accepted/cancelled'
                                   % (ticket.get('ticket_id'), ticket.get('status')),
                         'next': 'accept or cancel with evidence, then unbind'})
    return blockers


def _member_facts(api, state, process_probe=None, now=None):
    """Live facts for every registered member, using the *global* enumeration.

    A member is never reported ``missing`` while an agent carrying its registered
    name is live anywhere: that is ``moved`` (or ``duplicate``). Classification
    never guesses by cwd, model, list order or role.
    """
    probe = process_probe or process_facts
    del probe
    panes = []
    agents = []
    read_error = None
    try:
        for workspace in api('workspace', 'list')['workspaces']:
            panes.extend(herdr.pane_record(row, 'shutdown pane list')
                         for row in api('pane', 'list', '--workspace', workspace['workspace_id'])['panes'])
        agents = [herdr.agent_record(row, 'shutdown agent list') for row in api('agent', 'list')['agents']]
    except Exception as error:
        read_error = str(error)[:200]
    by_pane = {row.get('pane_id'): row for row in panes}
    live_by_name = {}
    for row in agents:
        name = row.get('name')
        if name:
            live_by_name.setdefault(name, []).append(row)
    rows = []
    for role, member in identity.roster(state):
        entry = {'role': role, 'name': member.get('name'), 'pane': member.get('pane'),
                 'desired_launch_id': member.get('launch_id'), 'desired_terminal_id': member.get('terminal_id'),
                 'classification': 'unknown', 'status': None,
                 'workspace_id': None, 'tab_id': None, 'pane_id': None, 'terminal_id': None,
                 'agent_name': None, 'observed_pane': None, 'observed_tab_id': None,
                 'observed_workspace_id': None, 'duplicates': [], 'error': None}
        if read_error:
            entry['error'] = read_error
            rows.append(entry)
            continue
        registered_pane = member.get('pane')
        candidates = live_by_name.get(member.get('name') or '', [])
        pane_row = by_pane.get(registered_pane)
        if len(candidates) > 1:
            entry['classification'] = 'duplicate'
            entry['duplicates'] = [row.get('pane_id') for row in candidates]
            primary = next((row for row in candidates if row.get('pane_id') == registered_pane), candidates[0])
            entry['agent_name'] = primary.get('name')
            entry['pane_id'] = primary.get('pane_id')
            rows.append(entry)
            continue
        if pane_row is not None:
            entry.update(workspace_id=pane_row.get('workspace_id'), tab_id=pane_row.get('tab_id'),
                         pane_id=pane_row.get('pane_id'), terminal_id=pane_row.get('terminal_id'))
        live_agent = next((row for row in agents if row.get('pane_id') == registered_pane), None)
        if live_agent is not None and pane_row is not None:
            entry.update(agent_name=live_agent.get('name'),
                         terminal_id=live_agent.get('terminal_id') or entry['terminal_id'])
            if live_agent.get('pane_id') != registered_pane:
                # The agent metadata names a different pane than the one queried.
                entry['classification'] = 'identity_mismatch'
            elif (live_agent.get('name') != member.get('name') or live_agent.get('agent') != 'pi'):
                entry['classification'] = 'replacement'
            elif member.get('terminal_id') and live_agent.get('terminal_id') != member['terminal_id']:
                entry['classification'] = 'replacement'
            elif pane_row.get('tab_id') != state.get('tab'):
                entry['classification'] = 'moved'
            elif live_agent.get('agent_status') not in herdr.AGENT_STATUSES:
                entry['classification'] = 'unknown'
            else:
                entry['classification'] = 'present_exact'
                entry['status'] = live_agent.get('agent_status')
            rows.append(entry)
            continue
        if candidates:
            elsewhere = candidates[0]
            candidate_pane = elsewhere.get('pane_id')
            observed_pane = by_pane.get(candidate_pane)
            if observed_pane is None:
                # Metadata names a pane that does not exist in the enumeration: the
                # identity is inconsistent, not relocated. Never guess a location.
                entry.update(classification='identity_mismatch', agent_name=elsewhere.get('name'),
                             observed_pane=candidate_pane, terminal_id=elsewhere.get('terminal_id'))
                rows.append(entry)
                continue
            # The registered pane is gone but the identity is live elsewhere: moved.
            entry.update(classification='moved', agent_name=elsewhere.get('name'),
                         observed_pane=candidate_pane,
                         observed_tab_id=observed_pane.get('tab_id'),
                         observed_workspace_id=observed_pane.get('workspace_id'),
                         terminal_id=elsewhere.get('terminal_id'))
            rows.append(entry)
            continue
        entry['classification'] = 'missing'
        rows.append(entry)
    return {'members': rows, 'panes': panes, 'agents': agents, 'read_error': read_error}


def _managed_tab_panes(state, member_facts):
    return [row for row in member_facts['panes']
            if row.get('tab_id') == state.get('tab')
            and row.get('pane_id') != (state.get('architect') or {}).get('pane')]


def preview(api, state_path, caller_pane, authority=None, repo=None, now=None,
            process_probe=None, transport_summary=None, handoff_summary=None):
    """Non-destructive plan. Never sends keys, closes, unbinds or deletes."""
    now = now or dt.datetime.now(dt.timezone.utc)
    state_dir = Path(state_path).parent.parent
    authority = dict(authority or {})
    authority.setdefault('kind', AUTHORITY if authority.get('action_id') else 'agent_cli')
    plan = {
        'schema': PLAN_SCHEMA,
        'plan_id': secrets.token_hex(16),
        'shop_id': None,
        'state_revision': None,
        'authority': authority.get('kind'),
        'authority_context': {'action_id': authority.get('action_id'), 'plugin_id': authority.get('plugin_id'),
                              'invocation_pane': caller_pane, 'recorded_at': stamp(now),
                              'note': 'Herdr user-action routing evidence, not an OS security boundary'},
        'invocation': {'pane_id': caller_pane, 'role': None, 'registered': False},
        'decision': 'unknown',
        'blockers': [],
        'keep': [],
        'close_order': [],
        'facts': {},
        'expires_at': (now + dt.timedelta(seconds=PLAN_TTL_SECONDS)).isoformat(),
    }
    try:
        state = read_state(state_path)
    except ShutdownRefused as refusal:
        plan['blockers'].append({'code': refusal.code, 'owner': None, 'detail': refusal.detail,
                                 'next': 'inspect the workstation state before any shutdown'})
        plan['facts'] = {'state_path': str(state_path)}
        if refusal.code == 'state_absent':
            receipt = last_receipt(state_dir, None)
            verification = _verify_closed_panes(api, (receipt or {}).get('closed') or [])
            if receipt and verification['verified']:
                plan['decision'] = 'already_closed'
                plan['facts']['receipt'] = {'shop_id': receipt.get('shop_id'),
                                            'closed_at': receipt.get('closed_at'),
                                            'verified_absent': True}
            else:
                plan['decision'] = 'unknown'
                plan['facts']['receipt_verification'] = verification
        else:
            plan['decision'] = 'unknown'
        return plan

    plan['shop_id'] = state.get('shop_id')
    plan['state_revision'] = state_revision(state)
    roster = identity.roster(state)
    invocation = next(((role, member) for role, member in roster
                       if member.get('pane') == caller_pane), None)
    if invocation:
        plan['invocation'] = {'pane_id': caller_pane, 'role': invocation[0], 'registered': True}
    else:
        plan['blockers'].append({'code': 'invocation_unregistered', 'owner': None,
                                 'detail': 'invoking pane is not a registered member pane',
                                 'next': 'diagnosis only; run the shutdown from a registered member pane'})
    plan['keep'] = [{'pane': (state.get('architect') or {}).get('pane'),
                     'name': (state.get('architect') or {}).get('name'), 'role': 'architect'}]

    if state.get('phase') in UNMANAGED_PHASES:
        plan['blockers'].append({'code': 'phase_' + str(state.get('phase')), 'owner': 'lead',
                                 'detail': 'workstation phase is ' + str(state.get('phase')),
                                 'next': 'inspect the recovery plan first; no close actions were taken'})
        plan['decision'] = 'recovery_required'
        plan['facts'] = {'phase': state.get('phase')}
        return plan

    binding = _binding_blocker(state)
    if binding:
        plan['blockers'].extend(binding)
        plan['decision'] = 'blocked'
        plan['facts'] = {'phase': state.get('phase'), 'run_id': state.get('run_id')}
        return plan

    member_facts = _member_facts(api, state, process_probe=process_probe, now=now)
    plan['facts'] = {'phase': state.get('phase'), 'tab': state.get('tab'),
                     'members': member_facts['members'], 'binding': 'none'}
    if member_facts['read_error']:
        plan['blockers'].append({'code': 'live_read_failed', 'owner': None,
                                 'detail': member_facts['read_error'],
                                 'next': 'retry after Herdr enumeration succeeds; nothing closed'})
        plan['decision'] = 'unknown'
        return plan

    for row in member_facts['members']:
        if row['role'] == 'architect':
            continue
        if row['classification'] != 'present_exact':
            plan['blockers'].append({'code': 'member_' + row['classification'], 'owner': row.get('owner'),
                                     'detail': 'member %s (%s) is %s' % (row['name'], row['pane'],
                                                                          row['classification']),
                                     'next': 'inspect the recovery plan; never close or reassign a drifted member'})
        elif row.get('status') not in ('idle', 'done'):
            plan['blockers'].append({'code': 'member_active', 'owner': row.get('owner'),
                                     'detail': 'member %s status is %s' % (row['name'], row.get('status')),
                                     'next': 'wait for the member to settle; shutdown never interrupts work'})
    architect = next((row for row in member_facts['members'] if row['role'] == 'architect'), None)
    if not architect or architect.get('classification') != 'present_exact':
        plan['blockers'].append({'code': 'architect_not_verified', 'owner': None,
                                 'detail': 'retained Architect pane is not verified: '
                                           + repr((architect or {}).get('classification')),
                                 'next': 'the Architect pane is never closed; repair the registration first'})

    managed = _managed_tab_panes(state, member_facts)
    registered_panes = {row.get('pane') for _, row in roster}
    extra = [row for row in managed if row.get('pane_id') not in registered_panes]
    if extra:
        plan['blockers'].append({'code': 'unregistered_pane', 'owner': None,
                                 'detail': 'unregistered panes in the managed tab: '
                                           + ','.join(str(row.get('pane_id')) for row in extra[:5]),
                                 'next': 'inspect those panes manually; shutdown never touches them'})
    plan['facts']['unregistered_panes'] = [row.get('pane_id') for row in extra]

    uncertainty = _uncertainty_blockers(state, transport_summary, handoff_summary, repo)
    if uncertainty:
        for blocker in uncertainty:
            plan['blockers'].append(blocker)

    executable = [row for row in member_facts['members']
                  if row['role'] != 'architect' and row['classification'] == 'present_exact'
                  and row.get('status') in ('idle', 'done')]
    order, process_blockers, process_rows = _close_order(state, executable, caller_pane,
                                                         process_probe or process_facts, api)
    plan['close_order'] = order
    plan['blockers'].extend(process_blockers)
    plan['facts']['process'] = process_rows

    codes = [entry['code'] for entry in plan['blockers']]
    if not plan['blockers'] and plan['close_order']:
        plan['decision'] = 'ready'
    elif any(code.startswith('member_') and code != 'member_active' for code in codes) \
            or any(code.startswith('unregistered_') for code in codes) \
            or 'architect_not_verified' in codes:
        # Drifted or unknown members need reconciliation, not a retry of the same close.
        plan['decision'] = 'recovery_required'
    elif plan['blockers']:
        plan['decision'] = 'blocked'
    else:
        plan['decision'] = 'unknown'
    return plan


def _close_order(state, executable, caller_pane, probe, api):
    """Deterministic caller-last order; Architect is never a target."""
    order = []
    process_rows = []
    blockers = []
    ranks = {'auxiliary_lead': 0, 'worker': 1, 'lead': 2}
    ordered = sorted(executable, key=lambda row: (ranks.get(row['role'], 3), str(row['name'])))
    for row in ordered:
        facts = probe(api, row['pane'])
        process_rows.append(facts)
        if not facts.get('available'):
            blockers.append({'code': 'process_uncertain', 'owner': row.get('owner'),
                             'detail': 'process facts unavailable for %s: %s' % (row['name'], facts.get('error')),
                             'next': 'retry after process-info works; nothing closed'})
            continue
        if facts.get('extra_foreground'):
            blockers.append({'code': 'foreground_work', 'owner': row.get('owner'),
                             'detail': 'member %s has foreground work (pids %s)'
                                       % (row['name'], [item.get('pid') for item in facts['extra_foreground']][:5]),
                             'next': 'stop or finish that work; shutdown never kills processes'})
            continue
        if not facts.get('background_proven'):
            blockers.append({'code': 'background_state_unknown', 'owner': row.get('owner'),
                             'detail': 'Herdr protocol %s exposes no descendant/background job list for %s'
                                       % (herdr.PROTOCOL, row['name']),
                             'next': 'verify the pane has no background jobs manually; capability gap, '
                                     'never assume stopped'})
            continue
        order.append({'pane': row['pane'], 'name': row['name'], 'role': row['role'],
                      'caller': row['pane'] == caller_pane})
    # The invoking execution pane is closed last; a non-caller order is preserved otherwise.
    order.sort(key=lambda item: (item['caller'], ranks.get(item['role'], 3), str(item['name'])))
    return order, blockers, process_rows


def _uncertainty_blockers(state, transport_summary, handoff_summary, repo):
    blockers = []
    rid = state.get('run_id')
    if transport_summary is None and repo and rid:
        try:
            import transport as transport_module
            transport_summary = transport_module.summary(repo, rid)
        except Exception as error:
            blockers.append({'code': 'transport_unknown', 'owner': 'lead',
                             'detail': 'transport facts unreadable: ' + str(error)[:200],
                             'next': 'inspect the transport store; never auto-retry a message'})
    if transport_summary:
        if transport_summary.get('unknown'):
            blockers.append({'code': 'transport_unknown', 'owner': 'lead',
                             'detail': '%s message(s) have an unresolved outcome'
                                       % transport_summary.get('unknown'),
                             'next': 'reconcile the transport records explicitly'})
        if transport_summary.get('pending'):
            blockers.append({'code': 'transport_pending', 'owner': 'lead',
                             'detail': '%s message(s) were received but never confirmed injected'
                                       % transport_summary.get('pending'),
                             'next': 'inspect pending records; no automatic re-injection'})
    if handoff_summary is None and repo and rid:
        try:
            import handoff as handoff_module
            handoff_summary = handoff_module.summary(repo, rid)
        except Exception as error:
            blockers.append({'code': 'handoff_unknown', 'owner': 'lead',
                             'detail': 'handoff facts unreadable: ' + str(error)[:200],
                             'next': 'inspect the handoff records manually'})
    if handoff_summary:
        open_handoffs = [item for item in handoff_summary.get('open') or []
                         if item.get('status') in ('proposed', 'accepted', 'blocked', 'cancellation_requested')]
        if open_handoffs:
            blockers.append({'code': 'handoff_pending', 'owner': 'lead',
                             'detail': '%d handoff(s) are not settled: %s'
                                       % (len(open_handoffs),
                                          ','.join(str(item.get('handoff_id')) for item in open_handoffs[:5])),
                             'next': 'resolve or explicitly cancel the handoffs; shutdown never cancels them'})
    return blockers


def _verify_closed_panes(api, panes):
    """Repeated shutdown: only a receipt plus absent panes counts as closed."""
    if not panes:
        return {'verified': False, 'reason': 'receipt lists no closed panes'}
    missing = []
    for pane_id in panes:
        verification = _verify_pane_absent(api, pane_id)
        if not verification['absent']:
            return {'verified': False, 'reason': verification.get('error') or ('pane %s still present' % pane_id)}
        missing.append(pane_id)
    return {'verified': True, 'verified_panes': missing}


def _live_registered_agents(api, registered_names):
    """Enumeration used for post-close verification; errors are uncertainty."""
    try:
        agents = [herdr.agent_record(row, 'shutdown agent list') for row in api('agent', 'list')['agents']]
    except Exception as error:
        return {'present': True, 'error': str(error)[:200], 'agents': []}
    remaining = [row for row in agents if row.get('name') in set(registered_names)]
    return {'present': bool(remaining), 'agents': remaining, 'error': None}


def revalidate(api, state_path, plan, process_probe=None, transport_summary=None, handoff_summary=None,
               now=None, repo=None):
    """Re-run every safety check and compare the state digest. Any drift refuses."""
    now = now or dt.datetime.now(dt.timezone.utc)
    if plan.get('schema') != PLAN_SCHEMA:
        raise ShutdownRefused('plan_invalid', 'unsupported plan schema')
    try:
        expires = dt.datetime.fromisoformat(str(plan.get('expires_at')))
    except ValueError:
        raise ShutdownRefused('plan_invalid', 'plan lacks a valid expiry')
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=dt.timezone.utc)
    if now > expires:
        raise ShutdownRefused('plan_expired', 'plan expired at ' + plan['expires_at'])
    fresh = preview(api, state_path, plan.get('invocation', {}).get('pane_id'),
                    authority={'kind': plan.get('authority'), 'action_id': plan.get('authority_context', {}).get('action_id'),
                               'plugin_id': plan.get('authority_context', {}).get('plugin_id')},
                    repo=repo, now=now, process_probe=process_probe,
                    transport_summary=transport_summary, handoff_summary=handoff_summary)
    if fresh.get('state_revision') != plan.get('state_revision'):
        raise ShutdownRefused('plan_stale', 'state changed after preview; new plan required')
    if fresh.get('decision') != 'ready':
        raise ShutdownRefused('plan_no_longer_ready',
                              'revalidation decision is ' + str(fresh.get('decision')))
    if [item['pane'] for item in fresh.get('close_order') or []] != [item['pane'] for item in plan.get('close_order') or []]:
        raise ShutdownRefused('plan_stale', 'close order or members changed; new plan required')
    return fresh


def execute(api, state_path, plan, process_probe=None, transport_summary=None, handoff_summary=None,
            now=None, repo=None):
    """Journaled execution of a ``ready`` plan. Caller must hold the Shop lock."""
    now = now or dt.datetime.now(dt.timezone.utc)
    if plan.get('decision') != 'ready':
        raise ShutdownRefused('plan_not_ready', 'only a ready plan may execute')
    fresh = revalidate(api, state_path, plan, process_probe=process_probe,
                       transport_summary=transport_summary, handoff_summary=handoff_summary,
                       now=now, repo=repo)
    state_dir = Path(state_path).parent.parent
    state_path = Path(state_path)
    state = read_state(state_path)
    result = {'schema': PLAN_SCHEMA, 'plan_id': plan.get('plan_id'), 'shop_id': state.get('shop_id'),
              'authority': plan.get('authority'), 'decision': 'unknown', 'closed': [], 'retained': None,
              'receipt': None, 'blockers': list(plan.get('blockers') or [])}
    # 1. archive before any mutation
    archive = archive_dir(state_dir, state.get('shop_id')) / (now.strftime('%Y%m%dT%H%M%S') + '.json')
    ct.atomic(archive, {'schema': 1, 'archived_at': stamp(now), 'reason': 'shutdown preview accepted',
                        'plan_id': plan.get('plan_id'), 'state': state})
    # 2. journal the intent
    journal = dict(state)
    journal['phase'] = 'shutdown_closing'
    journal['pending_shutdown'] = {'plan_id': plan.get('plan_id'), 'authority': plan.get('authority'),
                                   'started_at': stamp(now), 'archive': str(archive),
                                   'close_order': [item['pane'] for item in fresh.get('close_order') or []],
                                   'completed': []}
    ct.atomic(state_path, journal)
    result['journal'] = str(state_path)
    result['archive'] = str(archive)

    roster = {member.get('name') for _, member in identity.roster(state)}
    for item in fresh.get('close_order') or []:
        # strict recheck immediately before every close
        try:
            resolved = identity.resolve(api, _member_by_pane(state, item['pane']), state.get('tab'),
                                        allowed_status=('idle', 'done'), require_terminal=True)
        except Exception as error:
            return _partial(state_path, journal, result, state, item, 'identity_drift', str(error))
        facts = (process_probe or process_facts)(api, item['pane'])
        if not facts.get('available') or facts.get('extra_foreground') or not facts.get('background_proven'):
            return _partial(state_path, journal, result, state, item, 'process_uncertain',
                            'process facts changed before close')
        del resolved
        try:
            api('pane', 'close', item['pane'])
        except Exception as error:
            return _partial(state_path, journal, result, state, item, 'close_failed', str(error))
        verification = _verify_pane_absent(api, item['pane'])
        if not verification['absent']:
            return _partial(state_path, journal, result, state, item, 'still_present',
                            verification.get('error') or 'pane still enumerated after close')
        journal['pending_shutdown']['completed'].append({'pane': item['pane'], 'name': item['name'],
                                                         'closed_at': stamp(now)})
        ct.atomic(state_path, journal)
        result['closed'].append(item['pane'])

    # 3. final verification: retained Architect, final layout, no registered execution agents
    architect = state.get('architect') or {}
    final = _final_verification(api, state, architect, roster - {architect.get('name')})
    if not final['ok']:
        journal['phase'] = 'shutdown_partial'
        journal['shutdown_error'] = final
        ct.atomic(state_path, journal)
        result['decision'] = 'recovery_required'
        result['blockers'].append({'code': final['code'], 'owner': None, 'detail': final['detail'],
                                   'next': 'inspect the recovery plan; registration retained'})
        return result
    # Finalization is journaled: any failure here is a partial shutdown, not success.
    try:
        api('pane', 'zoom', '--pane', architect.get('pane'), '--off')
    except Exception as error:
        return _partial(state_path, journal, result, state,
                        {'pane': architect.get('pane'), 'name': architect.get('name')},
                        'zoom_failed', str(error))
    receipt_path = receipt_file(state_dir, state.get('shop_id'))
    receipt = {'schema': RECEIPT_SCHEMA, 'shop_id': state.get('shop_id'), 'plan_id': plan.get('plan_id'),
               'authority': plan.get('authority'), 'closed_at': stamp(now),
               'server_lineage': {'tab': state.get('tab'), 'state_file': str(state_path)},
               'retained': {'pane': architect.get('pane'), 'name': architect.get('name')},
               'closed': list(result['closed']), 'verified_absent': True,
               'registration_removed': False,
               'note': 'Verified absence of the registered execution identities; Architect retained.'}
    # Receipt first: an interrupted second write stays a reconciliation case.
    try:
        ct.atomic(receipt_path, receipt)
    except Exception as error:
        return _partial(state_path, journal, result, state,
                        {'pane': architect.get('pane'), 'name': architect.get('name')},
                        'receipt_write_failed', str(error))
    result['receipt'] = str(receipt_path)
    result['retained'] = receipt['retained']
    # 4. remove the active registration only after the receipt exists
    try:
        state_path.unlink()
    except OSError as error:
        journal['phase'] = 'shutdown_partial'
        journal['shutdown_error'] = {'code': 'registration_not_removed', 'detail': str(error)[:200],
                                     'at': ct.stamp(), 'receipt': str(receipt_path)}
        ct.atomic(state_path, journal)
        result['decision'] = 'recovery_required'
        result['blockers'].append({'code': 'registration_not_removed', 'owner': None,
                                   'detail': str(error)[:200],
                                   'next': 'receipt exists but the registration was not removed; reconcile '
                                           'manually and never announce a successful shutdown'})
        return result
    receipt['registration_removed'] = True
    receipt['removed_at'] = stamp(now)
    ct.atomic(receipt_path, receipt)
    result['receipt'] = str(receipt_path)
    result['decision'] = 'closed'
    return result


def _member_by_pane(state, pane_id):
    for _, member in identity.roster(state):
        if member.get('pane') == pane_id:
            return member
    raise ShutdownRefused('member_missing', 'no registered member for pane ' + str(pane_id))


def _partial(state_path, journal, result, state, item, code, detail):
    journal['phase'] = 'shutdown_partial'
    journal['shutdown_error'] = {'code': code, 'detail': str(detail)[:400], 'at': ct.stamp(),
                                 'pane': item.get('pane'), 'name': item.get('name')}
    ct.atomic(state_path, journal)
    result['decision'] = 'recovery_required'
    result['blockers'].append({'code': code, 'owner': None,
                               'detail': str(detail)[:200],
                               'next': 'inspect the shutdown journal and recovery plan; no success claimed'})
    return result


def _verify_pane_absent(api, pane_id):
    try:
        for workspace in api('workspace', 'list')['workspaces']:
            for row in api('pane', 'list', '--workspace', workspace['workspace_id'])['panes']:
                if row.get('pane_id') == pane_id:
                    return {'absent': False, 'error': None}
        agents = api('agent', 'list')['agents']
        for row in agents:
            if row.get('pane_id') == pane_id:
                return {'absent': False, 'error': 'agent still enumerated for pane'}
        return {'absent': True, 'error': None}
    except Exception as error:
        # Uncertain enumeration must never be reported as a verified close.
        return {'absent': False, 'error': 'verification failed: ' + str(error)[:200]}


def _final_verification(api, state, architect, execution_names):
    try:
        live = identity.observed_pane(api, architect.get('pane'), 'retained architect')
        agent = identity.observed_agent(api, architect.get('pane'), 'retained architect')
    except Exception as error:
        return {'ok': False, 'code': 'architect_verification_failed',
                'detail': str(error)[:200]}
    if live.get('tab_id') != state.get('tab') or agent.get('name') != architect.get('name'):
        return {'ok': False, 'code': 'architect_mismatch',
                'detail': 'retained pane does not match the registered Architect'}
    remaining = _live_registered_agents(api, execution_names)
    if remaining['error']:
        return {'ok': False, 'code': 'final_enumeration_failed', 'detail': remaining['error']}
    if remaining['present']:
        return {'ok': False, 'code': 'execution_agent_still_present',
                'detail': 'registered execution agent(s) still enumerated: '
                          + ','.join(str(row.get('name')) for row in remaining['agents'][:5])}
    try:
        layout = api('pane', 'layout', '--pane', architect.get('pane'))['layout']
    except Exception as error:
        return {'ok': False, 'code': 'final_layout_unreadable', 'detail': str(error)[:200]}
    panes = [row.get('pane_id') for row in layout.get('panes') or []]
    if panes != [architect.get('pane')]:
        return {'ok': False, 'code': 'unexpected_final_layout',
                'detail': 'final layout panes: ' + ','.join(str(item) for item in panes)}
    return {'ok': True, 'code': None, 'detail': None}


def load_plan(state_dir, plan_id):
    try:
        safe = ct.ident(plan_id)
    except RuntimeError:
        raise ShutdownRefused('plan_invalid', 'unsafe plan id')
    for candidate in (shutdown_dir(state_dir) / 'plans').glob('*/' + safe + '.json'):
        if candidate.is_file():
            return json.loads(candidate.read_text())
    raise ShutdownRefused('plan_invalid', 'no stored plan for id ' + str(plan_id))


def last_receipt(state_dir, shop_id):
    if not shop_id:
        folder = shutdown_dir(state_dir) / 'receipts'
        files = sorted(folder.glob('*.json')) if folder.is_dir() else []
        if not files:
            return None
        try:
            return json.loads(files[-1].read_text())
        except (OSError, ValueError):
            return None
    path = receipt_file(state_dir, shop_id)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def recovery_plan(api, state_path, repo=None, now=None, persist=True):
    """Read-only reconciliation plan for partial/moved/server-restart/mixed survivors.

    The plan carries a nonce, the state revision and a short expiry, and is stored
    (0600) so ``repair.restore`` can require a digest-matching, non-stale plan instead
    of trusting a hand-built dict. Producing the plan mutates no shop state.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    state_dir = Path(state_path).parent.parent
    plan = {'schema': RECOVERY_PLAN_SCHEMA, 'plan_id': secrets.token_hex(16),
            'generated_at': stamp(now), 'state_path': str(state_path),
            'expires_at': (now + dt.timedelta(seconds=RECOVERY_PLAN_TTL_SECONDS)).isoformat(),
            'state_revision': None,
            'decision': 'unknown', 'members': [], 'cases': [], 'keep': [], 'actions': [], 'blockers': [],
            'note': 'Read-only plan. It never restores, reassigns, replays, closes or deletes anything; '
                    'every apply path is a separate explicit user action.'}
    try:
        state = read_state(state_path)
    except ShutdownRefused as refusal:
        plan['blockers'].append({'code': refusal.code, 'detail': refusal.detail,
                                 'next': 'locate the registration through the bounded shutdown receipts/index'})
        receipt = last_receipt(Path(state_path).parent.parent, None)
        if receipt:
            plan['decision'] = 'already_closed'
            plan['receipt'] = {'shop_id': receipt.get('shop_id'), 'closed_at': receipt.get('closed_at')}
        return plan
    plan['shop_id'] = state.get('shop_id')
    plan['state_revision'] = state_revision(state)
    plan['phase'] = state.get('phase')
    plan['run_id'] = state.get('run_id')
    plan['binding'] = 'bound' if state.get('run_id') else 'unbound'
    facts = _member_facts(api, state)
    plan['members'] = facts['members']
    for row in facts['members']:
        classification = row['classification']
        if classification == 'present_exact':
            case = 'present_exact'
            action = 'retain (no action)'
        elif classification == 'moved':
            case = 'moved'
            action = 'display jump coordinates; explicit reattach is a separate archived operation'
        elif classification == 'missing':
            case = 'missing'
            action = 'preserve worktrees/tickets; explicit restore may create fresh sessions'
        elif classification == 'identity_mismatch':
            case = 'identity_mismatch'
            action = 'never adopt automatically; inspect the pane and decide explicitly'
        elif classification == 'replacement':
            case = 'replacement'
            action = 'registration stays stale until explicit reconciliation'
        elif classification == 'duplicate':
            case = 'duplicate'
            action = 'inspect both panes; never pick by name or list order'
        else:
            case = 'unknown'
            action = 'read-only diagnosis only'
        plan['cases'].append({'member': row['name'], 'role': row['role'], 'case': case, 'action': action})
    present = [row for row in facts['members'] if row['classification'] == 'present_exact']
    execution_present = [row for row in present if row['role'] != 'architect']
    plan['keep'] = [{'pane': row['pane'], 'name': row['name'], 'role': row['role']} for row in present]
    plan['execution_survivors'] = [{'name': row['name'], 'pane': row['pane']} for row in execution_present]
    if not execution_present:
        plan['decision'] = 'recovery_required'
        plan['actions'].append({'kind': 'restore_candidates', 'mode': 'preview-only',
                                'detail': 'no execution member survives (Architect only); restore may create '
                                          'fresh sessions only after proving no registered agent lives in any '
                                          'workspace',
                                'tickets_preserved': True,
                                'requires_explicit_apply': True})
    if any(row['classification'] in ('moved', 'replacement', 'duplicate', 'identity_mismatch',
                                     'missing', 'unknown')
           for row in facts['members']):
        plan['decision'] = 'recovery_required'
        plan['actions'].append({'kind': 'manual_reconciliation', 'mode': 'preview-only',
                                'detail': 'mixed survivors need explicit adopt/recreate choices'})
    elif state.get('phase') in UNMANAGED_PHASES:
        plan['decision'] = 'recovery_required'
        plan['actions'].append({'kind': 'inspect_pending_operation', 'mode': 'preview-only',
                                'detail': 'phase %s has a pending journal; verified absence is not proof '
                                          'that jobs stopped' % state.get('phase')})
    else:
        plan['decision'] = 'no_action_required'
    if persist and plan.get('shop_id'):
        plan['plan_path'] = str(recovery_plan_file(state_dir, plan['shop_id'], plan['plan_id']))
    else:
        plan['plan_path'] = None
    # Digest last: the body must not change after the digest is computed.
    plan['plan_digest'] = plan_digest(plan)
    if persist and plan.get('shop_id'):
        path = Path(plan['plan_path'])
        ct.atomic(path, plan)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    return plan


def parse_authority(env=None):
    """Authority context from the environment; never from agent-supplied flags."""
    env = os.environ if env is None else env
    action_id = env.get('HERDR_PLUGIN_ACTION_ID')
    plugin_id = env.get('HERDR_PLUGIN_ID')
    return {'kind': AUTHORITY if action_id else 'agent_cli', 'action_id': action_id, 'plugin_id': plugin_id}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['preview', 'execute', 'recovery'])
    parser.add_argument('--state', required=True)
    parser.add_argument('--caller', required=True)
    parser.add_argument('--plan', help='Plan id written by a previous preview (execute only)')
    parser.add_argument('--repo', help='Main repository root for binding/ticket facts')
    parser.add_argument('--json', action='store_true', help='Accepted for symmetry; output is JSON')
    args = parser.parse_args(argv)
    del args.json
    import shop as shop_module
    api = shop_module.api
    if args.action == 'preview':
        plan = preview(api, args.state, args.caller, authority=parse_authority(os.environ), repo=args.repo)
        state_dir = Path(args.state).parent.parent
        if plan.get('shop_id'):
            path = plan_file(state_dir, plan['shop_id'], plan['plan_id'])
            ct.atomic(path, plan)
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
            plan['plan_path'] = str(path)
        plan['state_path'] = args.state
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return 0
    if args.action == 'recovery':
        print(json.dumps(recovery_plan(api, args.state, repo=args.repo), ensure_ascii=False, indent=2))
        return 0
    authority = parse_authority(os.environ)
    if authority['kind'] != AUTHORITY:
        raise ShutdownRefused('authority_missing',
                              'execution requires Herdr plugin-action context (HERDR_PLUGIN_ACTION_ID)')
    if not args.plan:
        raise ShutdownRefused('plan_missing', 'execution requires a previewed plan id')
    state_dir = Path(args.state).parent.parent
    try:
        safe_plan = ct.ident(args.plan)
    except RuntimeError:
        raise ShutdownRefused('plan_invalid', 'unsafe plan id')
    plan_path = None
    for candidate in (shutdown_dir(state_dir) / 'plans').glob('*/' + safe_plan + '.json'):
        plan_path = candidate
    if not plan_path or not plan_path.is_file():
        raise ShutdownRefused('plan_invalid', 'no stored plan for id ' + args.plan)
    plan = json.loads(plan_path.read_text())
    result = execute(api, args.state, plan, repo=args.repo)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
