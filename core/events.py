#!/usr/bin/env python3
"""Bounded Herdr event hook: append validated facts, never authorize anything.

Invoked by Herdr plugin `[[events]]` hooks (see `herdr-plugin.toml`) with
`HERDR_PLUGIN_EVENT` / `HERDR_PLUGIN_EVENT_JSON`. Event handling rules:

* short-lived, no locks held while waiting, no Herdr call, no model, no network;
* strictly validated payloads; an unknown/foreign/invalid event is a quiet no-op
  with exit 0 (storage failure is the only non-zero exit);
* one bounded JSON line per fact in ``<STATE>/facts/<shop_id>.jsonl`` with a
  monotonic per-shop ``revision`` so a reader can detect gaps;
* facts are display/observation input only. ``identity.resolve()`` and the live
  read stay the only authority; nothing here can accept, dispatch or close work.
"""
import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sys

FACT_SCHEMA = 'shop.event_fact/v1'
MAX_LINES = 2000
MAX_FILE_BYTES = 512 * 1024
MAX_LINE_BYTES = 1024

# Frozen by Herdr protocol 22 (EventKind / EventData / AgentStatus). Copied
# deliberately instead of importing herdr.py so the hook stays tiny and cannot
# trigger a binary/protocol probe on every pane status change.
SUBSCRIBED = {
    'pane_agent_status_changed': 'pane.agent_status_changed',
    'pane_closed': 'pane.closed',
    'pane_exited': 'pane.exited',
}
AGENT_STATUSES = ('idle', 'working', 'blocked', 'done', 'unknown')
PAYLOAD_KEYS = {
    'pane_agent_status_changed': ('type', 'pane_id', 'workspace_id', 'agent_status'),
    'pane_closed': ('type', 'pane_id', 'workspace_id'),
    'pane_exited': ('type', 'pane_id', 'workspace_id'),
}


def stamp(now=None):
    return (now or dt.datetime.now(dt.timezone.utc)).isoformat()


def facts_dir(state_dir):
    return Path(state_dir) / 'facts'


def facts_file(state_dir, shop_id):
    safe = ''.join(char if char.isalnum() or char in '-_' else '-' for char in str(shop_id))[:64]
    return facts_dir(state_dir) / (safe + '.jsonl')


def state_key(env):
    socket = env.get('HERDR_SOCKET_PATH') or ''
    tab = env.get('HERDR_TAB_ID') or ''
    if not tab:
        return None
    return hashlib.sha256((socket + ':' + tab).encode()).hexdigest()[:12]


def lookup_state(state_dir, env):
    """Read-only shop registration for this hook's tab; no files are created."""
    key = state_key(env)
    if not key:
        return None
    path = Path(state_dir) / 'runtime' / (key + '.json')
    try:
        if path.is_symlink() or path.stat().st_size > 65536:
            return None
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not data.get('shop_id'):
        return None
    return {'shop_id': data['shop_id'], 'run_id': data.get('run_id'),
            'tab': data.get('tab'), 'path': str(path)}


def normalize_event(name):
    """Accept the manifest hook name (dot or underscore) and return the frozen key."""
    if not isinstance(name, str):
        return None
    key = name.strip().replace('.', '_')
    return key if key in SUBSCRIBED else None


def validate(event, payload):
    """Strict payload validation. Returns (fact_fields, None) or (None, reason)."""
    if not is_record(payload):
        return None, 'payload is not an object'
    required = PAYLOAD_KEYS[event]
    missing = [key for key in required if key not in payload]
    if missing:
        return None, 'missing ' + ','.join(missing)
    for key in ('pane_id', 'workspace_id'):
        if not isinstance(payload.get(key), str) or not payload[key].strip():
            return None, key + ' must be a nonempty string'
    if event == 'pane_agent_status_changed':
        if payload.get('agent_status') not in AGENT_STATUSES:
            return None, 'agent_status ' + repr(payload.get('agent_status')) + ' is not a known lifecycle state'
    for key in ('agent', 'display_agent', 'title'):
        if payload.get(key) is not None and not isinstance(payload[key], str):
            return None, key + ' must be a string or null'
    if payload.get('state_labels') is not None and not is_record(payload['state_labels']):
        return None, 'state_labels must be an object'
    fact = {'event': SUBSCRIBED[event], 'pane_id': payload['pane_id'],
            'workspace_id': payload['workspace_id']}
    for key in ('agent_status', 'agent', 'display_agent', 'title'):
        if payload.get(key) is not None:
            fact[key] = payload[key]
    return fact, None


def is_record(value):
    return isinstance(value, dict) and not isinstance(value, bool)


def _read_lines(path):
    try:
        return path.read_text().splitlines()
    except OSError:
        return []


def _rewrite(path, lines):
    tmp = path.with_name(path.name + '.tmp')
    tmp.write_text('\n'.join(lines) + ('\n' if lines else ''))
    os.replace(tmp, path)


def prune(path):
    """Keep the newest lines inside both caps; oldest facts are dropped first."""
    lines = [line for line in _read_lines(path) if line.strip()]
    changed = False
    if len(lines) > MAX_LINES:
        lines = lines[-MAX_LINES:]
        changed = True
    while lines and len(('\n'.join(lines) + '\n').encode('utf-8')) > MAX_FILE_BYTES:
        lines = lines[1:]
        changed = True
    if changed:
        _rewrite(path, lines)
    return lines


def record(state_dir, env, event_name, payload, now=None):
    """Append one validated fact. Returns (fact, None) or (None, reason)."""
    event = normalize_event(event_name)
    if not event:
        return None, 'not a subscribed event: ' + repr(event_name)
    registration = lookup_state(state_dir, env)
    if not registration:
        return None, 'no registered shop for this tab; fact dropped'
    fields, reason = validate(event, payload)
    if not fields:
        return None, reason
    folder = facts_dir(state_dir)
    folder.mkdir(parents=True, exist_ok=True)
    path = facts_file(state_dir, registration['shop_id'])
    lock = path.with_suffix('.lock')
    with lock.open('a+') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        lines = prune(path)
        revision = 1
        if lines:
            try:
                revision = int(json.loads(lines[-1]).get('revision', 0)) + 1
            except (ValueError, AttributeError, TypeError):
                revision = len(lines) + 1
        fact = dict(fields, schema=FACT_SCHEMA, revision=revision, at=stamp(now),
                    shop_id=registration['shop_id'], run_id=registration.get('run_id'),
                    tab_id=env.get('HERDR_TAB_ID'), source='herdr_event')
        line = json.dumps(fact, ensure_ascii=False, sort_keys=True)
        if len(line.encode('utf-8')) > MAX_LINE_BYTES:
            return None, 'fact line exceeds %d bytes' % MAX_LINE_BYTES
        with path.open('a') as stream:
            stream.write(line + '\n')
        prune(path)
    return fact, None


def read_facts(state_dir, shop_id, limit=MAX_LINES):
    """Bounded read with revision-gap detection. Never raises on a missing store."""
    path = facts_file(state_dir, shop_id) if shop_id else None
    section = {'coverage': 'unknown', 'last_event_at': None, 'revision': None,
               'subscriptions': sorted(SUBSCRIBED.values()), 'facts': [], 'gaps': [],
               'store': str(path) if path else None,
               'authority': 'display-only observation; live reads remain authoritative',
               'reason': 'no event facts recorded yet; the hook layer needs the plugin manifest '
                         'installed and Herdr to emit pane events'}
    if not path or not path.is_file():
        return section
    facts = []
    for line in _read_lines(path)[-limit:]:
        try:
            data = json.loads(line)
        except ValueError:
            continue
        if isinstance(data, dict) and data.get('schema') == FACT_SCHEMA:
            facts.append(data)
    if not facts:
        return section
    revisions = [fact.get('revision') for fact in facts if isinstance(fact.get('revision'), int)]
    gaps = []
    for previous, current in zip(revisions, revisions[1:]):
        if current != previous + 1:
            gaps.append({'from': previous, 'to': current})
    section.update({
        'coverage': 'partial' if gaps else 'observed',
        'last_event_at': facts[-1].get('at'),
        'revision': revisions[-1] if revisions else None,
        'facts': facts,
        'gaps': gaps,
        'reason': None if not gaps else 'revision gap detected; events may have been missed',
    })
    return section


def read_facts_for_state(state_path):
    """Read facts for the shop registered in one runtime state file."""
    try:
        data = json.loads(Path(state_path).read_text())
    except (OSError, ValueError):
        return read_facts(Path(state_path).parent.parent, None)
    if not isinstance(data, dict) or not data.get('shop_id'):
        return read_facts(Path(state_path).parent.parent, None)
    return read_facts(Path(state_path).parent.parent, data['shop_id'])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['record'])
    parser.add_argument('--event', help='Hook event name (defaults to HERDR_PLUGIN_EVENT)')
    parser.add_argument('--payload', help='Hook payload JSON (defaults to HERDR_PLUGIN_EVENT_JSON)')
    parser.add_argument('--state-dir', help='Shop state dir (defaults to SHOP_STATE_DIR)')
    args = parser.parse_args(argv)

    env = dict(os.environ)
    state_dir = args.state_dir or env.get('SHOP_STATE_DIR') or env.get('HERDR_PLUGIN_STATE_DIR') \
        or str(Path.home() / '.local/state/shop-workstation')
    raw = args.payload if args.payload is not None else env.get('HERDR_PLUGIN_EVENT_JSON')
    event = args.event or env.get('HERDR_PLUGIN_EVENT')
    if raw is None:
        print(json.dumps({'recorded': False, 'reason': 'no HERDR_PLUGIN_EVENT_JSON provided'}))
        return 0
    try:
        payload = json.loads(raw)
    except ValueError as error:
        print(json.dumps({'recorded': False, 'reason': 'invalid JSON: ' + str(error)[:200]}))
        return 0
    try:
        fact, reason = record(state_dir, env, event, payload)
    except OSError as error:
        print(json.dumps({'recorded': False, 'reason': 'storage failure: ' + str(error)[:200]}),
              file=sys.stderr)
        return 1
    print(json.dumps({'recorded': fact is not None, 'reason': reason,
                      'revision': (fact or {}).get('revision'),
                      'event': (fact or {}).get('event')}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    sys.exit(main())
