"""Durable dedupe store, identity verification and crash-window behaviour.

Isolated temp runs only; no live transport, socket, Pi, Herdr or plugin action.
"""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import contracts as ct
import run
import transport as tr

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / 'fixtures-transport'


def fixture(name):
    return json.loads((FIXTURES / name).read_text())


class TransportStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)
        self.r = run.Runs(self.repo)
        self.rid = self.r.new('transport')['created']
        self.state = {
            'shop_id': 'fixture-shop', 'run_id': self.rid, 'tab': 't1', 'cwd': str(self.repo),
            'architect': {'name': 'architect', 'pane': 'p1', 'launch_id': 'arch-launch-1'},
            'lead': {'name': 'lead', 'pane': 'p2', 'launch_id': 'lead-launch-1'},
            'workers': [{'name': 'worker', 'pane': 'p3', 'launch_id': 'worker-launch-1'}],
        }
        self.envelope = dict(fixture('envelope-note.json'), run_id=self.rid)

    def decide(self, envelope=None, self_name='worker', self_pane='p3'):
        return tr.decide(self.repo, self.rid, self.state, envelope or self.envelope, self_name, self_pane)

    def test_store_lives_under_the_run_and_is_not_a_ticket(self):
        self.decide()
        path = tr.dedupe_file(self.repo, self.rid)
        self.assertTrue(str(path).startswith(str(ct.run_path(self.repo, self.rid))))
        self.assertTrue(path.is_file())
        self.assertEqual(ct.tickets(self.repo, self.rid), [])
        self.assertFalse((self.r.path(self.rid) / 'tickets' / 'transport.json').exists())

    def test_valid_envelope_is_recorded_before_injection_is_reported(self):
        decision = self.decide()
        self.assertEqual(decision['decision'], 'inject')
        stored = tr.record(self.repo, self.rid, decision['message_id'])
        self.assertEqual(stored['state'], 'received')
        self.assertEqual(stored['sender']['launch_id'], 'lead-launch-1')
        self.assertEqual(stored['reply_to'], None)
        self.assertEqual(tr.status(self.repo, self.rid)['states'], {'received': 1})

    def test_foreign_identity_and_bad_shapes_are_refused(self):
        cases = {
            'envelope-foreign-run.json': 'E_RUN_MISMATCH',
            'envelope-unknown-field.json': 'E_MALFORMED',
            'envelope-stale-sender-launch.json': 'E_RUN_MISMATCH',
            'envelope-bad-body-hash.json': 'E_MALFORMED',
        }
        for name, code in cases.items():
            with self.subTest(name=name):
                envelope = dict(fixture(name), run_id=self.rid) if name != 'envelope-foreign-run.json' else fixture(name)
                with self.assertRaises(tr.TransportReject) as caught:
                    self.decide(envelope)
                self.assertEqual(caught.exception.code, code)
        with self.assertRaises(tr.TransportReject) as self_target:
            self.decide(dict(fixture('envelope-self-target.json'), run_id=self.rid),
                        self_name='lead', self_pane='p2')
        self.assertEqual(self_target.exception.code, 'E_SELF_TARGET')
        with self.assertRaises(tr.TransportReject):
            self.decide(dict(self.envelope, shop_id='other-shop'))
        with self.assertRaises(tr.TransportReject):
            self.decide(dict(self.envelope, recipient={'member_id': 'worker', 'launch_id': 'stale'}))
        with self.assertRaises(tr.TransportReject):
            self.decide(dict(self.envelope, recipient={'member_id': 'nobody', 'launch_id': 'worker-launch-1'}))
        with self.assertRaises(tr.TransportReject):
            self.decide(dict(self.envelope, reply_to=5))
        with self.assertRaises(tr.TransportReject):
            self.decide(dict(self.envelope, kind='chat'))
        with self.assertRaises(tr.TransportReject):
            self.decide(dict(self.envelope, body={'text': 'x' * (33 * 1024)},
                             body_sha256=tr.sha256_hex('x' * (33 * 1024))))
        if tr.dedupe_file(self.repo, self.rid).exists():
            self.assertEqual(ct.load(tr.dedupe_file(self.repo, self.rid))['records'], {})

    def test_self_pane_mismatch_is_refused(self):
        with self.assertRaises(tr.TransportReject) as caught:
            self.decide(self_pane='p9')
        self.assertEqual(caught.exception.code, 'E_RUN_MISMATCH')

    def test_same_id_same_body_is_a_duplicate_never_a_second_injection(self):
        first = self.decide()
        self.assertEqual(first['decision'], 'inject')
        second = self.decide()
        self.assertEqual(second['decision'], 'duplicate')
        self.assertEqual(second['state'], 'received')
        self.assertEqual(len(tr.load_store(self.repo, self.rid)['records']), 1)

    def test_same_id_different_body_is_a_recorded_conflict(self):
        self.decide()
        with self.assertRaises(tr.TransportReject) as caught:
            self.decide(dict(self.envelope, body={'text': 'different'}, body_sha256=tr.sha256_hex('different')))
        self.assertEqual(caught.exception.code, 'E_MESSAGE_ID_REUSE')
        stored = tr.record(self.repo, self.rid, self.envelope['message_id'])
        self.assertEqual(stored['state'], 'received')
        self.assertEqual(len(stored['conflicts']), 1)
        self.assertEqual(stored['body_sha256'], tr.sha256_hex(self.envelope['body']['text']))

    def test_state_machine_is_monotonic_and_unknown_blocks_automatic_resolution(self):
        self.decide()
        message_id = self.envelope['message_id']
        self.assertEqual(tr.transition(self.repo, self.rid, message_id, 'injected')['state'], 'injected')
        self.assertEqual(tr.transition(self.repo, self.rid, message_id, 'injected')['state'], 'injected')
        self.assertEqual(tr.transition(self.repo, self.rid, message_id, 'business_accepted')['state'],
                         'business_accepted')
        with self.assertRaises(tr.TransportReject):
            tr.transition(self.repo, self.rid, message_id, 'received')
        other = dict(self.envelope, message_id='m-unknown')
        self.decide(other)
        tr.transition(self.repo, self.rid, 'm-unknown', 'unknown', 'crash window before injection')
        with self.assertRaises(tr.TransportReject):
            tr.transition(self.repo, self.rid, 'm-unknown', 'injected')
        with self.assertRaises(tr.TransportReject):
            tr.transition(self.repo, self.rid, 'missing-id', 'injected')

    def test_crash_between_record_and_injection_stays_unresolved_and_never_reinjects(self):
        self.decide()
        # The extension never got to call the Pi API: no receipt was recorded.
        self.assertEqual([item['state'] for item in tr.unresolved(self.repo, self.rid)], ['received'])
        again = self.decide()
        self.assertEqual(again['decision'], 'duplicate')
        self.assertNotEqual(again['decision'], 'inject')
        tr.transition(self.repo, self.rid, self.envelope['message_id'], 'unknown', 'injection unconfirmed')
        self.assertEqual([item['state'] for item in tr.unresolved(self.repo, self.rid)], ['unknown'])

    def test_receipt_mapping_never_invents_delivery(self):
        self.decide()
        message_id = self.envelope['message_id']
        self.assertEqual(tr.record_receipt(self.repo, self.rid, message_id, 'receiver_received')['state'], 'received')
        self.assertEqual(tr.record_receipt(self.repo, self.rid, message_id, 'injected')['state'], 'injected')
        self.assertEqual(tr.record_receipt(self.repo, self.rid, message_id, 'rejected')['state'], 'business_rejected')
        with self.assertRaises(tr.TransportReject):
            tr.record_receipt(self.repo, self.rid, message_id, 'acknowledged')

    def test_injection_uncertainty_is_durable_unknown_not_a_rejection(self):
        other = dict(self.envelope, message_id='m-inject-unknown')
        self.decide(other)
        record = tr.record_receipt(self.repo, self.rid, 'm-inject-unknown', 'unknown',
                                  'injection outcome unknown: sendMessage refused')
        self.assertEqual(record['state'], 'unknown')
        self.assertNotEqual(record['state'], 'business_rejected')
        self.assertEqual(record['history'][-1]['detail'], 'injection outcome unknown: sendMessage refused')
        # Never auto-resolves, and cannot be laundered into a known state.
        for state in ('injected', 'business_accepted', 'business_rejected', 'received'):
            with self.subTest(state=state), self.assertRaises(tr.TransportReject):
                tr.transition(self.repo, self.rid, 'm-inject-unknown', state)
        self.assertEqual([item['message_id'] for item in tr.unresolved(self.repo, self.rid)], ['m-inject-unknown'])

    def test_cli_records_unknown_receipt(self):
        self.decide()
        state_file = self.repo / 'state.json'
        ct.atomic(state_file, self.state)
        env = dict(os.environ, PYTHONPATH=str(ROOT / 'core'))
        result = subprocess.run([sys.executable, str(ROOT / 'core/transport_cli.py'), '--repo', str(self.repo),
                                 '--run', self.rid, 'receipt', '--message-id', self.envelope['message_id'],
                                 '--status', 'unknown', '--detail', 'injection outcome unknown'],
                                capture_output=True, text=True, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['state'], 'unknown')
        second = subprocess.run([sys.executable, str(ROOT / 'core/transport_cli.py'), '--repo', str(self.repo),
                                 '--run', self.rid, 'receipt', '--message-id', self.envelope['message_id'],
                                 '--status', 'injected'], capture_output=True, text=True, env=env)
        self.assertNotEqual(second.returncode, 0)

    def test_durable_write_failure_never_reports_inject(self):
        with patch.object(tr, 'save_store', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.decide()
        # Nothing was recorded, so the sender may re-send the same message_id.
        self.assertFalse(tr.dedupe_file(self.repo, self.rid).exists())
        self.assertEqual(self.decide()['decision'], 'inject')

    def test_retention_caps_records_but_keeps_unknown(self):
        with patch.object(tr, 'MAX_RECORDS', 3):
            for index in range(5):
                envelope = dict(self.envelope, message_id='m-%d' % index,
                                body={'text': 'body-%d' % index}, body_sha256=tr.sha256_hex('body-%d' % index))
                self.decide(envelope)
            tr.transition(self.repo, self.rid, 'm-4', 'unknown', 'crash window')
            store = tr.load_store(self.repo, self.rid)
            self.assertLessEqual(len(store['records']), 4)
            self.assertIn('m-4', store['records'])
            self.assertEqual(tr.status(self.repo, self.rid)['states'].get('unknown'), 1)

    def test_cli_receive_and_receipt_round_trip(self):
        state_file = self.repo / 'state.json'
        ct.atomic(state_file, self.state)
        envelope_file = self.repo / 'envelope.json'
        ct.atomic(envelope_file, self.envelope)
        env = dict(os.environ, PYTHONPATH=str(ROOT / 'core'))
        def call(*args):
            return subprocess.run([sys.executable, str(ROOT / 'core/transport_cli.py'), '--repo', str(self.repo),
                                   '--run', self.rid, *args], capture_output=True, text=True, env=env)
        first = call('receive', '--state', str(state_file), '--self', 'worker', '--file', str(envelope_file))
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(json.loads(first.stdout)['decision'], 'inject')
        second = call('receive', '--state', str(state_file), '--self', 'worker', '--file', str(envelope_file))
        self.assertEqual(json.loads(second.stdout)['decision'], 'duplicate')
        rejected = dict(self.envelope, message_id='m-reject', shop_id='other-shop')
        ct.atomic(envelope_file, rejected)
        third = call('receive', '--state', str(state_file), '--self', 'worker', '--file', str(envelope_file))
        payload = json.loads(third.stdout)
        self.assertEqual(payload['decision'], 'reject')
        self.assertEqual(payload['code'], 'E_SHOP_MISMATCH')
        receipt = call('receipt', '--message-id', self.envelope['message_id'], '--status', 'injected')
        self.assertEqual(json.loads(receipt.stdout)['state'], 'injected')
        status = call('status')
        self.assertEqual(json.loads(status.stdout)['records'], 1)
        pending = call('pending')
        self.assertEqual(json.loads(pending.stdout)['unresolved'], [])

    def test_cli_refuses_a_state_file_bound_to_another_run(self):
        state_file = self.repo / 'state.json'
        ct.atomic(state_file, dict(self.state, run_id='other-run'))
        envelope_file = self.repo / 'envelope.json'
        ct.atomic(envelope_file, self.envelope)
        result = subprocess.run([sys.executable, str(ROOT / 'core/transport_cli.py'), '--repo', str(self.repo),
                                 '--run', self.rid, 'receive', '--state', str(state_file), '--self', 'worker',
                                 '--file', str(envelope_file)],
                                capture_output=True, text=True, env=dict(os.environ, PYTHONPATH=str(ROOT / 'core')))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('E_RUN_MISMATCH', result.stderr)

    def test_envelope_fixtures_are_self_consistent(self):
        for path in FIXTURES.glob('*.json'):
            data = json.loads(path.read_text())
            if path.name == 'envelope-unknown-field.json':
                # Deliberate negative fixture: unknown fields must be rejected.
                self.assertTrue(set(data) - set(tr.ENVELOPE_KEYS))
            else:
                self.assertEqual(set(data), set(tr.ENVELOPE_KEYS), path.name)
            if path.name == 'envelope-bad-body-hash.json':
                # Deliberate negative fixture: the hash must NOT match the body.
                self.assertNotEqual(data['body_sha256'], tr.sha256_hex(data['body']['text']))
            else:
                self.assertEqual(data['body_sha256'], tr.sha256_hex(data['body']['text']), path.name)


if __name__ == '__main__':
    unittest.main()
