"""Package audits: allowlist, license status, docs. Read-only; `npm pack --dry-run` in a temp cache."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
GUIDES = ('INSTALLATION', 'SEATS', 'MODELS', 'LANGUAGE', 'TESTING')
FORBIDDEN_IN_TARBALL = ('tests/', '.shop/', 'node_modules/', 'runtime/', 'sessions/', '.env',
                        'credentials', 'evidence/', '__pycache__', '.pyc', '.pyo')


class PackageMetadataTests(unittest.TestCase):
    def setUp(self):
        self.package = json.loads((ROOT / 'package.json').read_text())

    def test_single_pi_entrypoint_and_no_herdr_plugin(self):
        self.assertEqual(self.package['pi']['extensions'], ['./extensions/index.ts'])
        self.assertNotIn('herdr-plugin', self.package['keywords'])
        self.assertFalse((ROOT / 'herdr-plugin.toml').exists())
        self.assertIn('pi-context-curator', self.package['dependencies'])
        self.assertTrue(self.package['dependencies']['pi-context-curator'].startswith('https://'))

    def test_stays_private_with_declared_engine_and_no_fabricated_license(self):
        self.assertIs(self.package['private'], True)
        self.assertEqual(self.package['engines']['node'], '>=22.19.0')
        # The project license is a pending owner decision: no license field may claim one.
        self.assertNotIn('license', self.package)
        license_text = (ROOT / 'LICENSE').read_text()
        self.assertIn('License decision pending', license_text)
        self.assertIn('not authorized until', license_text)

    def test_files_allowlist_covers_runtime_assets_and_excludes_tests_runtime_and_secrets(self):
        files = self.package['files']
        for required in ('core', 'extensions', 'roles', 'locales/en.json', 'locales/zh-CN.json',
                         'README.md', 'README.zh-CN.md', 'LICENSE'):
            self.assertIn(required, files)
        for language in ('en', 'zh-CN'):
            for guide in GUIDES:
                self.assertIn(f'docs/{language}/{guide}.md', files)
                self.assertTrue((ROOT / 'docs' / language / f'{guide}.md').is_file())
        for forbidden in ('tests', '.shop', 'node_modules', 'runtime', 'sessions', 'docs', 'locales', 'bin', 'transport'):
            self.assertNotIn(forbidden, files)


class TarballAuditTests(unittest.TestCase):
    def test_pack_dry_run_lists_allowlisted_files_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run(['npm', 'pack', '--dry-run', '--json'], cwd=ROOT,
                                    capture_output=True, text=True, timeout=180,
                                    env={**os.environ, 'npm_config_cache': tmp})
            if result.returncode != 0:
                self.skipTest('npm pack unavailable: ' + (result.stderr or result.stdout)[:200])
            payload = json.loads(result.stdout)[0]
            names = [entry['path'] for entry in payload['files']]
            for required in ('extensions/index.ts', 'extensions/seats.ts', 'extensions/seat-curation.ts',
                             'core/configuration.py', 'core/language.py', 'roles/seat-lead.md', 'roles/seat-worker.md',
                             'README.zh-CN.md'):
                self.assertIn(required, names)
            for language in ('en', 'zh-CN'):
                self.assertIn(f'locales/{language}.json', names)
                for guide in GUIDES:
                    self.assertIn(f'docs/{language}/{guide}.md', names)
            self.assertNotIn('sync', names)
            for name in names:
                for forbidden in FORBIDDEN_IN_TARBALL:
                    self.assertNotIn(forbidden, name, name)
            self.assertLess(payload['size'], 200 * 1024)


if __name__ == '__main__':
    unittest.main()
