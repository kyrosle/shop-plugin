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


LIVE = {'model': 'cheap/flash', 'thinking': {'architect': 'max', 'lead': 'high', 'worker': 'low'},
        'budget_usd': 0.30, 'max_calls': 5, 'timeout': 60}
SECRET = 'sk-test-never-in-reports'


class LiveProviderTests(unittest.TestCase):
    def live_host(self):
        host = host_runner.HostTest({key: '/test-bin/' + key for key in ('herdr', 'pi', 'python', 'node')},
                                    live=dict(LIVE), credential={'cheap': {'type': 'api_key', 'key': SECRET}})
        self.addCleanup(shutil.rmtree, host.root)
        return host

    def auth_file(self, data):
        path = Path(self.enterContext(__import__('tempfile').TemporaryDirectory())) / 'auth.json'
        path.write_text(json.dumps(data))
        return path

    def main(self, *argv, auth=None):
        output, errors = io.StringIO(), io.StringIO()
        home = self.auth_file(auth or {'cheap': {'type': 'api_key', 'key': SECRET}}).parent
        (home / '.pi/agent').mkdir(parents=True)
        (home / 'auth.json').rename(home / '.pi/agent/auth.json')
        with patch.object(sys, 'argv', ['run.py', *argv]), patch.object(host_runner.shutil, 'which', return_value='/test/bin'), \
                patch.object(host_runner.Path, 'home', return_value=home), patch.object(host_runner, 'HostTest') as constructor, \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            try:
                code = host_runner.main()
            except SystemExit as exit:
                code = exit.code
        return code, output.getvalue(), errors.getvalue(), constructor

    def test_thinking_parser_defaults_off_and_rejects_unknown(self):
        self.assertEqual(host_runner.parse_thinking('architect=max,lead=high,worker=low'), LIVE['thinking'])
        self.assertEqual(host_runner.parse_thinking(''), {'architect': 'off', 'lead': 'off', 'worker': 'off'})
        for bad in ('architect=ultra', 'reviewer=low', 'lead'):
            with self.assertRaises(ValueError):
                host_runner.parse_thinking(bad)

    def test_only_api_key_credentials_are_copied(self):
        path = self.auth_file({'cheap': {'type': 'api_key', 'key': SECRET, 'extra': 'dropped'},
                               'oauth': {'type': 'oauth', 'access': 'a', 'refresh': 'r'}, 'other': {'type': 'api_key', 'key': 'x'}})
        self.assertEqual(host_runner.live_credential('cheap', path), {'cheap': {'type': 'api_key', 'key': SECRET}})
        with self.assertRaisesRegex(RuntimeError, 'OAuth is never copied'):
            host_runner.live_credential('oauth', path)
        with self.assertRaisesRegex(RuntimeError, 'No stored'):
            host_runner.live_credential('missing', path)

    def test_live_model_is_required_for_live_and_refused_elsewhere(self):
        code, _, errors, constructor = self.main('--run', '--scenario', 'live-smoke')
        self.assertEqual(code, 2)
        self.assertIn('--live-model is required', errors)
        code, _, _, _ = self.main('--run', '--scenario', 'all', '--live-model', 'cheap/flash')
        self.assertEqual(code, 2)
        constructor.assert_not_called()

    def test_oauth_provider_is_rejected_before_launch(self):
        code, _, errors, constructor = self.main('--run', '--scenario', 'live-smoke', '--live-model', 'oauth/m',
                                                 auth={'oauth': {'type': 'oauth', 'refresh': 'r'}})
        self.assertEqual(code, 2)
        self.assertIn('OAuth', errors)
        constructor.assert_not_called()

    def test_live_plan_shows_budget_but_never_the_key(self):
        code, output, _, constructor = self.main('--scenario', 'live-smoke', '--live-model', 'cheap/flash',
                                                 '--live-thinking', 'architect=max,lead=high,worker=low')
        self.assertEqual(code, 0)
        constructor.assert_not_called()
        plan = json.loads(output)
        self.assertEqual(plan['live']['thinking'], LIVE['thinking'])
        self.assertEqual(plan['live']['budget_usd'], 0.30)
        self.assertNotIn(SECRET, output)

    def test_budget_counts_cost_and_calls(self):
        host = self.live_host()
        ledger = host.root / 'live-usage.jsonl'
        row = {'pane': 'p', 'usage': {'input': 10, 'output': 5, 'cost': {'total': 0.1}}}
        ledger.write_text(json.dumps(row) + '\n')
        host.check_budget()
        self.assertEqual(host.report['live']['usage']['cost_usd'], 0.1)
        ledger.write_text((json.dumps(row) + '\n') * 4)
        with self.assertRaisesRegex(host_runner.BudgetExceeded, 'budget exceeded'):
            host.check_budget()
        ledger.write_text(json.dumps({'usage': {'cost': {'total': 0}}}) + '\n' * 1 + (json.dumps({'usage': {}}) + '\n') * 5)
        with self.assertRaisesRegex(host_runner.BudgetExceeded, 'calls'):
            host.check_budget()

    def test_live_run_scrubs_credential_and_keeps_it_out_of_report(self):
        host = self.live_host()
        (host.root / 'pi').mkdir()
        (host.root / 'pi/auth.json').write_text(json.dumps(host.credential))
        host.prepare = Mock()
        host.start = Mock(side_effect=RuntimeError('boom'))
        host.cleanup = Mock(return_value=True)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(host.run('live-smoke'), 1)
        self.assertFalse((host.root / 'pi/auth.json').exists())
        report = (host.root / 'report.json').read_text()
        self.assertNotIn(SECRET, report)
        self.assertTrue(json.loads(report)['credential_scrubbed'])

    def test_live_seats_load_a_private_snapshot_not_the_checkout(self):
        host = self.live_host()
        self.assertTrue(host.package.is_relative_to(host.root))
        plain = host_runner.HostTest({key: '/test-bin/' + key for key in ('herdr', 'pi', 'python', 'node')})
        self.addCleanup(shutil.rmtree, plain.root)
        self.assertEqual(plain.package, host_runner.REPO)

    def test_seat_usage_attributes_rows_by_registered_pane(self):
        host = self.live_host()
        host.pane = 'a'
        host.state = Mock(return_value={'lead': {'pane': 'l'}, 'workers': [{'pane': 'w'}]})
        row = lambda pane, cost: {'pane': pane, 'usage': {'input': 10, 'output': 2, 'cacheRead': 1, 'cost': {'total': cost}}}
        seats = host.seat_usage([row('a', 0.1), row('l', 0.2), row('w', 0.3), row('w', 0.3), row('x', 1)])
        self.assertEqual(seats['architect']['calls'], 1)
        self.assertEqual(seats['worker'], {'calls': 2, 'input': 20, 'output': 4, 'cache_read': 2, 'cost_usd': 0.6})
        self.assertEqual(seats['other']['calls'], 1)

    def test_architect_report_requires_final_answer_with_fixture_text(self):
        host = self.live_host()
        host.pane = 'a'
        host.state = Mock(return_value={})
        host.delegation = {'ledger_start': 1, 'started': 0, 'accepted_seconds': 10}
        ledger = host.root / 'live-usage.jsonl'
        rows = [{'pane': 'a', 'stopReason': 'stop', 'text': host_runner.FIXTURE_TEXT},  # before /shop: ignored
                {'pane': 'a', 'stopReason': 'toolUse', 'text': host_runner.FIXTURE_TEXT},
                {'pane': 'l', 'stopReason': 'stop', 'text': host_runner.FIXTURE_TEXT}]
        ledger.write_text(''.join(json.dumps(row) + '\n' for row in rows))
        host.live['timeout'] = 0
        with patch.object(host_runner.time, 'sleep'), self.assertRaises(TimeoutError):
            host.wait = lambda predicate, label, timeout=None: predicate() or (_ for _ in ()).throw(TimeoutError(label))
            host.live_architect_report()
        with ledger.open('a') as stream:
            stream.write(json.dumps({'pane': 'a', 'stopReason': 'stop', 'text': 'It says: ' + host_runner.FIXTURE_TEXT}) + '\n')
        host.live_architect_report()
        self.assertEqual(host.report['baseline']['seconds_to_accepted'], 10)
        self.assertIn('architect', host.report['baseline']['seats'])

    def test_fixture_match_tolerates_markdown_wrapping_but_not_other_text(self):
        self.assertTrue(host_runner.mentions_fixture('says: "Host smoke fixture; no\n  business repository."'))
        self.assertFalse(host_runner.mentions_fixture('Host smoke fixture; business repository.'))
        self.assertFalse(host_runner.mentions_fixture(None))

    def test_aggregate_reports_pass_rate_and_spreads(self):
        def report(result, cost, accepted=None, step='live.delegation_delivery'):
            value = {'root': '/tmp/r', 'result': result, 'scenario': 'live-delegation', 'live': {'model': 'm', 'usage': {'cost_usd': cost}},
                     'steps': [{'name': step, 'status': 'passed' if result == 'passed' else 'failed'}], 'error': None if result == 'passed' else 'x'}
            if accepted is not None:
                value['baseline'] = {'seconds_to_accepted': accepted, 'seconds_to_report': accepted + 5,
                                     'seats': {'lead': {'calls': accepted, 'input': 1, 'output': 1, 'cost_usd': cost}}}
            return value
        summary = host_runner.aggregate([report('passed', 0.02, 100), report('passed', 0.04, 300), report('failed', 0.01)])
        self.assertEqual((summary['runs'], summary['passed']), (3, 2))
        self.assertEqual(summary['failures'][0]['step'], 'live.delegation_delivery')
        self.assertEqual(summary['seconds_to_accepted'], {'min': 100, 'median': 200, 'max': 300})
        self.assertEqual(summary['cost_usd_per_run_all']['max'], 0.04)
        self.assertEqual(summary['seats']['lead']['calls']['median'], 200)

    def test_repeat_runs_independent_hosts_and_fails_if_any_run_fails(self):
        results = iter(['passed', 'failed'])
        created = []
        def fake_host(*args, **kwargs):
            host = Mock()
            host.root = Path(self.enterContext(__import__('tempfile').TemporaryDirectory()))
            result = next(results)
            def run(_scenario):
                (host.root / 'report.json').write_text(json.dumps({'root': str(host.root), 'result': result, 'scenario': 'live-smoke',
                                                                    'steps': [{'name': 's', 'status': result}], 'live': {'usage': {'cost_usd': 0.01}}}))
                return 0 if result == 'passed' else 1
            host.run = run
            created.append(host)
            return host
        home = self.auth_file({'cheap': {'type': 'api_key', 'key': SECRET}}).parent
        (home / '.pi/agent').mkdir(parents=True)
        (home / 'auth.json').rename(home / '.pi/agent/auth.json')
        output = io.StringIO()
        with patch.object(sys, 'argv', ['run.py', '--run', '--scenario', 'live-smoke', '--live-model', 'cheap/flash', '--repeat', '2']), \
                patch.object(host_runner.shutil, 'which', return_value='/test/bin'), patch.object(sys, 'platform', 'darwin'), \
                patch.object(host_runner.Path, 'home', return_value=home), patch.object(host_runner, 'HostTest', side_effect=fake_host), \
                patch.object(host_runner, 'write_json'), contextlib.redirect_stdout(output):
            self.assertEqual(host_runner.main(), 1)
        self.assertEqual(len(created), 2)
        self.assertNotIn(SECRET, output.getvalue())
        self.assertIn('"passed": 1', output.getvalue())

    def test_live_scenario_skips_fixture_configuration_ui(self):
        host = self.live_host()
        for name in ('prepare', 'start', 'live_roundtrip', 'setup', 'live_members', 'configuration_ui', 'live_delegation'):
            setattr(host, name, Mock())
        host.cleanup = Mock(return_value=True)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(host.run('live-smoke'), 0)
        host.configuration_ui.assert_not_called()
        host.live_delegation.assert_not_called()
        self.assertEqual([step['name'] for step in host.steps],
                         ['isolation.prepare', 'host.start_and_load', 'live.architect_roundtrip',
                          'setup.live_members', 'live.member_roundtrip'])


if __name__ == '__main__':
    unittest.main()
