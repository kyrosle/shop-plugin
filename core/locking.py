"""Bounded lock acquisition; teardown rechecks state only after acquiring lock."""
import fcntl
import time


def acquire(lock, wait_seconds=0, on_wait=None):
    deadline = time.monotonic() + wait_seconds
    notified = False
    while True:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            if time.monotonic() >= deadline:
                raise RuntimeError('Shop operation still running; close not performed. Wait for startup to finish, then retry close.' if wait_seconds else 'Another setup operation is active for this tab.')
            if not notified and on_wait:
                on_wait()
                notified = True
            time.sleep(min(0.2, max(0, deadline - time.monotonic())))
