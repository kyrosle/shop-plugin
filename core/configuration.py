"""Versioned Shop settings. Files and inheritance owned here; Pi owns session entries."""
import argparse
from contextlib import contextmanager
import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time

import settings

LIMIT = 65536
VERSION = 1
CANDIDATE_TTL = 20


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def read_document(path):
    path = Path(path)
    try:
        with path.open('rb') as stream:
            raw = stream.read(LIMIT + 1)
    except FileNotFoundError:
        return {}, 'missing'
    if len(raw) > LIMIT:
        raise RuntimeError('Configuration exceeds 64 KiB: ' + str(path))
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise RuntimeError('Expected configuration object: ' + str(path))
    return data, digest(raw)


def document_models(data):
    if not data:
        return {}
    if type(data.get('version')) is not int or data['version'] != VERSION:
        raise RuntimeError('Unsupported Shop settings version')
    value = data.get('models', {})
    settings._model_layers(value)
    return value


def session_models(data):
    if not isinstance(data, dict) or set(data) - {'models'}:
        raise RuntimeError('Session overrides accept only models')
    value = data.get('models', {})
    settings._model_layers(value)
    return value


def expand(models):
    layers, seats = settings._model_layers(models)
    return {seat: {**layers['defaults'], **layers[seat.split('-')[0]], **seats.get(seat, {})}
            for seat in settings.MODEL_SEATS}


def sparse(draft, parent):
    if not isinstance(draft, dict) or set(draft) != set(settings.MODEL_SEATS):
        raise RuntimeError('Draft must contain exactly four execution seats')
    result = {}
    for seat in settings.MODEL_SEATS:
        profile = settings._profile(draft[seat], seat)
        # An absent model cannot hide an inherited model. A null thinking value
        # is equivalent to the built-in Pi default, but can override explicit effort.
        changes = {key: value for key, value in profile.items() if value != parent[seat].get(key)}
        if changes:
            result[seat] = changes
    return {'models': {'seats': result}} if result else {}


def paths(root, config_dir, project_dir='.pi'):
    if not Path(root).is_absolute():
        raise RuntimeError('Absolute project root required')
    if (not isinstance(project_dir, str) or not project_dir.startswith('.')
            or '/' in project_dir or '\\' in project_dir or project_dir in ('.', '..')):
        raise RuntimeError('Invalid project configuration directory')
    return {'global': Path(config_dir) / 'settings.json',
            'project': Path(root).resolve() / project_dir / 'shop.json',
            'legacy': Path(config_dir) / 'models.json'}


def load(root, trusted, session, config_dir=None, project_dir='.pi'):
    if type(trusted) is not bool:
        raise RuntimeError('Explicit project trust decision required')
    locations = paths(root, config_dir or settings.CONFIG, project_dir)
    global_doc, global_rev = read_document(locations['global'])
    legacy_rev = 'unused'
    legacy = global_rev == 'missing' and locations['legacy'].exists()
    if legacy:
        legacy_data, legacy_rev = read_document(locations['legacy'])
        settings._model_layers(legacy_data)
        global_models = legacy_data
    else:
        if global_rev != 'missing' and not global_doc:
            raise RuntimeError('Missing Shop settings version')
        global_models = document_models(global_doc)
    project_doc, project_rev = read_document(locations['project']) if trusted else ({}, 'untrusted')
    if project_rev not in ('missing', 'untrusted') and not project_doc:
        raise RuntimeError('Missing Shop settings version')
    model_layers = {'global': global_models, 'project': document_models(project_doc),
                    'session': session_models(session)}
    profiles = {seat: {} for seat in settings.MODEL_SEATS}
    sources = {seat: {} for seat in settings.MODEL_SEATS}
    views = {}
    for scope, models in model_layers.items():
        parent = copy.deepcopy(profiles)
        for seat, profile in expand(models).items():
            profiles[seat].update(profile)
            sources[seat].update({key: 'legacy' if scope == 'global' and legacy else scope for key in profile})
        views[scope] = {'profiles': copy.deepcopy(profiles), 'parent': parent,
                        'sources': copy.deepcopy(sources), 'overrides': sparse(profiles, parent)}
    return {'version': VERSION, 'root': str(Path(root).resolve()), 'trusted': trusted,
            'paths': {key: str(value) for key, value in locations.items()},
            'revisions': {'global': global_rev, 'project': project_rev, 'legacy': legacy_rev},
            'legacy': legacy, 'layers': views}


@contextmanager
def file_lock(path):
    lock_path = Path(str(path) + '.lock')
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError('Configuration is being edited; reopen /shop-config') from error
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def atomic_document(path, data):
    raw = (json.dumps(data, ensure_ascii=False, indent=2) + '\n').encode()
    if len(raw) > LIMIT:
        raise RuntimeError('Configuration exceeds 64 KiB')
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        Path(temp).unlink(missing_ok=True)


def transaction(request, config_dir=None):
    root, trusted = request['root'], request['trusted']
    project_dir = request.get('project_dir', '.pi')
    locations = paths(root, config_dir or settings.CONFIG, project_dir)
    # One bridge/config-dir lock serializes participating global/project writers.
    # Never create lock files in the user's worktree merely to preview/session-save.
    with file_lock(locations['global']):
        view = load(root, trusted, request.get('session', {}), config_dir, project_dir)
        if request.get('revisions') != view['revisions']:
            raise RuntimeError('Configuration changed; reopen /shop-config (no write)')
        action = request['action']
        if action == 'migrate':
            if not view['legacy'] or view['revisions']['global'] != 'missing':
                raise RuntimeError('No legacy-only configuration to migrate; existing settings never overwritten')
            legacy, revision = read_document(locations['legacy'])
            if revision != view['revisions']['legacy']:
                raise RuntimeError('Legacy configuration changed; reopen /shop-config')
            proposed = {'version': VERSION, 'models': legacy}
            if request.get('apply') is True:
                atomic_document(locations['global'], proposed)
            return {'proposed': proposed, 'applied': request.get('apply') is True,
                    'backup': str(locations['legacy'])}
        scope = request['scope']
        if scope not in ('global', 'project', 'session') or scope == 'project' and not trusted:
            raise RuntimeError('Scope unavailable or project untrusted')
        if scope == 'global' and view['legacy']:
            raise RuntimeError('Migrate legacy models.json first: /shop-config migrate')
        overrides = {} if request.get('reset') is True else sparse(request['draft'], view['layers'][scope]['parent'])
        if action == 'save':
            if scope == 'session':
                raise RuntimeError('Session persistence belongs to Pi custom entries')
            raw, _ = read_document(locations[scope])
            raw.update(version=VERSION)
            raw.pop('models', None)
            raw.update(overrides)
            atomic_document(locations[scope], raw)
        elif action != 'prepare':
            raise RuntimeError('Unknown settings action')
        return {'overrides': overrides, 'scope': scope, 'saved': action == 'save'}


def candidate_path(state_dir, socket, tab, pane):
    key = digest((socket + ':' + tab + ':' + pane).encode())[:24]
    return Path(state_dir) / 'config-candidates' / (key + '.json')


def startup_candidate(api, pane, cwd, socket, state_dir=None):
    """Read a fresh identity claim; expiry means unknown, never permission to delete."""
    path = candidate_path(state_dir or settings.STATE, socket, pane['tab_id'], pane['pane_id'])
    record, revision = read_document(path)
    if revision == 'missing':
        raise RuntimeError('Missing Pi settings candidate; /reload or /shop-config in Architect (--models-file does not bypass session ownership)')
    expected = {'schema': 1, 'socket': socket, 'tab': pane['tab_id'], 'pane': pane['pane_id'],
                'root': str(Path(cwd).resolve())}
    if type(record.get('schema')) is not int or any(record.get(key) != value for key, value in expected.items()):
        raise RuntimeError('Settings candidate identity/root mismatch')
    now = time.time()
    updated = record.get('updated_at')
    if type(updated) not in (float, int) or not 0 <= now - updated <= CANDIDATE_TTL:
        raise RuntimeError('Settings candidate expired; refresh original Architect Pi')
    for field in ('terminal', 'session_id', 'instance'):
        if not isinstance(record.get(field), str) or not 0 < len(record[field]) <= 256:
            raise RuntimeError('Settings candidate missing ' + field)
    pid = record.get('pid')
    if type(pid) is not int or pid <= 0:
        raise RuntimeError('Settings candidate PID invalid')
    try:
        os.kill(pid, 0)
    except OSError as error:
        raise RuntimeError('Settings candidate Pi process unavailable') from error
    live = api('agent', 'get', pane['pane_id'])['agent']
    if (live.get('agent') != 'pi' or live.get('terminal_id') != record['terminal']
            or live.get('tab_id') != pane['tab_id']):
        raise RuntimeError('Settings candidate live instance mismatch')
    return record


def startup_profiles(api, pane, cwd, socket, state_dir=None, config_dir=None):
    """Consume one recent Pi claim; never read transcripts or guess another pane."""
    record = startup_candidate(api, pane, cwd, socket, state_dir)
    path = candidate_path(state_dir or settings.STATE, socket, pane['tab_id'], pane['pane_id'])
    view = load(cwd, record.get('trusted'), record.get('overrides', {}), config_dir,
                record.get('project_dir', '.pi'))
    profiles = settings.resolve_models({'seats': view['layers']['session']['profiles']})
    latest, _ = read_document(path)
    stable = ('schema', 'socket', 'tab', 'pane', 'root', 'terminal', 'session_id',
              'instance', 'pid', 'trusted', 'project_dir', 'overrides', 'settings_entry_id')
    if any(latest.get(key) != record.get(key) for key in stable):
        raise RuntimeError('Settings candidate changed during setup; retry explicitly')
    refreshed = latest.get('updated_at')
    if type(refreshed) not in (float, int) or not 0 <= time.time() - refreshed <= CANDIDATE_TTL:
        raise RuntimeError('Settings candidate expired during setup')
    if load(cwd, record['trusted'], record.get('overrides', {}), config_dir,
            record.get('project_dir', '.pi'))['revisions'] != view['revisions']:
        raise RuntimeError('Configuration changed during setup; retry explicitly')
    return profiles, {'kind': 'pi-scoped', 'session_id': record['session_id'],
                      'instance': record['instance'], 'root': record['root'],
                      'revisions': view['revisions'], 'sources': view['layers']['session']['sources']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--request', required=True, help='Bounded JSON request, no credentials')
    args = parser.parse_args()
    if len(args.request.encode()) > LIMIT:
        raise RuntimeError('Settings request exceeds 64 KiB')
    request = json.loads(args.request)
    for key, actual in (('expected_config_dir', settings.CONFIG), ('expected_state_dir', settings.STATE)):
        if key in request and Path(request[key]).resolve() != actual.resolve():
            raise RuntimeError('Shop bridge changed; reload configuration')
    action = request['action']
    if action == 'load':
        result = load(request['root'], request['trusted'], request.get('session', {}),
                      project_dir=request.get('project_dir', '.pi'))
    elif action == 'identify':
        # Reuse the current core's sole Herdr wrapper; never launch/control agents.
        from shop import api
        pane = os.environ.get('HERDR_PANE_ID')
        tab = os.environ.get('HERDR_TAB_ID')
        if os.environ.get('HERDR_ENV') != '1' or not pane or not tab:
            raise RuntimeError('Herdr Pi identity required')
        live = api('agent', 'get', pane)['agent']
        if live.get('agent') != 'pi' or live.get('tab_id') != tab or not live.get('terminal_id'):
            raise RuntimeError('Cannot verify current Pi terminal identity')
        result = {'terminal': live['terminal_id']}
    else:
        result = transaction(request)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print('shop-config: ' + str(error), file=sys.stderr)
        sys.exit(1)
