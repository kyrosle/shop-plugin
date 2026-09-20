"""Metadata-only, offline process evidence tests. No host/process is controlled."""
import copy
import errno
import os
import socket
import stat
import struct
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import processes as pr
import shutdown as sd


def row(pid, ppid, pgid, sid, tty='ttys001', comm='pi'):
    return {'pid': pid, 'ppid': ppid, 'pgid': pgid, 'sid': sid, 'uid': 501,
            'tty': tty, 'comm': comm, 'start': 'Sun Sep 20 10:00:00 2026'}


class ProcessEvidenceTests(unittest.TestCase):
    def setUp(self):
        uid = patch.object(pr.os, 'geteuid', return_value=501)
        uid.start(); self.addCleanup(uid.stop)
        self.table = {100: row(100, 1, 100, 100, '??', '/opt/bin/herdr'),
                      200: row(200, 100, 200, 200, comm='/bin/bash'),
                      300: row(300, 200, 300, 200)}
        self.info = {'pane_id': 'w1:p2', 'shell_pid': 200, 'foreground_process_group_id': 300,
                     'foreground_processes': [{'pid': 300, 'argv0': 'pi', 'name': 'node'}]}
        self.agent = {'pane_id': 'w1:p2', 'tab_id': 'w1:t1', 'terminal_id': 'term-pi',
                      'name': 'shop-lead', 'agent': 'pi', 'agent_status': 'idle', 'state_change_seq': 1}

    def api(self, *args):
        if args[:2] == ('pane', 'process-info'):
            return {'process_info': copy.deepcopy(self.info)}
        if args[:2] == ('agent', 'get'):
            return {'agent': copy.deepcopy(self.agent)}
        raise AssertionError('unexpected native API call: ' + repr(args))

    def facts(self, tables=None, **kwargs):
        with patch.object(pr, 'local_server_pid', return_value=100), \
                patch.object(pr, 'snapshot', side_effect=tables or [self.table, self.table]):
            return sd.process_facts(self.api, 'w1:p2', **kwargs)

    def test_idle_pi_is_not_extra_foreground_and_scope_is_proven(self):
        facts = self.facts(expected={'name': 'shop-lead', 'terminal_id': 'term-pi'})
        self.assertTrue(facts['available'])
        self.assertTrue(facts['background_proven'], facts)
        self.assertEqual(facts['extra_foreground'], [])
        self.assertEqual(facts['extra_background'], [])
        self.assertEqual(facts['pi_pid'], 300)
        self.assertEqual(set(facts['process_scope']['related']), {'200', '300'})

    def test_exec_pi_replacing_shell_is_supported(self):
        self.info['foreground_process_group_id'] = 200
        self.info['foreground_processes'][0]['pid'] = 200
        self.table.pop(300)
        self.table[200]['comm'] = 'pi'
        self.assertTrue(self.facts()['background_proven'])

    def test_detached_child_without_tty_still_blocks(self):
        self.table[400] = row(400, 300, 400, 400, '??', '/usr/bin/node')
        facts = self.facts()
        self.assertFalse(facts['background_proven'])
        self.assertEqual([p['pid'] for p in facts['extra_background']], [400])

    def test_orphan_in_same_session_still_blocks(self):
        self.table[400] = row(400, 1, 400, 200, '??', 'sleep')
        self.assertEqual(self.facts()['extra_background'][0]['pid'], 400)

    def test_foreign_process_holding_same_tty_blocks(self):
        self.table[400] = row(400, 1, 400, 400, comm='sleep')
        self.assertEqual(self.facts()['extra_background'][0]['pid'], 400)

    def test_background_shell_sibling_and_grandchild_block(self):
        self.table[400] = row(400, 200, 400, 200, comm='bash')
        self.table[401] = row(401, 400, 401, 401, '??', 'sleep')
        self.assertEqual([p['pid'] for p in self.facts()['extra_background']], [400, 401])

    def test_extra_foreground_is_not_whitelisted_as_node(self):
        self.info['foreground_processes'].append({'pid': 400, 'argv0': 'build.js', 'name': 'node'})
        self.table[400] = row(400, 300, 300, 200, comm='node')
        facts = self.facts()
        self.assertFalse(facts['background_proven'])
        self.assertEqual([p['pid'] for p in facts['extra_foreground']], [400])

    def test_unrelated_sessions_are_not_claimed_stopped(self):
        self.table[400] = row(400, 1, 400, 400, 'ttys002', 'node')
        facts = self.facts()
        self.assertTrue(facts['background_proven'])
        self.assertNotIn('400', facts['process_scope']['related'])
        self.assertIn('current shell descendants', facts['scope'])

    def test_foreground_node_without_pi_identity_is_not_exempted(self):
        self.info['foreground_processes'][0]['argv0'] = 'other.js'
        facts = self.facts()
        self.assertFalse(facts['background_proven'])
        self.assertEqual(facts['extra_foreground'][0]['pid'], 300)

    def test_ambiguous_pi_processes_are_refused(self):
        self.info['foreground_processes'].append({'pid': 400, 'argv0': 'pi', 'name': 'node'})
        self.assertFalse(self.facts()['background_proven'])

    def test_wrong_native_pane_refused(self):
        self.info['pane_id'] = 'w9:p9'
        self.assertFalse(self.facts()['available'])

    def test_foreign_server_session_owner_group_or_program_refused(self):
        cases = [(200, 'ppid', 999), (200, 'sid', 100), (300, 'uid', 502),
                 (300, 'pgid', 301), (300, 'ppid', 100), (300, 'tty', 'ttys999'),
                 (300, 'comm', 'other-app'), (200, 'tty', '??')]
        for pid, field, value in cases:
            with self.subTest(field=field, value=value):
                changed = copy.deepcopy(self.table)
                changed[pid][field] = value
                self.assertFalse(self.facts([changed, changed])['background_proven'])

    def test_registered_terminal_or_name_drift_refused(self):
        for expected in ({'name': 'other', 'terminal_id': 'term-pi'},
                         {'name': 'shop-lead', 'terminal_id': 'replacement'}):
            self.assertFalse(self.facts(expected=expected)['background_proven'])

    def test_status_change_during_probe_refused(self):
        agent = copy.deepcopy(self.agent)
        original = self.api
        count = 0
        def api(*args):
            nonlocal count
            if args[:2] == ('agent', 'get'):
                count += 1
                return {'agent': {**agent, 'agent_status': 'working' if count > 1 else 'idle'}}
            return original(*args)
        self.api = api
        self.assertFalse(self.facts()['background_proven'])

    def test_pid_reuse_or_new_background_work_between_snapshots_refused(self):
        reused = copy.deepcopy(self.table)
        reused[300]['start'] = 'Sun Sep 20 10:01:00 2026'
        spawned = copy.deepcopy(self.table)
        spawned[400] = row(400, 300, 400, 400, '??', 'sleep')
        for changed in (reused, spawned):
            facts = self.facts([self.table, changed])
            self.assertFalse(facts['background_proven'])
            self.assertIn('changed during inspection', facts['error'])

    def test_peer_change_between_snapshots_refused(self):
        with patch.object(pr, 'local_server_pid', side_effect=[100, 999]), \
                patch.object(pr, 'snapshot', return_value=self.table):
            self.assertFalse(sd.process_facts(self.api, 'w1:p2')['background_proven'])

    def test_snapshot_failure_is_unknown_not_clear(self):
        with patch.object(pr, 'local_server_pid', return_value=100), \
                patch.object(pr, 'snapshot', side_effect=TimeoutError('ps deadline')):
            facts = sd.process_facts(self.api, 'w1:p2')
        self.assertTrue(facts['available'])
        self.assertFalse(facts['background_proven'])
        self.assertEqual(facts['extra_foreground'], [])
        self.assertIn('ps deadline', facts['error'])


class MetadataBoundaryTests(unittest.TestCase):
    TEXT = ' 300 200 300 501 ttys001 Sun Sep 20 10:00:00 2026 /opt/bin/node\n'

    def test_parser_does_not_need_command_arguments_or_environment(self):
        parsed = pr.parse_table(self.TEXT, session_id=lambda pid: 200)
        self.assertEqual(parsed[300]['comm'], '/opt/bin/node')
        self.assertNotIn('argv', parsed[300])
        self.assertNotIn('environ', parsed[300])

    def test_missing_duplicate_empty_and_large_metadata_refused(self):
        for text in ('', '300 200\n', self.TEXT * 2, 'x' * (pr.MAX_TABLE_BYTES + 1)):
            with self.assertRaises(pr.ProcessUncertain):
                pr.parse_table(text, session_id=lambda pid: 200)

    def test_vanished_parent_edge_is_retained(self):
        def gone(pid):
            raise ProcessLookupError(errno.ESRCH, 'gone')
        parsed = pr.parse_table(self.TEXT, session_id=gone)
        self.assertEqual(parsed[300]['ppid'], 200)
        self.assertIsNone(parsed[300]['sid'])

    def test_permission_denied_is_not_empty_process_list(self):
        def denied(pid):
            raise PermissionError(errno.EPERM, 'denied')
        with self.assertRaises(pr.ProcessUncertain):
            pr.parse_table(self.TEXT, session_id=denied)

    def test_exit_requires_absence_or_new_birth_not_reparenting(self):
        original = row(300, 200, 300, 200)
        reparented = {**original, 'ppid': 1, 'sid': 300, 'tty': '??', 'comm': 'other-name'}
        with patch.object(pr, 'snapshot', return_value={300: reparented}):
            self.assertFalse(pr.wait_exited({300: original}, timeout=0))
        for table in ({}, {300: {**reparented, 'start': 'new birth'}}):
            with patch.object(pr, 'snapshot', return_value=table):
                self.assertTrue(pr.wait_exited({300: original}, timeout=0))

    def test_exit_query_failure_is_not_success(self):
        with patch.object(pr, 'snapshot', side_effect=pr.ProcessUncertain('unreadable')):
            with self.assertRaises(pr.ProcessUncertain):
                pr.wait_exited({300: row(300, 200, 300, 200)}, timeout=0)
        with self.assertRaises(pr.ProcessUncertain):
            pr.wait_exited({}, timeout=0)

    def test_query_is_metadata_only_without_shell_or_inherited_credentials(self):
        with patch.object(pr.subprocess, 'run', return_value=Mock(returncode=0, stdout=self.TEXT)) as run, \
                patch.object(pr, 'parse_table', return_value={300: {}}), patch.object(pr.sys, 'platform', 'darwin'):
            pr.snapshot()
        args, kwargs = run.call_args
        self.assertEqual(args[0], ['/bin/ps', '-axo', 'pid=,ppid=,pgid=,uid=,tty=,lstart=,comm='])
        self.assertEqual(kwargs['env'], {'PATH': '/usr/bin:/bin', 'LC_ALL': 'C'})
        self.assertNotIn('shell', kwargs)

    def test_no_explicit_socket_or_foreign_owner_or_symlink_refused(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(pr.ProcessUncertain):
                pr.local_server_pid()
        for mode, uid in ((stat.S_IFLNK, os.geteuid()), (stat.S_IFSOCK, os.geteuid() + 1)):
            with patch.dict(os.environ, {'HERDR_SOCKET_PATH': '/tmp/synthetic.sock'}), \
                    patch.object(pr.os, 'lstat', return_value=SimpleNamespace(st_mode=mode, st_uid=uid)), \
                    patch.object(pr.socket, 'socket') as connect:
                with self.assertRaises(pr.ProcessUncertain):
                    pr.local_server_pid()
                connect.assert_not_called()

    def test_kernel_socket_peer_and_inode_are_checked(self):
        attrs = dict(st_mode=stat.S_IFSOCK, st_uid=os.geteuid(), st_dev=1, st_ino=2)
        for inode in (2, 3):
            with patch.dict(os.environ, {'HERDR_SOCKET_PATH': '/tmp/synthetic.sock'}), \
                    patch.object(pr.sys, 'platform', 'darwin'), \
                    patch.object(pr.os, 'lstat', side_effect=[SimpleNamespace(**attrs), SimpleNamespace(**{**attrs, 'st_ino': inode})]), \
                    patch.object(pr.socket, 'socket') as socket_mock:
                client = socket_mock.return_value.__enter__.return_value
                client.getsockopt.return_value = 100
                if inode == 2:
                    self.assertEqual(pr.local_server_pid(), 100)
                else:
                    with self.assertRaises(pr.ProcessUncertain):
                        pr.local_server_pid()
                client.getsockopt.assert_called_once_with(0, 2)

    def test_linux_peer_uid_must_match_socket_owner(self):
        attrs = SimpleNamespace(st_mode=stat.S_IFSOCK, st_uid=os.geteuid(), st_dev=1, st_ino=2)
        for uid in (os.geteuid(), os.geteuid() + 1):
            with patch.dict(os.environ, {'HERDR_SOCKET_PATH': '/tmp/synthetic.sock'}), \
                    patch.object(pr.sys, 'platform', 'linux'), \
                    patch.object(pr.os, 'lstat', return_value=attrs), \
                    patch.object(pr.socket, 'SO_PEERCRED', 17, create=True), \
                    patch.object(pr.socket, 'socket') as socket_mock:
                client = socket_mock.return_value.__enter__.return_value
                client.getsockopt.return_value = struct.pack('3i', 100, uid, 0)
                if uid == os.geteuid():
                    self.assertEqual(pr.local_server_pid(), 100)
                else:
                    with self.assertRaises(pr.ProcessUncertain):
                        pr.local_server_pid()

    def test_linux_snapshot_stays_unknown_until_visibility_is_validated(self):
        with patch.object(pr.sys, 'platform', 'linux'), patch.object(pr.subprocess, 'run') as command:
            with self.assertRaises(pr.ProcessUncertain):
                pr.snapshot()
            command.assert_not_called()

    def test_unsupported_platform_fails_closed(self):
        with patch.object(pr.sys, 'platform', 'win32'):
            with self.assertRaises(pr.ProcessUncertain):
                pr.snapshot()
            with self.assertRaises(pr.ProcessUncertain):
                pr.local_server_pid()


if __name__ == '__main__':
    unittest.main()
