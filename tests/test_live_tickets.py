import tempfile
from pathlib import Path
import unittest
import contracts as ct


class LiveTicketsTests(unittest.TestCase):
    def test_retry_archive_not_outstanding_and_preserved(self):
        with tempfile.TemporaryDirectory() as repo:
            folder = Path(repo) / '.shop/runs/R1/tickets'
            folder.mkdir(parents=True)
            ticket = dict(run_id='R1', ticket_id='T002', owner='worker', attempt=1, status='review')
            canonical = folder / 'T002.ticket.json'
            ct.atomic(canonical, ticket)
            ct.retry(repo, 'R1', 'T002', True)
            archive = folder / 'T002.a1.ticket.json'
            evidence = archive.read_bytes()
            self.assertEqual(len(ct.tickets(repo, 'R1')), 1)
            self.assertEqual(ct.outstanding(repo, 'R1')[0]['attempt'], 2)
            current = ct.load(canonical)
            current['status'] = 'accepted'
            ct.atomic(canonical, current)
            self.assertEqual(ct.outstanding(repo, 'R1'), [])
            self.assertEqual(archive.read_bytes(), evidence)

    def test_identity_mismatch_fails_closed(self):
        with tempfile.TemporaryDirectory() as repo:
            folder = Path(repo) / '.shop/runs/R1/tickets'
            folder.mkdir(parents=True)
            ct.atomic(folder / 'T1.ticket.json', dict(run_id='R1', ticket_id='T2', status='accepted'))
            with self.assertRaisesRegex(RuntimeError, 'identity mismatch'):
                ct.tickets(repo, 'R1')


if __name__ == '__main__': unittest.main()
