"""Local Unix process evidence, not a process controller or an OS sandbox.

Read metadata only (never argv/environ). Bind observations to the actual Unix
socket peer, then inspect shell descendants, session, tty and foreground group.
A fully detached/reparented external service is outside that observable pane
scope; this probe does not certify all repository writers or finish any run.
"""
import errno
import os
from pathlib import Path
import socket
import stat
import struct
import subprocess
import sys
import time

MAX_TABLE_BYTES = 8 * 1024 * 1024
MAX_PROCESSES = 50000


class ProcessUncertain(RuntimeError):
    pass


def local_server_pid():
    """Kernel peer identity, not a process name or an assumed local PID namespace."""
    if sys.platform not in ('darwin', 'linux'):
        raise ProcessUncertain('local process inspection unsupported on this platform')
    path = os.environ.get('HERDR_SOCKET_PATH', '')
    if not path or not os.path.isabs(path):
        raise ProcessUncertain('explicit local HERDR_SOCKET_PATH required')
    before = os.lstat(path)
    if not stat.S_ISSOCK(before.st_mode) or before.st_uid != os.geteuid():
        raise ProcessUncertain('Herdr endpoint is not an owned Unix socket')
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(2)
        client.connect(path)
        if sys.platform == 'darwin':
            # Darwin sys/un.h: SOL_LOCAL=0, LOCAL_PEERPID=2.
            pid = client.getsockopt(0, 2)
        else:
            pid, uid, _gid = struct.unpack('3i', client.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            if uid != os.geteuid():
                raise ProcessUncertain('Herdr peer has another owner')
    after = os.lstat(path)
    if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino) or pid <= 1:
        raise ProcessUncertain('Herdr socket identity changed')
    return pid


def parse_table(text, session_id=None):
    """Strict ps metadata parser; command is executable/title, not command args."""
    session_id = session_id or getattr(os, 'getsid', None)
    if session_id is None:
        raise ProcessUncertain('POSIX session inspection unavailable')
    if len(text.encode()) > MAX_TABLE_BYTES:
        raise ProcessUncertain('process table too large')
    rows = {}
    for line in text.splitlines():
        parts = line.split(None, 10)
        if len(parts) != 11:
            raise ProcessUncertain('malformed process metadata')
        try:
            pid, ppid, pgid, uid = map(int, parts[:4])
        except ValueError:
            raise ProcessUncertain('non-numeric process identity') from None
        if pid <= 0 or ppid < 0 or pgid < 0 or uid < 0 or pid in rows:
            raise ProcessUncertain('invalid or duplicate process identity')
        try:
            sid = session_id(pid)
        except OSError as error:
            if error.errno != errno.ESRCH:
                raise ProcessUncertain('cannot inspect process session') from error
            # Keep the vanished row's parent edge: a live descendant must not
            # disappear from our tree merely because its parent just exited.
            sid = None
        rows[pid] = {'pid': pid, 'ppid': ppid, 'pgid': pgid, 'uid': uid,
                     'tty': parts[4], 'start': ' '.join(parts[5:10]),
                     'comm': parts[10], 'sid': sid}
        if len(rows) > MAX_PROCESSES:
            raise ProcessUncertain('too many processes')
    if not rows:
        raise ProcessUncertain('empty process table')
    return rows


def snapshot():
    # Linux proc visibility (e.g. hidepid / namespaces) needs a separate real-host
    # gate before absence can be accepted as evidence. Do not silently approve it.
    if sys.platform != 'darwin':
        raise ProcessUncertain('local process snapshots currently validated only on macOS')
    # No PATH lookup, shell, credentials, command arguments or environment dump.
    result = subprocess.run(['/bin/ps', '-axo', 'pid=,ppid=,pgid=,uid=,tty=,lstart=,comm='],
                            capture_output=True, text=True, timeout=3,
                            env={'PATH': '/usr/bin:/bin', 'LC_ALL': 'C'})
    if result.returncode:
        raise ProcessUncertain('process metadata query failed')
    return parse_table(result.stdout)


def pane_scope(table, info, pi_pid, server_pid):
    """Only exempt a directly attached, recognized Pi; other processes block."""
    shell_pid = info.get('shell_pid')
    group = info.get('foreground_process_group_id')
    shell, pi, server = (table.get(pid) for pid in (shell_pid, pi_pid, server_pid))
    if not shell or not pi or not server:
        raise ProcessUncertain('shell, Pi or server disappeared')
    if any(row['uid'] != os.geteuid() for row in (shell, pi, server)):
        raise ProcessUncertain('process ownership mismatch')
    if shell['ppid'] != server_pid or shell['sid'] != shell_pid:
        raise ProcessUncertain('shell is not a session owned by this Herdr server')
    if shell['tty'] in ('?', '??', '-') or pi['tty'] != shell['tty']:
        raise ProcessUncertain('pane terminal is unavailable or mismatched')
    if info.get('tty') and str(info['tty']).removeprefix('/dev/') != shell['tty']:
        raise ProcessUncertain('Herdr/local terminal mismatch')
    if pi['sid'] != shell_pid or pi['pgid'] != group:
        raise ProcessUncertain('Pi session/foreground group mismatch')
    if pi_pid != shell_pid and pi['ppid'] != shell_pid:
        raise ProcessUncertain('Pi is not the direct shell child')
    if Path(pi['comm']).name not in ('pi', 'node', 'bun'):
        raise ProcessUncertain('foreground executable is not a Pi runtime')
    if pi_pid != shell_pid and Path(shell['comm']).name.lstrip('-') not in ('bash', 'zsh', 'sh', 'fish', 'dash', 'ksh'):
        raise ProcessUncertain('unrecognized pane shell')
    children = {}
    for row in table.values():
        children.setdefault(row['ppid'], []).append(row['pid'])
    descendants, pending = set(), [shell_pid]
    while pending:
        pid = pending.pop()
        if pid in descendants:
            continue
        descendants.add(pid)
        pending.extend(children.get(pid, []))
    related = {pid: row for pid, row in table.items()
               if pid in descendants or row['sid'] == shell_pid
               or row['tty'] == shell['tty'] or row['pgid'] == group}
    return {'anchor': {str(pid): table[pid] for pid in (server_pid, shell_pid, pi_pid)},
            'related': {str(pid): related[pid] for pid in sorted(related)},
            'extra': [related[pid] for pid in sorted(related) if pid not in (shell_pid, pi_pid)]}


def wait_exited(anchors, timeout=5):
    """Verify original process instances exited; never signal a discovered PID.

    Reparenting, exec, tty loss or a new process group are NOT exit evidence.
    Only absence/ESRCH or a different birth stamp proves the old instance gone.
    """
    if not anchors:
        raise ProcessUncertain('missing post-close process identities')
    deadline = time.monotonic() + timeout
    while True:
        table = snapshot()
        if all(pid not in table or table[pid]['sid'] is None
               or table[pid]['start'] != old['start'] for pid, old in anchors.items()):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)
