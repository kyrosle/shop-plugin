#!/usr/bin/env python3
"""Opt-in real Herdr + Pi smoke gate. No user config, credentials or paid inference.

Default is a plan. --run creates a private headless server, real Pi PTYs and a
throwaway Git project. A failed host assertion is a nonzero exit, never a skip.
Artifacts remain outside the repository; only this run's server is stopped.

Live scenarios (live-smoke, live-delegation) are the only paid path: they need
an explicit --live-model, copy exactly one API-key credential (never OAuth) into
the private root, enforce a cost/call budget and delete the credential afterwards.
"""
import argparse
import hashlib
import json
import os
import re
from pathlib import Path
import shlex
import shutil
import signal
import statistics
import subprocess
import sys
import tempfile
import time
import uuid

REPO = Path(__file__).resolve().parents[2]
REQUIRED_COMMANDS = {'shop', 'shop-ui', 'shop-config', 'shop-language', 'shop-status', 'shop-reset'}
FIXTURE_MODEL = 'shop-host-fixture/no-network'
THINKING_LEVELS = ('off', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max')
LIVE_SCENARIOS = ('live-smoke', 'live-delegation', 'live-seats', 'live-fidelity')
HANDOFF_MODES = ('auto', 'raw', 'curate', 'brief')
LIVE_TOKEN = 'SHOP_LIVE_OK'
FIXTURE_TEXT = 'Host smoke fixture; no business repository.'
# Same work as LIVE_TASK, phrased for the ephemeral-seat flow (/shop-spec, /shop-go).
SEATS_TASK = ('Delegation test. A Worker must read fixture.txt in this repository and report in one sentence '
              'what it says. The Lead must dispatch it with shop_spawn_worker and must not read the file itself. '
              'Do not modify files.')
# Deliberately forces the Worker path: a trivial task lets Lead do it alone.
LIVE_TASK = ('Delegation test. The primary Lead must dispatch this as one analysis ticket to the Worker '
             'with shop_dispatch (not do it itself), then review and accept the Worker result. '
             'Task: read fixture.txt in this repository and report in one sentence what it says. '
             'Do not modify files.')


def mentions_fixture(text):
    """Models wrap Markdown lines; compare with whitespace and case normalized."""
    normalize = lambda value: ' '.join(value.split()).lower()
    return normalize(FIXTURE_TEXT) in normalize(text or '')


# S2: a constraint and a rejected alternative that live only in the Architect
# conversation. SPEC/PLAN deliberately omit them, so only the handoff can carry them.
FIDELITY_DISCUSSION = (
    'Context for an upcoming task; just discuss, do not use tools. We will produce a report about fixture.txt. '
    'My hard requirement: the report file must be named report.json in the repository root and be a JSON object '
    'with exactly two keys, "file" and "first_line". Acknowledge in one sentence.',
    'Alternative I considered: a YAML file report.yaml with a single key named summary. Compare the two in two '
    'sentences; do not use tools.',
    'Decision: reject the YAML/summary idea entirely. Keep report.json with exactly the keys file and first_line. '
    'Acknowledge in one sentence; do not use tools or write files.',
)
FIDELITY_SPEC = """# SPEC

Objective: produce the report about fixture.txt that the user and the Architect agreed on in their discussion.

Constraints: the report's file name, format and exact keys are as agreed in that discussion; they are
intentionally not repeated here. Do not modify fixture.txt.

Acceptance: the agreed report file exists in the repository root with the agreed format and keys, and its
content reflects fixture.txt.
"""
FIDELITY_PLAN = """# PLAN

1. Worker: read fixture.txt and write the agreed report file in the repository root, in the format agreed in the
   discussion. Acceptance: the file exists with exactly the agreed keys.
2. Lead: verify the file against the agreement, then report.
"""


class BudgetExceeded(RuntimeError):
    pass


def parse_thinking(text):
    """architect=max,lead=high,worker=low; omitted seats stay off."""
    result = {'architect': 'off', 'lead': 'off', 'worker': 'off'}
    for part in filter(None, (text or '').split(',')):
        seat, _, level = part.strip().partition('=')
        if seat not in result or level not in THINKING_LEVELS:
            raise ValueError('Invalid thinking entry ' + repr(part) + '; use seat=level for architect/lead/worker')
        result[seat] = level
    return result


def live_credential(provider, auth_path):
    """Exactly one API-key entry. OAuth refresh tokens are never copied: a test
    refresh could rotate and invalidate the user's own login."""
    try:
        entry = json.loads(Path(auth_path).read_text()).get(provider)
    except (OSError, ValueError) as error:
        raise RuntimeError('Cannot read Pi credentials: ' + type(error).__name__)
    if not isinstance(entry, dict):
        raise RuntimeError('No stored Pi credential for provider ' + provider)
    if entry.get('type') != 'api_key' or not isinstance(entry.get('key'), str) or not entry['key']:
        raise RuntimeError('Provider ' + provider + ' is not an API-key credential; OAuth is never copied into tests')
    return {provider: {'type': 'api_key', 'key': entry['key']}}


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    path.chmod(0o600)


def isolated_env(root, paths):
    """Allowlist, never copy os.environ (keys, proxies, sockets and agent context)."""
    home = root / 'home'
    return {
        'HOME': str(home), 'XDG_CONFIG_HOME': str(home / '.config'),
        'XDG_CACHE_HOME': str(home / '.cache'), 'XDG_DATA_HOME': str(home / '.local/share'),
        'XDG_STATE_HOME': str(home / '.local/state'), 'TMPDIR': str(root / 'tmp'),
        'PATH': os.pathsep.join([str(root / 'bin'), *dict.fromkeys(str(Path(p).parent) for p in paths), '/usr/bin', '/bin']),
        'SHELL': '/bin/bash', 'TERM': 'xterm-256color', 'LANG': 'en_US.UTF-8',
        'HERDR_CONFIG_PATH': str(root / 'herdr/config.toml'),
        'HERDR_SOCKET_PATH': str(root / 'herdr/h.sock'),
        'PI_CODING_AGENT_DIR': str(root / 'pi'), 'PI_CODING_AGENT_SESSION_DIR': str(root / 'sessions'),
        'PI_OFFLINE': '1', 'PI_TELEMETRY': '0', 'PI_SKIP_VERSION_CHECK': '1',
        'SHOP_LOCATOR': str(root / 'bridge.json'), 'SHOP_CONFIG_DIR': str(root / 'config'),
        'SHOP_STATE_DIR': str(root / 'state'), 'SHOP_HOST_TEST_ROOT': str(root),
        'SHOP_HERDR_BIN': str(root / 'bin/herdr-proxy'), 'GIT_CONFIG_NOSYSTEM': '1',
        'GIT_CONFIG_GLOBAL': str(root / 'empty-gitconfig'), 'GIT_OPTIONAL_LOCKS': '0',
        'PYTHONDONTWRITEBYTECODE': '1', 'NO_COLOR': '1',
    }


class HostTest:
    def __init__(self, binaries, timeout=30, live=None, credential=None):
        # Short, canonical path also avoids Unix socket length limits on macOS.
        self.root = Path(tempfile.mkdtemp(prefix='shop-host-', dir='/tmp')).resolve()
        self.root.chmod(0o700)
        self.binaries, self.timeout = binaries, timeout
        self.live, self.credential = live, credential
        self.architect_model = live['model'] if live else FIXTURE_MODEL
        # Code the host loads. Live seats have bash, so they get a private snapshot
        # instead of absolute paths into the developer's checkout.
        self.package = self.root / 'package' if live else REPO
        self.env = isolated_env(self.root, binaries.values())
        self.server = None
        self.log = None
        self.pane = self.tab = None
        self.steps = []
        self.report = {'schema': 'shop.host-test/v1', 'root': str(self.root), 'steps': self.steps,
                       'provider_policy': ('live: one API-key provider, budget-capped, credential deleted after run'
                                           if live else 'local-fixture-only; no user credentials'),
                       'model': self.architect_model,
                       'real_herdr': False, 'real_pi_tui': False, 'result': 'running',
                       'not_covered': ['real provider quality', 'full ticket delivery/acceptance',
                                       'worktree integration', 'visual screenshot comparison',
                                       'successful repeated-close acknowledgement']}
        if live:
            self.report['live'] = {key: live[key] for key in ('model', 'thinking', 'budget_usd', 'max_calls', 'handoff_mode')
                                   if key in live}
            # Inherited by every pane the private server starts, including seats.
            self.env['SHOP_HANDOFF_MODE'] = live.get('handoff_mode', 'auto')
        write_json(self.root / 'owned-test.json', {'nonce': uuid.uuid4().hex, 'runner_pid': os.getpid(),
                                                  'socket': self.env['HERDR_SOCKET_PATH']})

    def save(self):
        write_json(self.root / 'report.json', self.report)

    def step(self, name, function):
        started = time.monotonic()
        row = {'name': name, 'status': 'running'}
        self.steps.append(row)
        self.save()
        try:
            value = function()
            row.update(status='passed', seconds=round(time.monotonic() - started, 3))
            self.save()
            print('PASS', name, flush=True)
            return value
        except BaseException as error:
            row.update(status='failed', detail=str(error) or type(error).__name__, seconds=round(time.monotonic() - started, 3))
            self.save()
            raise

    def command(self, argv, env=None, check=True, timeout=None):
        result = subprocess.run([str(a) for a in argv], cwd=self.root / 'project',
                                env=self.env if env is None else env, capture_output=True,
                                text=True, timeout=timeout or self.timeout)
        with (self.root / 'commands.jsonl').open('a') as stream:
            stream.write(json.dumps({'argv': list(map(str, argv)), 'code': result.returncode,
                                     'stdout': result.stdout[-40000:], 'stderr': result.stderr[-12000:]}) + '\n')
        if check and result.returncode:
            raise RuntimeError(f'Command exit {result.returncode}: {argv}\n{result.stderr[-2000:]}')
        return result

    def api(self, *args):
        # Only this instance's private socket; no implicit current/focused target.
        if not Path(self.env['HERDR_SOCKET_PATH']).is_relative_to(self.root):
            raise RuntimeError('Endpoint escaped test root')
        result = self.command([self.binaries['herdr'], *args])
        parsed = json.loads(result.stdout)
        if 'error' in parsed:
            raise RuntimeError(str(parsed['error']))
        return parsed['result']

    def wait(self, predicate, label, timeout=None):
        deadline = time.monotonic() + (timeout or self.timeout)
        while time.monotonic() < deadline:
            if self.server and self.server.poll() is not None:
                raise RuntimeError('Owned Herdr server exited')
            self.check_budget()
            value = predicate()
            if value:
                return value
            time.sleep(0.15)
        raise TimeoutError(label)

    def usage_records(self):
        path = self.root / 'live-usage.jsonl'
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.exists() else []

    def usage(self):
        rows = self.usage_records()
        total = lambda key: sum((row.get('usage') or {}).get(key, 0) or 0 for row in rows)
        cost = sum(((row.get('usage') or {}).get('cost') or {}).get('total', 0) or 0 for row in rows)
        return {'calls': len(rows), 'input': total('input'), 'output': total('output'),
                'cache_read': total('cacheRead'), 'cost_usd': round(cost, 6)}

    def check_budget(self):
        if not self.live:
            return
        used = self.usage()
        self.report['live']['usage'] = used
        if used['cost_usd'] > self.live['budget_usd'] or used['calls'] > self.live['max_calls']:
            raise BudgetExceeded('Live budget exceeded: ' + json.dumps(used))

    def scrub_credentials(self):
        auth = self.root / 'pi/auth.json'
        auth.unlink(missing_ok=True)
        self.report['credential_scrubbed'] = not auth.exists()

    def prepare(self):
        for name in ('home', 'herdr', 'pi', 'sessions', 'tmp', 'bin', 'config', 'state', 'project', 'observations'):
            (self.root / name).mkdir(mode=0o700)
        (self.root / 'empty-gitconfig').touch()
        (self.root / 'herdr/config.toml').write_text('''onboarding = false
[update]
version_check = false
manifest_check = false
[terminal]
default_shell = "/bin/bash"
shell_mode = "non_login"
[server]
headless_cols = 240
headless_rows = 70
[ui.sound]
enabled = false
[session]
resume_agents_on_restore = false
''')
        self.report['versions'] = {}
        for binary in ('herdr', 'pi'):
            version = self.command([self.binaries[binary], '--version']).stdout.strip()
            self.report['versions'][binary] = version
            # Minimum only; the adapter probe gates protocol/schema/response types.
            match = re.fullmatch(r'(?:herdr )?(\d+)\.(\d+)\.(\d+)', version)
            if binary == 'herdr' and (not match or tuple(map(int, match.groups())) < (0, 9, 0)):
                raise RuntimeError(f'Unsupported Herdr version {version}; need >= 0.9.0')
            if binary == 'pi' and not re.fullmatch(r'\d+\.\d+\.\d+(?:[-+].+)?', version):
                raise RuntimeError(f'Unexpected Pi version response: {version}')
        provider, model = self.architect_model.split('/', 1)
        architect_thinking = self.live['thinking']['architect'] if self.live else 'off'
        write_json(self.root / 'pi/settings.json', {'defaultProvider': provider, 'defaultModel': model,
                                                   'defaultThinkingLevel': architect_thinking, 'quietStartup': False})
        write_json(self.root / 'pi/auth.json', self.credential or {})
        if self.live:
            listed = self.command(['git', '-C', REPO, 'ls-files', '-co', '--exclude-standard', '-z']).stdout
            for relative in filter(None, listed.split('\0')):
                source = REPO / relative
                if source.is_file() and not source.is_symlink():
                    target = self.package / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, target)
            (self.package / 'node_modules').symlink_to(REPO / 'node_modules')
        fixture = self.package / 'tests/host/fixture.ts'
        self.report['source'] = {
            'head': self.command(['git', '-C', REPO, 'rev-parse', 'HEAD']).stdout.strip(),
            'worktree': self.command(['git', '-c', 'core.fsmonitor=false', '-C', REPO,
                                      'status', '--porcelain', '--untracked-files=all']).stdout,
            'runner_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            'fixture_sha256': hashlib.sha256(fixture.read_bytes()).hexdigest(),
        }
        # Live seats need tools: Shop's own tools and bash for shop-run/herdr.
        args = [self.binaries['pi'], '--offline', '--no-extensions', '--no-skills', '--no-prompt-templates',
                '--no-themes', '--no-context-files', *([] if self.live else ['--no-tools']), '--approve',
                '-e', str(self.package / 'extensions/index.ts'), '-e', str(fixture),
                '--provider', provider, '--model', model, '--thinking', architect_thinking]
        wrapper = self.root / 'bin/pi'
        wrapper.write_text('#!/bin/sh\nexec ' + shlex.join(args) + ' "$@"\n')
        wrapper.chmod(0o700)
        # Fault scenario executes the REAL mutation once, then corrupts only its reply.
        proxy = self.root / 'bin/herdr-proxy'
        proxy.write_text('#!' + self.binaries['python'] + '\n' + f'''
import json, os, pathlib, subprocess, sys
root = pathlib.Path({str(self.root)!r})
assert os.environ.get('HERDR_SOCKET_PATH') == str(root/'herdr/h.sock')
r = subprocess.run([{self.binaries['herdr']!r}, *sys.argv[1:]], capture_output=True)
flag = root/'fail-rename-once'
if r.returncode == 0 and sys.argv[1:3] == ['pane','rename'] and flag.exists():
    flag.unlink()
    (root/'fault-original-reply.json').write_bytes(r.stdout)
    r.stdout = b'{{"result":{{"type":"ok"}}}}'
sys.stdout.buffer.write(r.stdout); sys.stderr.buffer.write(r.stderr); sys.exit(r.returncode)
''')
        proxy.chmod(0o700)
        write_json(self.root / 'bridge.json', {'protocol': 1, 'core_root': str(self.package),
                   'config_dir': str(self.root / 'config'), 'state_dir': str(self.root / 'state')})
        if self.live:
            thinking = self.live['thinking']
            models = {'defaults': {'model': self.architect_model, 'thinking': thinking['lead']},
                      'worker': {'model': self.architect_model, 'thinking': thinking['worker']}}
        else:
            models = {'defaults': {'model': FIXTURE_MODEL, 'thinking': 'off'}}
        write_json(self.root / 'config/settings.json', {'version': 1, 'models': models})
        write_json(self.root / 'config/language.json', {'version': 1, 'language': 'en'})
        self.command(['git', 'init', '-q'])
        (self.root / 'project/fixture.txt').write_text(FIXTURE_TEXT + '\n')
        self.command(['git', 'add', 'fixture.txt'])
        self.command(['git', '-c', 'user.name=HostFixture', '-c', 'user.email=fixture@example.invalid',
                      '-c', 'core.hooksPath=/dev/null', '-c', 'commit.gpgsign=false', 'commit', '-qm', 'fixture'])
        if self.live:
            # Fail before any launch if this Pi build cannot resolve the live model.
            listed = self.command([self.binaries['pi'], '--offline', '--no-extensions', '--list-models', model]).stdout
            if not any(line.split()[:2] == [provider, model] for line in listed.splitlines()):
                raise RuntimeError(f'Pi {self.report["versions"]["pi"]} cannot resolve {self.architect_model} with the copied credential')

    def start(self):
        before = self.command([self.binaries['herdr'], 'status', 'server'])
        if 'status: not running' not in before.stdout or self.env['HERDR_SOCKET_PATH'] not in before.stdout:
            raise RuntimeError('Private endpoint preflight failed; refusing server launch')
        if Path(self.env['HERDR_SOCKET_PATH']).exists():
            raise RuntimeError('Socket already exists; refusing to adopt a server')
        self.log = (self.root / 'server.log').open('w')
        self.server = subprocess.Popen([self.binaries['herdr'], 'server'], cwd=self.root / 'project',
                                       env=self.env, stdout=self.log, stderr=self.log, start_new_session=True)
        self.report['server_pid'] = self.server.pid
        self.wait(lambda: Path(self.env['HERDR_SOCKET_PATH']).exists(), 'Private server socket did not appear')
        status = self.command([self.binaries['herdr'], 'status', 'server']).stdout
        if 'status: running' not in status or self.env['HERDR_SOCKET_PATH'] not in status:
            raise RuntimeError('Server identity check failed')
        if self.api('workspace', 'list')['workspaces']:
            raise RuntimeError('Fresh server unexpectedly has workspaces; no mutation allowed')
        if self.api('plugin', 'list', '--json')['plugins']:
            raise RuntimeError('Fresh server unexpectedly has plugins; no mutation allowed')
        self.report['real_herdr'] = True
        self.command([self.binaries['herdr'], 'plugin', 'link', self.package])
        # Assert plugin registry stayed under private HOME/config, not user's registry.
        registry = self.root / 'home/.config/herdr/plugins.json'
        alternate = self.root / 'herdr/plugins.json'
        if not registry.exists() and not alternate.exists():
            raise RuntimeError('Cannot verify private plugin registry location')
        created = self.api('workspace', 'create', '--cwd', str(self.root / 'project'), '--label', 'Shop host test', '--no-focus')
        self.pane, self.tab = created['root_pane']['pane_id'], created['tab']['tab_id']
        self.report.update(pane=self.pane, tab=self.tab)
        self.api('agent', 'start', 'host-architect', '--kind', 'pi', '--pane', self.pane, '--timeout', '30000')
        self.wait(lambda: (self.root / 'observations' / (self.pane + '.json')).exists(), 'Real Pi extension did not start')
        observed = json.loads((self.root / 'observations' / (self.pane + '.json')).read_text())
        if observed['mode'] != 'tui' or not observed['hasUI'] or not REQUIRED_COMMANDS <= set(observed['commands']):
            raise RuntimeError('Real Pi did not load expected TUI commands: ' + str(observed))
        if observed['socket'] != self.env['HERDR_SOCKET_PATH'] or observed['model'] != self.architect_model:
            raise RuntimeError('Pi escaped isolated socket or expected model')
        if self.live and observed.get('thinking') != self.live['thinking']['architect']:
            raise RuntimeError('Architect did not start with requested thinking: ' + str(observed.get('thinking')))
        self.report['real_pi_tui'] = True
        self.api('pane', 'layout', '--pane', self.pane)

    @property
    def registration(self):
        key = hashlib.sha256((self.env['HERDR_SOCKET_PATH'] + ':' + self.tab).encode()).hexdigest()[:12]
        return self.root / 'state/runtime' / (key + '.json')

    def state(self):
        try:
            return json.loads(self.registration.read_text())
        except FileNotFoundError:
            return None

    def slash(self, command):
        # Slash commands do not trigger model turns; --wait would falsely require one.
        self.api('agent', 'prompt', self.pane, command)

    def screen(self):
        # Herdr read commands return terminal text, unlike topology JSON replies.
        return self.command([self.binaries['herdr'], 'agent', 'read', self.pane,
                             '--source', 'visible', '--lines', '120']).stdout

    def configuration_ui(self):
        config = self.root/'config/settings.json'
        initial = config.read_bytes()
        pi_settings = (self.root/'pi/settings.json').read_bytes()
        self.slash('/shop-config')
        self.wait(lambda: 'Shop settings' in self.screen(), 'Settings TUI did not render')
        self.api('agent', 'send-keys', self.pane, 'esc')
        self.wait(lambda: 'Shop settings' not in self.screen(), 'Settings cancel did not close')
        if config.read_bytes() != initial:
            raise RuntimeError('Settings cancel changed configuration')
        self.slash('/shop-config')
        self.wait(lambda: 'Shop settings' in self.screen(), 'Settings did not reopen')
        self.api('agent', 'send-keys', self.pane, 'tab')
        self.wait(lambda: '[Global]' in self.screen(), 'Global scope did not render')
        self.api('agent', 'send-keys', self.pane, 'enter')
        self.wait(lambda: 'Available providers only' in self.screen(), 'Model picker did not open')
        self.api('agent', 'send-keys', self.pane, *(['down'] * 12))
        self.wait(lambda: 'Model name: Host fixture variant 12' in self.screen(), 'Model picker did not scroll to last fixture')
        visible_rows = len(re.findall(r'no-network(?:-\d\d)? \[shop-host-fixture\]', self.screen()))
        if visible_rows != 10:
            raise RuntimeError('Model picker did not maintain ten visible model rows')
        self.report['model_picker_visible_rows'] = visible_rows
        self.api('agent', 'send-keys', self.pane, *list('fixture'))
        self.wait(lambda: 'Model name: Host fixture (no network)' in self.screen(), 'Model search did not select fixture')
        self.api('agent', 'send-keys', self.pane, *(['down'] * 12))
        self.wait(lambda: 'Model name: Host fixture variant 12' in self.screen(), 'Filtered model list did not scroll')
        self.api('agent', 'send-keys', self.pane, 'enter')
        self.wait(lambda: 'Shop settings' in self.screen() and 'Available providers only' not in self.screen(), 'Picker did not return to settings')
        self.api('agent', 'send-keys', self.pane, 's')
        self.wait(lambda: 'Save Shop configuration?' in self.screen(), 'Configuration save preview missing')
        self.api('agent', 'send-keys', self.pane, 'enter')
        self.wait(lambda: config.read_bytes() != initial, 'Global configuration not saved')
        if json.loads(config.read_text())['models']['seats']['lead']['model'] != 'shop-host-fixture/no-network-12':
            raise RuntimeError('Model picker selection was not saved')
        if (self.root/'pi/settings.json').read_bytes() != pi_settings:
            raise RuntimeError('Shop settings modified native Pi defaults')
        self.slash('/shop-language zh-CN')
        self.wait(lambda: json.loads((self.root/'config/language.json').read_text())['language'] == 'zh-CN', 'Language command did not persist')
        self.slash('/shop-language en')
        self.wait(lambda: json.loads((self.root/'config/language.json').read_text())['language'] == 'en', 'Language reset failed')
        before = json.loads((self.root/'observations'/(self.pane+'.json')).read_text())
        self.slash('/reload')
        self.wait(lambda: json.loads((self.root/'observations'/(self.pane+'.json')).read_text())['at'] > before['at'], 'Reload lifecycle not observed')
        after = json.loads((self.root/'observations'/(self.pane+'.json')).read_text())
        if after['session'] != before['session'] or after['model'] != 'shop-host-fixture/no-network':
            raise RuntimeError('Shop settings/reload replaced Architect session or model')

    def native_action(self, action, allow_failure=False):
        if not self.api('agent', 'get', self.pane)['agent']['focused']:
            raise RuntimeError('Architect is not focused; refusing implicit plugin action target')
        invoked = self.api('plugin', 'action', 'invoke', action, '--plugin', 'shop.workstation')
        log_id = invoked['log']['log_id']
        def finished():
            logs = self.api('plugin', 'log', 'list', '--plugin', 'shop.workstation', '--limit', '50')['logs']
            return next((row for row in logs if row['log_id'] == log_id and row['status'] != 'running'), None)
        result = self.wait(finished, 'Native plugin action did not finish: ' + action, timeout=90)
        if result.get('status') != 'succeeded' or result.get('exit_code') != 0:
            if not (allow_failure and result.get('status') == 'failed' and result.get('exit_code')):
                raise RuntimeError('Native plugin action failed: ' + action + ': ' + str(result))
        return result

    def setup(self, fault=False):
        if fault:
            (self.root / 'fail-rename-once').touch()
        # Real native action, Shop entrypoint and Herdr adapter. Only the marked
        # post-mutation fault scenario corrupts one actual response.
        action_log = self.native_action('open', allow_failure=fault)
        if fault:
            if not self.state() or self.state()['phase'] != 'partial':
                raise RuntimeError('Fault injection did not preserve partial registration')
            if not (self.root/'fault-original-reply.json').exists():
                raise RuntimeError('Fault did not follow a real mutation')
            return
        if not self.state() or self.state()['phase'] != 'ready':
            raise RuntimeError('Real setup failed: ' + json.dumps(action_log)[-4000:])
        state = self.state()
        members = [state['architect'], state['lead'], *state['workers']]
        if len(members) != 3:
            raise RuntimeError('Expected three real members')
        sessions = []
        for member in members:
            live = self.api('agent', 'get', member['pane'])['agent']
            if live['name'] != member['name'] or live['terminal_id'] != member['terminal_id']:
                raise RuntimeError('Registered/live identity mismatch')
            path = self.root / 'observations' / (member['pane'] + '.json')
            self.wait(path.exists, 'Member extension not loaded')
            observed = json.loads(path.read_text())
            role = ('architect' if member['pane'] == state['architect']['pane'] else
                    'lead' if member['pane'] == state['lead']['pane'] else 'worker')
            if self.live:
                expected = (self.architect_model, self.live['thinking'][role])
                actual = (observed['model'], observed.get('thinking'))
            else:
                expected = FIXTURE_MODEL + '-12' if role == 'lead' else FIXTURE_MODEL
                actual = observed['model']
            if actual != expected:
                raise RuntimeError('Member did not use its saved profile: ' + str(observed))
            sessions.append(observed['session'])
        if len(set(sessions)) != 3:
            raise RuntimeError('Member sessions were reused')

    def live_roundtrip(self, pane):
        """One real model turn in a pane, proven by the fixture usage ledger."""
        seen = len(self.usage_records())
        self.api('agent', 'prompt', pane, f'Connectivity check. Reply with exactly {LIVE_TOKEN} and nothing else. Do not use tools.')
        def replied():
            rows = self.usage_records()[seen:]
            return next((row for row in rows if row.get('pane') == pane and LIVE_TOKEN in (row.get('text') or '')), None)
        row = self.wait(replied, 'No live reply from ' + pane, timeout=240)
        if f"{row['provider']}/{row['model']}" != self.architect_model:
            raise RuntimeError('Live reply came from unexpected model: ' + str(row))
        return row

    def live_members(self):
        state = self.state()
        for member in (state['lead'], state['workers'][0]):
            self.live_roundtrip(member['pane'])

    def seat_usage(self, rows, roles=None):
        """Usage per role; the design-neutral baseline for comparing workflows."""
        if roles is None:
            state = self.state() or {}
            roles = {self.pane: 'architect'}
            if state.get('lead'):
                roles[state['lead']['pane']] = 'lead'
            roles.update({member['pane']: 'worker' for member in state.get('workers', [])})
        seats = {}
        for row in rows:
            seat = seats.setdefault(roles.get(row.get('pane'), 'other'),
                                    {'calls': 0, 'input': 0, 'output': 0, 'cache_read': 0, 'cost_usd': 0.0})
            usage = row.get('usage') or {}
            seat['calls'] += 1
            for key, source in (('input', 'input'), ('output', 'output'), ('cache_read', 'cacheRead')):
                seat[key] += usage.get(source, 0) or 0
            seat['cost_usd'] = round(seat['cost_usd'] + ((usage.get('cost') or {}).get('total', 0) or 0), 6)
        return seats

    def live_delegation(self):
        """Real /shop loop: Architect -> Lead -> Worker -> accepted ticket + run summary."""
        # /shop refuses a busy Architect; poll (budget-checked) instead of a long CLI wait.
        self.wait(lambda: self.api('agent', 'get', self.pane)['agent']['agent_status'] in ('idle', 'done'),
                  'Architect did not settle before /shop', timeout=120)
        self.delegation = {'ledger_start': len(self.usage_records()), 'started': time.monotonic()}
        self.slash('/shop ' + LIVE_TASK)
        runs = self.root / 'project/.shop/runs'
        def worker_accepted(run):
            for path in (run / 'tickets').glob('*.ticket.json'):
                ticket = json.loads(path.read_text())
                if ticket.get('status') == 'accepted' and str(ticket.get('owner', '')).endswith('-worker'):
                    return ticket
        def delivered():
            for run in sorted(runs.glob('*/')) if runs.exists() else []:
                summary = run / 'SUMMARY.md'
                if summary.exists() and summary.stat().st_size and worker_accepted(run):
                    return run
        run = self.wait(delivered, 'No Worker ticket accepted with run SUMMARY.md', timeout=self.live['timeout'])
        self.delegation['accepted_seconds'] = round(time.monotonic() - self.delegation['started'], 1)
        summary = (run / 'SUMMARY.md').read_text()
        self.report['live_delivery'] = {
            'run': str(run), 'files': sorted(str(path.relative_to(run)) for path in run.rglob('*') if path.is_file()),
            'accepted_ticket': {key: worker_accepted(run).get(key) for key in ('ticket_id', 'owner', 'status', 'attempt')},
            'summary_excerpt': summary[:2000]}
        if not mentions_fixture(summary):
            raise RuntimeError('SUMMARY.md does not quote the fixture content')

    def live_architect_report(self):
        """The loop ends when Architect reports the correct answer back to the user."""
        start = self.delegation['ledger_start']
        # Only replies after the downstream report count as the final answer.
        answer_from = self.delegation.get('report_from', start)
        def reported():
            rows = self.usage_records()[answer_from:]
            return next((row for row in rows if row.get('pane') == self.pane and row.get('stopReason') == 'stop'
                         and mentions_fixture(row.get('text'))), None)
        row = self.wait(reported, 'Architect did not report the fixture content back',
                        timeout=max(60, self.live['timeout'] - self.delegation['accepted_seconds']))
        rows = self.usage_records()[start:]
        roles, seats = None, []
        if self.delegation.get('run'):
            seats = [json.loads(path.read_text()) for path in (self.delegation['run'] / 'seats').glob('*.json')]
            roles = {self.pane: 'architect', **{seat['pane']: seat['role'] for seat in seats}}
        usage = self.seat_usage(rows, roles)
        analyzers = [seat['handoff'].get('analyzer') or {} for seat in seats]
        if analyzers:
            # Curator completions bypass the session ledger; count them as their own seat.
            usage['curator'] = {'calls': sum(a.get('calls', 0) for a in analyzers),
                                'input': sum(a.get('input', 0) for a in analyzers),
                                'output': sum(a.get('output', 0) for a in analyzers),
                                'cache_read': sum(a.get('cacheRead', 0) for a in analyzers),
                                'cost_usd': round(sum(a.get('cost', 0) for a in analyzers), 6)}
        self.report['baseline'] = {
            'task': self.delegation.get('task', LIVE_TASK),
            'seconds_to_accepted': self.delegation['accepted_seconds'],
            'seconds_to_report': round(time.monotonic() - self.delegation['started'], 1),
            'seats': usage,
            'cost_usd': round(sum(seat['cost_usd'] for seat in usage.values()), 6),
            'architect_report_excerpt': row['text'][:1500],
        }

    def settle_architect(self, timeout=300):
        self.wait(lambda: self.api('agent', 'get', self.pane)['agent']['agent_status'] in ('idle', 'done'),
                  'Architect did not settle', timeout=timeout)

    def seats_spec(self):
        """/shop-spec: Architect writes SPEC.md and PLAN.md into a new seat run."""
        self.settle_architect(120)
        self.delegation = {'task': SEATS_TASK, 'ledger_start': len(self.usage_records()), 'started': time.monotonic()}
        self.slash('/shop-spec ' + SEATS_TASK)
        root = self.root / 'project/.shop/seats'
        def written():
            runs = sorted(root.glob('*/')) if root.exists() else []
            return runs and all((runs[-1] / name).exists() and (runs[-1] / name).stat().st_size
                                for name in ('SPEC.md', 'PLAN.md')) and runs[-1]
        self.delegation['run'] = self.wait(written, 'SPEC.md/PLAN.md not written', timeout=300)
        self.settle_architect()
        self.delegation['spec_seconds'] = round(time.monotonic() - self.delegation['started'], 1)

    def seats_go(self):
        """/shop-go: confirm, then the Lead must dispatch a Worker and report back."""
        run = self.delegation['run']
        self.slash('/shop-go')
        self.wait(lambda: 'Launch Lead?' in self.screen(), 'Lead launch confirmation not shown', timeout=60)
        self.api('agent', 'send-keys', self.pane, 'enter')
        report = lambda seat: json.loads((run / 'reports' / (seat + '.json')).read_text())
        self.wait(lambda: (run / 'reports/lead.json').exists(), 'Lead did not report', timeout=self.live['timeout'])
        self.delegation['report_from'] = len(self.usage_records())
        self.delegation['accepted_seconds'] = round(time.monotonic() - self.delegation['started'], 1)
        seats = [json.loads(path.read_text()) for path in (run / 'seats').glob('*.json')]
        workers = [seat for seat in seats if seat['role'] == 'worker']
        done = [seat['id'] for seat in workers if (run / 'reports' / (seat['id'] + '.json')).exists()
                and report(seat['id'])['status'] == 'completed']
        self.report['live_delivery'] = {
            'run': str(run), 'lead_report': report('lead'), 'workers': [seat['id'] for seat in workers],
            'completed_workers': done,
            'handoff': {seat['id']: seat['handoff'] for seat in seats}}
        if report('lead')['status'] != 'completed' or not done:
            raise RuntimeError('Lead did not complete through a Worker: ' + json.dumps(self.report['live_delivery'])[:1500])
        if not any(mentions_fixture(report(seat)['summary'] + (report(seat).get('evidence') or '')) for seat in done):
            raise RuntimeError('No Worker report quotes the fixture content')

    def fidelity_discussion(self):
        """Real Architect turns that establish the constraint and reject the alternative."""
        self.settle_architect(120)
        self.delegation = {'task': 'S2 fidelity', 'ledger_start': len(self.usage_records()), 'started': time.monotonic()}
        for text in FIDELITY_DISCUSSION:
            seen = len(self.usage_records())
            self.api('agent', 'prompt', self.pane, text)
            self.wait(lambda: any(row.get('pane') == self.pane and row.get('stopReason') == 'stop'
                                  for row in self.usage_records()[seen:]), 'Architect did not answer', timeout=240)
            self.settle_architect()

    def fidelity_go(self):
        """Fixed SPEC/PLAN that omit the format; /shop-go hands the discussion to the Lead."""
        run = self.root / 'project/.shop/seats/fidelity'
        run.mkdir(parents=True)
        (run / 'SPEC.md').write_text(FIDELITY_SPEC)
        (run / 'PLAN.md').write_text(FIDELITY_PLAN)
        self.delegation['run'] = run
        self.slash('/shop-go ' + str(run))
        self.wait(lambda: 'Launch Lead?' in self.screen(), 'Lead launch confirmation not shown', timeout=60)
        self.api('agent', 'send-keys', self.pane, 'enter')
        self.wait(lambda: (run / 'reports/lead.json').exists(), 'Lead did not report', timeout=self.live['timeout'])
        self.delegation['accepted_seconds'] = round(time.monotonic() - self.delegation['started'], 1)

    def fidelity_check(self):
        """Pass only if the agreed format reached the output and the rejected one did not."""
        run = self.delegation['run']
        seats = [json.loads(path.read_text()) for path in (run / 'seats').glob('*.json')]
        lead_session = next(Path(seat['session']).read_text() for seat in seats if seat['role'] == 'lead')
        report_path, rejected = self.root / 'project/report.json', self.root / 'project/report.yaml'
        try:
            produced = json.loads(report_path.read_text())
        except (OSError, ValueError):
            produced = None
        rows = self.usage_records()[self.delegation['ledger_start']:]
        roles = {self.pane: 'architect', **{seat['pane']: seat['role'] for seat in seats}}
        usage = self.seat_usage(rows, roles)
        analyzers = [seat['handoff'].get('analyzer') or {} for seat in seats]
        usage['curator'] = {'calls': sum(a.get('calls', 0) for a in analyzers), 'input': sum(a.get('input', 0) for a in analyzers),
                            'output': sum(a.get('output', 0) for a in analyzers), 'cache_read': sum(a.get('cacheRead', 0) for a in analyzers),
                            'cost_usd': round(sum(a.get('cost', 0) for a in analyzers), 6)}
        verdict = {
            'handoff': {seat['id']: {key: seat['handoff'].get(key) for key in ('mode', 'reason', 'sourceTokens', 'checkpointTokens')}
                        for seat in seats},
            'lead_status': json.loads((run / 'reports/lead.json').read_text())['status'],
            'lead_context_has_constraint': 'first_line' in lead_session,
            'lead_context_has_rejected_key': 'summary' in lead_session and 'report.yaml' in lead_session,
            'report_json': produced,
            'report_yaml_written': rejected.exists(),
        }
        verdict['faithful'] = (isinstance(produced, dict) and set(produced) == {'file', 'first_line'}
                               and mentions_fixture(str(produced.get('first_line'))) and not rejected.exists())
        self.report['fidelity'] = verdict
        self.report['baseline'] = {'task': 'S2 fidelity', 'seconds_to_accepted': self.delegation['accepted_seconds'],
                                   'seconds_to_report': self.delegation['accepted_seconds'], 'seats': usage,
                                   'cost_usd': round(sum(seat['cost_usd'] for seat in usage.values()), 6)}
        if not verdict['faithful']:
            raise RuntimeError('Agreed format not delivered: ' + json.dumps(verdict)[:1500])

    def seats_closed(self):
        """S5: disposable seats close their own panes; only Architect remains."""
        self.wait(lambda: [pane['pane_id'] for pane in self.api('pane', 'layout', '--pane', self.pane)['layout']['panes']]
                  == [self.pane], 'Seat panes did not close', timeout=90)
        run = self.delegation['run']
        for path in (run / 'seats').glob('*.json'):
            if not Path(json.loads(path.read_text())['session']).exists():
                raise RuntimeError('Seat session not retained: ' + path.name)

    def reset_ui(self):
        before = self.registration.read_bytes()
        config = (self.root/'config/settings.json').read_bytes()
        self.slash('/shop-reset')
        self.wait(lambda: 'Archive failed setup in this tab?' in self.screen(), 'Reset preview not shown')
        self.api('agent', 'send-keys', self.pane, 'esc')
        if self.registration.read_bytes() != before:
            raise RuntimeError('Cancel modified registration')
        self.slash('/shop-reset')
        self.wait(lambda: 'Archive failed setup in this tab?' in self.screen(), 'Reset confirm not shown')
        self.api('agent', 'send-keys', self.pane, 'enter')
        self.wait(lambda: not self.registration.exists(), 'Confirmed reset did not archive registration')
        archives = list((self.root/'state/reset-archive').glob('*.json'))
        if not any(p.read_bytes() == before for p in archives):
            raise RuntimeError('Exact reset backup missing')
        if config != (self.root/'config/settings.json').read_bytes():
            raise RuntimeError('Reset changed configuration')
        layout = self.api('pane', 'layout', '--pane', self.pane)['layout']
        if [p['pane_id'] for p in layout['panes']] != [self.pane]:
            raise RuntimeError('Reset changed pane topology')

    def background_guard(self):
        state = self.state()
        original = self.registration.read_bytes()
        lead = state['lead']['pane']
        job = self.root/'observations'/(lead + '.job.json')
        try:
            self.api('agent', 'prompt', lead, '/shop-host-background-start')
            self.wait(job.exists, 'Fixture background child did not start')
            child = json.loads(job.read_text())
            if not child['running']:
                raise RuntimeError('Background fixture is not running')
            self.native_action('close')
            directory = self.root/'state/shutdown/plans'/state['shop_id']
            plans = sorted(directory.glob('*.json'), key=lambda p:p.stat().st_mtime_ns)
            if not plans:
                raise RuntimeError('Background guard produced no shutdown plan')
            plan = json.loads(plans[-1].read_text())
            write_json(self.root/'background-blocked-plan.json', plan)
            found = [p['pid'] for row in plan.get('facts', {}).get('process', [])
                     for p in row.get('extra_background', [])]
            if (plan['decision'] != 'blocked' or child['pid'] not in found
                    or 'background_work' not in {b['code'] for b in plan['blockers']}):
                raise RuntimeError('Live background child failed to block shutdown')
            if self.registration.read_bytes() != original:
                raise RuntimeError('Blocked shutdown mutated registration')
            for member in (state['architect'], state['lead'], *state['workers']):
                if self.api('agent', 'get', member['pane'])['agent']['terminal_id'] != member['terminal_id']:
                    raise RuntimeError('Blocked shutdown changed a member')
        finally:
            self.api('agent', 'prompt', lead, '/shop-host-background-stop')
            self.wait(lambda: job.exists() and not json.loads(job.read_text())['running'],
                      'Owned fixture child did not stop')

    def shutdown(self):
        # Invoke actual user plugin action, not a fake background_proven probe.
        state = self.state()
        self.native_action('close')
        directory = self.root/'state/shutdown/plans'/state['shop_id']
        self.wait(lambda: list(directory.glob('*.json')), 'Native close action produced no plan')
        plans = sorted(directory.glob('*.json'), key=lambda p:p.stat().st_mtime)
        plan = json.loads(plans[-1].read_text())
        write_json(self.root/'shutdown-plan.json', plan)
        if plan['decision'] != 'ready':
            raise RuntimeError('Real idle shutdown gate failed: ' + json.dumps(plan.get('blockers', []), ensure_ascii=False))
        self.wait(lambda: not self.registration.exists(), 'Shutdown never completed')
        panes = self.api('pane', 'layout', '--pane', self.pane)['layout']['panes']
        if [p['pane_id'] for p in panes] != [self.pane]:
            raise RuntimeError('Shutdown did not retain Architect only')
        receipt_path = self.root/'state/shutdown/receipts'/(state['shop_id'] + '.json')
        receipt_bytes = receipt_path.read_bytes()
        receipt = json.loads(receipt_bytes)
        expected_closed = {state['lead']['pane'], *(member['pane'] for member in state['workers'])}
        if (not receipt['registration_removed'] or not receipt['verified_absent']
                or set(receipt['closed']) != expected_closed or receipt['retained']['pane'] != self.pane):
            raise RuntimeError('Shutdown receipt does not prove expected closure')
        # The current CLI rejects missing registration. Verify non-mutation,
        # but do not misreport that rejection as an already_closed acknowledgement.
        repeated = self.native_action('close', allow_failure=True)
        self.report['repeat_shutdown'] = {key: repeated.get(key) for key in ('status', 'exit_code', 'stdout')}
        if self.registration.exists() or receipt_path.read_bytes() != receipt_bytes:
            raise RuntimeError('Repeated shutdown changed registration or receipt')
        panes = self.api('pane', 'layout', '--pane', self.pane)['layout']['panes']
        if [p['pane_id'] for p in panes] != [self.pane]:
            raise RuntimeError('Repeated shutdown changed the retained topology')

    def cleanup(self):
        # Observe only PIDs reported by this run's explicit fixture extension.
        # Birth stamps prevent PID reuse from being mistaken for leaked Pi. Never
        # kill these PIDs: terminate only our direct server child and verify exit.
        def birth(pid):
            result = subprocess.run(['/bin/ps', '-p', str(pid), '-o', 'lstart='],
                                    capture_output=True, text=True, env=self.env, timeout=5)
            if result.returncode == 1 and not result.stdout.strip() and not result.stderr.strip():
                return ''  # ps found no matching PID; other query failures are unknown.
            if result.returncode or not result.stdout.strip():
                raise RuntimeError('Cannot verify fixture process birth stamp')
            return result.stdout.strip()
        processes = {}
        inventory_ok = True
        try:
            for path in (self.root/'observations').glob('*.json'):
                pid = json.loads(path.read_text()).get('pid')
                if isinstance(pid, int) and pid > 1:
                    processes[pid] = birth(pid)
        except Exception as inventory_error:
            inventory_ok = False
            self.report['process_inventory_error'] = str(inventory_error)
        # Do not use default/global server stop or kill arbitrary pane/process IDs.
        # This direct child was created with its own HOME/socket and owns only fixtures.
        if self.server and self.server.poll() is None:
            self.server.terminate()
            try:
                self.server.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.report['cleanup'] = 'owned_server_did_not_stop; inspect artifacts'
                self.report['remaining_fixture_pids'] = list(processes)
                if self.log:
                    self.log.close()
                return False
        if self.log:
            self.log.close()
        deadline = time.monotonic() + 10
        while processes and time.monotonic() < deadline:
            processes = {pid: stamp for pid, stamp in processes.items() if stamp and birth(pid) == stamp}
            if processes:
                time.sleep(0.15)
        self.report['cleanup'] = ('process_inventory_unknown' if not inventory_ok else
                                  'owned_pi_still_alive' if processes else
                                  'owned_server_stopped' if self.server else 'no_server_started')
        self.report['remaining_fixture_pids'] = list(processes)
        return inventory_ok and not processes

    def run(self, scenario):
        self.report['scenario'] = scenario
        self.report['required_release_scenario'] = 'all'
        try:
            self.step('isolation.prepare', self.prepare)
            self.step('host.start_and_load', self.start)
            if scenario in LIVE_SCENARIOS:
                self.step('live.architect_roundtrip', lambda: self.live_roundtrip(self.pane))
                if scenario not in ('live-seats', 'live-fidelity'):
                    self.step('setup.live_members', self.setup)
                    self.step('live.member_roundtrip', self.live_members)
                if scenario == 'live-fidelity':
                    self.step('fidelity.discussion', self.fidelity_discussion)
                    self.step('fidelity.go', self.fidelity_go)
                    self.step('fidelity.check', self.fidelity_check)
                    self.step('seats.panes_closed', self.seats_closed)
                if scenario == 'live-seats':
                    self.step('seats.spec', self.seats_spec)
                    self.step('seats.go_worker_delivery', self.seats_go)
                    self.step('live.architect_report', self.live_architect_report)
                    self.step('seats.panes_closed', self.seats_closed)
                if scenario == 'live-delegation':
                    self.step('live.delegation_delivery', self.live_delegation)
                    self.step('live.architect_report', self.live_architect_report)
            else:
                self.step('pi.commands_configuration_reload', self.configuration_ui)
            if scenario in ('reset', 'all'):
                self.step('setup.real_mutation_bad_reply', lambda: self.setup(fault=True))
                self.step('pi.reset_cancel_confirm', self.reset_ui)
            if scenario in ('lifecycle', 'all'):
                self.step('setup.real_three_member_identity', self.setup)
                self.step('shutdown.real_background_refusal', self.background_guard)
                self.step('shutdown.real_idle_lifecycle', self.shutdown)
            if (self.root/'provider-calls.jsonl').exists():
                raise RuntimeError('Unexpected fixture inference in no-inference smoke scenarios')
            self.report['result'] = 'passed'
        except KeyboardInterrupt:
            self.report.update(result='failed', error='interrupted; owned-resource cleanup requested')
            raise
        except Exception as error:
            self.report.update(result='failed', error=str(error))
            print('FAIL', str(error), file=sys.stderr)
            if self.pane and self.server and self.server.poll() is None:
                try:
                    write_json(self.root/'last-screen.json', {'text': self.screen()})
                except Exception as capture_error:
                    self.report['capture_error'] = str(capture_error)
        finally:
            calls = self.root/'provider-calls.jsonl'
            self.report['fixture_provider_calls'] = len(calls.read_text().splitlines()) if calls.exists() else 0
            try:
                if not self.cleanup():
                    self.report['result'] = 'failed'
            except Exception as cleanup_error:
                self.report.update(result='failed', cleanup='failed', cleanup_error=str(cleanup_error))
            finally:
                # After the server (and its Pi panes) stopped; always, even on failure.
                if self.live:
                    self.report['live']['usage'] = self.usage()
                self.scrub_credentials()
            self.save()
            print('Artifacts:', self.root, flush=True)
        return 0 if self.report['result'] == 'passed' else 1


def aggregate(reports):
    """Pass rate plus min/median/max of the baseline metrics over passed runs."""
    def spread(values):
        values = [value for value in values if value is not None]
        return values and {'min': min(values), 'median': statistics.median(values), 'max': max(values)}
    passed = [report for report in reports if report['result'] == 'passed']
    baselines = [report['baseline'] for report in passed if 'baseline' in report]
    seats = sorted({seat for baseline in baselines for seat in baseline['seats']})
    return {
        'schema': 'shop.host-aggregate/v1', 'scenario': reports[0].get('scenario'), 'live': reports[0].get('live', {}).get('model'),
        'runs': len(reports), 'passed': len(passed),
        'failures': [{'root': report['root'], 'step': next((step['name'] for step in report['steps'] if step['status'] == 'failed'), None),
                      'error': report.get('error')} for report in reports if report['result'] != 'passed'],
        'cost_usd_per_run_all': spread([report.get('live', {}).get('usage', {}).get('cost_usd') for report in reports]),
        # Delegated task only (excludes connectivity checks), including curator analyzer calls.
        'task_cost_usd': spread([baseline.get('cost_usd') for baseline in baselines]),
        'seconds_to_accepted': spread([baseline['seconds_to_accepted'] for baseline in baselines]),
        'seconds_to_report': spread([baseline['seconds_to_report'] for baseline in baselines]),
        'seats': {seat: {key: spread([baseline['seats'].get(seat, {}).get(key) for baseline in baselines])
                         for key in ('calls', 'input', 'output', 'cost_usd')} for seat in seats},
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', action='store_true', help='Explicitly launch owned isolated Herdr/Pi processes')
    parser.add_argument('--scenario', choices=['startup', 'reset', 'lifecycle', 'all', *LIVE_SCENARIOS], default='all')
    parser.add_argument('--pi-bin', help='Pi executable (default: first pi on PATH; npm run prefers the repo copy)')
    parser.add_argument('--live-model', help='provider/model for live scenarios; required there, refused elsewhere')
    parser.add_argument('--live-thinking', default='', help='e.g. architect=max,lead=high,worker=low')
    parser.add_argument('--live-budget-usd', type=float, default=0.30)
    parser.add_argument('--live-max-calls', type=int, default=80)
    parser.add_argument('--live-timeout', type=int, default=900, help='Seconds to wait for live delegation delivery')
    parser.add_argument('--repeat', type=int, default=1, help='Independent isolated runs; live budget applies per run')
    parser.add_argument('--handoff-mode', choices=HANDOFF_MODES, default='auto', help='Seat context handoff (live-seats/live-fidelity)')
    args = parser.parse_args()
    binaries = {name: shutil.which(command) for name, command in
                [('herdr','herdr'), ('pi','pi'), ('python','python3'), ('node','node')]}
    if args.pi_bin:
        binaries['pi'] = shutil.which(args.pi_bin)
    if not all(binaries.values()):
        parser.error('herdr, pi, python3 and node must already be installed; no automatic installation')
    if not 1 <= args.repeat <= 20:
        parser.error('--repeat must be between 1 and 20')
    live = credential = None
    if (args.scenario in LIVE_SCENARIOS) != bool(args.live_model):
        parser.error('--live-model is required for live scenarios and refused for no-inference scenarios')
    if args.live_model:
        if args.live_model.count('/') < 1 or not all(args.live_model.split('/', 1)):
            parser.error('--live-model must be provider/model')
        if not 0 < args.live_budget_usd <= 5 or args.live_max_calls < 1:
            parser.error('--live-budget-usd must be in (0, 5] and --live-max-calls positive')
        try:
            thinking = parse_thinking(args.live_thinking)
            credential = live_credential(args.live_model.split('/', 1)[0], Path.home() / '.pi/agent/auth.json')
        except (ValueError, RuntimeError) as error:
            parser.error(str(error))
        live = {'model': args.live_model, 'thinking': thinking, 'budget_usd': args.live_budget_usd,
                'max_calls': args.live_max_calls, 'timeout': args.live_timeout, 'handoff_mode': args.handoff_mode}
    if not args.run:
        print(json.dumps({'mode': 'plan_only', 'binaries': binaries, 'scenario': args.scenario,
                          'live': live and {key: live[key] for key in ('model', 'thinking', 'budget_usd', 'max_calls')},
                          'repeat': args.repeat,
                          'launch': 'Use --run to opt in; no user server inherited' + (
                              '; live copies one API-key credential and spends up to the budget' if live
                              else '; no credentials'),
                          'required_gate': 'Idle shutdown must pass; live background work must block; neither check is skipped'}, indent=2))
        return 0
    if sys.platform != 'darwin':
        parser.error('Live host runner currently validated for macOS only')
    os.umask(0o077)
    def terminate(_signum, _frame):
        raise KeyboardInterrupt('SIGTERM')
    previous = signal.signal(signal.SIGTERM, terminate)
    try:
        reports = []
        for index in range(args.repeat):
            if args.repeat > 1:
                print(f'=== run {index + 1}/{args.repeat} ===', flush=True)
            host = HostTest(binaries, live=live, credential=credential)
            host.run(args.scenario)
            reports.append(json.loads((host.root / 'report.json').read_text()))
        if args.repeat > 1:
            summary = aggregate(reports)
            path = Path(tempfile.gettempdir()) / f'shop-host-aggregate-{time.strftime("%Y%m%d-%H%M%S")}.json'
            write_json(path, summary)
            print(json.dumps(summary, ensure_ascii=False, indent=2))
            print('Aggregate:', path, flush=True)
        return 0 if all(report['result'] == 'passed' for report in reports) else 1
    finally:
        signal.signal(signal.SIGTERM, previous)


if __name__ == '__main__':
    sys.exit(main())
