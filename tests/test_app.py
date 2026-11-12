import unittest
import importlib.machinery
import importlib.util
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import Mock, patch

from app import (GPU_HEALTH_SCRIPT, GPU_TEST_PREFIX, _execution_args, _gpu_health_nodes,
                 _gpu_test_selection, _idle_gpu_nodes, _lifecycle_confirmation,
                 _stream_gpu_health, app, validate_command)
from app import NCCL_PREFIX, _nccl_selection, _idle_nccl_nodes
from app import GPU_FRYER_PREFIX, _gpu_fryer_selection, _gpu_fryer_nodes
from app import RDMA_SCRIPT, _rdma_selection
from app import _serial_selection, _worker_nodes
from app import TENANCY_SCRIPT


class SlurmConsoleTest(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()

    @patch("app.shutil.which", return_value=None)
    @patch("app.subprocess.run")
    def test_drained_picker_works_without_management_cli(self, run, _which):
        run.return_value = Mock(stdout="node-a|drained|maintenance\nnode-b|drained*|Not responding\n"
                                      "node-c|draining|jobs running\nnode-d|idle|none\n"
                                      "node-a|drained|maintenance\nALL|drained|invalid\n"
                                      "node[1-2]|drained|hostlist\n")
        response = self.client.get('/api/slurm/drained-nodes')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"nodes": [
            {"node": "node-a", "state": "drained", "reason": "maintenance"},
            {"node": "node-b", "state": "drained*", "reason": "Not responding"},
        ], "termination_available": False})

    @patch("app.subprocess.run", side_effect=subprocess.TimeoutExpired("sinfo", 15))
    def test_drained_picker_reports_inventory_failure(self, run):
        self.assertEqual(self.client.get('/api/slurm/drained-nodes').status_code, 503)

    @patch("app.LIFECYCLE_ACTIONS_ENABLED", True)
    @patch("app.shutil.which", return_value="/usr/local/bin/mgmt")
    @patch("app.subprocess.run")
    def test_termination_rechecks_drained_selection(self, run, _which):
        run.return_value = Mock(stdout="node-a|idle|none\n")
        response = self.client.post('/api/command', json={
            "command": "mgmt nodes tag-and-terminate --nodes node-a",
            "confirmation": "confirm",
        })
        self.assertEqual(response.status_code, 409)
        self.assertIn("fully drained", response.get_json()["error"])
        self.assertEqual(run.call_count, 1)

    def test_home_page_renders(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Slurm Console", response.data)

    def test_tenancy_lookup_uses_only_fixed_inventory(self):
        self.assertEqual(validate_command("tenancy_ocid.sh"), (["tenancy_ocid.sh"], None))
        self.assertEqual(_execution_args(["tenancy_ocid.sh"]),
                         ["/bin/bash", str(TENANCY_SCRIPT), "/etc/ansible/hosts"])
        for raw in ("tenancy_ocid.sh /etc/passwd", "tenancy_ocid.sh --help", "tenancy_ocid.sh; whoami"):
            self.assertIsNotNone(validate_command(raw)[1])

    def test_tenancy_script_handles_unique_missing_and_conflicting_values(self):
        for content, expected, code in (
            ('  tenancy_ocid = "ocid1.tenancy.oc1..example" # comment\ntenancy_ocid=\'ocid1.tenancy.oc1..example\'\n', 'ocid1.tenancy.oc1..example', 0),
            ('tenancy_ocid=ocid1.tenancy.oc1..example\n', 'ocid1.tenancy.oc1..example', 0),
            ('[nodes]\n', 'tenancy_ocid not found', 1),
            ('tenancy_ocid=one\ntenancy_ocid=two\n', 'Multiple distinct', 1),
        ):
            with self.subTest(content=content), tempfile.NamedTemporaryFile(mode='w') as inventory:
                inventory.write(content)
                inventory.flush()
                result = subprocess.run(['/bin/bash', str(TENANCY_SCRIPT), inventory.name], capture_output=True, text=True)
                self.assertEqual(result.returncode, code)
                self.assertIn(expected, result.stdout + result.stderr)

    @patch("app._stream_fixed_helper", return_value=iter(["data: ocid1.tenancy.oc1..example\n\n"]))
    def test_tenancy_lookup_streams_to_output(self, stream):
        response = self.client.post('/api/command', json={"command": "tenancy_ocid.sh"})
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'ocid1.tenancy.oc1..example', response.data)
        stream.assert_called_once_with(["tenancy_ocid.sh"], 15)

    @patch("app.shutil.which", return_value="/usr/bin/tool")
    def test_rdma_command_is_fixed_and_requires_distinct_nodes(self, _which):
        self.assertEqual(_rdma_selection(["rdma_per_link_bw_test.py", "GPU-1", "GPU-2"]), ("GPU-1", "GPU-2"))
        self.assertEqual(_execution_args(["rdma_per_link_bw_test.py", "GPU-1", "GPU-2"]),
                         ["/usr/bin/env", "python3", str(RDMA_SCRIPT), "GPU-1", "GPU-2"])
        for raw in ("rdma_per_link_bw_test.py GPU-1 GPU-1", "rdma_per_link_bw_test.py ALL GPU-2",
                    "rdma_per_link_bw_test.py GPU-1 GPU-2 extra", "rdma_per_link_bw_test.py -oProxyCommand=x GPU-2"):
            with self.subTest(raw=raw):
                self.assertIsNotNone(validate_command(raw)[1])

    @patch("app.shutil.which", return_value="/usr/bin/tool")
    @patch("app._idle_gpu_nodes", return_value=[{"node": "GPU-1"}])
    def test_rdma_command_rechecks_idle_nodes(self, _nodes, _which):
        response = self.client.post("/api/command", json={
            "command": "rdma_per_link_bw_test.py GPU-1 GPU-2",
            "confirmation": "confirm"})
        self.assertEqual(response.status_code, 409)
        self.assertIn("idle", response.get_json()["error"])

    @patch("app.subprocess.run")
    def test_worker_inventory_is_literal_and_deduplicated(self, run):
        run.return_value = Mock(stdout=" GPU-1 \nGPU-2\nGPU-1\nALL\nGPU-[3-4]\n-oProxyCommand=bad\n")
        self.assertEqual(_worker_nodes(), ["GPU-1", "GPU-2"])
        self.assertEqual(run.call_args.args[0], ["sinfo", "-N", "-h", "-o", "%N"])

    @patch("app.shutil.which", return_value="/usr/bin/tool")
    def test_serial_lookup_is_fixed_to_one_worker_and_one_command(self, _which):
        self.assertEqual(_serial_selection(["node-serial", "GPU-1"]), "GPU-1")
        self.assertEqual(validate_command("node-serial GPU-1"), (["node-serial", "GPU-1"], None))
        self.assertEqual(_execution_args(["node-serial", "GPU-1"])[-2:],
                         ["GPU-1", "sudo -n dmidecode -s system-serial-number"])
        for raw in ("node-serial", "node-serial ALL", "node-serial GPU-1 GPU-2",
                    "node-serial GPU-[1-2]", "node-serial -oProxyCommand=x",
                    "node-serial 'GPU-1;whoami'"):
            with self.subTest(raw=raw):
                self.assertIsNotNone(validate_command(raw)[1])

    @patch("app.shutil.which", return_value="/usr/bin/tool")
    @patch("app._worker_nodes", return_value=["GPU-1"])
    @patch("app._stream_fixed_helper")
    def test_serial_lookup_rechecks_worker_inventory(self, stream, _nodes, _which):
        response = self.client.post("/api/command", json={"command": "node-serial GPU-2"})
        self.assertEqual(response.status_code, 409)
        stream.assert_not_called()

    @patch("app.NODE_STATE_SUDO_ENABLED", True)
    def test_sudo_only_for_explicit_node_updates(self):
        drain = ["scontrol", "update", "NodeName=GPU-5443", "State=DRAIN", "Reason=testing"]
        self.assertEqual(_execution_args(drain), ["/usr/bin/sudo", "-n", "--",
            "/usr/local/sbin/slurm-console-node-state", *drain[2:]])
        for args in (["squeue"], ["scontrol", "show", "nodes"],
                     ["scontrol", "update", "JobId=1", "Priority=100"],
                     ["mgmt", "status"]):
            self.assertEqual(_execution_args(args), args)

    @patch("app.LIFECYCLE_ACTIONS_ENABLED", False)
    def test_disabled_node_updates(self):
        args, error = validate_command('scontrol update NodeName=GPU-5443 State=DRAIN Reason="testing"')
        self.assertIsNone(args)
        self.assertIn("not enabled", error)

    @patch("app.LIFECYCLE_ACTIONS_ENABLED", True)
    @patch("app.shutil.which", return_value="/usr/local/bin/scontrol")
    @patch("app.NODE_STATE_SUDO_ENABLED", True)
    @patch("app.subprocess.Popen")
    def test_node_update_requires_confirmation_and_uses_helper(self, popen, _which):
        raw = 'scontrol update NodeName=GPU-5443 State=DRAIN Reason="testing"'
        response = self.client.post('/api/command', json={"command": raw})
        self.assertEqual(response.status_code, 400)
        popen.assert_not_called()
        popen.return_value.stdout.readline.return_value = ""
        popen.return_value.wait.return_value = 0
        response = self.client.post('/api/command', json={"command": raw, "confirmation": "confirm"})
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'"exit_code": 0', response.data)
        self.assertEqual(popen.call_args.args[0], ["/usr/bin/sudo", "-n", "--",
            "/usr/local/sbin/slurm-console-node-state", "NodeName=GPU-5443", "State=DRAIN", "Reason=testing"])

    def test_root_helper_rejects_other_actions(self):
        path = str(Path(__file__).resolve().parents[1] / 'deploy/slurm-console-node-state')
        loader = importlib.machinery.SourceFileLoader('node_state_helper', path)
        spec = importlib.util.spec_from_loader(loader.name, loader)
        helper = importlib.util.module_from_spec(spec)
        loader.exec_module(helper)
        for args in (["NodeName=GPU-5443", "State=DRAIN", "Reason=testing"],
                     ["NodeName=GPU-5443,GPU-8124", "State=RESUME"]):
            self.assertEqual(helper.validate(args), args)
        for args in ([], ["NodeName=ALL", "State=RESUME"],
                     ["NodeName=GPU-5443,ALL", "State=RESUME"],
                     ["NodeName=GPU-[1-5]", "State=RESUME"],
                     ["NodeName=GPU-5443", "State=DOWN"],
                     ["NodeName=GPU-5443", "State=DRAIN"],
                     ["NodeName=GPU-5443", "State=DRAIN", "Reason="],
                     ["NodeName=GPU-5443", "State=RESUME", "Reason=test"],
                     ["NodeName=GPU-5443", "State=RESUME", "JobId=1"]):
            with self.subTest(args=args), self.assertRaises(ValueError):
                helper.validate(args)

    @patch("app.subprocess.run")
    def test_only_idle_gpu_nodes_in_up_partitions(self, run):
        run.return_value = Mock(stdout="\n".join([
            "GPU-1|idle|gpu:A100:8(S:0-7)|compute*|up",
            "GPU-1|idle|gpu:A100:8(S:0-7)|health|up",
            "GPU-2|allocated|gpu:A100:8|compute*|up",
            "GPU-3|mixed|gpu:A100:8|compute*|up",
            "GPU-4|idle*|gpu:A100:8|compute*|up",
            "GPU-5|drained|gpu:A100:8|compute*|up",
            "GPU-6|idle|(null)|compute*|up",
            "GPU-7|idle|gpu:A100:8|compute*|down",
            "GPU-8|idle|gpu:1|compute*|up",
        ]))
        self.assertEqual(_idle_gpu_nodes(), [
            {"node": "GPU-1", "partition": "compute", "gres": "gpu:A100:8(S:0-7)"},
            {"node": "GPU-8", "partition": "compute", "gres": "gpu:1"},
        ])

    @patch("app.subprocess.run")
    def test_gpu_health_inventory_includes_equipped_nodes_and_deduplicates(self, run):
        run.return_value = Mock(stdout="\n".join([
            "GPU-1|gpu:A100:8(S:0-7)", "GPU-1|gpu:A100:8(S:0-7)",
            "GPU-2|gpu:1", "GPU-3|gpu:0", "CPU-1|(null)",
            "GPU-4|mps:100,gpu:H100:4", "GPU-[5-6]|gpu:8",
            "ALL|gpu:8", "-oProxyCommand=bad|gpu:8", "bad name|gpu:8",
            "GPU-7|gpu:08", "GPU-8|gpu:1|unexpected", "GPU-9|gpu:A100:16",
        ]))
        self.assertEqual(_gpu_health_nodes(), ["GPU-1", "GPU-2", "GPU-4", "GPU-9"])
        self.assertEqual(run.call_args.args[0], ["sinfo", "-N", "-h", "-o", "%N|%G"])
        self.assertEqual(run.call_args.kwargs["timeout"], 15)

    @patch("app.LIFECYCLE_ACTIONS_ENABLED", False)
    @patch("app.ALLOWED_COMMANDS", {"squeue"})
    @patch("app.subprocess.run")
    def test_gpu_health_inventory_is_independent_of_lifecycle_and_allowlist(self, run):
        run.return_value = Mock(stdout="drained-gpu|gpu:H100:8\nallocated-gpu|gpu:8\n")
        response = self.client.get("/api/troubleshooting/gpu-nodes")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"nodes": ["allocated-gpu", "drained-gpu"]})

    @patch("app.subprocess.run", side_effect=subprocess.TimeoutExpired("sinfo", 15))
    def test_gpu_health_inventory_failure_is_reported(self, run):
        response = self.client.get("/api/troubleshooting/gpu-nodes")
        self.assertEqual(response.status_code, 503)
        self.assertIn("Could not load", response.get_json()["error"])

    @patch("app.LIFECYCLE_ACTIONS_ENABLED", False)
    @patch("app.ALLOWED_COMMANDS", {"squeue"})
    @patch("app.Path.is_file", return_value=True)
    @patch("app.os.access", return_value=True)
    @patch("app.shutil.which", return_value="/usr/bin/sinfo")
    def test_gpu_health_accepts_only_one_literal_node_without_lifecycle_optin(self, _which, _access, _file):
        self.assertEqual(validate_command("hrun.sh GPU-1"), (["hrun.sh", "GPU-1"], None))
        self.assertEqual(_execution_args(["hrun.sh", "GPU-1"]), [str(GPU_HEALTH_SCRIPT), "GPU-1"])
        for raw in ("hrun.sh", "hrun.sh ALL", "hrun.sh -GPU-1", "hrun.sh -- GPU-1",
                    "hrun.sh GPU-1 GPU-2", "hrun.sh GPU-[1-2]", "hrun.sh GPU-1,GPU-2",
                    "hrun.sh GPU-1 -- nvidia-smi", "hrun.sh GPU-1; whoami",
                    "hrun.sh 'GPU-1$(whoami)'", "hrun.sh 'GPU-1|whoami'",
                    "hrun.sh '-oProxyCommand=whoami'", "./hrun.sh GPU-1"):
            with self.subTest(raw=raw):
                self.assertIsNone(validate_command(raw)[0])

    @patch("app.Path.is_file", return_value=True)
    @patch("app.os.access", return_value=True)
    @patch("app.shutil.which", return_value="/usr/bin/sinfo")
    @patch("app._gpu_health_nodes", return_value=["GPU-1"])
    @patch("app.subprocess.Popen")
    def test_gpu_health_rechecks_node_and_never_launches_unknown_or_invalid_hosts(self, popen, inventory, _which, _access, _file):
        for raw, status in (("hrun.sh GPU-2", 409), ("hrun.sh ALL", 400),
                            ("hrun.sh GPU-1 -- whoami", 400), ("hrun.sh -oBad", 400)):
            with self.subTest(raw=raw):
                response = self.client.post("/api/command", json={"command": raw})
                self.assertEqual(response.status_code, status)
        inventory.assert_called_once()
        popen.assert_not_called()

    @patch("app.Path.is_file", return_value=True)
    @patch("app.os.access", return_value=True)
    @patch("app.shutil.which", return_value="/usr/bin/sinfo")
    @patch("app._gpu_health_nodes", side_effect=OSError("inventory failed"))
    @patch("app.subprocess.Popen")
    def test_gpu_health_recheck_failure_prevents_execution(self, popen, inventory, _which, _access, _file):
        response = self.client.post("/api/command", json={"command": "hrun.sh GPU-1"})
        self.assertEqual(response.status_code, 503)
        self.assertIn("no health-check", response.get_json()["error"])
        popen.assert_not_called()

    @patch("app.Path.is_file", return_value=True)
    @patch("app.os.access", return_value=True)
    @patch("app.shutil.which", return_value="/usr/bin/sinfo")
    @patch("app._gpu_health_nodes", return_value=["GPU-1"])
    @patch("app._stream_gpu_health", return_value=[
        "data: ====== GPU-1 ======\n\n", "data: GPU check failed\n\n",
        'event: complete\ndata: {"exit_code": 3}\n\n',
    ])
    def test_gpu_health_uses_command_sse_and_reports_nonzero_result(self, stream, inventory, _which, _access, _file):
        response = self.client.post("/api/command", json={"command": "hrun.sh GPU-1"})
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"GPU check failed", response.data)
        self.assertIn(b'"exit_code": 3', response.data)
        stream.assert_called_once_with(["hrun.sh", "GPU-1"])

    def test_gpu_health_streams_stdout_and_exit_code_without_shell(self):
        real_popen = subprocess.Popen
        launched = []

        def fake_check(args, **kwargs):
            self.assertEqual(args, [str(GPU_HEALTH_SCRIPT), "GPU-1"])
            self.assertTrue(kwargs["start_new_session"])
            self.assertNotIn("shell", kwargs)
            process = real_popen([sys.executable, "-c", "print('first'); print('last', end=''); raise SystemExit(3)"], **kwargs)
            launched.append(process)
            return process

        with patch("app.subprocess.Popen", side_effect=fake_check):
            events = "".join(_stream_gpu_health(["hrun.sh", "GPU-1"]))
        self.assertIn("data: first\n\n", events)
        self.assertIn("data: last\n\n", events)
        self.assertIn('"exit_code": 3', events)
        self.assertTrue(launched[0].stdout.closed)

    def test_gpu_health_silent_command_has_wall_deadline_and_is_reaped(self):
        real_popen = subprocess.Popen
        launched = []

        def fake_check(args, **kwargs):
            process = real_popen([sys.executable, "-c", "import time; time.sleep(10)"], **kwargs)
            launched.append(process)
            return process

        started = time.monotonic()
        with patch("app.subprocess.Popen", side_effect=fake_check), patch("app.GPU_HEALTH_TIMEOUT_SECONDS", 0.1):
            with self.assertRaises(subprocess.TimeoutExpired):
                list(_stream_gpu_health(["hrun.sh", "GPU-1"]))
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(launched[0].returncode, -signal.SIGKILL)
        self.assertTrue(launched[0].stdout.closed)

    def test_gpu_health_disconnect_stops_process_group_and_reaps_helper(self):
        real_popen = subprocess.Popen
        launched = []

        def fake_check(args, **kwargs):
            process = real_popen([sys.executable, "-u", "-c", "import time; print('started'); time.sleep(10)"], **kwargs)
            launched.append(process)
            return process

        with patch("app.subprocess.Popen", side_effect=fake_check), patch("app.os.killpg", wraps=os.killpg) as killpg:
            stream = _stream_gpu_health(["hrun.sh", "GPU-1"])
            self.assertEqual(next(stream), "data: started\n\n")
            stream.close()
            killpg.assert_called_once_with(launched[0].pid, signal.SIGKILL)
        self.assertEqual(launched[0].returncode, -signal.SIGKILL)
        self.assertTrue(launched[0].stdout.closed)

    @patch("app.LIFECYCLE_ACTIONS_ENABLED", True)
    @patch("app.shutil.which", return_value="/usr/local/bin/sbatch")
    def test_only_fixed_gpu_test_is_submitted(self, _which):
        args = [*GPU_TEST_PREFIX, "--nodelist=GPU-1", "--partition=compute", "gpu-test-30s.sh"]
        self.assertEqual(_gpu_test_selection(args), ("GPU-1", "compute"))
        self.assertEqual(validate_command(" ".join(args)), (args, None))
        self.assertEqual(_lifecycle_confirmation(args), "confirm")
        for raw in ("sbatch arbitrary.sh", " ".join(args).replace("GPU-1", "ALL"),
                    " ".join(args).replace("--gres=gpu:1", "--gres=gpu:8"),
                    " ".join(args).replace("GPU-1", "GPU-1,GPU-2")):
            with self.subTest(raw=raw):
                self.assertIsNone(validate_command(raw)[0])

    @patch("app.LIFECYCLE_ACTIONS_ENABLED", True)
    @patch("app.shutil.which", return_value="/usr/local/bin/sbatch")
    @patch("app._idle_gpu_nodes", return_value=[])
    @patch("app.subprocess.run")
    def test_gpu_test_rechecks_availability_and_requires_confirmation(self, run, inventory, _which):
        raw = " ".join([*GPU_TEST_PREFIX, "--nodelist=GPU-1", "--partition=compute", "gpu-test-30s.sh"])
        response = self.client.post('/api/command', json={"command": raw})
        self.assertEqual(response.status_code, 400)
        inventory.assert_not_called()
        response = self.client.post('/api/command', json={"command": raw, "confirmation": "confirm"})
        self.assertEqual(response.status_code, 409)
        run.assert_not_called()

    @patch("app.LIFECYCLE_ACTIONS_ENABLED", True)
    @patch("app.shutil.which", return_value="/usr/local/bin/sbatch")
    @patch("app._idle_gpu_nodes", return_value=[{"node": "GPU-1", "partition": "compute"}])
    @patch("app.subprocess.run")
    @patch("app.Path.mkdir")
    def test_gpu_test_transports_fixed_script_and_reports_job_id(self, mkdir, run, inventory, _which):
        run.return_value = Mock(returncode=0, stdout="1234\n", stderr="")
        raw = " ".join([*GPU_TEST_PREFIX, "--nodelist=GPU-1", "--partition=compute", "gpu-test-30s.sh"])
        response = self.client.post('/api/command', json={"command": raw, "confirmation": "confirm"})
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'Submitted GPU test job 1234', response.data)
        self.assertIn(b'may still be queued or running', response.data)
        self.assertIn("--export=NONE", run.call_args.args[0])
        self.assertNotIn("sudo", run.call_args.args[0])
        self.assertIn("compute<<<1024, 256>>>", run.call_args.kwargs['input'])

    @patch("app.os.open", side_effect=FileNotFoundError)
    def test_gpu_test_log_pending(self, open_log):
        response = self.client.get('/api/slurm/gpu-test-output/1234')
        self.assertEqual(response.status_code, 200)
        self.assertIn('may be queued', response.get_json()['output'])
        self.assertEqual(Path(open_log.call_args.args[0]).name, 'gpu-test-1234.out')
        self.assertEqual(self.client.get('/api/slurm/gpu-test-output/0').status_code, 400)
        self.assertEqual(self.client.get('/api/slurm/gpu-test-output/../../app.py').status_code, 404)

    @patch("app.subprocess.run")
    def test_nccl_inventory_requires_idle_eight_gpu_nodes(self, run):
        run.return_value = Mock(stdout="\n".join([
            "GPU-1|idle|gpu:H100:8|compute*|up",
            "GPU-2|idle|gpu:H100:8|compute|up",
            "GPU-3|idle|gpu:H100:4|compute|up",
            "GPU-4|mixed|gpu:H100:8|compute|up",
            "GPU-5|idle|gpu:H100:8|compute|down",
        ]))
        self.assertEqual([item["node"] for item in _idle_nccl_nodes()], ["GPU-1", "GPU-2"])

    @patch("app.LIFECYCLE_ACTIONS_ENABLED", True)
    @patch("app.shutil.which", return_value="/usr/local/bin/sbatch")
    def test_nccl_command_rejects_duplicate_nodes_and_hostlists(self, _which):
        args = [*NCCL_PREFIX, "--nodelist=GPU-1,GPU-2", "--partition=compute", "nccl_run_allreduce.sbatch"]
        self.assertEqual(_nccl_selection(args), (["GPU-1", "GPU-2"], "compute"))
        self.assertEqual(validate_command(" ".join(args)), (args, None))
        self.assertEqual(_lifecycle_confirmation(args), "confirm")
        for raw in (" ".join(args).replace("GPU-2", "GPU-1"),
                    " ".join(args).replace("GPU-2", "GPU-[2-3]"),
                    " ".join(args).replace("--gpus-per-node=8", "--gpus-per-node=1")):
            with self.subTest(raw=raw):
                self.assertIsNone(validate_command(raw)[0])

    @patch("app.LIFECYCLE_ACTIONS_ENABLED", True)
    @patch("app.shutil.which", return_value="/usr/local/bin/sbatch")
    @patch("app._idle_nccl_nodes", return_value=[{"node": "GPU-1", "partition": "compute", "gres": "gpu:H100:8"}, {"node": "GPU-2", "partition": "compute", "gres": "gpu:H100:8"}])
    @patch("app.subprocess.run")
    @patch("app.Path.mkdir")
    def test_nccl_submission_uses_fixed_script_and_shared_log(self, mkdir, run, inventory, _which):
        run.return_value = Mock(returncode=0, stdout="5651\n", stderr="")
        raw = " ".join([*NCCL_PREFIX, "--nodelist=GPU-1,GPU-2", "--partition=compute", "nccl_run_allreduce.sbatch"])
        response = self.client.post('/api/command', json={"command": raw})
        self.assertEqual(response.status_code, 400)
        run.assert_not_called()
        response = self.client.post('/api/command', json={"command": raw, "confirmation": "confirm"})
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'Submitted NCCL all-reduce job 5651', response.data)
        self.assertIn(b'"job_id": 5651', response.data)
        self.assertIn("--output=", " ".join(run.call_args.args[0]))
        self.assertIn("--export=ALL", run.call_args.args[0])
        self.assertIn("all_reduce_perf", run.call_args.kwargs["input"])

    @patch("app.LIFECYCLE_ACTIONS_ENABLED", True)
    @patch("app.shutil.which", return_value="/usr/local/bin/sbatch")
    @patch("app._idle_nccl_nodes", return_value=[{"node": "GPU-1", "partition": "compute", "gres": "gpu:H100:8"}])
    @patch("app.subprocess.run")
    def test_nccl_rejects_node_that_became_busy(self, run, inventory, _which):
        raw = " ".join([*NCCL_PREFIX, "--nodelist=GPU-1,GPU-2", "--partition=compute", "nccl_run_allreduce.sbatch"])
        response = self.client.post('/api/command', json={"command": raw, "confirmation": "confirm"})
        self.assertEqual(response.status_code, 409)
        run.assert_not_called()

    @patch("app.subprocess.run")
    def test_nccl_output_reports_measured_bandwidth(self, run):
        run.return_value = Mock(stdout="5651|COMPLETED\n")
        with tempfile.TemporaryDirectory() as temp_dir, patch("app.NCCL_LOG_DIR", Path(temp_dir)):
            (Path(temp_dir) / "nccl-5651.out").write_text(
                "# Avg bus bandwidth    : 186.713\n# Collective test concluded: all_reduce_perf\n")
            response = self.client.get('/api/troubleshooting/nccl-output/5651')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["avg_bus_bandwidth_gbps"], 186.713)
        self.assertEqual(response.get_json()["job_state"], "COMPLETED")
        self.assertTrue(response.get_json()["concluded"])
        self.assertTrue(response.get_json()["log_available"])

    @patch("app.subprocess.run", return_value=Mock(stdout="77367|COMPLETED\n"))
    def test_nccl_standard_summary_without_optional_marker(self, run):
        with tempfile.TemporaryDirectory() as temp_dir, patch("app.NCCL_LOG_DIR", Path(temp_dir)):
            path = Path(temp_dir) / "nccl-77367.out"
            for errors, expected in [("0 OK", True), ("1 FAILED", False)]:
                with self.subTest(validation=errors):
                    path.write_text(f"# Out of bounds values : {errors}\n# Avg bus bandwidth    : 463.839\n#\n")
                    response = self.client.get('/api/troubleshooting/nccl-output/77367')
                    self.assertEqual(response.get_json()["avg_bus_bandwidth_gbps"], 463.839)
                    self.assertEqual(response.get_json()["concluded"], expected)
                    self.assertEqual(response.headers['Cache-Control'], 'no-store')

    @patch("app._idle_gpu_nodes", return_value=[
        {"node": "GPU-1", "partition": "compute", "gres": "gpu:A100:8(S:0-7)"},
        {"node": "GPU-2", "partition": "compute", "gres": "gpu:2"},
    ])
    def test_gpu_fryer_inventory_counts_allocatable_gpus(self, inventory):
        self.assertEqual([(item["node"], item["gpu_count"]) for item in _gpu_fryer_nodes()],
                         [("GPU-1", 8), ("GPU-2", 2)])

    @patch("app.LIFECYCLE_ACTIONS_ENABLED", True)
    @patch("app.shutil.which", return_value="/usr/local/bin/sbatch")
    def test_gpu_fryer_accepts_only_fixed_one_node_job(self, _which):
        args = [*GPU_FRYER_PREFIX, "--gres=gpu:8", "--nodelist=GPU-1", "--partition=compute", "gpu-fryer-120s.sbatch"]
        self.assertEqual(_gpu_fryer_selection(args), ("GPU-1", "compute", 8))
        self.assertEqual(validate_command(" ".join(args)), (args, None))
        self.assertEqual(_lifecycle_confirmation(args), "confirm")
        for raw in (" ".join(args).replace("GPU-1", "ALL"),
                    " ".join(args).replace("GPU-1", "GPU-1,GPU-2"),
                    " ".join(args).replace("--time=00:20:00", "--time=24:00:00")):
            with self.subTest(raw=raw):
                self.assertIsNone(validate_command(raw)[0])

    @patch("app.LIFECYCLE_ACTIONS_ENABLED", True)
    @patch("app.shutil.which", return_value="/usr/local/bin/sbatch")
    @patch("app._gpu_fryer_nodes", return_value=[{"node": "GPU-1", "partition": "compute", "gpu_count": 8}])
    @patch("app.subprocess.run")
    @patch("app.Path.mkdir")
    def test_gpu_fryer_submits_fixed_install_and_run_script(self, mkdir, run, inventory, _which):
        run.return_value = Mock(returncode=0, stdout="6000\n", stderr="")
        raw = " ".join([*GPU_FRYER_PREFIX, "--gres=gpu:8", "--nodelist=GPU-1", "--partition=compute", "gpu-fryer-120s.sbatch"])
        response = self.client.post('/api/command', json={"command": raw})
        self.assertEqual(response.status_code, 400)
        run.assert_not_called()
        response = self.client.post('/api/command', json={"command": raw, "confirmation": "confirm"})
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'Submitted gpu-fryer job 6000', response.data)
        self.assertIn("--output=", " ".join(run.call_args.args[0]))
        self.assertIn("cargo\" install --git https://github.com/huggingface/gpu-fryer.git", run.call_args.kwargs["input"])
        self.assertIn('"$fryer" "${nvml_args[@]}" 120', run.call_args.kwargs["input"])

    @patch("app.LIFECYCLE_ACTIONS_ENABLED", True)
    @patch("app.shutil.which", return_value="/usr/local/bin/sbatch")
    @patch("app._gpu_fryer_nodes", return_value=[])
    @patch("app.subprocess.run")
    def test_gpu_fryer_refuses_node_that_became_busy(self, run, inventory, _which):
        raw = " ".join([*GPU_FRYER_PREFIX, "--gres=gpu:8", "--nodelist=GPU-1", "--partition=compute", "gpu-fryer-120s.sbatch"])
        response = self.client.post('/api/command', json={"command": raw, "confirmation": "confirm"})
        self.assertEqual(response.status_code, 409)
        run.assert_not_called()

    @patch("app.subprocess.run", return_value=Mock(stdout="6000|COMPLETED\n"))
    def test_gpu_fryer_output_reports_health(self, run):
        with tempfile.TemporaryDirectory() as temp_dir, patch("app.GPU_FRYER_LOG_DIR", Path(temp_dir)):
            (Path(temp_dir) / "gpu-fryer-6000.out").write_text("GPU #0: 100 Gflops/s\nAll GPUs seem healthy\n")
            response = self.client.get('/api/troubleshooting/gpu-fryer-output/6000')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["job_state"], "COMPLETED")
        self.assertTrue(response.get_json()["healthy"])
        self.assertTrue(response.get_json()["log_available"])

    def test_rejects_non_slurm_and_shell_commands(self):
        for command in ("whoami", "squeue; whoami", "squeue | whoami", ""):
            with self.subTest(command=command):
                response = self.client.post("/api/command", json={"command": command})
                self.assertEqual(response.status_code, 400)

    @patch("app.shutil.which", return_value="/usr/local/bin/mgmt")
    @patch("app.subprocess.run")
    def test_cluster_dropdown_inventory(self, run, _which):
        run.return_value = Mock(stdout='["cluster-a", "cluster-b"]')
        response = self.client.get("/api/management/clusters")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"clusters": ["cluster-a", "cluster-b"]})
        run.assert_called_once()

    @patch("app.shutil.which", return_value="/usr/local/bin/mgmt")
    def test_allows_only_read_only_oci_hpc_management_commands(self, _which):
        for command in (
            "mgmt nodes list --cluster example --fields role=compute --format json",
            "mgmt fabrics list --rack-state AVAILABLE",
            "mgmt images list --format json --used",
            "mgmt network rails list --cluster example --nodes",
            "mgmt configurations list --role compute --format yaml --shape BM.GPU.GB300.4",
        ):
            with self.subTest(command=command):
                args, error = validate_command(command)
                self.assertIsNotNone(args)
                self.assertIsNone(error)

        args, error = validate_command("mgmt nodes terminate --nodes node01")
        self.assertIsNone(args)
        self.assertIn("not enabled", error)

    @patch("app.LIFECYCLE_ACTIONS_ENABLED", True)
    @patch("app.shutil.which", return_value="/usr/local/bin/mgmt")
    def test_allows_narrow_lifecycle_actions_with_confirmation(self, _which):
        args, error = validate_command("mgmt nodes terminate --nodes GPU-3127,GPU-7648")
        self.assertEqual(args, ["mgmt", "nodes", "terminate", "--nodes", "GPU-3127,GPU-7648"])
        self.assertIsNone(error)
        self.assertEqual(_lifecycle_confirmation(args), "confirm")

        args, error = validate_command("mgmt clusters add node --count 2 --cluster expert-tortoise")
        self.assertIsNotNone(args)
        self.assertIsNone(error)
        self.assertEqual(_lifecycle_confirmation(args), "confirm")

        args, error = validate_command('scontrol update NodeName=GPU-3127 State=DRAIN Reason="Planned maintenance"')
        self.assertIsNotNone(args)
        self.assertIsNone(error)
        self.assertEqual(_lifecycle_confirmation(args), "confirm")

        args, error = validate_command("scontrol update NodeName=ALL State=DRAIN")
        self.assertIsNone(args)
        self.assertIn("ALL is not permitted", error)

        args, error = validate_command("mgmt nodes list --width 200")
        self.assertIsNone(args)
        self.assertIn("not enabled", error)
