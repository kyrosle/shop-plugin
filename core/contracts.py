"""File contracts and repository-level guards. Caller holds .shop/.run.lock."""
import datetime as dt
import json
import os
from pathlib import Path
import re
import subprocess
import uuid


def stamp():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def atomic(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name('.' + path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


def load(path):
    p = Path(path)
    if p.is_symlink():
        raise RuntimeError('Symlink refused: ' + str(p))
    return json.loads(p.read_text())


def ident(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,95}', value):
        raise RuntimeError('Invalid identifier')
    return value


def git(cwd, *args):
    p = subprocess.run(['git', '-C', str(cwd), *args], capture_output=True, text=True, timeout=60)
    if p.returncode:
        raise RuntimeError(p.stderr.strip())
    return p.stdout.strip()


def binding_file(repo):
    return Path(repo) / '.shop/bindings.json'


def bindings(repo):
    p = binding_file(repo)
    return load(p) if p.exists() else {}


def require_unbound(repo, rid):
    if rid in bindings(repo):
        raise RuntimeError('Run still bound to workstation; unbind explicitly before finish/cleanup: ' + rid)


def run_path(repo, rid):
    p = Path(repo) / '.shop/runs' / ident(rid)
    if p.is_symlink() or not p.is_dir():
        raise RuntimeError('Run missing or symlink')
    return p


def tickets(repo, rid):
    p = run_path(repo, rid) / 'tickets'
    if p.is_symlink():
        raise RuntimeError('Ticket directory symlink refused')
    current = []
    for f in sorted(p.glob('*.ticket.json')):
        # Retry snapshots are evidence, never live assignments.
        if re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,95}\.a[1-9][0-9]*\.ticket\.json', f.name):
            continue
        tid = f.name[:-len('.ticket.json')]
        ident(tid)
        t = load(f)
        if t.get('ticket_id') != tid or t.get('run_id') != rid:
            raise RuntimeError('Canonical ticket identity mismatch: ' + str(f))
        current.append(t)
    return current


def outstanding(repo, rid, owner=None):
    return [t for t in tickets(repo, rid)
            if t['status'] not in ('accepted', 'cancelled') and (owner is None or t['owner'] == owner)]


def ticket_path(repo, rid, tid):
    return run_path(repo, rid) / 'tickets' / (ident(tid) + '.ticket.json')


def baseline(repo, cwd, base):
    if not isinstance(base, str) or not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', base):
        raise RuntimeError('Development requires exact base commit SHA, not HEAD/branch')
    cwd = str(Path(cwd).resolve(strict=True))
    if Path(git(cwd, 'rev-parse', '--show-toplevel')).resolve() != Path(cwd):
        raise RuntimeError('Worktree root required')
    # Compare canonical git common directories, not names or remote URLs.
    def common(root):
        p = Path(git(root, 'rev-parse', '--git-common-dir'))
        return (Path(root) / p).resolve() if not p.is_absolute() else p.resolve()
    if common(repo) != common(cwd):
        raise RuntimeError('Worktree belongs to another repository')
    if git(cwd, 'rev-parse', 'HEAD') != base:
        raise RuntimeError('Worktree HEAD differs from ticket baseline')
    dirty = git(cwd, 'status', '--porcelain', '--untracked-files=all', '--', '.', ':(exclude).shop')
    if dirty:
        raise RuntimeError('Dirty development worktree. Save baseline with human approval or use read-only analysis; never auto-commit.')
    return cwd


def new_ticket(repo, rid, doc):
    ident(doc.get('ticket_id'))
    meta = load(run_path(repo, rid) / 'run.json')
    if meta['status'] != 'active':
        raise RuntimeError('Run must be active')
    for key in ('owner', 'objective', 'worktree'):
        if not isinstance(doc.get(key), str) or not doc[key].strip():
            raise RuntimeError('Missing ' + key)
    ident(doc['owner'])
    if doc.get('kind') not in ('analysis', 'development'):
        raise RuntimeError('kind must be analysis/development')
    for key in ('scope', 'checks', 'depends_on'):
        if not isinstance(doc.get(key), list) or not all(isinstance(x, str) and x for x in doc[key]):
            raise RuntimeError(key + ' must be string list')
    if not doc['scope']:
        raise RuntimeError('Nonempty scope required')
    for scope in doc['scope']:
        if Path(scope).is_absolute() or '..' in Path(scope).parts or scope.startswith('.git'):
            raise RuntimeError('Scope must be safe worktree-relative path')
    for dep in doc['depends_on']:
        if dep == doc['ticket_id'] or not ticket_path(repo, rid, dep).is_file():
            raise RuntimeError('Dependency must be an existing different ticket')
    p = ticket_path(repo, rid, doc['ticket_id'])
    if p.exists():
        raise RuntimeError('Ticket exists; retry uses new attempt, never overwrite')
    if doc['kind'] == 'development':
        baseline(repo, doc['worktree'], doc.get('base_commit'))
        if not doc['checks']:
            raise RuntimeError('Development ticket requires explicit checks')
    else:
        if not Path(doc['worktree']).is_dir():
            raise RuntimeError('Analysis directory missing')
    t = {k: doc[k] for k in ('ticket_id', 'owner', 'objective', 'worktree', 'kind', 'scope', 'checks', 'depends_on')}
    t.update(run_id=rid, attempt=1, status='ready', base_commit=doc.get('base_commit'), created_at=stamp(),
             spec_revision=load(run_path(repo, rid) / 'run.json').get('spec_revision', 0))
    atomic(p, t)
    return t


def attempt_path(repo, t, suffix):
    return run_path(repo, t['run_id']) / 'tickets' / (t['ticket_id'] + '.a' + str(t['attempt']) + '.' + suffix + '.json')


def publish(repo, rid, tid, doc, checkpoint=False):
    p = ticket_path(repo, rid, tid)
    t = load(p)
    if t['status'] != 'assigned':
        raise RuntimeError('Ticket not assigned; late/duplicate result refused')
    for key in ('run_id', 'ticket_id', 'attempt', 'owner', 'base_commit'):
        if key not in doc or doc.get(key) != t.get(key):
            raise RuntimeError('Stale or mismatched ' + key)
    if checkpoint:
        if not isinstance(doc.get('progress'), str) or not isinstance(doc.get('next_steps'), list):
            raise RuntimeError('Checkpoint requires progress string and next_steps list')
        atomic(attempt_path(repo, t, 'checkpoint'), dict(doc, saved_at=stamp()))
        return {'checkpoint_saved': True}
    if doc.get('status') not in ('completed', 'blocked', 'failed'):
        raise RuntimeError('Invalid result status')
    for key in ('changed_files', 'checks', 'remaining_work'):
        if not isinstance(doc.get(key), list):
            raise RuntimeError('Missing list: ' + key)
    if not isinstance(doc.get('summary'), str) or not doc['summary'].strip():
        raise RuntimeError('Result summary required')
    for path in doc['changed_files']:
        if not isinstance(path, str) or Path(path).is_absolute() or '..' in Path(path).parts:
            raise RuntimeError('Unsafe changed file')
        if not any(path == s or path.startswith(s.rstrip('/') + '/') or s == '.' for s in t['scope']):
            raise RuntimeError('Changed file outside scope')
    if t['kind'] == 'analysis' and doc['changed_files']:
        raise RuntimeError('Analysis must not change code')
    for check in doc['checks']:
        if (not isinstance(check, dict) or not isinstance(check.get('command'), str)
                or type(check.get('exit_code')) is not int or not isinstance(check.get('evidence'), str)):
            raise RuntimeError('Each check requires command, integer exit_code, evidence path')
        evidence = Path(check['evidence'])
        if not evidence.is_absolute() or not evidence.is_file():
            raise RuntimeError('Check evidence must be an existing absolute file')
    if 'result_commit' not in doc:
        raise RuntimeError('result_commit required (null if no commit)')
    result_commit = doc.get('result_commit')
    if t['kind'] == 'development' and doc['status'] == 'completed':
        if result_commit is not None:
            if not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', result_commit):
                raise RuntimeError('Invalid result commit')
            git(t['worktree'], 'cat-file', '-e', result_commit + '^{commit}')
        if set(t['checks']) - {c['command'] for c in doc['checks']}:
            raise RuntimeError('Required checks missing')
        if any(c['exit_code'] != 0 for c in doc['checks']) or doc['remaining_work']:
            raise RuntimeError('Completed development result has failed checks or remaining work')
    result = attempt_path(repo, t, 'result')
    if result.exists():
        raise RuntimeError('Attempt result immutable; use retry')
    atomic(result, dict(doc, saved_at=stamp()))
    t.update(status='review', result_path=str(result), updated_at=stamp())
    atomic(p, t)
    return t


def retry(repo, rid, tid, stopped):
    p = ticket_path(repo, rid, tid)
    t = load(p)
    if t['status'] in ('accepted', 'cancelled') or not stopped:
        raise RuntimeError('Retry requires unfinished ticket and --writer-stopped attestation')
    atomic(attempt_path(repo, t, 'ticket'), t)
    t.update(attempt=t['attempt'] + 1, status='ready', updated_at=stamp())
    t.pop('result_path', None)
    t.pop('dispatch', None)
    atomic(p, t)
    return t


def revise(repo, rid, tid, changes):
    p = ticket_path(repo, rid, tid)
    t = load(p)
    if t['status'] != 'ready':
        raise RuntimeError('Revise only ready ticket; stop old writer and retry first')
    allowed = {'owner', 'objective', 'worktree', 'scope', 'checks', 'base_commit'}
    if not changes or set(changes) - allowed:
        raise RuntimeError('Revise supports owner/objective/worktree/scope/checks/base_commit only')
    updated = dict(t, **changes)
    ident(updated['owner'])
    if not isinstance(updated['objective'], str) or not updated['objective'].strip():
        raise RuntimeError('Nonempty objective required')
    for key in ('scope', 'checks'):
        if not isinstance(updated[key], list) or not all(isinstance(x, str) and x for x in updated[key]):
            raise RuntimeError('Invalid ' + key)
    if not updated['scope'] or any(Path(s).is_absolute() or '..' in Path(s).parts or s.startswith('.git') for s in updated['scope']):
        raise RuntimeError('Invalid scope')
    if updated['kind'] == 'development':
        baseline(repo, updated['worktree'], updated['base_commit'])
        if not updated['checks']:
            raise RuntimeError('Development checks required')
    elif not Path(updated['worktree']).is_dir():
        raise RuntimeError('Analysis source missing')
    updated['updated_at'] = stamp()
    updated['spec_revision'] = load(run_path(repo, rid) / 'run.json').get('spec_revision', 0)
    atomic(p, updated)
    return updated


def cancel(repo, rid, tid, stopped):
    path = ticket_path(repo, rid, tid)
    ticket = load(path)
    if not stopped:
        raise RuntimeError('--writer-stopped required')
    if ticket['status'] == 'accepted':
        raise RuntimeError('Cannot cancel accepted ticket')
    if any(tid in other.get('depends_on', []) and other['status'] not in ('accepted', 'cancelled')
           for other in tickets(repo, rid)):
        raise RuntimeError('Dependent tickets still outstanding; handle them explicitly before cancellation')
    ticket.update(status='cancelled', updated_at=stamp())
    atomic(path, ticket)
    return ticket


def accept(repo, rid, tid, evidence):
    p = ticket_path(repo, rid, tid)
    t = load(p)
    if t['status'] != 'review' or not Path(evidence).is_file():
        raise RuntimeError('Review-state ticket and existing review evidence required')
    expected = attempt_path(repo, t, 'result')
    if Path(t['result_path']) != expected:
        raise RuntimeError('Result path differs from current attempt')
    result = load(expected)
    if any(result.get(k) != t.get(k) for k in ('run_id', 'ticket_id', 'attempt', 'owner', 'base_commit')):
        raise RuntimeError('Result identity/attempt mismatch at acceptance')
    if result['status'] != 'completed':
        raise RuntimeError('Cannot accept blocked/failed result')
    t.update(status='accepted', review_evidence=str(Path(evidence).resolve()), updated_at=stamp())
    atomic(p, t)
    return t
