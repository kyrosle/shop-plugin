"""Identity/epoch mapping, fail-closed resolution and route fencing.

Fixtures are sanitized captures from a real Herdr 0.9.0 instance, so the
"pane without name but matching agent identity" case is real, not invented.
"""
import copy
import json
from pathlib import Path
import unittest

import herdr
import identity

FIXTURES = Path(__file__).resolve().parent / 'fixtures' / 'herdr-0.9.0'


def fixture(name):
    return json.loads((FIXTURES / name).read_text())['result']


PANE = fixture('pane-get.json')['pane']          # real pane payload, no `name` field
AGENT = fixture('agent-get.json')['agent']       # real agent payload, has `name`


class FixtureApi(object):
    """Serve captured payloads; optionally mutate or corrupt them per test."""

    def __init__(self, pane=None, agent=None):
        self.pane = copy.deepcopy(PANE if pane is None else pane)
        self.agent = copy.deepcopy(AGENT if agent is None else agent)

    def __call__(self, *args):
        if args[:2] == ('pane', 'get'):
            return {'pane': copy.deepcopy(self.pane)}
        if args[:2] == ('agent', 'get'):
            return {'agent': copy.deepcopy(self.agent)}
        raise AssertionError(args)


class MemberMappingTests(unittest.TestCase):
    def test_member_view_is_explicit_about_absent_transport(self):
        member = {'name': 'w', 'pane': 'p3', 'launch_id': 'l1', 'terminal_id': 't1',
                  'session_dir': '/sessions/w-l1', 'session_source': 'cli_session_dir'}
        view = identity.member_view(member)
        self.assertEqual(set(identity.MEMBER_FIELDS), set(view) - {'transport', 'transport_present'})
        self.assertIsNone(view['transport'])
        self.assertIsNone(view['transport_epoch'])
        self.assertFalse(view['transport_present'])
        self.assertEqual(view['session_dir'], '/sessions/w-l1')

    def test_member_view_refuses_pretend_transport(self):
        for field, value in (('transport_epoch', 1), ('transport', {'kind': 'socket'})):
            with self.subTest(field=field), self.assertRaises(herdr.HerdrIdentityError):
                identity.member_view({'name': 'w', 'pane': 'p3', field: value})

    def test_assign_identity_persists_epoch_fields_without_transport(self):
        item = {'name': 'w', 'pane': 'p3'}
        identity.assign_identity(item, session_dir='/s/w-l1', launch_id='l1', terminal_id='t1')
        self.assertEqual(item['transport_epoch'], None)
        self.assertEqual(item['transport'], None)
        self.assertEqual(item['session_dir'], '/s/w-l1')
        with self.assertRaises(herdr.HerdrSchemaError):
            identity.assign_identity(item, terminal_id='')
        item['transport_epoch'] = 2
        with self.assertRaises(herdr.HerdrIdentityError):
            identity.assign_identity(item, terminal_id='t2')

    def test_roster_roles_are_stable(self):
        state = {'architect': {'name': 'a', 'pane': 'p1'}, 'lead': {'name': 'l', 'pane': 'p2'},
                 'extra_leads': [{'name': 'l2', 'pane': 'p4'}],
                 'workers': [{'name': 'w', 'pane': 'p3'}]}
        self.assertEqual([role for role, _ in identity.roster(state)],
                         ['architect', 'lead', 'auxiliary_lead', 'worker'])


class ResolveTests(unittest.TestCase):
    def setUp(self):
        self.tab = PANE['tab_id']
        self.member = {'name': AGENT['name'], 'pane': PANE['pane_id'],
                       'launch_id': 'l1', 'terminal_id': PANE['terminal_id']}

    def test_pane_without_name_but_matching_agent_identity_is_allowed(self):
        self.assertNotIn('name', PANE)
        resolved = identity.resolve(FixtureApi(), self.member, self.tab,
                                     allowed_status=('working',))
        self.assertEqual(resolved['name'], AGENT['name'])
        self.assertEqual(resolved['pane'], PANE['pane_id'])
        self.assertEqual(resolved['terminal_id'], PANE['terminal_id'])

    def test_idle_identity_is_allowed_and_working_is_not(self):
        idle = dict(AGENT, agent_status='idle')
        self.assertEqual(identity.resolve(FixtureApi(agent=idle), self.member, self.tab)['status'], 'idle')
        for status in ('working', 'blocked', 'unknown'):
            with self.subTest(status=status):
                api = FixtureApi(agent=dict(AGENT, agent_status=status))
                with self.assertRaises(herdr.HerdrIdentityError):
                    identity.resolve(api, self.member, self.tab)
                self.assertEqual(identity.resolve(api, self.member, self.tab, require_status=False)['status'], status)

    def test_missing_or_unknown_status_fails_closed(self):
        for agent in (dict(AGENT, agent_status=None), {k: v for k, v in AGENT.items() if k != 'agent_status'}):
            with self.subTest(agent=agent), self.assertRaises(herdr.HerdrError):
                identity.resolve(FixtureApi(agent=agent), self.member, self.tab)

    def test_wrong_fields_are_refused(self):
        cases = {
            'name': FixtureApi(agent=dict(AGENT, name='replacement-agent')),
            'pane': FixtureApi(agent=dict(AGENT, pane_id='w5:p1')),
            'tab': FixtureApi(agent=dict(AGENT, tab_id='w5:t1')),
            'kind': FixtureApi(agent=dict(AGENT, agent='claude')),
            'terminal': FixtureApi(agent=dict(AGENT, terminal_id='term_replaced')),
            'missing-name': FixtureApi(agent={k: v for k, v in AGENT.items() if k != 'name'}),
            'pane-tab': FixtureApi(pane=dict(PANE, tab_id='w5:t1')),
        }
        for label, api in cases.items():
            with self.subTest(label=label), self.assertRaises(herdr.HerdrIdentityError):
                identity.resolve(api, self.member, self.tab)

    def test_pane_without_agent_is_refused(self):
        shell = {k: v for k, v in PANE.items() if k != 'agent'}
        with self.assertRaises(herdr.HerdrIdentityError):
            identity.resolve(FixtureApi(pane=shell), self.member, self.tab)

    def test_recorded_transport_epoch_refuses_identification(self):
        member = dict(self.member, transport_epoch=1)
        with self.assertRaises(herdr.HerdrIdentityError):
            identity.resolve(FixtureApi(), member, self.tab)

    def test_require_terminal_refuses_missing_terminal_identity(self):
        member = {'name': AGENT['name'], 'pane': PANE['pane_id'], 'launch_id': 'l1'}
        agent = {k: v for k, v in AGENT.items() if k != 'terminal_id'}
        with self.assertRaises(herdr.HerdrIdentityError):
            identity.resolve(FixtureApi(agent=agent), member, self.tab, require_terminal=True)

    def test_unknown_payload_fields_are_refused_not_guessed(self):
        with self.assertRaises(herdr.HerdrSchemaError):
            identity.resolve(FixtureApi(pane=dict(PANE, agent_name=AGENT['name'])), self.member, self.tab)


class ObserveTests(unittest.TestCase):
    def test_pane_list_missing_pane_never_authorizes(self):
        state = {'tab': 'w5:t4', 'architect': {'name': AGENT['name'], 'pane': 'w5:p4'},
                 'lead': {'name': 'absent', 'pane': 'w5:p9'},
                 'workers': [{'name': 'moved', 'pane': 'w5:p1'}]}

        def api(*args):
            if args[:2] == ('workspace', 'list'):
                return {'workspaces': [{'workspace_id': 'w5'}]}
            if args[:2] == ('pane', 'list'):
                return {'panes': [copy.deepcopy(PANE), dict(PANE, pane_id='w5:p1', tab_id='other')]}
            if args[:2] == ('agent', 'get'):
                return {'agent': copy.deepcopy(AGENT)}
            raise AssertionError(args)

        report = identity.observe(api, state)
        self.assertEqual([m['health'] for m in report['members']], ['present', 'missing', 'moved'])
        self.assertEqual(report['health'], 'degraded')
        self.assertFalse(report['members'][0]['identity']['transport_present'])


class RouteTests(unittest.TestCase):
    def setUp(self):
        self.state = {'shop_id': 's1', 'run_id': 'r1', 'tab': 'w5:t4',
                      'architect': {'name': 'a', 'pane': 'p1', 'launch_id': 'a1'},
                      'lead': {'name': 'l', 'pane': 'p2', 'launch_id': 'l1', 'terminal_id': 't2'},
                      'workers': [{'name': 'w', 'pane': 'p3', 'launch_id': 'w1', 'terminal_id': 't3',
                                   'session_dir': '/sessions/w-w1'}]}
        self.envelope = {'protocol': 1, 'shop_id': 's1', 'run_id': 'r1', 'sender': 'l',
                         'sender_launch_id': 'l1', 'recipient': 'w', 'recipient_role': 'worker',
                         'recipient_launch_id': 'w1', 'recipient_terminal_id': 't3',
                         'recipient_session_dir': '/sessions/w-w1'}

    def test_valid_route(self):
        self.assertEqual(identity.validate_route(self.state, self.envelope, 'w')['pane'], 'p3')

    def test_foreign_and_stale_routes(self):
        for key in ['shop_id', 'run_id', 'sender_launch_id', 'recipient_launch_id',
                    'recipient_role', 'recipient', 'recipient_terminal_id', 'recipient_session_dir']:
            with self.subTest(key=key), self.assertRaises(RuntimeError):
                identity.validate_route(self.state, dict(self.envelope, **{key: 'wrong'}), 'w')

    def test_no_guessing_by_same_role(self):
        with self.assertRaises(RuntimeError):
            identity.validate_route(self.state, self.envelope, 'l')

    def test_transport_epoch_in_route_is_refused(self):
        for key in ('transport_epoch', 'transport'):
            with self.subTest(key=key), self.assertRaises(herdr.HerdrIdentityError):
                identity.validate_route(self.state, dict(self.envelope, **{key: 1}), 'w')

    def test_registered_member_with_fake_transport_is_refused(self):
        state = copy.deepcopy(self.state)
        state['workers'][0]['transport_epoch'] = 1
        with self.assertRaises(herdr.HerdrIdentityError):
            identity.validate_route(state, self.envelope, 'w')

    def test_build_envelope_uses_registered_launch_and_no_transport(self):
        envelope = identity.build_envelope(self.state, self.state['workers'][0], 'worker', 'T1', 2)
        self.assertEqual(envelope['recipient_launch_id'], 'w1')
        self.assertEqual(envelope['recipient_terminal_id'], 't3')
        self.assertEqual(envelope['attempt'], 2)
        self.assertNotIn('transport_epoch', envelope)
        identity.validate_route(self.state, envelope, 'w')

    def test_missing_and_moved_does_not_mutate_registration(self):
        original = copy.deepcopy(self.state)

        def api(*args):
            if args[:2] == ('workspace', 'list'):
                return {'workspaces': [{'workspace_id': 'ws'}]}
            if args[:2] == ('pane', 'list'):
                return {'panes': [{'pane_id': 'p1', 'tab_id': self.state['tab']},
                                  {'pane_id': 'p2', 'tab_id': 'elsewhere'}]}
            if args[:2] == ('agent', 'get'):
                return {'agent': {'name': 'a', 'agent': 'pi'}}
            raise AssertionError(args)

        result = identity.observe(api, self.state)
        self.assertEqual([x['health'] for x in result['members']], ['present', 'moved', 'missing'])
        self.assertEqual(result['health'], 'degraded')
        self.assertEqual(self.state, original)


if __name__ == '__main__':
    unittest.main()
