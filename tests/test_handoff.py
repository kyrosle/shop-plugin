"""Handoff state machine: transitions, idempotency and separation from tickets."""
from pathlib import Path
import tempfile
import unittest

import contracts as ct
import handoff as ho
import run


class HandoffTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)
        self.r = run.Runs(self.repo)
        self.rid = self.r.new('handoff')['created']

    def propose(self, handoff_id='h-1', **kwargs):
        return ho.propose(self.repo, self.rid, handoff_id, summary=kwargs.pop('summary', 'lead -> worker handoff'),
                          **kwargs)

    def test_propose_is_idempotent_and_content_bound(self):
        first = self.propose()
        self.assertEqual(first['state'], 'proposed')
        again = self.propose()
        self.assertEqual(again['history'], first['history'])
        with self.assertRaises(ho.HandoffError):
            ho.propose(self.repo, self.rid, 'h-1', summary='different content')

    def test_happy_path_and_monotonic_history(self):
        self.propose()
        self.assertEqual(ho.transition(self.repo, self.rid, 'h-1', 'accept', actor='worker')['state'], 'accepted')
        self.assertEqual(ho.transition(self.repo, self.rid, 'h-1', 'deliver')['state'], 'delivered')
        record = ho.load(self.repo, self.rid, 'h-1')
        self.assertEqual([entry['state'] for entry in record['history']],
                         ['proposed', 'accepted', 'delivered'])
        with self.assertRaises(ho.HandoffError):
            ho.transition(self.repo, self.rid, 'h-1', 'cancel')

    def test_replay_of_a_settled_action_adds_no_history(self):
        self.propose()
        ho.transition(self.repo, self.rid, 'h-1', 'accept')
        before = ho.load(self.repo, self.rid, 'h-1')['history']
        self.assertEqual(ho.transition(self.repo, self.rid, 'h-1', 'accept')['history'], before)

    def test_cancellation_is_explicit_and_separate_from_acceptance(self):
        self.propose()
        self.assertEqual(ho.transition(self.repo, self.rid, 'h-1', 'request-cancel')['state'],
                         'cancellation_requested')
        self.assertEqual(ho.transition(self.repo, self.rid, 'h-1', 'cancel', detail='writer stopped')['state'],
                         'cancelled')
        with self.assertRaises(ho.HandoffError):
            ho.transition(self.repo, self.rid, 'h-1', 'accept')

    def test_blocked_can_resume_or_be_cancelled(self):
        self.propose()
        ho.transition(self.repo, self.rid, 'h-1', 'accept')
        self.assertEqual(ho.transition(self.repo, self.rid, 'h-1', 'block', detail='dependency missing')['state'],
                         'blocked')
        self.assertEqual(ho.transition(self.repo, self.rid, 'h-1', 'accept')['state'], 'accepted')
        ho.transition(self.repo, self.rid, 'h-1', 'block')
        ho.transition(self.repo, self.rid, 'h-1', 'request-cancel')
        self.assertEqual(ho.transition(self.repo, self.rid, 'h-1', 'cancel')['state'], 'cancelled')

    def test_illegal_actions_and_unknown_ids_are_refused(self):
        self.propose()
        for action in ('deliver', 'cancel'):
            with self.subTest(action=action), self.assertRaises(ho.HandoffError):
                ho.transition(self.repo, self.rid, 'h-1', action)
        with self.assertRaises(ho.HandoffError):
            ho.transition(self.repo, self.rid, 'missing', 'accept')
        with self.assertRaises(ho.HandoffError):
            ho.transition(self.repo, self.rid, 'h-1', 'approve')

    def test_rejected_is_terminal_and_listing_works(self):
        self.propose()
        ho.transition(self.repo, self.rid, 'h-1', 'reject', detail='out of scope')
        with self.assertRaises(ho.HandoffError):
            ho.transition(self.repo, self.rid, 'h-1', 'accept')
        self.propose('h-2')
        self.assertEqual(sorted(item['handoff_id'] for item in ho.listing(self.repo, self.rid)), ['h-1', 'h-2'])

    def test_handoff_never_touches_canonical_tickets(self):
        self.propose(ticket_id='T-ignored', message_id='m-1')
        ho.transition(self.repo, self.rid, 'h-1', 'accept')
        ho.transition(self.repo, self.rid, 'h-1', 'deliver')
        self.assertEqual(ct.tickets(self.repo, self.rid), [])
        tickets_dir = ct.run_path(self.repo, self.rid) / 'tickets'
        self.assertEqual(list(tickets_dir.glob('*.ticket.json')), [])
        self.assertTrue((ct.run_path(self.repo, self.rid) / 'handoff' / 'h-1.json').is_file())

    def test_unsafe_handoff_id_is_refused(self):
        for bad in ('../escape', 'a/b', '', '.hidden'):
            with self.subTest(bad=bad), self.assertRaises(ho.HandoffError):
                ho.propose(self.repo, self.rid, bad)

    def test_cli_handoff_propose_and_transition(self):
        import json
        import os
        import subprocess
        import sys
        root = Path(__file__).resolve().parents[1]
        env = dict(os.environ, PYTHONPATH=str(root / 'core'))
        def call(*args):
            return subprocess.run([sys.executable, str(root / 'core/transport_cli.py'), '--repo', str(self.repo),
                                   '--run', self.rid, 'handoff', *args], capture_output=True, text=True, env=env)
        proposed = call('--action', 'propose', '--handoff-id', 'h-cli', '--summary', 'cli handoff')
        self.assertNotEqual(proposed.returncode, 0)
        self.assertIn('verified member identity', proposed.stderr)
        accepted = call('--action', 'accept', '--handoff-id', 'h-cli', '--actor', 'worker')
        self.assertNotEqual(accepted.returncode, 0)
        self.assertFalse(ho.handoff_file(self.repo, self.rid, 'h-cli').exists())
        missing = call('--action', 'accept', '--handoff-id', 'nope')
        self.assertNotEqual(missing.returncode, 0)

    def test_cli_handoff_job_and_status_paths(self):
        import os
        import subprocess
        import sys
        root = Path(__file__).resolve().parents[1]
        env = dict(os.environ, PYTHONPATH=str(root / 'core'))
        state = self.repo / 'state.json'
        ct.atomic(state, {'shop_id': 's', 'run_id': self.rid, 'tab': 't1',
                          'lead': {'name': 'lead', 'pane': 'p2', 'launch_id': 'l1'},
                          'workers': [{'name': 'worker', 'pane': 'p3', 'launch_id': 'w1'}]})
        result = subprocess.run([sys.executable, str(root / 'core/transport_cli.py'), '--repo', str(self.repo),
                                 '--run', self.rid, 'status'], capture_output=True, text=True, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
