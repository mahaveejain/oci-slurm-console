import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "hrun.sh"


class GpuHealthScriptTest(unittest.TestCase):
    """Exercise the wrapper with fake SSH/checks; never contact a node."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.trace = self.root / "trace.jsonl"
        self.env = dict(os.environ, PATH=f"{self.root}:/usr/bin:/bin",
                        HRUN_TEST_TRACE=str(self.trace), HRUN_TEST_ROOT=str(self.root))
        self.make_program("timeout", """
record('timeout', sys.argv[1:])
if sys.argv[3] == '105s' and os.environ.get('HRUN_TEST_TIMEOUT'):
    sys.exit(124)
sys.exit(subprocess.run(sys.argv[4:]).returncode)
""")
        self.make_program("ssh", """
record('ssh', sys.argv[1:])
if os.environ.get('HRUN_TEST_SSH_FAILURE'):
    sys.exit(255)
# Substitute only the privileged executable with our fixture before running
# the fixed remote command. No SSH or real healthcheck/sudo is executed.
remote = sys.argv[-1].replace('/usr/bin/sudo', os.environ['HRUN_TEST_ROOT'] + '/sudo')
sys.exit(subprocess.run(['/bin/bash', '-c', remote]).returncode)
""")
        self.make_program("hostname", """
record('hostname', sys.argv[1:])
print('node-fixture', flush=True)
sys.exit(int(os.environ.get('HRUN_TEST_HOSTNAME_EXIT', '0')))
""")
        self.make_program("nvidia-smi", """
record('nvidia-smi', sys.argv[1:])
print('nvidia-fixture', flush=True)
sys.exit(int(os.environ.get('HRUN_TEST_NVIDIA_EXIT', '0')))
""")
        self.make_program("sudo", """
record('sudo', sys.argv[1:])
print('healthcheck-fixture', flush=True)
sys.exit(int(os.environ.get('HRUN_TEST_HEALTH_EXIT', '0')))
""")

    def make_program(self, name, body):
        path = self.root / name
        path.write_text(f"#!{sys.executable}\n" + """
import json
import os
import subprocess
import sys
def record(name, args):
    with open(os.environ['HRUN_TEST_TRACE'], 'a') as stream:
        stream.write(json.dumps([name, args]) + '\\n')
""" + body)
        path.chmod(0o755)

    def run_script(self, *args, **env):
        return subprocess.run(["/bin/bash", str(SCRIPT), *args],
                              env=dict(self.env, **env), capture_output=True,
                              text=True, timeout=5)

    def recorded(self):
        if not self.trace.exists():
            return []
        return [json.loads(line) for line in self.trace.read_text().splitlines()]

    def test_rejects_hostlists_options_and_additional_commands_before_ssh(self):
        for args in ((), ("",), ("ALL",), ("all",), ("aLl",),
                     ("GPU-[1-3]",), ("GPU-1,GPU-2",), ("-oProxyCommand=id",),
                     ("user@GPU-1",), ("GPU-1;id",), ("GPU-1 id",),
                     ("GPU-1\nGPU-2",), ("a" * 129,), ("GPU-1", "--", "id")):
            with self.subTest(args=args):
                result = self.run_script(*args)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("one literal hostname", result.stderr)
                self.assertEqual(self.recorded(), [])

    def test_fixed_ssh_options_deadlines_and_diagnostic_sequence(self):
        result = self.run_script("h100-ord01-03-940")
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.recorded()
        self.assertEqual([call[0] for call in calls],
                         ["timeout", "ssh", "timeout", "hostname", "nvidia-smi", "sudo"])
        ssh_args = calls[1][1]
        self.assertEqual(ssh_args[:-1], [
            "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=15",
            "-o", "ServerAliveCountMax=2", "h100-ord01-03-940"])
        self.assertNotIn("StrictHostKeyChecking", " ".join(ssh_args))
        self.assertEqual(calls[0][1][:4],
                         ["--signal=TERM", "--kill-after=5s", "105s", "ssh"])
        self.assertEqual(calls[2][1][:5],
                         ["--signal=TERM", "--kill-after=5s", "85s", "/bin/bash", "-c"])
        self.assertEqual(calls[-1][1], [
            "-n", "/usr/bin/python3", "/opt/oci-hpc/healthchecks/check_gpu_setup.py"])
        self.assertLess(result.stdout.index("node-fixture"), result.stdout.index("nvidia-fixture"))
        self.assertLess(result.stdout.index("nvidia-fixture"), result.stdout.index("healthcheck-fixture"))
        self.assertIn("====== h100-ord01-03-940 ======", result.stdout)

    def test_failed_checks_do_not_skip_remaining_checks_or_report_success(self):
        for env, exit_code in (({"HRUN_TEST_HOSTNAME_EXIT": "3"}, 3),
                               ({"HRUN_TEST_NVIDIA_EXIT": "7"}, 7),
                               ({"HRUN_TEST_HEALTH_EXIT": "9"}, 9),
                               ({"HRUN_TEST_NVIDIA_EXIT": "7", "HRUN_TEST_HEALTH_EXIT": "9"}, 9)):
            with self.subTest(env=env):
                self.trace.unlink(missing_ok=True)
                result = self.run_script("GPU-1", **env)
                self.assertEqual(result.returncode, exit_code, result.stderr)
                self.assertEqual([call[0] for call in self.recorded()][-3:],
                                 ["hostname", "nvidia-smi", "sudo"])
                self.assertIn("healthcheck-fixture", result.stdout)

    def test_ssh_failure_and_timeout_propagate(self):
        for env, exit_code in (({"HRUN_TEST_SSH_FAILURE": "1"}, 255),
                               ({"HRUN_TEST_TIMEOUT": "1"}, 124)):
            with self.subTest(env=env):
                self.trace.unlink(missing_ok=True)
                result = self.run_script("GPU-1", **env)
                self.assertEqual(result.returncode, exit_code)
                self.assertNotIn("sudo", [call[0] for call in self.recorded()])


if __name__ == "__main__":
    unittest.main()
