import importlib.util
import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest

s = importlib.util.spec_from_file_location('run', Path(__file__).resolve().parents[1] / 'core/run.py')
m = importlib.util.module_from_spec(s)
s.loader.exec_module(m)


class RunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.r = m.Runs(self.tmp.name)

    def complete(self, rid, days=10):
        p = self.r.runs / rid
        p.mkdir()
        for name in ('tickets', 'evidence'):
            (p / name).mkdir()
            (p / name / 'data.txt').write_text('evidence')
        for name in ('SUMMARY.md', 'REVIEW.md', 'SPEC.md'):
            (p / name).write_text('report')
        m.atomic(p / 'run.json', {'id': rid, 'status': 'completed',
                 'completed_at': (m.now() - dt.timedelta(days=days)).isoformat()})
        return p

    def test_new_finish_requires_evidence_and_handoff(self):
        rid = self.r.new('test')['created']
        self.assertEqual(self.r.current(), rid)
        with self.assertRaises(RuntimeError): self.r.new('second')
        with self.assertRaises(RuntimeError): self.r.finish(rid, True)
        p = self.r.path(rid)
        for name in ('SUMMARY.md', 'REVIEW.md'): (p / name).write_text('done')
        with self.assertRaises(RuntimeError): self.r.finish(rid, False)
        self.r.finish(rid, True)
        self.assertIsNone(self.r.current())
        self.assertEqual(self.r.meta(rid)['status'], 'completed')

    def test_independent_preserves_current_old_run_and_bindings(self):
        old = self.r.new('old')['created']
        pointer = (self.r.shop / 'current.json').read_bytes()
        meta = (self.r.path(old) / 'run.json').read_bytes()
        bindings = self.r.shop / 'bindings.json'
        bindings.write_text(json.dumps({old: {'shop_id': 'other-shop'}}))
        registry = bindings.read_bytes()
        result = self.r.new('separate task', independent=True)
        self.assertNotEqual(result['created'], old)
        self.assertFalse(result['selected'])
        self.assertEqual((self.r.shop / 'current.json').read_bytes(), pointer)
        self.assertEqual((self.r.path(old) / 'run.json').read_bytes(), meta)
        self.assertEqual(bindings.read_bytes(), registry)
        self.assertEqual(self.r.meta(result['created'])['status'], 'active')

    def test_independent_does_not_read_stale_pointer_or_select_when_absent(self):
        result = self.r.new('fresh', independent=True)
        self.assertFalse((self.r.shop / 'current.json').exists())
        (self.r.shop / 'current.json').write_text('{invalid legacy pointer')
        again = self.r.new('new independent', independent=True)
        self.assertNotEqual(result['created'], again['created'])
        self.assertEqual((self.r.shop / 'current.json').read_text(), '{invalid legacy pointer')

    def test_independent_cli(self):
        import subprocess
        import sys
        old = self.r.new('old')['created']
        result = subprocess.run([sys.executable, str(Path(m.__file__)), '--repo', str(self.r.repo),
                                 'new', '--independent', 'CLI task'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(json.loads(result.stdout)['selected'])
        self.assertEqual(self.r.current(), old)

    def test_gc_keep_five_age_and_summary(self):
        paths = [self.complete('r' + str(i), days=i+10) for i in range(7)]
        self.r.gc(False)
        self.assertTrue(all((p/'tickets').exists() for p in paths))
        self.r.gc(True)
        for i,p in enumerate(paths):
            self.assertEqual((p/'tickets').exists(), i < 5)
            self.assertTrue((p/'SUMMARY.md').exists())
            self.assertTrue((p/'SPEC.md').exists())
        self.r.gc(True)  # Idempotent

    def test_age_prevents_gc(self):
        for i in range(7): self.complete('r'+str(i), days=1)
        self.assertTrue(all(row['reason'] for row in self.r.gc_plan()))

    def test_active_blocked_unmanaged_excluded(self):
        rid = self.r.new('active')['created']
        self.r.meta(rid)
        (self.r.runs/'legacy').mkdir()
        self.assertEqual(self.r.gc_plan(), [])
        with self.assertRaises(RuntimeError): self.r.delete(rid, True, rid)
        with self.assertRaises(RuntimeError): self.r.delete('legacy', True, 'legacy')
        self.r.adopt('legacy')
        self.assertEqual(self.r.meta('legacy')['status'], 'active')

    def test_delete_needs_exact_confirmation(self):
        p = self.complete('old')
        self.r.delete('old')
        self.assertTrue(p.exists())
        with self.assertRaises(RuntimeError): self.r.delete('old', True, 'wrong')
        self.r.delete('old', True, 'old')
        self.assertFalse(p.exists())

    def test_traversal_and_symlink_refused(self):
        with self.assertRaises(RuntimeError): self.r.path('../outside')
        p = self.complete('old')
        outside = Path(self.tmp.name)/'outside'; outside.write_text('keep')
        (p/'tickets'/'link').symlink_to(outside)
        with self.assertRaises(RuntimeError): self.r.delete('old', True, 'old')
        self.assertEqual(outside.read_text(), 'keep')

    def test_nested_worktree_refused(self):
        p = self.complete('old')
        (p/'.git').write_text('gitdir: elsewhere')
        with self.assertRaises(RuntimeError): self.r.delete('old', True, 'old')

    def test_batch_preflight_no_partial_deletion(self):
        ps = [self.complete('r'+str(i), i+10) for i in range(7)]
        (ps[-1]/'evidence'/'link').symlink_to('/tmp')
        with self.assertRaises(RuntimeError): self.r.gc(True)
        self.assertTrue((ps[-2]/'tickets').exists())


if __name__ == '__main__': unittest.main()
