"""Single bounded, redacted observability producer: ``shop.snapshot/v1``.

One read-only shape for `herdr-shop status`, bounded patrol, the Pi commands and
the plugin status action. The snapshot never authorizes anything: it separates
*desired* (registered) from *observed* (live read / event) facts, records ticket
and checkpoint staleness as inspection hints, and renders unknown as unknown.

Rules encoded here:

* no model calls, no dispatch, no pause, no close, no focus, no second daemon;
* no raw state dump, no prompt/transcript/message body/env value ever serialized;
* hard caps (member/ticket counts, per-string caps, total bytes) with an explicit
  ``snapshot_truncated`` attention entry instead of unbounded growth;
* silence or long duration never produces a stuck/dead/done verdict.
"""
import copy
import datetime as dt
import json
from pathlib import Path
import re

import contracts as ct
import events as event_module
import herdr
import identity

SCHEMA = 'shop.snapshot/v1'
GENERATOR_PROTOCOL = 1
STALE_SNAPSHOT_SECONDS = 30
STALE_CHECKPOINT_SECONDS = 180
LIMITS = {
    'members': 64,
    'tickets': 128,
    'next_steps': 5,
    'next_step_chars': 250,
    'checkpoint_progress_chars': 1000,
    'objective_chars': 400,
    'error_chars': 250,
    'total_bytes': 262144,
}
REDACTION = {
    'version': 1,
    'omitted': ['prompt_text', 'transcript', 'message_body', 'env_values',
                'credentials', 'session_file_content'],
}
UNOBSERVED = ('unknown', 'missing', 'moved', 'identity_mismatch', 'unreachable', 'identity_refused')
_ENV_ASSIGNMENT = re.compile(r'\b[A-Z][A-Z0-9_]{2,}=\S+')
_HOME_PATH = re.compile(r'/(?:Users|home)/[^/\s"\']+')
_CONTROL = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]')
# Conservative credential shapes only: never generic hex/hashes, so commit SHAs and
# message fingerprints in retained free text stay readable.
_CREDENTIAL = re.compile(r'\b(?:sk|pk|rk|ghp|gho|ghs|glpat|xox[baprs]|AKIA)[-_A-Za-z0-9]{8,}\b')
_BEARER = re.compile(r'(?i)\bbearer\s+[A-Za-z0-9._-]{8,}')


def stamp(now=None):
    return (now or dt.datetime.now(dt.timezone.utc)).isoformat()


def _parse_iso(value):
    try:
        parsed = dt.datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed


def age_seconds(value, now=None):
    parsed = _parse_iso(value)
    if not parsed:
        return None
    current = now or dt.datetime.now(dt.timezone.utc)
    return max(0, int((current - parsed).total_seconds()))


def sanitize_text(value, limit, now=None):
    """Cap and strip credential-looking strings, home paths and control chars."""
    if value is None:
        return None
    text = _CONTROL.sub(' ', str(value))
    text = _ENV_ASSIGNMENT.sub('<redacted-env>', text)
    text = _BEARER.sub('<redacted-credential>', text)
    text = _CREDENTIAL.sub('<redacted-credential>', text)
    text = _HOME_PATH.sub('<home>', text)
    text = ' '.join(text.split())
    if limit and len(text) > limit:
        text = text[:limit] + '…'
    return text


def _bounded_list(values, count, chars):
    items = []
    for value in list(values or [])[:count]:
        items.append(sanitize_text(value, chars))
    return items


def checkpoint_row(path, now=None):
    row = {'path': str(path) if path else None, 'exists': False, 'progress': None,
           'next_steps': [], 'age_seconds': None, 'stale': False}
    if not path:
        return row
    row['exists'] = Path(path).is_file()
    try:
        data = ct.load(path)
    except Exception:
        return row
    if isinstance(data, dict):
        row['progress'] = sanitize_text(data.get('progress'), LIMITS['checkpoint_progress_chars'])
        row['next_steps'] = _bounded_list(data.get('next_steps'), LIMITS['next_steps'],
                                          LIMITS['next_step_chars'])
        row['saved_at'] = sanitize_text(data.get('saved_at'), 40)
    row['age_seconds'] = age_seconds(row.get('saved_at'), now)
    if row['age_seconds'] is None:
        try:
            row['age_seconds'] = max(0, int((now or dt.datetime.now(dt.timezone.utc)).timestamp()
                                           - Path(path).stat().st_mtime))
        except OSError:
            row['age_seconds'] = None
    row['stale'] = bool(row['age_seconds'] is not None and row['age_seconds'] > STALE_CHECKPOINT_SECONDS)
    return row


def responsibility(row):
    """Explicit接手 state: a missing owner is `unclaimed`, never `idle`."""
    status = row.get('status')
    owner = row.get('owner') or None
    if status == 'accepted':
        return {'owner': owner, 'state': 'accepted'}
    if status == 'review':
        return {'owner': owner, 'state': 'review'}
    if not owner:
        return {'owner': None, 'state': 'unclaimed'}
    return {'owner': owner, 'state': 'claimed'}


def _attempt_path(repo, ticket):
    """Best-effort checkpoint path: a synthetic/stale ticket is never fatal."""
    if not repo:
        return None
    try:
        return ct.attempt_path(repo, ticket, 'checkpoint')
    except (KeyError, RuntimeError, TypeError):
        return None


def ticket_row(ticket, repo, now=None):
    checkpoint = checkpoint_row(_attempt_path(repo, ticket), now)
    row = {
        'ticket_id': ticket.get('ticket_id'),
        'attempt': ticket.get('attempt'),
        'status': ticket.get('status'),
        'kind': ticket.get('kind'),
        'owner': ticket.get('owner') or None,
        'objective': sanitize_text(ticket.get('objective'), LIMITS['objective_chars']),
        'result_path': ticket.get('result_path'),
        'delivery': (ticket.get('dispatch') or {}).get('delivery'),
        'dependencies_ready': None,
        'checkpoint': checkpoint,
        'responsibility': responsibility(ticket),
        'attention': [],
    }
    return row


def _member_cwd(member, state):
    return member.get('cwd') or state.get('cwd')


def _observed_member(api, state, member, now, fact=None, older_than=None):
    """Live read for one member: pane first, then agent identity/status.

    Event facts may only annotate an observation. ``authoritative`` is true only
    for a live read, and a disagreement keeps the live status with a gap entry.
    """
    observed = {'status': 'unknown', 'tab_id': None, 'pane_id': None, 'workspace_id': None,
                'agent_kind': None, 'agent_name': None, 'terminal_id': None,
                'source': 'none', 'live_checked_at': None, 'stale_live': False,
                'authoritative': False, 'event_status': None, 'event_at': None,
                'event_age_seconds': None, 'event_disagrees': False}
    if fact:
        observed['event_status'] = fact.get('agent_status')
        observed['event_at'] = fact.get('at')
        observed['event_age_seconds'] = age_seconds(fact.get('at'), now)
    if api is None:
        if fact:
            # Non-authoritative observation only: no live read was performed.
            observed['source'] = 'event'
            observed['status'] = fact.get('agent_status') or 'unknown'
            observed['workspace_id'] = fact.get('workspace_id')
            observed['pane_id'] = fact.get('pane_id')
        return observed
    observed['source'] = 'live'
    observed['live_checked_at'] = stamp(now)
    try:
        pane_row = identity.observed_pane(api, member['pane'], 'member ' + str(member.get('name')))
    except Exception as error:
        observed['status'] = 'unreachable'
        observed['error'] = sanitize_text(str(error), LIMITS['error_chars'])
        return observed
    observed['tab_id'] = pane_row.get('tab_id')
    observed['pane_id'] = pane_row.get('pane_id')
    observed['workspace_id'] = pane_row.get('workspace_id')
    observed['terminal_id'] = pane_row.get('terminal_id')
    observed['agent_kind'] = pane_row.get('agent')
    verdict = identity.health(api, state, member, pane_row)
    observed['health'] = verdict
    observed['status'] = verdict
    observed['authoritative'] = verdict in ('present', 'missing', 'moved', 'identity_mismatch')
    try:
        agent = identity.observed_agent(api, member['pane'], 'member ' + str(member.get('name')))
        if verdict == 'present':
            # Verified identity: report the agent lifecycle status, never the
            # presence verdict, and fail closed on an unknown status.
            status = agent.get('agent_status')
            observed['status'] = status if status in herdr.AGENT_STATUSES else 'unknown'
        observed['agent_name'] = agent.get('name')
        observed['agent_kind'] = agent.get('agent') or observed['agent_kind']
        observed['terminal_id'] = agent.get('terminal_id') or observed['terminal_id']
        observed['tab_id'] = agent.get('tab_id') or observed['tab_id']
        observed['pane_id'] = agent.get('pane_id') or observed['pane_id']
        observed['workspace_id'] = agent.get('workspace_id') or observed['workspace_id']
    except Exception as error:
        observed['agent_name'] = None
        observed['error'] = sanitize_text(str(error), LIMITS['error_chars'])
    if verdict == 'present':
        observed['source'] = 'live'
    if fact:
        if fact.get('agent_status') == observed.get('status'):
            observed['source'] = 'live+event'
        else:
            # Live read wins; the mismatch is reported, never merged silently.
            observed['event_disagrees'] = True
    return observed


def _event_index(facts):
    index = {}
    for fact in (facts or {}).get('facts', []) if isinstance(facts, dict) else []:
        pane = fact.get('pane_id')
        if pane:
            index[pane] = fact  # facts are appended in order: newest wins
    return index


def member_rows(state, tickets, api=None, now=None, roles=None, facts=None):
    """One row per registered member: desired facts + observed facts + tickets.

    ``roles`` narrows the view (patrol uses execution members only) while every
    reader still receives the same schema and fact separation.
    """
    workers = list(state.get('workers', []))
    primary_worker = workers[0] if workers else None
    event_index = _event_index(facts)
    rows = []
    for role, member in identity.roster(state):
        if roles is not None and role not in roles:
            continue
        member_tickets = [t for t in tickets if t.get('owner') == member.get('name')]
        observed = _observed_member(api, state, member, now, event_index.get(member.get('pane')))
        row = {
            'role': role,
            'name': member.get('name'),
            'member_id': member.get('name'),
            # Compatibility projections so patrol consumers keep working.
            'pane': member.get('pane'),
            'cwd': _member_cwd(member, state),
            'status': observed.get('status'),
            'terminal_id': observed.get('terminal_id'),
            'identity': _member_view(member),
            'desired': _member_view(member),
            'observed': observed,
            'transport': {'epoch': None, 'present': False},
            'tickets': [],
            'removable_candidate': False,
        }
        if row['identity'].get('refused'):
            # A member that claims a transport epoch is refused, not shown healthy.
            observed['status'] = 'identity_refused'
            observed['source'] = 'refused'
            observed['error'] = row['identity']['refused']
            row['status'] = observed['status']
        owned_open = [t for t in member_tickets if t.get('status') not in ('accepted', 'cancelled')]
        row['removable_candidate'] = bool(not owned_open and observed.get('status') in ('idle', 'done')
                                          and member is not primary_worker)
        rows.append(row)
    return rows


def _member_view(member):
    try:
        return identity.member_view(member)
    except Exception as error:
        return {'name': member.get('name'), 'pane': member.get('pane'),
                'transport': None, 'transport_epoch': None, 'transport_present': None,
                'refused': sanitize_text(str(error), LIMITS['error_chars'])}


def _attach_tickets(rows, tickets, repo, now):
    by_id = {ticket.get('ticket_id'): ticket for ticket in tickets}
    for row in rows:
        for ticket in [t for t in tickets if t.get('owner') == row['name']]:
            entry = ticket_row(ticket, repo, now)
            entry['dependencies_ready'] = all(by_id.get(dep, {}).get('status') == 'accepted'
                                              for dep in ticket.get('depends_on', []))
            dispatch = ticket.get('dispatch') or {}
            entry['occupant_changed'] = bool(dispatch and (
                dispatch.get('pane') != row.get('pane')
                or (dispatch.get('terminal_id') and dispatch.get('terminal_id') != row.get('terminal_id'))))
            entry['inspect_output'] = bool(
                entry['occupant_changed']
                or row.get('status') in UNOBSERVED
                or (ticket.get('status') == 'assigned' and row.get('status') in ('idle', 'done'))
                or entry['delivery'] in ('pending', 'uncertain')
                or entry['checkpoint'].get('stale'))
            if ticket.get('status') == 'assigned' and not entry['checkpoint'].get('exists'):
                age = age_seconds(dispatch.get('sent_at'), now)
                if age is None or age > STALE_CHECKPOINT_SECONDS:
                    entry['inspect_output'] = True
            if entry['checkpoint'].get('stale'):
                entry['attention'].append('checkpoint_stale')
            if entry['status'] == 'assigned' and not entry['checkpoint'].get('exists'):
                # Missing checkpoint is an inspection hint after the dispatch window,
                # never an age-based verdict about the writer.
                age = age_seconds((ticket.get('dispatch') or {}).get('sent_at'), now)
                if age is None or age > STALE_CHECKPOINT_SECONDS:
                    entry['attention'].append('no_checkpoint_yet')
            row['tickets'].append(entry)


def build(state, tickets, api=None, runtime=None, caller_pane=None, repo=None, run_id=None,
          transport_summary=None, handoff_summary=None, facts=None, now=None, roles=None,
          scope_label='all-members'):
    """Compose the bounded snapshot. Read-only; never authorizes or mutates."""
    now = now or dt.datetime.now(dt.timezone.utc)
    rid = run_id or state.get('run_id')
    tickets = list(tickets or [])
    rows = member_rows(state, tickets, api=api, now=now, roles=roles, facts=facts)
    _attach_tickets(rows, tickets, repo, now)
    capped = False
    if len(rows) > LIMITS['members']:
        rows = rows[:LIMITS['members']]
        capped = True
    for row in rows:
        if len(row['tickets']) > LIMITS['tickets']:
            row['tickets'] = row['tickets'][:LIMITS['tickets']]
            capped = True
    registered = {row['name'] for row in rows}
    unregistered = [{'ticket_id': t.get('ticket_id'), 'owner': t.get('owner') or None,
                     'status': t.get('status'), 'responsibility': responsibility(t)}
                    for t in tickets
                    if t.get('owner') not in registered and t.get('status') not in ('accepted', 'cancelled')]
    binding = _binding(state, rid)
    doc = {
        'schema': SCHEMA,
        'generated_at': stamp(now),
        'generator': {'core_root': str(Path(__file__).resolve().parent.parent),
                      'protocol': GENERATOR_PROTOCOL},
        'herdr_runtime': runtime or {'binary': None, 'compatible': None, 'probe': 'not-read'},
        'shop': {
            'shop_id': state.get('shop_id'), 'run_id': rid, 'phase': state.get('phase'),
            'tab': state.get('tab'), 'cwd': state.get('cwd'), 'prefix': state.get('prefix'),
            'bound': bool(rid), 'binding': binding,
        },
        'members': rows,
        'unregistered_owner_tickets': unregistered,
        'capacity': {'lead': len(state.get('extra_leads', [])) + 1,
                     'worker': len(state.get('workers', [])), 'max_each': 2},
        'transport': _transport_section(repo, rid, transport_summary),
        'handoff': _handoff_section(repo, rid, handoff_summary),
        'events': _events_section(facts, now, rows),
        'shutdown': _shutdown_section(state),
        'links': _links(rows, caller_pane),
        'attention': [],
        'unknowns': [],
        'redaction': dict(REDACTION),
        'limits': dict(LIMITS),
        'truncated': capped,
        'scope': {'kind': scope_label, 'roles': sorted(roles) if roles else ['architect', 'lead',
                                                                             'auxiliary_lead', 'worker']},
        # Compatibility projection: inspection hints are never stuck/direction verdicts.
        'note': 'Inspection hints are not stuck/direction verdicts. No screen reads, pauses, adds, '
                'removes or prompts performed by the snapshot.',
    }
    _attention(doc, now)
    _unknowns(doc)
    return doc


def _binding(state, rid):
    if not rid:
        return {'run_id': None, 'shop_id': None, 'ok': False,
                'detail': 'no bound run; status never infers a run from current.json'}
    try:
        registry = ct.bindings(state.get('cwd'))
    except Exception as error:
        return {'run_id': rid, 'shop_id': state.get('shop_id'), 'ok': False,
                'detail': sanitize_text(str(error), LIMITS['error_chars'])}
    record = registry.get(rid)
    ok = bool(record) and record.get('shop_id') == state.get('shop_id')
    return {'run_id': rid, 'shop_id': state.get('shop_id'), 'ok': ok,
            'detail': None if ok else 'binding registry mismatch; manual reconciliation required'}


def _transport_section(repo, rid, summary):
    base = {'implemented': False, 'reason': 'P2 transport report unavailable', 'endpoint': None,
            'broker': None, 'peers': [], 'pending': 0, 'unknown': 0}
    if summary:
        base = dict(base, **summary)
    elif repo and rid:
        try:
            import transport as transport_module
            base = dict(base, **transport_module.summary(repo, rid))
        except Exception as error:
            base['reason'] = sanitize_text('transport summary failed: ' + str(error), LIMITS['error_chars'])
            base['unknown'] = None
    return base


def _handoff_section(repo, rid, summary):
    base = {'implemented': False, 'reason': 'P2 handoff report unavailable', 'open': [], 'counts': {}}
    if summary:
        base = dict(base, **summary)
    elif repo and rid:
        try:
            import handoff as handoff_module
            base = dict(base, **handoff_module.summary(repo, rid))
        except Exception as error:
            base['reason'] = sanitize_text('handoff summary failed: ' + str(error), LIMITS['error_chars'])
    return base


def _events_section(facts, now, members=None):
    if not isinstance(facts, dict):
        facts = event_module.read_facts(None, None)
    section = {'coverage': 'unknown', 'last_event_at': None, 'revision': None,
               'subscriptions': sorted(event_module.SUBSCRIBED.values()), 'facts': [], 'gaps': [],
               'authority': 'display-only observation; live reads remain authoritative'}
    section.update({key: value for key, value in facts.items() if key != 'facts'})
    section['fact_count'] = len(facts.get('facts') or [])
    if any((member.get('observed') or {}).get('event_disagrees') for member in (members or [])):
        # Live read wins; the disagreement is surfaced as a coverage gap.
        section['coverage'] = 'partial'
        section['reason'] = section.get('reason') or 'live read disagrees with the last event fact; live wins'
    return section


def _shutdown_section(state):
    """Read-only shutdown/recovery facts: journal phase, pending close and last receipt."""
    section = {'phase': state.get('phase'), 'pending': None, 'last_receipt': None,
               'journal': 'none', 'authority': 'user action (Herdr plugin action) only',
               'note': 'Preview is read-only; execution requires plugin-action context and revalidation.'}
    pending = state.get('pending_shutdown')
    if isinstance(pending, dict):
        section['pending'] = {'plan_id': sanitize_text(pending.get('plan_id'), 64),
                              'started_at': pending.get('started_at'),
                              'completed': len(pending.get('completed') or []),
                              'close_order': len(pending.get('close_order') or [])}
        section['journal'] = 'shutdown_closing'
    if state.get('shutdown_error'):
        section['journal'] = 'shutdown_partial'
        section['error'] = {'code': (state['shutdown_error'] or {}).get('code'),
                            'detail': sanitize_text((state['shutdown_error'] or {}).get('detail'),
                                                    LIMITS['error_chars'])}
    return section


def _links(rows, caller_pane):
    links = []
    for row in rows:
        observed = row.get('observed') or {}
        workspace = observed.get('workspace_id')
        tab = observed.get('tab_id')
        pane = observed.get('pane_id') or row.get('pane')
        label = '%s (%s)' % (row.get('name'), row.get('role'))
        focus = None
        if workspace and tab:
            # Read-only: the snapshot only prints a command sequence that the typed
            # adapter actually declares. Validation is pure; nothing focuses here.
            focus = {'command': ['herdr', 'workspace', 'focus', workspace],
                     'then': ['herdr', 'tab', 'focus', tab]}
            for command in (focus['command'], focus['then']):
                herdr.validate_focus_command(command)
        links.append({'label': label, 'role': row.get('role'), 'workspace_id': workspace,
                      'tab_id': tab, 'pane_id': pane, 'focus': focus,
                      'current': bool(caller_pane and pane == caller_pane),
                      'pane_focus': None})
    return links


def _attention(doc, now):
    entries = []
    if doc.get('truncated'):
        entries.append({'code': 'snapshot_truncated', 'severity': 'warn', 'target': 'snapshot',
                        'detail': 'member/ticket caps of %d/%d applied; use the ticket files for the rest'
                                  % (LIMITS['members'], LIMITS['tickets']),
                        'owner': None, 'next': 'open the run ticket files directly'})
    for row in doc['members']:
        observed = row.get('observed') or {}
        status = observed.get('status')
        if status in UNOBSERVED:
            entries.append({'code': 'member_unobserved', 'severity': 'warn', 'target': row.get('name'),
                            'detail': 'observed status %s; needs inspection, never a death verdict' % status,
                            'owner': row.get('name'), 'next': 'inspect live pane/agent before any action'})
        if row.get('identity', {}).get('refused'):
            entries.append({'code': 'identity_refused', 'severity': 'warn', 'target': row.get('name'),
                            'detail': row['identity']['refused'], 'owner': row.get('name'),
                            'next': 'inspect member identity mapping'})
        for ticket in row.get('tickets', []):
            if 'checkpoint_stale' in ticket['attention']:
                entries.append({'code': 'checkpoint_stale', 'severity': 'info',
                                'target': ticket.get('ticket_id'),
                                'detail': 'checkpoint older than %ds; inspection hint only' % STALE_CHECKPOINT_SECONDS,
                                'owner': ticket.get('owner'),
                                'next': 'read the checkpoint and the latest output before judging progress'})
            if 'no_checkpoint_yet' in ticket['attention']:
                entries.append({'code': 'no_checkpoint_yet', 'severity': 'warn',
                                'target': ticket.get('ticket_id'),
                                'detail': 'assigned ticket has no checkpoint past %ds' % STALE_CHECKPOINT_SECONDS,
                                'owner': ticket.get('owner'), 'next': 'inspect the owner pane'})
            if ticket.get('status') == 'review':
                entries.append({'code': 'result_awaiting_review', 'severity': 'info',
                                'target': ticket.get('ticket_id'), 'detail': 'result published; acceptance pending',
                                'owner': 'lead', 'next': 'independent review, then accept or retry'})
            if ticket.get('responsibility', {}).get('state') == 'unclaimed':
                entries.append({'code': 'ticket_unclaimed', 'severity': 'warn',
                                'target': ticket.get('ticket_id'),
                                'detail': '未接手: work exists without a responsible member',
                                'owner': None, 'next': 'assign explicitly, never infer an owner'})
    transport = doc.get('transport') or {}
    if transport.get('unknown'):
        entries.append({'code': 'transport_unknown_records', 'severity': 'warn', 'target': 'transport',
                        'detail': '%s message(s) have an unresolved outcome; no automatic retry' % transport['unknown'],
                        'owner': 'lead', 'next': 'reconcile explicitly; never auto-re-inject'})
    events_section = doc.get('events') or {}
    if events_section.get('coverage') == 'unknown':
        entries.append({'code': 'events_unknown', 'severity': 'info', 'target': 'events',
                        'detail': 'no event facts recorded yet; live reads are authoritative',
                        'owner': None, 'next': 'install the plugin manifest so Herdr emits pane events'})
    for gap in (events_section.get('gaps') or [])[:8]:
        entries.append({'code': 'event_gap', 'severity': 'warn', 'target': 'events',
                        'detail': 'revision gap %s -> %s; events may have been missed' % (gap.get('from'), gap.get('to')),
                        'owner': None, 'next': 'rely on the live read, then re-check after the next event'})
    if events_section.get('coverage') == 'partial' and not events_section.get('gaps'):
        entries.append({'code': 'event_gap', 'severity': 'warn', 'target': 'events',
                        'detail': 'live read disagrees with the last event fact; live read wins',
                        'owner': None, 'next': 'trust the live read; inspect the pane before any action'})
    for entry in doc.get('unregistered_owner_tickets', []):
        if not entry.get('owner'):
            entries.append({'code': 'ticket_unclaimed', 'severity': 'warn',
                            'target': entry.get('ticket_id'),
                            'detail': '未接手: work exists without a responsible member',
                            'owner': None, 'next': 'assign explicitly, never infer an owner'})
    doc['attention'] = entries


def _unknowns(doc):
    unknowns = []
    if not doc['transport'].get('implemented'):
        unknowns.append('transport section is not reporting from a live transport store')
    if doc['events'].get('coverage') == 'unknown':
        unknowns.append('event coverage unknown: no facts recorded yet (hook layer needs the plugin manifest)')
    if doc['events'].get('coverage') == 'partial':
        unknowns.append('event coverage partial: revisions gapped or the live read disagreed; live read used')
    if not doc['shop'].get('bound'):
        unknowns.append('no bound run: work assignment cannot be derived from a snapshot alone')
    for row in doc['members']:
        if (row.get('observed') or {}).get('status') in UNOBSERVED:
            unknowns.append('member %s observed as %s' % (row.get('name'),
                                                          (row.get('observed') or {}).get('status')))
    doc['unknowns'] = unknowns[:32]


def _fit(doc, text):
    return len(text.encode('utf-8')) <= LIMITS['total_bytes']


def _dumps(doc):
    return json.dumps(doc, ensure_ascii=False, indent=2, sort_keys=True)


def serialize(doc):
    """Deterministic, bounded serialization with an explicit truncation entry."""
    doc = copy.deepcopy(doc)
    text = _dumps(doc)
    if _fit(doc, text):
        return text
    doc['truncated'] = True
    if not any(entry.get('code') == 'snapshot_truncated' for entry in doc.get('attention', [])):
        doc.setdefault('attention', []).append({
            'code': 'snapshot_truncated', 'severity': 'warn', 'target': 'snapshot',
            'detail': 'snapshot exceeded %d bytes; lists were shortened' % LIMITS['total_bytes'],
            'owner': None, 'next': 'use the ticket/result files for the full detail'})
    reducers = [
        lambda d: [row.__setitem__('tickets', []) for row in d.get('members', [])],
        lambda d: d.__setitem__('members', d.get('members', [])[:8]),
        lambda d: d.__setitem__('links', d.get('links', [])[:8]),
        lambda d: d.__setitem__('unregistered_owner_tickets', []),
        lambda d: d.__setitem__('attention',
                                [e for e in d.get('attention', []) if e.get('code') == 'snapshot_truncated']
                                + [e for e in d.get('attention', []) if e.get('code') != 'snapshot_truncated'][:15]),
        lambda d: d.__setitem__('unknowns', d.get('unknowns', [])[:8]),
        lambda d: d.__setitem__('members', []),
    ]
    for reducer in reducers:
        reducer(doc)
        text = _dumps(doc)
        if _fit(doc, text):
            return text
    minimal = {'schema': SCHEMA, 'generated_at': doc.get('generated_at'), 'truncated': True,
               'attention': [{'code': 'snapshot_truncated', 'severity': 'warn', 'target': 'snapshot',
                              'detail': 'snapshot could not fit the byte cap', 'owner': None,
                              'next': 'read the ticket files directly'}],
               'limits': dict(LIMITS)}
    return _dumps(minimal)


def snapshot_bytes(doc):
    return len(serialize(doc).encode('utf-8'))


def stale(doc, now=None):
    age = age_seconds(doc.get('generated_at'), now)
    return {'stale': bool(age is not None and age > STALE_SNAPSHOT_SECONDS), 'age_seconds': age}


def summarize(doc):
    """Short human/console view used by the Pi commands (bounded)."""
    lines = ['%s · %s · run %s · phase %s' % (doc.get('schema'), doc['shop'].get('shop_id'),
                                              doc['shop'].get('run_id') or 'unbound',
                                              doc['shop'].get('phase'))]
    for row in doc.get('members', []):
        observed = row.get('observed') or {}
        lines.append('  %-14s %-9s %-22s %s [%s]' % (row.get('role'), observed.get('status'),
                                                     row.get('name'), row.get('pane'),
                                                     observed.get('source')))
        for ticket in row.get('tickets', []):
            lines.append('    %-8s a%s %-10s %-11s %s' % (ticket.get('ticket_id'), ticket.get('attempt'),
                                                          ticket.get('status'),
                                                          ticket.get('responsibility', {}).get('state'),
                                                          (ticket.get('objective') or '')[:60]))
    events_section = doc.get('events') or {}
    lines.append('events: %s · revision %s · facts %s' % (events_section.get('coverage'),
                                                          events_section.get('revision'),
                                                          events_section.get('fact_count', 0)))
    transport = doc.get('transport') or {}
    lines.append('transport: %s · pending %s · unknown %s · broker %s'
                 % ('reported' if transport.get('implemented') else 'unavailable',
                    transport.get('pending', 0), transport.get('unknown', 0),
                    'live' if (transport.get('broker') or {}).get('live') else 'not running'))
    handoff = doc.get('handoff') or {}
    lines.append('handoff: %s · open %s' % ('reported' if handoff.get('implemented') else 'unavailable',
                                            len(handoff.get('open') or [])))
    if doc.get('attention'):
        lines.append('attention:')
        for entry in doc['attention'][:12]:
            lines.append('  [%s] %s %s — %s' % (entry.get('severity'), entry.get('code'),
                                                entry.get('target'), entry.get('detail')))
    if doc.get('unknowns'):
        lines.append('unknowns: ' + '; '.join(doc['unknowns'][:6]))
    return '\n'.join(lines)[:4000]
