"""Safe shutdown: authority, fail-closed blockers, journaled close, recovery plans.

Every test uses a fake adapter and a temp root. No pane is closed on a live
workstation, no plugin is switched, no process is killed and nothing is deleted
except files created inside the temp root by the test itself.
"""
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import contracts as ct
import run
import shutdown as sd

STATE_KEY = hashlib.sha256(b'/tmp/shop-shutdown.sock:w8:t1').hexdigest()[:12]


class ShutdownApi:
    """Recording fake of the typed Herdr adapter."""

    def __init__(self, members=None, statuses=None, panes=None, extra_panes=(), layout=None,
                 close_effect=None, background=False, process_extra=(), agent_names=None,
                 terminal_overrides=None, tab_overrides=None, pane_id_overrides=None,
                 zoom_fails=False):
        self.members = {member['pane']: member for member in (members or [])}
        self.statuses = statuses or {}
        self.extra_panes = list(extra_panes)
        self.layout = layout
        self.close_effect = close_effect or (lambda pane: None)
        self.background = background
        self.process_extra = list(process_extra)
        self.agent_names = agent_names
        self.terminal_overrides = terminal_overrides or {}
        self.tab_overrides = tab_overrides or {}
        self.pane_id_overrides = pane_id_overrides or {}
        self.zoom_fails = zoom_fails
        self.stuck = set()
        self.calls = []
        self.closed = []

    def panes(self):
        rows = []
        for pane, member in self.members.items():
            rows.append({'pane_id': pane, 'tab_id': self.tab_overrides.get(pane, 'w8:t1'), 'workspace_id': 'w8',
                         'terminal_id': self.terminal_overrides.get(pane, 'term-' + pane), 'agent': 'pi'})
        rows.extend(self.extra_panes)
        for pane in self.closed:
            if pane in self.stuck:
                continue
            rows = [row for row in rows if row.get('pane_id') != pane]
        return rows

    def agents(self):
        names = self.agent_names if self.agent_names is not None else \
            {pane: member['name'] for pane, member in self.members.items()}
        rows = list(getattr(self, 'agent_extra', []))
        for pane, member in self.members.items():
            if pane in self.closed and pane not in self.stuck:
                continue
            rows.append({'name': names.get(pane, member['name']), 'agent': 'pi',
                         'pane_id': self.pane_id_overrides.get(pane, pane),
                         'tab_id': self.tab_overrides.get(pane, 'w8:t1'),
                         'terminal_id': self.terminal_overrides.get(pane, 'term-' + pane),
                         'agent_status': self.statuses.get(pane, 'idle'), 'workspace_id': 'w8'})
        return rows

    def __call__(self, *args):
        self.calls.append(args)
        verb = args[:2]
        if verb == ('workspace', 'list'):
            return {'workspaces': [{'workspace_id': 'w8'}]}
        if verb == ('pane', 'list'):
            return {'panes': self.panes()}
        if verb == ('agent', 'list'):
            return {'agents': self.agents()}
        if verb == ('pane', 'get'):
            pane = args[2]
            for row in self.panes():
                if row['pane_id'] == pane:
                    return {'pane': row}
            raise RuntimeError('pane not found: ' + str(pane))
        if verb == ('agent', 'get'):
            pane = args[2]
            for row in self.agents():
                if row['pane_id'] == pane:
                    return {'agent': row}
            raise RuntimeError('agent not found: ' + str(pane))
        if verb == ('pane', 'process-info'):
            pane = args[3] if args[2] == '--pane' else args[2]
            shell = 1000 + len(pane)
            foreground = [{'pid': shell, 'name': 'zsh'}]
            if pane in self.process_extra:
                foreground.append({'pid': shell + 1, 'name': 'sleep'})
            info = {'pane_id': pane, 'shell_pid': shell, 'foreground_processes': foreground}
            if self.background:
                info['descendants'] = []
            return {'process_info': info}
        if verb == ('pane', 'close'):
            pane = args[2]
            self.close_effect(pane)
            self.closed.append(pane)
            return {'type': 'ok'}
        if verb == ('pane', 'zoom'):
            if self.zoom_fails:
                raise RuntimeError('zoom refused')
            return {'type': 'ok'}
        if verb == ('pane', 'layout'):
            panes = self.layout if self.layout is not None else \
                [{'pane_id': row['pane_id']} for row in self.panes()]
            return {'layout': {'panes': panes, 'tab_id': 'w8:t1', 'workspace_id': 'w8',
                               'area': {'width': 200, 'height': 50}}}
        raise AssertionError('unexpected call ' + repr(args))


def proven_processes(api, pane_id):
    """Process probe with descendant proof (the shape a capable Herdr would give)."""
    facts = sd.process_facts(api, pane_id)
    facts['background_proven'] = True
    facts['source'] = 'test_double_with_descendant_proof'
    return facts


class ShutdownBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_root = Path(self.tmp.name) / 'state'
        self.runtime = self.state_root / 'runtime'
        self.runtime.mkdir(parents=True)
        self.repo = Path(self.tmp.name) / 'repo'
        self.repo.mkdir()
        self.rid = run.Runs(self.repo).new('shutdown')['created']
        self.state = {
            'schema_version': 1, 'shop_id': 'shop-shutdown', 'run_id': None, 'phase': 'ready',
            'tab': 'w8:t1', 'cwd': str(self.repo), 'prefix': 's1',
            'architect': {'name': 'arch', 'pane': 'w8:p1', 'launch_id': 'a1', 'terminal_id': 'term-w8:p1'},
            'lead': {'name': 'lead', 'pane': 'w8:p2', 'launch_id': 'l1', 'terminal_id': 'term-w8:p2'},
            'workers': [{'name': 'wa', 'pane': 'w8:p3', 'launch_id': 'w1', 'terminal_id': 'term-w8:p3'},
                        {'name': 'wb', 'pane': 'w8:p4', 'launch_id': 'w2', 'terminal_id': 'term-w8:p4'}],
        }
        self.state_path = self.runtime / (STATE_KEY + '.json')

    def write_state(self, **overrides):
        state = dict(self.state, **overrides)
        ct.atomic(self.state_path, state)
        return state

    def api_for(self, state=None, **kwargs):
        state = state or self.state
        members = [state['architect'], state['lead'], *state['workers']]
        return ShutdownApi(members=members, **kwargs)

    def preview(self, api=None, caller='w8:p4', **kwargs):
        return sd.preview(api or self.api_for(), self.state_path, caller,
                          authority={'kind': sd.AUTHORITY, 'action_id': 'act-1', 'plugin_id': 'shop.workstation'},
                          repo=str(self.repo), **kwargs)

    def close_calls(self, api):
        return [call for call in api.calls if call[:2] == ('pane', 'close')]

    def execute(self, api=None, plan=None, **kwargs):
        api = api or self.api_for()
        kwargs.setdefault('process_probe', proven_processes)
        plan = plan or self.preview(api=api, process_probe=kwargs['process_probe'])
        self.assertEqual(plan['decision'], 'ready', plan['blockers'])
        return sd.execute(api, self.state_path, plan, repo=str(self.repo), **kwargs)


class AuthorityTests(ShutdownBase):
    def test_preview_records_the_real_role_and_never_impersonates_architect(self):
        self.write_state()
        for caller, role in [('w8:p1', 'architect'), ('w8:p2', 'lead'), ('w8:p3', 'worker'), ('w8:p4', 'worker')]:
            with self.subTest(caller=caller):
                plan = self.preview(caller=caller)
                self.assertEqual(plan['authority'], 'herdr_user_action')
                self.assertEqual(plan['invocation']['role'], role)
                self.assertTrue(plan['invocation']['registered'])
                self.assertEqual(plan['keep'][0]['pane'], 'w8:p1')
                self.assertIn('not an OS security boundary', plan['authority_context']['note'])

    def test_unregistered_invocation_is_diagnosis_only(self):
        self.write_state()
        plan = self.preview(caller='w8:p9')
        self.assertFalse(plan['invocation']['registered'])
        self.assertIn('invocation_unregistered', [entry['code'] for entry in plan['blockers']])
        self.assertNotEqual(plan['decision'], 'ready')

    def test_agent_cli_is_recorded_as_such_and_cannot_execute(self):
        self.write_state()
        api = self.api_for()
        plan = sd.preview(api, self.state_path, 'w8:p4', authority=sd.parse_authority({}), repo=str(self.repo))
        self.assertEqual(plan['authority'], 'agent_cli')
        self.assertIsNone(plan['authority_context']['action_id'])
        # The CLI refuses execution without plugin-action context (S02).
        with self.assertRaises(sd.ShutdownRefused) as caught:
            sd.main(['execute', '--state', str(self.state_path), '--caller', 'w8:p4',
                     '--plan', plan['plan_id']])
        self.assertEqual(caught.exception.code, 'authority_missing')
        self.assertEqual(self.close_calls(api), [])

    def test_preview_helper_never_mutates(self):
        self.write_state()
        api = self.api_for()
        before = json.dumps(json.loads(self.state_path.read_text()), sort_keys=True)
        self.preview(api=api)
        self.assertEqual(json.dumps(json.loads(self.state_path.read_text()), sort_keys=True), before)
        self.assertEqual(self.close_calls(api), [])
        self.assertEqual([call for call in api.calls if call[:2] == ('agent', 'send-keys')], [])
        self.assertEqual([call for call in api.calls
                          if call[0] == 'pane' and call[1] in ('close', 'zoom')], [])


class BlockerTests(ShutdownBase):
    def test_bound_run_blocks_with_zero_mutation(self):
        self.write_state(run_id=self.rid)
        api = self.api_for()
        plan = self.preview(api=api, process_probe=proven_processes)
        codes = [entry['code'] for entry in plan['blockers']]
        self.assertIn('bound_run', codes)
        self.assertEqual(plan['decision'], 'blocked')
        self.assertEqual(self.close_calls(api), [])

    def test_binding_registry_mismatch_blocks(self):
        self.write_state(run_id=self.rid)
        ct.atomic(ct.binding_file(self.repo), {self.rid: {'shop_id': 'other-shop', 'tab': 'w8:t1'}})
        plan = self.preview(process_probe=proven_processes)
        self.assertIn('binding_mismatch', [entry['code'] for entry in plan['blockers']])

    def test_active_or_unknown_member_blocks(self):
        for status in ('working', 'blocked', 'unknown'):
            with self.subTest(status=status):
                self.write_state()
                api = self.api_for(statuses={'w8:p3': status})
                plan = self.preview(api=api, process_probe=proven_processes)
                self.assertIn('member_active', [entry['code'] for entry in plan['blockers']])
                self.assertEqual(self.close_calls(api), [])

    def test_drifted_members_require_recovery(self):
        cases = {
            'missing': ('w8:p3', 'missing'),
            'replacement': ('w8:p3', 'replacement'),
            'moved': ('w8:p3', 'moved'),
        }
        for label, (pane, kind) in cases.items():
            with self.subTest(label=label):
                self.write_state()
                api = self.api_for()
                if kind == 'missing':
                    del api.members[pane]
                elif kind == 'replacement':
                    api.terminal_overrides[pane] = 'term-replaced'
                else:
                    api.members[pane] = dict(api.members[pane], name='someone-else')
                plan = self.preview(api=api, process_probe=proven_processes)
                self.assertEqual(plan['decision'], 'recovery_required')
                self.assertEqual(self.close_calls(api), [])

    def test_unregistered_pane_in_managed_tab_blocks(self):
        self.write_state()
        api = self.api_for(extra_panes=[{'pane_id': 'w8:p9', 'tab_id': 'w8:t1', 'workspace_id': 'w8',
                                         'terminal_id': 'term-w8:p9', 'agent': None}])
        plan = self.preview(api=api, process_probe=proven_processes)
        self.assertIn('unregistered_pane', [entry['code'] for entry in plan['blockers']])
        self.assertEqual(self.close_calls(api), [])

    def test_architect_drift_blocks(self):
        self.write_state()
        api = self.api_for()
        api.members['w8:p1'] = dict(api.members['w8:p1'], name='not-arch')
        plan = self.preview(api=api, process_probe=proven_processes)
        self.assertEqual(plan['decision'], 'recovery_required')

    def test_background_state_unknown_blocks_and_is_reported(self):
        self.write_state()
        plan = self.preview()  # default probe: Herdr exposes no descendant proof
        codes = [entry['code'] for entry in plan['blockers']]
        self.assertIn('background_state_unknown', codes)
        self.assertEqual(plan['decision'], 'blocked')
        self.assertFalse(all(row.get('background_proven') for row in plan['facts']['process']))

    def test_foreground_work_blocks(self):
        self.write_state()
        api = self.api_for(process_extra=('w8:p3',))
        plan = self.preview(api=api, process_probe=sd.process_facts)
        self.assertIn('foreground_work', [entry['code'] for entry in plan['blockers']])

    def test_process_facts_unavailable_blocks(self):
        self.write_state()

        def failing(api, pane_id):
            return {'pane_id': pane_id, 'available': False, 'error': 'process-info failed',
                    'background_proven': False, 'extra_foreground': []}

        plan = self.preview(process_probe=failing)
        self.assertIn('process_uncertain', [entry['code'] for entry in plan['blockers']])

    def test_phase_and_unknown_state_fail_closed(self):
        for phase in ('partial', 'shutdown_closing', 'shutdown_partial'):
            with self.subTest(phase=phase):
                self.write_state(phase=phase)
                plan = self.preview(process_probe=proven_processes)
                self.assertEqual(plan['decision'], 'recovery_required')
        self.write_state()
        self.state_path.unlink()
        plan = self.preview(process_probe=proven_processes)
        self.assertEqual(plan['decision'], 'unknown')

    def test_oversized_state_is_refused(self):
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(dict(self.state, padding='x' * 70000)))
        plan = self.preview(process_probe=proven_processes)
        self.assertIn('state_oversized', [entry['code'] for entry in plan['blockers']])

    def test_transport_and_handoff_uncertainty_block(self):
        self.write_state()
        plan = self.preview(process_probe=proven_processes,
                           transport_summary={'unknown': 2, 'pending': 1},
                           handoff_summary={'open': [{'handoff_id': 'h-1', 'status': 'accepted'}]})
        codes = [entry['code'] for entry in plan['blockers']]
        self.assertIn('transport_unknown', codes)
        self.assertIn('transport_pending', codes)
        self.assertIn('handoff_pending', codes)

    def test_redaction_of_process_and_authority_facts(self):
        self.write_state()
        plan = self.preview(process_probe=proven_processes)
        text = json.dumps(plan)
        for forbidden in ('Bearer ', 'API_TOKEN=', '/Users/'):
            self.assertNotIn(forbidden, text)


class ExecutionTests(ShutdownBase):
    def test_ready_plan_closes_in_caller_last_order_and_retains_architect(self):
        self.write_state()
        api = self.api_for()
        plan = self.preview(api=api, caller='w8:p4', process_probe=proven_processes)
        self.assertEqual(plan['decision'], 'ready')
        self.assertEqual([item['pane'] for item in plan['close_order']],
                         ['w8:p3', 'w8:p2', 'w8:p4'])
        self.assertTrue(plan['close_order'][-1]['caller'])
        result = sd.execute(api, self.state_path, plan, repo=str(self.repo), process_probe=proven_processes)
        self.assertEqual(result['decision'], 'closed')
        self.assertEqual(result['closed'], ['w8:p3', 'w8:p2', 'w8:p4'])
        self.assertEqual(result['retained']['pane'], 'w8:p1')
        self.assertFalse(self.state_path.exists())
        receipt = sd.last_receipt(self.state_root, 'shop-shutdown')
        self.assertTrue(receipt['verified_absent'])
        self.assertEqual(receipt['retained']['pane'], 'w8:p1')
        self.assertTrue(Path(result['archive']).is_file())
        self.assertEqual([call for call in api.calls if call[:2] == ('pane', 'close')],
                         [('pane', 'close', 'w8:p3'), ('pane', 'close', 'w8:p2'), ('pane', 'close', 'w8:p4')])

    def test_architect_invocation_keeps_architect_and_orders_others(self):
        self.write_state()
        api = self.api_for()
        plan = self.preview(api=api, caller='w8:p1', process_probe=proven_processes)
        # Non-caller order: workers, then lead; no caller entry when Architect invokes.
        self.assertEqual([item['pane'] for item in plan['close_order']], ['w8:p3', 'w8:p4', 'w8:p2'])
        self.assertFalse(any(item['caller'] for item in plan['close_order']))
        result = sd.execute(api, self.state_path, plan, repo=str(self.repo), process_probe=proven_processes)
        self.assertEqual(result['decision'], 'closed')

    def test_plan_is_revalidated_and_stale_plans_are_refused(self):
        self.write_state()
        api = self.api_for()
        plan = self.preview(api=api, process_probe=proven_processes)
        self.write_state(workers=[*self.state['workers'], {'name': 'wc', 'pane': 'w8:p5',
                                                            'launch_id': 'w3', 'terminal_id': 'term-w8:p5'}])
        with self.assertRaises(sd.ShutdownRefused) as caught:
            sd.execute(api, self.state_path, plan, repo=str(self.repo), process_probe=proven_processes)
        self.assertEqual(caught.exception.code, 'plan_stale')
        self.assertEqual(self.close_calls(api), [])
        self.assertTrue(self.state_path.exists())

    def test_expired_plan_is_refused(self):
        self.write_state()
        api = self.api_for()
        plan = self.preview(api=api, process_probe=proven_processes)
        later = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1)
        with self.assertRaises(sd.ShutdownRefused) as caught:
            sd.execute(api, self.state_path, plan, repo=str(self.repo), process_probe=proven_processes,
                       now=later)
        self.assertEqual(caught.exception.code, 'plan_expired')
        self.assertEqual(self.close_calls(api), [])

    def test_close_failure_journals_shutdown_partial_without_success(self):
        self.write_state()

        def fail_on_second(pane):
            if pane == 'w8:p2':  # second target in the deterministic order
                raise RuntimeError('pane close failed')

        api = self.api_for(close_effect=fail_on_second)
        plan = self.preview(api=api, process_probe=proven_processes)
        result = sd.execute(api, self.state_path, plan, repo=str(self.repo), process_probe=proven_processes)
        self.assertEqual(result['decision'], 'recovery_required')
        self.assertEqual(result['closed'], ['w8:p3'])
        journal = json.loads(self.state_path.read_text())
        self.assertEqual(journal['phase'], 'shutdown_partial')
        self.assertEqual(journal['shutdown_error']['code'], 'close_failed')
        self.assertEqual(len(journal['pending_shutdown']['completed']), 1)
        self.assertIsNone(sd.last_receipt(self.state_root, 'shop-shutdown'))

    def test_close_success_but_pane_still_enumerated_is_unknown(self):
        self.write_state()
        api = self.api_for()
        plan = self.preview(api=api, process_probe=proven_processes)
        api.stuck.add('w8:p3')  # close returns success but the pane/agent is still enumerated
        result = sd.execute(api, self.state_path, plan, repo=str(self.repo), process_probe=proven_processes)
        self.assertEqual(result['decision'], 'recovery_required')
        self.assertEqual([entry['code'] for entry in result['blockers']], ['still_present'])
        self.assertEqual(json.loads(self.state_path.read_text())['phase'], 'shutdown_partial')
        self.assertTrue(self.state_path.exists())
        self.assertIsNone(sd.last_receipt(self.state_root, 'shop-shutdown'))

    def test_extra_pane_appearing_after_preview_is_refused_before_any_close(self):
        self.write_state()
        api = self.api_for()
        plan = self.preview(api=api, process_probe=proven_processes)
        api.extra_panes.append({'pane_id': 'w8:p9', 'tab_id': 'w8:t1', 'workspace_id': 'w8'})
        with self.assertRaises(sd.ShutdownRefused) as caught:
            sd.execute(api, self.state_path, plan, repo=str(self.repo), process_probe=proven_processes)
        self.assertEqual(caught.exception.code, 'plan_no_longer_ready')
        self.assertEqual(self.close_calls(api), [])
        self.assertIsNone(sd.last_receipt(self.state_root, 'shop-shutdown'))
        self.assertTrue(self.state_path.exists())

    def test_extra_pane_after_close_blocks_the_final_receipt(self):
        self.write_state()
        api = self.api_for()
        plan = self.preview(api=api, process_probe=proven_processes)
        original_close = api.close_effect

        def add_pane(pane):
            original_close(pane)
            if pane == 'w8:p2':
                api.extra_panes.append({'pane_id': 'w8:p9', 'tab_id': 'w8:t1', 'workspace_id': 'w8'})

        api.close_effect = add_pane
        result = sd.execute(api, self.state_path, plan, repo=str(self.repo), process_probe=proven_processes)
        self.assertEqual(result['decision'], 'recovery_required')
        self.assertIn('unexpected_final_layout', [entry['code'] for entry in result['blockers']])
        self.assertNotIn(('pane', 'close', 'w8:p9'), [call for call in api.calls])
        self.assertIsNone(sd.last_receipt(self.state_root, 'shop-shutdown'))
        self.assertTrue(self.state_path.exists())

    def test_repeated_close_after_receipt_is_idempotent(self):
        self.write_state()
        api = self.api_for()
        self.execute(api=api)
        # A realistic post-close live view: only the retained Architect pane remains.
        second_api = ShutdownApi(members=[self.state['architect']])
        plan = sd.preview(second_api, self.state_path, 'w8:p4',
                          authority={'kind': sd.AUTHORITY, 'action_id': 'a'}, repo=str(self.repo),
                          process_probe=proven_processes)
        self.assertEqual(plan['decision'], 'already_closed')
        self.assertTrue(plan['facts']['receipt']['verified_absent'])
        self.assertEqual(self.close_calls(second_api), [])

    def test_absent_state_without_receipt_is_unknown(self):
        self.write_state()
        self.state_path.unlink()
        api = self.api_for()
        plan = sd.preview(api, self.state_path, 'w8:p4',
                          authority={'kind': sd.AUTHORITY, 'action_id': 'a'}, repo=str(self.repo))
        self.assertEqual(plan['decision'], 'unknown')

    def test_architect_lost_during_close_keeps_registration_and_reports_recovery(self):
        self.write_state()
        api = self.api_for()
        plan = self.preview(api=api, process_probe=proven_processes)
        original_effect = api.close_effect

        def drop_architect(pane):
            original_effect(pane)
            del api.members['w8:p1']

        api.close_effect = drop_architect
        result = sd.execute(api, self.state_path, plan, repo=str(self.repo), process_probe=proven_processes)
        self.assertEqual(result['decision'], 'recovery_required')
        self.assertTrue(self.state_path.exists())
        self.assertIsNone(sd.last_receipt(self.state_root, 'shop-shutdown'))

    def test_execute_never_kills_processes_sends_keys_or_deletes_extra_files(self):
        self.write_state()
        api = self.api_for()
        extra = self.runtime / 'keep.json'
        extra.write_text('{}')
        self.execute(api=api)
        self.assertTrue(extra.is_file())
        for forbidden in ('send-keys', 'kill', 'delete'):
            self.assertFalse(any(forbidden in ' '.join(str(part) for part in call) for call in api.calls))

    def test_archive_precedes_the_phase_change(self):
        self.write_state()
        api = self.api_for()
        plan = self.preview(api=api, process_probe=proven_processes)
        result = sd.execute(api, self.state_path, plan, repo=str(self.repo), process_probe=proven_processes)
        archived = json.loads(Path(result['archive']).read_text())
        self.assertEqual(archived['state']['phase'], 'ready')
        self.assertEqual(archived['plan_id'], plan['plan_id'])


class RecoveryPlanTests(ShutdownBase):
    def test_present_members_require_no_action_and_never_mutate(self):
        self.write_state()
        api = self.api_for()
        plan = sd.recovery_plan(api, self.state_path, repo=str(self.repo))
        self.assertEqual(plan['decision'], 'no_action_required')
        self.assertEqual({case['case'] for case in plan['cases']}, {'present_exact'})
        self.assertEqual(self.close_calls(api), [])
        self.assertEqual([call for call in api.calls if call[1] in ('close', 'send-keys')], [])

    def test_each_drift_case_is_classified_with_read_only_actions(self):
        self.write_state()
        api = self.api_for()
        api.members['w8:p3'] = dict(api.members['w8:p3'], name='replacement-agent')   # replacement
        api.tab_overrides['w8:p4'] = 'w9:t1'                                          # moved (other tab)
        del api.members['w8:p2']                                                      # missing
        plan = sd.recovery_plan(api, self.state_path, repo=str(self.repo))
        cases = {case['member']: case['case'] for case in plan['cases']}
        self.assertEqual(cases['wa'], 'replacement')
        self.assertEqual(cases['wb'], 'moved')
        self.assertEqual(cases['lead'], 'missing')
        self.assertEqual(plan['decision'], 'recovery_required')
        self.assertTrue(all('preview-only' in action['mode'] or 'retain' in action['action']
                            for action in plan['actions']))
        self.assertEqual(self.close_calls(api), [])

    def test_moved_member_is_found_globally_and_never_reported_missing(self):
        self.write_state()
        # Registered pane is gone; the identity lives in another workspace.
        api = self.api_for()
        displaced = api.members.pop('w8:p3')
        api.extra_panes.append({'pane_id': 'w9:p1', 'tab_id': 'w9:t1', 'workspace_id': 'w9',
                                'terminal_id': 'term-w8:p3', 'agent': 'pi'})
        api.agent_extra = [{'name': displaced['name'], 'agent': 'pi', 'pane_id': 'w9:p1',
                            'tab_id': 'w9:t1', 'terminal_id': 'term-w8:p3', 'agent_status': 'idle',
                            'workspace_id': 'w9'}]
        plan = sd.recovery_plan(api, self.state_path, repo=str(self.repo))
        row = next(item for item in plan['members'] if item['name'] == 'wa')
        self.assertEqual(row['classification'], 'moved')
        self.assertEqual(row['observed_pane'], 'w9:p1')
        self.assertEqual(row['observed_workspace_id'], 'w9')
        self.assertEqual(plan['decision'], 'recovery_required')

    def test_duplicate_identity_is_reported_with_all_panes(self):
        self.write_state()
        api = self.api_for()
        api.agent_extra = [{'name': 'wa', 'agent': 'pi', 'pane_id': 'w9:p2', 'tab_id': 'w9:t1',
                            'terminal_id': 'term-dup', 'agent_status': 'idle', 'workspace_id': 'w9'}]
        api.extra_panes.append({'pane_id': 'w9:p2', 'tab_id': 'w9:t1', 'workspace_id': 'w9'})
        plan = sd.recovery_plan(api, self.state_path, repo=str(self.repo))
        row = next(item for item in plan['members'] if item['name'] == 'wa')
        self.assertEqual(row['classification'], 'duplicate')
        self.assertEqual(sorted(row['duplicates']), ['w8:p3', 'w9:p2'])

    def test_identity_mismatch_metadata_is_reported_not_guessed(self):
        self.write_state()
        api = self.api_for(pane_id_overrides={'w8:p3': 'w8:pX'})
        plan = sd.recovery_plan(api, self.state_path, repo=str(self.repo))
        row = next(item for item in plan['members'] if item['name'] == 'wa')
        self.assertEqual(row['classification'], 'identity_mismatch')

    def test_unreadable_enumeration_reports_unknown_never_missing(self):
        self.write_state()

        def broken(*args):
            if args[:2] == ('agent', 'list'):
                raise RuntimeError('agent list unavailable')
            return self.api_for()(*args)

        plan = sd.recovery_plan(broken, self.state_path, repo=str(self.repo))
        self.assertEqual({case['case'] for case in plan['cases']}, {'unknown'})
        self.assertNotIn('missing', [case['case'] for case in plan['cases']])
        self.assertEqual(plan['decision'], 'recovery_required')

    def test_single_architect_survivor_reports_restore_candidates(self):
        self.write_state()
        api = self.api_for()
        for pane in ('w8:p2', 'w8:p3', 'w8:p4'):
            del api.members[pane]
        plan = sd.recovery_plan(api, self.state_path, repo=str(self.repo))
        self.assertEqual([row['classification'] for row in plan['members'] if row['role'] != 'architect'],
                         ['missing', 'missing', 'missing'])
        kinds = [action['kind'] for action in plan['actions']]
        self.assertIn('restore_candidates', kinds)
        self.assertTrue(next(action for action in plan['actions'] if action['kind'] == 'restore_candidates')
                        ['requires_explicit_apply'])
        self.assertEqual(plan['decision'], 'recovery_required')

    def test_pending_phase_plan_keeps_everything(self):
        self.write_state(phase='shutdown_partial')
        api = self.api_for()
        plan = sd.recovery_plan(api, self.state_path, repo=str(self.repo))
        self.assertEqual(plan['decision'], 'recovery_required')
        self.assertTrue(self.state_path.exists())
        self.assertEqual(self.close_calls(api), [])

    def test_absent_state_uses_receipt_for_already_closed(self):
        self.write_state()
        api = self.api_for()
        self.execute(api=api)
        plan = sd.recovery_plan(api, self.state_path, repo=str(self.repo))
        self.assertEqual(plan['decision'], 'already_closed')
        self.assertEqual(plan['receipt']['shop_id'], 'shop-shutdown')

    def test_all_members_absent_preserves_tickets_and_never_dispatches(self):
        self.write_state()
        api = self.api_for()
        api.members = {}
        plan = sd.recovery_plan(api, self.state_path, repo=str(self.repo))
        self.assertEqual(plan['decision'], 'recovery_required')
        self.assertIn('tickets_preserved', json.dumps(plan))
        self.assertFalse(any(call[:2] == ('agent', 'prompt') for call in api.calls))

    def test_redaction_of_recovery_plan(self):
        self.write_state()
        api = self.api_for()
        text = json.dumps(sd.recovery_plan(api, self.state_path, repo=str(self.repo)))
        for forbidden in ('API_TOKEN=', 'Bearer ', 'sk-SECRET'):
            self.assertNotIn(forbidden, text)


if __name__ == '__main__':
    unittest.main()


class PlanIntegrityTests(ShutdownBase):
    """Recovery plans carry identity and integrity, and restore enforces them."""

    def test_recovery_plan_is_identified_persisted_and_digest_protected(self):
        self.write_state()
        api = self.api_for()
        plan = sd.recovery_plan(api, self.state_path, repo=str(self.repo))
        self.assertEqual(plan['schema'], sd.RECOVERY_PLAN_SCHEMA)
        self.assertTrue(plan['plan_id'])
        self.assertEqual(plan['state_revision'], sd.state_revision(self.state))
        self.assertEqual(plan['plan_digest'], sd.plan_digest(plan))
        path = Path(plan['plan_path'])
        self.assertTrue(path.is_file())
        self.assertTrue(str(path).startswith(str(self.state_root / 'shutdown' / 'recovery')))
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        stored = json.loads(path.read_text())
        self.assertEqual(stored['plan_digest'], plan['plan_digest'])
        self.assertEqual(sd.load_recovery_plan(self.state_root, 'shop-shutdown', plan['plan_id'])['plan_id'],
                         plan['plan_id'])

    def test_verification_refuses_unpersisted_tampered_stale_and_expired_plans(self):
        self.write_state()
        api = self.api_for()
        plan = sd.recovery_plan(api, self.state_path, repo=str(self.repo))
        tampered = json.loads(json.dumps(plan))
        tampered['actions'] = [{'kind': 'injected', 'mode': 'apply'}]
        with self.assertRaises(sd.ShutdownRefused) as caught:
            sd.verify_recovery_plan(self.state, tampered, self.state_root)
        self.assertEqual(caught.exception.code, 'plan_tampered')
        # A hand-built plan with a self-consistent digest still fails: it is not the
        # plan this core persisted.
        bypass = json.loads(json.dumps(plan))
        bypass['actions'] = [{'kind': 'injected', 'mode': 'apply'}]
        bypass['plan_digest'] = sd.plan_digest(bypass)
        with self.assertRaises(sd.ShutdownRefused) as caught:
            sd.verify_recovery_plan(self.state, bypass, self.state_root)
        self.assertEqual(caught.exception.code, 'plan_tampered')
        unpersisted = json.loads(json.dumps(plan))
        unpersisted['plan_id'] = 'deadbeefdeadbeef'
        unpersisted['plan_digest'] = sd.plan_digest(unpersisted)
        with self.assertRaises(sd.ShutdownRefused) as caught:
            sd.verify_recovery_plan(self.state, unpersisted, self.state_root)
        self.assertEqual(caught.exception.code, 'plan_not_persisted')
        moved_state = dict(self.state, workers=[*self.state['workers'],
                                                {'name': 'extra', 'pane': 'w8:p9'}])
        with self.assertRaises(sd.ShutdownRefused) as caught:
            sd.verify_recovery_plan(moved_state, plan, self.state_root)
        self.assertEqual(caught.exception.code, 'plan_stale')
        expired = json.loads(json.dumps(plan))
        expired['expires_at'] = '2000-01-01T00:00:00+00:00'
        expired['plan_digest'] = sd.plan_digest(expired)
        ct.atomic(Path(plan['plan_path']), expired)
        with self.assertRaises(sd.ShutdownRefused) as caught:
            sd.verify_recovery_plan(self.state, expired, self.state_root)
        self.assertEqual(caught.exception.code, 'plan_expired')
        # A freshly generated plan verifies again (the store is not permanently poisoned).
        fresh = sd.recovery_plan(self.api_for(), self.state_path, repo=str(self.repo))
        self.assertTrue(sd.verify_recovery_plan(self.state, fresh, self.state_root).is_file())


class FinalizationTests(ShutdownBase):
    def test_zoom_failure_is_journaled_as_partial_without_receipt(self):
        self.write_state()
        api = self.api_for(zoom_fails=True)
        plan = self.preview(api=api, process_probe=proven_processes)
        result = sd.execute(api, self.state_path, plan, repo=str(self.repo), process_probe=proven_processes)
        self.assertEqual(result['decision'], 'recovery_required')
        self.assertEqual([entry['code'] for entry in result['blockers']], ['zoom_failed'])
        journal = json.loads(self.state_path.read_text())
        self.assertEqual(journal['phase'], 'shutdown_partial')
        self.assertEqual(journal['shutdown_error']['code'], 'zoom_failed')
        self.assertIsNone(sd.last_receipt(self.state_root, 'shop-shutdown'))

    def test_receipt_write_failure_is_journaled_as_partial(self):
        self.write_state()
        api = self.api_for()
        plan = self.preview(api=api, process_probe=proven_processes)
        real_atomic = sd.ct.atomic

        def failing_atomic(path, value):
            if 'receipts' in str(path):
                raise OSError('receipt write failed')
            return real_atomic(path, value)

        with mock.patch.object(sd.ct, 'atomic', failing_atomic):
            result = sd.execute(api, self.state_path, plan, repo=str(self.repo),
                                process_probe=proven_processes)
        self.assertEqual(result['decision'], 'recovery_required')
        self.assertEqual([entry['code'] for entry in result['blockers']], ['receipt_write_failed'])
        journal = json.loads(self.state_path.read_text())
        self.assertEqual(journal['phase'], 'shutdown_partial')
        self.assertTrue(self.state_path.exists())

    def test_registration_removal_failure_is_truthful_and_never_success(self):
        self.write_state()
        api = self.api_for()
        plan = self.preview(api=api, process_probe=proven_processes)
        real_unlink = Path.unlink
        target = self.state_path

        def failing_unlink(path_self, *args, **kwargs):
            if Path(path_self) == target:
                raise OSError('registration removal failed')
            return real_unlink(path_self, *args, **kwargs)
        Path.unlink = failing_unlink
        try:
            result = sd.execute(api, self.state_path, plan, repo=str(self.repo),
                                process_probe=proven_processes)
        finally:
            Path.unlink = real_unlink
        self.assertEqual(result['decision'], 'recovery_required')
        self.assertNotEqual(result['decision'], 'closed')
        self.assertEqual([entry['code'] for entry in result['blockers']], ['registration_not_removed'])
        journal = json.loads(self.state_path.read_text())
        self.assertEqual(journal['phase'], 'shutdown_partial')
        self.assertEqual(journal['shutdown_error']['code'], 'registration_not_removed')
        receipt = sd.last_receipt(self.state_root, 'shop-shutdown')
        self.assertIsNotNone(receipt)
        self.assertFalse(receipt['registration_removed'])
        self.assertTrue(receipt['verified_absent'])
        # A repeated close must not claim success from the half-finished transition.
        follow_up = sd.preview(self.api_for(), self.state_path, 'w8:p4',
                               authority={'kind': sd.AUTHORITY, 'action_id': 'a'}, repo=str(self.repo),
                               process_probe=proven_processes)
        self.assertEqual(follow_up['decision'], 'recovery_required')
