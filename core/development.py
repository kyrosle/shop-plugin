"""Development preparation and explicit, fast-forward-only integration plans.

Plans are pinned to SHA, identities and clean Git state, then rechecked at apply.
Failure preserves partial work. Never stash/reset/clean/remove a worktree.
"""
import hashlib
from pathlib import Path
import time

import contracts as ct


def git(repo, *args):
    return ct.git(repo, *map(str, args))


def baseline(repo):
    root = Path(git(repo, 'rev-parse', '--show-toplevel')).resolve()
    common = Path(git(repo, 'rev-parse', '--path-format=absolute', '--git-common-dir')).resolve()
    return {'repo': str(root), 'common': str(common), 'head': git(repo, 'rev-parse', 'HEAD'),
            'branch': git(repo, 'symbolic-ref', '--quiet', 'HEAD'),
            'dirty': git(repo, 'status', '--porcelain', '--untracked-files=all', '--', '.', ':(exclude).shop'),
            'worktrees': git(repo, 'worktree', 'list', '--porcelain')}


def require_isolated_path(workbench, path):
    roots = [Path(row[9:]).resolve() for row in git(workbench.repo, 'worktree', 'list', '--porcelain', '-z').split('\0')
             if row.startswith('worktree ')]
    roots.extend(Path(member.get('cwd', workbench.repo)).resolve()
                 for _, member in __import__('identity').roster(workbench.state))
    if any(path == root or root in path.parents for root in roots):
        raise RuntimeError('Destination overlaps an existing/reserved worktree; choose an isolated sibling path')


def preview(workbench, data):
    workbench.authorize(controller=True)
    facts = baseline(workbench.repo)
    if facts['dirty']:
        raise RuntimeError('Source worktree dirty; no stash/reset. Commit or choose a clean worktree explicitly')
    base = git(workbench.repo, 'rev-parse', '--verify', data.get('base', 'HEAD') + '^{commit}')
    branch = data['branch']
    if not isinstance(branch, str) or branch.startswith('-') or len(branch) > 160:
        raise RuntimeError('Invalid new branch')
    git(workbench.repo, 'check-ref-format', 'refs/heads/' + branch)
    if git(workbench.repo, 'for-each-ref', '--format=%(refname)', 'refs/heads/' + branch):
        raise RuntimeError('Branch already exists; choose a new branch')
    requested_path = Path(data['path']).expanduser()
    if not requested_path.is_absolute():
        raise RuntimeError('Absolute destination path required')
    if requested_path.exists() or requested_path.is_symlink():
        raise RuntimeError('Destination already exists; never reuse user state')
    path = requested_path.resolve()
    require_isolated_path(workbench, path)
    payload = {'facts': facts, 'path': str(path), 'branch': branch, 'base_commit': base,
               'expires_at': time.time() + 300}
    return workbench.save_request('development-plan', payload)


def apply(workbench, data):
    workbench.authorize(controller=True)
    record = workbench.load_request(data['id'])
    if record['kind'] != 'development-plan' or record['status'] != 'requested':
        raise RuntimeError('No unused development plan')
    plan = record['payload']
    if plan['expires_at'] < time.time() or baseline(workbench.repo) != plan['facts']:
        raise RuntimeError('Plan stale: repository changed or preview expired')
    if Path(plan['path']).exists() or Path(plan['path']).is_symlink():
        raise RuntimeError('Destination appeared since preview')
    if Path(plan['path']).resolve() != Path(plan['path']):
        raise RuntimeError('Destination parent path changed since preview')
    require_isolated_path(workbench, Path(plan['path']))
    if git(workbench.repo, 'for-each-ref', '--format=%(refname)', 'refs/heads/' + plan['branch']):
        raise RuntimeError('Branch appeared since preview')
    workbench.update(record, 'applying')
    try:
        git(workbench.repo, 'worktree', 'add', '-b', plan['branch'], '--', plan['path'], plan['base_commit'])
        actual = baseline(plan['path'])
        if actual['head'] != plan['base_commit'] or actual['dirty'] or actual['common'] != plan['facts']['common']:
            raise RuntimeError('Created worktree did not match preview; inspect retained files')
    except Exception as error:
        workbench.update(record, 'unknown', detail=str(error)[:2000])
        raise
    return workbench.update(record, 'created',
                            next_step='Explicitly add an execution seat using this --cwd; then create and dispatch a ticket')


def delivery(workbench):
    tickets = ct.tickets(workbench.repo, workbench.rid)
    rows = []
    for ticket in tickets:
        path = ct.attempt_path(workbench.repo, ticket, 'result')
        result = ct.load(path) if path.exists() and ticket.get('result_path') == str(path) else {}
        if result and any(result.get(key) != ticket.get(key) for key in ('run_id', 'ticket_id', 'attempt', 'owner', 'base_commit')):
            result = {}
        rows.append({'ticket_id': ticket['ticket_id'], 'attempt': ticket['attempt'], 'status': ticket['status'],
                     'owner': ticket['owner'], 'worktree': ticket.get('worktree'), 'base_commit': ticket.get('base_commit'),
                     'result_commit': result.get('result_commit'), 'summary': result.get('summary'),
                     'checks': result.get('checks', []), 'required_checks': ticket.get('checks', []),
                     'remaining_work': result.get('remaining_work', []),
                     'result_path': str(path) if result else None,
                     'note': 'Recorded checks are author reports, not independent verification'})
    try:
        head = git(workbench.repo, 'rev-parse', 'HEAD')
    except RuntimeError:
        head = None
    final_checks = []
    for record in workbench.records():
        if record['kind'] != 'final-check':
            continue
        payload = record['payload']
        evidence = Path(payload['evidence'])
        valid = False
        try:
            valid = (payload['target_commit'] == head and not evidence.is_symlink()
                     and evidence.stat().st_size <= 1024 * 1024
                     and hashlib.sha256(evidence.read_bytes()).hexdigest() == payload['sha256'])
        except OSError:
            pass
        final_checks.append({**payload, 'current': valid, 'provenance': 'user-attested; not executed by plugin'})
    return {'run_id': workbench.rid, 'tickets': rows, 'target_commit': head, 'final_checks': final_checks,
            'integration_eligible': any(row['status'] == 'accepted' for row in rows)
                and all(row['status'] in ('accepted', 'cancelled') for row in rows),
            'note': 'Task acceptance, Git integration and run closure are separate decisions.'}


def final_check(workbench, data):
    workbench.authorize(controller=True)
    head = git(workbench.repo, 'rev-parse', 'HEAD')
    if data.get('target_commit') != head:
        raise RuntimeError('Final check target commit changed; old evidence cannot certify new HEAD')
    if type(data.get('exit_code')) is not int or not isinstance(data.get('command'), str) or not data['command'].strip():
        raise RuntimeError('Command and integer exit_code required')
    evidence = Path(data['evidence']).expanduser()
    if not evidence.is_absolute() or evidence.is_symlink() or not evidence.is_file() or evidence.stat().st_size > 1024 * 1024:
        raise RuntimeError('Evidence must be an existing absolute regular file, at most 1 MiB')
    payload = {'target_commit': head, 'command': data['command'][:2000], 'exit_code': data['exit_code'],
               'evidence': str(evidence.resolve()), 'sha256': hashlib.sha256(evidence.read_bytes()).hexdigest()}
    return workbench.save_request('final-check', payload)


def integration_preview(workbench, data):
    workbench.authorize(controller=True)
    report = delivery(workbench)
    if not report['integration_eligible']:
        raise RuntimeError('Accept all tickets before integration')
    selected = next((row for row in report['tickets'] if row['ticket_id'] == data['ticket']), None)
    if not selected or selected['status'] != 'accepted' or not selected['result_commit']:
        raise RuntimeError('Selected ticket has no accepted result commit')
    facts = baseline(workbench.repo)
    if facts['dirty']:
        raise RuntimeError('Integration target dirty; no stash/reset')
    commit = git(workbench.repo, 'rev-parse', '--verify', selected['result_commit'] + '^{commit}')
    git(workbench.repo, 'merge-base', '--is-ancestor', facts['head'], commit)
    return workbench.save_request('integration-plan', {
        'facts': facts, 'commit': commit, 'delivery': report, 'expires_at': time.time() + 300,
        'strategy': 'ff-only', 'operation': ['git', '-C', workbench.repo, 'merge', '--ff-only', commit]})


def integrate(workbench, data):
    workbench.authorize(controller=True)
    record = workbench.load_request(data['id'])
    if record['kind'] != 'integration-plan' or record['status'] != 'requested':
        raise RuntimeError('No unused integration plan')
    plan = record['payload']
    if data.get('writers_stopped') is not True:
        raise RuntimeError('Explicit foreground/background writer-stop attestation required')
    for _, member in __import__('identity').roster(workbench.state):
        if member['name'] != workbench.actor['name']:
            workbench.idle_seat(member)
    if (plan['expires_at'] < time.time() or baseline(workbench.repo) != plan['facts']
            or delivery(workbench) != plan['delivery']):
        raise RuntimeError('Plan stale; Git or ticket facts changed')
    workbench.update(record, 'applying', writer_stop_attested_by=workbench.actor['name'])
    try:
        git(workbench.repo, 'merge', '--ff-only', plan['commit'])
        after = baseline(workbench.repo)
        if after['head'] != plan['commit'] or after['dirty']:
            raise RuntimeError('Post-integration facts differ; inspect without automatic rollback')
    except Exception as error:
        workbench.update(record, 'unknown', detail=str(error)[:2000])
        raise
    return workbench.update(record, 'integrated', result_commit=after['head'],
                            next_step='Review evidence, explicitly unbind, then shop-run finish. No automatic closure.')
