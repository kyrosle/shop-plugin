#!/usr/bin/env python3
"""Herdr actions and explicit bridge configuration. No install-time mutation."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import herdr

PACKAGE = Path(__file__).resolve().parent.parent
HERDR = herdr.Herdr()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['configure', 'doctor', 'open', 'close', 'status', 'recover',
                                           'shutdown'])
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    locator = Path(os.environ.get('SHOP_LOCATOR', str(Path.home() / '.config/shop-workstation/bridge.json'))).expanduser()
    if args.apply and args.action != 'configure':
        parser.error('--apply only for configure')
    if args.action == 'configure':
        state = Path(os.environ.get('SHOP_STATE_DIR') or os.environ.get('HERDR_PLUGIN_STATE_DIR') or str(Path.home() / '.local/state/shop-workstation')).expanduser().resolve()
        config = Path(os.environ.get('SHOP_CONFIG_DIR') or os.environ.get('HERDR_PLUGIN_CONFIG_DIR') or str(Path.home() / '.config/shop-workstation')).expanduser().resolve()
        for path in (state, config):
            if path == PACKAGE or PACKAGE in path.parents:
                raise RuntimeError('Runtime/config must live outside package checkout')
        proposed = {'protocol': 1, 'core_root': str(PACKAGE), 'state_dir': str(state), 'config_dir': str(config)}
        print(json.dumps({'locator': str(locator), 'previous': json.loads(locator.read_text()) if locator.exists() else None, 'proposed': proposed, 'apply': args.apply}, indent=2))
        if args.apply:
            import contracts as ct
            if locator.exists():
                raise RuntimeError('Existing bridge: inspect and back up before switching cores; no automatic overwrite')
            config.mkdir(parents=True, exist_ok=True);state.mkdir(parents=True, exist_ok=True)
            ct.atomic(locator, proposed)
        return
    import settings
    if args.action == 'doctor':
        result = {'protocol': 1, 'core_root': str(PACKAGE), 'bridge_configured': locator.exists(),
                  'state_dir': str(settings.STATE), 'config_dir': str(settings.CONFIG),
                  'herdr': HERDR.binary, 'pi': shutil.which('pi'), 'git': shutil.which('git'),
                  'models_configured': sorted(settings.models())}
        try:
            result['herdr_probe'] = HERDR.server_status()
        except herdr.HerdrError as error:
            result['herdr_probe'] = {'error': str(error)}
        print(json.dumps(result, indent=2));return
    if not locator.exists():
        raise RuntimeError('Configure bridge first: python3 core/plugin.py configure --apply')
    # Herdr action cwd is plugin root; use explicit invocation pane, never plugin cwd.
    if os.environ.get('HERDR_PLUGIN_ID') and os.environ.get('HERDR_PANE_ID'):
        os.environ['HERDR_ACTIVE_PANE_ID'] = os.environ['HERDR_PANE_ID']
    if args.action in ('close', 'shutdown'):
        # Independent user-action host: this process is not hosted by the invoking
        # Pi pane, so the invoking execution pane can be closed last. Execution
        # needs Herdr plugin-action context, which the child inherits.
        if not (os.environ.get('HERDR_PLUGIN_ID') and os.environ.get('HERDR_PLUGIN_ACTION_ID')):
            raise RuntimeError('Shutdown requires Herdr plugin-action context (Shift+U action), '
                               'not an agent CLI call')
    action = {'open': 'setup', 'close': 'shutdown', 'shutdown': 'shutdown',
              'status': 'status', 'recover': 'recovery'}[args.action]
    # Only subprocess boundary in this module: the Python core entry point, which
    # re-validates the plan inside one process before any pane is closed.
    r = subprocess.run([sys.executable, str(PACKAGE / 'core/shop.py'), action],
                       capture_output=True, text=True)
    if r.returncode:
        raise SystemExit(r.returncode)
    if args.action in ('close', 'shutdown'):
        print((r.stdout or '').strip()[:20000])
        try:
            document = json.loads(r.stdout)
        except ValueError:
            document = {}
        decision = document.get('decision')
        if decision == 'closed':
            HERDR.notify('Shop 已收工', '执行成员已关闭并经核实，Architect 保留。')
        elif decision == 'already_closed':
            HERDR.notify('Shop 已收工', '此前已完成收工并经核实；未再关闭任何 pane。')
        else:
            HERDR.notify('Shop 收工未完成',
                         '决策 %s：存在阻塞或部分关闭，状态与票据保留；请查看插件日志与恢复计划。'
                         % (decision or 'unknown'))
        return
    if args.action == 'open':
        HERDR.notify('Shop 已就绪', '成员启动完成，可以派工或收工。')
    if args.action == 'status':
        # Same bounded snapshot as herdr-shop status / shop_status: printed to the
        # plugin action log (bounded) and summarised in the notification.
        print((r.stdout or '').strip()[:20000])
        try:
            document = json.loads(r.stdout)
            attention = document.get('attention') or []
            warn = sum(1 for entry in attention if entry.get('severity') == 'warn')
            body = 'phase %s · run %s · members %s · attention %s (warn %s). Full snapshot in the plugin action log.' % (
                (document.get('shop') or {}).get('phase'), (document.get('shop') or {}).get('run_id') or 'unbound',
                len(document.get('members') or []), len(attention), warn)
        except (ValueError, AttributeError):
            body = 'Snapshot written to plugin action log; use shop_status in Pi for the bounded view.'
        HERDR.notify('Shop status', body)


if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        print('shop-plugin: ' + str(e), file=sys.stderr)
        if os.environ.get('HERDR_PLUGIN_ID'):
            HERDR.notify('Shop operation stopped', str(e)[:600])
        sys.exit(1)
