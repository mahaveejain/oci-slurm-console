"""Slurm Console - a small, controller-side Flask command console.

Run behind the controller's existing authentication proxy.  The application
never invokes a shell: submitted commands are tokenized and executed directly.
"""

from __future__ import annotations

import codecs
import json
import os
import re
import selectors
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path
from datetime import datetime, timezone

from flask import Flask, Response, jsonify, render_template, request, stream_with_context

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024

# Keep this narrow by default.  Add site-approved commands through the
# SLURM_CONSOLE_ALLOWED_COMMANDS environment variable when deploying.
DEFAULT_ALLOWED = {"squeue", "sinfo", "scontrol", "sacct", "sstat", "sbatch", "scancel", "mgmt"}
ALLOWED_COMMANDS = {
    item.strip()
    for item in os.environ.get("SLURM_CONSOLE_ALLOWED_COMMANDS", ",".join(DEFAULT_ALLOWED)).split(",")
    if item.strip()
}
BLOCKED_TOKENS = {";", "&&", "||", "|", ">", ">>", "<", "<<", "`", "$(", "${"}
MAX_TIMEOUT_SECONDS = int(os.environ.get("SLURM_CONSOLE_TIMEOUT", "60"))
# Lifecycle mutations are intentionally an explicit deployment opt-in.  The
# UI still requires a per-command typed confirmation before execution.
LIFECYCLE_ACTIONS_ENABLED = os.environ.get("SLURM_CONSOLE_ENABLE_LIFECYCLE_ACTIONS") == "1"
NODE_STATE_SUDO_ENABLED = os.environ.get("SLURM_CONSOLE_NODE_STATE_SUDO") == "1"
NODE_STATE_HELPER = "/usr/local/sbin/slurm-console-node-state"
GPU_TEST_SCRIPT = Path(__file__).resolve().parent / "scripts" / "gpu-test-30s.sh"
GPU_TEST_LOG_DIR = Path(os.environ.get("SLURM_CONSOLE_GPU_TEST_LOG_DIR", "/fss/slurm-console-ubuntu/gpu-tests"))
GPU_TEST_PREFIX = ["sbatch", "--nodes=1", "--ntasks=1", "--cpus-per-task=2", "--mem=2G", "--gres=gpu:1", "--time=00:02:00"]
NCCL_SCRIPT = Path(__file__).resolve().parent / "scripts" / "nccl_run_allreduce.sbatch"
NCCL_LOG_DIR = Path(os.environ.get("SLURM_CONSOLE_NCCL_LOG_DIR", "/config/logs/slurm-console-nccl"))
NCCL_PREFIX = ["sbatch", "--nodes=2", "--gpus-per-node=8", "--ntasks-per-node=8", "--exclusive"]
GPU_FRYER_SCRIPT = Path(__file__).resolve().parent / "scripts" / "gpu-fryer-120s.sbatch"
GPU_FRYER_LOG_DIR = Path(os.environ.get("SLURM_CONSOLE_GPU_FRYER_LOG_DIR", "/config/logs/slurm-console-gpu-fryer"))
GPU_FRYER_PREFIX = ["sbatch", "--nodes=1", "--ntasks=1", "--exclusive", "--time=00:20:00"]
GPU_HEALTH_SCRIPT = Path(__file__).resolve().parent / "scripts" / "hrun.sh"
GPU_HEALTH_TIMEOUT_SECONDS = 120
RDMA_SCRIPT = Path(__file__).resolve().parent / "scripts" / "rdma_per_link_bw_test.py"
RDMA_TIMEOUT_SECONDS = 600
SERIAL_TIMEOUT_SECONDS = 45
TENANCY_SCRIPT = Path(__file__).resolve().parent / "scripts" / "tenancy_ocid.sh"
CAPTOP_SCRIPT = Path(__file__).resolve().parent / "scripts" / "captop.sh"
BM_REBOOT_SCRIPT = Path(__file__).resolve().parent / "scripts" / "oci_node_reboot.py"


def _bm_reboot_selection(args):
    if (len(args) == 3 and args[0] == "bm-hard-reboot" and
            SAFE_GPU_NODE.fullmatch(args[1]) and args[1].upper() != "ALL" and
            re.fullmatch(r"ocid1\.instance\.[A-Za-z0-9_.-]+", args[2])):
        return args[1], args[2]
    return None


def _is_captop_command(args):
    if not args or args[0] != "captop" or len(args) % 2 != 1:
        return False
    patterns = {
        "--capacity-id": r"ocid1\.computecapacitytopology\.[A-Za-z0-9_.-]+",
        "--compartment-id": r"ocid1\.compartment\.[A-Za-z0-9_.-]+",
        "--tenancy-id": r"ocid1\.tenancy\.[A-Za-z0-9_.-]+",
        "--region": r"[a-z][a-z0-9]*(?:-[a-z0-9]+)+",
    }
    seen = set()
    for option, value in zip(args[1::2], args[2::2]):
        if option in seen or option not in patterns or not re.fullmatch(patterns[option], value):
            return False
        seen.add(option)
    return True


SAFE_GPU_NODE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")


def _gpu_health_selection(args: list[str]) -> str | None:
    """The health helper accepts one literal node, never a hostlist or command."""
    if len(args) != 2 or args[0] != "hrun.sh":
        return None
    node = args[1]
    if not SAFE_GPU_NODE.fullmatch(node) or node.upper() == "ALL":
        return None
    return node


def _gpu_health_nodes() -> list[str]:
    """Include equipped nodes in every state, including drained/unreachable."""
    result = subprocess.run(
        ["sinfo", "-N", "-h", "-o", "%N|%G"],
        capture_output=True, check=True, text=True, timeout=15,
    )
    nodes = set()
    for line in result.stdout.splitlines():
        fields = line.strip().split("|")
        if len(fields) != 2:
            continue
        node, gres = fields
        if not SAFE_GPU_NODE.fullmatch(node) or node.upper() == "ALL":
            continue
        if re.search(r"(?:^|,)gpu(?::[^:,()]+)?:[1-9][0-9]*(?:\(|,|$)", gres):
            nodes.add(node)
    return sorted(nodes)


def _stream_fixed_helper(args: list[str], timeout_seconds: int):
    """Bound a streamed helper, including silence and disconnected clients."""
    process = subprocess.Popen(
        _execution_args(args), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        env={**os.environ, "LC_ALL": "C"}, start_new_session=True,
    )
    assert process.stdout is not None
    deadline = time.monotonic() + timeout_seconds
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    pending = ""
    finished = False
    last_heartbeat = time.monotonic()
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(args, timeout_seconds)
                if time.monotonic() - last_heartbeat >= 5:
                    # Keep silent checks observable to the web server so it
                    # can close the generator when a browser disconnects.
                    yield "event: heartbeat\ndata: {}\n\n"
                    last_heartbeat = time.monotonic()
                if not selector.select(timeout=min(remaining, 1)):
                    continue
                chunk = os.read(process.stdout.fileno(), 8192)
                if not chunk:
                    pending += decoder.decode(b"", final=True)
                    if pending:
                        yield f"data: {pending.rstrip()}\n\n"
                    return_code = process.wait(timeout=max(0.01, deadline - time.monotonic()))
                    finished = True
                    yield f"event: complete\ndata: {{\"exit_code\": {return_code}}}\n\n"
                    return
                pending += decoder.decode(chunk)
                while "\n" in pending:
                    line, pending = pending.split("\n", 1)
                    yield f"data: {line.rstrip()}\n\n"
    finally:
        try:
            if not finished:
                # Stop the local helper and its SSH child together.  A browser
                # disconnect must not leave controller-side processes running.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=2)
        finally:
            process.stdout.close()


def _stream_gpu_health(args: list[str]):
    yield from _stream_fixed_helper(args, GPU_HEALTH_TIMEOUT_SECONDS)


def _rdma_selection(args: list[str]) -> tuple[str, str] | None:
    if (len(args) != 3 or args[0] != "rdma_per_link_bw_test.py" or
        args[1] == args[2] or
        any(not SAFE_GPU_NODE.fullmatch(node) or node.upper() == "ALL" for node in args[1:])):
        return None
    return args[1], args[2]


def _serial_selection(args: list[str]) -> str | None:
    """Accept one literal worker hostname for the fixed DMI serial lookup."""
    if len(args) != 2 or args[0] != "node-serial":
        return None
    node = args[1]
    if not SAFE_GPU_NODE.fullmatch(node) or node.upper() == "ALL":
        return None
    return node


def _worker_nodes() -> list[str]:
    """Slurm's per-node inventory contains workers, not the controller."""
    result = subprocess.run(
        ["sinfo", "-N", "-h", "-o", "%N"],
        capture_output=True, check=True, text=True, timeout=15,
    )
    nodes = {line.strip() for line in result.stdout.splitlines()}
    return sorted(node for node in nodes if SAFE_GPU_NODE.fullmatch(node) and node.upper() != "ALL")


def _gpu_test_selection(args):
    """Only the fixed GPU test template is allowed, not arbitrary batch scripts."""
    if len(args) != 10 or args[:7] != GPU_TEST_PREFIX or args[-1] != "gpu-test-30s.sh":
        return None
    if not args[7].startswith("--nodelist=") or not args[8].startswith("--partition="):
        return None
    node, partition = args[7][11:], args[8][12:]
    if any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", value) for value in (node, partition)):
        return None
    if node.upper() == "ALL":
        return None
    return node, partition


def _nccl_selection(args):
    if len(args) != 8 or args[:5] != NCCL_PREFIX or args[-1] != "nccl_run_allreduce.sbatch":
        return None
    if not args[5].startswith("--nodelist=") or not args[6].startswith("--partition="):
        return None
    nodes = args[5].removeprefix("--nodelist=").split(",")
    partition = args[6].removeprefix("--partition=")
    if (len(nodes) != 2 or nodes[0] == nodes[1] or
        any(not SAFE_GPU_NODE.fullmatch(node) or node.upper() == "ALL" for node in nodes) or
        not SAFE_GPU_NODE.fullmatch(partition) or partition.upper() == "ALL"):
        return None
    return nodes, partition


def _gpu_fryer_selection(args):
    if len(args) != 9 or args[:5] != GPU_FRYER_PREFIX or args[-1] != "gpu-fryer-120s.sbatch":
        return None
    if not args[5].startswith("--gres=gpu:") or not args[6].startswith("--nodelist=") or not args[7].startswith("--partition="):
        return None
    count_text = args[5].removeprefix("--gres=gpu:")
    node = args[6].removeprefix("--nodelist=")
    partition = args[7].removeprefix("--partition=")
    if (not count_text.isdigit() or not 1 <= int(count_text) <= 16 or
        not SAFE_GPU_NODE.fullmatch(node) or node.upper() == "ALL" or
        not SAFE_GPU_NODE.fullmatch(partition) or partition.upper() == "ALL"):
        return None
    return node, partition, int(count_text)


def _gpu_fryer_nodes():
    nodes = []
    for item in _idle_gpu_nodes():
        counts = [int(count) for count in re.findall(r"(?:^|,)gpu(?::[^:,()]+)?:([0-9]+)(?:\(|,|$)", item["gres"])]
        count = sum(counts)
        if 1 <= count <= 16:
            nodes.append({**item, "gpu_count": count})
    return nodes


def _idle_nccl_nodes():
    """Keep every eligible partition so a pair can share one partition."""
    result = subprocess.run(["sinfo", "-N", "-h", "-o", "%N|%T|%G|%P|%a"],
                            capture_output=True, check=True, text=True, timeout=15)
    nodes = set()
    for line in result.stdout.splitlines():
        fields = line.strip().split("|")
        if len(fields) != 5:
            continue
        node, state, gres, partition, availability = fields
        partition = partition.rstrip("*")
        if (state.lower() != "idle" or availability.lower() != "up" or
            not SAFE_GPU_NODE.fullmatch(node) or node.upper() == "ALL" or
            not SAFE_GPU_NODE.fullmatch(partition) or
            not re.search(r"(?:^|,)gpu(?::[^:,()]+)?:([0-9]+)(?:\(|,|$)", gres)):
            continue
        gpu_counts = [int(count) for count in re.findall(r"(?:^|,)gpu(?::[^:,()]+)?:([0-9]+)(?:\(|,|$)", gres)]
        if sum(gpu_counts) >= 8:
            nodes.add((node, partition, gres))
    return [{"node": node, "partition": partition, "gres": gres} for node, partition, gres in sorted(nodes)]


def _idle_gpu_nodes():
    result = subprocess.run(["sinfo", "-N", "-h", "-o", "%N|%T|%G|%P|%a"],
                            capture_output=True, check=True, text=True, timeout=15)
    candidates = {}
    for line in result.stdout.splitlines():
        fields = line.strip().split("|")
        if len(fields) != 5:
            continue
        node, state, gres, partition, availability = fields
        default = partition.endswith("*")
        partition = partition.rstrip("*")
        if state.lower() != "idle" or availability.lower() != "up":
            continue
        if not re.search(r"(?:^|,)gpu(?::[^:,()]+)?:[1-9][0-9]*(?:\(|,|$)", gres):
            continue
        if not _gpu_test_selection([*GPU_TEST_PREFIX, f"--nodelist={node}", f"--partition={partition}", "gpu-test-30s.sh"]):
            continue
        key = (not default, partition)
        if node not in candidates or key < candidates[node][0]:
            candidates[node] = (key, {"node": node, "partition": partition, "gres": gres})
    return [candidates[node][1] for node in sorted(candidates)]

# The OCI-HPC ``mgmt`` command can create, terminate, or reconfigure cloud
# resources.  It is therefore never a general shell escape hatch here: only
# these read-only status and inventory commands may be invoked through the UI.
SAFE_MGMT_COMMANDS = {
    ("mgmt", "status"),
    ("mgmt", "status", "--no_color"),
}
SAFE_MGMT_VALUE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:,=/@-]*$")


def _safe_mgmt_value(value: str) -> bool:
    """Accept identifiers and filter expressions, never option-like values."""
    return bool(SAFE_MGMT_VALUE.fullmatch(value)) and not value.startswith("-")


def _safe_node_list(value: str) -> bool:
    return bool(value) and all(_safe_mgmt_value(node) for node in value.split(","))


def _lifecycle_confirmation(args: list[str]) -> str | None:
    """Return the acknowledgement required for an eligible action."""
    reboot = _bm_reboot_selection(args)
    if reboot:
        return "confirm"
    gpu_test = _gpu_test_selection(args)
    if gpu_test:
        return "confirm"
    nccl = _nccl_selection(args)
    if nccl:
        return "confirm"
    gpu_fryer = _gpu_fryer_selection(args)
    if gpu_fryer:
        return "confirm"
    rdma = _rdma_selection(args)
    if rdma:
        return "confirm"
    if args[:4] == ["mgmt", "nodes", "terminate", "--nodes"]:
        return "confirm"
    if args[:4] == ["mgmt", "nodes", "tag-and-terminate", "--nodes"]:
        return "confirm"
    if args[:4] == ["mgmt", "clusters", "add", "node"]:
        return "confirm"
    if len(args) in {4, 5} and args[:2] == ["scontrol", "update"]:
        node_name, node_state = args[2:4]
        if node_state == "State=DRAIN":
            if len(args) != 5 or not args[4].startswith("Reason=") or not args[4][7:].strip():
                return None
        elif len(args) != 4:
            return None
        if node_name.startswith("NodeName=") and node_state in {"State=DRAIN", "State=RESUME"}:
            nodes = node_name.removeprefix("NodeName=")
            if all(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", node)
                   and node.upper() != "ALL" for node in nodes.split(",")):
                return "confirm"
    return None


def _validate_mgmt_options(
    options: list[str],
    *,
    value_options: dict[str, set[str] | None],
    flag_options: set[str] = frozenset(),
    required: set[str] = frozenset(),
) -> bool:
    """Validate a deliberately small set of read-only mgmt CLI options."""
    seen: set[str] = set()
    position = 0
    while position < len(options):
        option = options[position]
        if option in flag_options:
            if option in seen:
                return False
            seen.add(option)
            position += 1
            continue
        allowed_values = value_options.get(option)
        if allowed_values is None and option not in value_options:
            return False
        if option in seen or position + 1 >= len(options):
            return False
        value = options[position + 1]
        if not _safe_mgmt_value(value) or (allowed_values is not None and value not in allowed_values):
            return False
        seen.add(option)
        position += 2
    return required.issubset(seen)


def _is_safe_mgmt_command(args: list[str]) -> bool:
    """Allow only generated, read-only OCI-HPC Explorer commands."""
    if tuple(args) in SAFE_MGMT_COMMANDS:
        return True
    if LIFECYCLE_ACTIONS_ENABLED:
        if args[:4] in (
            ["mgmt", "nodes", "terminate", "--nodes"],
            ["mgmt", "nodes", "tag-and-terminate", "--nodes"],
        ):
            return len(args) == 5 and _safe_node_list(args[4])
        if args[:4] == ["mgmt", "clusters", "add", "node"]:
            if len(args) != 8 or args[4] != "--count" or args[6] != "--cluster":
                return False
            return args[5].isdigit() and 1 <= int(args[5]) <= 1000 and _safe_mgmt_value(args[7])
    command = tuple(args[:3])
    options = args[3:]
    output_formats = {"tabular", "json", "yaml", "csv", "node"}

    if command == ("mgmt", "nodes", "list"):
        return _validate_mgmt_options(
            options,
            value_options={
                "--cluster": None,
                "--memory-cluster": None,
                "--fields": None,
                "--columns": None,
                "--format": {"tabular", "node", "csv", "json"},
                "--style": {"lines", "box", "none"},
            },
            flag_options={"--one-line", "--no-header"},
        )
    if command in {("mgmt", "clusters", "list"), ("mgmt", "login", "list")}:
        return _validate_mgmt_options(options, value_options={"--format": {"tabular", "json"}})
    if command == ("mgmt", "fabrics", "list"):
        return _validate_mgmt_options(
            options,
            value_options={"--rack-state": {"AVAILABLE", "UNAVAILABLE", "OCCUPIED"}},
        )
    if command == ("mgmt", "images", "list"):
        return _validate_mgmt_options(
            options,
            value_options={"--format": {"tabular", "json", "yaml"}, "--compartment": None},
            flag_options={"--used"},
        )
    if command == ("mgmt", "recommendations", "list"):
        return _validate_mgmt_options(
            options,
            value_options={},
            flag_options={"--healthcheck", "--unreachable", "--unconfigured"},
        )
    if command == ("mgmt", "configurations", "list"):
        return _validate_mgmt_options(
            options,
            value_options={
                "--format": {"tabular", "json", "yaml"},
                "--role": {"compute", "login", "all"},
                "--partition": None,
                "--shape": None,
            },
        )
    if tuple(args[:4]) == ("mgmt", "network", "blocks", "list"):
        return _validate_mgmt_options(options, value_options={"--cluster": None}, required={"--cluster"})
    if tuple(args[:4]) == ("mgmt", "network", "rails", "list"):
        return _validate_mgmt_options(
            args[4:], value_options={"--cluster": None}, flag_options={"--nodes"}, required={"--cluster"}
        )
    return False


def validate_command(raw: str) -> tuple[list[str] | None, str | None]:
    """Return safely parsed command arguments or a useful validation error."""
    if not raw or not raw.strip():
        return None, "Enter a Slurm command first."
    if len(raw) > 4096:
        return None, "Command is too long."
    try:
        args = shlex.split(raw, posix=True)
    except ValueError as exc:
        return None, f"Could not parse command: {exc}"
    if not args:
        return None, "Enter a Slurm command first."
    if args[0] == "captop":
        if not _is_captop_command(args):
            return None, "captop accepts only read-only capacity, compartment, tenancy, and region options. Creation and custom file paths are not permitted."
        if not CAPTOP_SCRIPT.is_file() or not CAPTOP_SCRIPT.with_suffix(".py").is_file():
            return None, "The capacity topology report helper is unavailable on this controller."
        return args, None
    if args[0] == "tenancy_ocid.sh":
        if args != ["tenancy_ocid.sh"]:
            return None, "Tenancy lookup uses only /etc/ansible/hosts; extra arguments are not permitted."
        if not TENANCY_SCRIPT.is_file():
            return None, "The tenancy lookup helper is unavailable on this controller."
        return args, None
    if args[0] == "hrun.sh":
        if not _gpu_health_selection(args):
            return None, "GPU health-check requires exactly one node name; hostlists, options, and additional commands are not permitted."
        if not GPU_HEALTH_SCRIPT.is_file() or not os.access(GPU_HEALTH_SCRIPT, os.X_OK):
            return None, "The GPU health-check helper is unavailable on this controller."
        if shutil.which("sinfo") is None:
            return None, "Slurm GPU node inventory is unavailable on this controller."
        return args, None
    if args[0] == "rdma_per_link_bw_test.py":
        if not _rdma_selection(args):
            return None, "RDMA test requires distinct literal server and client node names."
        if not RDMA_SCRIPT.is_file():
            return None, "The RDMA test helper is unavailable on this controller."
        if shutil.which("sinfo") is None or shutil.which("ssh") is None:
            return None, "Slurm inventory or SSH is unavailable on this controller."
        return args, None
    if args[0] == "bm-hard-reboot":
        if not _bm_reboot_selection(args):
            return None, "Hard reboot requires one literal node and its resolved OCI instance OCID."
        if not LIFECYCLE_ACTIONS_ENABLED:
            return None, "Hard reboot is not enabled on this controller."
        if not BM_REBOOT_SCRIPT.is_file():
            return None, "The OCI hard-reboot helper is unavailable."
        return args, None
    if args[0] == "node-serial":
        if not _serial_selection(args):
            return None, "Serial lookup requires exactly one literal worker node name."
        if shutil.which("sinfo") is None or shutil.which("ssh") is None:
            return None, "Slurm inventory or SSH is unavailable on this controller."
        return args, None
    if _gpu_test_selection(args):
        if not LIFECYCLE_ACTIONS_ENABLED:
            return None, "GPU test submission is not enabled on this controller."
        if shutil.which("sbatch") is None:
            return None, "sbatch is unavailable on this controller."
        return args, None
    if _nccl_selection(args):
        if not LIFECYCLE_ACTIONS_ENABLED:
            return None, "NCCL test submission is not enabled on this controller."
        if shutil.which("sbatch") is None or not NCCL_SCRIPT.is_file():
            return None, "The NCCL test script or sbatch is unavailable on this controller."
        return args, None
    if _gpu_fryer_selection(args):
        if not LIFECYCLE_ACTIONS_ENABLED:
            return None, "gpu-fryer submission is not enabled on this controller."
        if shutil.which("sbatch") is None or not GPU_FRYER_SCRIPT.is_file():
            return None, "The gpu-fryer script or sbatch is unavailable on this controller."
        return args, None
    if args[0] == "sbatch":
        return None, "Only the fixed GPU, NCCL, and gpu-fryer test templates can be submitted here."
    if args[0] not in ALLOWED_COMMANDS:
        return None, f"'{args[0]}' is not approved. Allowed: {', '.join(sorted(ALLOWED_COMMANDS))}."
    if args[0] == "mgmt" and not _is_safe_mgmt_command(args):
        return None, "This OCI-HPC management action is not enabled. Only read-only mgmt status and inventory commands are approved."
    if args[:2] == ["scontrol", "update"] and len(args) >= 3 and args[2].startswith("NodeName="):
        if not LIFECYCLE_ACTIONS_ENABLED:
            return None, "Slurm node state changes are not enabled on this controller."
        if _lifecycle_confirmation(args) is None:
            return None, "Only explicit node DRAIN or RESUME updates are enabled here; ALL is not permitted."
    if any(any(marker in token for marker in BLOCKED_TOKENS) for token in args):
        return None, "Shell operators and redirection are not permitted."
    if any(token in {"sudo", "su", "--uid=0"} for token in args):
        return None, "Privilege-changing options are not permitted."
    if shutil.which(args[0]) is None:
        return None, f"'{args[0]}' is not available on this controller."
    return args, None


@app.get("/")
def index():
    return render_template("index.html", allowed_commands=sorted(ALLOWED_COMMANDS | {"hrun.sh", "node-serial", "tenancy_ocid.sh", "captop", "bm-hard-reboot"}), node_state_sudo=NODE_STATE_SUDO_ENABLED)


def _execution_args(args: list[str]) -> list[str]:
    """Map fixed helpers and elevate only a validated node-state update."""
    if _bm_reboot_selection(args):
        return [sys.executable, str(BM_REBOOT_SCRIPT), *args[1:]]
    if _is_captop_command(args):
        return ["/usr/bin/env", f"CAPTOP_PYTHON={sys.executable}", "/bin/bash", str(CAPTOP_SCRIPT), *args[1:]]
    if args == ["tenancy_ocid.sh"]:
        return ["/bin/bash", str(TENANCY_SCRIPT), "/etc/ansible/hosts"]
    if _gpu_health_selection(args):
        return [str(GPU_HEALTH_SCRIPT), args[1]]
    if _rdma_selection(args):
        return ["/usr/bin/env", "python3", str(RDMA_SCRIPT), args[1], args[2]]
    if _serial_selection(args):
        return ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
                "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=5",
                "-o", "ServerAliveCountMax=2", args[1],
                "sudo -n dmidecode -s system-serial-number"]
    if NODE_STATE_SUDO_ENABLED and args[:2] == ["scontrol", "update"] and _lifecycle_confirmation(args):
        return ["/usr/bin/sudo", "-n", "--", NODE_STATE_HELPER, *args[2:]]
    return args


@app.get("/api/health")
def health():
    return jsonify(status="ok", slurm_commands={name: bool(shutil.which(name)) for name in sorted(ALLOWED_COMMANDS)})


@app.get("/api/management/clusters")
def management_clusters():
    """Return controller-known cluster names for read-only builder dropdowns."""
    if "mgmt" not in ALLOWED_COMMANDS or shutil.which("mgmt") is None:
        return jsonify(error="The OCI-HPC management CLI is unavailable."), 503
    try:
        result = subprocess.run(
            ["mgmt", "clusters", "list", "--format", "json"],
            capture_output=True,
            check=True,
            text=True,
            timeout=MAX_TIMEOUT_SECONDS,
            env={**os.environ, "LC_ALL": "C", "NO_COLOR": "1", "COLUMNS": "240"},
        )
        clusters = json.loads(result.stdout)
        if not isinstance(clusters, list) or not all(isinstance(cluster, str) and _safe_mgmt_value(cluster) for cluster in clusters):
            raise ValueError("Management CLI returned an unexpected cluster inventory.")
        return jsonify(clusters=sorted(clusters))
    except (subprocess.SubprocessError, ValueError) as exc:
        app.logger.warning("Could not load OCI-HPC cluster inventory: %s", exc)
        return jsonify(error="Could not load the cluster inventory."), 503


@app.get("/api/slurm/nodes")
def slurm_nodes():
    if "sinfo" not in ALLOWED_COMMANDS or shutil.which("sinfo") is None:
        return jsonify(error="Slurm node inventory is unavailable."), 503
    try:
        result = subprocess.run(
            ["sinfo", "-N", "-h", "-o", "%N"],
            capture_output=True, check=True, text=True, timeout=MAX_TIMEOUT_SECONDS,
        )
        nodes = sorted({name.strip() for name in result.stdout.splitlines() if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name.strip())})
        return jsonify(nodes=nodes)
    except (OSError, subprocess.SubprocessError):
        return jsonify(error="Could not load Slurm node names."), 503


@app.get("/api/slurm/idle-gpu-nodes")
def idle_gpu_nodes():
    try:
        return jsonify(nodes=_idle_gpu_nodes())
    except (OSError, subprocess.SubprocessError):
        return jsonify(error="Could not load idle GPU nodes."), 503


@app.get("/api/troubleshooting/gpu-nodes")
def troubleshooting_gpu_nodes():
    try:
        return jsonify(nodes=_gpu_health_nodes())
    except (OSError, subprocess.SubprocessError):
        return jsonify(error="Could not load GPU node names for troubleshooting."), 503


@app.get("/api/troubleshooting/idle-nccl-nodes")
def troubleshooting_idle_nccl_nodes():
    try:
        return jsonify(nodes=_idle_nccl_nodes())
    except (OSError, subprocess.SubprocessError):
        return jsonify(error="Could not load idle eight-GPU nodes."), 503


@app.get("/api/troubleshooting/idle-gpu-fryer-nodes")
def troubleshooting_idle_gpu_fryer_nodes():
    try:
        return jsonify(nodes=_gpu_fryer_nodes())
    except (OSError, subprocess.SubprocessError):
        return jsonify(error="Could not load idle GPU nodes for gpu-fryer."), 503


@app.get("/api/troubleshooting/idle-rdma-nodes")
def troubleshooting_idle_rdma_nodes():
    try:
        return jsonify(nodes=_idle_gpu_nodes())
    except (OSError, subprocess.SubprocessError):
        return jsonify(error="Could not load idle GPU nodes for RDMA testing."), 503


@app.get("/api/utilities/worker-nodes")
def utilities_worker_nodes():
    try:
        return jsonify(nodes=_worker_nodes())
    except (OSError, subprocess.SubprocessError):
        return jsonify(error="Could not load worker nodes for serial lookup."), 503


def _slurm_job_state(job_id):
    try:
        accounting = subprocess.run(["sacct", "-n", "-P", "-j", str(job_id), "--format=JobIDRaw,State"],
                                    capture_output=True, check=True, text=True, timeout=5)
        for line in accounting.stdout.splitlines():
            fields = line.split("|")
            if len(fields) >= 2 and fields[0] == str(job_id):
                return fields[1].split()[0]
    except (OSError, subprocess.SubprocessError):
        pass
    return None


@app.get("/api/troubleshooting/gpu-fryer-output/<int:job_id>")
def gpu_fryer_output(job_id):
    if job_id <= 0:
        return jsonify(error="Enter a positive Slurm job ID."), 400
    path = GPU_FRYER_LOG_DIR / f"gpu-fryer-{job_id}.out"
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            return jsonify(error="gpu-fryer log must be a regular file."), 400
        with os.fdopen(fd, "r", errors="replace") as log:
            log.seek(0, os.SEEK_END)
            log.seek(max(0, log.tell() - 262144))
            output = log.read()
        log_available = bool(output)
        if not output:
            output = "gpu-fryer log is empty; the job may be starting."
    except FileNotFoundError:
        log_available = False
        output = "No gpu-fryer output yet. The job may be queued or installing dependencies."
    except OSError:
        return jsonify(error="Could not read this gpu-fryer log."), 503
    return jsonify(output=output, log_available=log_available, job_state=_slurm_job_state(job_id),
                   healthy="All GPUs seem healthy" in output)


@app.get("/api/troubleshooting/nccl-output/<int:job_id>")
def nccl_output(job_id):
    if job_id <= 0:
        return jsonify(error="Enter a positive Slurm job ID."), 400
    path = NCCL_LOG_DIR / f"nccl-{job_id}.out"
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            return jsonify(error="NCCL log must be a regular file."), 400
        with os.fdopen(fd, "r", errors="replace") as log:
            log.seek(0, os.SEEK_END)
            log.seek(max(0, log.tell() - 262144))
            output = log.read()
        log_available = bool(output)
        if not output:
            output = "NCCL log is empty; the job may be starting."
    except FileNotFoundError:
        log_available = False
        output = "No NCCL output yet. The job may be queued."
    except OSError:
        return jsonify(error="Could not read this NCCL log."), 503
    bandwidth = re.findall(r"^# Avg bus bandwidth\s*:\s*([0-9]+(?:\.[0-9]+)?)", output, re.M)
    # NCCL versions differ: many finish with the validation/bandwidth summary
    # and do not print the optional "Collective test concluded" marker.
    validated = bool(re.search(r"^# Out of bounds values\s*:\s*0\s+OK\s*$", output, re.M))
    concluded = "# Collective test concluded: all_reduce_perf" in output or bool(bandwidth and validated)
    response = jsonify(output=output, avg_bus_bandwidth_gbps=float(bandwidth[-1]) if bandwidth else None,
                       concluded=concluded, job_state=_slurm_job_state(job_id), log_available=log_available)
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/api/slurm/gpu-test-output/<int:job_id>")
def gpu_test_output(job_id):
    """Only fixed GPU-test logs, never arbitrary files from browser input."""
    if job_id <= 0:
        return jsonify(error="Enter a positive Slurm job ID."), 400
    path = GPU_TEST_LOG_DIR / f"gpu-test-{job_id}.out"
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            return jsonify(error="GPU test log must be a regular file."), 400
        with os.fdopen(fd, "r", errors="replace") as log:
            output = log.read(65536)
        return jsonify(output=output or "The test log is empty; the job may be starting.",
                       log_available=bool(output), job_state=_slurm_job_state(job_id))
    except FileNotFoundError:
        return jsonify(output="No test output yet. The job may be queued.",
                       log_available=False, job_state=_slurm_job_state(job_id))
    except OSError:
        return jsonify(error="Could not read this GPU test log."), 503


def _drained_compute_nodes():
    result = subprocess.run(
        ["sinfo", "-N", "-h", "-o", "%N|%T|%E"],
        capture_output=True, check=True, text=True, timeout=15,
        env={**os.environ, "LC_ALL": "C"},
    )
    nodes = {}
    for line in result.stdout.splitlines():
        fields = line.strip().split("|", 2)
        if len(fields) != 3:
            continue
        node, state, reason = fields
        # Draining nodes can still have running jobs; only fully drained
        # Slurm compute nodes are offered for termination.
        if not SAFE_GPU_NODE.fullmatch(node) or node.upper() == "ALL":
            continue
        if state.lower().rstrip("*~#$@%!") != "drained":
            continue
        nodes[node] = {"node": node, "state": state, "reason": reason}
    return [nodes[node] for node in sorted(nodes)]


@app.get("/api/utilities/bm-reboot-target/<node>")
def bm_reboot_target(node):
    if not SAFE_GPU_NODE.fullmatch(node) or node.upper() == "ALL":
        return jsonify(error="Select one drained compute node."), 400
    if not LIFECYCLE_ACTIONS_ENABLED:
        return jsonify(error="Hard reboot is not enabled on this controller."), 403
    try:
        result = subprocess.run([sys.executable, str(BM_REBOOT_SCRIPT), "--resolve", node],
                                capture_output=True, text=True, timeout=60)
        if result.returncode:
            return jsonify(error=result.stderr.strip() or "Could not resolve bare-metal instance."), 503
        return jsonify(target=json.loads(result.stdout))
    except (OSError, ValueError, subprocess.SubprocessError):
        return jsonify(error="Could not resolve this node to an OCI bare-metal instance."), 503


@app.get("/api/slurm/drained-nodes")
def drained_compute_nodes():
    try:
        return jsonify(nodes=_drained_compute_nodes(),
                       termination_available=LIFECYCLE_ACTIONS_ENABLED and "mgmt" in ALLOWED_COMMANDS and shutil.which("mgmt") is not None)
    except (OSError, subprocess.SubprocessError):
        return jsonify(error="Could not load drained Slurm nodes."), 503


@app.get("/api/management/compute-nodes")
def management_compute_nodes():
    """Return compute-node names only; controller nodes are never lifecycle targets."""
    if "mgmt" not in ALLOWED_COMMANDS or shutil.which("mgmt") is None:
        return jsonify(error="The OCI-HPC management CLI is unavailable."), 503
    try:
        result = subprocess.run(
            ["mgmt", "nodes", "list", "--format", "json", "--columns", "hostname,role"],
            capture_output=True,
            check=True,
            text=True,
            timeout=MAX_TIMEOUT_SECONDS,
            env={**os.environ, "LC_ALL": "C", "NO_COLOR": "1", "COLUMNS": "240"},
        )
        inventory = json.loads(result.stdout)
        if not isinstance(inventory, list):
            raise ValueError("Management CLI returned an unexpected node inventory.")
        nodes = sorted(
            row["hostname"]
            for row in inventory
            if isinstance(row, dict) and row.get("role") == "compute" and isinstance(row.get("hostname"), str) and _safe_mgmt_value(row["hostname"])
        )
        return jsonify(nodes=nodes)
    except (subprocess.SubprocessError, ValueError) as exc:
        app.logger.warning("Could not load OCI-HPC compute-node inventory: %s", exc)
        return jsonify(error="Could not load the compute-node inventory."), 503


@app.post("/api/command")
def command():
    payload = request.get_json(silent=True) or {}
    raw = str(payload.get("command", ""))
    args, error = validate_command(raw)
    if error:
        return jsonify(error=error), 400
    expected_confirmation = _lifecycle_confirmation(args)
    if expected_confirmation and str(payload.get("confirmation", "")) != expected_confirmation:
        return jsonify(error=f"Type exactly: {expected_confirmation}"), 400

    if args[:3] in (["mgmt", "nodes", "terminate"], ["mgmt", "nodes", "tag-and-terminate"]):
        try:
            drained = {item["node"] for item in _drained_compute_nodes()}
            if not set(args[4].split(",")).issubset(drained):
                return jsonify(error="Selected nodes must still be fully drained. Refresh drained nodes before termination."), 409
        except (OSError, subprocess.SubprocessError):
            return jsonify(error="Cannot verify drained nodes; termination was not started."), 503

    gpu_health_node = _gpu_health_selection(args)
    if gpu_health_node:
        try:
            if gpu_health_node not in _gpu_health_nodes():
                return jsonify(error="This node is not in the current Slurm GPU inventory. Refresh the GPU node dropdown."), 409
        except (OSError, subprocess.SubprocessError):
            return jsonify(error="Cannot verify the GPU node inventory; no health-check was started."), 503

    gpu_test = _gpu_test_selection(args)
    if gpu_test:
        try:
            if not any((item["node"], item["partition"]) == gpu_test for item in _idle_gpu_nodes()):
                return jsonify(error="This node is no longer idle in the selected partition. Refresh the idle GPU dropdown."), 409
        except (OSError, subprocess.SubprocessError):
            return jsonify(error="Cannot verify idle GPU availability; no job was submitted."), 503

    nccl = _nccl_selection(args)
    if nccl:
        try:
            eligible = {(item["node"], item["partition"]): item["gres"] for item in _idle_nccl_nodes()}
            if any((node, nccl[1]) not in eligible for node in nccl[0]):
                return jsonify(error="Both nodes must still be idle eight-GPU nodes in the selected partition. Refresh NCCL nodes."), 409
            if eligible[(nccl[0][0], nccl[1])] != eligible[(nccl[0][1], nccl[1])]:
                return jsonify(error="Select nodes with matching GPU resources for the NCCL test."), 409
        except (OSError, subprocess.SubprocessError):
            return jsonify(error="Cannot verify idle NCCL nodes; no job was submitted."), 503

    gpu_fryer = _gpu_fryer_selection(args)
    if gpu_fryer:
        try:
            if not any((item["node"], item["partition"], item["gpu_count"]) == gpu_fryer
                       for item in _gpu_fryer_nodes()):
                return jsonify(error="This node is no longer idle with the selected GPU count. Refresh gpu-fryer nodes."), 409
        except (OSError, subprocess.SubprocessError):
            return jsonify(error="Cannot verify idle gpu-fryer node; no job was submitted."), 503

    rdma = _rdma_selection(args)
    if rdma:
        try:
            available = {item["node"] for item in _idle_gpu_nodes()}
            if not set(rdma) <= available:
                return jsonify(error="Both RDMA nodes must still be idle. Refresh RDMA nodes."), 409
        except (OSError, subprocess.SubprocessError):
            return jsonify(error="Cannot verify idle RDMA nodes; no test was started."), 503

    serial_node = _serial_selection(args)
    if serial_node:
        try:
            if serial_node not in _worker_nodes():
                return jsonify(error="This node is not in the current Slurm worker inventory. Refresh worker nodes."), 409
        except (OSError, subprocess.SubprocessError):
            return jsonify(error="Cannot verify the worker inventory; no serial lookup was started."), 503

    started_at = datetime.now(timezone.utc).isoformat()

    def generate():
        yield f"event: meta\ndata: {{\"started_at\": \"{started_at}\", \"command\": {raw!r}}}\n\n"
        try:
            if _bm_reboot_selection(args):
                yield "data: Verifying drained compute node and OCI bare-metal instance before hard reboot.\n\n"
                yield from _stream_fixed_helper(args, 120)
                return
            if _is_captop_command(args):
                yield "data: Read-only OCI capacity topology report; no resources will be created.\n\n"
                yield from _stream_fixed_helper(args, 120)
                return
            if args == ["tenancy_ocid.sh"]:
                yield "data: Cluster tenancy ID from /etc/ansible/hosts:\n\n"
                yield from _stream_fixed_helper(args, 15)
                return
            if gpu_health_node:
                yield from _stream_gpu_health(args)
                return
            if rdma:
                yield from _stream_fixed_helper(args, RDMA_TIMEOUT_SECONDS)
                return
            if serial_node:
                yield f"data: System serial number for {serial_node}:\n\n"
                yield from _stream_fixed_helper(args, SERIAL_TIMEOUT_SECONDS)
                return
            if gpu_test:
                GPU_TEST_LOG_DIR.mkdir(parents=True, exist_ok=True)
                log_path = str(GPU_TEST_LOG_DIR / "gpu-test-%j.out")
                result = subprocess.run(
                    [*args[:-1], "--parsable", "--job-name=oci-gpu-test-30s", "--export=NONE",
                     "--chdir=/tmp", f"--output={log_path}", f"--error={log_path}"],
                    input=GPU_TEST_SCRIPT.read_text(), capture_output=True, text=True,
                    timeout=20, env={**os.environ, "LC_ALL": "C"},
                )
                if result.returncode:
                    yield f"data: Submission failed: {json.dumps(result.stderr.strip())}\n\n"
                else:
                    job_id = result.stdout.strip().split(";")[0]
                    yield f"data: Submitted GPU test job {job_id} on {gpu_test[0]} (one GPU, 30 seconds of compute).\n\n"
                    if job_id.isdigit():
                        yield f"event: gpu-test\ndata: {{\"job_id\": {int(job_id)}}}\n\n"
                    yield "data: Submission complete; the GPU test may still be queued or running.\n\n"
                    yield f"data: Job output: {log_path.replace('%j', job_id)}\n\n"
                    yield f"data: Check progress: squeue -j {job_id}\n\n"
                    yield f"data: Check result: sacct -j {job_id} --format=JobID,State,Elapsed,ExitCode\n\n"
                yield f"event: complete\ndata: {{\"exit_code\": {result.returncode}}}\n\n"
                return
            if nccl:
                NCCL_LOG_DIR.mkdir(parents=True, exist_ok=True)
                log_path = str(NCCL_LOG_DIR / "nccl-%j.out")
                result = subprocess.run(
                    [*args[:-1], "--parsable", "--job-name=nccl-allreduce-slurm", "--export=ALL",
                     "--chdir=/tmp", f"--output={log_path}", f"--error={log_path}"],
                    input=NCCL_SCRIPT.read_text(), capture_output=True, text=True,
                    timeout=20, env={**os.environ, "LC_ALL": "C"},
                )
                if result.returncode:
                    yield f"data: NCCL submission failed: {json.dumps(result.stderr.strip())}\n\n"
                else:
                    job_id = result.stdout.strip().split(";")[0]
                    yield f"data: Submitted NCCL all-reduce job {job_id} on {', '.join(nccl[0])}.\n\n"
                    if job_id.isdigit():
                        yield f"event: nccl-test\ndata: {{\"job_id\": {int(job_id)}}}\n\n"
                    yield f"data: Job output: {log_path.replace('%j', job_id)}\n\n"
                    yield "data: Output will update here while the job runs. Bandwidth is reported after the test finishes.\n\n"
                yield f"event: complete\ndata: {{\"exit_code\": {result.returncode}}}\n\n"
                return
            if gpu_fryer:
                GPU_FRYER_LOG_DIR.mkdir(parents=True, exist_ok=True)
                log_path = str(GPU_FRYER_LOG_DIR / "gpu-fryer-%j.out")
                result = subprocess.run(
                    [*args[:-1], "--parsable", "--job-name=gpu-fryer-120s", "--export=ALL",
                     "--chdir=/tmp", f"--output={log_path}", f"--error={log_path}"],
                    input=GPU_FRYER_SCRIPT.read_text(), capture_output=True, text=True,
                    timeout=20, env={**os.environ, "LC_ALL": "C"},
                )
                if result.returncode:
                    yield f"data: gpu-fryer submission failed: {json.dumps(result.stderr.strip())}\n\n"
                else:
                    job_id = result.stdout.strip().split(";")[0]
                    yield f"data: Submitted gpu-fryer job {job_id} on {gpu_fryer[0]} ({gpu_fryer[2]} GPUs, 120-second test).\n\n"
                    if job_id.isdigit():
                        yield f"event: gpu-fryer\ndata: {{\"job_id\": {int(job_id)}}}\n\n"
                    yield f"data: Job output: {log_path.replace('%j', job_id)}\n\n"
                    yield "data: Missing software is installed on the node before the stress test starts.\n\n"
                yield f"event: complete\ndata: {{\"exit_code\": {result.returncode}}}\n\n"
                return
            process_env = {**os.environ, "LC_ALL": "C"}
            # The OCI-HPC CLI formats tables from COLUMNS when no interactive
            # terminal is attached.  A wide value preserves full headings and
            # values; the browser terminal provides horizontal scrolling.
            if args[0] == "mgmt":
                process_env["COLUMNS"] = "240"
                process_env["NO_COLOR"] = "1"
            process = subprocess.Popen(
                _execution_args(args),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                env=process_env,
            )
            assert process.stdout is not None
            for line in iter(process.stdout.readline, ""):
                yield f"data: {line.rstrip().replace(chr(10), ' ')}\n\n"
            process.stdout.close()
            return_code = process.wait(timeout=MAX_TIMEOUT_SECONDS)
            yield f"event: complete\ndata: {{\"exit_code\": {return_code}}}\n\n"
        except subprocess.TimeoutExpired:
            if _is_captop_command(args):
                yield "event: error\ndata: captop timed out after 120 seconds; the read-only helper was stopped. Verify OCI metadata connectivity, topology access, and region.\n\n"
            elif args == ["tenancy_ocid.sh"]:
                yield "event: error\ndata: Tenancy lookup timed out after 15 seconds; the helper was stopped.\n\n"
            elif gpu_health_node:
                yield "event: error\ndata: GPU health-check timed out after 120 seconds; the local helper and SSH connection were stopped.\n\n"
            elif rdma:
                yield "event: error\ndata: RDMA test timed out after 600 seconds; the local helper was stopped.\n\n"
            elif serial_node:
                yield "event: error\ndata: Serial lookup timed out after 45 seconds; SSH was stopped.\n\n"
            elif gpu_test:
                yield "event: error\ndata: Submission timed out. A job may have been submitted; check squeue before retrying.\n\n"
            elif nccl or gpu_fryer:
                yield "event: error\ndata: Submission timed out. A job may have been submitted; check squeue before retrying.\n\n"
            else:
                process.kill()
                yield "event: error\ndata: Command timed out and was stopped.\n\n"
        except Exception as exc:  # includes controller-level execution faults
            app.logger.exception("Command execution failed")
            yield f"event: error\ndata: Execution failed: {str(exc)}\n\n"

    return Response(stream_with_context(generate()), mimetype="text/event-stream", headers={"Cache-Control": "no-cache"})


if __name__ == "__main__":
    app.run(host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", "8080")), debug=False)
