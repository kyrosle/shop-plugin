"""Offline lifecycle: real core, adapter, candidate files and process-identity checks.
Only native CLI replies and OS process metadata are substituted; no host/model calls.
"""
import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import configuration
import contracts as ct
import herdr
import lifecycle as lc
import processes
from test_setup_adapter import SetupRunner


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        self.path = self.root / 'runtime' / (hashlib.sha256(b'fixture-socket:t1').hexdigest()[:12] + '.json')
        self.runner = SetupRunner(self.repo)
        for index, role in enumerate(('architect', 'lead', 'worker'), 1):
            pane = 'p' + str(index)
            self.runner.panes[pane] = self.runner.pane(pane, self.repo, 'pi')
            self.runner.agents[pane] = dict(self.runner.panes[pane], name='s-test-' + role)
        self.table = self.runner.process_table()
        self.api = herdr.Herdr(binary='/fake/herdr', runner=self.runner).api
        for mock in (patch.dict(os.environ, {'HERDR_SOCKET_PATH': 'fixture-socket'}),
                     patch.object(processes, 'snapshot', side_effect=lambda: copy.deepcopy(self.table)),
                     patch.object(processes, 'local_server_pid', return_value=100),
                     patch.object(configuration.os, 'kill', return_value=None)):
            mock.start(); self.addCleanup(mock.stop)
        self.candidate = configuration.candidate_path(self.root, 'fixture-socket', 't1', 'p1')
        self.claim = {'schema': 1, 'socket': 'fixture-socket', 'tab': 't1', 'pane': 'p1',
                      'terminal': 'term-p1', 'root': str(self.repo.resolve()), 'pid': 201,
                      'session_id': 'session-1', 'instance': 'reload-1', 'updated_at': time.time()}
        self.write_claim()
        members = [{'pane': 'p' + str(i), 'name': 's-test-' + role, 'terminal_id': 'term-p' + str(i),
                    'launch_id': 'launch-' + str(i)} for i, role in enumerate(('architect', 'lead', 'worker'), 1)]
        self.state = {'shop_id': 'shop-1', 'tab': 't1', 'prefix': 's-test', 'cwd': str(self.repo),
                      'phase': 'ready', 'setup_stage': 'ready', 'architect': members[0],
                      'lead': members[1], 'workers': [members[2]], 'extra_leads': [],
                      'lifecycle': lc.begin(self.api, self.root, self.runner.panes['p1'], self.repo)}
        for member in members[1:]:
            lc.record_member(self.api, self.state, member)
        self.save()

    def write_claim(self):
        ct.atomic(self.candidate, self.claim)

    def save(self):
        ct.atomic(self.path, self.state)

    def probe(self, session='session-1'):
        return lc.preflight(self.api, self.path, self.state, session)

    def disappear(self):
        self.runner.panes = {'p1': self.runner.panes['p1']}
        self.runner.agents = {'p1': self.runner.agents['p1']}
        self.table = {pid: row for pid, row in self.table.items() if pid < 300}

    def reopen(self):
        return lc.open_existing(self.api, self.path, self.state, self.runner.panes['p1'])

    def test_fresh_ready_reuses_incarnation_and_reload_does_not_change_session(self):
        self.assertEqual(self.probe()['decision'], 'ready')
        self.claim['instance'] = 'reload-2'
        self.write_claim()
        self.assertEqual(self.probe()['decision'], 'ready')
        before = self.path.read_bytes()
        self.assertEqual(self.reopen()['reused'], 'shop-1')
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.runner.mutations, [])

    def test_recovery_expiry_or_session_switch_spends_no_permission_and_never_deletes(self):
        for change in ('recovery', 'expired', 'session', 'legacy'):
            with self.subTest(change=change):
                saved_state, saved_claim = copy.deepcopy(self.state), dict(self.claim)
                if change == 'recovery': self.state['recovery_required'] = True
                if change == 'expired': self.claim['updated_at'] = time.time() - 60
                if change == 'session': self.claim['session_id'] = 'another'
                if change == 'legacy': self.state.pop('lifecycle')
                self.save(); self.write_claim()
                before = self.path.read_bytes()
                self.assertEqual(self.probe()['decision'], 'blocked')
                self.assertEqual(self.path.read_bytes(), before)
                self.state, self.claim = saved_state, saved_claim
        self.assertEqual(self.runner.mutations, [])

    def test_same_pid_with_changed_birth_and_moved_or_busy_lead_block(self):
        start = self.table[100]['start']
        self.table[100]['start'] = 'different-server'
        self.assertEqual(self.probe()['decision'], 'blocked')
        self.table[100]['start'] = start
        self.table[301]['start'] = 'replacement-member'
        self.assertIn('incarnation changed', self.probe()['reason'])
        self.table[301]['start'] = start
        self.runner.panes['p2']['tab_id'] = 'other-tab'
        self.assertIn('moved', self.probe()['reason'])
        self.runner.panes['p2']['tab_id'] = 't1'
        self.runner.agents['p2']['agent_status'] = 'working'
        self.assertIn('busy', self.probe()['reason'])

    def test_binding_mismatch_and_unknown_worker_block_without_mutation(self):
        ct.atomic(ct.binding_file(self.repo), {'old-run': {'shop_id': 'shop-1', 'tab': 't1', 'state_path': str(self.path)}})
        self.assertIn('binding mismatch', self.probe()['reason'])
        ct.atomic(ct.binding_file(self.repo), {})
        self.runner.agents['p3']['agent_status'] = 'unknown'
        self.assertIn('unverified', self.probe()['reason'])
        self.assertEqual(self.runner.mutations, [])

    def test_other_server_with_same_tab_id_is_not_this_shop(self):
        ct.atomic(ct.binding_file(self.repo), {'foreign-run': {'shop_id': 'foreign', 'tab': 't1', 'state_path': '/other/runtime/slot.json'}})
        self.assertEqual(self.probe()['decision'], 'ready')
        self.disappear()
        self.reopen()
        self.assertIn('foreign-run', ct.bindings(self.repo))

    def test_unused_orphan_archive_is_exact_private_and_other_artifacts_survive(self):
        self.disappear()
        other = self.root / 'runtime/other.json'
        ct.atomic(other, {'shop_id': 'do-not-touch'})
        session = self.root / 'runtime/sessions/shop-1/history.jsonl'
        session.parent.mkdir(parents=True); session.write_text('old session\n')
        report = self.repo / 'SUMMARY.md'; report.write_text('retained\n')
        before = self.path.read_bytes()
        result = self.reopen()
        archive = Path(result['archive'])
        self.assertEqual(result['closes_panes'], 0)
        self.assertEqual(archive.read_bytes(), before)
        self.assertEqual(archive.stat().st_mode & 0o777, 0o600)
        self.assertFalse(self.path.exists())
        self.assertTrue(other.exists() and session.exists() and report.exists())
        self.assertEqual(self.runner.mutations, [])

    def test_missing_panes_never_mean_original_processes_exited(self):
        table = copy.deepcopy(self.table)
        self.disappear(); self.table = table
        before = self.path.read_bytes()
        with self.assertRaisesRegex(RuntimeError, 'process still alive'):
            self.reopen()
        self.assertEqual(self.path.read_bytes(), before)

    def test_orphan_process_group_and_unknown_native_enumeration_block(self):
        self.disappear()
        self.table[900] = dict(self.table[201], pid=900, sid=300, pgid=900, tty='??')
        with self.assertRaisesRegex(RuntimeError, 'session/group/terminal'):
            self.reopen()
        self.table.pop(900)
        with patch.object(self.runner, 'probe_runner', side_effect=OSError('native unavailable')):
            # New adapter forces an unavailable probe; never interpret as empty enumeration.
            with self.assertRaises(Exception):
                lc.open_existing(herdr.Herdr(binary='/fake/herdr', runner=self.runner).api,
                                 self.path, self.state, self.runner.panes['p1'])
        self.assertTrue(self.path.exists())

    def test_bound_history_unknown_launch_and_foreign_slot_never_auto_archive(self):
        self.disappear()
        for change in ('history', 'binding', 'anchor', 'slot'):
            with self.subTest(change=change):
                old = copy.deepcopy(self.state)
                if change == 'history': self.state['has_run_history'] = True
                if change == 'binding': self.state['run_id'] = 'R'
                if change == 'anchor': self.state['lead'].pop('process_anchor')
                if change == 'slot': self.state['architect']['pane'] = 'foreign-pane'
                self.save()
                before = self.path.read_bytes()
                with self.assertRaises(Exception): self.reopen()
                self.assertEqual(self.path.read_bytes(), before)
                self.state = old

    def test_backup_failure_or_changed_preimage_retains_registration(self):
        self.disappear()
        before = self.path.read_bytes()
        with patch.object(lc.reset, 'archive_registration', side_effect=OSError('disk full')):
            with self.assertRaises(OSError): self.reopen()
        self.assertEqual(self.path.read_bytes(), before)
        archive = lc.reset.archive_registration
        def changed(*args):
            result = archive(*args)
            self.path.write_bytes(before + b' ')
            return result
        with patch.object(lc.reset, 'archive_registration', side_effect=changed):
            with self.assertRaisesRegex(RuntimeError, 'changed'): self.reopen()
        self.assertEqual(self.path.read_bytes(), before + b' ')
        self.assertEqual(next((self.root / 'reset-archive').glob('*.json')).read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
