"""Offline model configuration and launch tests. Never starts Herdr or Pi."""
import contextlib
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from types import SimpleNamespace

import settings
import shop
import configuration


LEGACY = {'lead': 'provider/lead-model', 'worker': 'provider/worker-model'}
LAYERED = {
    'defaults': {'model': 'provider/default', 'thinking': 'medium'},
    'lead': {'model': 'provider/lead-model', 'thinking': 'high'},
    'worker': 'provider/worker-model',
    'seats': {
        'lead': {'thinking': 'xhigh'},
        'lead-2': {'model': 'other/reviewer', 'thinking': 'max'},
        'worker': {'thinking': 'off'},
        'worker-2': {'model': 'other/fast', 'thinking': 'low'},
    },
}


class ModelConfigTests(unittest.TestCase):
    def test_legacy_strings_preserve_pi_thinking_default(self):
        profiles = settings.resolve_models(LEGACY)
        for seat in settings.MODEL_SEATS:
            self.assertEqual(profiles[seat], {'model': LEGACY[seat.split('-')[0]]})

    def test_field_precedence_and_distinct_seats(self):
        original = copy.deepcopy(LAYERED)
        profiles = settings.resolve_models(LAYERED)
        self.assertEqual(profiles, {
            'lead': {'model': 'provider/lead-model', 'thinking': 'xhigh'},
            'lead-2': {'model': 'other/reviewer', 'thinking': 'max'},
            'worker': {'model': 'provider/worker-model', 'thinking': 'off'},
            'worker-2': {'model': 'other/fast', 'thinking': 'low'},
        })
        self.assertEqual(LAYERED, original)
        profiles['lead']['thinking'] = 'off'
        self.assertEqual(LAYERED, original)

    def test_defaults_and_null_thinking_override(self):
        data = {'defaults': {'model': 'provider/model', 'thinking': 'high'},
                'seats': {'worker-2': {'thinking': None}}}
        profiles = settings.resolve_models(data)
        self.assertIsNone(profiles['worker-2']['thinking'])
        self.assertEqual(profiles['worker']['thinking'], 'high')
        self.assertEqual(profiles['lead']['model'], 'provider/model')

    def test_all_thinking_levels(self):
        for level in settings.THINKING_LEVELS:
            with self.subTest(level=level):
                profiles = settings.resolve_models({'defaults': {'model': 'p/m', 'thinking': level}})
                self.assertEqual(profiles['lead']['thinking'], level)

    def test_invalid_config_rejected(self):
        cases = [None, [], 1, 'model', {'architect': 'p/m'}, {'lead': []},
                 {'lead': {'model': 'p/m', 'thinkng': 'high'}},
                 {'seats': []}, {'seats': {'worker-3': 'p/m'}},
                 {'seats': {'s-worker': 'p/m'}}, {'defaults': {'thinking': 'ultra'}},
                 {'defaults': {'thinking': True}}, {'defaults': {'thinking': []}}]
        for value in ('model', '/model', 'provider/', '-p/model', 'p/m\n', 'p/m\x00', 'p/' + 'm' * 512, 1, None):
            cases.append({'lead': {'model': value}})
        for data in cases:
            with self.subTest(data=data), self.assertRaises(RuntimeError):
                settings.resolve_models(data)

    def test_missing_model_rejected(self):
        for data in ({}, {'lead': 'p/m'}, {'defaults': {'thinking': 'high'}}):
            with self.subTest(data=data), self.assertRaisesRegex(RuntimeError, 'Missing model'):
                settings.resolve_models(data)

    def test_file_loading_and_size_limit(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(settings, 'CONFIG', Path(directory)):
            self.assertEqual(settings.models(), {})
            path = Path(directory) / 'models.json'
            with self.assertRaises(FileNotFoundError):
                settings.models(path)
            path.write_text(json.dumps(LEGACY))
            self.assertEqual(settings.models(), LEGACY)
            other = Path(directory) / 'other.json'
            other.write_text(json.dumps(LAYERED))
            self.assertEqual(settings.models(other), LAYERED)
            other.write_text('{bad')
            with self.assertRaises(json.JSONDecodeError):
                settings.models(other)
            other.write_text(' ' * 65537)
            with self.assertRaisesRegex(RuntimeError, '64 KiB'):
                settings.models(other)


class ModelLaunchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = {'cwd': str(self.root), 'prefix': 's-test', 'shop_id': 'shop-test',
                      'model_profiles': settings.resolve_models(LAYERED)}
        self.calls = []
        self.names = {}
        for target, value in [('state_root', lambda: self.root), ('api', self.api)]:
            patcher = patch.object(shop, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        adapter_call = patch.object(shop.HERDR, 'call', self.api)
        adapter_call.start()
        self.addCleanup(adapter_call.stop)

    def api(self, *args, **kwargs):
        self.calls.append(args)
        if args[:2] == ('agent', 'start'):
            self.names[args[args.index('--pane') + 1]] = args[2]
            return {'agent': {}}
        if args[:2] == ('agent', 'get'):
            name = self.names.get(args[2], args[2])
            return {'agent': {'name': name, 'terminal_id': 'terminal-' + name}}
        if args[:2] == ('pane', 'rename'):
            return {}
        raise AssertionError(args)

    def launch(self, seat, state=None):
        item = {'name': 's-test-' + seat, 'pane': 'pane-' + seat}
        shop.start(item, seat.split('-')[0], self.state if state is None else state)
        call = next(args for args in reversed(self.calls) if args[:2] == ('agent', 'start'))
        return item, call

    def test_four_independent_launch_profiles_and_status_evidence(self):
        for seat in settings.MODEL_SEATS:
            with self.subTest(seat=seat):
                item, call = self.launch(seat)
                profile = self.state['model_profiles'][seat]
                self.assertEqual(call[call.index('--model') + 1], profile['model'])
                self.assertEqual(call[call.index('--thinking') + 1], profile['thinking'])
                self.assertEqual(item['launch_profile'], profile)
                self.assertIsNot(item['launch_profile'], self.state['model_profiles'][seat])
                self.assertTrue(Path(item['session_dir']).is_dir())
                self.assertNotIn('--continue', call)
        self.assertEqual(len([c for c in self.calls if c[:2] == ('agent', 'start')]), 4)

    def test_legacy_and_null_omit_thinking_flag(self):
        for data in (LEGACY, {'defaults': {'model': 'p/m', 'thinking': None}}):
            with self.subTest(data=data):
                self.state['model_profiles'] = settings.resolve_models(data)
                _, call = self.launch('worker-2')
                self.assertNotIn('--thinking', call)

    def test_restart_uses_pinned_profile_not_current_config(self):
        item, first = self.launch('lead')
        old_launch = item['launch_id']
        old_session = item['session_dir']
        with patch.object(settings, 'models', side_effect=AssertionError('must not reload config')):
            shop.start(item, 'lead', self.state)
        second = next(c for c in reversed(self.calls) if c[:2] == ('agent', 'start'))
        self.assertEqual(first[first.index('--model'):], second[second.index('--model'):])
        self.assertNotEqual(item['launch_id'], old_launch)
        self.assertNotEqual(item['session_dir'], old_session)

    def test_legacy_shop_pins_config_for_later_auxiliary(self):
        del self.state['model_profiles']
        with patch.object(settings, 'models', return_value=LAYERED) as load:
            self.launch('worker')
            load.assert_called_once_with()
        with patch.object(settings, 'models', side_effect=AssertionError('must not reload config')):
            _, call = self.launch('worker-2')
        self.assertEqual(call[call.index('--model') + 1], 'other/fast')

    def test_two_shops_do_not_share_profiles(self):
        other = dict(self.state, shop_id='other', model_profiles=settings.resolve_models(LEGACY))
        _, first = self.launch('lead')
        _, second = self.launch('lead', other)
        self.assertIn('--thinking', first)
        self.assertNotIn('--thinking', second)
        self.assertEqual(self.state['model_profiles']['lead']['thinking'], 'xhigh')

    def test_invalid_pinned_profile_has_no_launch_side_effects(self):
        self.state['model_profiles']['worker-2']['thinking'] = 'ultra'
        with self.assertRaises(RuntimeError):
            self.launch('worker')
        self.assertEqual(self.calls, [])
        self.assertFalse((self.root / 'runtime').exists())


class ModelSetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / 'chosen.json'
        self.config.write_text(json.dumps(LAYERED))
        self.calls = []
        self.pane = {'pane_id': 'p1', 'tab_id': 't1', 'agent': 'pi', 'cwd': str(self.root)}
        self.key = hashlib.sha256(b'socket:t1').hexdigest()[:12]
        self.state_file = self.root / 'runtime' / (self.key + '.json')

    def api(self, *args):
        self.calls.append(args)
        if args[:2] == ('pane', 'current'):
            return {'pane': self.pane}
        if args[:2] == ('pane', 'layout'):
            return {'layout': {'panes': [self.pane], 'area': {'width': 200, 'height': 60}}}
        if args[:2] in (('agent', 'rename'), ('pane', 'rename')):
            return {}
        if args[:2] == ('pane', 'split'):
            return {'pane': {'pane_id': 'p' + str(len(self.calls))}}
        raise AssertionError(args)

    def run_main(self, *args):
        output = io.StringIO()
        env = {'HERDR_ENV': '1', 'HERDR_PANE_ID': 'p1', 'HERDR_SOCKET_PATH': 'socket'}
        with patch.dict(os.environ, env, clear=True), patch.object(shop, 'state_root', return_value=self.root), \
                patch.object(shop.HERDR, 'probe', return_value=SimpleNamespace(as_dict=lambda: {})), \
                patch.object(shop, 'api', self.api), patch.object(shop, 'start') as start, \
                patch.object(shop.lifecycle, 'begin', return_value={'version': 1}), \
                patch.object(shop.lifecycle, 'require_current'), \
                patch('sys.argv', ['herdr-shop', *args]), contextlib.redirect_stdout(output):
            shop.main()
        return output.getvalue(), start

    def test_setup_pins_selected_file_without_reading_global(self):
        with patch.object(settings, 'CONFIG', self.root / 'missing'):
            _, start = self.run_main('setup', '--models-file', str(self.config))
        state = json.loads(self.state_file.read_text())
        self.assertEqual(state['model_profiles'], settings.resolve_models(LAYERED))
        self.assertEqual(start.call_count, 2)
        self.assertEqual(state['phase'], 'ready')
        self.assertFalse(any(c[:2] == ('agent', 'start') and c[2].endswith('architect') for c in self.calls))

    def test_default_setup_consumes_pi_candidate_and_pins_source(self):
        (self.root / 'settings.json').write_text(json.dumps({'version': 1, 'models': LEGACY}))
        candidate = configuration.candidate_path(self.root, 'socket', 't1', 'p1')
        configuration.atomic_document(candidate, {
            'schema': 1, 'socket': 'socket', 'tab': 't1', 'pane': 'p1', 'terminal': 'terminal',
            'root': str(self.root.resolve()), 'pid': os.getpid(), 'instance': 'test-instance',
            'session_id': 'test-session', 'trusted': True, 'updated_at': time.time(),
            'overrides': {'models': {'seats': {'lead-2': {'model': 'p/session', 'thinking': 'high'}}}},
        })
        original = self.api
        def api(*args):
            if args[:2] == ('agent', 'get'):
                return {'agent': {'agent': 'pi', 'tab_id': 't1', 'terminal_id': 'terminal'}}
            return original(*args)
        with patch.object(self, 'api', api), patch.object(settings, 'CONFIG', self.root):
            _, start = self.run_main('setup')
        start.assert_called()
        state = json.loads(self.state_file.read_text())
        self.assertEqual(state['model_profiles']['lead-2'], {'model': 'p/session', 'thinking': 'high'})
        self.assertEqual(state['config_source']['session_id'], 'test-session')

    def test_default_setup_without_candidate_does_not_split(self):
        with self.assertRaisesRegex(RuntimeError, 'Missing Pi settings candidate'):
            self.run_main('setup')
        self.assertFalse(any(c[:2] == ('pane', 'split') for c in self.calls))
        self.assertFalse(self.state_file.exists())

    def test_dry_run_shows_profiles_without_mutation(self):
        output, start = self.run_main('setup', '--models-file', str(self.config), '--dry-run')
        self.assertEqual(json.loads(output)['model_profiles'], settings.resolve_models(LAYERED))
        start.assert_not_called()
        self.assertFalse(self.state_file.exists())
        self.assertTrue(all(c[:2] in (('pane', 'current'), ('pane', 'layout')) for c in self.calls))

    def test_bad_config_fails_before_split_or_registration(self):
        self.config.write_text(json.dumps({'defaults': {'model': 'p/m', 'thinking': 'ultra'}}))
        with self.assertRaisesRegex(RuntimeError, 'thinking'):
            self.run_main('setup', '--models-file', str(self.config))
        self.assertFalse(self.state_file.exists())
        self.assertFalse(any(c[:2] in (('pane', 'split'), ('agent', 'rename')) for c in self.calls))

    def test_existing_shop_cannot_be_reconfigured(self):
        self.state_file.parent.mkdir(parents=True)
        self.state_file.write_text(json.dumps({'phase': 'ready', 'cwd': str(self.root)}))
        before = self.state_file.read_bytes()
        with self.assertRaisesRegex(RuntimeError, 'existing shop'):
            self.run_main('setup', '--models-file', str(self.config))
        self.assertEqual(self.state_file.read_bytes(), before)
        self.assertEqual(self.calls, [('pane', 'current', '--current')])

    def test_models_file_rejected_for_other_commands(self):
        for action in ('status', 'reset', 'add-worker', 'recover-lead'):
            with self.subTest(action=action), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                self.run_main(action, '--models-file', str(self.config))
        self.assertEqual(self.calls, [])

    def test_add_worker_uses_pinned_auxiliary_profile(self):
        auxiliary = self.root / 'auxiliary'
        auxiliary.mkdir()
        state = {'phase': 'ready', 'cwd': str(self.root), 'tab': 't1', 'prefix': 's-test',
                 'architect': {'pane': 'p1', 'name': 's-test-architect'},
                 'lead': {'pane': 'p2', 'name': 's-test-lead'},
                 'workers': [{'pane': 'p3', 'name': 's-test-worker'}],
                 'model_profiles': settings.resolve_models(LAYERED)}
        self.state_file.parent.mkdir(parents=True)
        self.state_file.write_text(json.dumps(state))
        original_api = self.api

        def api(*args):
            if args[:2] == ('pane', 'layout'):
                return {'layout': {'panes': [{'pane_id': 'p3', 'rect': {'width': 100, 'height': 30}}]}}
            return original_api(*args)

        def cmd(argv):
            if argv[3:] == ['rev-parse', '--show-toplevel']:
                return str(auxiliary)
            if argv[3:] == ['worktree', 'list', '--porcelain']:
                return 'worktree ' + str(self.root) + '\nworktree ' + str(auxiliary) + '\n'
            if argv[3:] == ['status', '--porcelain']:
                return ''
            raise AssertionError(argv)

        with patch.object(self, 'api', api), patch.object(shop, 'cmd', cmd), \
                patch.object(shop.co, 'caller_control'), \
                patch.object(settings, 'models', side_effect=AssertionError('must use pinned profiles')):
            output, start = self.run_main('add-worker', '--cwd', str(auxiliary), '--dry-run')
            self.assertEqual(json.loads(output)['launch_profile'], state['model_profiles']['worker-2'])
            start.assert_not_called()
            self.assertEqual(json.loads(self.state_file.read_text()), state)
            _, start = self.run_main('add-worker', '--cwd', str(auxiliary))
        start.assert_called_once()
        item, role, saved_state = start.call_args.args
        self.assertEqual(item['name'], 's-test-worker-2')
        self.assertEqual(role, 'worker')
        self.assertEqual(saved_state['model_profiles'], state['model_profiles'])
        self.assertEqual(json.loads(self.state_file.read_text())['model_profiles'], state['model_profiles'])

    def test_status_does_not_load_model_configuration(self):
        with patch.object(settings, 'models', side_effect=AssertionError('status must not load config')):
            output, start = self.run_main('status')
        self.assertIsNone(json.loads(output)['shop'])
        start.assert_not_called()


if __name__ == '__main__':
    unittest.main()
