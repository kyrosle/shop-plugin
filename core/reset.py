"""Archive an early failed setup only. No Herdr mutations or project writes.

The preview token is a compare-and-swap fence, not an authorization credential.
Pi requires interactive confirmation; the legacy member-closing reset is separate.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys

import herdr
import locking

LIMIT = 65536


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def read_file(path):
    if path.is_symlink():
        raise RuntimeError('Symlink refused: ' + str(path))
    if not path.exists():
        return None
    if not stat.S_ISREG(path.stat().st_mode) or path.stat().st_size > LIMIT:
        raise RuntimeError('Invalid or oversized reset evidence: ' + str(path))
    raw = path.read_bytes()
    if len(raw) > LIMIT:
        raise RuntimeError('Reset evidence exceeds 64 KiB')
    return raw


def plan(api, path, caller):
    """Caller holds this tab's normal Shop lock. All Herdr calls are reads."""
    result = {'schema': 1, 'decision': 'blocked', 'reason': 'invalid_registration',
              'state_file': str(path), 'tab': caller['tab'], 'pane': caller['pane'],
              'closes_panes': 0, 'starts_members': 0}
    raw = read_file(path)
    if raw is None:
        return {**result, 'decision': 'empty', 'reason': 'no_registration'}
    state = json.loads(raw)
    if not isinstance(state, dict):
        return result
    result['original_error'] = str(state.get('error', ''))[:4000]
    architect = state.get('architect')
    prefix = 's' + path.stem[:8]
    if (type(state.get('schema_version')) is not int or state['schema_version'] != 1
            or state.get('tab') != caller['tab']
            or state.get('prefix') != prefix or not isinstance(state.get('shop_id'), str)
            or not state['shop_id'].startswith(path.stem + '-')
            or not isinstance(architect, dict)
            or architect.get('pane') != caller['pane']
            or architect.get('name') != prefix + '-architect'
            or not isinstance(architect.get('terminal_id'), str) or not architect['terminal_id']
            or not isinstance(state.get('cwd'), str) or not Path(state['cwd']).is_absolute()):
        return result
    if (state.get('phase') != 'partial' or state.get('run_id') is not None
            or state.get('lead') is not None or state.get('workers') != []
            or state.get('extra_leads', []) != []):
        return {**result, 'reason': 'not_early_setup'}
    # Older registrations predate the stage journal. Only a rename of Architect
    # is known to precede the first split. A failed split may have created a pane.
    legacy_rename = re.match(r'^herdr (?:agent|pane) rename ' + re.escape(caller['pane']) + r' ',
                             str(state.get('error', '')))
    if (state.get('setup_stage') != 'architect'
            and not ('setup_stage' not in state and legacy_rename)):
        return {**result, 'reason': 'unknown_setup_stage'}
    repo = Path(state['cwd']).resolve(strict=True)
    shop_dir = repo / '.shop'
    if shop_dir.is_symlink():
        raise RuntimeError('Symlink refused: ' + str(shop_dir))
    bindings_raw = read_file(shop_dir / 'bindings.json')
    bindings = json.loads(bindings_raw) if bindings_raw is not None else {}
    if not isinstance(bindings, dict) or any(not isinstance(b, dict) for b in bindings.values()):
        return {**result, 'reason': 'binding_conflict'}
    if any(b.get('shop_id') == state['shop_id'] or b.get('tab') == state['tab']
           or b.get('state_path') == str(path) for b in bindings.values()):
        return {**result, 'reason': 'binding_conflict'}
    pane = api('pane', 'get', caller['pane'])['pane']
    owner = api('agent', 'get', caller['pane'])['agent']
    for observation in (pane, owner):
        if (observation.get('pane_id') != caller['pane']
                or observation.get('tab_id') != caller['tab']
                or observation.get('terminal_id') != architect['terminal_id']
                or observation.get('agent') != 'pi'):
            return {**result, 'reason': 'identity_changed'}
    result.update(recorded_name=architect['name'], observed_name=owner.get('name'),
                  terminal_id=owner['terminal_id'])
    # A lost display name is explicitly shown for confirmation. A different
    # named agent or a replaced terminal is never adopted by this command.
    if owner.get('name') not in (None, architect['name']):
        return {**result, 'reason': 'identity_changed'}
    observed_cwd = pane.get('foreground_cwd') or pane.get('cwd')
    if not observed_cwd or Path(observed_cwd).resolve() != repo:
        return {**result, 'reason': 'identity_changed'}
    layout = api('pane', 'layout', '--pane', caller['pane'])['layout']
    if [p.get('pane_id') for p in layout.get('panes', [])] != [caller['pane']]:
        return {**result, 'reason': 'extra_panes'}
    agents = api('agent', 'list')['agents']
    if any(a.get('pane_id') != caller['pane'] and str(a.get('name') or '').startswith(prefix + '-')
           for a in agents):
        return {**result, 'reason': 'other_members'}
    result.update(decision='ready', reason='early_setup_only', shop_id=state['shop_id'],
                  state_revision=digest(raw), bindings_revision=digest(bindings_raw or b''))
    result['token'] = digest(json.dumps(result, sort_keys=True).encode())
    return result


def execute(api, path, caller, expected):
    fresh = plan(api, path, caller)
    if fresh.get('decision') != 'ready' or fresh.get('token') != expected:
        raise RuntimeError('Reset preview changed or blocked; nothing reset. Preview again.')
    raw = read_file(path)
    if raw is None or digest(raw) != fresh['state_revision']:
        raise RuntimeError('Registration changed; nothing reset')
    archive = archive_registration(path, raw, expected)
    # Recheck live identity, layout, binding and exact bytes after backup. On
    # failure both original registration and backup remain; never retry writes.
    if plan(api, path, caller).get('token') != expected or read_file(path) != raw:
        raise RuntimeError('Reset facts changed; registration retained. Backup: ' + str(archive))
    path.unlink()
    return {'schema': 1, 'decision': 'archived', 'archive': str(archive),
            'state_revision': fresh['state_revision'], 'pane': caller['pane'],
            'tab': caller['tab'], 'closes_panes': 0, 'starts_members': 0}


def archive_registration(path, raw, token):
    """Durable exact-byte backup, shared by reset and checked orphan retirement."""
    if not re.fullmatch(r'[a-f0-9]{64}', token):
        raise RuntimeError('Invalid archive token')
    archive_dir = path.parent.parent / 'reset-archive'
    if archive_dir.is_symlink():
        raise RuntimeError('Symlink archive directory refused')
    archive_dir.mkdir(mode=0o700, exist_ok=True)
    archive = archive_dir / (path.stem + '-' + token + '.json')
    # Exclusive creation preserves evidence after uncertain/partial outcomes.
    # Flush backup and its directory before removing the active registration.
    archive_fd = os.open(archive, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(archive_fd, 'wb') as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    fd = os.open(archive_dir, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    return archive


def request(api, root, payload, env):
    if env.get('HERDR_ENV') != '1' or not all(env.get(k) for k in
            ('HERDR_SOCKET_PATH', 'HERDR_TAB_ID', 'HERDR_PANE_ID')):
        raise RuntimeError('Reset requires current Herdr Pi pane, tab and socket context')
    caller = {'tab': env['HERDR_TAB_ID'], 'pane': env['HERDR_PANE_ID']}
    key = digest((env['HERDR_SOCKET_PATH'] + ':' + caller['tab']).encode())[:12]
    root = Path(root).resolve()
    runtime = root / 'runtime'
    if runtime.is_symlink():
        raise RuntimeError('Symlink runtime directory refused')
    action = payload.get('action')
    if action not in ('preview', 'apply'):
        raise RuntimeError('Unknown reset action')
    if action == 'apply' and (payload.get('confirmed') is not True
            or not re.fullmatch(r'[a-f0-9]{64}', str(payload.get('token', '')))):
        raise RuntimeError('Reset needs confirmed preview token')
    if not runtime.exists():
        if action == 'apply':
            raise RuntimeError('Registration missing; nothing reset')
        return {'schema': 1, 'decision': 'empty', 'reason': 'no_registration', **caller}
    path = runtime / (key + '.json')
    lock_path = runtime / (key + '.lock')
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'r+') as lock:
        locking.acquire(lock)
        if action == 'preview':
            return plan(api, path, caller)
        return execute(api, path, caller, payload['token'])


def main():
    import settings
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--request', required=True)
    args = parser.parse_args()
    if len(args.request.encode()) > LIMIT:
        raise RuntimeError('Reset request exceeds 64 KiB')
    payload = json.loads(args.request)
    if (not isinstance(payload, dict) or payload.get('expected_state_dir') != str(settings.STATE)
            or payload.get('expected_config_dir') != str(settings.CONFIG)):
        raise RuntimeError('Shop bridge changed; reset refused')
    print(json.dumps(request(herdr.Herdr().api, settings.STATE, payload, os.environ), ensure_ascii=False))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print('shop-reset: ' + str(error), file=sys.stderr)
        sys.exit(1)
