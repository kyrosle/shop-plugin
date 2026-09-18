"""Publication privacy checks; no credentials, model calls or live host queries."""
import json
from pathlib import Path
import re
import unittest
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]


class PublicationPrivacyTests(unittest.TestCase):
    def test_local_sync_helper_is_git_ignored(self):
        rules = (ROOT / '.gitignore').read_text().splitlines()
        self.assertIn('/sync', rules)

    def test_snapshot_generator_path_is_synthetic(self):
        fixture = ROOT / 'tests/fixtures-snapshot/snapshot-sample.json'
        snapshot = json.loads(fixture.read_text())
        self.assertEqual(snapshot['generator']['core_root'], '/opt/shop-plugin')

    def test_only_public_user_guides_are_present(self):
        guides = {'ARCHITECTURE.md', 'INSTALLATION.md', 'MIGRATION.md', 'MODELS.md', 'WORKBENCH.md', 'WORKFLOW.md', 'LANGUAGE.md'}
        public = {f'{lang}/{name}' for lang in ('en', 'zh-CN') for name in guides}
        self.assertEqual({str(path.relative_to(ROOT / 'docs')) for path in (ROOT / 'docs').rglob('*.md')}, public)
        package = json.loads((ROOT / 'package.json').read_text())
        self.assertNotIn('docs', package['files'])
        self.assertEqual({name for name in package['files'] if name.startswith('docs/')},
                         {'docs/' + name for name in public})

    def test_internal_records_have_git_and_package_exclusions(self):
        git_rules = (ROOT / '.gitignore').read_text().splitlines()
        npm_rules = (ROOT / 'docs/.npmignore').read_text().splitlines()
        for name in ('PLAN-*.md', 'SPEC-*.md', 'REFERENCE-ANALYSIS.md', 'PUBLISHING.md'):
            self.assertIn('/docs/' + name, git_rules)
            self.assertIn(name, npm_rules)
            self.assertIn('/docs/**/' + name, git_rules)
            self.assertIn('**/' + name, npm_rules)

    def test_public_markdown_links_resolve(self):
        paths = [ROOT / 'README.md', ROOT / 'README.zh-CN.md', ROOT / 'THIRD_PARTY.md', ROOT / 'transport/NOTICE.md',
                 *sorted((ROOT / 'docs').rglob('*.md'))]
        for path in paths:
            for target in re.findall(r'\[[^\]]+\]\(([^)]+)\)', path.read_text()):
                if target.startswith('#') or urlsplit(target).scheme:
                    continue
                destination = unquote(target.partition('#')[0])
                with self.subTest(file=path.name, target=destination):
                    self.assertTrue((path.parent / destination).is_file(), 'Public documentation link is broken')

    def test_json_fixtures_do_not_contain_personal_home_paths(self):
        home = re.compile(r'(?:/Users/|/home/)[^/\s"\'<>]+|[A-Za-z]:\\\\Users\\\\')
        for path in (ROOT / 'tests').rglob('*.json'):
            with self.subTest(file=str(path.relative_to(ROOT))):
                self.assertIsNone(home.search(path.read_text()),
                                  'Fixture contains a personal home path; replace it with a synthetic path')


if __name__ == '__main__':
    unittest.main()
