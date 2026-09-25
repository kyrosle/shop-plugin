#!/usr/bin/env python3
"""Opt-in real Herdr + Pi smoke gate. No user config, credentials or paid inference.

Default is a plan. --run creates a private headless server, real Pi PTYs and a
throwaway Git project. A failed host assertion is a nonzero exit, never a skip.
Artifacts remain outside the repository; only this run's server is stopped.
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
import subprocess
import sys
import tempfile
import time
import uuid

REPO = Path(__file__).resolve().parents[2]
REQUIRED_COMMANDS = {'shop', 'shop-ui', 'shop-config', 'shop-language', 'shop-status', 'shop-reset'}


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
    def __init__(self, binaries, timeout=30):
        # Short, canonical path also avoids Unix socket length limits on macOS.
        self.root = Path(tempfile.mkdtemp(prefix='shop-host-', dir='/tmp')).resolve()
        self.root.chmod(0o700)
        self.binaries, self.timeout = binaries, timeout
        self.env = isolated_env(self.root, binaries.values())
        self.server = None
        self.log = None
        self.pane = self.tab = None
        self.steps = []
        self.report = {'schema': 'shop.host-test/v1', 'root': str(self.root), 'steps': self.steps,
                       'provider_policy': 'local-fixture-only; no user credentials',
                       'model': 'shop-host-fixture/no-network',
                       'real_herdr': False, 'real_pi_tui': False, 'result': 'running',
                       'not_covered': ['real provider quality', 'full ticket delivery/acceptance',
                                       'worktree integration', 'visual screenshot comparison',
                                       'successful repeated-close acknowledgement']}
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
            value = predicate()
            if value:
                return value
            time.sleep(0.15)
        raise TimeoutError(label)

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
        write_json(self.root / 'pi/settings.json', {'defaultProvider': 'shop-host-fixture', 'defaultModel': 'no-network',
                                                   'defaultThinkingLevel': 'off', 'quietStartup': False})
        write_json(self.root / 'pi/auth.json', {})
        fixture = REPO / 'tests/host/fixture.ts'
        self.report['source'] = {
            'head': self.command(['git', '-C', REPO, 'rev-parse', 'HEAD']).stdout.strip(),
            'worktree': self.command(['git', '-c', 'core.fsmonitor=false', '-C', REPO,
                                      'status', '--porcelain', '--untracked-files=all']).stdout,
            'runner_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            'fixture_sha256': hashlib.sha256(fixture.read_bytes()).hexdigest(),
        }
        args = [self.binaries['pi'], '--offline', '--no-extensions', '--no-skills', '--no-prompt-templates',
                '--no-themes', '--no-context-files', '--no-tools', '--approve',
                '-e', str(REPO / 'extensions/index.ts'), '-e', str(fixture),
                '--provider', 'shop-host-fixture', '--model', 'no-network', '--thinking', 'off']
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
        write_json(self.root / 'bridge.json', {'protocol': 1, 'core_root': str(REPO),
                   'config_dir': str(self.root / 'config'), 'state_dir': str(self.root / 'state')})
        write_json(self.root / 'config/settings.json', {'version': 1, 'models': {'defaults': {
            'model': 'shop-host-fixture/no-network', 'thinking': 'off'}}})
        write_json(self.root / 'config/language.json', {'version': 1, 'language': 'en'})
        self.command(['git', 'init', '-q'])
        (self.root / 'project/fixture.txt').write_text('Host smoke fixture; no business repository.\n')
        self.command(['git', 'add', 'fixture.txt'])
        self.command(['git', '-c', 'user.name=HostFixture', '-c', 'user.email=fixture@example.invalid',
                      '-c', 'core.hooksPath=/dev/null', '-c', 'commit.gpgsign=false', 'commit', '-qm', 'fixture'])

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
        self.command([self.binaries['herdr'], 'plugin', 'link', REPO])
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
        if observed['socket'] != self.env['HERDR_SOCKET_PATH'] or observed['model'] != 'shop-host-fixture/no-network':
            raise RuntimeError('Pi escaped isolated socket or fixture model')
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
            expected = 'shop-host-fixture/no-network-12' if member['pane'] == state['lead']['pane'] else 'shop-host-fixture/no-network'
            if observed['model'] != expected:
                raise RuntimeError('Member did not use its saved fixture profile: ' + str(observed))
            sessions.append(observed['session'])
        if len(set(sessions)) != 3:
            raise RuntimeError('Member sessions were reused')

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
            self.save()
            print('Artifacts:', self.root, flush=True)
        return 0 if self.report['result'] == 'passed' else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', action='store_true', help='Explicitly launch owned isolated Herdr/Pi processes')
    parser.add_argument('--scenario', choices=['startup', 'reset', 'lifecycle', 'all'], default='all')
    args = parser.parse_args()
    binaries = {name: shutil.which(command) for name, command in
                [('herdr','herdr'), ('pi','pi'), ('python','python3'), ('node','node')]}
    if not all(binaries.values()):
        parser.error('herdr, pi, python3 and node must already be installed; no automatic installation')
    if not args.run:
        print(json.dumps({'mode': 'plan_only', 'binaries': binaries, 'scenario': args.scenario,
                          'launch': 'Use --run to opt in; no credentials or user server inherited',
                          'required_gate': 'Idle shutdown must pass; live background work must block; neither check is skipped'}, indent=2))
        return 0
    if sys.platform != 'darwin':
        parser.error('Live host runner currently validated for macOS only')
    os.umask(0o077)
    def terminate(_signum, _frame):
        raise KeyboardInterrupt('SIGTERM')
    previous = signal.signal(signal.SIGTERM, terminate)
    try:
        return HostTest(binaries).run(args.scenario)
    finally:
        signal.signal(signal.SIGTERM, previous)


if __name__ == '__main__':
    sys.exit(main())
