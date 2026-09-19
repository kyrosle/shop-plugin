"""Offline setup path through the real adapter, with a fake CLI boundary.

No host calls or AI processes. Covers typed mutation replies and repeat-open
failures; it does not substitute for separately authorized live acceptance.
"""
import contextlib
import copy
import hashlib
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import herdr
import settings
import shop
from test_herdr import Result, wire


class SetupRunner:
    def __init__(self, cwd, failure=None):
        self.probe_runner = wire()
        self.failure = failure
        self.mutations = []
        self.panes = {'p1': self.pane('p1', cwd, 'pi')}
        self.agents = {'p1': dict(self.panes['p1'], screen_detection_skipped=True)}

    @staticmethod
    def pane(pane_id, cwd, agent=None):
        return {'pane_id': pane_id, 'tab_id': 't1', 'workspace_id': 'w1',
                'terminal_id': 'term-' + pane_id, 'cwd': str(cwd),
                'agent': agent, 'agent_status': 'idle'}

    def __call__(self, argv, **kwargs):
        args = tuple(argv[1:])
        route = args[:2]
        if args in (('--version',), ('api', 'schema', '--json')):
            return self.probe_runner(argv, **kwargs)
        if route == ('pane', 'current'):
            result = {'type': 'pane_current', 'pane': self.panes['p1']}
        elif route == ('pane', 'layout'):
            result = {'type': 'pane_layout', 'layout': {
                'area': {'x': 0, 'y': 0, 'width': 200, 'height': 60},
                'zoomed': False, 'panes': [{'pane_id': p} for p in self.panes]}}
        elif route == ('agent', 'get'):
            agent = next(a for a in self.agents.values()
                         if args[2] in (a['pane_id'], a.get('name')))
            result = {'type': 'agent_info', 'agent': agent}
        elif route == ('agent', 'rename'):
            self.agents[args[2]]['name'] = args[3]
            result = {'type': 'agent_info', 'agent': self.agents[args[2]]}
        elif route == ('pane', 'rename'):
            self.panes[args[2]]['label'] = args[3]
            result = {'type': 'pane_info', 'pane': self.panes[args[2]]}
        elif route == ('pane', 'split'):
            pane_id = 'p' + str(len(self.panes) + 1)
            self.panes[pane_id] = self.pane(pane_id, args[args.index('--cwd') + 1])
            result = {'type': 'pane_info', 'pane': self.panes[pane_id]}
        elif route == ('agent', 'start'):
            pane_id = args[args.index('--pane') + 1]
            agent = dict(self.panes[pane_id], agent='pi', name=args[2],
                         screen_detection_skipped=True)
            self.agents[pane_id] = agent
            result = {'type': 'agent_started', 'agent': agent, 'argv': ['pi']}
        else:
            raise AssertionError('Unexpected CLI call: ' + repr(args))
        if route in {('agent', 'rename'), ('pane', 'rename'),
                     ('pane', 'split'), ('agent', 'start')}:
            self.mutations.append(route)
            if self.failure == (route, self.mutations.count(route)):
                # Mutation succeeded, but reply is incompatible. Do not replay it.
                result = {'type': 'ok'}
        return Result(returncode=0, stdout=json.dumps({'result': result}), stderr='')


class SetupAdapterTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.cwd = self.root / 'project'
        self.cwd.mkdir()
        self.state_root = self.root / 'state'
        self.path = self.state_root / 'runtime' / (
            hashlib.sha256(b'fixture-socket:t1').hexdigest()[:12] + '.json')
        self.profiles = {seat: {'model': 'fixture/model', 'thinking': 'medium'}
                         for seat in settings.MODEL_SEATS}
        self.config = SimpleNamespace(STATE=self.state_root, PACKAGE=self.root,
                                      resolve_models=settings.resolve_models,
                                      role_text=lambda role: 'Fixture role: ' + role)

    def run_setup(self, runner):
        adapter = herdr.Herdr(binary='/fake/herdr', runner=runner)
        with patch.object(shop, 'HERDR', adapter), \
                patch.object(shop, '_SETTINGS', self.config), \
                patch('configuration.startup_profiles', return_value=(copy.deepcopy(self.profiles), {'kind': 'fixture'})), \
                patch('sys.argv', ['shop', 'setup']), \
                patch.dict('os.environ', {'HERDR_ENV': '1', 'HERDR_PANE_ID': 'p1',
                                           'HERDR_ACTIVE_PANE_ID': '', 'HERDR_SOCKET_PATH': 'fixture-socket'}), \
                contextlib.redirect_stdout(io.StringIO()):
            shop.main()

    def test_setup_uses_typed_mutation_replies_through_ready(self):
        runner = SetupRunner(self.cwd)
        self.run_setup(runner)
        state = json.loads(self.path.read_text())
        self.assertEqual(state['phase'], 'ready')
        self.assertEqual(state['model_profiles'], self.profiles)
        self.assertEqual(state['architect']['pane'], 'p1')
        self.assertEqual(state['lead']['terminal_id'], 'term-p2')
        self.assertEqual(state['workers'][0]['terminal_id'], 'term-p3')
        self.assertEqual(runner.mutations, [
            ('agent', 'rename'), ('pane', 'rename'),
            ('pane', 'split'), ('pane', 'split'),
            ('pane', 'rename'), ('agent', 'start'),
            ('pane', 'rename'), ('agent', 'start')])

    def test_failed_mutation_then_open_preserves_evidence_without_retry(self):
        failures = [(('agent', 'rename'), 1), (('pane', 'rename'), 1),
                    (('pane', 'split'), 1), (('pane', 'split'), 2),
                    (('agent', 'start'), 1)]
        for failure in failures:
            with self.subTest(failure=failure):
                runner = SetupRunner(self.cwd, failure)
                # Each scenario gets its own runtime; no prior state is overwritten.
                self.config.STATE = self.root / ('case-' + str(failures.index(failure)))
                self.state_root = self.config.STATE
                self.path = self.state_root / 'runtime' / self.path.name
                with self.assertRaises(herdr.HerdrSchemaError):
                    self.run_setup(runner)
                before = self.path.read_bytes()
                state = json.loads(before)
                self.assertEqual(state['phase'], 'partial')
                self.assertIn('expected response type', state['error'])
                self.assertEqual(runner.mutations[-1], failure[0])
                self.assertEqual(runner.mutations.count(failure[0]), failure[1])
                mutations = list(runner.mutations)
                with self.assertRaisesRegex(RuntimeError, 'Incomplete setup registration|Existing/partial layout'):
                    self.run_setup(runner)
                self.assertEqual(runner.mutations, mutations)
                self.assertEqual(self.path.read_bytes(), before)
