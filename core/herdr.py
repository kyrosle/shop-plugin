"""Single typed Herdr adapter for every Python Herdr CLI/API call.

Rules enforced here, not by callers:

* one subprocess boundary (``Herdr._run``) with bounded timeouts and diagnostics;
* explicit binary version + socket protocol/schema compatibility (``probe``);
* ``plugin.py`` uses this adapter for doctor/status/notification commands; modules
  such as ``messaging.py`` only consume an injected adapter callback. Replacing
  that callback with transport belongs to P2 and is intentionally not done here;
* pane and agent payloads are different schemas: ``pane`` payloads are never
  authoritative for the agent name, ``agent`` payloads are;
* unknown route, unknown field, wrong payload type or a missing/unknown
  identity/status fails closed instead of being treated as healthy.

No transport, no auto-repair, no fallback to prompts. Transport is not
implemented in this package version: ``transport_epoch`` stays null and a
non-null value is refused rather than silently accepted.
"""
import json
import os
import re
import subprocess
import time

BINARY_VERSION = '0.9.0'
PROTOCOL = 22
SCHEMA_VERSION = 1

PROBE_TIMEOUT = 10.0
DEFAULT_TIMEOUT = 30.0
START_TIMEOUT = 180.0
NOTIFY_TIMEOUT = 5.0

AGENT_STATUSES = frozenset({'idle', 'working', 'blocked', 'done', 'unknown'})
SETTLED_STATUSES = ('idle', 'done')

_VERSION_RE = re.compile(r'\bherdr\s+(\d+)\.(\d+)\.(\d+)\b')


class HerdrError(RuntimeError):
    """Base class: any Herdr boundary failure. Callers must treat it as unknown."""


class HerdrUnavailable(HerdrError):
    """Binary missing or not executable."""


class HerdrTimeout(HerdrError):
    """Bounded CLI invocation exceeded its timeout."""


class HerdrIncompatible(HerdrError):
    """Binary version or socket protocol/schema is not the pinned compatible one."""


class HerdrMalformed(HerdrError):
    """Non-JSON/empty/foreign stdout envelope."""


class HerdrSchemaError(HerdrError):
    """Unknown route or payload whose typed fields do not match the schema."""


class HerdrIdentityError(HerdrError):
    """Missing/unknown identity or status where a decision is required."""


def _short(value, limit=400):
    text = ' '.join(str(value or '').split())
    return text[:limit]


_OPTIONAL_STR = (str, type(None))
PANE_SPEC = {
    'pane_id': str, 'tab_id': str, 'workspace_id': str, 'terminal_id': str,
    'agent': (str, type(None)), 'display_agent': _OPTIONAL_STR,
    'agent_status': 'status', 'agent_session': 'session',
    'cwd': _OPTIONAL_STR, 'foreground_cwd': _OPTIONAL_STR, 'label': _OPTIONAL_STR,
    'title': _OPTIONAL_STR, 'terminal_title': _OPTIONAL_STR,
    'terminal_title_stripped': _OPTIONAL_STR,
    'focused': bool, 'revision': int, 'interactive_ready': bool,
    'launch_pending': bool, 'state_change_seq': int,
    'state_labels': dict, 'tokens': dict, 'scroll': 'scroll',
}
# Agent-only keys. A pane payload that carries them is tolerated but they are
# dropped: only the agent endpoint may supply an agent name.
AGENT_ONLY_KEYS = frozenset({'name'})
PANE_IGNORED_KEYS = AGENT_ONLY_KEYS
# Optional observation metadata from Herdr's agent endpoints. It does not
# establish identity or imply any particular agent_status.
AGENT_SPEC = dict(PANE_SPEC, name=_OPTIONAL_STR, screen_detection_skipped=bool)
SESSION_SPEC = {'source': str, 'agent': str, 'kind': str, 'value': str}
SCROLL_SPEC = {'max_offset_from_bottom': int, 'offset_from_bottom': int, 'viewport_rows': int}
LAYOUT_AREA_SPEC = {'x': int, 'y': int, 'width': int, 'height': int}
LAYOUT_PANE_SPEC = {'pane_id': str, 'focused': bool, 'rect': 'rect'}
PROCESS_SPEC = {'pid': int, 'name': str, 'cwd': (str, type(None)),
                'argv0': (str, type(None)), 'argv': (list, type(None)),
                'cmdline': (str, type(None))}
PROCESS_INFO_SPEC = {'pane_id': str, 'shell_pid': (int, type(None)),
                     'foreground_process_group_id': (int, type(None)),
                     'tty': (str, type(None)), 'foreground_processes': 'processes'}
WORKSPACE_SPEC = {'workspace_id': str, 'number': int, 'label': str, 'focused': bool,
                  'pane_count': int, 'tab_count': int, 'active_tab_id': str,
                  'agent_status': 'status', 'tokens': dict, 'worktree': (dict, type(None))}


def _check_dict(record, spec, where):
    if not isinstance(record, dict):
        raise HerdrSchemaError(where + ': expected object, got ' + type(record).__name__)
    out = {}
    for key, value in record.items():
        if key not in spec:
            raise HerdrSchemaError(where + ': unknown field ' + repr(key))
        rule = spec[key]
        if rule == 'status':
            if value not in AGENT_STATUSES:
                raise HerdrSchemaError(where + ': unknown agent_status ' + repr(value))
        elif rule == 'session':
            if value is not None:
                _check_dict(value, SESSION_SPEC, where + '.agent_session')
        elif rule == 'scroll':
            if value is not None:
                _check_dict(value, SCROLL_SPEC, where + '.scroll')
        elif rule == 'rect':
            if value is not None:
                _check_dict(value, LAYOUT_AREA_SPEC, where + '.rect')
        elif rule == 'processes':
            if not isinstance(value, list):
                raise HerdrSchemaError(where + '.foreground_processes: expected list')
            for index, item in enumerate(value):
                _check_dict(item, PROCESS_SPEC, where + '.foreground_processes[%d]' % index)
        elif rule == 'panes':
            if not isinstance(value, list):
                raise HerdrSchemaError(where + '.panes: expected list')
        elif isinstance(rule, tuple):
            if not isinstance(value, rule):
                raise HerdrSchemaError(where + '.' + key + ': unexpected type ' + type(value).__name__)
        elif isinstance(rule, type):
            if not isinstance(value, rule):
                raise HerdrSchemaError(where + '.' + key + ': unexpected type ' + type(value).__name__)
        out[key] = value
    return out


def pane_record(raw, where='pane'):
    """Typed pane payload. Agent-only keys are dropped, never used as identity."""
    if not isinstance(raw, dict):
        raise HerdrSchemaError(where + ': pane payload must be an object')
    cleaned = {k: v for k, v in raw.items() if k not in PANE_IGNORED_KEYS}
    record = _check_dict(cleaned, PANE_SPEC, where)
    if not record.get('pane_id'):
        raise HerdrSchemaError(where + ': pane payload lacks pane_id')
    return record


def agent_record(raw, where='agent'):
    """Typed agent payload. ``name`` may be absent (unnamed agent), never guessed."""
    return _check_dict(raw, AGENT_SPEC, where)


def workspace_record(raw, where='workspace'):
    record = _check_dict(raw, WORKSPACE_SPEC, where)
    if not record.get('workspace_id'):
        raise HerdrSchemaError(where + ': workspace payload lacks workspace_id')
    return record


def layout_record(raw, where='layout'):
    record = _check_dict(raw, {'workspace_id': str, 'tab_id': str, 'zoomed': bool,
                               'focused_pane_id': str, 'area': 'rect',
                               'panes': 'panes', 'splits': list,
                               'focused': bool}, where)
    panes = record.get('panes')
    if not isinstance(panes, list):
        raise HerdrSchemaError(where + ': layout lacks panes list')
    for index, item in enumerate(panes):
        parsed = _check_dict(item, LAYOUT_PANE_SPEC, where + '.panes[%d]' % index)
        if not parsed.get('pane_id'):
            raise HerdrSchemaError(where + '.panes[%d]: lacks pane_id' % index)
    return record


def process_info_record(raw, where='process_info'):
    return _check_dict(raw, PROCESS_INFO_SPEC, where)


def require_status(agent, allowed=None, where='agent'):
    """Fail closed when the agent status is missing or outside the allowed set."""
    status = agent.get('agent_status')
    if status not in AGENT_STATUSES:
        raise HerdrIdentityError(where + ': missing/unknown agent_status ' + repr(status))
    if allowed is not None and status not in allowed:
        raise HerdrIdentityError(where + ': agent_status ' + repr(status)
                                 + ' not in ' + repr(tuple(allowed)))
    return status


def require_text(record, key, where):
    value = record.get(key)
    if not isinstance(value, str) or not value.strip():
        raise HerdrSchemaError(where + ': missing ' + key)
    return value


# Route -> (required payload key, accepted response type names).
# Type names are cross-checked against the pinned protocol schema in probe():
# a table that disagrees with the live schema is an incompatibility, not a warning.
# `pane split` returns the created pane as `.result.pane` (Herdr 0.9.0 skill doc);
# the 0.9.0 binary also carries the `pane_created` name, so both are accepted with
# the same strict pane payload validation.
ROUTES = {
    ('workspace', 'list'): ('workspaces', ('workspace_list',)),
    # Focus routes are declared read-only for link validation only: the snapshot
    # prints the command sequence and the adapter never focuses automatically.
    ('workspace', 'focus'): (None, ('ok', 'workspace_info')),
    ('tab', 'focus'): (None, ('ok', 'tab_info')),
    ('pane', 'list'): ('panes', ('pane_list',)),
    ('pane', 'get'): ('pane', ('pane_info',)),
    ('pane', 'current'): ('pane', ('pane_current',)),
    ('pane', 'layout'): ('layout', ('pane_layout',)),
    ('pane', 'split'): ('pane', ('pane_info', 'pane_created')),
    ('pane', 'close'): (None, ('ok',)),
    ('pane', 'rename'): ('pane', ('pane_info',)),
    ('pane', 'zoom'): (None, ('ok', 'pane_zoom')),
    ('pane', 'process-info'): ('process_info', ('pane_process_info',)),
    ('agent', 'list'): ('agents', ('agent_list',)),
    ('agent', 'get'): ('agent', ('agent_info',)),
    ('agent', 'start'): ('agent', ('agent_started',)),
    ('agent', 'prompt'): ('agent', ('agent_prompted',)),
    ('agent', 'send-keys'): (None, ('ok',)),
    # Rename returns the updated agent, not a generic acknowledgement.
    ('agent', 'rename'): ('agent', ('agent_info',)),
    ('notification', 'show'): (None, ('notification_show',)),
}
# Names not declared by the pinned schema, kept deliberately and only where a
# strict payload validator still runs below.
UNDECLARED_RESPONSE_TYPES = frozenset({'pane_created'})
# Verbs a printed link command may use. Validation is pure: no subprocess, no
# context switch, no focus.
FOCUS_ROUTES = frozenset({('workspace', 'focus'), ('tab', 'focus')})


def validate_focus_command(argv):
    """Validate a printed focus command against the typed routes (no execution).

    Returns the route tuple. Anything else is refused, so a link can never print
    a command the adapter does not actually support.
    """
    if not isinstance(argv, (list, tuple)) or len(argv) != 4:
        raise HerdrSchemaError('focus command must be [herdr, <scope>, focus, <id>]')
    head, scope, verb, target = argv
    if head != 'herdr' or verb != 'focus' or (scope, verb) not in FOCUS_ROUTES:
        raise HerdrSchemaError('unsupported focus command ' + repr(list(argv)))
    if (scope, verb) not in ROUTES:
        raise HerdrSchemaError('focus route is not declared by the adapter: ' + repr(scope))
    if not isinstance(target, str) or not target.strip():
        raise HerdrSchemaError('focus target must be a nonempty id')
    return (scope, verb)


class Runtime(object):
    """Verified binary/protocol identity for one Herdr instance."""

    def __init__(self, binary, version, protocol, schema_version):
        self.binary = binary
        self.version = version
        self.protocol = protocol
        self.schema_version = schema_version

    def as_dict(self):
        return {'binary': self.binary, 'version': self.version, 'protocol': self.protocol,
                'schema_version': self.schema_version, 'supported_version': BINARY_VERSION,
                'compatible': True}


class Herdr(object):
    def __init__(self, binary=None, timeout=DEFAULT_TIMEOUT, runner=None):
        self.binary = binary or os.environ.get('SHOP_HERDR_BIN') or os.environ.get('HERDR_BIN_PATH') or 'herdr'
        self.timeout = float(timeout)
        self.runner = runner or subprocess.run
        self._runtime = None

    # ---------------------------------------------------------------- boundary
    def _run(self, argv, timeout):
        start = time.monotonic()
        try:
            done = self.runner(argv, text=True, capture_output=True, timeout=timeout)
        except FileNotFoundError as error:
            raise HerdrUnavailable('herdr binary not found (' + self.binary + '): ' + _short(error))
        except subprocess.TimeoutExpired:
            raise HerdrTimeout('herdr call timed out after ' + str(timeout) + 's: '
                               + ' '.join(argv[1:]))
        except OSError as error:
            raise HerdrUnavailable('herdr call failed (' + self.binary + '): ' + _short(error))
        return done, time.monotonic() - start

    def _call_raw(self, args, timeout=None):
        argv = [self.binary, *[str(a) for a in args]]
        done, elapsed = self._run(argv, self.timeout if timeout is None else timeout)
        where = 'herdr ' + ' '.join(argv[1:]) + ' [exit=' + str(done.returncode) \
                + ' elapsed=%.2fs]' % elapsed
        text = (done.stdout or '').strip()
        if not text:
            raise HerdrMalformed(where + ': empty stdout; stderr=' + _short(done.stderr))
        try:
            data = json.loads(text)
        except ValueError:
            raise HerdrMalformed(where + ': non-JSON stdout: ' + _short(text))
        if not isinstance(data, dict):
            raise HerdrMalformed(where + ': expected JSON object envelope')
        error = data.get('error')
        if error is not None or done.returncode:
            code = error.get('code') if isinstance(error, dict) else None
            message = error.get('message') if isinstance(error, dict) else (done.stderr or text)
            raise HerdrError(where + ': error ' + str(code) + ': ' + _short(message))
        result = data.get('result')
        if not isinstance(result, dict):
            raise HerdrMalformed(where + ': envelope lacks result object')
        return result, where

    def probe(self, refresh=False):
        """Verify binary version and socket protocol/schema; cache per process."""
        if self._runtime is not None and not refresh:
            return self._runtime
        done, _ = self._run([self.binary, '--version'], PROBE_TIMEOUT)
        text = ((done.stdout or '') + ' ' + (done.stderr or '')).strip()
        if done.returncode:
            raise HerdrUnavailable('herdr --version failed (' + self.binary + '): ' + _short(text))
        match = _VERSION_RE.search(text)
        if not match:
            raise HerdrIncompatible('unparsable herdr --version output: ' + _short(text))
        version = '.'.join(match.groups())
        if version != BINARY_VERSION:
            raise HerdrIncompatible('herdr ' + version + ' is not compatible with pinned '
                                    + BINARY_VERSION + ' (' + self.binary + '); update the adapter deliberately')
        # `api schema --json` prints the raw schema document, not the CLI envelope.
        done, _ = self._run([self.binary, 'api', 'schema', '--json'], PROBE_TIMEOUT)
        where = 'herdr api schema --json [exit=' + str(done.returncode) + ']'
        text = (done.stdout or '').strip()
        try:
            schema = json.loads(text)
        except ValueError:
            raise HerdrMalformed(where + ': non-JSON schema output: ' + _short(text or done.stderr))
        if not isinstance(schema, dict) or done.returncode:
            raise HerdrMalformed(where + ': unexpected schema output')
        protocol, schema_version = schema.get('protocol'), schema.get('schema_version')
        if not isinstance(protocol, int) or not isinstance(schema_version, int):
            raise HerdrIncompatible(where + ': schema metadata lacks integer protocol/schema_version')
        if protocol != PROTOCOL or schema_version != SCHEMA_VERSION:
            raise HerdrIncompatible('herdr protocol/schema ' + str(protocol) + '/' + str(schema_version)
                                    + ' != pinned ' + str(PROTOCOL) + '/' + str(SCHEMA_VERSION)
                                    + ' (' + self.binary + ')')
        declared = _declared_response_types(schema)
        for route, (key, types) in sorted(ROUTES.items()):
            for name in types:
                if name in UNDECLARED_RESPONSE_TYPES:
                    continue
                if name not in declared:
                    raise HerdrIncompatible('protocol ' + str(protocol) + ' does not declare response type '
                                            + name + ' for ' + repr(route) + ' (' + self.binary + ')')
                if key is not None and key not in declared[name]:
                    raise HerdrIncompatible('protocol ' + str(protocol) + ' response type ' + name
                                            + ' does not declare payload key ' + key
                                            + ' for ' + repr(route))
        self._runtime = Runtime(self.binary, version, protocol, schema_version)
        return self._runtime

    # ------------------------------------------------------------------ routing
    def call(self, *args, **kwargs):
        """Typed dispatch. Unknown route or mismatched payload fails closed."""
        timeout = kwargs.pop('timeout', None)
        if kwargs:
            raise HerdrSchemaError('unexpected arguments: ' + repr(sorted(kwargs)))
        if len(args) < 2:
            raise HerdrSchemaError('Herdr route requires namespace and verb')
        route = (args[0], args[1])
        if route not in ROUTES:
            raise HerdrSchemaError('unsupported Herdr route ' + repr(route)
                                   + '; add it to the adapter before use')
        self.probe()
        result, where = self._call_raw(args, timeout)
        key, allowed_types = ROUTES[route]
        payload_type = result.get('type')
        if payload_type not in allowed_types:
            raise HerdrSchemaError(where + ': expected response type in ' + repr(allowed_types)
                                   + ', got ' + repr(payload_type))
        if key is None:
            if route == ('notification', 'show'):
                parsed = _check_dict(result, {'type': str, 'shown': bool,
                                              'reason': _OPTIONAL_STR}, where + '.result')
                for field in ('shown', 'reason'):
                    if field not in parsed:
                        raise HerdrSchemaError(where + '.result: missing ' + field)
                return parsed
            return result
        if key not in result:
            raise HerdrSchemaError(where + ': ' + str(payload_type) + ' payload lacks ' + key)
        value = result[key]
        if key == 'panes':
            return {'panes': [pane_record(item, where + '.panes[%d]' % i)
                              for i, item in enumerate(_require_list(value, where, key))]}
        if key == 'agents':
            return {'agents': [agent_record(item, where + '.agents[%d]' % i)
                               for i, item in enumerate(_require_list(value, where, key))]}
        if key == 'workspaces':
            return {'workspaces': [workspace_record(item, where + '.workspaces[%d]' % i)
                                   for i, item in enumerate(_require_list(value, where, key))]}
        if key == 'process_info':
            return {'process_info': process_info_record(value, where + '.process_info')}
        if key == 'layout':
            return {'layout': layout_record(value, where + '.layout')}
        if key == 'agent':
            return {'agent': agent_record(value, where + '.agent')}
        return {'pane': pane_record(value, where + '.pane')}

    # Compatibility entry point used by the rest of the package.
    def api(self, *args):
        return self.call(*args)

    # ------------------------------------------------------------- typed reads
    def workspaces(self):
        return self.call('workspace', 'list')['workspaces']

    def panes(self, workspace_id):
        return self.call('pane', 'list', '--workspace', workspace_id)['panes']

    def pane(self, pane_id):
        return self.call('pane', 'get', pane_id)['pane']

    def current_pane(self):
        return self.call('pane', 'current', '--current')['pane']

    def layout(self, pane_id):
        return self.call('pane', 'layout', '--pane', pane_id)['layout']

    def process_info(self, pane_id):
        return self.call('pane', 'process-info', '--pane', pane_id)['process_info']

    def agents(self):
        return self.call('agent', 'list')['agents']

    def agent(self, target):
        return self.call('agent', 'get', target)['agent']

    def split(self, pane_id, direction, cwd=None, focus=False):
        args = ['pane', 'split', '--pane', pane_id, '--direction', direction]
        if cwd:
            args += ['--cwd', str(cwd)]
        if not focus:
            args.append('--no-focus')
        return self.call(*args)['pane']

    def close_pane(self, pane_id):
        return self.call('pane', 'close', pane_id)

    def rename_pane(self, pane_id, label):
        return self.call('pane', 'rename', pane_id, label)

    def focus_workspace(self, workspace_id):
        """Explicit-only: the snapshot never calls this, and no path auto-focuses."""
        return self.call('workspace', 'focus', workspace_id)

    def focus_tab(self, tab_id):
        """Explicit-only: the snapshot never calls this, and no path auto-focuses."""
        return self.call('tab', 'focus', tab_id)

    def zoom(self, pane_id, on=False):
        return self.call('pane', 'zoom', '--pane', pane_id, '--on' if on else '--off')

    def start_agent(self, name, kind, pane_id, session_dir, model, prompt_path, timeout_ms=120000, thinking=None):
        effort = [] if thinking is None else ['--thinking', thinking]
        return self.call('agent', 'start', name, '--kind', kind, '--pane', pane_id,
                         '--timeout', str(timeout_ms), '--', '--session-dir', str(session_dir),
                         '--model', model, *effort, '--append-system-prompt', str(prompt_path),
                         timeout=START_TIMEOUT)['agent']

    def prompt(self, target, text):
        return self.call('agent', 'prompt', target, text)

    def send_keys(self, target, *keys):
        return self.call('agent', 'send-keys', target, *keys)

    def rename_agent(self, target, name):
        return self.call('agent', 'rename', target, name)

    def server_status(self):
        """Bounded doctor diagnostic for Herdr server; never expose subprocess objects."""
        runtime = self.probe()
        done, elapsed = self._run([self.binary, 'status', 'server'], PROBE_TIMEOUT)
        return {'binary': runtime.binary, 'code': done.returncode,
                'output': ((done.stdout or '') + (done.stderr or ''))[:3000],
                'elapsed_seconds': round(elapsed, 3)}

    def notify(self, title, body):
        """Best-effort typed notification; failure must not mask operation result."""
        try:
            result = self.call('notification', 'show', title, '--body', body,
                               '--sound', 'none', timeout=NOTIFY_TIMEOUT)
            return result['shown']
        except HerdrError:
            return False


def _declared_response_types(schema):
    """Map response type name -> required payload keys from the pinned schema."""
    try:
        variants = schema['schemas']['success_response']['$defs']['ResponseResult']['oneOf']
    except (KeyError, TypeError):
        raise HerdrIncompatible('schema lacks success_response.ResponseResult union')
    declared = {}
    for variant in variants:
        name = variant.get('properties', {}).get('type', {}).get('const')
        if isinstance(name, str):
            declared[name] = frozenset(variant.get('required', []))
    if not declared:
        raise HerdrIncompatible('schema declares no response types')
    return declared


def _require_list(value, where, key):
    if not isinstance(value, list):
        raise HerdrSchemaError(where + ': ' + key + ' must be a list')
    return value
