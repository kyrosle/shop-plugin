"""Offline language contract: preferences never touch Shop/runtime/model state."""
import ast
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import language

ROOT = Path(__file__).resolve().parents[1]


class LanguageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = self.root / 'config'
        self.path = self.config / 'language.json'
        self.env = patch.dict(os.environ, {'SHOP_CONFIG_DIR': str(self.config), 'LC_ALL': 'en_US.UTF-8'})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def save(self, value):
        current = language.read_preference()
        return language.save_preference(value, current['revision'], current['path'])

    def test_detection_priority_and_unsupported_fallback(self):
        for env, expected in [({}, 'en'), ({'LANG': 'zh_CN.UTF-8'}, 'zh-CN'),
                ({'LANG': 'zh-TW'}, 'zh-CN'), ({'LANG': 'ZH_hans'}, 'zh-CN'),
                ({'LANG': 'zh_CN', 'LC_MESSAGES': 'en_GB'}, 'en'),
                ({'LANG': 'en', 'LC_MESSAGES': 'zh_CN', 'LC_ALL': ''}, 'zh-CN'),
                ({'LANG': 'zh_CN', 'LC_ALL': 'C'}, 'en'), ({'LANG': 'fr_FR'}, 'en'),
                ({'LANG': 'zhgarbage'}, 'en')]:
            self.assertEqual(language.detect_language(env), expected)

    def test_missing_is_read_only_explicit_wins_auto_uses_environment(self):
        self.assertEqual(language.read_preference()['revision'], 'missing')
        self.assertFalse(self.config.exists())
        self.save('zh-CN')
        self.assertEqual(language.resolve_language(), 'zh-CN')
        self.save('auto')
        self.assertEqual(language.resolve_language(), 'en')
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertFalse((self.root / 'state').exists())
        self.assertFalse((self.config / 'settings.json').exists())
        self.assertEqual({p.name for p in self.config.iterdir()}, {'language.json', 'language.lock'})

    def test_existing_models_and_running_state_stay_byte_identical(self):
        self.config.mkdir()
        state = self.root / 'state/runtime'
        state.mkdir(parents=True)
        protected = {
            self.config / 'settings.json': '{"version":1,"models":{"lead":"p/model"}}',
            state / 'active.json': '{"phase":"ready","run_id":"r","model_profiles":{"lead":{"model":"p/model"}}}',
        }
        for path, content in protected.items():
            path.write_text(content)
        self.save('zh-CN')
        self.save('en')
        for path, content in protected.items():
            self.assertEqual(path.read_bytes(), content.encode())

    def test_stale_revision_and_changed_location_refuse_without_overwrite(self):
        stale = language.read_preference()
        self.save('en')
        original = self.path.read_bytes()
        with self.assertRaisesRegex(RuntimeError, 'changed'):
            language.save_preference('zh-CN', stale['revision'], stale['path'])
        with self.assertRaisesRegex(RuntimeError, 'location changed'):
            language.save_preference('zh-CN', 'missing', str(self.root / 'elsewhere'))
        self.assertEqual(self.path.read_bytes(), original)

    def test_invalid_values_and_documents_never_get_overwritten(self):
        for invalid in ['fr', '', '../zh-CN']:
            with self.assertRaisesRegex(RuntimeError, 'Expected'):
                language.save_preference(invalid, 'missing', str(self.path))
        self.config.mkdir()
        for raw in ['{', '[]', 'null', '{"version":true,"language":"en"}',
                    '{"version":2,"language":"en"}', '{"version":1,"language":"fr"}',
                    '{"version":1,"language":"en","models":{}}', 'x' * 4097]:
            self.path.write_text(raw)
            self.assertEqual(language.resolve_language(), 'en')
            with self.assertRaises((ValueError, RuntimeError)):
                language.read_preference()
            with self.assertRaises((ValueError, RuntimeError)):
                language.save_preference('zh-CN', 'missing', str(self.path))
            self.assertEqual(self.path.read_text(), raw)

    def test_competing_process_lock_refuses_without_rewriting(self):
        self.save('en')
        original = self.path.read_bytes()
        with (self.config / 'language.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = subprocess.run(['python3', str(ROOT / 'core/language.py'), 'zh-CN'],
                env=os.environ, capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 1)
        self.assertIn('Another language update is active', result.stderr)
        self.assertEqual(self.path.read_bytes(), original)

    def test_symlink_not_replaced_and_atomic_failure_keeps_previous_value(self):
        self.save('en')
        original = self.path.read_bytes()
        with patch('language.os.replace', side_effect=OSError('simulated disk failure')):
            with self.assertRaisesRegex(OSError, 'simulated'):
                self.save('zh-CN')
        self.assertEqual(self.path.read_bytes(), original)
        self.assertFalse(list(self.config.glob('.language-*')))
        other = self.root / 'other.json'
        self.path.rename(other)
        self.path.symlink_to(other)
        with self.assertRaisesRegex(RuntimeError, 'symlink'):
            self.save('zh-CN')
        self.assertEqual(other.read_bytes(), original)

    def test_translation_parity_and_values_are_opaque(self):
        en, zh = language.CATALOGS['en'], language.CATALOGS['zh-CN']
        self.assertEqual(set(en), set(zh))
        for key in en:
            self.assertTrue(zh[key], key)
            self.assertEqual(en[key], key)
            self.assertEqual(sorted(re.findall(r'\{\d+\}|%s', en[key])), sorted(re.findall(r'\{\d+\}|%s', zh[key])), key)
        self.assertEqual(language.t('Missing English key', language='zh-CN'), 'Missing English key')
        self.assertEqual(language.t('Inherit parent: {0}', 'unchanged {1} 中文 /path', language='zh-CN'),
                         '继承父层：unchanged {1} 中文 /path')

    def test_python_owned_message_keys_have_both_translations(self):
        for path in [ROOT / 'core' / name for name in ('language.py', 'configuration.py', 'settings.py')]:
            tree = ast.parse(path.read_text())
            for call in ast.walk(tree):
                if isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and call.func.id == 't' and call.args:
                    arg = call.args[0]
                    key = arg.value if isinstance(arg, ast.Constant) else ast.get_docstring(tree) if isinstance(arg, ast.Name) and arg.id == '__doc__' else None
                    if key is not None:
                        self.assertIn(key, language.CATALOGS['en'], (path.name, key))


if __name__ == '__main__':
    unittest.main()
