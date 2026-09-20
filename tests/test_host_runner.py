"""Offline safety tests for the opt-in real-host runner. Never launches Herdr/Pi."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch

SPEC = importlib.util.spec_from_file_location('host_runner', Path(__file__).parent/'host/run.py')
host_runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(host_runner)


class HostRunnerTests(unittest.TestCase):
    def host(self):
        host = host_runner.HostTest({key: '/test-bin/' + key for key in ('herdr', 'pi', 'python', 'node')})
        self.addCleanup(shutil.rmtree, host.root)
        return host

    def test_environment_is_allowlisted_and_private(self):
        poisoned = {key: 'DO_NOT_INHERIT' for key in (
            'OPENAI_API_KEY', 'ANTHROPIC_API_KEY', 'AWS_PROFILE', 'SSH_AUTH_SOCK', 'HTTP_PROXY',
            'NODE_OPTIONS', 'PYTHONPATH', 'BASH_ENV', 'PI_SESSION_ID', 'HERDR_PANE_ID', 'HERDR_ENV',
            'HERDR_SOCKET_PATH', 'PI_CODING_AGENT_DIR', 'SHOP_LOCATOR', 'SHOP_RUN_ID', 'HOME')}
        root = Path('/private/tmp/synthetic-host-test')
        with patch.dict(os.environ, poisoned):
            env = host_runner.isolated_env(root, ['/opt/bin/herdr', '/opt/bin/pi'])
        self.assertNotIn('DO_NOT_INHERIT', env.values())
        for key in ('HOME', 'PI_CODING_AGENT_DIR', 'HERDR_SOCKET_PATH', 'HERDR_CONFIG_PATH',
                    'SHOP_CONFIG_DIR', 'SHOP_STATE_DIR', 'SHOP_LOCATOR', 'TMPDIR'):
            self.assertTrue(Path(env[key]).is_relative_to(root), key)
        self.assertEqual(env['PATH'].split(os.pathsep).count('/opt/bin'), 1)
        self.assertEqual(env['PI_OFFLINE'], '1')

    def test_default_is_plan_only(self):
        output = io.StringIO()
        with patch.object(sys, 'argv', ['run.py']), patch.object(host_runner.shutil, 'which', return_value='/test/bin'), \
                patch.object(host_runner, 'HostTest') as constructor, contextlib.redirect_stdout(output):
            self.assertEqual(host_runner.main(), 0)
        constructor.assert_not_called()
        self.assertEqual(json.loads(output.getvalue())['mode'], 'plan_only')

    def test_missing_binary_is_failure_not_skip(self):
        with patch.object(sys, 'argv', ['run.py', '--run']), patch.object(host_runner.shutil, 'which', return_value=None), \
                patch.object(host_runner, 'HostTest') as constructor, contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as error:
                host_runner.main()
        self.assertEqual(error.exception.code, 2)
        constructor.assert_not_called()

    def test_live_non_macos_is_rejected_before_launch(self):
        with patch.object(sys, 'argv', ['run.py', '--run']), patch.object(sys, 'platform', 'linux'), \
                patch.object(host_runner.shutil, 'which', return_value='/test/bin'), \
                patch.object(host_runner, 'HostTest') as constructor, contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as error:
                host_runner.main()
        self.assertEqual(error.exception.code, 2)
        constructor.assert_not_called()

    def test_api_rejects_endpoint_outside_owned_root(self):
        host = self.host()
        host.env['HERDR_SOCKET_PATH'] = '/tmp/not-owned.sock'
        host.command = Mock()
        with self.assertRaisesRegex(RuntimeError, 'Endpoint escaped'):
            host.api('workspace', 'list')
        host.command.assert_not_called()

    def test_start_never_adopts_existing_server(self):
        host = self.host()
        host.command = Mock(return_value=Mock(stdout='status: running\n' + host.env['HERDR_SOCKET_PATH']))
        with patch.object(host_runner.subprocess, 'Popen') as launch:
            with self.assertRaisesRegex(RuntimeError, 'preflight failed'):
                host.start()
        launch.assert_not_called()

    def test_start_rejects_existing_socket_even_if_status_says_not_running(self):
        host = self.host()
        socket = Path(host.env['HERDR_SOCKET_PATH'])
        socket.parent.mkdir()
        socket.touch()
        host.command = Mock(return_value=Mock(stdout='status: not running\n' + str(socket)))
        with patch.object(host_runner.subprocess, 'Popen') as launch:
            with self.assertRaisesRegex(RuntimeError, 'Socket already exists'):
                host.start()
        launch.assert_not_called()

    def test_bad_reply_case_requires_evidence_of_real_mutation_and_no_retry(self):
        host = self.host()
        host.native_action = Mock(return_value={})
        host.state = Mock(return_value={'phase': 'partial'})
        with self.assertRaisesRegex(RuntimeError, 'Fault did not follow a real mutation'):
            host.setup(fault=True)
        host.native_action.assert_called_once_with('open', allow_failure=True)

    def test_implicit_action_requires_focused_architect(self):
        host = self.host()
        host.pane = 'w1:p1'
        host.api = Mock(return_value={'agent': {'focused': False}})
        with self.assertRaisesRegex(RuntimeError, 'Architect is not focused'):
            host.native_action('close')
        host.api.assert_called_once_with('agent', 'get', 'w1:p1')

    def test_native_action_checks_exit_status_unless_failure_is_explicit(self):
        for allow_failure in (False, True):
            with self.subTest(allow_failure=allow_failure):
                host = self.host()
                host.pane = 'w1:p1'
                result = {'log_id': 'log-1', 'status': 'failed', 'exit_code': 1}
                host.api = Mock(side_effect=[{'agent': {'focused': True}},
                                            {'log': {'log_id': 'log-1'}}, {'logs': [result]}])
                if allow_failure:
                    self.assertEqual(host.native_action('open', allow_failure=True), result)
                else:
                    with self.assertRaisesRegex(RuntimeError, 'Native plugin action failed'):
                        host.native_action('open')
                self.assertEqual(host.api.call_count, 3)

    def test_cleanup_stops_only_direct_owned_server_and_verifies_pi_exit(self):
        host = self.host()
        host.server = Mock()
        host.server.poll.return_value = None
        host.log = Mock()
        host_runner.write_json(host.root/'observations/w1:p1.json', {'pid': 123456})
        with patch.object(host_runner.subprocess, 'run', side_effect=[
                Mock(returncode=0, stdout='same birth stamp'), Mock(returncode=1, stdout='', stderr='')]) as ps:
            self.assertTrue(host.cleanup())
        host.server.terminate.assert_called_once_with()
        host.server.wait.assert_called_once_with(timeout=15)
        self.assertEqual(host.report['remaining_fixture_pids'], [])
        for call in ps.call_args_list:
            self.assertEqual(call.args[0][:3], ['/bin/ps', '-p', '123456'])
        host.log.close.assert_called_once_with()

    def test_cleanup_query_errors_are_not_exit_evidence(self):
        for result in (Mock(returncode=2, stdout='', stderr='query failed'),
                       Mock(returncode=1, stdout='', stderr='permission denied'),
                       Mock(returncode=0, stdout='', stderr='')):
            with self.subTest(code=result.returncode, stderr=result.stderr):
                host = self.host()
                host.server = Mock()
                host.server.poll.return_value = None
                host_runner.write_json(host.root/'observations/w1:p1.json', {'pid': 123456})
                with patch.object(host_runner.subprocess, 'run', return_value=result):
                    self.assertFalse(host.cleanup())
                host.server.terminate.assert_called_once_with()
                self.assertEqual(host.report['cleanup'], 'process_inventory_unknown')

    def test_cleanup_rename_does_not_prove_process_exit(self):
        host = self.host()
        host_runner.write_json(host.root/'observations/w1:p1.json', {'pid': 123456})
        names = iter(('pi', 'renamed'))
        def ps(argv, **kwargs):
            name = next(names)
            return Mock(returncode=0, stdout='same birth stamp' + (' ' + name if 'comm=' in argv else ''))
        with patch.object(host_runner.subprocess, 'run', side_effect=ps), \
                patch.object(host_runner.time, 'monotonic', side_effect=[0, 0, 11]), \
                patch.object(host_runner.time, 'sleep'):
            self.assertFalse(host.cleanup())
        self.assertEqual(host.report['remaining_fixture_pids'], [123456])
        self.assertEqual(host.report['cleanup'], 'owned_pi_still_alive')

    def test_inventory_failure_still_stops_server_and_fails_cleanup(self):
        host = self.host()
        host.server = Mock()
        host.server.poll.return_value = None
        host_runner.write_json(host.root/'observations/w1:p1.json', {'pid': 123456})
        with patch.object(host_runner.subprocess, 'run', side_effect=OSError('ps unavailable')):
            self.assertFalse(host.cleanup())
        host.server.terminate.assert_called_once_with()
        self.assertEqual(host.report['cleanup'], 'process_inventory_unknown')

    def test_cleanup_timeout_does_not_kill_unknown_processes_or_claim_success(self):
        host = self.host()
        host.server = Mock()
        host.server.poll.return_value = None
        host.server.wait.side_effect = subprocess.TimeoutExpired('owned server', 15)
        host.log = Mock()
        self.assertFalse(host.cleanup())
        host.server.kill.assert_not_called()
        host.log.close.assert_called_once_with()
        self.assertIn('did_not_stop', host.report['cleanup'])

    def test_real_gate_failure_is_nonzero_with_durable_report(self):
        host = self.host()
        for name in ('prepare', 'start', 'configuration_ui', 'setup', 'background_guard'):
            setattr(host, name, Mock())
        host.shutdown = Mock(side_effect=RuntimeError('background_state_unknown'))
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(host.run('lifecycle'), 1)
        report = json.loads((host.root/'report.json').read_text())
        self.assertEqual(report['result'], 'failed')
        self.assertEqual(report['steps'][-1]['status'], 'failed')
        self.assertIn('background_state_unknown', report['error'])
        self.assertEqual(report['cleanup'], 'no_server_started')

    def test_interrupt_records_failure_and_attempts_cleanup(self):
        host = self.host()
        host.prepare = Mock(side_effect=KeyboardInterrupt)
        host.cleanup = Mock(return_value=True)
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(KeyboardInterrupt):
            host.run('startup')
        host.cleanup.assert_called_once_with()
        self.assertEqual(json.loads((host.root/'report.json').read_text())['result'], 'failed')


if __name__ == '__main__':
    unittest.main()
