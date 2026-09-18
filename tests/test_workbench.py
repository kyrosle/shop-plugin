"""Workbench acceptance against temporary Git repos and fake Herdr facts only."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import time
import unittest

import contracts as ct
import coordination as co
import development
import handoff
import run
from workbench import Workbench, operation_error


class WorkbenchTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        ct.git(self.repo, 'init', '-b', 'main')
        ct.git(self.repo, 'config', 'user.name', 'Offline Test')
        ct.git(self.repo, 'config', 'user.email', 'offline@example.invalid')
        (self.repo / 'file').write_text('baseline\n')
        ct.git(self.repo, 'add', 'file')
        ct.git(self.repo, 'commit', '-m', 'baseline')
        self.rid = run.Runs(self.repo).new('test')['created']
        members = [{'name': name, 'pane': pane, 'launch_id': 'launch-' + pane, 'terminal_id': 'term-' + pane,
                    'cwd': str(self.repo)} for name, pane in [('architect', 'p1'), ('lead', 'p2'), ('worker', 'p3')]]
        self.state = {'shop_id': 's1', 'phase': 'ready', 'tab': 't1', 'cwd': str(self.repo),
                      'architect': members[0], 'lead': members[1], 'workers': [members[2]],
                      'model_profiles': {'worker': {'model': 'test/old', 'thinking': 'low'}}}
        self.state_path = self.root / 'state/runtime/tab.json'
        self.calls = []
        self.live = {m['pane']: {'name': m['name'], 'terminal_id': m['terminal_id'],
                                'pane_id': m['pane'], 'tab_id': 't1', 'agent': 'pi',
                                'agent_status': 'idle'} for m in members}
        co.bind(self.api, ct.atomic, self.state_path, self.state, {'pane_id': 'p1'}, self.rid)
        for member in members:
            self.endpoint(member)

    def endpoint(self, member, session=None):
        key = hashlib.sha256(json.dumps(['s1', member['name']], separators=(',', ':')).encode()).hexdigest()
        ct.atomic(self.root / 'state/endpoints' / (key + '.json'), {
            'shop_id': 's1', 'run_id': self.rid, 'member_id': member['name'], 'pane_id': member['pane'],
            'terminal_id': member['terminal_id'], 'launch_id': member['launch_id'],
            'session_id': session or 'session-' + member['pane'], 'endpoint_epoch': 'epoch-' + member['pane'],
            'broker_epoch': 'broker-test', 'expires_at': time.time() * 1000 + 20000})

    def api(self, *args):
        self.calls.append(args)
        if args[:2] in (('pane', 'get'), ('agent', 'get')):
            return {args[0]: copy.deepcopy(self.live[args[2]])}
        if args[:2] == ('pane', 'process-info'):
            return {'process_info': {'shell_pid': 1, 'foreground_processes': [{'pid': 1}]}}
        if args[:2] == ('agent', 'send-keys'):
            return {}
        raise AssertionError('Forbidden live/mutating API: ' + repr(args))

    def wb(self, pane='p1'):
        return Workbench(self.api, self.state_path, pane)

    def request(self, action, **data):
        return self.wb().execute({'action': action, **data})

    def propose(self):
        return self.request('handoff-propose', recipient='worker', objective='inspect module',
                            scope='read-only', acceptance='source paths', evidence='SPEC.md')

    def test_view_is_read_only_and_uses_common_snapshot(self):
        def files():
            return {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        before = files()
        view = self.request('view')
        self.assertEqual(view['snapshot']['schema'], 'shop.snapshot/v1')
        self.assertEqual(view['actor'], 'architect')
        self.assertEqual(files(), before)
        self.assertFalse(any(call[1] not in ('get', 'process-info') for call in self.calls))

    def test_handoff_requires_exact_recipient_and_distinct_business_acceptance(self):
        prepared = self.propose()
        rid = prepared['request']['id']
        self.assertEqual(prepared['handoff']['state'], 'proposed')
        self.assertIn('transport_request', prepared)
        with self.assertRaisesRegex(RuntimeError, 'intended recipient'):
            self.wb('p2').execute({'action': 'handoff-transition', 'id': rid, 'transition': 'accept'})
        result = self.wb('p3').execute({'action': 'handoff-transition', 'id': rid, 'transition': 'needs_context'})
        self.assertEqual(result['state'], 'needs_context')
        accepted = self.wb('p3').execute({'action': 'handoff-transition', 'id': rid, 'transition': 'accept'})
        self.assertEqual(accepted['state'], 'accepted')
        self.assertEqual(self.request('view')['records'][0]['status'], 'accepted')
        self.assertEqual(ct.tickets(self.repo, self.rid), [])

    def test_handoff_refuses_replacement_launch_and_session(self):
        rid = self.propose()['request']['id']
        self.endpoint(self.state['workers'][0], 'new-session')
        with self.assertRaisesRegex(RuntimeError, 'session changed'):
            self.wb('p3').execute({'action': 'handoff-transition', 'id': rid, 'transition': 'accept'})
        self.state['workers'][0]['launch_id'] = 'replacement'
        ct.atomic(self.state_path, self.state)
        with self.assertRaisesRegex(RuntimeError, 'identity is stale'):
            self.wb('p3').execute({'action': 'handoff-transition', 'id': rid, 'transition': 'accept'})

    def test_profile_request_is_not_application_and_cannot_target_architect(self):
        with self.assertRaisesRegex(RuntimeError, 'Architect'):
            self.wb('p2').execute({'action': 'profile-request', 'recipient': 'architect',
                                   'profile': {'model': 'test/new', 'thinking': 'high'}})
        record = self.request('profile-request', recipient='worker', profile={'model': 'test/new', 'thinking': 'high'}, persist=True)
        self.assertEqual(ct.load(self.state_path)['model_profiles']['worker']['model'], 'test/old')
        with self.assertRaisesRegex(RuntimeError, 'exact recipient'):
            self.request('profile-claim', id=record['id'], session_id='session-p1')
        worker = self.wb('p3')
        worker.execute({'action': 'profile-claim', 'id': record['id'], 'session_id': 'session-p3'})
        with self.assertRaisesRegex(RuntimeError, 'already consumed'):
            worker.execute({'action': 'profile-claim', 'id': record['id'], 'session_id': 'session-p3'})
        worker.execute({'action': 'profile-result', 'id': record['id'], 'session_id': 'session-p3', 'status': 'applied'})
        self.assertEqual(ct.load(self.state_path)['model_profiles']['worker']['model'], 'test/new')

    def test_profile_busy_and_unknown_outcome_fail_closed(self):
        self.live['p3']['agent_status'] = 'working'
        with self.assertRaises(RuntimeError):
            self.request('profile-request', recipient='worker', profile={'model': 'test/new', 'thinking': 'high'})
        self.live['p3']['agent_status'] = 'idle'
        record = self.request('profile-request', recipient='worker', profile={'model': 'test/new', 'thinking': 'high'}, persist=True)
        worker = self.wb('p3')
        worker.execute({'action': 'profile-claim', 'id': record['id'], 'session_id': 'session-p3'})
        worker.execute({'action': 'profile-result', 'id': record['id'], 'session_id': 'session-p3', 'status': 'unknown'})
        self.assertEqual(ct.load(self.state_path)['model_profiles']['worker']['model'], 'test/old')
        with self.assertRaises(RuntimeError):
            worker.execute({'action': 'profile-claim', 'id': record['id'], 'session_id': 'session-p3'})

    def test_intervention_is_request_not_stop_or_cancel(self):
        record = self.request('intervention-request', kind='cancel', text='requirement withdrawn', tickets=[])
        self.assertEqual(record['status'], 'requested')
        with self.assertRaisesRegex(RuntimeError, 'Primary Lead'):
            self.request('intervention-acknowledge', id=record['id'])
        ack = self.wb('p2').execute({'action': 'intervention-acknowledge', 'id': record['id']})
        self.assertEqual(ack['status'], 'acknowledged')
        self.assertFalse(any(call[:2] == ('agent', 'send-keys') for call in self.calls))

    def test_scope_revisions_are_monotonic(self):
        one = self.request('intervention-request', kind='scope-change', text='first', tickets=[])
        two = self.request('intervention-request', kind='scope-change', text='second', tickets=[])
        self.assertEqual((one['payload']['spec_revision'], two['payload']['spec_revision']), (1, 2))

    def test_prepare_creates_only_after_explicit_action_and_never_reuses(self):
        target = self.root / 'new-worktree'
        plan = self.request('development-preview', path=str(target), branch='task/test')
        self.assertFalse(target.exists())
        created = self.request('development-create', id=plan['id'])
        self.assertEqual(created['status'], 'created')
        self.assertEqual(ct.git(target, 'rev-parse', 'HEAD'), plan['payload']['base_commit'])
        with self.assertRaisesRegex(RuntimeError, 'unused'):
            self.request('development-create', id=plan['id'])

    def test_prepare_dirty_and_stale_refused_without_cleanup(self):
        target = self.root / 'new-worktree'
        plan = self.request('development-preview', path=str(target), branch='task/test')
        (self.repo / 'file').write_text('user edits\n')
        with self.assertRaisesRegex(RuntimeError, 'stale'):
            self.request('development-create', id=plan['id'])
        with self.assertRaisesRegex(RuntimeError, 'dirty'):
            self.request('development-preview', path=str(target), branch='task/test2')
        self.assertEqual((self.repo / 'file').read_text(), 'user edits\n')
        self.assertFalse(target.exists())

    def test_prepare_wrong_branch_existing_path_and_expiry_refused(self):
        with self.assertRaisesRegex(RuntimeError, 'already exists'):
            self.request('development-preview', path=str(self.root / 'next'), branch='main')
        with self.assertRaisesRegex(RuntimeError, 'already exists'):
            self.request('development-preview', path=str(self.repo), branch='new')
        plan = self.request('development-preview', path=str(self.root / 'next'), branch='new')
        plan['payload']['expires_at'] = 0
        ct.atomic(self.wb().folder() / (plan['id'] + '.json'), plan)
        with self.assertRaisesRegex(RuntimeError, 'stale'):
            self.request('development-create', id=plan['id'])

    def accepted_development(self):
        path = self.root / 'delivery'
        plan = self.request('development-preview', path=str(path), branch='delivery')
        self.request('development-create', id=plan['id'])
        (path / 'feature').write_text('new\n')
        ct.git(path, 'add', 'feature')
        ct.git(path, 'commit', '-m', 'feature')
        sha = ct.git(path, 'rev-parse', 'HEAD')
        ticket = {'ticket_id': 'T1', 'run_id': self.rid, 'attempt': 1, 'owner': 'worker', 'status': 'accepted',
                  'kind': 'development', 'worktree': str(path), 'base_commit': plan['payload']['base_commit']}
        result_path = ct.attempt_path(self.repo, ticket, 'result')
        ticket['result_path'] = str(result_path)
        ct.atomic(ct.ticket_path(self.repo, self.rid, 'T1'), ticket)
        ct.atomic(result_path, {**{key: ticket[key] for key in ('run_id', 'ticket_id', 'attempt', 'owner', 'base_commit')},
                                'result_commit': sha, 'summary': 'new feature', 'checks': []})
        return sha

    def test_integration_plan_pins_sha_and_does_not_finish_run(self):
        sha = self.accepted_development()
        plan = self.request('integration-preview', ticket='T1')
        self.assertNotEqual(ct.git(self.repo, 'rev-parse', 'HEAD'), sha)
        with self.assertRaisesRegex(RuntimeError, 'attestation'):
            self.request('integrate', id=plan['id'])
        result = self.request('integrate', id=plan['id'], writers_stopped=True)
        self.assertEqual(result['status'], 'integrated')
        self.assertEqual(ct.git(self.repo, 'rev-parse', 'HEAD'), sha)
        self.assertEqual(run.Runs(self.repo).meta(self.rid)['status'], 'active')
        self.assertIn(self.rid, ct.bindings(self.repo))

    def test_integration_refuses_busy_member_and_changed_evidence(self):
        self.accepted_development()
        plan = self.request('integration-preview', ticket='T1')
        self.live['p3']['agent_status'] = 'working'
        with self.assertRaises(RuntimeError):
            self.request('integrate', id=plan['id'], writers_stopped=True)
        self.live['p3']['agent_status'] = 'idle'
        p = ct.ticket_path(self.repo, self.rid, 'T1')
        ticket = ct.load(p); ticket['status'] = 'review'; ct.atomic(p, ticket)
        with self.assertRaises(RuntimeError):
            self.request('integrate', id=plan['id'], writers_stopped=True)

    def test_diagnostics_allowlist_excludes_prompts_paths_and_ids(self):
        self.request('intervention-request', kind='pause', text='sk-secret-test /home/private', tickets=[])
        serialized = json.dumps(self.request('diagnostics'))
        for secret in ('sk-secret', '/home', str(self.repo), self.rid, 's1', 'architect'):
            self.assertNotIn(secret, serialized)
        self.assertIn('pending_requests', serialized)

    def ticket(self, tid='T1', depends=None):
        return ct.new_ticket(self.repo, self.rid, {'ticket_id': tid, 'owner': 'worker', 'objective': 'inspect',
            'worktree': str(self.repo), 'kind': 'analysis', 'scope': ['file'], 'checks': [], 'depends_on': depends or []})

    def test_real_dispatch_creates_handoff_and_attempt_fences_receipt(self):
        self.ticket()
        result = co.dispatch(self.api, self.state, {'pane_id': 'p2'}, 'T1', self.state_path)
        hid = result['dispatch']['handoff_id']
        self.assertIsNotNone(hid)
        self.assertEqual(handoff.load(self.repo, self.rid, hid)['message_id'], result['dispatch']['message_id'])
        self.wb('p3').execute({'action': 'handoff-transition', 'id': hid, 'transition': 'accept'})
        self.assertEqual(ct.load(ct.ticket_path(self.repo, self.rid, 'T1'))['status'], 'assigned')
        ct.retry(self.repo, self.rid, 'T1', True)
        with self.assertRaisesRegex(RuntimeError, 'attempt changed'):
            self.wb('p3').execute({'action': 'handoff-transition', 'id': hid, 'transition': 'deliver'})

    def test_profile_claim_blocks_dispatch_until_explicit_reconciliation(self):
        self.ticket()
        record = self.request('profile-request', recipient='worker', profile={'model': 'test/new', 'thinking': 'high'})
        worker = self.wb('p3')
        worker.execute({'action': 'profile-claim', 'id': record['id'], 'session_id': 'session-p3'})
        with self.assertRaisesRegex(RuntimeError, 'profile change'):
            co.dispatch(self.api, self.state, {'pane_id': 'p2'}, 'T1', self.state_path)
        worker.execute({'action': 'profile-result', 'id': record['id'], 'session_id': 'session-p3', 'status': 'unknown'})
        with self.assertRaisesRegex(RuntimeError, 'profile change'):
            co.dispatch(self.api, self.state, {'pane_id': 'p2'}, 'T1', self.state_path)
        worker.execute({'action': 'profile-resolve', 'id': record['id'], 'session_id': 'session-p3',
                        'observed': {'model': 'test/new', 'thinking': 'high'}})
        co.dispatch(self.api, self.state, {'pane_id': 'p2'}, 'T1', self.state_path)

    def test_scope_ack_requires_ready_ticket_revision_before_dispatch(self):
        self.ticket()
        record = self.request('intervention-request', kind='scope-change', text='new scope', tickets=['T1'])
        self.wb('p2').execute({'action': 'intervention-acknowledge', 'id': record['id']})
        with self.assertRaisesRegex(RuntimeError, 'Requirements changed'):
            co.dispatch(self.api, self.state, {'pane_id': 'p2'}, 'T1', self.state_path)
        revised = ct.revise(self.repo, self.rid, 'T1', {'objective': 'new scope'})
        self.assertEqual(revised['spec_revision'], 1)
        co.dispatch(self.api, self.state, {'pane_id': 'p2'}, 'T1', self.state_path)

    def test_cancel_requires_attestation_and_handles_dependencies_explicitly(self):
        self.ticket(); self.ticket('T2', ['T1'])
        record = self.request('intervention-request', kind='cancel', text='cancel upstream', tickets=['T1'])
        lead = self.wb('p2')
        with self.assertRaisesRegex(RuntimeError, 'attest stopped'):
            lead.execute({'action': 'intervention-execute', 'id': record['id']})
        with self.assertRaisesRegex(RuntimeError, 'Dependent tickets'):
            lead.execute({'action': 'intervention-execute', 'id': record['id'], 'writers_stopped': True})
        self.assertEqual(ct.load(ct.ticket_path(self.repo, self.rid, 'T1'))['status'], 'ready')
        for tid in ('T2', 'T1'):
            request = self.request('intervention-request', kind='cancel', text='cancel explicitly', tickets=[tid])
            result = lead.execute({'action': 'intervention-execute', 'id': request['id'], 'writers_stopped': True})
            self.assertEqual(result['status'], 'cancelled')
        self.assertTrue((self.repo / 'file').exists())

    def test_pause_sends_only_esc_and_never_claims_stopped(self):
        self.ticket()
        co.dispatch(self.api, self.state, {'pane_id': 'p2'}, 'T1', self.state_path)
        self.live['p3']['agent_status'] = 'working'
        request = self.request('intervention-request', kind='pause', text='scope changed', tickets=['T1'])
        result = self.wb('p2').execute({'action': 'intervention-execute', 'id': request['id']})
        self.assertEqual(result['status'], 'pause_requested')
        self.assertIn(('agent', 'send-keys', 'worker', 'esc'), self.calls)
        self.assertEqual(ct.load(ct.ticket_path(self.repo, self.rid, 'T1'))['status'], 'assigned')

    def test_final_evidence_is_attested_hash_bound_and_expires(self):
        evidence = self.root / 'checks.log'; evidence.write_text('checks passed')
        head = ct.git(self.repo, 'rev-parse', 'HEAD')
        self.request('final-check', target_commit=head, command='manual review', exit_code=0, evidence=str(evidence))
        report = self.request('delivery')
        self.assertTrue(report['final_checks'][0]['current'])
        self.assertIn('user-attested', report['final_checks'][0]['provenance'])
        evidence.write_text('changed evidence')
        self.assertFalse(self.request('delivery')['final_checks'][0]['current'])
        with self.assertRaisesRegex(RuntimeError, 'commit changed'):
            self.request('final-check', target_commit='0' * 40, command='check', exit_code=0, evidence=str(evidence))

    def test_operation_errors_have_actionable_bounded_shape(self):
        for message, code in [('Plan stale', 'E_STALE_PLAN'), ('Dirty worktree', 'E_DIRTY_WORKTREE'),
                              ('endpoint unavailable', 'E_IDENTITY_UNVERIFIED')]:
            error = operation_error(RuntimeError(message))
            self.assertEqual(error['code'], code)
            self.assertEqual(error['performed'], 'unknown')
            self.assertTrue(error['next_action'])
            self.assertIn('automatic_cleanup', error['not_performed'])

    def test_destination_symlinks_and_nested_worktrees_are_refused(self):
        alias = self.root / 'dangling'
        alias.symlink_to(self.root / 'absent')
        with self.assertRaisesRegex(RuntimeError, 'already exists'):
            self.request('development-preview', path=str(alias), branch='new1')
        with self.assertRaisesRegex(RuntimeError, 'overlaps'):
            self.request('development-preview', path=str(self.repo / 'nested'), branch='new2')
        parent = self.root / 'future-parent'
        plan = self.request('development-preview', path=str(parent / 'tree'), branch='new3')
        parent.symlink_to(self.repo, target_is_directory=True)
        with self.assertRaisesRegex(RuntimeError, 'parent path changed'):
            self.request('development-create', id=plan['id'])
        self.assertFalse((self.repo / 'tree').exists())

    def test_only_one_profile_claim_can_be_in_flight_for_a_seat(self):
        one = self.request('profile-request', recipient='worker', profile={'model': 'test/one', 'thinking': 'high'})
        two = self.request('profile-request', recipient='worker', profile={'model': 'test/two', 'thinking': 'low'})
        worker = self.wb('p3')
        worker.execute({'action': 'profile-claim', 'id': one['id'], 'session_id': 'session-p3'})
        with self.assertRaisesRegex(RuntimeError, 'Another profile change'):
            worker.execute({'action': 'profile-claim', 'id': two['id'], 'session_id': 'session-p3'})
        worker.execute({'action': 'profile-resolve', 'id': one['id'], 'session_id': 'session-p3',
                        'observed': {'model': 'test/one', 'thinking': 'high'}})
        self.assertEqual(worker.execute({'action': 'profile-claim', 'id': two['id'], 'session_id': 'session-p3'})['status'], 'applying')

    def test_delivery_refuses_mismatched_result_attempt(self):
        self.accepted_development()
        ticket = ct.load(ct.ticket_path(self.repo, self.rid, 'T1'))
        result = ct.load(ticket['result_path']); result['attempt'] += 1
        ct.atomic(ticket['result_path'], result)
        self.assertIsNone(self.request('delivery')['tickets'][0]['result_commit'])
        with self.assertRaisesRegex(RuntimeError, 'no accepted result commit'):
            self.request('integration-preview', ticket='T1')

    def test_ui_expected_identity_is_fenced(self):
        with self.assertRaisesRegex(RuntimeError, 'changed since UI'):
            self.request('profile-request', expected={'shop_id': 'wrong'}, recipient='worker', profile={'model': 'test/new'})


if __name__ == '__main__':
    unittest.main()
