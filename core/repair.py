"""Rebuild seats after manual pane close, preserving run and assignment evidence."""
import copy
from pathlib import Path
import uuid
import contracts as ct
import coordination as co
import herdr
import identity


def is_pending_add(state):
    """Recognize only add-member failures that are safe to resume in-place."""
    return (state.get('phase') == 'partial'
            and 'agent_pane_busy' in str(state.get('error', '')))


def resume_pending_add(api, start, save, path, state, caller, dry_run=False):
    """Start one registered add-member pane after agent-start failed.

    This never creates, closes, moves, or reassigns a pane. It only accepts the
    exact partial-add shape: one registered member without a terminal identity,
    an empty foreground shell in that member's recorded pane/cwd, and every
    older member still present with its recorded identity.
    """
    if not is_pending_add(state):
        raise RuntimeError('Partial state is not a resumable agent_pane_busy add')
    architect = state['architect']
    if (caller.get('pane_id') != architect.get('pane')
            or caller.get('tab_id') != state.get('tab')
            or caller.get('agent') != 'pi'
            or caller.get('name') not in (None, architect.get('name'))):
        raise RuntimeError('Focus original Architect Pi before resuming pending add')

    members = co.members(state)
    pending = [member for member in members if not member.get('terminal_id')]
    if len(pending) != 1:
        raise RuntimeError('Pending add requires exactly one member without terminal identity')
    item = pending[0]
    if not item.get('name') or not item.get('pane') or not item.get('cwd'):
        raise RuntimeError('Pending add registration lacks name/pane/cwd')
    expected_cwd = Path(item['cwd']).resolve(strict=True)

    layout = api('pane', 'layout', '--pane', architect['pane'])['layout']
    pane_row = next((p for p in layout.get('panes', [])
                     if p.get('pane_id') == item['pane']), None)
    if pane_row is None:
        raise RuntimeError('Pending add pane is not in registered tab layout')
    pane = identity.observed_pane(api, item['pane'], 'pending add pane')
    if pane.get('tab_id') != state.get('tab') or pane.get('pane_id') != item['pane']:
        raise RuntimeError('Pending add pane moved or identity changed')
    observed_cwd = pane.get('foreground_cwd') or pane.get('cwd')
    if not observed_cwd or Path(observed_cwd).resolve() != expected_cwd:
        raise RuntimeError('Pending add pane cwd differs from registration')
    if pane.get('agent'):
        raise RuntimeError('Pending add pane already contains an agent')

    agents = [herdr.agent_record(a, 'agent list entry')
              for a in api('agent', 'list')['agents']]
    if any(agent.get('pane_id') == item['pane'] or agent.get('name') == item['name']
           for agent in agents):
        raise RuntimeError('Pending add agent already exists; inspect identity')
    for member in members:
        if member is item:
            continue
        live = next((agent for agent in agents
                     if agent.get('pane_id') == member.get('pane')), None)
        if (not live or not live.get('name') or live.get('name') != member.get('name')
                or live.get('agent') != 'pi'
                or live.get('tab_id') != state.get('tab')
                or (member.get('terminal_id')
                    and live.get('terminal_id') != member.get('terminal_id'))):
            raise RuntimeError('Existing member identity changed: ' + str(member.get('name')))

    process = herdr.process_info_record(api('pane', 'process-info', '--pane', item['pane'])['process_info'],
                                        'pending add process-info')
    foreground = process.get('foreground_processes') or []
    shell_pid = process.get('shell_pid')
    if (not shell_pid or len(foreground) != 1
            or foreground[0].get('pid') != shell_pid):
        raise RuntimeError('Pending add pane is not an empty foreground shell')
    if dry_run:
        return {'would_resume_pending_add': item['name'], 'pane': item['pane'],
                'cwd': str(expected_cwd), 'creates_panes': 0, 'prompts_sent': 0}

    role = 'lead' if item in state.get('extra_leads', []) else 'worker'
    try:
        start(item, role, state)
        state['phase'] = 'ready'
        state.pop('error', None)
        save(path, state)
    except Exception as error:
        state['phase'] = 'partial'
        state['error'] = str(error)
        save(path, state)
        raise
    return {'resumed_pending_add': item['name'], 'pane': item['pane'],
            'cwd': str(expected_cwd), 'phase': 'ready',
            'note': 'Fresh agent session started; no ticket prompt or reassignment'}


def restore(api, start, save, path, state, caller, dry_run=False, plan=None):
    """Only a plan-approved apply may rebuild seats.

    ``dry_run`` stays free (preview), but an apply needs a read-only recovery plan
    produced by ``core/shutdown.py`` for this exact state revision, so a rebuild can
    never be triggered without a previewed reconciliation.
    """
    # Initial setup can fail before either seat is recorded, including after a
    # successful mutation whose reply was rejected. Never invent missing seats
    # or treat that registration as a complete roster eligible for restoration.
    lead, workers = state.get('lead'), state.get('workers')
    if (not isinstance(lead, dict) or not lead.get('pane') or not lead.get('name')
            or not isinstance(workers, list) or not workers
            or any(not isinstance(item, dict) or not item.get('pane') or not item.get('name')
                   for item in workers)):
        raise RuntimeError('Incomplete setup registration: primary Lead/Worker seats were not fully recorded. '
                           'No automatic retry. Inspect the saved error and live panes; '
                           'preview reset from the original Architect before any fresh setup.')
    architect = state['architect']
    if caller['pane_id'] != architect['pane'] or caller.get('agent') != 'pi':
        raise RuntimeError('Focus original Architect Pi before restoring seats')
    if caller['tab_id'] != state['tab']:
        raise RuntimeError('Architect tab changed')
    if caller.get('name') not in (None, architect['name']):
        raise RuntimeError('Architect occupied by a differently named agent')
    layout = api('pane', 'layout', '--pane', architect['pane'])['layout']
    if len(layout['panes']) != 1 or layout['panes'][0]['pane_id'] != architect['pane']:
        raise RuntimeError('Existing/partial layout: restore requires only original Architect; no panes changed')
    if layout.get('zoomed') or layout['area']['width'] < 140 or layout['area']['height'] < 40:
        raise RuntimeError('Unzoom Architect; need 140 columns and 40 rows')
    if state.get('phase') not in ('ready', 'partial', 'restoring'):
        raise RuntimeError('Cannot restore during unfinished removal/reset; inspect state')
    old_members = co.members(state)
    # Search all workspaces, not only current tab: moved panes aren't dead.
    all_panes = []
    for w in api('workspace', 'list')['workspaces']:
        all_panes += [herdr.pane_record(p, 'pane list entry')
                      for p in api('pane', 'list', '--workspace', w['workspace_id'])['panes']]
    for old in old_members:
        if any(p['pane_id'] == old['pane'] or p.get('name') == old['name'] for p in all_panes):
            raise RuntimeError('Registered member still exists/moved: ' + old['name'])
    agents = [herdr.agent_record(a, 'agent list entry')
              for a in api('agent', 'list')['agents']]
    if any(a.get('name') in {m['name'] for m in old_members} for a in agents):
        raise RuntimeError('Registered agent name still live elsewhere')
    if state.get('run_id'):
        co.binding(state)
    extras = state.get('extra_leads', [])
    if len(extras) > 1 or len(state['workers']) > 2:
        raise RuntimeError('Unsupported membership count')
    if (extras or len(state['workers']) > 1) and layout['area']['width'] < 180:
        raise RuntimeError('Need 180 columns to restore auxiliary seats')
    # Validate preserved cwd before making any new panes. Dirty changes are NOT discarded.
    from pathlib import Path
    for member in old_members:
        if not Path(member.get('cwd', state['cwd'])).is_dir():
            raise RuntimeError('Preserved worktree missing: ' + member['name'])
    if dry_run:
        return {'restore': [m['name'] for m in old_members], 'run_id': state.get('run_id'),
                'prompts_sent': 0, 'tickets_reassigned': 0, 'keep': architect['pane']}
    import shutdown as shutdown_module
    if not isinstance(plan, dict):
        raise RuntimeError('Restore requires a previewed recovery plan (dry-run/plan first).')
    # A plan must be the one this core produced: persisted, digest-matching, for this
    # state revision, unexpired and approving. A hand-built dict cannot pass.
    try:
        shutdown_module.verify_recovery_plan(state, plan, Path(path).parent.parent)
    except shutdown_module.ShutdownRefused as refusal:
        raise RuntimeError('Recovery plan refused (%s): %s' % (refusal.code, refusal.detail))
    archive = path.parent / 'repairs' / (path.stem + '-' + uuid.uuid4().hex[:8] + '.json')
    ct.atomic(archive, copy.deepcopy(state))
    state['shop_id'] = state.get('shop_id') or path.stem + '-' + uuid.uuid4().hex[:8]
    state['phase'] = 'restoring'
    state['repair_archive'] = str(archive)
    state['recovery_required'] = True
    save(path, state)
    try:
        api('agent', 'rename', architect['pane'], architect['name'])
        def split(item, target, direction):
            cwd = item.get('cwd', state['cwd'])
            result = api('pane', 'split', '--pane', target, '--direction', direction, '--cwd', cwd, '--no-focus')
            item['pane'] = result['pane']['pane_id']
            item.pop('session_dir', None)
            save(path, state)
        split(state['lead'], architect['pane'], 'right')
        split(state['workers'][0], state['lead']['pane'], 'down')
        for item in extras:
            split(item, state['lead']['pane'], 'right')
        for item in state['workers'][1:]:
            split(item, state['workers'][0]['pane'], 'right')
        if state.get('run_id'):
            registry = ct.bindings(state['cwd'])
            registry[state['run_id']]['lead'] = copy.deepcopy(state['lead'])
            ct.atomic(ct.binding_file(state['cwd']), registry)
        for item in co.members(state):
            start(item, 'lead' if item in [state['lead'], *extras] else 'worker', state)
            save(path, state)
        state['phase'] = 'ready'
        state.pop('error', None)
        save(path, state)
        report = co.resume_report(api, state) if state.get('run_id') else None
        return {'restored': [m['name'] for m in co.members(state)], 'archive': str(archive),
                'recovery_report': report['report'] if report else None,
                'note': 'Fresh sessions, no automatic task prompts/retry. Read recovery report; old attempts remain fenced.'}
    except Exception as e:
        state['phase'] = 'partial'
        state['error'] = str(e)
        save(path, state)
        raise
