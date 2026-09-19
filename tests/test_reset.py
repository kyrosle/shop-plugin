"""Offline reset safety tests. No host calls, member launches or live state."""
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import reset


class ResetTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.runtime = self.root / 'state/runtime'
        self.runtime.mkdir(parents=True)
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        self.env = {'HERDR_ENV': '1', 'HERDR_PANE_ID': 'p1', 'HERDR_TAB_ID': 't1', 'HERDR_SOCKET_PATH': 'socket'}
        key = reset.digest(b'socket:t1')[:12]
        self.path = self.runtime / (key + '.json')
        self.prefix = 's' + key[:8]
        self.state = {'schema_version': 1, 'phase': 'partial', 'setup_stage': 'architect',
                      'prefix': self.prefix, 'shop_id': key + '-12345678', 'tab': 't1', 'cwd': str(self.repo),
                      'architect': {'pane': 'p1', 'name': self.prefix + '-architect', 'terminal_id': 'term1'},
                      'workers': [], 'error': "herdr pane rename p1 Architect [exit=0]: expected ok, got pane_info"}
        self.write_state()
        self.owner = {'pane_id': 'p1', 'tab_id': 't1', 'terminal_id': 'term1', 'agent': 'pi',
                      'name': None, 'cwd': str(self.repo)}
        self.layout = {'panes': [{'pane_id': 'p1'}]}
        self.others = []
        self.calls = []

    def write_state(self):
        self.path.write_text(json.dumps(self.state, ensure_ascii=False))

    def api(self, *args):
        self.calls.append(args)
        if args[:2] == ('pane', 'get'):
            return {'pane': copy.deepcopy(self.owner)}
        if args[:2] == ('agent', 'get'):
            return {'agent': copy.deepcopy(self.owner)}
        if args[:2] == ('pane', 'layout'):
            return {'layout': copy.deepcopy(self.layout)}
        if args[:2] == ('agent', 'list'):
            return {'agents': [copy.deepcopy(self.owner), *copy.deepcopy(self.others)]}
        raise AssertionError('Mutation/unexpected call: ' + repr(args))

    def request(self, **payload):
        return reset.request(self.api, self.runtime.parent, payload or {'action': 'preview'}, self.env)

    def apply(self, plan):
        return self.request(action='apply', token=plan['token'], confirmed=True)

    def test_confirmed_name_loss_archives_only_current_registration_exactly(self):
        original = self.path.read_bytes()
        other = self.runtime / 'other-tab.json'
        other.write_bytes(b'other tab untouched')
        plan = self.request()
        self.assertEqual(plan['decision'], 'ready')
        self.assertIsNone(plan['observed_name'])
        self.assertEqual(self.path.read_bytes(), original)
        self.assertFalse((self.runtime.parent / 'reset-archive').exists())
        result = self.apply(plan)
        archive = Path(result['archive'])
        self.assertEqual(archive.read_bytes(), original)
        self.assertEqual(archive.stat().st_mode & 0o777, 0o600)
        self.assertFalse(self.path.exists())
        self.assertEqual(other.read_bytes(), b'other tab untouched')
        self.assertEqual(result['closes_panes'], 0)
        self.assertEqual(result['starts_members'], 0)
        self.assertEqual(list(self.repo.iterdir()), [])
        self.assertEqual(self.request()['decision'], 'empty')
        with self.assertRaisesRegex(RuntimeError, 'changed or blocked'):
            self.apply(plan)

    def test_named_architect_and_legacy_rename_are_supported(self):
        self.owner['name'] = self.state['architect']['name']
        del self.state['setup_stage']
        self.write_state()
        self.assertEqual(self.request()['decision'], 'ready')
        self.state['error'] = 'herdr pane split --pane p1: invalid reply'
        self.write_state()
        self.assertEqual(self.request()['reason'], 'unknown_setup_stage')

    def test_early_shape_and_identity_are_required(self):
        for changes, reason in [({'schema_version': True}, 'invalid_registration'),
                                ({'phase': 'creating'}, 'not_early_setup'),
                                ({'phase': 'ready'}, 'not_early_setup'),
                                ({'lead': {}}, 'not_early_setup'),
                                ({'workers': [{}]}, 'not_early_setup'),
                                ({'extra_leads': [{}]}, 'not_early_setup'),
                                ({'run_id': 'r1'}, 'not_early_setup'),
                                ({'setup_stage': 'members'}, 'unknown_setup_stage'),
                                ({'tab': 'other'}, 'invalid_registration')]:
            before = copy.deepcopy(self.state)
            with self.subTest(changes=changes):
                self.state.update(changes)
                self.write_state()
                raw = self.path.read_bytes()
                self.assertEqual(self.request()['reason'], reason)
                self.assertEqual(self.path.read_bytes(), raw)
            self.state = before
        self.write_state()
        for key, value in [('name', 'different-agent'), ('terminal_id', 'replaced'), ('tab_id', 'moved'),
                           ('agent', 'claude'), ('pane_id', 'p2'), ('cwd', str(self.root))]:
            original = self.owner[key]
            with self.subTest(key=key):
                self.owner[key] = value
                self.assertEqual(self.request()['reason'], 'identity_changed')
                self.owner[key] = original

    def test_extra_panes_or_moved_named_members_block(self):
        self.layout['panes'].append({'pane_id': 'p2'})
        self.assertEqual(self.request()['reason'], 'extra_panes')
        self.layout['panes'].pop()
        self.others = [{'pane_id': 'other', 'name': self.prefix + '-worker', 'tab_id': 'other-tab'}]
        self.assertEqual(self.request()['reason'], 'other_members')

    def test_bindings_are_read_only_and_stale_bindings_block(self):
        directory = self.repo / '.shop'
        directory.mkdir()
        path = directory / 'bindings.json'
        path.write_text(json.dumps({'other-run': {'shop_id': 'other', 'tab': 'other'}}))
        before = path.read_bytes()
        self.assertEqual(self.request()['decision'], 'ready')
        self.assertEqual(path.read_bytes(), before)
        path.write_text(json.dumps({'r1': {'shop_id': self.state['shop_id']}}))
        self.assertEqual(self.request()['reason'], 'binding_conflict')
        self.assertEqual(sorted(p.name for p in directory.iterdir()), ['bindings.json'])

    def test_stale_state_or_observation_never_applies(self):
        plan = self.request()
        self.state['error'] = 'changed failure'
        self.write_state()
        with self.assertRaisesRegex(RuntimeError, 'changed or blocked'):
            self.apply(plan)
        plan = self.request()
        self.owner['name'] = self.state['architect']['name']
        with self.assertRaisesRegex(RuntimeError, 'changed or blocked'):
            self.apply(plan)
        self.assertTrue(self.path.exists())
        self.assertFalse((self.runtime.parent / 'reset-archive').exists())

    def test_backup_failure_keeps_original_and_never_overwrites_evidence(self):
        original = self.path.read_bytes()
        plan = self.request()
        with patch('reset.os.fsync', side_effect=OSError('fixture disk failure')):
            with self.assertRaisesRegex(OSError, 'fixture disk failure'):
                self.apply(plan)
        self.assertEqual(self.path.read_bytes(), original)
        with self.assertRaises(FileExistsError):
            self.apply(plan)
        self.assertEqual(self.path.read_bytes(), original)

    def test_facts_change_after_backup_keeps_registration_and_backup(self):
        plan = self.request()
        original = reset.plan
        count = 0
        def changing(*args):
            nonlocal count
            count += 1
            if count == 2:
                self.owner['terminal_id'] = 'replaced'
            return original(*args)
        with patch('reset.plan', side_effect=changing):
            with self.assertRaisesRegex(RuntimeError, 'registration retained'):
                self.apply(plan)
        self.assertTrue(self.path.exists())
        self.assertEqual(len(list((self.runtime.parent / 'reset-archive').glob('*.json'))), 1)

    def test_apply_requires_confirmation_context_and_nonbusy_tab(self):
        plan = self.request()
        with self.assertRaisesRegex(RuntimeError, 'confirmed'):
            self.request(action='apply', token=plan['token'])
        with self.assertRaisesRegex(RuntimeError, 'confirmed'):
            self.request(action='apply', token='../other', confirmed=True)
        import locking
        with self.path.with_suffix('.lock').open('r+') as lock:
            locking.acquire(lock)
            with self.assertRaisesRegex(RuntimeError, 'active for this tab'):
                self.request()
        self.env['HERDR_PANE_ID'] = ''
        with self.assertRaisesRegex(RuntimeError, 'context'):
            self.request()

    def test_symlinks_invalid_and_oversized_files_are_refused(self):
        original = self.path.read_bytes()
        self.path.unlink()
        target = self.root / 'external.json'
        target.write_bytes(original)
        self.path.symlink_to(target)
        with self.assertRaisesRegex(RuntimeError, 'Symlink'):
            self.request()
        self.assertEqual(target.read_bytes(), original)
        self.path.unlink()
        for data in [b'{' , b'x' * 65537]:
            self.path.write_bytes(data)
            with self.assertRaises((RuntimeError, ValueError)):
                self.request()
            self.assertEqual(self.path.read_bytes(), data)
        self.write_state()
        (self.runtime.parent / 'reset-archive').symlink_to(self.repo, target_is_directory=True)
        with self.assertRaisesRegex(RuntimeError, 'Symlink'):
            self.apply(self.request())
        self.assertEqual(list(self.repo.iterdir()), [])

    def test_cli_request_bridge_fence_and_json_response(self):
        import settings
        from types import SimpleNamespace
        payload = {'action': 'preview', 'expected_state_dir': str(self.runtime.parent),
                   'expected_config_dir': str(self.root / 'config')}
        with patch.object(settings, 'STATE', self.runtime.parent), \
                patch.object(settings, 'CONFIG', self.root / 'config'), \
                patch('reset.herdr.Herdr', return_value=SimpleNamespace(api=self.api)), \
                patch.dict(os.environ, self.env):
            output = io.StringIO()
            with patch('sys.argv', ['reset', '--request', json.dumps(payload)]), contextlib.redirect_stdout(output):
                reset.main()
            self.assertEqual(json.loads(output.getvalue())['decision'], 'ready')
            self.calls.clear()
            payload['expected_state_dir'] = '/wrong/state'
            with patch('sys.argv', ['reset', '--request', json.dumps(payload)]):
                with self.assertRaisesRegex(RuntimeError, 'bridge changed'):
                    reset.main()
            self.assertEqual(self.calls, [])

    def test_unknown_observations_fail_without_mutation(self):
        with patch.object(self, 'api', side_effect=RuntimeError('offline host unavailable')):
            with self.assertRaisesRegex(RuntimeError, 'offline host unavailable'):
                self.request()
        self.assertTrue(self.path.exists())
