"""Package/license/migration audits: allowlist, version sync, docs, executable bits.

Read-only: inspects the checked-out package metadata and runs `npm pack --dry-run`
in a temp directory, so nothing is published and no file is modified.
"""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / 'herdr-plugin.toml'
FORBIDDEN_IN_TARBALL = ('tests/', '.shop/', 'node_modules/', 'runtime/', 'sessions/', '.env',
                        'credentials', 'evidence/', '__pycache__', '.pyc', '.pyo',
                        'docs/PLAN-', 'docs/SPEC-', 'docs/REFERENCE-ANALYSIS.md', 'docs/PUBLISHING.md')


class PackageMetadataTests(unittest.TestCase):
    def setUp(self):
        self.package = json.loads((ROOT / 'package.json').read_text())
        self.manifest = MANIFEST.read_text()

    def test_versions_stay_in_sync_with_the_manifest(self):
        match = [line for line in self.manifest.splitlines() if line.startswith('version =')]
        self.assertTrue(match)
        self.assertEqual(json.loads(match[0].split('=', 1)[1].strip()), self.package['version'])
        self.assertIn('min_herdr_version', self.manifest)

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
        for required in ('core', 'bin', 'extensions', 'transport', 'roles', 'config',
                         'docs/ARCHITECTURE.md', 'docs/MIGRATION.md', 'docs/MODELS.md',
                         'docs/WORKBENCH.md', 'docs/WORKFLOW.md',
                         'herdr-plugin.toml', 'README.md', 'THIRD_PARTY.md', 'LICENSE',
                         'LICENSE.pi-intercom'):
            self.assertIn(required, files)
        for forbidden in ('tests', '.shop', 'node_modules', 'runtime', 'sessions', 'docs'):
            self.assertNotIn(forbidden, files)

    def test_plugin_actions_include_user_action_shutdown_preview_and_recovery(self):
        for action in ('id = "open"', 'id = "close"', 'id = "status"', 'id = "recover"',
                       'id = "doctor"', 'id = "shutdown-preview"', 'id = "recovery"'):
            self.assertIn(action, self.manifest)
        self.assertIn('command = ["python3", "core/plugin.py", "close"]', self.manifest)
        self.assertEqual(self.manifest.count('[[events]]'), 3)

    def test_wrappers_are_executable(self):
        for name in ('herdr-shop', 'shop-run', 'shop-transport'):
            path = ROOT / 'bin' / name
            self.assertTrue(path.is_file(), name)
            self.assertTrue(path.stat().st_mode & 0o111, name)

    def test_attribution_and_migration_docs_exist(self):
        for name in ('THIRD_PARTY.md', 'LICENSE.pi-intercom', 'transport/NOTICE.md',
                     'docs/MIGRATION.md', 'docs/ARCHITECTURE.md', 'docs/WORKFLOW.md', 'README.md'):
            self.assertTrue((ROOT / name).is_file(), name)
        workflow = (ROOT / 'docs/WORKFLOW.md').read_text()
        self.assertTrue('shutdown' in workflow.lower() or '收工' in workflow)


class TarballAuditTests(unittest.TestCase):
    def test_pack_dry_run_lists_allowlisted_files_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = subprocess.run(['npm', 'pack', '--dry-run', '--json'], cwd=ROOT,
                                    capture_output=True, text=True, timeout=180,
                                    env={**__import__('os').environ, 'npm_config_cache': tmp})
            if result.returncode != 0:
                self.skipTest('npm pack unavailable: ' + (result.stderr or result.stdout)[:200])
            payload = json.loads(result.stdout)[0]
            names = [entry['path'] for entry in payload['files']]
            self.assertIn('core/shutdown.py', names)
            self.assertIn('core/workbench.py', names)
            self.assertIn('extensions/workbench-ui.ts', names)
            self.assertIn('docs/WORKBENCH.md', names)
            self.assertNotIn('sync', names)
            self.assertIn('bin/herdr-shop', names)
            self.assertIn('LICENSE.pi-intercom', names)
            self.assertIn('docs/MIGRATION.md', names)
            for name in names:
                for forbidden in FORBIDDEN_IN_TARBALL:
                    self.assertNotIn(forbidden, name, name)
            for entry in payload['files']:
                if entry['path'].startswith('bin/'):
                    self.assertEqual(entry['mode'] & 0o111, 0o111, entry['path'])
            self.assertLess(payload['size'], 400 * 1024)
            self.assertLess(payload['unpackedSize'], 1024 * 1024)


if __name__ == '__main__':
    unittest.main()
