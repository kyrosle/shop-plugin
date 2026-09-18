import unittest
from unittest.mock import patch
import locking


class LockingTests(unittest.TestCase):
    def test_close_waits_then_acquires_once(self):
        messages=[]
        with patch.object(locking.fcntl,'flock',side_effect=[BlockingIOError(),None]) as flock, patch.object(locking.time,'sleep'):
            locking.acquire(1,30,lambda:messages.append('waiting'))
        self.assertEqual(flock.call_count,2)
        self.assertEqual(messages,['waiting'])
    def test_regular_operation_fails_immediately(self):
        with patch.object(locking.fcntl,'flock',side_effect=BlockingIOError()):
            with self.assertRaisesRegex(RuntimeError,'Another setup'):
                locking.acquire(1)
    def test_close_timeout_does_not_claim_completion(self):
        with patch.object(locking.fcntl,'flock',side_effect=BlockingIOError()), patch.object(locking.time,'monotonic',side_effect=[0,31]):
            with self.assertRaisesRegex(RuntimeError,'close not performed'):
                locking.acquire(1,30)


if __name__=='__main__':unittest.main()
