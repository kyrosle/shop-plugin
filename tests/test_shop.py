"""Offline removal safety tests. No live Herdr calls."""
import contextlib
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import contracts as ct
import herdr
import run

spec = importlib.util.spec_from_file_location('shop', Path(__file__).resolve().parents[1] / 'core/shop.py')
shop = importlib.util.module_from_spec(spec)
spec.loader.exec_module(shop)


class RemovalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'state.json'
        self.state = {'phase': 'ready', 'tab': 't1',
                      'architect': {'name': 's-architect', 'pane': 'p1'},
                      'lead': {'name': 's-lead', 'pane': 'p2'},
                      'extra_leads': [{'name': 's-lead-2', 'pane': 'p4', 'cwd': '/wt-sol'}],
                      'workers': [{'name': 's-worker', 'pane': 'p3'},
                                  {'name': 's-worker-2', 'pane': 'p5', 'cwd': '/wt-ds'}]}
        shop.save(self.path, self.state)
        self.agents = {x['pane']: dict(x, agent='pi', tab_id='t1', agent_status='idle')
                       for x in [self.state['architect'], self.state['lead'],
                                 *self.state['extra_leads'], *self.state['workers']]}
        self.calls = []
        self.closed = []

    def api(self, *args):
        self.calls.append(args)
        if args[:2] == ('agent', 'get'):
            return {'agent': copy.deepcopy(self.agents[args[2]])}
        if args[:2] == ('pane', 'close'):
            self.closed.append(args[2])
            return {}
        raise AssertionError(args)

    def run_remove(self, target='s-lead-2', action='remove-lead', caller='p2',
                   dry=False, confirmed=True, api=None):
        with patch.object(shop, 'api', api or self.api), contextlib.redirect_stdout(io.StringIO()):
            shop.remove_member(self.path, self.state, {'pane_id': caller}, action,
                               target, dry, confirmed)

    def test_remove_lead_and_update_registration(self):
        self.run_remove()
        self.assertEqual(self.closed, ['p4'])
        saved = json.loads(self.path.read_text())
        self.assertEqual(saved['extra_leads'], [])
        self.assertEqual(saved['phase'], 'ready')
        self.assertEqual(saved['lead']['pane'], 'p2')

    def test_remove_worker_retains_first(self):
        self.run_remove('s-worker-2', 'remove-worker', caller='p1')
        self.assertEqual(self.closed, ['p5'])
        self.assertEqual(len(json.loads(self.path.read_text())['workers']), 1)

    def test_dry_run_has_no_mutations(self):
        original = self.path.read_text()
        self.run_remove(dry=True, confirmed=False)
        self.assertEqual(self.closed, [])
        self.assertEqual(self.path.read_text(), original)

    def test_handoff_required(self):
        with self.assertRaisesRegex(RuntimeError, 'handoff-complete'):
            self.run_remove(confirmed=False)
        self.assertEqual(self.closed, [])

    def test_protected_unknown_wrong_role(self):
        for name, action in [('s-lead', 'remove-lead'), ('s-architect', 'remove-lead'),
                             ('s-worker', 'remove-worker'), ('other', 'remove-lead'),
                             ('s-worker-2', 'remove-lead')]:
            with self.subTest(name=name), self.assertRaises(RuntimeError):
                self.run_remove(name, action)
        self.assertEqual(self.closed, [])

    def test_self_and_non_controller(self):
        for caller in ['p4', 'p3', 'other']:
            with self.subTest(caller=caller), self.assertRaises(RuntimeError):
                self.run_remove(caller=caller)
        self.assertEqual(self.closed, [])

    def test_busy_blocked_unknown(self):
        for status in ['working', 'blocked', 'unknown']:
            self.agents['p4']['agent_status'] = status
            with self.subTest(status=status), self.assertRaises(RuntimeError):
                self.run_remove()
        self.assertEqual(self.closed, [])

    def test_changed_identity_or_tab(self):
        for field, value in [('name', 'other'), ('tab_id', 't2'), ('agent', 'claude')]:
            original = self.agents['p4'][field]
            self.agents['p4'][field] = value
            with self.subTest(field=field), self.assertRaises(RuntimeError):
                self.run_remove()
            self.agents['p4'][field] = original
        self.assertEqual(self.closed, [])

    def test_recheck_catches_new_work(self):
        def api(*args):
            if args == ('agent', 'get', 'p4') and args in self.calls:
                self.agents['p4']['agent_status'] = 'working'
            return self.api(*args)
        with self.assertRaises(RuntimeError):
            self.run_remove(api=api)
        self.assertEqual(self.closed, [])

    def test_close_failure_preserves_intent(self):
        def api(*args):
            if args[:2] == ('pane', 'close'):
                raise RuntimeError('close failed')
            return self.api(*args)
        with self.assertRaisesRegex(RuntimeError, 'close failed'):
            self.run_remove(api=api)
        saved = json.loads(self.path.read_text())
        self.assertEqual(saved['phase'], 'removing')
        self.assertEqual(saved['pending_removal']['pane'], 'p4')
        self.assertEqual(len(saved['extra_leads']), 1)

    def test_partial_state_refused(self):
        self.state['phase'] = 'partial'
        with self.assertRaises(RuntimeError):
            self.run_remove()
        self.assertEqual(self.calls, [])


class AdapterRoutingTests(unittest.TestCase):
    """shop.py must reach Herdr only through the single typed adapter."""

    def test_import_does_not_bind_machine_runtime_state(self):
        self.assertFalse(hasattr(shop, 'ROOT'))
        self.assertFalse(hasattr(shop, 'MODELS'))
        self.assertIsInstance(shop.HERDR, herdr.Herdr)

    def test_notify_and_api_delegate_to_the_adapter(self):
        fake = FakeAdapter()
        with patch.object(shop, 'HERDR', fake):
            shop.notify('Shop ready', 'body')
            shop.api('agent', 'get', 'w5:p4')
        self.assertEqual(fake.calls[0], ('notify', 'Shop ready', 'body'))
        self.assertEqual(fake.calls[1], ('call', 'agent', 'get', 'w5:p4'))


class FakeAdapter(object):
    def __init__(self):
        self.calls = []

    def call(self, *args):
        self.calls.append(('call',) + args)
        return {'agent': {'agent': 'pi'}}

    def notify(self, title, body):
        self.calls.append(('notify', title, body))
        return True


class RetryAuthorizationTests(unittest.TestCase):
    """Retry/cancel may only pass when the old writer is a settled identity."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)
        folder = self.repo / '.shop/runs/R1/tickets'
        folder.mkdir(parents=True)
        ct.atomic(folder / 'T1.ticket.json', {
            'run_id': 'R1', 'ticket_id': 'T1', 'owner': 's-worker', 'attempt': 1,
            'status': 'assigned', 'scope': ['src'], 'kind': 'analysis',
            'dispatch': {'pane': 'p3', 'terminal_id': 'term-p3', 'delivery': 'submitted'}})
        ct.atomic(ct.binding_file(self.repo), {'R1': {
            'shop_id': 'shop1', 'tab': 't1',
            'lead': {'name': 's-lead', 'pane': 'p2', 'terminal_id': 'term-p2'}}})
        self.agents = {
            'p2': {'name': 's-lead', 'agent': 'pi', 'tab_id': 't1', 'pane_id': 'p2',
                   'terminal_id': 'term-p2', 'agent_status': 'idle'},
            'p3': {'name': 's-worker', 'agent': 'pi', 'tab_id': 't1', 'pane_id': 'p3',
                   'terminal_id': 'term-p3', 'agent_status': 'idle'}}
        self.calls = []

    def api(self, *args):
        self.calls.append(args)
        if args[:2] == ('agent', 'get'):
            return {'agent': dict(self.agents[args[2]])}
        if args[:2] == ('pane', 'layout'):
            return {'layout': {'panes': [{'pane_id': 'p2'}, {'pane_id': 'p3'}],
                               'area': {'width': 200, 'height': 50}}}
        if args[:2] == ('pane', 'get'):
            # Real pane payloads carry the agent kind/status when an agent runs there.
            return {'pane': {'pane_id': 'p3', 'tab_id': 't1', 'agent': 'pi',
                             'agent_status': self.agents['p3'].get('agent_status'),
                             'terminal_id': self.agents['p3'].get('terminal_id')}}
        if args[:2] == ('pane', 'process-info'):
            return {'process_info': {'pane_id': 'p3', 'shell_pid': 42,
                                     'foreground_processes': [{'pid': 42, 'name': 'zsh'}]}}
        if args[:2] == ('agent', 'list'):
            return {'agents': []}
        raise AssertionError(args)

    def authorize(self, op='retry'):
        import shop as imported_shop  # the module run.authorize_ticket imports
        env = {'HERDR_PANE_ID': 'p2', 'HERDR_ACTIVE_PANE_ID': ''}
        with patch.dict(os.environ, env), patch.object(imported_shop, 'api', self.api):
            run.authorize_ticket(self.repo, 'R1', 'T1', op)

    def test_idle_matching_occupant_allows_retry(self):
        self.authorize()

    def test_working_blocked_unknown_refuse_retry(self):
        for status in ('working', 'blocked', 'unknown'):
            with self.subTest(status=status):
                self.agents['p3']['agent_status'] = status
                with self.assertRaises(RuntimeError):
                    self.authorize()

    def test_missing_status_refuses_retry(self):
        self.agents['p3'].pop('agent_status')
        with self.assertRaises(RuntimeError):
            self.authorize()

    def test_changed_name_or_terminal_refuses_retry(self):
        for field, value in (('name', 'replacement-worker'), ('terminal_id', 'term-new')):
            with self.subTest(field=field):
                original = self.agents['p3'][field]
                self.agents['p3'][field] = value
                with self.assertRaises(RuntimeError):
                    self.authorize()
                self.agents['p3'][field] = original

    def test_pane_without_agent_requires_empty_shell(self):
        self.calls = []

        def shell_api(*args):
            if args[:2] == ('pane', 'get'):
                return {'pane': {'pane_id': 'p3', 'tab_id': 't1'}}
            return self.api(*args)

        import shop as imported_shop
        with patch.dict(os.environ, {'HERDR_PANE_ID': 'p2', 'HERDR_ACTIVE_PANE_ID': ''}), \
                patch.object(imported_shop, 'api', shell_api):
            run.authorize_ticket(self.repo, 'R1', 'T1', 'retry')

        def busy_shell(*args):
            value = shell_api(*args) if args[:2] != ('pane', 'process-info') else {
                'process_info': {'pane_id': 'p3', 'shell_pid': 42,
                                 'foreground_processes': [{'pid': 42}, {'pid': 99}]}}
            return value

        with patch.dict(os.environ, {'HERDR_PANE_ID': 'p2', 'HERDR_ACTIVE_PANE_ID': ''}), \
                patch.object(imported_shop, 'api', busy_shell):
            with self.assertRaises(RuntimeError):
                run.authorize_ticket(self.repo, 'R1', 'T1', 'retry')

    def test_moved_writer_still_live_is_refused(self):
        self.agents['p3'].pop('agent')
        def moved(*args):
            if args[:2] == ('pane', 'layout'):
                return {'layout': {'panes': [{'pane_id': 'p2'}], 'area': {'width': 200, 'height': 50}}}
            if args[:2] == ('agent', 'list'):
                return {'agents': [{'name': 's-worker', 'pane_id': 'w9:p1'}]}
            return self.api(*args)
        import shop as imported_shop
        with patch.dict(os.environ, {'HERDR_PANE_ID': 'p2', 'HERDR_ACTIVE_PANE_ID': ''}), \
                patch.object(imported_shop, 'api', moved):
            with self.assertRaises(RuntimeError):
                run.authorize_ticket(self.repo, 'R1', 'T1', 'retry')


if __name__ == '__main__':
    unittest.main()
