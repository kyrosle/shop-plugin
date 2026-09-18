#!/usr/bin/python3
"""Herdr workstation with explicit file-ticket dispatch and recovery; no scheduler or conversation cloning."""
from language import ArgumentParser, t
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid
import contracts as ct
import coordination as co
import supervision as su
import events as events_module
import snapshot as snapshot_module
import repair
import shutdown as shutdown_module
import herdr
import identity
import messaging
import locking
import language as language_module

# One typed adapter owns every Herdr subprocess/API call in this package.
HERDR = herdr.Herdr()
_SETTINGS = None


def settings_module():
    """Machine runtime config is read on CLI use, never at import time.

    Importing this module must not require a bridge bound to this checkout;
    the real bridge guard still runs before any CLI action touches state.
    """
    global _SETTINGS
    if _SETTINGS is None:
        import settings as module
        _SETTINGS = module
    return _SETTINGS


def state_root():
    return settings_module().STATE


def models():
    return settings_module().models()


def model_profiles(state):
    config = settings_module()
    if "model_profiles" not in state:
        state["model_profiles"] = config.resolve_models(config.models())
    return config.resolve_models({"seats": state["model_profiles"]})


def cmd(argv):
    """Non-Herdr subprocesses only (git). Herdr calls must use ``api``."""
    p = subprocess.run(argv, text=True, capture_output=True, timeout=150)
    if p.returncode:
        raise RuntimeError((p.stderr or p.stdout).strip())
    return p.stdout


def api(*args):
    return HERDR.call(*args)


def save(path, data):
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(data, indent=2) + '\n')
    tmp.replace(path)


def notify(title, body):
    # Best-effort through the adapter; never masks a real operation result.
    HERDR.notify(title, body)


def main():
    if sys.argv[1:2] == ['language']:
        language_module.cli(sys.argv[2:])
        return
    parser = ArgumentParser(description=t(__doc__))
    parser.add_argument('action', nargs='?', choices=['setup', 'status', 'add-worker', 'add-lead', 'remove-lead', 'remove-worker', 'reset', 'bind', 'unbind', 'dispatch', 'resume', 'recover-lead', 'patrol', 'pause', 'route-check', 'message', 'shutdown', 'recovery', 'language'], default='setup')
    parser.add_argument('target', nargs='?', help=t('Run ID for bind, ticket ID for dispatch, or exact auxiliary name for removal'))
    parser.add_argument('--handoff-complete', action='store_true',
                        help=t('Attest results are saved, dependents released, and unsent input disposable'))
    parser.add_argument('--cwd', help=t('Existing independent git worktree for additional agent'))
    parser.add_argument('--models-file', help=t('Model/thinking configuration for a NEW shop; pinned for future launches'))
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--apply', action='store_true', help=t('Explicit recover-lead execution'))
    parser.add_argument('--seconds', type=int, default=0, help=t('patrol wait window, 0..120 seconds'))
    parser.add_argument('--reason', help=t('Evidence-based reason for pause'))
    parser.add_argument('--file', help=t('Route envelope JSON for route-check'))
    parser.add_argument('--text', help=t('Light message, 1..4000 characters'))
    parser.add_argument('--allow-busy', action='store_true', help=t('Explicit message to working member, not cancellation'))
    parser.add_argument('--plan', help=t('Shutdown plan id from a previous preview (execute)'))
    parser.add_argument('--preview', action='store_true', help=t('Shutdown/recovery preview only; never closes'))
    args = parser.parse_args()
    if args.action == 'language':
        parser.error('Use herdr-shop language [auto|zh-CN|en] without other Shop flags')
    if args.models_file and args.action != 'setup':
        parser.error('--models-file only for setup of a new shop')
    if args.action == 'message':
        if not args.target or not args.text or args.dry_run or args.apply:
            parser.error('message requires target and --text; no apply/dry-run')
    elif args.text is not None or args.allow_busy:
        parser.error('--text/--allow-busy only for message')
    if args.file and args.action != 'route-check':
        parser.error('--file only for route-check')
    if args.action == 'route-check' and not args.file:
        parser.error('route-check requires --file')
    if args.seconds and args.action != 'patrol':
        parser.error('--seconds only for patrol')
    if args.reason and args.action != 'pause':
        parser.error('--reason only for pause')
    if args.plan is not None and args.action != 'shutdown':
        parser.error('--plan only for shutdown')
    if args.preview and args.action not in ('shutdown', 'recovery'):
        parser.error('--preview only for shutdown/recovery')
    if args.action == 'pause' and (not args.target or not args.reason):
        parser.error('pause requires target and --reason')
    if args.action in ('patrol', 'pause') and (args.dry_run or args.apply):
        parser.error('patrol/pause do not accept apply/dry-run')
    removing = args.action in ('remove-lead', 'remove-worker')
    if removing and not args.target:
        parser.error('Removal requires an exact agent name (see status).')
    if args.target and not (removing or args.action in ('bind', 'dispatch', 'pause', 'message')):
        parser.error('Unexpected target')
    if args.handoff_complete and not (removing or args.action == 'unbind'):
        parser.error('Unexpected --handoff-complete')
    if args.action in ('bind', 'dispatch') and not args.target:
        parser.error('Explicit run/ticket target required')
    if args.apply and args.action != 'recover-lead':
        parser.error('--apply only valid for recover-lead')
    if args.apply and args.dry_run:
        parser.error('--apply conflicts with --dry-run')
    if args.dry_run and args.action in ('bind', 'unbind', 'dispatch', 'resume'):
        parser.error('This action has no --dry-run; resume itself never dispatches')
    if removing and args.cwd:
        parser.error('--cwd is not valid for removal.')
    # Machine runtime binding is verified before any Herdr access, then the
    # adapter pins binary version + protocol before any state is touched.
    root = state_root()
    settings = settings_module()
    import configuration
    runtime = HERDR.probe()
    # Detached key commands receive ACTIVE_* context, not pane-shell HERDR_ENV/IDs.
    active = os.environ.get('HERDR_ACTIVE_PANE_ID')
    if active and os.environ.get('HERDR_SOCKET_PATH'):
        pane = api('pane', 'get', active)['pane']
    elif os.environ.get('HERDR_ENV') == '1' and os.environ.get('HERDR_PANE_ID'):
        pane = api('pane', 'current', '--current')['pane']
    else:
        raise RuntimeError('Run inside Herdr or via Herdr shortcut; explicit pane/socket context is required.')
    tab = pane['tab_id']
    socket = os.environ.get('HERDR_SOCKET_PATH', '')
    key = hashlib.sha256((socket + ':' + tab).encode()).hexdigest()[:12]
    state_dir = root / 'runtime'
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / (key + '.json')
    if args.action == 'patrol':
        print(json.dumps(su.patrol(api, path, pane, args.seconds), ensure_ascii=False, indent=2))
        return
    with (state_dir / (key + '.lock')).open('w') as lock:
        locking.acquire(lock, wait_seconds=30 if args.action == 'reset' else 0,
                        on_wait=lambda: notify(t('Shop shutdown waiting'), t('Startup/member operation still running; wait at most 30 seconds, then recheck identity and task state.')))
        state = json.loads(path.read_text()) if path.exists() else None
        if args.action == 'status':
            # One bounded, redacted producer for every status reader. No raw state dump.
            if not state:
                print(json.dumps({'schema': snapshot_module.SCHEMA, 'state_file': str(path), 'shop': None,
                                  'members': [], 'attention': [], 'unknowns': ['no registered shop']}, indent=2))
                return
            document = snapshot_module.build(state, ct.tickets(state['cwd'], state['run_id']) if state.get('run_id') else [],
                                             api=api, runtime=runtime.as_dict(), caller_pane=pane['pane_id'],
                                             repo=state.get('cwd'), run_id=state.get('run_id'),
                                             facts=events_module.read_facts(state_root(), state.get('shop_id')))
            document['state_file'] = str(path)
            print(snapshot_module.serialize(document))
            return
        if args.action in ('shutdown', 'recovery'):
            # User-action shutdown authority: separate from caller_control(), never
            # impersonating Architect/Lead, never unbinding or finishing a run.
            if not state:
                raise RuntimeError('No registered shop in this tab.')
            with co.repo_lock(state['cwd']):
                if args.action == 'recovery':
                    print(json.dumps(shutdown_module.recovery_plan(api, path, repo=state['cwd']),
                                     ensure_ascii=False, indent=2))
                    return
                authority = shutdown_module.parse_authority(os.environ)
                plan = shutdown_module.preview(api, path, pane['pane_id'], authority=authority,
                                               repo=state['cwd'])
                plan_path = None
                if plan.get('shop_id'):
                    plan_path = shutdown_module.plan_file(path.parent.parent, plan['shop_id'], plan['plan_id'])
                    ct.atomic(plan_path, plan)
                    try:
                        os.chmod(plan_path, 0o600)
                    except OSError:
                        pass
                    plan['plan_path'] = str(plan_path)
                if args.preview:
                    print(json.dumps(plan, ensure_ascii=False, indent=2))
                    return
                if authority['kind'] != shutdown_module.AUTHORITY:
                    raise RuntimeError('Shutdown execution requires Herdr plugin-action context '
                                       '(use the Shift+U action); preview only from agent CLI.')
                if args.plan and args.plan != plan['plan_id']:
                    stored = shutdown_module.load_plan(path.parent.parent, args.plan)
                    plan = stored
                if plan.get('decision') != 'ready':
                    print(json.dumps(plan, ensure_ascii=False, indent=2))
                    return
                print(json.dumps(shutdown_module.execute(api, path, plan, repo=state['cwd']),
                                 ensure_ascii=False, indent=2))
                return
        if args.action == 'message':
            if not state:
                raise RuntimeError('No registered shop')
            with co.repo_lock(state['cwd']):
                result = messaging.send(api, state, pane, args.target, args.text, args.allow_busy)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return
        if args.action == 'route-check':
            if not state or state.get('phase') != 'ready':
                raise RuntimeError('Ready shop required')
            co.binding(state)
            member = next((m for _, m in identity.roster(state) if m['pane'] == pane['pane_id']), None)
            if not member:
                raise RuntimeError('Caller not registered recipient')
            live = api('agent', 'get', member['pane'])['agent']
            if live.get('name') != member['name'] or live.get('agent') != 'pi' or live.get('tab_id') != state['tab']:
                raise RuntimeError('Recipient runtime identity mismatch')
            identity.validate_route(state, ct.load(args.file), member['name'])
            print(json.dumps({'validated': True, 'recipient': member['name'], 'run_id': state['run_id']}))
            return
        if args.action == 'pause':
            if not state or state.get('phase') != 'ready':
                raise RuntimeError('Ready shop required')
            with co.repo_lock(state['cwd']):
                print(json.dumps(su.pause(api, state, pane, args.target, args.reason), ensure_ascii=False, indent=2))
            return
        if args.action in ('bind', 'unbind', 'dispatch', 'resume', 'recover-lead'):
            if state and not state.get('shop_id'):
                raise RuntimeError('Legacy shop has no binding identity; reset/reopen before using contracts')
            if not state or state.get('phase') != 'ready':
                raise RuntimeError('Ready shop required')
            with co.repo_lock(state['cwd']):
                if args.action == 'bind':
                    result = co.bind(api, save, path, state, pane, args.target)
                elif args.action == 'unbind':
                    result = co.unbind(api, save, path, state, pane, args.handoff_complete)
                elif args.action == 'dispatch':
                    result = co.dispatch(api, state, pane, args.target, state_path=path)
                elif args.action == 'resume':
                    result = co.resume_report(api, state)
                else:
                    model_profiles(state)
                    result = co.recover_lead(api, start, save, path, state, pane, args.apply)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return
        if removing:
            with co.repo_lock(state['cwd']):
                remove_member(path, state, pane, args.action, args.target,
                              args.dry_run, args.handoff_complete)
            return
        if args.action == 'reset':
            reset_shop(path, state, pane, args.dry_run)
            return
        if args.action == 'setup':
            if state:
                if args.models_file:
                    raise RuntimeError('--models-file cannot change an existing shop; its launch profiles are pinned')
                model_profiles(state)
                with co.repo_lock(state['cwd']):
                    if repair.is_pending_add(state):
                        result = repair.resume_pending_add(
                            api, start, save, path, state, pane, args.dry_run)
                    else:
                        result = repair.restore(
                            api, start, save, path, state, pane, args.dry_run)
                print(json.dumps(result, ensure_ascii=False, indent=2))
                return
            layout = api('pane', 'layout', '--pane', pane['pane_id'])['layout']
            if len(layout['panes']) != 1 or layout.get('zoomed'):
                raise RuntimeError('Setup requires one unzoomed Pi pane in current tab; existing layout untouched.')
            if pane.get('agent') != 'pi':
                raise RuntimeError('Current pane must already host Architect Pi. Use shortcut while Architect is focused.')
            if layout['area']['width'] < 140 or layout['area']['height'] < 40:
                raise RuntimeError('Need at least 140 columns and 40 rows for left Architect and right two zones.')
            cwd = pane.get('foreground_cwd') or pane.get('cwd')
            if not cwd or not Path(cwd).is_dir():
                raise RuntimeError('Cannot resolve project directory.')
            prefix = 's' + key[:8]
            if args.models_file:
                profiles = settings.resolve_models(settings.models(args.models_file))
                config_source = {'kind': 'explicit-models-file', 'path': str(Path(args.models_file).expanduser().resolve())}
            else:
                profiles, config_source = configuration.startup_profiles(api, pane, cwd, socket, root)
            if args.dry_run:
                print(json.dumps({'cwd': cwd, 'tab': tab, 'architect': pane['pane_id'],
                                  'name_prefix': prefix, 'model_profiles': profiles, 'config_source': config_source, 'creates': 2}, indent=2))
                return
            state = {'schema_version': 1, 'tab': tab, 'cwd': cwd, 'prefix': prefix, 'shop_id': key + '-' + uuid.uuid4().hex[:8], 'phase': 'creating', 'layout': 'left-architect-right-zones',
                     'architect': {'pane': pane['pane_id'], 'name': prefix + '-architect',
                                   'terminal_id': pane.get('terminal_id')},
                     'workers': [], 'model_profiles': profiles, 'config_source': config_source}
            identity.assign_identity(state['architect'],
                                     terminal_id=pane.get('terminal_id'),
                                     launch_id=uuid.uuid4().hex,
                                     session_source='herdr_pane_terminal')
            save(path, state)
            try:
                api('agent', 'rename', pane['pane_id'], state['architect']['name'])
                api('pane', 'rename', pane['pane_id'], 'Architect · current Pi')
                # Left Architect; right upper Lead and lower Worker zones.
                lead = api('pane', 'split', '--pane', pane['pane_id'], '--direction', 'right',
                           '--cwd', cwd, '--no-focus')['pane']['pane_id']
                state['lead'] = identity.assign_identity({'pane': lead, 'name': prefix + '-lead'})
                save(path, state)
                worker = api('pane', 'split', '--pane', lead, '--direction', 'down',
                             '--cwd', cwd, '--no-focus')['pane']['pane_id']
                state['workers'].append(identity.assign_identity(
                    {'pane': worker, 'name': prefix + '-worker', 'cwd': cwd}))
                save(path, state)
                start(state['lead'], 'lead', state)
                save(path, state)
                start(state['workers'][0], 'worker', state)
                state['phase'] = 'ready'
                save(path, state)
            except Exception as error:
                state['phase'] = 'partial'
                state['error'] = str(error)
                save(path, state)
                raise
        else:
            if not state or state['phase'] != 'ready':
                raise RuntimeError('No ready shop in this tab. Run setup/status first.')
            co.caller_control(api, state, pane)
            role = 'lead' if args.action == 'add-lead' else 'worker'
            members = (state.setdefault('extra_leads', []) if role == 'lead' else state['workers'])
            count = len(members) + (1 if role == 'lead' else 0)
            if count >= 2:
                raise RuntimeError(role + ' limit is 2. No pane created.')
            if not args.cwd:
                raise RuntimeError('Supply --cwd /absolute/existing-independent-worktree. No worktree is created automatically.')
            cwd = str(Path(args.cwd).resolve(strict=True))
            base = state['cwd']
            top = cmd(['git', '-C', cwd, 'rev-parse', '--show-toplevel']).strip()
            if Path(top).resolve() != Path(cwd):
                raise RuntimeError('--cwd must be worktree root.')
            trees = cmd(['git', '-C', base, 'worktree', 'list', '--porcelain'])
            allowed = [Path(line[9:]).resolve() for line in trees.splitlines() if line.startswith('worktree ')]
            if Path(cwd) not in allowed or Path(cwd) == Path(base).resolve():
                raise RuntimeError('Additional agent needs a different registered worktree of this repository.')
            occupied = [state['lead'], *state.get('extra_leads', []), *state['workers']]
            if any(Path(item.get('cwd', base)).resolve() == Path(cwd) for item in occupied):
                raise RuntimeError('Worktree already assigned to another shop agent.')
            if cmd(['git', '-C', cwd, 'status', '--porcelain']).strip():
                raise RuntimeError('Additional worktree must be clean.')
            live = api('pane', 'layout', '--pane', pane['pane_id'])['layout']
            target = state['lead']['pane'] if role == 'lead' else state['workers'][0]['pane']
            match = next((p for p in live['panes'] if p['pane_id'] == target), None)
            if not match or match['rect']['width'] < 90 or match['rect']['height'] < 20:
                raise RuntimeError('Role zone missing or too small (need 90 columns / 20 rows).')
            profile = model_profiles(state)[role + '-2']
            if args.dry_run:
                print(json.dumps({'split_right': target, 'cwd': cwd, 'launch_profile': profile}, indent=2))
                return
            new = api('pane', 'split', '--pane', target, '--direction', 'right',
                      '--cwd', cwd, '--no-focus')['pane']['pane_id']
            item = {'pane': new, 'name': state['prefix'] + '-' + role + '-2', 'cwd': cwd}
            identity.assign_identity(item, session_source='cli_session_dir')
            members.append(item)
            state['phase'] = 'creating'
            save(path, state)
            try:
                start(item, role, state)
                state['phase'] = 'ready'
                save(path, state)
            except Exception as error:
                state['phase'] = 'partial'
                state['error'] = str(error)
                save(path, state)
                raise
        print(json.dumps(state, indent=2))


def remove_member(path, state, caller, action, target, dry_run, handoff_complete):
    if not state or state.get('phase') != 'ready':
        raise RuntimeError('Removal requires a ready shop; inspect partial state manually.')
    if state.get('run_id') and ct.outstanding(state['cwd'], state['run_id'], target):
        raise RuntimeError('Member has unaccepted tickets; accept/cancel with evidence before removal')
    group = 'extra_leads' if action == 'remove-lead' else 'workers'
    members = state.get(group, [])
    candidates = members if group == 'extra_leads' else members[1:]
    item = next((m for m in candidates if m['name'] == target), None)
    if item is None:
        raise RuntimeError('Not a registered auxiliary member of requested role: ' + target)
    if item['pane'] == caller['pane_id']:
        raise RuntimeError('Cannot remove caller pane.')
    controllers = [state['architect'], state['lead']]
    controller = next((m for m in controllers if m['pane'] == caller['pane_id']), None)
    if controller is None:
        raise RuntimeError('Removal must be called from Architect or primary Lead.')

    def check():
        owner = api('agent', 'get', controller['pane'])['agent']
        if (owner.get('agent') != 'pi' or owner.get('name') != controller['name']
                or owner.get('tab_id') != state['tab']):
            raise RuntimeError('Controller identity changed; removal refused.')
        agent = api('agent', 'get', item['pane'])['agent']
        if (agent.get('agent') != 'pi' or agent.get('name') != target
                or agent.get('tab_id') != state['tab']):
            raise RuntimeError('Target identity or tab changed; removal refused.')
        if agent.get('agent_status') not in ('idle', 'done'):
            raise RuntimeError('Target busy, blocked or unknown; removal refused.')

    check()
    if dry_run:
        print(json.dumps({'close': item, 'preserve_worktree': item.get('cwd'),
                          'requires_handoff_complete': True}, indent=2))
        return
    if not handoff_complete:
        raise RuntimeError('Confirm saved results and released dependents with --handoff-complete. '
                           'Closing discards unsent input and in-memory conversation.')
    check()
    # Write intent first. Any ambiguous CLI/disk failure blocks further additions;
    # never silently forget a pane that may still be alive.
    state['phase'] = 'removing'
    state['pending_removal'] = dict(item)
    save(path, state)
    try:
        api('pane', 'close', item['pane'])
        updated = dict(state)
        updated[group] = [m for m in members if m['name'] != target]
        updated['phase'] = 'ready'
        updated.pop('pending_removal', None)
        updated.pop('error', None)
        save(path, updated)
    except Exception as error:
        state['error'] = str(error)
        save(path, state)
        raise
    cleanup_prompt(item)
    print(json.dumps({'removed': target, 'pane': item['pane'],
                      'worktree_preserved': item.get('cwd'), 'phase': 'ready'}, indent=2))


def reset_shop(path, state, caller, dry_run):
    if not state:
        raise RuntimeError('No registered shop in this tab.')
    if state.get('run_id'):
        raise RuntimeError('Unbind run before reset; retained binding protects unfinished work')
    architect = state['architect']
    if caller['pane_id'] != architect['pane']:
        raise RuntimeError('Focus Architect before reset; never close the caller pane.')
    owner = api('agent', 'get', architect['pane'])['agent']
    if owner.get('agent') != 'pi' or owner.get('name') != architect['name']:
        raise RuntimeError('Architect identity changed; reset refused.')
    layout = api('pane', 'layout', '--pane', architect['pane'])['layout']
    live_ids = {p['pane_id'] for p in layout['panes']}
    items = list(reversed(state['workers'])) + list(reversed(state.get('extra_leads', [])))
    if state.get('lead'):
        items.append(state['lead'])
    def check(item):
        agent = api('agent', 'get', item['pane'])['agent']
        if (agent.get('name') != item['name'] or agent.get('agent') != 'pi'
                or agent.get('tab_id') != state['tab']
                or agent.get('agent_status') not in ('idle', 'done')):
            raise RuntimeError('Reset refused: agent changed, busy, blocked or unknown: ' + item['name'])
    targets = [item for item in items if item['pane'] in live_ids]
    known = {architect['pane']} | {item['pane'] for item in items}
    if live_ids - known:
        raise RuntimeError('Unregistered panes in tab; reset refuses to touch this layout.')
    for item in targets:
        check(item)
    if dry_run:
        print(json.dumps({'keep': architect['pane'], 'close': targets}, indent=2))
        return
    # Persist partial progress so rerun can skip panes already closed.
    state['phase'] = 'resetting'
    save(path, state)
    for item in targets:
        check(item)
        api('pane', 'close', item['pane'])
    api('pane', 'zoom', '--pane', architect['pane'], '--off')
    path.unlink()
    for item in items:
        cleanup_prompt(item)
    print(t('Reset complete: Architect preserved; code, tickets and worktrees untouched.'))


def cleanup_prompt(item):
    # Only generated per-agent prompt, after confirmed teardown. Never sweep runtime.
    name = item['name']
    if not name or any(c not in 'abcdefghijklmnopqrstuvwxyz0123456789-_' for c in name):
        return
    try:
        (state_root() / 'runtime' / 'prompts' / (name + '.md')).unlink(missing_ok=True)
    except OSError as error:
        print(t('Prompt cleanup skipped: {0}', str(error)), file=sys.stderr)
    except RuntimeError as error:
        # Best-effort cleanup must not fail a completed pane operation when this
        # checkout has no machine runtime binding (for example a review checkout).
        print(t('Prompt cleanup skipped (no machine runtime binding): {0}', str(error)), file=sys.stderr)


def start(item, role, state):
    seat = role + ('-2' if item['name'].endswith('-' + role + '-2') else '')
    if role not in ('lead', 'worker'):
        raise RuntimeError('Unknown execution role: ' + role)
    profile = model_profiles(state)[seat]
    # Requested launch settings, NOT observed live model/thinking (Pi can change
    # these manually or normalize unsupported reasoning levels).
    item['launch_profile'] = dict(profile)
    label = ('Lead' if role == 'lead' else 'Worker') + (' 2' if seat.endswith('-2') else '')
    api('pane', 'rename', item['pane'], label)
    role_file = 'worker' if seat == 'lead-2' else role
    config = settings_module()
    prompt = config.role_text(role_file)
    prompt += '\nPackage CLI (use these absolute paths, not old global wrappers): ' + str(config.PACKAGE / 'bin/herdr-shop') + ' ; ' + str(config.PACKAGE / 'bin/shop-run')
    prompt += '\n工位主仓库：' + state['cwd'] + '\n工位名称前缀：' + state['prefix']
    prompt += '\n运行任务前读取 herdr-shop status 中 run_id；只处理绑定run，不追随current.json。未绑定则等待显式bind。'
    if state.get('run_id'):
        prompt += '\n绑定run：' + state['run_id']
    if item['name'].endswith('-lead-2'):
        prompt += '\n你是辅助 Sol，只处理主 Lead 分配的独立票或 review；不扩员、不自行派 Worker、不合并，由主 Lead 统一协调。'
    # Pi resolves existing prompt-file paths. Keep PTY launch input short:
    # inline role text can be truncated before the shell enables bracketed paste.
    prompt_dir = state_root() / 'runtime' / 'prompts'
    prompt_dir.mkdir(parents=True, exist_ok=True)
    prompt_path = prompt_dir / (item['name'] + '.md')
    prompt_path.write_text(prompt + '\n')
    # A fresh session per launch, retained for crash recovery. Never auto-resume/fork.
    launch_id = uuid.uuid4().hex
    session_dir = state_root() / 'runtime' / 'sessions' / state.get('shop_id', state['prefix']) / (item['name'] + '-' + launch_id)
    session_dir.mkdir(parents=True, exist_ok=True)
    identity.assign_identity(item, session_dir=str(session_dir), launch_id=launch_id,
                             session_source='cli_session_dir')
    if state.get('run_id'):
        ct.atomic(ct.run_path(state['cwd'], state['run_id']) / 'session-refs' / (session_dir.name + '.json'),
                  {'agent': item['name'], 'session_dir': str(session_dir), 'created_at': ct.stamp(),
                   'launch_id': item['launch_id'],
                   'note': 'Recovery only; never automatic conversation handoff'})
    HERDR.start_agent(item['name'], 'pi', item['pane'], session_dir, profile['model'], prompt_path, thinking=profile.get('thinking'))
    live = HERDR.agent(item['name'])
    if live.get('name') != item['name'] or not live.get('terminal_id'):
        raise RuntimeError('Started agent identity not confirmed')
    identity.assign_identity(item, terminal_id=live['terminal_id'],
                             session_source='herdr_agent_terminal')


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        message = 'herdr-shop: ' + str(error)
        print(message, file=sys.stderr)
        state_root().mkdir(parents=True, exist_ok=True)
        with (state_root() / 'last-error.log').open('a') as log:
            log.write(ct.stamp() + ' ' + message + '\n')
        if os.environ.get('HERDR_ACTIVE_PANE_ID') or os.environ.get('HERDR_PLUGIN_ACTION_ID'):
            try:
                HERDR.notify(t('Shop operation stopped'), message[:600])
            except Exception:
                pass
        sys.exit(1)
