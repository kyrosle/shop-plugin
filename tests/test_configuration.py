"""Scoped config, CAS and startup bridge: offline, no Herdr/Pi processes launched."""
import copy
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import configuration as cfg
import settings


class ConfigFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.project = self.root / 'repo'
        self.project.mkdir()
        self.config = self.root / 'config'
        self.config.mkdir()
        self.global_path = self.config / 'settings.json'
        self.project_path = self.project / '.pi/shop.json'

    def write(self, path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))

    def load(self, trusted=True, session=None):
        return cfg.load(str(self.project), trusted, session or {}, self.config)

    def request(self, action, view, scope='session', **extra):
        return dict(action=action, root=str(self.project), trusted=view['trusted'], session={},
                    scope=scope, revisions=view['revisions'], **extra)

    def transaction(self, action, view, scope='session', **extra):
        return cfg.transaction(self.request(action, view, scope, **extra), self.config)


class ConfigurationTests(ConfigFixture):
    def test_scopes_override_fields_and_higher_role_beats_lower_seat(self):
        self.write(self.global_path, {'version': 1, 'models': {
            'defaults': {'model': 'p/base', 'thinking': 'high'},
            'seats': {'worker-2': {'thinking': 'max'}}}})
        self.write(self.project_path, {'version': 1, 'models': {'worker': {'thinking': 'low'}}})
        view = self.load(session={'models': {'seats': {'worker-2': {'model': 'p/session'}}}})
        self.assertEqual(view['layers']['project']['profiles']['worker-2']['thinking'], 'low')
        profile = view['layers']['session']['profiles']['worker-2']
        self.assertEqual(profile, {'model': 'p/session', 'thinking': 'low'})
        self.assertEqual(view['layers']['session']['sources']['worker-2'], {'model': 'session', 'thinking': 'project'})
        self.assertEqual(view['layers']['session']['profiles']['lead'], {'model': 'p/base', 'thinking': 'high'})

    def test_untrusted_project_is_not_read_or_created(self):
        self.write(self.project_path, {'version': 987})
        view = self.load(trusted=False)
        self.assertEqual(view['revisions']['project'], 'untrusted')
        self.assertEqual(view['layers']['session']['profiles']['lead'], {})
        with self.assertRaisesRegex(RuntimeError, 'untrusted'):
            self.transaction('save', view, 'project', reset=True)
        self.assertFalse(Path(str(self.project_path) + '.lock').exists())

    def test_sparse_save_preserves_advanced_groups_and_inherits_parent(self):
        self.write(self.global_path, {'version': 1, 'models': {'defaults': {'model': 'p/base', 'thinking': 'high'}}})
        self.write(self.project_path, {'version': 1, 'advanced': {'retention': 42}})
        view = self.load()
        draft = copy.deepcopy(view['layers']['project']['profiles'])
        draft['lead']['thinking'] = 'low'
        result = self.transaction('save', view, 'project', draft=draft)
        self.assertEqual(result['overrides'], {'models': {'seats': {'lead': {'thinking': 'low'}}}})
        document = json.loads(self.project_path.read_text())
        self.assertEqual(document['advanced'], {'retention': 42})
        self.assertEqual(document['models'], result['overrides']['models'])
        self.assertEqual(self.project_path.stat().st_mode & 0o777, 0o600)
        # Later parent change propagates where the directory has no override.
        self.write(self.global_path, {'version': 1, 'models': {'defaults': {'model': 'p/new', 'thinking': 'max'}}})
        profiles = self.load()['layers']['project']['profiles']
        self.assertEqual(profiles['lead'], {'model': 'p/new', 'thinking': 'low'})
        self.assertEqual(profiles['worker']['thinking'], 'max')

    def test_reset_preserves_advanced_and_null_is_explicit_default(self):
        self.write(self.global_path, {'version': 1, 'models': {'defaults': {'model': 'p/base', 'thinking': 'high'}}})
        self.write(self.project_path, {'version': 1, 'advanced': True})
        view = self.load()
        draft = copy.deepcopy(view['layers']['project']['profiles'])
        draft['worker']['thinking'] = None
        self.transaction('save', view, 'project', draft=draft)
        view = self.load()
        self.assertIsNone(view['layers']['project']['profiles']['worker']['thinking'])
        self.transaction('save', view, 'project', reset=True)
        self.assertEqual(json.loads(self.project_path.read_text()), {'version': 1, 'advanced': True})
        self.assertEqual(self.load()['layers']['project']['profiles']['worker']['thinking'], 'high')

    def test_parent_and_target_changes_refuse_write(self):
        for target in (self.global_path, self.project_path):
            with self.subTest(target=target):
                view = self.load()
                self.write(target, {'version': 1, 'advanced': 'changed'})
                before = target.read_bytes()
                with self.assertRaisesRegex(RuntimeError, 'changed'):
                    self.transaction('save', view, 'project', reset=True)
                self.assertEqual(target.read_bytes(), before)

    def test_lock_collision_refuses_write(self):
        view = self.load()
        with cfg.file_lock(self.global_path), self.assertRaisesRegex(RuntimeError, 'being edited'):
            self.transaction('save', view, 'global', reset=True)
        self.assertFalse(self.global_path.exists())

    def test_bad_json_unknown_version_and_large_files_fail_closed(self):
        for raw in ('{', '[]', '{}', '{"version":2}', '{"version":true}', ' ' * 65537,
                    '{"version":1,"models":{"worker":{"thinking":"ultra"}}}'):
            with self.subTest(raw=raw[:80]):
                self.global_path.write_text(raw)
                with self.assertRaises((RuntimeError, ValueError)):
                    self.load()

    def test_explicit_migration_preview_then_apply_and_no_overwrite(self):
        legacy = self.config / 'models.json'
        raw = {'lead': 'p/lead', 'worker': 'p/worker'}
        self.write(legacy, raw)
        original = legacy.read_bytes()
        view = self.load()
        self.assertTrue(view['legacy'])
        preview = self.transaction('migrate', view)
        self.assertFalse(preview['applied'])
        self.assertFalse(self.global_path.exists())
        with self.assertRaisesRegex(RuntimeError, 'Migrate legacy'):
            self.transaction('save', view, 'global', reset=True)
        self.transaction('migrate', view, apply=True)
        self.assertEqual(legacy.read_bytes(), original)
        self.assertEqual(json.loads(self.global_path.read_text()), preview['proposed'])
        self.assertFalse(self.load()['legacy'])
        with self.assertRaises(RuntimeError):
            self.transaction('migrate', self.load(), apply=True)
        with patch.object(settings, 'CONFIG', self.config):
            self.assertEqual(settings.models(), raw)

    def test_session_prepare_returns_metadata_only_and_rejects_unknown_keys(self):
        view = self.load()
        draft = copy.deepcopy(view['layers']['session']['profiles'])
        draft['lead']['model'] = 'p/m'
        result = self.transaction('prepare', view, draft=draft)
        self.assertEqual(result['overrides'], {'models': {'seats': {'lead': {'model': 'p/m'}}}})
        self.assertFalse(self.global_path.exists())
        self.assertFalse(self.project_path.exists())
        with self.assertRaisesRegex(RuntimeError, 'Pi custom entries'):
            self.transaction('save', view, draft=draft)
        with self.assertRaises(RuntimeError):
            self.load(session={'unsafe': True})


class CandidateTests(ConfigFixture):
    def setUp(self):
        super().setUp()
        self.write(self.global_path, {'version': 1, 'models': {'defaults': {'model': 'p/global', 'thinking': 'high'}}})
        self.pane = {'pane_id': 'p1', 'tab_id': 't1'}
        self.path = cfg.candidate_path(self.root, '/socket', 't1', 'p1')
        self.record = {'schema': 1, 'socket': '/socket', 'tab': 't1', 'pane': 'p1',
                       'terminal': 'terminal', 'session_id': 'session', 'instance': 'instance',
                       'pid': os.getpid(), 'root': str(self.project), 'trusted': True,
                       'updated_at': time.time(), 'overrides': {'models': {'seats': {'worker-2': {'model': 'p/session'}}}}}
        self.write(self.path, self.record)

    def api(self, *args):
        self.assertEqual(args, ('agent', 'get', 'p1'))
        return {'agent': {'agent': 'pi', 'terminal_id': 'terminal', 'tab_id': 't1'}}

    def startup(self):
        return cfg.startup_profiles(self.api, self.pane, str(self.project), '/socket', self.root, self.config)

    def test_candidate_consumes_all_layers_and_returns_provenance(self):
        self.write(self.project_path, {'version': 1, 'models': {'worker': {'thinking': 'low'}}})
        profiles, source = self.startup()
        self.assertEqual(profiles['worker-2'], {'model': 'p/session', 'thinking': 'low'})
        self.assertEqual(source['sources']['worker-2'], {'model': 'session', 'thinking': 'project'})
        self.assertEqual(source['session_id'], 'session')

    def test_invalid_candidate_identity_timestamp_or_process_refused(self):
        bad = [('schema', 2), ('terminal', 'other'), ('tab', 'other'), ('pane', 'other'),
               ('socket', 'other'), ('root', '/different'), ('session_id', ''), ('pid', -1),
               ('updated_at', time.time() - 60), ('updated_at', time.time() + 60), ('trusted', 'yes')]
        for field, value in bad:
            with self.subTest(field=field):
                self.write(self.path, dict(self.record, **{field: value}))
                with self.assertRaises(RuntimeError):
                    self.startup()
        self.write(self.path, self.record)
        with patch.object(cfg.os, 'kill', side_effect=ProcessLookupError), self.assertRaisesRegex(RuntimeError, 'process unavailable'):
            self.startup()

    def test_missing_candidate_never_silently_uses_global(self):
        self.path.unlink()
        with self.assertRaisesRegex(RuntimeError, 'Missing Pi settings candidate'):
            self.startup()

    def test_candidate_change_during_live_check_refused(self):
        original_api = self.api
        def api(*args):
            self.write(self.path, dict(self.record, session_id='replacement'))
            return original_api(*args)
        with patch.object(self, 'api', api), self.assertRaisesRegex(RuntimeError, 'changed during setup'):
            self.startup()


if __name__ == '__main__':
    unittest.main()
