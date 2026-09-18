"""Thin Shop handoff state machine, separate from tickets and from light messages.

States: proposed -> accepted | rejected | cancellation_requested;
accepted -> delivered | blocked | cancellation_requested;
blocked -> accepted | cancellation_requested;
cancellation_requested -> cancelled.
Idempotent by ``handoff_id``: the same id with the same content replays the
recorded state, the same id with different content is refused. Records live in a
per-run ``handoff/`` directory and never touch canonical tickets.
"""
from pathlib import Path
import re

import contracts as ct

STATES = ('proposed', 'needs_context', 'accepted', 'rejected', 'delivered', 'blocked',
          'cancellation_requested', 'cancelled')
TRANSITIONS = {
    'proposed': ('accepted', 'needs_context', 'rejected', 'cancellation_requested'),
    'needs_context': ('accepted', 'rejected', 'cancellation_requested'),
    'accepted': ('delivered', 'blocked', 'cancellation_requested'),
    'blocked': ('accepted', 'cancellation_requested'),
    'cancellation_requested': ('cancelled',),
    'rejected': (),
    'delivered': (),
    'cancelled': (),
}
# Alias actions -> resulting state, so callers never pass a free-form state.
ACTIONS = {
    'propose': 'proposed',
    'accept': 'accepted',
    'needs_context': 'needs_context',
    'reject': 'rejected',
    'deliver': 'delivered',
    'block': 'blocked',
    'request-cancel': 'cancellation_requested',
    'cancel': 'cancelled',
}


class HandoffError(RuntimeError):
    pass


def handoff_dir(repo, run_id):
    folder = ct.run_path(repo, run_id) / 'handoff'
    return folder


def handoff_file(repo, run_id, handoff_id):
    if not isinstance(handoff_id, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', handoff_id):
        raise HandoffError('Unsafe handoff_id')
    return handoff_dir(repo, run_id) / (handoff_id + '.json')


def propose(repo, run_id, handoff_id, ticket_id=None, message_id=None, summary='', payload=None):
    """Create or replay a handoff record. Never mutates tickets."""
    path = handoff_file(repo, run_id, handoff_id)
    body = {'summary': str(summary), 'payload': payload or {}, 'ticket_id': ticket_id, 'message_id': message_id}
    if path.exists():
        record = ct.load(path)
        if record.get('content') != body:
            raise HandoffError('handoff_id already exists with different content; retry uses a new id')
        return record
    record = {
        'handoff_id': handoff_id,
        'handoff_format': 1,
        'state': 'proposed',
        'ticket_id': ticket_id,
        'message_id': message_id,
        'content': body,
        'created_at': ct.stamp(),
        'history': [{'state': 'proposed', 'at': ct.stamp()}],
        'note': 'Handoff is not a ticket and is not transport delivery evidence.',
    }
    ct.atomic(path, record)
    return record


def transition(repo, run_id, handoff_id, action, detail=None, actor=None):
    path = handoff_file(repo, run_id, handoff_id)
    if not path.exists():
        raise HandoffError('Unknown handoff ' + handoff_id)
    if action not in ACTIONS:
        raise HandoffError('Unknown handoff action ' + str(action))
    target = ACTIONS[action]
    record = ct.load(path)
    current = record.get('state')
    if current not in STATES:
        raise HandoffError('Handoff record has invalid state ' + str(current))
    if current == target:
        return record  # Idempotent replay, no duplicate history entry.
    if target not in TRANSITIONS[current]:
        raise HandoffError('Illegal handoff transition %s -> %s' % (current, target))
    record['state'] = target
    record['updated_at'] = ct.stamp()
    entry = {'state': target, 'at': ct.stamp()}
    if detail:
        entry['detail'] = str(detail)
    if actor:
        entry['actor'] = str(actor)
    record.setdefault('history', []).append(entry)
    ct.atomic(path, record)
    return record


def load(repo, run_id, handoff_id):
    return ct.load(handoff_file(repo, run_id, handoff_id))


def listing(repo, run_id):
    folder = handoff_dir(repo, run_id)
    return [ct.load(path) for path in sorted(folder.glob('*.json'))]


def summary(repo, run_id):
    """Read-only handoff facts for the observability snapshot."""
    counts = {state: 0 for state in STATES}
    open_records = []
    try:
        records = listing(repo, run_id)
    except Exception as error:  # noqa: BLE001 - report unknown, never guess
        return {'implemented': True, 'reason': 'handoff records unreadable: ' + str(error),
                'open': [], 'counts': counts}
    for record in records:
        state = record.get('state')
        if state in counts:
            counts[state] += 1
        if state in ('proposed', 'needs_context', 'accepted', 'blocked', 'cancellation_requested'):
            open_records.append({'handoff_id': record.get('handoff_id'), 'task_ref': record.get('ticket_id'),
                                 'message_id': record.get('message_id'), 'status': state,
                                 'created_at': record.get('created_at')})
    return {'implemented': True, 'reason': None, 'open': open_records[:64], 'counts': counts,
            'note': 'handoff is not a ticket and is not transport delivery evidence'}
