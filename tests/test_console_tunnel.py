"""Exercise tunnel ownership and lifecycle with isolated fake SSH/network tools."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'slurm-console-tunnel.sh'


class TunnelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='tun-', dir='/tmp')
        self.root = Path(self.temp.name)
        self.script = self.root / 'tunnel.sh'
        shutil.copy(SCRIPT, self.script)
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        self.env = dict(os.environ, PATH=f'{self.bin}:{os.environ["PATH"]}',
                        SLURM_TUNNEL_NO_OPEN='1', HOME=str(self.root), MOCK_LOG=str(self.root / 'calls'))
        self.tool('ssh', '''import os, socket, sys
from pathlib import Path
args = sys.argv[1:]
with open(os.environ['MOCK_LOG'], 'a') as f: f.write(' '.join(args) + '\\n')
sock = Path(args[args.index('-S') + 1])
if '-O' in args:
    if not sock.exists(): sys.exit(1)
    if args[args.index('-O') + 1] == 'exit': sock.unlink()
else:
    if os.environ.get('MOCK_SSH_FAIL'): sys.exit(255)
    s = socket.socket(socket.AF_UNIX); s.bind(str(sock)); s.close()
''')
        self.tool('lsof', "import os, sys; sys.exit(0 if os.environ.get('MOCK_PORT_BUSY') else 1)")
        self.tool('launchctl', 'import sys; sys.exit(1)')
        self.tool('curl', "import os, sys; sys.exit(1 if os.environ.get('MOCK_HTTP_FAIL') else 0)")

    def tearDown(self):
        self.temp.cleanup()

    def tool(self, name, source):
        path = self.bin / name
        path.write_text(f'#!{shutil.which("python3")}\n' + source)
        path.chmod(0o755)

    def run_cmd(self, *args):
        return subprocess.run(['bash', str(self.script), *args], env=self.env,
                              capture_output=True, text=True)

    def test_start_reuse_switch_and_stop(self):
        result = self.run_cmd('start', '192.0.2.1')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('ubuntu@192.0.2.1', result.stdout)
        self.assertEqual(self.run_cmd('start').returncode, 0)
        calls = (self.root / 'calls').read_text()
        self.assertEqual(calls.count('-f -N -T'), 1)
        self.assertEqual(self.run_cmd('start', 'admin@192.0.2.2').returncode, 0)
        self.assertIn('admin@192.0.2.2', self.run_cmd('status').stdout)
        self.assertEqual(self.run_cmd('stop').returncode, 0)
        self.assertIn('stopped', self.run_cmd('status').stdout)
        self.assertEqual(self.run_cmd('start').returncode, 0)
        self.assertIn('admin@192.0.2.2', self.run_cmd('status').stdout)
        self.assertEqual(self.run_cmd('restart').returncode, 0)

    def test_does_not_take_over_unmanaged_listener(self):
        self.env['MOCK_PORT_BUSY'] = '1'
        result = self.run_cmd('start', '192.0.2.1')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('already occupied', result.stderr)
        self.assertFalse((self.root / 'calls').exists())
        self.assertEqual(self.run_cmd('stop').returncode, 0)

    def test_failed_ssh_does_not_save_target(self):
        self.env['MOCK_SSH_FAIL'] = '1'
        self.assertNotEqual(self.run_cmd('start', '192.0.2.1').returncode, 0)
        self.assertFalse((self.root / '.slurm-console-tunnel/8080.target').exists())

    def test_http_failure_reports_live_tunnel(self):
        self.env['MOCK_HTTP_FAIL'] = '1'
        result = self.run_cmd('start', '192.0.2.1')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('console is not responding', result.stderr)
        self.assertIn('running', self.run_cmd('status').stdout)

    def test_validation_and_separate_ports(self):
        for args in [('start', '-evil'), ('start', '192.0.2.1', '22'),
                     ('stop', 'junk'), ('run',)]:
            self.assertNotEqual(self.run_cmd(*args).returncode, 0)
        self.assertEqual(self.run_cmd('start', '192.0.2.1', '18080').returncode, 0)
        self.assertEqual(self.run_cmd('start', '192.0.2.2').returncode, 0)
        self.assertEqual(self.run_cmd('stop', '18080').returncode, 0)
        self.assertIn('ubuntu@192.0.2.2', self.run_cmd('status').stdout)


if __name__ == '__main__':
    unittest.main()
