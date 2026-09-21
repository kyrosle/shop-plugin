#!/usr/bin/env python3
"""Explicit Shop workbench actions. Read-only views never execute a plan.

Python owns business records. Pi owns user confirmation and runtime model APIs.
No timers, automatic dispatch, retries, merging, process termination or cleanup.
"""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import time
import uuid

import contracts as ct
import coordination as co
import handoff
import identity
import locking
import lifecycle
import settings
import snapshot
import transport

LIMIT = 64 * 1024


def bounded(path):
    path = Path(path)
    if path.stat().st_size > LIMIT:
        raise RuntimeError('Document exceeds 64 KiB: ' + str(path))
    value = ct.load(path)
    if not isinstance(value, dict):
        raise RuntimeError('Expected object: ' + str(path))
    return value


def fingerprint(value):
    return hashlib.sha256(transport.canonical_json(value).encode()).hexdigest()


def member_ref(member):
    return {key: member.get(key) for key in ('name', 'pane', 'launch_id', 'terminal_id')}


class Workbench:
    def __init__(self, api, state_path, pane):
        self.api, self.state_path = api, Path(state_path)
        self.state = bounded(state_path)
        self.actor = next((m for _, m in identity.roster(self.state) if m['pane'] == pane), None)
        if not self.actor:
            raise RuntimeError('Caller is not a registered member')
        self.repo = self.state['cwd']
        self.rid = self.state.get('run_id')

    def authorize(self, controller=False, primary=False):
        if self.state.get('phase') != 'ready' or self.state.get('recovery_required'):
            raise RuntimeError('Ready Shop required; inspect/recover registration first')
        lifecycle.require_current(self.api, self.state, self.state_path.parent.parent)
        identity.resolve(self.api, self.actor, self.state['tab'], require_terminal=True, require_status=False)
        if controller and self.actor['name'] not in (self.state['architect']['name'], self.state['lead']['name']):
            raise RuntimeError('Architect or primary Lead required')
        if primary and self.actor['name'] != self.state['lead']['name']:
            raise RuntimeError('Primary Lead required; Architect may request, not impersonate Lead')
        if not self.rid:
            raise RuntimeError('Bind a run first')
        co.binding(self.state)
        if ct.load(ct.run_path(self.repo, self.rid) / 'run.json')['status'] == 'completed':
            raise RuntimeError('Run already completed')

    def folder(self):
        if not self.rid:
            raise RuntimeError('Bind a run first')
        return ct.run_path(self.repo, self.rid) / 'workbench'

    def target(self, name):
        found = next((m for _, m in identity.roster(self.state) if m['name'] == name), None)
        if not found or found['name'] == self.actor['name']:
            raise RuntimeError('Use exact registered recipient; cannot target self')
        identity.resolve(self.api, found, self.state['tab'], require_terminal=True, require_status=False)
        return found

    def records(self):
        if not self.rid or not self.folder().exists():
            return []
        files = sorted(self.folder().glob('*.json'))
        if len(files) > 500:
            raise RuntimeError('Workbench has more than 500 records; inspect retention')
        records = [bounded(path) for path in files]
        for record in records:
            if record.get('kind') == 'handoff':
                path = handoff.handoff_file(self.repo, self.rid, record['id'])
                record['status'] = bounded(path)['state'] if path.exists() else 'unknown'
        return records

    def overview(self):
        tickets = ct.tickets(self.repo, self.rid) if self.rid else []
        document = snapshot.build(self.state, tickets, api=self.api, caller_pane=self.actor['pane'],
                                  repo=self.repo, run_id=self.rid)
        endpoints = []
        for _, member in identity.roster(self.state):
            try:
                endpoints.append({'name': member['name'], 'advertised': self.endpoint(member),
                                  'note': 'Advertisement only; broker verifies exact epoch on send'})
            except (RuntimeError, OSError, ValueError):
                endpoints.append({'name': member['name'], 'advertised': None, 'note': 'missing/stale/unverified'})
        return {'snapshot': document, 'endpoints': endpoints, 'records': self.records(),
                'handoffs': handoff.listing(self.repo, self.rid) if self.rid else [],
                'actor': self.actor['name'], 'models': self.state.get('model_profiles', {}),
                'note': 'Snapshot/receipts are facts, never task acceptance or stopped-writer proof.'}

    def endpoint(self, member):
        key = hashlib.sha256(json.dumps([self.state['shop_id'], member['name']], separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()
        path = self.state_path.parent.parent / 'endpoints' / (key + '.json')
        if not path.exists():
            raise RuntimeError('No live Pi endpoint for ' + member['name'] + '; open/bind that seat first')
        endpoint = bounded(path)
        expected = {'shop_id': self.state['shop_id'], 'run_id': self.rid, 'member_id': member['name'],
                    'launch_id': member.get('launch_id'), 'pane_id': member['pane'], 'terminal_id': member.get('terminal_id')}
        if (any(endpoint.get(key) != value for key, value in expected.items())
                or not endpoint.get('session_id') or not endpoint.get('endpoint_epoch') or not endpoint.get('broker_epoch')
                or not isinstance(endpoint.get('expires_at'), (int, float))
                or not math.isfinite(endpoint['expires_at']) or endpoint['expires_at'] < time.time() * 1000):
            raise RuntimeError('Endpoint expired or identity changed: ' + member['name'])
        return endpoint

    def save_request(self, kind, payload, recipient=None):
        record = {'id': uuid.uuid4().hex, 'kind': kind, 'status': 'requested',
                  'shop_id': self.state['shop_id'], 'run_id': self.rid, 'created_at': ct.stamp(),
                  'sender': member_ref(self.actor), 'recipient': member_ref(recipient) if recipient else None,
                  'payload': payload}
        if kind in ('handoff', 'profile'):
            record['sender_session'] = self.endpoint(self.actor)['session_id']
            record['recipient_session'] = self.endpoint(recipient)['session_id']
        ct.atomic(self.folder() / (record['id'] + '.json'), record)
        return record

    def load_request(self, request_id):
        record = bounded(self.folder() / (ct.ident(request_id) + '.json'))
        if record['shop_id'] != self.state['shop_id'] or record['run_id'] != self.rid:
            raise RuntimeError('Request belongs to another Shop/run')
        roster = {m['name']: member_ref(m) for _, m in identity.roster(self.state)}
        for key in ('sender', 'recipient'):
            ref = record.get(key)
            if ref and roster.get(ref['name']) != ref:
                raise RuntimeError('Request identity is stale; never replay into a replacement seat')
        for key in ('sender', 'recipient'):
            if record.get(key + '_session'):
                member = next(m for _, m in identity.roster(self.state) if m['name'] == record[key]['name'])
                if self.endpoint(member)['session_id'] != record[key + '_session']:
                    raise RuntimeError('Request Pi session changed; do not replay')
        return record

    def update(self, record, status, **extra):
        record.update(status=status, updated_at=ct.stamp(), **extra)
        ct.atomic(self.folder() / (ct.ident(record['id']) + '.json'), record)
        return record

    def request_handoff(self, data):
        recipient = self.target(data['recipient'])
        payload = {key: data.get(key) for key in ('objective', 'scope', 'acceptance', 'evidence', 'ticket_id')}
        if not all(isinstance(payload[key], str) and payload[key].strip() for key in ('objective', 'scope', 'acceptance')):
            raise RuntimeError('Objective, scope and acceptance required')
        if len(json.dumps(payload).encode()) > 8192:
            raise RuntimeError('Handoff payload exceeds 8 KiB')
        if payload['ticket_id']:
            ticket = ct.load(ct.ticket_path(self.repo, self.rid, payload['ticket_id']))
            if ticket['owner'] != recipient['name']:
                raise RuntimeError('Ticket owner differs from handoff recipient')
            payload['attempt'] = ticket['attempt']
        record = self.save_request('handoff', payload, recipient)
        envelope = transport.prepare(self.repo, self.rid, self.state, self.actor, recipient, 'handoff',
                                      'Handoff ' + record['id'] + '\n' + json.dumps(payload, ensure_ascii=False))
        business = handoff.propose(self.repo, self.rid, record['id'], ticket_id=payload['ticket_id'],
                                   message_id=envelope['message_id'], summary=payload['objective'], payload=payload)
        self.update(record, 'requested', message_id=envelope['message_id'])
        return {'request': record, 'handoff': business, 'transport_request': envelope,
                'transport_session': record['recipient_session']}

    def handoff_action(self, data):
        record = self.load_request(data['id'])
        if record['kind'] != 'handoff' or record['recipient'] != member_ref(self.actor):
            raise RuntimeError('Only the exact intended recipient may acknowledge a handoff')
        if record['payload'].get('ticket_id'):
            ticket = ct.load(ct.ticket_path(self.repo, self.rid, record['payload']['ticket_id']))
            if ticket['attempt'] != record['payload'].get('attempt') or ticket['status'] == 'cancelled':
                raise RuntimeError('Handoff ticket attempt changed/cancelled; no stale acknowledgment')
        action = data['transition']
        if action not in ('accept', 'needs_context', 'reject', 'deliver', 'block'):
            raise RuntimeError('Unsupported recipient transition')
        return handoff.transition(self.repo, self.rid, record['id'], action,
                                  detail=str(data.get('detail', ''))[:2000], actor=self.actor['name'])

    def request_profile(self, data):
        self.authorize(controller=True)
        recipient = self.target(data['recipient'])
        if recipient['name'] == self.state['architect']['name']:
            raise RuntimeError('Architect uses native /model and /thinking only')
        profile = settings._profile(data['profile'], 'requested profile')
        if not profile.get('model'):
            raise RuntimeError('Explicit model required')
        if profile.get('thinking') is None:
            raise RuntimeError('Live application requires explicit thinking; Pi defaults remain available in /shop-config')
        self.idle_seat(recipient)
        return self.save_request('profile', {'profile': profile, 'persist': data.get('persist') is True}, recipient)

    def idle_seat(self, member):
        identity.resolve(self.api, member, self.state['tab'], allowed_status=('idle', 'done'), require_terminal=True)
        if any(t['owner'] == member['name'] and t['status'] in ('assigned', 'review')
               for t in ct.tickets(self.repo, self.rid)):
            raise RuntimeError('Seat owns assigned/review work; profile cannot change')

    def profile_action(self, data):
        record = self.load_request(data['id'])
        if record['kind'] != 'profile' or record['recipient'] != member_ref(self.actor):
            raise RuntimeError('Only the exact recipient Pi may apply this profile')
        self.idle_seat(self.actor)
        if data['session_id'] != record.get('recipient_session'):
            raise RuntimeError('Profile belongs to another Pi session')
        if data['action'] == 'profile-resolve':
            if record['status'] not in ('applying', 'unknown'):
                raise RuntimeError('No uncertain profile to reconcile')
            observed = settings._profile(data['observed'], 'observed profile')
            return self.update(record, 'reconciled', observed=observed,
                               detail='Recipient explicitly inspected native Pi model/thinking; no automatic retry')
        if data['action'] == 'profile-claim':
            if record['status'] != 'requested':
                raise RuntimeError('Request already consumed or uncertain; no automatic retry')
            if any(other['id'] != record['id'] and other['kind'] == 'profile'
                   and other.get('recipient') == record['recipient']
                   and other['status'] in ('applying', 'unknown') for other in self.records()):
                raise RuntimeError('Another profile change is applying/unknown; reconcile it first')
            return self.update(record, 'applying', session_id=data['session_id'])
        if record['status'] != 'applying' or record.get('session_id') != data['session_id']:
            raise RuntimeError('No applying request for this Pi session')
        status = data['status']
        if status not in ('applied', 'unknown', 'rejected'):
            raise RuntimeError('Invalid profile outcome')
        # launch_profile remains immutable evidence of the original invocation.
        if status == 'applied' and record['payload']['persist']:
            seat = self.actor.get('seat')
            if not seat:
                roster = [self.state['lead'], *self.state.get('extra_leads', []), *self.state.get('workers', [])]
                seats = ['lead', *['lead-' + str(i + 2) for i in range(len(self.state.get('extra_leads', [])))],
                         *['worker' if i == 0 else 'worker-' + str(i + 1) for i in range(len(self.state.get('workers', [])))]]
                seat = seats[roster.index(self.actor)]
            self.state.setdefault('model_profiles', {})[seat] = record['payload']['profile']
            ct.atomic(self.state_path, self.state)
        return self.update(record, status, detail=str(data.get('detail', ''))[:1000])

    def intervention(self, data):
        self.authorize(controller=True)
        kind = data['kind']
        if kind not in ('scope-change', 'pause', 'cancel'):
            raise RuntimeError('Use shop_message for supplements; no formal task record')
        text = data.get('text', '')
        if not isinstance(text, str) or not 1 <= len(text) <= 4000:
            raise RuntimeError('Request text must contain 1..4000 characters')
        tids = data.get('tickets', [])
        if not isinstance(tids, list) or len(tids) > 100:
            raise RuntimeError('Expected bounded ticket list')
        affected = [ct.load(ct.ticket_path(self.repo, self.rid, tid)) for tid in tids]
        payload = {'text': text, 'tickets': [{'ticket_id': t['ticket_id'], 'attempt': t['attempt'],
                                            'status': t['status']} for t in affected],
                   'spec_revision': 1 + sum(r['kind'] == 'scope-change' for r in self.records()) if kind == 'scope-change' else None}
        return self.save_request(kind, payload, self.state['lead'])

    def acknowledge_intervention(self, data):
        self.authorize(primary=True)
        record = self.load_request(data['id'])
        if record['kind'] not in ('scope-change', 'pause', 'cancel') or record['status'] != 'requested':
            raise RuntimeError('No pending intervention')
        for previous in record['payload']['tickets']:
            current = ct.load(ct.ticket_path(self.repo, self.rid, previous['ticket_id']))
            if current['attempt'] != previous['attempt']:
                raise RuntimeError('Ticket attempt changed; intervention must be reissued')
        if record['kind'] == 'scope-change':
            path = ct.run_path(self.repo, self.rid) / 'run.json'
            metadata = ct.load(path)
            revision = record['payload']['spec_revision']
            if revision <= metadata.get('spec_revision', 0):
                raise RuntimeError('Requirements revision superseded; inspect latest request')
            metadata['spec_revision'] = revision
            ct.atomic(path, metadata)
        # Receipt != stopped/cancelled. Existing ticket operations remain sole authority.
        return self.update(record, 'acknowledged',
                           detail='Lead received request. Replan/pause/cancel requires separate authorized ticket operations.')

    def execute_intervention(self, data):
        self.authorize(primary=True)
        record = self.load_request(data['id'])
        if record['kind'] not in ('pause', 'cancel') or record['status'] not in ('requested', 'acknowledged'):
            raise RuntimeError('No executable pause/cancel request')
        previous = record['payload']['tickets']
        if not previous:
            raise RuntimeError('Explicit affected tickets required before stopping or cancelling')
        if record['kind'] == 'cancel' and data.get('writers_stopped') is not True:
            raise RuntimeError('Inspect foreground/background writers and attest stopped before cancelling')
        tickets = []
        for ref in previous:
            ticket = ct.load(ct.ticket_path(self.repo, self.rid, ref['ticket_id']))
            if ticket['attempt'] != ref['attempt'] or ticket['status'] in ('accepted', 'cancelled'):
                raise RuntimeError('Ticket changed since request; inspect and reissue')
            member = self.target(ticket['owner'])
            dispatch = ticket.get('dispatch')
            if dispatch and (dispatch.get('pane') != member['pane'] or dispatch.get('terminal_id') != member.get('terminal_id')):
                raise RuntimeError('Dispatched writer changed; cannot assume old writer stopped')
            identity.resolve(self.api, member, self.state['tab'], require_terminal=True,
                             allowed_status=('working',) if record['kind'] == 'pause' else ('idle', 'done'))
            tickets.append(ticket)
        self.update(record, 'executing', outcomes=[])
        outcomes = []
        try:
            for ticket in tickets:
                if record['kind'] == 'pause':
                    import supervision
                    result = supervision.pause(self.api, self.state, {'pane_id': self.actor['pane']},
                                               ticket['owner'], record['payload']['text'])
                else:
                    result = ct.cancel(self.repo, self.rid, ticket['ticket_id'], True)
                outcomes.append(result)
                self.update(record, 'executing', outcomes=outcomes)
        except Exception as error:
            self.update(record, 'unknown', outcomes=outcomes, detail=str(error)[:1000])
            raise
        return self.update(record, 'pause_requested' if record['kind'] == 'pause' else 'cancelled', outcomes=outcomes,
                           note='Esc is not stopped. No code/checkpoint deletion, retry, reassignment or automatic replay.')

    def execute(self, data):
        action = data.get('action')
        expected = data.get('expected')
        if expected and any(expected.get(key) != value for key, value in {
                'shop_id': self.state.get('shop_id'), 'run_id': self.rid,
                'actor': self.actor['name'], 'launch_id': self.actor.get('launch_id')}.items()):
            raise RuntimeError('Shop/run/member changed since UI opened')
        if action == 'view':
            return self.overview()
        if action == 'diagnostics':
            return self.diagnostics()
        self.authorize()
        if action == 'note':
            import messaging
            return messaging.send(self.api, self.state, {'pane_id': self.actor['pane']}, data['recipient'], data['text'], True)
        if action == 'handoff-propose':
            return self.request_handoff(data)
        if action == 'handoff-transition':
            return self.handoff_action(data)
        if action == 'profile-request':
            return self.request_profile(data)
        if action in ('profile-claim', 'profile-result', 'profile-resolve'):
            return self.profile_action(data)
        if action == 'intervention-request':
            return self.intervention(data)
        if action == 'intervention-acknowledge':
            return self.acknowledge_intervention(data)
        if action == 'intervention-execute':
            return self.execute_intervention(data)
        if action in ('development-preview', 'development-create', 'delivery', 'final-check', 'integration-preview', 'integrate'):
            import development
            operations = {'development-preview': development.preview, 'development-create': development.apply,
                          'integration-preview': development.integration_preview, 'integrate': development.integrate,
                          'final-check': development.final_check}
            if action == 'delivery':
                return development.delivery(self)
            return operations[action](self, data)
        raise RuntimeError('Unsupported workbench action: ' + str(action))

    def diagnostics(self):
        # Export allowlist: never include paths, IDs, prompts, env, config or raw errors.
        tickets = ct.tickets(self.repo, self.rid) if self.rid else []
        counts = {status: sum(t['status'] == status for t in tickets)
                  for status in ('ready', 'assigned', 'review', 'accepted', 'cancelled')}
        pending = sum(r['status'] in ('requested', 'applying', 'unknown') for r in self.records())
        return {'schema': 'shop.diagnostics/v1', 'redacted': True, 'bound': bool(self.rid),
                'member_count': len(identity.roster(self.state)), 'tickets': counts, 'pending_requests': pending,
                'actions': ['Inspect unknown outcomes before retry; no automatic resend',
                            'Reopen /shop-ui on recipient for pending handoffs/profile requests',
                            'Verify live provider/Herdr behavior separately; offline tests are not live evidence']}


def operation_error(error):
    reason = str(error)[:2000]
    lower = reason.lower()
    if any(word in lower for word in ('stale', 'changed', 'expired', 'superseded')):
        code, next_action = 'E_STALE_PLAN', 'Refresh state and prepare a new plan; do not replay old requests'
    elif any(word in lower for word in ('busy', 'working', 'assigned', 'review', 'writer')):
        code, next_action = 'E_WRITER_UNSAFE', 'Inspect exact owner, foreground and background jobs before continuing'
    elif 'dirty' in lower:
        code, next_action = 'E_DIRTY_WORKTREE', 'Save user work manually or choose a clean worktree; never auto-stash/reset'
    elif any(word in lower for word in ('identity', 'recipient', 'endpoint', 'session', 'registered')):
        code, next_action = 'E_IDENTITY_UNVERIFIED', 'Inspect /shop-status and the target Pi endpoint; never retarget by name alone'
    else:
        code, next_action = 'E_WORKBENCH_REFUSED', 'Inspect request/plan record and preserved state before trying a new action'
    return {'code': code, 'phase': 'workbench', 'reason': reason, 'performed': 'unknown',
            'not_performed': ['automatic_retry', 'push', 'automatic_cleanup'],
            'preserved': 'No automatic rollback, stash, reset, worktree deletion or checkpoint deletion',
            'next_action': next_action}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', required=True)
    parser.add_argument('--request', required=True)
    args = parser.parse_args()
    import shop
    data = json.loads(args.request)
    if len(args.request.encode()) > LIMIT or not isinstance(data, dict):
        raise RuntimeError('Invalid request')
    pane = os.environ.get('HERDR_ACTIVE_PANE_ID') or os.environ.get('HERDR_PANE_ID')
    if data.get('action') in ('view', 'delivery', 'diagnostics'):
        print(json.dumps(Workbench(shop.api, args.state, pane).execute(data), ensure_ascii=False))
        return
    # Same Shop lock as setup/reset; business mutations also use repository lock.
    with Path(args.state).with_suffix('.lock').open('a+') as lock:
        locking.acquire(lock)
        pane = os.environ.get('HERDR_ACTIVE_PANE_ID') or os.environ.get('HERDR_PANE_ID')
        workbench = Workbench(shop.api, args.state, pane)
        with co.repo_lock(workbench.repo):
            result = workbench.execute(data)
        print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(json.dumps(operation_error(error), ensure_ascii=False), file=__import__('sys').stderr)
        raise SystemExit(1)
