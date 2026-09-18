"""Shop-owned business side of the built-in transport: identity verification,
durable dedupe and crash-window states. No transport I/O, no subprocess, no
model calls, no ticket mutation.

Transport carries identity and an opaque payload; Shop decides. A message is
durably recorded as ``received`` *before* the extension is told to inject, so a
crash between the record and the Pi injection leaves ``received``/``unknown``
and is never automatically re-injected.
"""
import contextlib
import fcntl
import hashlib
import json
from pathlib import Path
import re
import uuid

import contracts as ct

SCHEMA = 'shop-transport-v1'
DEDUPE_FORMAT = 1
MAX_RECORDS = 20000
MAX_BODY_BYTES = 32 * 1024
KINDS = ('note', 'task_notice', 'handoff')
STATES = ('received', 'injected', 'business_accepted', 'business_rejected', 'unknown')
# Monotonic durable transitions. `unknown` never auto-resolves back.
ALLOWED_TRANSITIONS = {
    'received': ('injected', 'business_rejected', 'unknown'),
    'injected': ('business_accepted', 'business_rejected', 'unknown'),
    'business_accepted': ('unknown',),
    'business_rejected': ('unknown',),
    'unknown': ('unknown',),
}
ENVELOPE_KEYS = ('schema', 'message_id', 'shop_id', 'run_id', 'sender', 'recipient',
                 'kind', 'reply_to', 'body', 'body_sha256')


class TransportReject(RuntimeError):
    """Business refusal with a frozen transport error code."""

    def __init__(self, code, detail):
        super().__init__(code + ': ' + detail)
        self.code = code
        self.detail = detail


def canonical_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def sha256_hex(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def transport_dir(repo, run_id):
    return ct.run_path(repo, run_id) / 'transport'


def dedupe_file(repo, run_id):
    return transport_dir(repo, run_id) / 'dedupe.json'


def dedupe_lock(repo, run_id):
    return transport_dir(repo, run_id) / 'dedupe.lock'


@contextlib.contextmanager
def store_lock(repo, run_id):
    folder = transport_dir(repo, run_id)
    folder.mkdir(parents=True, exist_ok=True)
    path = dedupe_lock(repo, run_id)
    with path.open('a+') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def load_store(repo, run_id):
    path = dedupe_file(repo, run_id)
    if not path.exists():
        return {'formatVersion': DEDUPE_FORMAT, 'shop_id': None, 'run_id': run_id, 'records': {}}
    data = ct.load(path)
    if data.get('formatVersion') != DEDUPE_FORMAT:
        raise TransportReject('E_MALFORMED', 'dedupe store formatVersion mismatch; manual inspection required')
    if data.get('run_id') != run_id:
        raise TransportReject('E_RUN_MISMATCH', 'dedupe store belongs to another run')
    if not isinstance(data.get('records'), dict):
        raise TransportReject('E_MALFORMED', 'dedupe store has no records object')
    return data


def save_store(repo, run_id, store):
    ct.atomic(dedupe_file(repo, run_id), store)


def _require_keys(value, keys, where):
    if not isinstance(value, dict):
        raise TransportReject('E_MALFORMED', where + ' must be an object')
    unknown = set(value) - set(keys)
    if unknown:
        raise TransportReject('E_MALFORMED', where + ' has unknown field(s): ' + ','.join(sorted(unknown)))
    missing = [key for key in keys if key not in value]
    if missing:
        raise TransportReject('E_MALFORMED', where + ' is missing ' + ','.join(missing))


def _member_ref(value, where):
    _require_keys(value, ('member_id', 'launch_id'), where)
    for key in ('member_id', 'launch_id'):
        if not isinstance(value[key], str) or not value[key].strip():
            raise TransportReject('E_IDENTITY_INCOMPLETE', where + '.' + key + ' must be a nonempty string')
    return {'member_id': value['member_id'], 'launch_id': value['launch_id']}


def verify_envelope(state, envelope, self_name, self_pane=None):
    """Fail-closed validation of one business envelope against the roster.

    Returns the typed envelope. Raises TransportReject with a frozen code, so a
    malformed or foreign frame can never reach the model.
    """
    _require_keys(envelope, ENVELOPE_KEYS, 'envelope')
    if envelope['schema'] != SCHEMA:
        raise TransportReject('E_MALFORMED', 'envelope.schema must be ' + SCHEMA)
    if not isinstance(envelope['message_id'], str) or not envelope['message_id'].strip():
        raise TransportReject('E_MALFORMED', 'envelope.message_id must be a nonempty string')
    if envelope['shop_id'] != state.get('shop_id'):
        raise TransportReject('E_SHOP_MISMATCH', 'envelope.shop_id does not match this workstation')
    if envelope['run_id'] != state.get('run_id'):
        raise TransportReject('E_RUN_MISMATCH', 'envelope.run_id does not match the bound run')
    if envelope['kind'] not in KINDS:
        raise TransportReject('E_MALFORMED', 'envelope.kind is unknown: ' + str(envelope['kind']))
    if envelope['reply_to'] is not None and not isinstance(envelope['reply_to'], str):
        raise TransportReject('E_MALFORMED', 'envelope.reply_to must be a string or null')
    sender = _member_ref(envelope['sender'], 'envelope.sender')
    recipient = _member_ref(envelope['recipient'], 'envelope.recipient')
    _require_keys(envelope['body'], ('text',), 'envelope.body')
    if not isinstance(envelope['body']['text'], str):
        raise TransportReject('E_MALFORMED', 'envelope.body.text must be a string')
    body_bytes = len(envelope['body']['text'].encode('utf-8'))
    if body_bytes > MAX_BODY_BYTES:
        raise TransportReject('E_PAYLOAD_TOO_LARGE', 'body is %d bytes, limit %d' % (body_bytes, MAX_BODY_BYTES))
    if envelope['body_sha256'] != sha256_hex(envelope['body']['text']):
        raise TransportReject('E_MALFORMED', 'envelope.body_sha256 does not match body text')

    roster = {member['name']: (role, member) for role, member in _roster(state)}
    if recipient['member_id'] != self_name:
        raise TransportReject('E_RUN_MISMATCH', 'envelope recipient is not this member')
    if self_name not in roster:
        raise TransportReject('E_IDENTITY_INCOMPLETE', 'self member is not registered in this workstation')
    self_record = roster[self_name][1]
    if self_pane and self_record.get('pane') != self_pane:
        raise TransportReject('E_RUN_MISMATCH', 'pane identity does not match the registered member')
    if self_record.get('launch_id') != recipient['launch_id']:
        raise TransportReject('E_RUN_MISMATCH', 'recipient launch epoch is stale or unknown')
    sender_record = roster.get(sender['member_id'])
    if not sender_record:
        raise TransportReject('E_IDENTITY_INCOMPLETE', 'sender is not a registered member')
    if sender_record[1].get('launch_id') != sender['launch_id']:
        raise TransportReject('E_RUN_MISMATCH', 'sender launch epoch is stale or unknown')
    if sender['member_id'] == recipient['member_id'] and sender['launch_id'] == recipient['launch_id']:
        raise TransportReject('E_SELF_TARGET', 'sender and recipient are the same endpoint')
    return {
        'schema': SCHEMA,
        'message_id': envelope['message_id'],
        'shop_id': envelope['shop_id'],
        'run_id': envelope['run_id'],
        'sender': sender,
        'recipient': recipient,
        'kind': envelope['kind'],
        'reply_to': envelope['reply_to'],
        'body': {'text': envelope['body']['text']},
        'body_sha256': envelope['body_sha256'],
        'sender_role': sender_record[0],
    }


def _roster(state):
    return [('architect', state['architect']), ('lead', state['lead']),
            *[('auxiliary_lead', item) for item in state.get('extra_leads', [])],
            *[('worker', item) for item in state.get('workers', [])]]


def _record_key(message_id):
    return ct.ident(message_id)


def decide(repo, run_id, state, envelope, self_name, self_pane=None):
    """Verify, then durably decide inject/duplicate/reject under one lock."""
    typed = verify_envelope(state, envelope, self_name, self_pane)
    if not isinstance(typed['message_id'], str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', typed['message_id']):
        raise TransportReject('E_MALFORMED', 'message_id must be a safe identifier')
    with store_lock(repo, run_id):
        store = load_store(repo, run_id)
        records = store['records']
        record = records.get(typed['message_id'])
        if record:
            if record.get('body_sha256') != typed['body_sha256']:
                record.setdefault('conflicts', []).append({
                    'at': ct.stamp(), 'body_sha256': typed['body_sha256'],
                    'detail': 'same message_id with different content; refused for both sides',
                })
                save_store(repo, run_id, store)
                raise TransportReject('E_MESSAGE_ID_REUSE',
                                      'message_id ' + typed['message_id'] + ' was already used with different content')
            return {'decision': 'duplicate', 'message_id': typed['message_id'],
                    'state': record.get('state'), 'record': record,
                    'dedupe_path': str(dedupe_file(repo, run_id)),
                    'note': 'already recorded; no second injection, no auto re-injection'}
        record = {
            'message_id': typed['message_id'],
            'body_sha256': typed['body_sha256'],
            'shop_id': typed['shop_id'],
            'run_id': typed['run_id'],
            'sender': typed['sender'],
            'sender_role': typed['sender_role'],
            'recipient': typed['recipient'],
            'kind': typed['kind'],
            'reply_to': typed['reply_to'],
            'state': 'received',
            'received_at': ct.stamp(),
            'history': [{'state': 'received', 'at': ct.stamp()}],
        }
        records[typed['message_id']] = record
        _prune(store)
        # Written before returning `inject`: a crash after this write but before
        # the Pi injection leaves `received` and is never auto-injected.
        save_store(repo, run_id, store)
        return {'decision': 'inject', 'message_id': typed['message_id'], 'state': 'received',
                'body': typed['body']['text'], 'kind': typed['kind'], 'reply_to': typed['reply_to'],
                'sender': typed['sender'], 'sender_role': typed['sender_role'],
                'dedupe_path': str(dedupe_file(repo, run_id))}


def prepare(repo, run_id, state, sender, recipient, kind, text, reply_to=None):
    """Journal an unsent notice. Only the calling Pi endpoint may transmit it."""
    envelope = {'schema': SCHEMA, 'message_id': uuid.uuid4().hex,
                'shop_id': state['shop_id'], 'run_id': run_id,
                'sender': {'member_id': sender['name'], 'launch_id': sender.get('launch_id')},
                'recipient': {'member_id': recipient['name'], 'launch_id': recipient.get('launch_id')},
                'kind': kind, 'reply_to': reply_to, 'body': {'text': text}, 'body_sha256': sha256_hex(text)}
    verify_envelope(state, envelope, recipient['name'], recipient['pane'])
    with store_lock(repo, run_id):
        path = transport_dir(repo, run_id) / 'outbox.json'
        store = ct.load(path) if path.exists() else {}
        if len(store) >= MAX_RECORDS:
            raise TransportReject('E_RATE_LIMITED', 'outbox full; inspect retention before sending')
        store[envelope['message_id']] = {key: value for key, value in envelope.items() if key != 'body'}
        store[envelope['message_id']].update(submission='prepared', receipt=None, created_at=ct.stamp())
        ct.atomic(path, store)
    return envelope


def outgoing(repo, run_id, message_id, status, actor):
    if status not in ('not_submitted', 'unknown', 'submitted', 'delivered',
                      'receiver_received', 'injected', 'rejected'):
        raise TransportReject('E_MALFORMED', 'invalid outgoing outcome')
    with store_lock(repo, run_id):
        path = transport_dir(repo, run_id) / 'outbox.json'
        store = ct.load(path) if path.exists() else {}
        record = store.get(message_id)
        if not record or record['sender'] != {'member_id': actor['name'], 'launch_id': actor.get('launch_id')}:
            raise TransportReject('E_UNKNOWN_MESSAGE', 'not an outgoing message of this launch')
        if status in ('receiver_received', 'injected', 'rejected'):
            if record.get('receipt') not in ('injected', 'rejected'):
                record['receipt'] = status
        else:
            record['submission'] = status
        record['updated_at'] = ct.stamp()
        ct.atomic(path, store)
        return record


def _prune(store):
    records = store['records']
    if len(records) <= MAX_RECORDS:
        return
    # Oldest first, but an unresolved `unknown` record is always retained.
    ordered = sorted((item for item in records.items() if item[1].get('state') != 'unknown'),
                     key=lambda item: item[1].get('received_at', ''))
    for message_id, _ in ordered:
        if len(records) <= MAX_RECORDS:
            break
        del records[message_id]


def transition(repo, run_id, message_id, state, detail=None):
    """Monotonic durable state transition. `unknown` never auto-resolves."""
    if state not in STATES:
        raise TransportReject('E_MALFORMED', 'unknown record state ' + str(state))
    with store_lock(repo, run_id):
        store = load_store(repo, run_id)
        record = store['records'].get(message_id)
        if not record:
            raise TransportReject('E_UNKNOWN_MESSAGE', 'no durable record for ' + str(message_id))
        current = record.get('state')
        if current not in STATES:
            raise TransportReject('E_MALFORMED', 'record has invalid state ' + str(current))
        if current == state:
            return record
        if state not in ALLOWED_TRANSITIONS.get(current, ()):
            raise TransportReject('E_MALFORMED',
                                  'illegal durable transition %s -> %s' % (current, state))
        record['state'] = state
        record.setdefault('history', []).append({'state': state, 'at': ct.stamp(),
                                                 **({'detail': detail} if detail else {})})
        record['updated_at'] = ct.stamp()
        save_store(repo, run_id, store)
        return record


def record_receipt(repo, run_id, message_id, status, detail=None):
    """Map a wire receipt onto the durable state without inventing delivery.

    ``unknown`` is the injection-uncertainty state: it never auto-resolves and
    must never be recorded as a known business rejection.
    """
    if status == 'receiver_received':
        return record_touch(repo, run_id, message_id, 'receiver_received', detail)
    if status == 'injected':
        return transition(repo, run_id, message_id, 'injected', detail)
    if status == 'unknown':
        return transition(repo, run_id, message_id, 'unknown',
                          detail or 'injection outcome unknown; never auto-injected')
    if status == 'rejected':
        return transition(repo, run_id, message_id, 'business_rejected', detail or 'refused before injection')
    raise TransportReject('E_MALFORMED', 'unknown receipt status ' + str(status))


def record_touch(repo, run_id, message_id, label, detail=None):
    with store_lock(repo, run_id):
        store = load_store(repo, run_id)
        record = store['records'].get(message_id)
        if not record:
            raise TransportReject('E_UNKNOWN_MESSAGE', 'no durable record for ' + str(message_id))
        record.setdefault('history', []).append({'state': record.get('state'), 'at': ct.stamp(),
                                                 'receipt': label,
                                                 **({'detail': detail} if detail else {})})
        record['updated_at'] = ct.stamp()
        save_store(repo, run_id, store)
        return record


def status(repo, run_id):
    store = load_store(repo, run_id)
    counts = {}
    for record in store['records'].values():
        counts[record.get('state')] = counts.get(record.get('state'), 0) + 1
    return {'run_id': run_id, 'records': len(store['records']), 'states': counts,
            'dedupe_path': str(dedupe_file(repo, run_id))}


def record(repo, run_id, message_id):
    return load_store(repo, run_id)['records'].get(message_id)


def unresolved(repo, run_id):
    """Records that were received/durably recorded but never confirmed injected."""
    store = load_store(repo, run_id)
    return [record for record in store['records'].values()
            if record.get('state') in ('received', 'unknown')]


def _broker_liveness(transport_root):
    """Read-only broker facts: stat the pid file and probe the PID, never spawn."""
    pid_path = transport_root / 'broker.pid'
    socket_path = transport_root / 'transport.sock'
    broker = {'live': False, 'pid': None, 'broker_epoch': None, 'age_seconds': None,
              'socket_present': socket_path.exists(), 'note': None}
    if not pid_path.is_file():
        broker['note'] = 'no broker pid file; broker is not running'
        return broker
    try:
        pid = int(pid_path.read_text().strip())
    except (OSError, ValueError):
        broker['note'] = 'unreadable broker pid file; treat as unknown, never kill blindly'
        return broker
    broker['pid'] = pid
    try:
        import datetime as dt
        broker['age_seconds'] = max(0, int(dt.datetime.now(dt.timezone.utc).timestamp()
                                          - pid_path.stat().st_mtime))
    except OSError:
        broker['age_seconds'] = None
    try:
        import os as os_module
        os_module.kill(pid, 0)
        broker['live'] = True
    except OSError:
        broker['note'] = 'pid file present but process is not live; next launch replaces stale files'
    # broker_epoch lives in the running broker's memory only; never guessed here.
    broker['broker_epoch'] = None
    return broker


def summary(repo, run_id):
    """Read-only transport facts for the observability snapshot.

    Never connects to the broker, never reads a message body, never spawns.
    Peer lists require a live broker query and are reported as unavailable
    instead of being invented.
    """
    root = transport_dir(repo, run_id)
    store = {'records': {}}
    reason = None
    if dedupe_file(repo, run_id).is_file():
        try:
            store = load_store(repo, run_id)
        except Exception as error:  # noqa: BLE001 - report unknown, never guess
            reason = 'dedupe store unreadable: ' + str(error)
    records = list((store.get('records') or {}).values())
    pending = sum(1 for record in records if record.get('state') == 'received')
    unknown = sum(1 for record in records if record.get('state') == 'unknown')
    return {
        'implemented': True,
        'reason': reason,
        'endpoint': {'kind': 'unix-socket', 'dir': str(root), 'protocol': 'pi-shop-transport',
                     'version': 1, 'exists': root.is_dir()},
        'broker': _broker_liveness(root),
        'peers': [],
        'peers_reason': 'peer registry lives in broker memory; snapshot does not connect to the broker',
        'pending': pending,
        'unknown': unknown,
        'records': len(records),
        'redaction': 'message bodies and payload hashes are never read by the snapshot',
    }
