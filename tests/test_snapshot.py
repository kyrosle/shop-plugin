"""shop.snapshot/v1: schema, caps, redaction, staleness, responsibility, links.

Isolated temp runs only. No Herdr, no Pi, no live state mutation.
"""
import copy
import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest

import contracts as ct
import handoff as ho
import run
import snapshot
import transport as tr


def iso(seconds_ago, now):
    return (now - dt.timedelta(seconds=seconds_ago)).isoformat()


class SnapshotApi(object):
    """Recording fake of the typed Herdr adapter: reads only."""

    def __init__(self, members, now=None, statuses=None, workspace='ws1', tab='t1', failing=()):
        self.members = {member['pane']: member for member in members}
        self.statuses = statuses or {}
        self.workspace = workspace
        self.tab = tab
        self.failing = set(failing)
        self.calls = []

    def __call__(self, *args):
        self.calls.append(args)
        if args[:2] == ('pane', 'get'):
            pane = args[2]
            if pane in self.failing:
                raise RuntimeError('pane get failed')
            member = self.members.get(pane, {})
            return {'pane': {'pane_id': pane, 'tab_id': member.get('tab_id', self.tab),
                             'workspace_id': self.workspace, 'terminal_id': 'term-' + pane,
                             'agent': 'pi', 'agent_status': self.statuses.get(pane, 'idle')}}
        if args[:2] == ('agent', 'get'):
            pane = args[2]
            member = self.members.get(pane)
            if not member:
                raise RuntimeError('no agent for ' + pane)
            return {'agent': {'name': member['name'], 'agent': 'pi', 'pane_id': pane,
                              'tab_id': member.get('tab_id', self.tab),
                              'terminal_id': 'term-' + pane,
                              'agent_status': self.statuses.get(pane, 'idle'),
                              'workspace_id': self.workspace}}
        raise AssertionError('snapshot must not call ' + repr(args))


def attention_codes(doc):
    return [entry['code'] for entry in doc['attention']]


def _all_keys(node, out=None):
    out = [] if out is None else out
    if isinstance(node, dict):
        for key, value in node.items():
            out.append(key)
            _all_keys(value, out)
    elif isinstance(node, list):
        for item in node:
            _all_keys(item, out)
    return out


def assert_no_lifecycle_verdict(testcase, doc):
    """The snapshot must never render a stuck/dead/done verdict from age or silence."""
    for verdict in ('stuck', 'dead', 'timed_out', 'auto_close', 'auto_dispatch', 'assumed_done'):
        testcase.assertNotIn(verdict, attention_codes(doc))
        testcase.assertNotIn(verdict, json.dumps(doc.get('unknowns', [])))
    # No structured verdict field anywhere: only explicit facts and hints.
    testcase.assertNotIn('verdict', _all_keys(doc))
    testcase.assertNotIn('verdicts', _all_keys(doc))


class SnapshotTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)
        self.r = run.Runs(self.repo)
        self.rid = self.r.new('snapshot')['created']
        self.now = dt.datetime.now(dt.timezone.utc)
        self.state = {
            'shop_id': 'shop1', 'run_id': self.rid, 'phase': 'ready', 'tab': 't1',
            'cwd': str(self.repo), 'prefix': 's1',
            'architect': {'name': 'arch', 'pane': 'p1', 'launch_id': 'a1', 'terminal_id': 'term-p1'},
            'lead': {'name': 'lead', 'pane': 'p2', 'launch_id': 'l1', 'terminal_id': 'term-p2'},
            'workers': [{'name': 'wa', 'pane': 'p3', 'launch_id': 'w1', 'terminal_id': 'term-p3'},
                        {'name': 'wb', 'pane': 'p4', 'launch_id': 'w2', 'terminal_id': 'term-p4'}],
        }
        ct.atomic(ct.binding_file(self.repo), {self.rid: {'shop_id': 'shop1', 'tab': 't1',
                                                          'lead': self.state['lead']}})

    def ticket(self, **overrides):
        doc = {'ticket_id': 'T1', 'owner': 'wa', 'objective': 'inspect', 'kind': 'analysis',
               'worktree': str(self.repo), 'scope': ['src'], 'checks': [], 'depends_on': []}
        doc.update(overrides)
        return ct.new_ticket(self.repo, self.rid, doc)

    def build(self, api=None, tickets=None, **kwargs):
        return snapshot.build(self.state, tickets if tickets is not None else ct.tickets(self.repo, self.rid),
                              api=api, repo=self.repo, run_id=self.rid, now=self.now, **kwargs)


class SchemaTests(SnapshotTestBase):
    def test_ready_phase_does_not_hide_recovery_flag(self):
        self.state.update(phase='ready', recovery_required=True)
        document = self.build()
        self.assertTrue(document['shop']['recovery_required'])
        self.assertIn('registration_needs_recovery', [x['code'] for x in document['attention']])

    def test_schema_sections_and_determinism(self):
        doc = self.build()
        self.assertEqual(doc['schema'], 'shop.snapshot/v1')
        self.assertEqual(doc['shop']['run_id'], self.rid)
        self.assertEqual(doc['shop']['binding'], {'run_id': self.rid, 'shop_id': 'shop1', 'ok': True,
                                                  'detail': None})
        for section in ('generated_at', 'generator', 'herdr_runtime', 'shop', 'members',
                        'unregistered_owner_tickets', 'capacity', 'transport', 'handoff', 'events',
                        'links', 'attention', 'unknowns', 'redaction', 'limits', 'scope'):
            self.assertIn(section, doc, section)
        self.assertEqual(doc['capacity'], {'lead': 1, 'worker': 2, 'max_each': 2})
        self.assertEqual(doc['redaction']['omitted'],
                         ['prompt_text', 'transcript', 'message_body', 'env_values', 'credentials',
                          'session_file_content'])
        first = snapshot.serialize(doc)
        self.assertEqual(first, snapshot.serialize(doc))
        self.assertEqual(json.loads(first)['schema'], 'shop.snapshot/v1')

    def test_desired_and_observed_are_separate_facts(self):
        api = SnapshotApi([self.state['architect'], self.state['lead'], *self.state['workers']],
                          statuses={'p3': 'working'})
        doc = self.build(api=api)
        worker = next(row for row in doc['members'] if row['name'] == 'wa')
        self.assertEqual(worker['desired']['pane'], 'p3')
        self.assertEqual(worker['desired']['launch_id'], 'w1')
        self.assertEqual(worker['observed']['status'], 'working')
        self.assertEqual(worker['observed']['health'], 'present')
        self.assertEqual(worker['observed']['source'], 'live')
        self.assertIsNotNone(worker['observed']['live_checked_at'])
        # reads only: pane get + agent get
        self.assertEqual({call[0] for call in api.calls}, {'pane', 'agent'})
        self.assertTrue(all(call[1] == 'get' for call in api.calls))

    def test_unknown_and_unreachable_members_are_rendered_as_unknown(self):
        api = SnapshotApi([self.state['architect'], self.state['lead'], *self.state['workers']],
                          failing=('p4',))
        doc = self.build(api=api, roles=('worker',), scope_label='execution-members')
        rows = {row['name']: row for row in doc['members']}
        self.assertEqual(rows['wb']['observed']['status'], 'unreachable')
        self.assertIn('pane get failed', rows['wb']['observed']['error'])
        codes = [entry['code'] for entry in doc['attention']]
        self.assertIn('member_unobserved', codes)
        assert_no_lifecycle_verdict(self, doc)
        self.assertNotIn('auto_retry', snapshot.serialize(doc))

    def test_identity_refusal_is_visible_not_healthy(self):
        self.state['workers'][0]['transport_epoch'] = 1
        api = SnapshotApi([self.state['architect'], self.state['lead'], *self.state['workers']])
        doc = self.build(api=api)
        row = next(item for item in doc['members'] if item['name'] == 'wa')
        self.assertEqual(row['status'], 'identity_refused')
        self.assertIn('transport epoch', row['identity']['refused'])
        self.assertFalse(row['identity']['transport_present'])
        self.assertIn('identity_refused', [entry['code'] for entry in doc['attention']])


class BoundsAndRedactionTests(SnapshotTestBase):
    def test_member_and_ticket_caps_are_enforced_with_attention(self):
        self.state['workers'] = [{'name': 'w%d' % index, 'pane': 'p%d' % index,
                                  'launch_id': 'l%d' % index, 'terminal_id': 't%d' % index}
                                 for index in range(200)]
        tickets = []
        for index in range(200):
            tickets.append({'ticket_id': 'T%d' % index, 'owner': 'w%d' % index, 'attempt': 1,
                            'status': 'assigned', 'kind': 'analysis', 'objective': 'x' * 900,
                            'depends_on': [], 'dispatch': {'delivery': 'submitted', 'sent_at': iso(400, self.now)}})
        doc = snapshot.build(self.state, tickets, api=None, repo=self.repo, run_id=self.rid, now=self.now)
        self.assertLessEqual(len(doc['members']), snapshot.LIMITS['members'])
        self.assertIn('snapshot_truncated', [entry['code'] for entry in doc['attention']])
        text = snapshot.serialize(doc)
        self.assertLessEqual(len(text.encode('utf-8')), snapshot.LIMITS['total_bytes'])

    def test_byte_cap_applies_even_with_many_rows(self):
        self.state['workers'] = [{'name': 'w%d' % index, 'pane': 'p%d' % index} for index in range(64)]
        tickets = []
        for index in range(128):
            tickets.append({'ticket_id': 'T%d' % index, 'owner': 'w%d' % (index % 64), 'attempt': 1,
                            'status': 'assigned', 'kind': 'development', 'objective': 'y' * 900,
                            'depends_on': [],
                            'dispatch': {'delivery': 'submitted', 'sent_at': iso(1000, self.now)}})
        for index in range(64):
            ct.atomic(ct.run_path(self.repo, self.rid) / 'tickets' / ('T%d.a1.checkpoint.json' % index),
                      {'progress': 'p' * 1000, 'next_steps': ['n' * 250] * 5, 'saved_at': iso(400, self.now)})
        doc = snapshot.build(self.state, tickets, api=None, repo=self.repo, run_id=self.rid, now=self.now)
        text = snapshot.serialize(doc)
        self.assertLessEqual(len(text.encode('utf-8')), snapshot.LIMITS['total_bytes'])
        self.assertTrue(json.loads(text)['truncated'])

    def test_secrets_prompts_and_env_are_never_serialized(self):
        secret = 'sk-SECRET-TOKEN-1234567890'
        excluded = 'EXCLUDED_PROMPT_MARKER'
        visible = 'OBJECTIVE_VISIBLE_BY_DESIGN'
        ticket = {'ticket_id': 'T1', 'run_id': self.rid, 'owner': 'wa', 'attempt': 1,
                  'status': 'assigned', 'kind': 'analysis', 'objective': 'objective ' + visible,
                  'depends_on': [], 'dispatch': {'delivery': 'submitted', 'sent_at': iso(10, self.now)}}
        # Excluded sources: registry free text, the checkpoint's non-allowlisted keys and
        # credential-shaped/environment/home-path material inside retained free text.
        self.state['prompt'] = excluded
        self.state['env'] = {'API_TOKEN': secret}
        self.state['transcript'] = excluded
        ct.atomic(ct.attempt_path(self.repo, ticket, 'checkpoint'),
                  {'progress': 'worked on API_TOKEN=' + secret,
                   'next_steps': ['read /Users/someone/.config/shop-workstation/bridge.json '
                                  + 'API_KEY=' + secret + ' using sk-ABCDEFGH12345678'],
                   'saved_at': iso(1, self.now), 'transcript': excluded,
                   'session_file_content': excluded, 'attachments': [excluded]})
        doc = self.build(tickets=[ticket])
        text = snapshot.serialize(doc)
        self.assertNotIn(secret, text)
        self.assertNotIn(excluded, text)
        self.assertNotIn('/Users/someone', text)
        self.assertIn('<redacted-env>', text)
        self.assertIn('<redacted-credential>', text)
        self.assertIn('<home>', text)
        # The raw registry is never dumped: unknown registry keys are dropped.
        self.assertNotIn('"prompt"', text)
        self.assertNotIn('"env"', text)
        # Documented boundary: the ticket objective is an allowlisted, capped free-text
        # field, so its own content remains visible by design.
        self.assertIn(visible, text)

    def test_capped_free_text_fields(self):
        row = snapshot.ticket_row({'ticket_id': 'T1', 'owner': 'wa', 'attempt': 1, 'status': 'assigned',
                                   'kind': 'analysis', 'objective': 'o' * 5000, 'depends_on': [],
                                   'dispatch': {'sent_at': iso(1, self.now)}}, self.repo, self.now)
        self.assertLessEqual(len(row['objective']), snapshot.LIMITS['objective_chars'] + 1)


class StalenessTests(SnapshotTestBase):
    def test_checkpoint_age_is_a_hint_not_a_verdict(self):
        ticket = {'ticket_id': 'T1', 'run_id': self.rid, 'owner': 'wa', 'attempt': 1,
                  'status': 'assigned', 'kind': 'analysis', 'objective': 'inspect',
                  'depends_on': [], 'dispatch': {'delivery': 'submitted', 'sent_at': iso(400, self.now)}}
        ct.atomic(ct.attempt_path(self.repo, ticket, 'checkpoint'),
                  {'progress': 'still working', 'next_steps': ['finish'], 'saved_at': iso(181, self.now)})
        doc = self.build(api=SnapshotApi([self.state['architect'], self.state['lead'], *self.state['workers']],
                                         statuses={'p3': 'working'}),
                         tickets=[ticket])
        row = next(item for item in doc['members'] if item['name'] == 'wa')
        entry = row['tickets'][0]['checkpoint']
        self.assertTrue(entry['stale'])
        self.assertGreaterEqual(entry['age_seconds'], 180)
        codes = attention_codes(doc)
        self.assertIn('checkpoint_stale', codes)
        detail = next(item['detail'] for item in doc['attention'] if item['code'] == 'checkpoint_stale')
        self.assertIn('hint', detail)
        assert_no_lifecycle_verdict(self, doc)

    def test_long_working_member_produces_no_death_verdict(self):
        api = SnapshotApi([self.state['architect'], self.state['lead'], *self.state['workers']],
                          statuses={'p3': 'working'})
        doc = self.build(api=api)
        assert_no_lifecycle_verdict(self, doc)

    def test_missing_checkpoint_after_dispatch_window(self):
        ticket = {'ticket_id': 'T1', 'run_id': self.rid, 'owner': 'wa', 'attempt': 1,
                  'status': 'assigned', 'kind': 'analysis', 'objective': 'inspect',
                  'depends_on': [], 'dispatch': {'delivery': 'submitted', 'sent_at': iso(400, self.now)}}
        doc = self.build(tickets=[ticket])
        self.assertIn('no_checkpoint_yet', attention_codes(doc))

    def test_snapshot_staleness_threshold(self):
        doc = self.build()
        self.assertFalse(snapshot.stale(doc, self.now)['stale'])
        old = dict(doc, generated_at=iso(31, self.now))
        self.assertTrue(snapshot.stale(old, self.now)['stale'])


class ResponsibilityTests(SnapshotTestBase):
    def test_review_and_unclaimed_states_are_explicit(self):
        ticket = {'ticket_id': 'T1', 'run_id': self.rid, 'owner': 'wa', 'attempt': 1, 'status': 'review',
                  'kind': 'analysis', 'objective': 'inspect', 'depends_on': [], 'result_path': '/tmp/r.json'}
        doc = self.build(tickets=[ticket])
        entry = next(item for item in doc['members'] if item['name'] == 'wa')['tickets'][0]
        self.assertEqual(entry['responsibility'], {'owner': 'wa', 'state': 'review'})
        self.assertIn('result_awaiting_review', attention_codes(doc))

        ownerless = dict(ticket, ticket_id='T-ownerless', owner='')
        doc2 = self.build(tickets=[ownerless])
        entry = next(item for item in doc2['unregistered_owner_tickets'] if item['ticket_id'] == 'T-ownerless')
        self.assertIsNone(entry['owner'])
        attention = next(item for item in doc2['attention'] if item['code'] == 'ticket_unclaimed')
        self.assertIn('未接手', attention['detail'])

    def test_accepted_ticket_is_never_rendered_as_open_work(self):
        ticket = {'ticket_id': 'T1', 'run_id': self.rid, 'owner': 'wa', 'attempt': 1, 'status': 'accepted',
                  'kind': 'analysis', 'objective': 'inspect', 'depends_on': []}
        doc = self.build(tickets=[ticket])
        entry = next(item for item in doc['members'] if item['name'] == 'wa')['tickets'][0]
        self.assertEqual(entry['responsibility']['state'], 'accepted')
        self.assertNotIn('ticket_unclaimed', attention_codes(doc))


class LinkAndIntegrationTests(SnapshotTestBase):
    def test_links_carry_ids_and_read_only_focus_commands(self):
        api = SnapshotApi([self.state['architect'], self.state['lead'], *self.state['workers']])
        doc = self.build(api=api, caller_pane='p3')
        link = next(item for item in doc['links'] if item['pane_id'] == 'p3')
        self.assertEqual(link['workspace_id'], 'ws1')
        self.assertEqual(link['tab_id'], 't1')
        self.assertEqual(link['focus'], {'command': ['herdr', 'workspace', 'focus', 'ws1'],
                                         'then': ['herdr', 'tab', 'focus', 't1']})
        self.assertTrue(link['current'])
        self.assertIsNone(link['pane_focus'])
        self.assertFalse(next(item for item in doc['links'] if item['pane_id'] == 'p4')['current'])
        # Focus is display-only: the snapshot never executes a command.
        self.assertTrue(all(call[0] in ('pane', 'agent') for call in api.calls))

    def test_transport_and_handoff_sections_read_real_stores(self):
        envelope = json.loads((Path(__file__).resolve().parent / 'fixtures-transport' /
                               'envelope-note.json').read_text())
        envelope = dict(envelope, run_id=self.rid)
        state = dict(self.state, shop_id='fixture-shop', lead={'name': 'lead', 'pane': 'p2',
                                                               'launch_id': 'lead-launch-1'},
                     workers=[{'name': 'worker', 'pane': 'p3', 'launch_id': 'worker-launch-1'}])
        tr.decide(self.repo, self.rid, state, envelope, 'worker', 'p3')
        tr.record_receipt(self.repo, self.rid, envelope['message_id'], 'unknown', 'injection unconfirmed')
        ho.propose(self.repo, self.rid, 'h-1', summary='handoff under test')
        doc = snapshot.build(state, [], api=None, repo=self.repo, run_id=self.rid, now=self.now)
        self.assertTrue(doc['transport']['implemented'])
        self.assertEqual(doc['transport']['unknown'], 1)
        self.assertEqual(doc['transport']['pending'], 0)
        self.assertEqual(doc['transport']['endpoint']['protocol'], 'pi-shop-transport')
        self.assertEqual(doc['transport']['peers'], [])
        self.assertIn('transport_unknown_records', [item['code'] for item in doc['attention']])
        self.assertTrue(doc['handoff']['implemented'])
        self.assertEqual(doc['handoff']['counts']['proposed'], 1)
        self.assertEqual(doc['handoff']['open'][0]['handoff_id'], 'h-1')

    def test_events_section_is_honest_before_any_fact_arrives(self):
        doc = self.build()
        # The hook layer ships, but no fact has been recorded yet: coverage is
        # unknown, never a claim of observation.
        self.assertEqual(doc['events']['coverage'], 'unknown')
        self.assertEqual(doc['events']['fact_count'], 0)
        self.assertEqual(sorted(doc['events']['subscriptions']),
                         ['pane.agent_status_changed', 'pane.closed', 'pane.exited'])
        self.assertIn('display-only', doc['events']['authority'])
        self.assertIn('events_unknown', [item['code'] for item in doc['attention']])
        self.assertTrue(any('event coverage unknown' in item for item in doc['unknowns']))
        manifest = (Path(__file__).resolve().parents[1] / 'herdr-plugin.toml').read_text()
        self.assertIn('core/events.py', manifest)
        self.assertIn('pane.agent_status_changed', manifest)

    def test_binding_mismatch_and_unbound_are_reported_not_hidden(self):
        ct.atomic(ct.binding_file(self.repo), {})
        doc = self.build()
        self.assertFalse(doc['shop']['binding']['ok'])
        self.assertIn('manual reconciliation', doc['shop']['binding']['detail'])
        unbound = dict(self.state, run_id=None)
        doc2 = snapshot.build(unbound, [], api=None, now=self.now)
        self.assertFalse(doc2['shop']['bound'])
        self.assertFalse(doc2['shop']['binding']['ok'])

    def test_summarize_is_bounded_and_hides_nothing_extra(self):
        doc = self.build()
        text = snapshot.summarize(doc)
        self.assertLessEqual(len(text), 4000)
        self.assertIn('shop.snapshot/v1', text)

    def test_one_producer_feeds_every_status_surface(self):
        shop_source = (Path(__file__).resolve().parents[1] / 'core/shop.py').read_text()
        supervision_source = (Path(__file__).resolve().parents[1] / 'core/supervision.py').read_text()
        plugin_source = (Path(__file__).resolve().parents[1] / 'core/plugin.py').read_text()
        cli_source = (Path(__file__).resolve().parents[1] / 'bin/herdr-shop').read_text()
        self.assertIn('snapshot_module.build', shop_source)
        self.assertIn('snapshot_module.serialize', shop_source)
        self.assertIn('snapshot_module.build', supervision_source)
        self.assertIn("'status': 'status'", plugin_source)
        self.assertIn("core/shop.py'), action", plugin_source)
        # The plugin action prints the same bounded document the CLI produces.
        self.assertIn('r.stdout', plugin_source)
        self.assertIn('capture_output=True', plugin_source)
        self.assertIn('core/shop.py', cli_source)
        # No surface may dump the raw state file again.
        self.assertNotIn("'shop': state", shop_source)
        self.assertNotIn("'observed': identity.observe", shop_source)

    def test_snapshot_build_never_mutates_state_or_tickets(self):
        ticket = self.ticket(status='assigned')
        before_state = copy.deepcopy(self.state)
        before_tickets = copy.deepcopy(ct.tickets(self.repo, self.rid))
        api = SnapshotApi([self.state['architect'], self.state['lead'], *self.state['workers']])
        doc = self.build(api=api, tickets=[dict(ticket)])
        self.assertEqual(self.state, before_state)
        self.assertEqual(ct.tickets(self.repo, self.rid), before_tickets)
        self.assertIn(doc['schema'], ('shop.snapshot/v1',))


if __name__ == '__main__':
    unittest.main()


class EventMergeTests(SnapshotTestBase):
    """Event facts annotate observations and never become authoritative."""

    def facts(self, *entries):
        return {'coverage': 'observed', 'last_event_at': entries[-1]['at'], 'revision': entries[-1]['revision'],
                'subscriptions': ['pane.agent_status_changed', 'pane.closed', 'pane.exited'],
                'facts': list(entries), 'gaps': [], 'authority': 'display-only'}

    def fact(self, pane, status, revision=1, at=None):
        return {'schema': 'shop.event_fact/v1', 'revision': revision, 'at': at or iso(5, self.now),
                'event': 'pane.agent_status_changed', 'pane_id': pane, 'workspace_id': 'ws1',
                'agent_status': status, 'source': 'herdr_event', 'shop_id': 'shop1'}

    def build_with_events(self, facts, statuses=None):
        api = SnapshotApi([self.state['architect'], self.state['lead'], *self.state['workers']],
                          statuses=statuses or {})
        return self.build(api=api, facts=facts)

    def member(self, doc, name):
        return next(row for row in doc['members'] if row['name'] == name)

    def test_agreeing_fact_marks_live_plus_event(self):
        doc = self.build_with_events(self.facts(self.fact('p3', 'working')), statuses={'p3': 'working'})
        observed = self.member(doc, 'wa')['observed']
        self.assertEqual(observed['status'], 'working')
        self.assertEqual(observed['source'], 'live+event')
        self.assertTrue(observed['authoritative'])
        self.assertEqual(observed['event_status'], 'working')
        self.assertIsInstance(observed['event_age_seconds'], int)
        self.assertEqual(doc['events']['coverage'], 'observed')
        self.assertEqual(doc['events']['fact_count'], 1)

    def test_disagreeing_fact_keeps_live_status_and_reports_a_gap(self):
        doc = self.build_with_events(self.facts(self.fact('p3', 'blocked')), statuses={'p3': 'working'})
        observed = self.member(doc, 'wa')['observed']
        self.assertEqual(observed['status'], 'working')          # live read wins
        self.assertEqual(observed['source'], 'live')
        self.assertTrue(observed['authoritative'])
        self.assertTrue(observed['event_disagrees'])
        self.assertEqual(doc['events']['coverage'], 'partial')
        codes = [entry['code'] for entry in doc['attention']]
        self.assertIn('event_gap', codes)
        detail = next(entry['detail'] for entry in doc['attention'] if entry['code'] == 'event_gap')
        self.assertIn('live read wins', detail)

    def test_event_only_view_is_not_authoritative(self):
        doc = self.build(api=None, facts=self.facts(self.fact('p3', 'idle')))
        observed = self.member(doc, 'wa')['observed']
        self.assertEqual(observed['status'], 'idle')
        self.assertEqual(observed['source'], 'event')
        self.assertFalse(observed['authoritative'])
        self.assertIsNone(observed['live_checked_at'])
        self.assertIn('display-only', doc['events']['authority'])

    def test_revision_gap_is_surfaced(self):
        facts = self.facts(self.fact('p3', 'working', revision=1))
        facts['coverage'] = 'partial'
        facts['gaps'] = [{'from': 1, 'to': 4}]
        facts['reason'] = 'revision gap detected; events may have been missed'
        doc = self.build_with_events(facts, statuses={'p3': 'working'})
        self.assertEqual(doc['events']['coverage'], 'partial')
        self.assertIn('event_gap', [entry['code'] for entry in doc['attention']])
        self.assertTrue(any('gapped' in item or 'partial' in item for item in doc['unknowns']))

    def test_facts_cannot_authorize_or_mutate_anything(self):
        before = copy.deepcopy(self.state)
        api = SnapshotApi([self.state['architect'], self.state['lead'], *self.state['workers']],
                          statuses={'p3': 'working'})
        doc = self.build(api=api, facts=self.facts(self.fact('p3', 'idle')))
        self.assertEqual(self.state, before)
        self.assertTrue(all(call[1] == 'get' for call in api.calls))
        text = snapshot.serialize(doc)
        for forbidden in ('accepted', 'auto_dispatch', 'auto_close'):
            self.assertNotIn(forbidden, [entry['code'] for entry in doc['attention']])
        self.assertIn('"authoritative"', text)
