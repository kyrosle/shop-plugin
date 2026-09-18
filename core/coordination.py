"""Explicit Herdr run binding, dispatch receipts and conservative recovery."""
import contextlib
import fcntl
import os
from pathlib import Path
import contracts as ct
import herdr
import identity


@contextlib.contextmanager
def repo_lock(repo):
    folder = Path(repo) / '.shop'
    if folder.is_symlink():
        raise RuntimeError('Symlink shop refused')
    folder.mkdir(exist_ok=True)
    fd = os.open(folder / '.run.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as f:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def caller_control(api, state, caller, primary_only=False):
    controllers = [state['lead']] if primary_only else [state['architect'], state['lead']]
    item = next((a for a in controllers if a['pane'] == caller['pane_id']), None)
    if not item:
        raise RuntimeError('Caller must be primary Lead' if primary_only else 'Caller must be Architect/primary Lead')
    live = identity.observed_agent(api, item['pane'], 'controller ' + str(item.get('name')))
    # The calling controller may be busy; only its identity and tab are checked here.
    if (live.get('name') != item['name'] or live.get('tab_id') != state['tab']
            or live.get('agent') != 'pi' or live.get('pane_id') != item['pane']):
        raise herdr.HerdrIdentityError('Controller identity changed')


def members(state):
    return [state['lead'], *state.get('extra_leads', []), *state['workers']]


def binding(state):
    if not state.get('run_id'):
        raise RuntimeError('No bound run. Use explicit bind <run-id>; never infer from current.json')
    registry = ct.bindings(state['cwd'])
    b = registry.get(state['run_id'])
    if not b or b['shop_id'] != state['shop_id']:
        raise RuntimeError('Binding registry mismatch; manual reconciliation required')
    return b


def bind(api, save, path, state, caller, rid):
    caller_control(api, state, caller)
    if state.get('run_id'):
        if state['run_id'] == rid:
            binding(state)
            return {'bound': rid, 'unchanged': True}
        raise RuntimeError('Already bound; stop writers and explicitly unbind first')
    meta = ct.load(ct.run_path(state['cwd'], rid) / 'run.json')
    if meta['status'] not in ('active', 'blocked'):
        raise RuntimeError('Only active/blocked managed run can bind')
    registry = ct.bindings(state['cwd'])
    if rid in registry:
        raise RuntimeError('Run already bound to another workstation (stale bindings require inspection)')
    for item in members(state):
        if item['pane'] == caller['pane_id']:
            continue  # Calling Lead can be working while issuing this command.
        identity.resolve(api, item, state['tab'], allowed_status=herdr.SETTLED_STATUSES,
                         where='member ' + str(item.get('name')))
    state['run_id'] = rid
    registry[rid] = {'shop_id': state['shop_id'], 'tab': state['tab'], 'lead': state['lead'],
                     'state_path': str(path), 'bound_at': ct.stamp()}
    # Registry first: fail closed against GC if second write fails.
    ct.atomic(ct.binding_file(state['cwd']), registry)
    save(path, state)
    for item in members(state):
        if item.get('session_dir'):
            ct.atomic(ct.run_path(state['cwd'], rid) / 'session-refs' / (Path(item['session_dir']).name + '.json'),
                      {'agent': item['name'], 'session_dir': item['session_dir'], 'recorded_at': ct.stamp()})
    return {'bound': rid, 'path': str(ct.run_path(state['cwd'], rid))}


def unbind(api, save, path, state, caller, confirmed):
    caller_control(api, state, caller)
    binding(state)
    if not confirmed:
        raise RuntimeError('--handoff-complete required before unbind')
    if ct.outstanding(state['cwd'], state['run_id']):
        raise RuntimeError('Unaccepted tickets remain; do not unbind')
    for item in members(state):
        if item['pane'] == caller['pane_id']:
            continue
        identity.resolve(api, item, state['tab'], allowed_status=herdr.SETTLED_STATUSES,
                         where='member ' + str(item.get('name')))
    rid = state.pop('run_id')
    save(path, state)
    registry = ct.bindings(state['cwd'])
    registry.pop(rid)
    ct.atomic(ct.binding_file(state['cwd']), registry)
    return {'unbound': rid, 'run_not_finished': True}


def dispatch(api, state, caller, tid, state_path=None):
    caller_control(api, state, caller, primary_only=True)
    binding(state)
    repo, rid = state['cwd'], state['run_id']
    if ct.load(ct.run_path(repo, rid) / 'run.json')['status'] != 'active':
        raise RuntimeError('Run blocked/completed')
    p = ct.ticket_path(repo, rid, tid)
    t = ct.load(p)
    if t['status'] != 'ready':
        raise RuntimeError('Only ready tickets may dispatch; duplicate/uncertain delivery requires reconciliation')
    if t.get('spec_revision', 0) != ct.load(ct.run_path(repo, rid) / 'run.json').get('spec_revision', 0):
        raise RuntimeError('Requirements changed; explicitly revise ready ticket before dispatch')
    for dep in t['depends_on']:
        if ct.load(ct.ticket_path(repo, rid, dep))['status'] != 'accepted':
            raise RuntimeError('Dependency not accepted')
    item = next((x for x in [*state.get('extra_leads', []), *state['workers']] if x['name'] == t['owner']), None)
    if not item:
        raise RuntimeError('Owner is not a registered execution member')
    from workbench import bounded
    for path in (ct.run_path(repo, rid) / 'workbench').glob('*.json'):
        request = bounded(path)
        if (request.get('kind') == 'profile' and request.get('status') in ('applying', 'unknown')
                and (request.get('recipient') or {}).get('name') == item['name']):
            raise RuntimeError('Owner profile change is applying/unknown; inspect in recipient /shop-ui before dispatch')
    for other in ct.outstanding(repo, rid):
        if other['ticket_id'] != tid and other['owner'] == t['owner'] and other['status'] in ('assigned', 'review'):
            raise RuntimeError('Owner already has assigned/review work')
    live = identity.resolve(api, item, state['tab'], allowed_status=herdr.SETTLED_STATUSES,
                            require_terminal=True, where='owner ' + str(item.get('name')))
    if t['kind'] == 'development':
        if Path(t['worktree']).resolve() != Path(item.get('cwd', repo)).resolve():
            raise RuntimeError('Development ticket cwd differs from agent worktree')
        ct.baseline(repo, t['worktree'], t['base_commit'])
        # Same repo/common working tree is never concurrently writable through dispatch.
        for other in ct.outstanding(repo, rid):
            if other['ticket_id'] != tid and other['kind'] == 'development' and other['status'] in ('assigned', 'review'):
                if Path(other['worktree']).resolve() == Path(t['worktree']).resolve():
                    raise RuntimeError('Another writer owns this worktree')
    envelope = None
    if state.get('schema_version') == 1:
        target_role = 'auxiliary_lead' if item in state.get('extra_leads', []) else 'worker'
        envelope = identity.build_envelope(state, item, target_role, tid, t['attempt'])
        identity.validate_route(state, envelope, item['name'])
    import transport
    message = '处理工单 ' + str(p) + '。run/attempt固定；按角色说明用shop-run发布checkpoint/result，不读current.json改换任务。'
    handoff_id = None
    recipient_session = None
    if state_path is not None:
        from workbench import Workbench
        prepared = Workbench(api, state_path, caller['pane_id']).execute({
            'action': 'handoff-propose', 'recipient': item['name'], 'ticket_id': tid,
            'objective': t['objective'], 'scope': ', '.join(t['scope']),
            'acceptance': '; '.join(t['checks']) or 'Primary Lead reviews objective and evidence',
            'evidence': message})
        notice = prepared['transport_request']
        handoff_id = prepared['request']['id']
        recipient_session = prepared['transport_session']
    else:
        # Library-only preparation path; CLI/Pi always supplies the locked state path.
        notice = transport.prepare(repo, rid, state, state['lead'], item, 'task_notice', message)
    t['status'] = 'assigned'
    t['dispatch'] = {'pane': item['pane'], 'terminal_id': live['terminal_id'],
                     'delivery': 'prepared', 'message_id': notice['message_id'], 'handoff_id': handoff_id, 'sent_at': ct.stamp()}
    if envelope:
        t['dispatch']['envelope'] = envelope
    ct.atomic(p, t)
    return {**t, 'transport_request': notice, 'transport_session': recipient_session,
            'note': 'Assigned, notice prepared only. Calling Pi must send via built-in transport. Never auto-resend.'}


def resume_report(api, state):
    binding(state)
    rows = []
    for item in members(state):
        try:
            pane = identity.observed_pane(api, item['pane'], 'member ' + str(item.get('name')))
            process = herdr.process_info_record(api('pane', 'process-info', '--pane', item['pane'])['process_info'])
            rows.append({'registered': identity.member_view(item), 'live': pane, 'process': process})
        except Exception as e:
            rows.append({'registered': identity.member_view(item), 'error': str(e)})
    report = {'run_id': state['run_id'], 'members': rows, 'tickets': ct.tickets(state['cwd'], state['run_id']),
              'checkpoints': [str(p) for p in (ct.run_path(state['cwd'], state['run_id']) / 'tickets').glob('*.checkpoint.json')],
              'instructions': 'No automatic re-dispatch. Inspect old writers and uncertain deliveries first. '
                              'A saved session is recovery evidence, not cross-agent handoff. '
                              'Use retry --writer-stopped only after verifying old writer stopped; new attempt preserves old results.'}
    p = ct.run_path(state['cwd'], state['run_id']) / 'RECOVERY.json'
    ct.atomic(p, report)
    return {'report': str(p), 'details': report, 'agents_started': 0, 'prompts_sent': 0}


def recover_lead(api, start, save, path, state, caller, apply=False):
    # Architect only; do not guess a replacement pane or kill a running occupant.
    if caller['pane_id'] != state['architect']['pane']:
        raise RuntimeError('Only Architect may recover primary Lead')
    caller_control(api, state, caller)
    report = resume_report(api, state)
    item = state['lead']
    pane = identity.observed_pane(api, item['pane'], 'lead pane')
    process = herdr.process_info_record(api('pane', 'process-info', '--pane', item['pane'])['process_info'])
    if pane.get('agent') or pane.get('tab_id') != state['tab'] or not process.get('foreground_processes'):
        raise RuntimeError('Lead pane not an available shell; inspect resume report')
    if any(x['pid'] != process['shell_pid'] for x in process['foreground_processes']):
        raise RuntimeError('Lead pane has foreground work; never replace automatically')
    if not apply:
        return {'would_start_fresh_lead': item, 'recovery': report['report']}
    start(item, 'lead', state)
    save(path, state)
    # Report path only; no transcript replay and no auto-ticket assignment.
    api('agent', 'prompt', item['name'], '先阅读恢复报告 ' + report['report'] + '，核对旧Worker和工单状态；仅汇报恢复建议，不重派任务。')
    return {'started': item['name'], 'recovery': report['report']}
