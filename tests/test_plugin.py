import ast
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]


class PluginTests(unittest.TestCase):
    def test_configure_explicit_and_never_overwrites(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            env=dict(os.environ,SHOP_LOCATOR=str(root/'bridge.json'),SHOP_STATE_DIR=str(root/'state'),SHOP_CONFIG_DIR=str(root/'config'))
            def call(*args):
                return subprocess.run([sys.executable,str(ROOT/'core/plugin.py'),*args],env=env,capture_output=True,text=True)
            self.assertEqual(call('configure').returncode,0)
            self.assertFalse((root/'bridge.json').exists())
            self.assertEqual(call('configure','--apply').returncode,0)
            data=json.loads((root/'bridge.json').read_text())
            self.assertEqual(data['core_root'],str(ROOT))
            self.assertNotEqual(call('configure','--apply').returncode,0)
            self.assertEqual(json.loads((root/'bridge.json').read_text()),data)
    def test_another_checkout_refused(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'bridge.json'
            p.write_text(json.dumps({'protocol':1,'core_root':d,'config_dir':d,'state_dir':d}))
            r=subprocess.run([sys.executable,str(ROOT/'core/plugin.py'),'doctor'],env=dict(os.environ,SHOP_LOCATOR=str(p)),capture_output=True,text=True)
            self.assertNotEqual(r.returncode,0)
            self.assertIn('Another Shop core owns bridge',r.stderr)

    def test_only_adapter_owns_herdr_binary_and_subprocess_boundary(self):
        """Static adapter boundary: other Python modules cannot find or execute Herdr."""
        forbidden_names = {'SHOP_HERDR_BIN', 'HERDR_BIN_PATH'}
        offenders = []
        for path in sorted((ROOT/'core').glob('*.py')):
            if path.name == 'herdr.py':
                continue
            source = path.read_text()
            tree = ast.parse(source, filename=str(path))
            for name in forbidden_names:
                if name in source:
                    offenders.append(f'{path.name}: derives {name}')
            subprocess_calls = []
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                is_subprocess = (isinstance(func, ast.Attribute)
                                 and isinstance(func.value, ast.Name)
                                 and func.value.id == 'subprocess'
                                 and func.attr in {'run', 'call', 'check_call', 'check_output', 'Popen'})
                if is_subprocess:
                    subprocess_calls.append(node)
                    segment = ast.get_source_segment(source, node) or ''
                    if 'herdr' in segment.lower() or 'notification' in segment.lower():
                        offenders.append(f'{path.name}: direct Herdr subprocess: {segment}')
                is_which = (isinstance(func, ast.Attribute)
                            and isinstance(func.value, ast.Name)
                            and func.value.id == 'shutil' and func.attr == 'which')
                if is_which and node.args and isinstance(node.args[0], ast.Constant) \
                        and str(node.args[0].value).lower() == 'herdr':
                    offenders.append(f'{path.name}: derives Herdr through shutil.which')
            allowed_prefix = {
                'contracts.py': "subprocess.run(['git'",
                'plugin.py': 'subprocess.run([sys.executable',
                'run.py': "subprocess.check_output(['git'",
                'shop.py': 'subprocess.run(argv',
                'processes.py': "subprocess.run(['/bin/ps',",
            }.get(path.name)
            segments = [ast.get_source_segment(source, call) or '' for call in subprocess_calls]
            if allowed_prefix is None:
                if segments:
                    offenders.append(f'{path.name}: unexpected subprocess calls {segments}')
            elif len(segments) != 1 or not segments[0].startswith(allowed_prefix):
                offenders.append(f'{path.name}: subprocess boundary changed: {segments}')
            if path.name == 'processes.py' and len(subprocess_calls) == 1:
                call = subprocess_calls[0]
                self.assertEqual(ast.literal_eval(call.args[0]),
                                 ['/bin/ps', '-axo', 'pid=,ppid=,pgid=,uid=,tty=,lstart=,comm='])
                self.assertEqual({kw.arg: ast.literal_eval(kw.value) for kw in call.keywords},
                                 {'capture_output': True, 'text': True, 'timeout': 3,
                                  'env': {'PATH': '/usr/bin:/bin', 'LC_ALL': 'C'}})
            if path.name == 'shop.py':
                for node in ast.walk(tree):
                    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                            and node.func.id == 'cmd':
                        first = node.args[0] if node.args else None
                        if not isinstance(first, ast.List) or not first.elts \
                                or not isinstance(first.elts[0], ast.Constant) \
                                or first.elts[0].value != 'git':
                            offenders.append('shop.py: cmd wrapper used for non-git command')
        self.assertEqual(offenders, [])

    def test_plugin_subprocess_is_only_python_core_invocation(self):
        source = (ROOT/'core/plugin.py').read_text()
        tree = ast.parse(source)
        calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Attribute) and isinstance(n.func.value, ast.Name)
                 and n.func.value.id == 'subprocess']
        self.assertEqual(len(calls), 1)
        segment = ast.get_source_segment(source, calls[0])
        self.assertIn('sys.executable', segment)
        self.assertIn("PACKAGE / 'core/shop.py'", segment)

    def test_shutdown_host_only_announces_success_on_verified_close(self):
        """A partial/recovery-required shutdown must never notify success."""
        source = (ROOT / 'core' / 'plugin.py').read_text()
        self.assertIn("document.get('decision')", source)
        self.assertIn("decision == 'closed'", source)
        # recovery_required/blocked/unknown fall through to the failure notification.
        self.assertIn("t('Shop shutdown incomplete')", source)
        self.assertNotIn("document.get('decision') or", source)
        # Shutdown execution requires Herdr plugin-action context.
        self.assertIn("HERDR_PLUGIN_ACTION_ID", source)

    def test_shutdown_core_keeps_single_subprocess_and_no_herdr_binary(self):
        source = (ROOT / 'core' / 'shutdown.py').read_text()
        self.assertNotIn('import subprocess', source)
        self.assertNotIn('subprocess.', source)
        self.assertNotIn('HERDR_BIN_PATH', source)
        self.assertNotIn('SHOP_HERDR_BIN', source)
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                    and isinstance(node.func.value, ast.Name) and node.func.value.id == 'subprocess':
                self.fail('shutdown.py must use the typed adapter, not subprocess')

if __name__=='__main__':unittest.main()
