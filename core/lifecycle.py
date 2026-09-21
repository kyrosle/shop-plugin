"""Shop incarnation and live readiness. Caller holds the tab lock for mutations.

A lease expiring only removes permission; it never proves a process stopped.
Only explicit open can retire a proven orphan, archiving registration bytes.
No process control, run completion, ticket changes or worktree deletion here.
"""
import hashlib
import json
import os
from pathlib import Path
import time

import contracts as ct
import identity
import processes
import reset

LEASE_SECONDS = 15


def birth(row):
    return {'pid': row['pid'], 'start': row['start']}


def host(table):
    pid = processes.local_server_pid()
    row = table.get(pid)
    if not row or row['uid'] != os.geteuid() or row['sid'] is None:
        raise RuntimeError('Herdr process instance unavailable')
    return birth(row)


def owner(api, state_dir, pane, cwd):
    import configuration
    socket = os.environ.get('HERDR_SOCKET_PATH', '')
    record = configuration.startup_candidate(api, pane, cwd, socket, state_dir)
    table = processes.snapshot()
    server = host(table)
    info = api('pane', 'process-info', '--pane', pane['pane_id'])['process_info']
    shell = table.get(info.get('shell_pid'))
    process = table.get(record['pid'])
    if (info.get('pane_id') != pane['pane_id'] or not shell or not process
            or shell['ppid'] != server['pid'] or shell['sid'] != shell['pid']
            or process['uid'] != os.geteuid() or process['sid'] != shell['pid']):
        raise RuntimeError('Architect candidate is not attached to this Herdr pane')
    ancestor, seen = process['pid'], set()
    while ancestor != shell['pid'] and ancestor in table and ancestor not in seen:
        seen.add(ancestor)
        ancestor = table[ancestor]['ppid']
    if ancestor != shell['pid']:
        raise RuntimeError('Architect candidate ancestry changed')
    return {'version': 1, 'socket': socket, 'server': server,
            'root': str(Path(cwd).resolve()), 'session_id': record['session_id'],
            'architect_process': birth(process)}


def begin(api, state_dir, pane, cwd):
    before = owner(api, state_dir, pane, cwd)
    if owner(api, state_dir, pane, cwd) != before:
        raise RuntimeError('Architect or Herdr instance changed during open')
    return before


def record_member(api, state, member):
    """Record original shell/Pi identities, including a launch that is not idle yet."""
    scope = state.get('lifecycle')
    if not scope:
        return  # Legacy restore stays legacy; never invent historical evidence.
    info = api('pane', 'process-info', '--pane', member['pane'])['process_info']
    candidates = [p['pid'] for p in info.get('foreground_processes', [])
                  if p.get('argv0') and Path(p['argv0']).name == 'pi']
    if info.get('pane_id') != member['pane'] or len(candidates) != 1:
        raise RuntimeError('Cannot record unique member process identity')
    before, after = processes.snapshot(), processes.snapshot()
    if host(before) != scope['server'] or host(after) != scope['server']:
        raise RuntimeError('Herdr instance changed during member launch')
    a = processes.pane_scope(before, info, candidates[0], scope['server']['pid'])
    b = processes.pane_scope(after, info, candidates[0], scope['server']['pid'])
    if a['anchor'] != b['anchor']:
        raise RuntimeError('Member process identity changed during launch')
    member['process_anchor'] = {key: value for key, value in b['anchor'].items()
                                if int(key) != scope['server']['pid']}


def require_owner(api, state, state_dir, session_id=None):
    if state.get('phase') != 'ready' or state.get('recovery_required'):
        raise RuntimeError('Shop needs recovery; no delegation permitted')
    scope = state.get('lifecycle')
    if not isinstance(scope, dict) or scope.get('version') != 1:
        raise RuntimeError('Legacy Shop lacks session/process ownership; inspect recovery before opening a new Shop')
    if session_id is not None and session_id != scope.get('session_id'):
        raise RuntimeError('Architect session changed; old Shop permission expired')
    pane = api('pane', 'get', state['architect']['pane'])['pane']
    if pane.get('tab_id') != state['tab'] or pane.get('terminal_id') != state['architect'].get('terminal_id'):
        raise RuntimeError('Architect terminal/tab changed')
    if str(Path(pane.get('foreground_cwd') or pane['cwd']).resolve()) != scope.get('root'):
        raise RuntimeError('Architect directory changed')
    if begin(api, state_dir, pane, state['cwd']) != scope:
        raise RuntimeError('Shop owner session, directory or Herdr instance changed')


def require_current(api, state, state_dir, session_id=None):
    require_owner(api, state, state_dir, session_id)
    table = processes.snapshot()
    if host(table) != state['lifecycle']['server']:
        raise RuntimeError('Herdr process instance changed')
    for member in [state.get('lead'), *state.get('workers', []), *state.get('extra_leads', [])]:
        anchors = (member or {}).get('process_anchor')
        if not anchors or any(table.get(int(pid)) != old for pid, old in anchors.items()):
            raise RuntimeError('Member process incarnation changed or is unverified')
    # Global enumeration detects moved/duplicate agents, not just missing local panes.
    import shutdown
    facts = shutdown._member_facts(api, state)
    if facts['read_error'] or not state.get('lead') or not state.get('workers'):
        raise RuntimeError('Shop member inventory unavailable or incomplete')
    for row in facts['members']:
        if row['classification'] != 'present_exact':
            raise RuntimeError('Shop member %s is %s' % (row['name'], row['classification']))
        if row['status'] not in ('idle', 'done', 'working'):
            raise RuntimeError('Shop member status is unverified: ' + str(row['name']))
    registered = {m['pane'] for _, m in identity.roster(state)}
    if any(p['pane_id'] not in registered for p in shutdown._managed_tab_panes(state, facts)):
        raise RuntimeError('Unregistered pane in Shop tab')
    key = hashlib.sha256((state['lifecycle']['socket'] + ':' + state['tab']).encode()).hexdigest()[:12]
    location = str(Path(state_dir) / 'runtime' / (key + '.json'))
    related = [rid for rid, record in ct.bindings(state['cwd']).items()
               if record.get('shop_id') == state['shop_id'] or record.get('state_path') == location]
    if set(related) != ({state['run_id']} if state.get('run_id') else set()):
        raise RuntimeError('Registration/binding mismatch; explicit reconciliation required')
    if state.get('run_id'):
        import coordination
        coordination.binding(state)
        if ct.load(ct.run_path(state['cwd'], state['run_id']) / 'run.json')['status'] == 'completed':
            raise RuntimeError('Completed run still bound; explicit reconciliation required')
    return facts


def preflight(api, path, state, session_id):
    """Live read before /shop; never consult chat history to authorize delegation."""
    raw = reset.read_file(path) or b''
    revision = reset.digest(raw)
    try:
        if not raw or json.loads(raw) != state:
            raise RuntimeError('Registration changed before preflight')
        bindings = ct.bindings(state['cwd'])
        facts = require_current(api, state, path.parent.parent, session_id)
        lead = next(row for row in facts['members'] if row['role'] == 'lead')
        if lead['status'] not in ('idle', 'done'):
            raise RuntimeError('Primary Lead is busy or unknown; nothing delegated')
        if reset.digest(reset.read_file(path) or b'') != revision or ct.bindings(state['cwd']) != bindings:
            raise RuntimeError('Shop registration/binding changed during preflight')
        return {'decision': 'ready', 'shop_id': state['shop_id'], 'run_id': state.get('run_id'),
                'session_id': session_id, 'state_revision': revision,
                'expires_at': time.time() * 1000 + LEASE_SECONDS * 1000}
    except Exception as error:
        return {'decision': 'blocked', 'shop_id': state.get('shop_id'), 'run_id': state.get('run_id'),
                'state_revision': revision, 'reason': str(error)[:500]}


def orphan_proof(api, path, state, caller):
    """Absence plus original process evidence; missing/legacy evidence blocks."""
    raw = reset.read_file(path)
    if not raw or json.loads(raw) != state:
        raise RuntimeError('Registration changed during orphan inspection')
    socket = os.environ.get('HERDR_SOCKET_PATH', '')
    key = hashlib.sha256((socket + ':' + caller['tab_id']).encode()).hexdigest()[:12]
    if (path.stem != key or state.get('tab') != caller['tab_id']
            or state.get('architect', {}).get('pane') != caller['pane_id']):
        raise RuntimeError('Old Shop belongs to another slot; nothing archived')
    if state.get('run_id') or state.get('has_run_history') or state.get('pending_shutdown') or state.get('phase') not in ('ready', 'partial'):
        raise RuntimeError('Run history/binding or pending operation requires explicit recovery; no automatic cleanup')
    registry = ct.bindings(state['cwd'])
    if any(b.get('shop_id') == state['shop_id'] or b.get('state_path') == str(path)
           for b in registry.values()):
        raise RuntimeError('Binding registry still owns this Shop; explicit unbind required')
    scope = state.get('lifecycle')
    if not scope or scope.get('version') != 1 or scope.get('socket') != socket:
        raise RuntimeError('Legacy registration lacks process evidence; explicit recovery required')
    current = begin(api, path.parent.parent, caller, caller.get('foreground_cwd') or caller['cwd'])
    layout = api('pane', 'layout', '--pane', caller['pane_id'])['layout']
    if [p['pane_id'] for p in layout['panes']] != [caller['pane_id']]:
        raise RuntimeError('Existing/partial layout: orphan cleanup never closes panes')
    panes = [p for w in api('workspace', 'list')['workspaces']
             for p in api('pane', 'list', '--workspace', w['workspace_id'])['panes']]
    agents = api('agent', 'list')['agents']
    members = [state.get('lead'), *state.get('workers', []), *state.get('extra_leads', [])]
    members = [m for m in members if m]
    if not members and state.get('setup_stage') != 'architect':
        raise RuntimeError('Incomplete setup: unknown member mutation; inspect recovery')
    # Prefix scan also catches a split/start whose successful reply was lost.
    if any(a.get('pane_id') != caller['pane_id'] and str(a.get('name') or '').startswith(state['prefix'] + '-') for a in agents):
        raise RuntimeError('Old Shop member still live or moved')
    table = processes.snapshot()
    for member in members:
        if any(p.get('pane_id') == member.get('pane') or
               (member.get('terminal_id') and p.get('terminal_id') == member['terminal_id']) for p in panes):
            raise RuntimeError('Old member pane still exists; nothing archived')
        anchors = member.get('process_anchor')
        if not anchors:
            raise RuntimeError('Missing original member process evidence; no automatic cleanup')
        for old in anchors.values():
            live = table.get(old['pid'])
            if live and live['start'] == old['start']:
                raise RuntimeError('Original member process still alive')
            if any(p['sid'] == old['sid'] or p['pgid'] == old['pgid'] or
                   (old['tty'] not in ('?', '??', '-') and p['tty'] == old['tty']) for p in table.values()):
                raise RuntimeError('Old pane session/group/terminal still has processes')
    return {'owner': current, 'shop_id': state['shop_id'], 'revision': reset.digest(reset.read_file(path) or b'')}


def open_existing(api, path, state, caller):
    """Return reuse or archive. Never restore/restart members on an ordinary open."""
    try:
        require_current(api, state, path.parent.parent)
        if state['architect']['pane'] != caller['pane_id']:
            raise RuntimeError('Open must originate from the registered Architect')
        return {'reused': state['shop_id'], 'creates_panes': 0}
    except Exception as error:
        reason = str(error)
    # Caller must also hold the old repository lock before this operation.
    proof = orphan_proof(api, path, state, caller)
    raw = reset.read_file(path)
    if not raw or reset.digest(raw) != proof['revision']:
        raise RuntimeError('Registration changed before archival')
    token = reset.digest(json.dumps(proof, sort_keys=True).encode())
    archive = reset.archive_registration(path, raw, token)
    if orphan_proof(api, path, state, caller) != proof or reset.read_file(path) != raw:
        raise RuntimeError('Orphan facts changed; registration retained. Backup: ' + str(archive))
    path.unlink()
    return {'archived': state['shop_id'], 'archive': str(archive), 'reason': reason,
            'closes_panes': 0, 'tasks_preserved': True}
