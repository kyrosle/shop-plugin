"""Adapter tests against sanitized real Herdr 0.9.0 captures.

Every test uses a fake subprocess runner, so nothing here touches a live
Herdr session, socket, pane or agent.
"""
import datetime as dt
import json
from pathlib import Path
import subprocess
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import herdr

FIXTURES = Path(__file__).resolve().parent / 'fixtures' / 'herdr-0.9.0'


def fixture(name):
    return json.loads((FIXTURES / name).read_text())


def version_text():
    return (FIXTURES / 'version.txt').read_text().strip()


class Result(SimpleNamespace):
    pass


class FakeRunner(object):
    """Maps an argv (without the binary) to a captured CLI result."""

    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append((tuple(argv), kwargs))
        key = tuple(argv[1:])
        response = self.responses.get(key)
        if response is None:
            return Result(returncode=1, stdout='', stderr='unexpected ' + repr(key))
        if isinstance(response, Exception):
            raise response
        code, stdout, stderr = response
        return Result(returncode=code, stdout=stdout, stderr=stderr)


def wire(responses=None):
    base = {
        ('--version',): (0, version_text() + '\n', ''),
        ('api', 'schema', '--json'): (0, json.dumps(fixture('api-schema-document.json')), ''),
    }
    base.update(responses or {})
    return FakeRunner(base)


def adapter(responses=None):
    return herdr.Herdr(binary='/fake/herdr', runner=wire(responses))


class FixtureTests(unittest.TestCase):
    def test_capture_is_sanitized_and_pinned(self):
        provenance = fixture('provenance.json')
        self.assertEqual(provenance['binary_version_output'], 'herdr 0.9.0')
        self.assertEqual(herdr.BINARY_VERSION, '0.9.0')
        summary = fixture('api-schema-summary.json')
        self.assertEqual(summary['protocol'], herdr.PROTOCOL)
        self.assertEqual(summary['schema_version'], herdr.SCHEMA_VERSION)
        for path in FIXTURES.glob('*.json'):
            text = path.read_text()
            for leak in ('/Users/', 'shop-plugin', 'fixture-user', 'term_65'):
                self.assertNotIn(leak, text, path.name + ' leaked ' + leak)

    def test_real_payloads_validate(self):
        for name, where in [('pane-list.json', 'panes'), ('pane-get.json', 'pane'),
                            ('agent-list.json', 'agents'), ('agent-get.json', 'agent'),
                            ('workspace-list.json', 'workspaces')]:
            with self.subTest(name=name):
                payload = fixture(name)['result'][where]
                if isinstance(payload, list):
                    for item in payload:
                        (herdr.agent_record if where == 'agents' else
                         herdr.pane_record if where == 'panes' else herdr.workspace_record)(item)
                elif where == 'agent':
                    herdr.agent_record(payload)
                elif where == 'pane':
                    herdr.pane_record(payload)
                else:
                    herdr.workspace_record(payload)
        herdr.layout_record(fixture('pane-layout.json')['result']['layout'])
        herdr.process_info_record(fixture('pane-process-info.json')['result']['process_info'])

    def test_real_unnamed_agent_record_is_not_guessed(self):
        record = fixture('agent-list-unnamed.json')['result']['agents'][0]
        parsed = herdr.agent_record(record)
        self.assertNotIn('name', parsed)
        self.assertIsNone(parsed.get('name'))
        self.assertTrue(parsed['pane_id'])

    def test_agent_screen_detection_metadata_is_optional_and_strictly_boolean(self):
        original = fixture('agent-get.json')['result']['agent']
        self.assertNotIn('screen_detection_skipped', herdr.agent_record(original))
        for value in (True, False):
            parsed = herdr.agent_record(dict(original, screen_detection_skipped=value))
            self.assertIs(parsed.pop('screen_detection_skipped'), value)
            self.assertEqual(parsed, herdr.agent_record(original))
        for value in (None, 0, 1, 'true', [], {}):
            with self.subTest(value=value), self.assertRaises(herdr.HerdrSchemaError):
                herdr.agent_record(dict(original, screen_detection_skipped=value))

    def test_screen_detection_metadata_does_not_grant_identity_or_idle_status(self):
        record = fixture('agent-list-unnamed.json')['result']['agents'][0]
        parsed = herdr.agent_record(dict(record, screen_detection_skipped=True))
        self.assertNotIn('name', parsed)
        parsed.pop('agent_status', None)
        with self.assertRaises(herdr.HerdrIdentityError):
            herdr.require_status(parsed)
        parsed['agent_status'] = 'unknown'
        with self.assertRaises(herdr.HerdrIdentityError):
            herdr.require_status(parsed, allowed=('idle', 'done'))
        with self.assertRaises(herdr.HerdrSchemaError):
            herdr.agent_record(dict(record, screen_detection_skipped=True, unrelated_field=True))

    def test_agent_get_and_list_accept_screen_detection_metadata(self):
        for command, filename, key in [('get', 'agent-get.json', 'agent'),
                                        ('list', 'agent-list.json', 'agents')]:
            with self.subTest(command=command):
                document = fixture(filename)
                rows = [document['result'][key]] if command == 'get' else document['result'][key]
                for row in rows:
                    row['screen_detection_skipped'] = True
                args = ('agent', 'get', 'demo-pane') if command == 'get' else ('agent', 'list')
                result = adapter({args: (0, json.dumps(document), '')}).call(*args)
                actual = [result[key]] if command == 'get' else result[key]
                self.assertTrue(actual)
                for row in actual:
                    self.assertIs(row['screen_detection_skipped'], True)

    def test_pane_payload_has_no_authoritative_name(self):
        pane = fixture('pane-get.json')['result']['pane']
        self.assertNotIn('name', pane)
        parsed = herdr.pane_record(pane)
        self.assertNotIn('name', parsed)
        with_name = herdr.pane_record(dict(pane, name='hostile-name'))
        self.assertNotIn('name', with_name)


class ProbeTests(unittest.TestCase):
    def test_probe_accepts_pinned_real_capture(self):
        runtime = adapter().probe()
        self.assertEqual((runtime.version, runtime.protocol, runtime.schema_version),
                         (herdr.BINARY_VERSION, herdr.PROTOCOL, herdr.SCHEMA_VERSION))
        self.assertEqual(runtime.as_dict()['compatible'], True)

    def test_probe_refuses_wrong_binary_version(self):
        with self.assertRaises(herdr.HerdrIncompatible) as caught:
            adapter({('--version',): (0, 'herdr 0.8.9\n', '')}).probe()
        self.assertIn('0.8.9', str(caught.exception))

    def test_probe_refuses_unparsable_version(self):
        with self.assertRaises(herdr.HerdrIncompatible):
            adapter({('--version',): (0, 'no version here\n', '')}).probe()

    def test_probe_refuses_wrong_protocol_and_schema(self):
        document = fixture('api-schema-document.json')
        for field, value in (('protocol', herdr.PROTOCOL + 1), ('schema_version', herdr.SCHEMA_VERSION + 1)):
            with self.subTest(field=field):
                drifted = dict(document, **{field: value})
                with self.assertRaises(herdr.HerdrIncompatible):
                    adapter({('api', 'schema', '--json'): (0, json.dumps(drifted), '')}).probe()

    def test_probe_refuses_schema_without_response_union(self):
        with self.assertRaises(herdr.HerdrIncompatible):
            adapter({('api', 'schema', '--json'):
                       (0, json.dumps({'protocol': herdr.PROTOCOL, 'schema_version': 1}), '')}).probe()

    def test_probe_refuses_table_disagreeing_with_protocol(self):
        drifted = dict(herdr.ROUTES)
        drifted[('pane', 'get')] = ('pane', ('pane_not_real',))
        with patch.object(herdr, 'ROUTES', drifted):
            with self.assertRaises(herdr.HerdrIncompatible) as caught:
                adapter().probe()
        self.assertIn('pane_not_real', str(caught.exception))

    def test_probe_refuses_payload_key_not_declared_by_type(self):
        drifted = dict(herdr.ROUTES)
        drifted[('pane', 'get')] = ('agents', ('pane_info',))
        with patch.object(herdr, 'ROUTES', drifted):
            with self.assertRaises(herdr.HerdrIncompatible):
                adapter().probe()

    def test_probe_cached_per_process(self):
        instance = adapter()
        instance.probe()
        instance.probe()
        version_calls = [c for c in instance.runner.calls if c[0][1:] == ('--version',)]
        self.assertEqual(len(version_calls), 1)


class BoundaryTests(unittest.TestCase):
    def test_timeout_and_missing_binary_are_distinct(self):
        with self.assertRaises(herdr.HerdrTimeout):
            adapter({('--version',): subprocess.TimeoutExpired('herdr', 10)}).probe()
        with self.assertRaises(herdr.HerdrUnavailable):
            adapter({('--version',): FileNotFoundError('herdr')}).probe()

    def test_error_envelope_and_garbage_are_reported_with_command(self):
        with self.assertRaises(herdr.HerdrError) as caught:
            adapter({('pane', 'get', 'w5:p4'):
                       (1, json.dumps({'id': 'cli:pane:get',
                                       'error': {'code': 'pane_not_found', 'message': 'pane nope not found'}}), '')}
                    ).call('pane', 'get', 'w5:p4')
        message = str(caught.exception)
        self.assertIn('pane_not_found', message)
        self.assertIn('pane get w5:p4', message)
        with self.assertRaises(herdr.HerdrMalformed):
            adapter({('pane', 'get', 'x'): (0, 'not json', '')}).call('pane', 'get', 'x')
        with self.assertRaises(herdr.HerdrMalformed):
            adapter({('pane', 'get', 'x'): (0, '{"id":"cli:pane:get"}', '')}).call('pane', 'get', 'x')

    def test_unknown_route_refused_before_any_subprocess(self):
        instance = adapter()
        with self.assertRaises(herdr.HerdrSchemaError):
            instance.call('agent', 'delete', 'x')
        self.assertEqual(instance.runner.calls, [])

    def test_bounded_timeout_is_passed_to_subprocess(self):
        instance = adapter({('agent', 'list'): (0, json.dumps(fixture('agent-list.json')), '')})
        instance.call('agent', 'list')
        self.assertEqual(instance.runner.calls[-1][1]['timeout'], herdr.DEFAULT_TIMEOUT)

    def test_notify_is_typed_and_never_raises(self):
        route = ('notification', 'show', 't', '--body', 'b', '--sound', 'none')
        shown = {'result': {'type': 'notification_show', 'shown': True, 'reason': None}}
        self.assertTrue(adapter({route: (0, json.dumps(shown), '')}).notify('t', 'b'))
        malformed = {'result': {'type': 'notification_show', 'shown': 'yes', 'reason': None}}
        self.assertFalse(adapter({route: (0, json.dumps(malformed), '')}).notify('t', 'b'))
        self.assertFalse(adapter({route:
                                  (1, '{"error":{"code":"x","message":"y"}}', '')}).notify('t', 'b'))

    def test_server_status_is_bounded_adapter_diagnostic(self):
        instance = adapter({('status', 'server'): (7, 'server down\n', 'diagnostic')})
        status = instance.server_status()
        self.assertEqual(status['binary'], '/fake/herdr')
        self.assertEqual(status['code'], 7)
        self.assertEqual(status['output'], 'server down\ndiagnostic')
        call = instance.runner.calls[-1]
        self.assertEqual(call[0], ('/fake/herdr', 'status', 'server'))
        self.assertEqual(call[1]['timeout'], herdr.PROBE_TIMEOUT)


class RouteSchemaTests(unittest.TestCase):
    def test_pane_and_agent_payloads_are_not_interchangeable(self):
        with self.assertRaises(herdr.HerdrSchemaError) as caught:
            adapter({('pane', 'get', 'w5:p4'): (0, json.dumps(fixture('agent-get.json')), '')}).call('pane', 'get', 'w5:p4')
        self.assertIn('agent_info', str(caught.exception))
        with self.assertRaises(herdr.HerdrSchemaError):
            adapter({('agent', 'get', 'w5:p4'): (0, json.dumps(fixture('pane-get.json')), '')}).call('agent', 'get', 'w5:p4')

    def test_agent_rename_returns_validated_agent_info(self):
        response = fixture('agent-get.json')
        response['result']['agent']['name'] = 'demo-renamed'
        response['result']['agent']['screen_detection_skipped'] = True
        route = ('agent', 'rename', 'w5:p4', 'demo-renamed')
        instance = adapter({route: (0, json.dumps(response), '')})
        result = instance.rename_agent('w5:p4', 'demo-renamed')
        self.assertEqual(result, {'agent': response['result']['agent']})
        self.assertEqual(result['agent']['terminal_id'], 'term_sanitized4')
        self.assertEqual(sum(call[0][1:] == route for call in instance.runner.calls), 1)

    def test_agent_rename_rejects_untyped_or_malformed_response_without_retry(self):
        agent = fixture('agent-get.json')['result']['agent']
        replies = [
            {'type': 'ok'},
            {'type': 'pane_info', 'pane': fixture('pane-get.json')['result']['pane']},
            {'type': 'agent_info'},
            {'type': 'agent_info', 'agent': None},
            {'type': 'agent_info', 'agent': dict(agent, screen_detection_skipped='yes')},
            {'type': 'agent_info', 'agent': dict(agent, unexpected_field=True)},
        ]
        route = ('agent', 'rename', 'w5:p4', 'demo-renamed')
        for reply in replies:
            with self.subTest(reply=reply):
                instance = adapter({route: (0, json.dumps({'result': reply}), '')})
                with self.assertRaises(herdr.HerdrSchemaError):
                    instance.rename_agent('w5:p4', 'demo-renamed')
                # A bad reply may follow a successful mutation. Never replay it.
                self.assertEqual(sum(call[0][1:] == route for call in instance.runner.calls), 1)

    def test_missing_payload_key_refused(self):
        with self.assertRaises(herdr.HerdrSchemaError):
            adapter({('pane', 'get', 'w5:p4'): (0, json.dumps({'result': {'type': 'pane_info'}}), '')}
                    ).call('pane', 'get', 'w5:p4')

    def test_unknown_field_refused(self):
        pane = dict(fixture('pane-get.json')['result']['pane'], terminal='typo-field')
        with self.assertRaises(herdr.HerdrSchemaError) as caught:
            herdr.pane_record(pane)
        self.assertIn('terminal', str(caught.exception))
        agent = dict(fixture('agent-get.json')['result']['agent'], agentname='typo')
        with self.assertRaises(herdr.HerdrSchemaError):
            herdr.agent_record(agent)

    def test_wrong_field_type_refused(self):
        pane = dict(fixture('pane-get.json')['result']['pane'], revision='three')
        with self.assertRaises(herdr.HerdrSchemaError):
            herdr.pane_record(pane)
        agent = dict(fixture('agent-get.json')['result']['agent'], agent_status='busy')
        with self.assertRaises(herdr.HerdrSchemaError) as caught:
            herdr.agent_record(agent)
        self.assertIn('busy', str(caught.exception))

    def test_typed_reads_return_validated_payloads(self):
        instance = adapter({
            ('workspace', 'list'): (0, json.dumps(fixture('workspace-list.json')), ''),
            ('pane', 'list', '--workspace', 'w5'): (0, json.dumps(fixture('pane-list.json')), ''),
            ('pane', 'get', 'w5:p4'): (0, json.dumps(fixture('pane-get.json')), ''),
            ('agent', 'get', 'w5:p4'): (0, json.dumps(fixture('agent-get.json')), ''),
            ('pane', 'process-info', '--pane', 'w5:p4'): (0, json.dumps(fixture('pane-process-info.json')), ''),
        })
        self.assertEqual([w['workspace_id'] for w in instance.workspaces()][:2], ['w1', 'wA'])
        self.assertEqual(instance.panes('w5')[0]['pane_id'], 'w5:p1')
        self.assertEqual(instance.pane('w5:p4')['terminal_id'], 'term_sanitized4')
        self.assertEqual(instance.agent('w5:p4')['name'], 'demo-11')
        self.assertEqual(instance.process_info('w5:p4')['foreground_processes'][0]['pid'], 35140)


class StatusTests(unittest.TestCase):
    def test_status_requires_known_values(self):
        record = fixture('agent-get.json')['result']['agent']
        self.assertEqual(herdr.require_status(record), 'working')
        for bad in ({}, {'agent_status': None}, {'agent_status': 'busy'}):
            with self.subTest(bad=bad), self.assertRaises(herdr.HerdrIdentityError):
                herdr.require_status(bad)
        with self.assertRaises(herdr.HerdrIdentityError):
            herdr.require_status({'agent_status': 'blocked'}, herdr.SETTLED_STATUSES)
        self.assertEqual(herdr.require_status({'agent_status': 'idle'}, herdr.SETTLED_STATUSES), 'idle')

    def test_require_text_refuses_missing_identity(self):
        for bad in ({}, {'pane_id': ''}, {'pane_id': 7}):
            with self.subTest(bad=bad), self.assertRaises(herdr.HerdrSchemaError):
                herdr.require_text(bad, 'pane_id', 'pane')


if __name__ == '__main__':
    unittest.main()


class FocusRouteTests(unittest.TestCase):
    """Typed read-only focus routes: declared, validated, never auto-executed."""

    def test_focus_routes_are_declared_and_probe_accepts_them(self):
        runtime = adapter().probe()
        self.assertEqual((runtime.version, runtime.protocol, runtime.schema_version),
                         (herdr.BINARY_VERSION, herdr.PROTOCOL, herdr.SCHEMA_VERSION))
        for route in (('workspace', 'focus'), ('tab', 'focus')):
            self.assertIn(route, herdr.ROUTES)
            self.assertIn(route, herdr.FOCUS_ROUTES)

    def test_validate_focus_command_accepts_only_declared_routes(self):
        self.assertEqual(herdr.validate_focus_command(['herdr', 'workspace', 'focus', 'w8']),
                         ('workspace', 'focus'))
        self.assertEqual(herdr.validate_focus_command(['herdr', 'tab', 'focus', 'w8:t1']),
                         ('tab', 'focus'))
        for bad in (['herdr', 'pane', 'focus', 'w8:p1'],
                    ['herdr', 'workspace', 'close', 'w8'],
                    ['herdr', 'workspace', 'focus', ''],
                    ['herdr', 'workspace', 'focus', '  '],
                    ['herdr', 'workspace', 'focus'],
                    'herdr workspace focus w8',
                    None):
            with self.subTest(bad=bad), self.assertRaises(herdr.HerdrSchemaError):
                herdr.validate_focus_command(bad)

    def test_validation_never_spawns_a_process(self):
        calls = []

        def runner(argv, **kwargs):
            calls.append(argv)
            raise AssertionError('focus validation must not spawn')

        instance = herdr.Herdr(binary='/fake/herdr', runner=runner)
        herdr.validate_focus_command(['herdr', 'workspace', 'focus', 'w8'])
        del instance
        self.assertEqual(calls, [])

    def test_focus_calls_use_the_typed_routes_and_are_explicit_only(self):
        captured = {}

        def runner(argv, **kwargs):
            captured['argv'] = argv
            return SimpleNamespace(returncode=0, stdout=json.dumps({'result': {'type': 'ok'}}), stderr='')

        instance = herdr.Herdr(binary='/fake/herdr', runner=runner)
        # The adapter probe is bypassed deliberately: the route table declares both
        # focus responses and probe() re-checks them against the pinned schema.
        instance._runtime = herdr.Runtime('/fake/herdr', herdr.BINARY_VERSION, herdr.PROTOCOL,
                                          herdr.SCHEMA_VERSION)
        instance.focus_workspace('w8')
        self.assertEqual(captured['argv'][1:], ['workspace', 'focus', 'w8'])
        instance.focus_tab('w8:t1')
        self.assertEqual(captured['argv'][1:], ['tab', 'focus', 'w8:t1'])

    def test_snapshot_links_validate_their_own_commands(self):
        import snapshot as snapshot_module
        state = {'shop_id': 's1', 'run_id': 'r1', 'phase': 'ready', 'tab': 't1', 'cwd': '/tmp',
                 'architect': {'name': 'arch', 'pane': 'p1', 'launch_id': 'a1', 'terminal_id': 't1'},
                 'lead': {'name': 'lead', 'pane': 'p2', 'launch_id': 'l1', 'terminal_id': 't2'},
                 'workers': [{'name': 'wa', 'pane': 'p3', 'launch_id': 'w1', 'terminal_id': 't3'}]}

        class Api:
            def __call__(self, *args):
                pane = args[2] if len(args) > 2 else 'p1'
                return {'pane': {'pane_id': pane, 'tab_id': 't1', 'workspace_id': 'w1',
                                 'terminal_id': 'term', 'agent': 'pi', 'agent_status': 'idle'}}

        doc = snapshot_module.build(state, [], api=Api(), now=dt.datetime.now(dt.timezone.utc))
        link = doc['links'][0]
        self.assertEqual(herdr.validate_focus_command(link['focus']['command']), ('workspace', 'focus'))
        self.assertEqual(herdr.validate_focus_command(link['focus']['then']), ('tab', 'focus'))
        self.assertIsNone(link['pane_focus'])
