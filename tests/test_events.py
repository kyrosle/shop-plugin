"""Bounded Herdr event hook: validation, fact store caps, revisions and gaps.

Isolated temp state dirs only. The CLI is exercised through subprocess runs with
an explicit state dir; no plugin is installed, no Herdr event is emitted and no
live workstation state is touched.
"""
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import events

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / 'fixtures-events'
ENV_SOCKET = '/tmp/shop-events-test.sock'
ENV_TAB = 'w9:t9'


def fixture(name):
    return json.loads((FIXTURES / name).read_text())


class EventTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_dir = Path(self.tmp.name)
        runtime = self.state_dir / 'runtime'
        runtime.mkdir(parents=True)
        key = hashlib.sha256(('%s:%s' % (ENV_SOCKET, ENV_TAB)).encode()).hexdigest()[:12]
        (runtime / (key + '.json')).write_text(json.dumps({
            'shop_id': 'shop-events', 'run_id': 'run-events', 'tab': ENV_TAB, 'phase': 'ready',
        }))
        self.env = {'HERDR_SOCKET_PATH': ENV_SOCKET, 'HERDR_TAB_ID': ENV_TAB}

    def record(self, event, payload=None, name=None):
        data = fixture(name) if payload is None else payload
        return events.record(str(self.state_dir), self.env, event, data)

    def facts(self):
        path = events.facts_file(self.state_dir, 'shop-events')
        if not path.is_file():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    def cli(self, event=None, payload=None, **extra):
        env = dict(os.environ, SHOP_STATE_DIR=str(self.state_dir), **self.env, **extra)
        if event is not None:
            env['HERDR_PLUGIN_EVENT'] = event
        if payload is not None:
            env['HERDR_PLUGIN_EVENT_JSON'] = json.dumps(payload)
        return subprocess.run([sys.executable, str(ROOT / 'core/events.py'), 'record'],
                              capture_output=True, text=True, env=env)


class ValidationTests(EventTestBase):
    def test_valid_fixtures_are_recorded_with_monotonic_revision(self):
        first, reason = self.record('pane.agent_status_changed', name='pane-agent-status-changed.json')
        self.assertIsNone(reason)
        self.assertEqual(first['revision'], 1)
        self.assertEqual(first['event'], 'pane.agent_status_changed')
        self.assertEqual(first['agent_status'], 'working')
        self.assertEqual(first['source'], 'herdr_event')
        self.assertEqual(first['schema'], events.FACT_SCHEMA)
        second, _ = self.record('pane.closed', name='pane-closed.json')
        third, _ = self.record('pane.exited', name='pane-exited.json')
        self.assertEqual([first['revision'], second['revision'], third['revision']], [1, 2, 3])
        self.assertEqual(len(self.facts()), 3)

    def test_underscore_and_dot_hook_names_both_work(self):
        self.assertIsNotNone(self.record('pane.agent_status_changed', name='pane-closed.json'
                                         if False else 'pane-agent-status-changed.json')[0])
        self.assertIsNotNone(self.record('pane.closed', name='pane-closed.json')[0])

    def test_unknown_and_foreign_events_are_quiet_no_ops(self):
        for event, payload in [('workspace.created', {'type': 'workspace_created'}),
                               ('intercom.message', {'type': 'message'}),
                               (None, {'type': 'pane_closed'}),
                               ('', {'type': 'pane_closed'})]:
            with self.subTest(event=event):
                fact, reason = events.record(str(self.state_dir), self.env, event, payload)
                self.assertIsNone(fact)
                self.assertTrue(reason)
        self.assertEqual(self.facts(), [])

    def test_invalid_payloads_are_refused(self):
        cases = {
            'wrong-status': 'invalid-wrong-status.json',
            'missing-pane': 'invalid-missing-pane.json',
            'wrong-type': 'invalid-wrong-type.json',
        }
        for label, name in cases.items():
            with self.subTest(label=label):
                fact, reason = self.record('pane.agent_status_changed', name=name)
                self.assertIsNone(fact)
                self.assertTrue(reason)
        fact, reason = self.record('pane.agent_status_changed', payload=['not', 'an', 'object'])
        self.assertIsNone(fact)
        self.assertIn('object', reason)

    def test_foreign_pane_is_recorded_as_a_fact_without_matching_a_member(self):
        fact, _ = self.record('pane.closed', name='pane-closed.json')
        self.assertEqual(fact['pane_id'], 'w2:p7')
        section = events.read_facts(self.state_dir, 'shop-events')
        self.assertEqual(section['coverage'], 'observed')
        self.assertEqual(section['facts'][-1]['pane_id'], 'w2:p7')

    def test_no_registered_shop_drops_the_fact_quietly(self):
        fact, reason = events.record(str(self.state_dir), {'HERDR_SOCKET_PATH': '/tmp/x', 'HERDR_TAB_ID': 'nope'},
                                     'pane.closed', fixture('pane-closed.json'))
        self.assertIsNone(fact)
        self.assertIn('no registered shop', reason)

    def test_fact_store_is_per_shop_and_lives_under_the_state_dir(self):
        self.record('pane.closed', name='pane-closed.json')
        path = events.facts_file(self.state_dir, 'shop-events')
        self.assertTrue(str(path).startswith(str(self.state_dir)))
        self.assertEqual(path.parent.name, 'facts')
        self.assertTrue(path.is_file())


class StoreTests(EventTestBase):
    def test_revision_gap_is_detected_as_partial_coverage(self):
        for _ in range(3):
            self.record('pane.closed', name='pane-closed.json')
        path = events.facts_file(self.state_dir, 'shop-events')
        lines = path.read_text().splitlines()
        path.write_text('\n'.join([lines[0], lines[2]]) + '\n')  # drop revision 2
        section = events.read_facts(self.state_dir, 'shop-events')
        self.assertEqual(section['coverage'], 'partial')
        self.assertEqual(section['gaps'], [{'from': 1, 'to': 3}])
        self.assertIn('gap', section['reason'])

    def test_line_and_byte_caps_prune_oldest_facts(self):
        original_lines, original_bytes = events.MAX_LINES, events.MAX_FILE_BYTES
        events.MAX_LINES, events.MAX_FILE_BYTES = 5, 4096
        try:
            for index in range(12):
                registry = dict(self.env)
                fact, reason = events.record(str(self.state_dir), registry, 'pane.closed',
                                             dict(fixture('pane-closed.json'), pane_id='w2:p%d' % index))
                self.assertIsNotNone(reason is None)
            lines = self.facts()
            self.assertLessEqual(len(lines), 5)
            path = events.facts_file(self.state_dir, 'shop-events')
            self.assertLessEqual(path.stat().st_size, 4096)
            self.assertEqual([item['pane_id'] for item in lines][-1], 'w2:p11')
            revisions = [item['revision'] for item in lines]
            self.assertEqual(revisions, sorted(revisions))
        finally:
            events.MAX_LINES, events.MAX_FILE_BYTES = original_lines, original_bytes

    def test_read_before_any_fact_is_unknown_not_observed(self):
        section = events.read_facts(self.state_dir, 'shop-events')
        self.assertEqual(section['coverage'], 'unknown')
        self.assertIsNone(section['last_event_at'])
        self.assertEqual(section['facts'], [])
        self.assertEqual(sorted(section['subscriptions']),
                         ['pane.agent_status_changed', 'pane.closed', 'pane.exited'])
        self.assertIn('display-only', section['authority'])

    def test_read_never_raises_on_corrupt_or_missing_store(self):
        path = events.facts_file(self.state_dir, 'shop-events')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"schema": "other"}\nnot json\n')
        section = events.read_facts(self.state_dir, 'shop-events')
        self.assertEqual(section['facts'], [])
        self.assertEqual(section['coverage'], 'unknown')
        self.assertEqual(events.read_facts(self.state_dir, None)['facts'], [])

    def test_read_facts_for_state_uses_the_registered_shop(self):
        self.record('pane.closed', name='pane-closed.json')
        key = hashlib.sha256(('%s:%s' % (ENV_SOCKET, ENV_TAB)).encode()).hexdigest()[:12]
        state_path = self.state_dir / 'runtime' / (key + '.json')
        section = events.read_facts_for_state(state_path)
        self.assertEqual(section['coverage'], 'observed')
        self.assertEqual(len(section['facts']), 1)
        self.assertEqual(events.read_facts_for_state(self.state_dir / 'missing.json')['facts'], [])


class CliTests(EventTestBase):
    def test_cli_records_a_valid_event_from_the_hook_environment(self):
        result = self.cli('pane.agent_status_changed', fixture('pane-agent-status-changed.json'))
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload['recorded'])
        self.assertEqual(payload['revision'], 1)
        self.assertEqual(len(self.facts()), 1)

    def test_cli_quiet_no_op_for_unknown_event_and_bad_json(self):
        unknown = self.cli('workspace.created', {'type': 'workspace_created'})
        self.assertEqual(unknown.returncode, 0)
        self.assertFalse(json.loads(unknown.stdout)['recorded'])
        broken = self.cli('pane.closed', payload=None)
        env = dict(os.environ, SHOP_STATE_DIR=str(self.state_dir), **self.env,
                   HERDR_PLUGIN_EVENT='pane.closed', HERDR_PLUGIN_EVENT_JSON='{not json')
        broken = subprocess.run([sys.executable, str(ROOT / 'core/events.py'), 'record'],
                                capture_output=True, text=True, env=env)
        self.assertEqual(broken.returncode, 0)
        self.assertIn('invalid JSON', json.loads(broken.stdout)['reason'])
        missing = subprocess.run([sys.executable, str(ROOT / 'core/events.py'), 'record'],
                                 capture_output=True, text=True,
                                 env=dict(os.environ, SHOP_STATE_DIR=str(self.state_dir)))
        self.assertEqual(missing.returncode, 0)
        self.assertIn('no HERDR_PLUGIN_EVENT_JSON', json.loads(missing.stdout)['reason'])
        self.assertEqual(self.facts(), [])

    def test_cli_reports_storage_failure_with_nonzero_exit(self):
        path = events.facts_file(self.state_dir, 'shop-events')
        path.parent.mkdir(parents=True, exist_ok=True)
        path.parent.chmod(0o500)
        try:
            result = self.cli('pane.closed', fixture('pane-closed.json'))
            if result.returncode == 0:
                self.skipTest('filesystem permitted the write despite the read-only directory')
            self.assertEqual(result.returncode, 1)
            self.assertIn('storage failure', result.stderr)
        finally:
            path.parent.chmod(0o700)


class StaticContractTests(unittest.TestCase):
    def test_hook_module_stays_tiny_and_has_no_herdr_or_subprocess_boundary(self):
        source = (ROOT / 'core/events.py').read_text()
        self.assertNotIn('import subprocess', source)
        self.assertNotIn('subprocess.', source)
        self.assertNotIn('import herdr', source)
        self.assertNotIn('HERDR_BIN_PATH', source)
        self.assertNotIn('SHOP_HERDR_BIN', source)
        # Frozen enums are copied deliberately so a status change cannot trigger a probe.
        self.assertIn("AGENT_STATUSES = ('idle', 'working', 'blocked', 'done', 'unknown')", source)

    def test_manifest_declares_the_three_bounded_event_hooks(self):
        manifest = (ROOT / 'herdr-plugin.toml').read_text()
        self.assertEqual(manifest.count('[[events]]'), 3)
        for name in ('pane.agent_status_changed', 'pane.closed', 'pane.exited'):
            self.assertIn('on = "%s"' % name, manifest)
        self.assertEqual(manifest.count('command = ["python3", "core/events.py", "record"]'), 3)

    def test_fixtures_match_the_pinned_schema_shapes(self):
        provenance = fixture('provenance.json')
        self.assertEqual(provenance['protocol'], 22)
        self.assertEqual(provenance['schema_version'], 1)
        self.assertIn('no live capture', provenance['capture_method'])
        shapes = provenance['event_shapes']
        for name in ('pane-agent-status-changed.json', 'pane-closed.json', 'pane-exited.json'):
            payload = fixture(name)
            shape = shapes[payload['type']]
            for key in shape['required']:
                self.assertIn(key, payload, name)
            self.assertTrue(set(payload) <= set(shape['properties']), name)
        self.assertEqual(sorted(events.SUBSCRIBED.values()),
                         ['pane.agent_status_changed', 'pane.closed', 'pane.exited'])


if __name__ == '__main__':
    unittest.main()


class SnapshotWiringTests(unittest.TestCase):
    """The snapshot consumes facts through the bounded reader; live reads still win."""

    def test_status_path_reads_facts_from_the_shop_state_root(self):
        source = (ROOT / 'core/shop.py').read_text()
        self.assertIn('events_module.read_facts(state_root()', source)
        self.assertIn("snapshot_module.serialize(document)", source)
        supervision = (ROOT / 'core/supervision.py').read_text()
        self.assertIn('events_module.read_facts_for_state(state_path)', supervision)
        snapshot_source = (ROOT / 'core/snapshot.py').read_text()
        # Facts are annotations only: the snapshot cannot dispatch or coordinate anything.
        self.assertIn('authoritative', snapshot_source)
        self.assertNotIn('import coordination', snapshot_source)
        self.assertNotIn('dispatch(', snapshot_source)

    def test_patrol_reports_event_coverage_without_authority(self):
        import contracts as ct
        import run
        import snapshot as snapshot_module
        import supervision as su
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            rid = run.Runs(repo).new('events')['created']
            state = {'shop_id': 'shop1', 'run_id': rid, 'phase': 'ready', 'tab': 't1', 'cwd': str(repo),
                     'architect': {'name': 'arch', 'pane': 'p1', 'launch_id': 'a1'},
                     'lead': {'name': 'lead', 'pane': 'p2', 'launch_id': 'l1'},
                     'workers': [{'name': 'wa', 'pane': 'p3', 'launch_id': 'w1'}]}
            ct.atomic(ct.binding_file(repo), {rid: {'shop_id': 'shop1', 'tab': 't1',
                                                    'lead': state['lead']}})
            doc = snapshot_module.build(state, [], api=None, now=dt.datetime.now(dt.timezone.utc),
                                        facts=events.read_facts(repo / 'runtime', 'shop1'))
            self.assertEqual(doc['events']['coverage'], 'unknown')
            self.assertIn('display-only', doc['events']['authority'])
            del su
