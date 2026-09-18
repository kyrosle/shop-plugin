#!/usr/bin/python3
"""Explicit repository run lifecycle and conservative retention; no agents or worktree deletion."""
from language import ArgumentParser, t
import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import uuid
import contracts as ct

KEEP = 5
DAYS = 7
DETAILS = ('tickets', 'evidence')


def now():
    return dt.datetime.now(dt.timezone.utc)


def atomic(path, value):
    tmp = path.with_name('.' + path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


def safe_tree(path):
    # Never follow links, nested repositories/worktrees, or special filesystem objects.
    if path.is_symlink():
        raise RuntimeError('Refuse symlink: ' + str(path))
    if path.name == '.git':
        raise RuntimeError('Refuse nested repository: ' + str(path))
    if path.is_dir():
        for child in path.iterdir():
            safe_tree(child)
    elif not path.is_file():
        raise RuntimeError('Refuse special/missing file: ' + str(path))


def size(path):
    return sum(p.stat().st_size for p in path.rglob('*') if p.is_file()) if path.is_dir() else path.stat().st_size


class Runs:
    def __init__(self, repo):
        self.repo = Path(repo).resolve()
        self.shop = self.repo / '.shop'
        self.runs = self.shop / 'runs'
        for p in (self.shop, self.runs):
            if p.is_symlink():
                raise RuntimeError('Refuse symlink: ' + str(p))
            p.mkdir(exist_ok=True)

    def path(self, rid):
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,95}', rid):
            raise RuntimeError('Invalid run ID')
        p = self.runs / rid
        if not p.is_dir() or p.is_symlink():
            raise RuntimeError('Run missing or symlink: ' + rid)
        return p

    def meta(self, rid):
        p = self.path(rid) / 'run.json'
        if not p.exists():
            return {'id': rid, 'status': 'unmanaged'}
        if p.is_symlink():
            raise RuntimeError('Refuse symlink metadata')
        m = json.loads(p.read_text())
        if m.get('id') != rid or m.get('status') not in ('active', 'blocked', 'completed'):
            raise RuntimeError('Invalid metadata: ' + rid)
        return m

    def current(self):
        p = self.shop / 'current.json'
        if p.is_symlink():
            raise RuntimeError('Refuse symlink current pointer')
        if not p.exists():
            return None
        rid = json.loads(p.read_text())['run_id']
        self.path(rid)
        return rid

    def listing(self):
        return [self.meta(p.name) for p in sorted(self.runs.iterdir()) if p.is_dir() and not p.is_symlink()]

    def use(self, rid):
        m = self.meta(rid)
        if m['status'] not in ('active', 'blocked'):
            raise RuntimeError('Only managed unfinished runs can be selected')
        atomic(self.shop / 'current.json', {'run_id': rid})

    def new(self, title, independent=False):
        if not independent and self.current():
            raise RuntimeError('Current run exists. Use new --independent to preserve it and create a separate run, or explicitly switch with use.')
        rid = now().strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:6]
        p = self.runs / rid
        p.mkdir()
        for folder in DETAILS:
            (p / folder).mkdir()
        atomic(p / 'run.json', {'id': rid, 'title': title, 'status': 'active', 'created_at': now().isoformat()})
        for name in ('SPEC', 'PLAN', 'STATUS'):
            (p / (name + '.md')).write_text('# ' + name + '\n\n' + title + '\n')
        if not independent:
            self.use(rid)
        return {'created': rid, 'path': str(p), 'selected': not independent,
                'note': 'Bind created ID explicitly; independent creation never changes current.json or existing bindings.'}

    def adopt(self, rid):
        p = self.path(rid)
        if self.meta(rid)['status'] != 'unmanaged':
            raise RuntimeError('Already managed')
        atomic(p / 'run.json', {'id': rid, 'title': rid, 'status': 'active', 'created_at': now().isoformat()})
        return {'adopted': rid, 'status': 'active', 'selected': False}

    def finish(self, rid, confirmed):
        m = self.meta(rid)
        if m['status'] not in ('active', 'blocked'):
            raise RuntimeError('Only managed unfinished run can finish')
        if not confirmed:
            raise RuntimeError('Require --handoff-complete: all writers stopped, results accepted, no waiting dependents')
        ct.require_unbound(self.repo, rid)
        if ct.outstanding(self.repo, rid):
            raise RuntimeError('Unaccepted tickets remain; finish refused')
        p = self.path(rid)
        for name in ('SUMMARY.md', 'REVIEW.md'):
            f = p / name
            if f.is_symlink() or not f.is_file() or not f.read_text().strip():
                raise RuntimeError('Require nonempty ' + name)
        m.update(status='completed', completed_at=now().isoformat())
        atomic(p / 'run.json', m)
        if self.current() == rid:
            (self.shop / 'current.json').unlink()
        return {'finished': rid}

    def gc_plan(self):
        completed = [m for m in self.listing() if m['status'] == 'completed']
        completed.sort(key=lambda m: dt.datetime.fromisoformat(m['completed_at']), reverse=True)
        current = self.current()
        entries = []
        for rank, m in enumerate(completed):
            rid = m['id']
            age = (now() - dt.datetime.fromisoformat(m['completed_at'])).total_seconds()
            reason = ('bound-to-workstation' if rid in ct.bindings(self.repo) else 'current' if rid == current else 'newest-five' if rank < KEEP
                      else 'younger-than-seven-days' if age <= DAYS * 86400 else None)
            p = self.path(rid)
            targets = [p / name for name in DETAILS if (p / name).exists() or (p / name).is_symlink()]
            if not reason:
                for summary in ('SUMMARY.md', 'REVIEW.md'):
                    f = p / summary
                    if f.is_symlink() or not f.is_file() or not f.read_text().strip():
                        reason = 'missing-summary-or-review'
                if not reason:
                    for target in targets:
                        safe_tree(target)
            entries.append({'id': rid, 'reason': reason, 'paths': [str(t) for t in targets] if not reason else [],
                            'bytes': sum(size(t) for t in targets) if not reason else 0})
        return entries

    def gc(self, apply=False):
        plan = self.gc_plan()  # Preflight entire batch before first deletion.
        if apply:
            for row in plan:
                for name in row['paths']:
                    p = Path(name)
                    safe_tree(p)
                    shutil.rmtree(p) if p.is_dir() else p.unlink()
        return {'apply': apply, 'keep_latest': KEEP, 'older_than_days': DAYS, 'plan': plan,
                'note': 'Only tickets/evidence removed; summaries and metadata preserved.'}

    def delete(self, rid, apply=False, confirm=None):
        if self.meta(rid)['status'] != 'completed' or self.current() == rid:
            raise RuntimeError('Only completed, non-current managed runs may be deleted')
        ct.require_unbound(self.repo, rid)
        p = self.path(rid)
        safe_tree(p)
        result = {'id': rid, 'path': str(p), 'bytes': size(p), 'apply': apply}
        if apply:
            if confirm != rid:
                raise RuntimeError('Require --confirm ' + rid + ' for irreversible full run deletion')
            shutil.rmtree(p)
        return result


def authorize_ticket(repo, rid, tid, op):
    binding = ct.bindings(repo).get(rid)
    if not binding:
        raise RuntimeError('Ticket mutations require a bound workstation')
    import shop
    pane_id = os.environ.get('HERDR_ACTIVE_PANE_ID') or os.environ.get('HERDR_PANE_ID')
    if not pane_id:
        raise RuntimeError('Herdr caller identity required')
    agent = shop.api('agent', 'get', pane_id)['agent']
    if agent.get('agent') != 'pi' or agent.get('tab_id') != binding['tab']:
        raise RuntimeError('Wrong workstation caller')
    if op in ('publish', 'checkpoint'):
        t = ct.load(ct.ticket_path(repo, rid, tid))
        dispatch = t.get('dispatch', {})
        if (agent.get('name') != t['owner'] or pane_id != dispatch.get('pane')
                or agent.get('terminal_id') != dispatch.get('terminal_id')):
            raise RuntimeError('Result writer differs from dispatched occupant')
    elif pane_id != binding['lead']['pane'] or agent.get('name') != binding['lead']['name']:
        raise RuntimeError('Primary Lead is sole ticket coordinator')
    if op in ('retry', 'cancel'):
        t = ct.load(ct.ticket_path(repo, rid, tid))
        dispatch = t.get('dispatch')
        if dispatch:
            layout = shop.api('pane', 'layout', '--pane', pane_id)['layout']
            if dispatch['pane'] in {p['pane_id'] for p in layout['panes']}:
                pane = shop.api('pane', 'get', dispatch['pane'])['pane']
                if pane.get('agent'):
                    # Pane metadata intentionally omits registered agent identity. Query
                    # the agent endpoint before attesting that the old writer stopped.
                    old = shop.api('agent', 'get', dispatch['pane'])['agent']
                    if (old.get('name') != t['owner']
                            or old.get('terminal_id') != dispatch.get('terminal_id')
                            or old.get('agent_status') not in ('idle', 'done')):
                        raise RuntimeError('Old writer still working/blocked/unknown or changed; stop and inspect first')
                else:
                    proc = shop.api('pane', 'process-info', '--pane', dispatch['pane'])['process_info']
                    if not proc.get('foreground_processes') or any(x['pid'] != proc['shell_pid'] for x in proc['foreground_processes']):
                        raise RuntimeError('Old writer pane has unknown foreground work')
            else:
                # Missing from this tab could mean moved, not dead; explicit inspection required.
                for other in shop.api('agent', 'list')['agents']:
                    if other.get('name') == t['owner']:
                        raise RuntimeError('Old writer moved to another pane/tab; inspect before retry')


def main():
    parser = ArgumentParser(description=t(__doc__))
    parser.add_argument('--repo', help=t('Explicit project root, especially from auxiliary worktree'))
    sub = parser.add_subparsers(dest='action', required=True)
    new_parser = sub.add_parser('new')
    new_parser.add_argument('title')
    new_parser.add_argument('--independent', action='store_true',
                            help=t('Create separate run without reading/changing current.json or existing runs/bindings'))
    for name in ('list', 'status'):
        sub.add_parser(name)
    for name in ('use', 'adopt', 'finish', 'block', 'resume', 'delete'):
        p = sub.add_parser(name)
        p.add_argument('id')
        if name == 'finish':
            p.add_argument('--handoff-complete', action='store_true')
        if name == 'delete':
            g = p.add_mutually_exclusive_group()
            g.add_argument('--apply', action='store_true')
            g.add_argument('--dry-run', action='store_true')
            p.add_argument('--confirm')
    p = sub.add_parser('gc')
    g = p.add_mutually_exclusive_group()
    g.add_argument('--apply', action='store_true')
    g.add_argument('--dry-run', action='store_true')
    p = sub.add_parser('ticket')
    p.add_argument('operation', choices=['new', 'publish', 'checkpoint', 'retry', 'accept', 'cancel', 'list', 'revise'])
    p.add_argument('id', help=t('Explicit run ID; never silently follow current'))
    p.add_argument('ticket_id', nargs='?')
    p.add_argument('--file', help=t('JSON draft, or review evidence for accept'))
    p.add_argument('--writer-stopped', action='store_true')
    args = parser.parse_args()
    repo = args.repo or subprocess.check_output(['git', 'rev-parse', '--show-toplevel'], text=True).strip()
    runs = Runs(repo)
    lock_path = runs.shop / '.run.lock'
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.action == 'ticket':
            op = args.operation
            if op == 'list':
                result = ct.tickets(repo, args.id)
            else:
                authorize_ticket(repo, args.id, args.ticket_id, op)
                if op in ('new', 'publish', 'checkpoint', 'accept', 'revise') and not args.file:
                    raise RuntimeError('--file required')
                if op == 'new':
                    result = ct.new_ticket(repo, args.id, ct.load(args.file))
                elif not args.ticket_id:
                    raise RuntimeError('ticket_id required')
                elif op in ('publish', 'checkpoint'):
                    result = ct.publish(repo, args.id, args.ticket_id, ct.load(args.file), op == 'checkpoint')
                elif op == 'revise':
                    result = ct.revise(repo, args.id, args.ticket_id, ct.load(args.file))
                elif op == 'retry':
                    result = ct.retry(repo, args.id, args.ticket_id, args.writer_stopped)
                elif op == 'accept':
                    result = ct.accept(repo, args.id, args.ticket_id, args.file)
                else:
                    result = ct.cancel(repo, args.id, args.ticket_id, args.writer_stopped)
        elif args.action == 'new':
            result = runs.new(args.title, args.independent)
        elif args.action in ('list', 'status'):
            result = {'current': runs.current(), 'runs': runs.listing(), 'cleanup': runs.gc_plan(),
                      'legacy_top_level': [p.name for p in runs.shop.glob('*.md')],
                      'note': 'Unmanaged/top-level records never auto-cleaned; no agent is notified.'}
        elif args.action == 'use':
            runs.use(args.id)
            result = {'current': args.id}
        elif args.action == 'adopt':
            result = runs.adopt(args.id)
        elif args.action == 'finish':
            result = runs.finish(args.id, args.handoff_complete)
        elif args.action in ('block', 'resume'):
            m = runs.meta(args.id)
            if m['status'] not in ('active', 'blocked'):
                raise RuntimeError('Only unfinished managed run can change state')
            m['status'] = 'blocked' if args.action == 'block' else 'active'
            atomic(runs.path(args.id) / 'run.json', m)
            result = m
        elif args.action == 'gc':
            result = runs.gc(args.apply)
        else:
            result = runs.delete(args.id, args.apply, args.confirm)
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        print('shop-run: ' + str(e), file=sys.stderr)
        sys.exit(1)
