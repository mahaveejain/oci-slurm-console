# Slurm Console

A web-based operations console for Slurm and OCI HPC clusters, with streamed output, GPU diagnostics, and optional node lifecycle tools.

[Getting started](#local-run) · [Deployment](#controller-deployment) · [Contributing](CONTRIBUTING.md) · [Security](SECURITY.md)

A small Flask web console for a Slurm controller. It presents a command workspace on the left and streamed command output on the right. The layout is designed for the same kind of operational workflow supported by AWS Engineering Development Hub (EDH), while remaining a standalone controller-side service.

## Security model

This console does **not** execute a shell. It uses `shlex.split()` followed by `subprocess.Popen(args, shell=False)`, accepts only a configured Slurm allowlist, blocks redirection/pipeline operators and privilege-changing tokens, and enforces a timeout. Put it behind your organization’s authenticated reverse proxy and run it using a dedicated, least-privilege Unix account.

For OCI-HPC deployments that have Oracle's `mgmt` CLI installed, the console exposes a separate Node Lifecycle Management and Management Commands catalog. Only a fixed read-only subset is accepted: `mgmt status`, node/cluster/fabric/image/login inventory lists, and recommendation lists. Optional destructive lifecycle actions use separate validated helpers and require explicit configuration; see the lifecycle sections below.

The assistant only proposes reviewable commands; it never executes anything by itself. It is intentionally deterministic by default. Connect it to an approved internal model gateway only after adding authentication, audit logging, and an authorization policy.

## Local run

### Read-only capacity topology utility

Utilities → **Run captop** uses the supplied report/discovery logic via
`scripts/captop.sh` and `scripts/captop.py`. Install the optional dependency
with `.venv/bin/pip install -r requirements-captop.txt`. The console runs it
using its own virtual environment, without sudo. It accepts `captop` and
validated `--capacity-id`, `--compartment-id`, `--tenancy-id`, `--region`
options. Custom config-file paths and `--create` are deliberately unavailable.

Discovery reads `/opt/oci-hpc/conf/queues.conf` and `/etc/ansible/hosts`, then
searches the HPC compartment followed by tenancy. It requires instance-principal
authentication and IAM access to existing capacity topologies. No matching
topology, missing SDK/config, multiple matches, and OCI authorization failures
produce errors rather than a misleading zero-host report. Administrators must
enable/configure capacity topology and policies separately; this utility never
creates it. The streamed report is limited to 120 seconds.

### GPU health-check troubleshooting

Open **Troubleshooting** below **Slurm commands**, select one GPU node, and
choose **Build health-check**, then **Run command**. The console streams
`hostname`, `nvidia-smi`, and the installed
`/opt/oci-hpc/healthchecks/check_gpu_setup.py` results from that node, using
the fixed single-node version of the supplied `scripts/hrun.sh`.

GPU-equipped Slurm nodes in every state appear in the dropdown. Inventory
loads only when Troubleshooting is first opened or **Refresh nodes** is
selected, and the backend verifies the selected node again before execution.
Hostlists and custom SSH commands are not accepted. Building a check clears
the previous command output; it does not run the check.

Install `scripts/hrun.sh` with mode 0755 alongside the app. The console account
needs existing noninteractive SSH access to compute nodes; SSH uses that
account's existing configuration. The controller and nodes need GNU `timeout`;
nodes need `nvidia-smi` and noninteractive permission to run the fixed checker
through `/usr/bin/sudo -n /usr/bin/python3`. No SSH configuration or node sudo
policy is installed by the console. The node command is bounded to 90 seconds,
the SSH connection to 110 seconds, and the backend stream to 120 seconds.
The console reports command completion or errors, without interpreting an
exit status as a guarantee that every GPU health check passed.

### Two-node NCCL all-reduce test

Open **Troubleshooting**, select two distinct idle eight-GPU nodes in the same
available partition with matching GPU resources, then choose **Build NCCL
test** and **Run command**. The console requires a typed confirmation and
rechecks both nodes immediately before submitting the fixed
`scripts/nccl_run_allreduce.sbatch` through `sbatch` stdin. Slurm can still
queue the job if node availability changes after that check.

The script follows the OCI-HPC shape-specific NCCL and RDMA settings in the
provided sample, runs `all_reduce_perf` across 16 ranks, and uses the installed
`/opt/oci-hpc/nccl-test`, Open MPI, and node-ordering helper on the compute
nodes. The job log is written to `SLURM_CONSOLE_NCCL_LOG_DIR` (defaults to
`/config/logs/slurm-console-nccl` on OCI-HPC) as `nccl-<job-id>.out`. Configure that path
on a filesystem writable from the batch node and readable from the controller.
This explicit shared output path avoids a batch-host-only `StdOut` location.

After submission, the Output section polls the log and Slurm accounting,
shows the full available NCCL output, and highlights the reported average bus
bandwidth in GB/s. **View NCCL output** can reload a job by ID. A successful
submission is not a completed test; check the final Slurm state and the
`Collective test concluded` line. NCCL submission requires
`SLURM_CONSOLE_ENABLE_LIFECYCLE_ACTIONS=1`.

### gpu-fryer stress test

Open **Troubleshooting**, select one idle GPU node, and build the fixed
gpu-fryer test. After typed confirmation, Slurm reserves all GPUs on that node
for a 20-minute job. The batch script runs `nvidia-smi`, uses an existing
`gpu-fryer` binary when available, otherwise installs Rust with the official
rustup installer and builds gpu-fryer from the Hugging Face GitHub repository
with Cargo. Installation is user-local and needs outbound HTTPS and Git access
from the compute node. It then runs `gpu-fryer 120`.

The output is written to `SLURM_CONSOLE_GPU_FRYER_LOG_DIR` (default
`/config/logs/slurm-console-gpu-fryer`), which must be shared and writable by
the controller and compute nodes. The Output section follows the log and
Slurm state. A completed job with `All GPUs seem healthy` is shown as a pass;
installation errors, runtime errors, or a nonzero Slurm exit are shown in the
same log. First-time installation can take several minutes. The feature
requires `SLURM_CONSOLE_ENABLE_LIFECYCLE_ACTIONS=1`.

### RDMA per-link bandwidth test

Open **Troubleshooting**, choose distinct idle GPU nodes for **Server** and
**Client**, then build and confirm the fixed RDMA test. The console verifies
both nodes are still idle before it starts. The controller SSHes to both nodes,
matches active `rdmaX` or `ibX` interfaces, and runs `ib_write_bw` for 10 seconds
per link. The Output section streams one row per link with average bandwidth
in Gb/s and finishes with a passed-link count. A failed link makes the command
exit nonzero and shows its error in Output.

The controller account needs existing noninteractive SSH access. New node
host keys are recorded on first use, while changed keys are rejected. Compute
nodes need `ibdev2netdev`, `ib_write_bw`,
`timeout`, and permission for `sudo -n ib_write_bw`. Each server process is
bounded to 30 seconds and stopped by its own PID; the test does not stop other
RDMA tests. The full controller-side run has a 600-second limit. Node selection
is checked at start; other Slurm jobs can claim a node during the test.

### Utilities: BM node serial number

Open **Utilities** below Troubleshooting, select a Slurm worker node, and
choose **Build serial check**. Review the fixed `node-serial <hostname>`
command in the console, then run it. The backend verifies the node is still
present in Slurm's per-node worker inventory and uses noninteractive SSH to
run only `sudo -n dmidecode -s system-serial-number` on that node. The serial
number or SSH/sudo error appears in Output. The lookup has a 45-second limit.
The controller account needs SSH access to workers, and workers need
`dmidecode` and noninteractive sudo permission for this command.

### One-node GPU smoke test

The SLURM COMMAND section includes an idle-GPU-node dropdown and a fixed
30-second CUDA compute test. Only exact `idle` state nodes with GPU GRES in
an `up` partition appear. Availability is rechecked server-side before
submission; Slurm remains responsible for allocation and may queue the job
if another job wins the scheduling race. The operator must confirm submission.

The test requests one node, one GPU, two CPU cores, 2 GiB RAM, and a two-minute walltime
limit (compilation/setup plus 30 seconds of GPU computation). It runs as the
console user, never sudo, and requires CUDA `nvcc`, `g++`, and `timeout` on
the compute node. No PyTorch installation is needed. Slurm transports the
fixed script from `scripts/gpu-test-30s.sh` using sbatch stdin.

Set `SLURM_CONSOLE_GPU_TEST_LOG_DIR` to a writable shared filesystem directory
visible at the same path on the controller and compute nodes. The current
controller uses `/fss/slurm-console-ubuntu/gpu-tests`. The UI reports the job
ID, log path, and accounting commands, and labels success as **submitted**,
not completed. Use **View test output** with the returned job ID to load its
log in the right pane (manual refresh, no timer). A successful log ends with `PASS`; compilation/runtime failures
exit nonzero. GPU test submission requires lifecycle actions enabled, but
does not require adding arbitrary `sbatch` to the general command allowlist.

### Hard reboot of a drained bare-metal node

Node lifecycle → **Destructive lifecycle actions** → **Hard reboot bare-metal node** lists fully drained Slurm nodes.
Select a node and choose **Build hard reboot** to resolve its private IP to
one primary OCI VNIC and instance in the cluster's `targetCompartment`.
The console shows the instance name, OCID, shape, address, and region.
**Run command** requires the exact `HARD REBOOT <node> <instance-OCID>` phrase.

The helper uses the app virtual environment's OCI SDK and controller instance
principal. It requires read access to the cluster's subnets, private IPs,
VNICs, attachments, and instances, plus permission for the instance `RESET`
action. `SLURM_CONSOLE_ENABLE_LIFECYCLE_ACTIONS=1` must be enabled.
Before RESET, it verifies the compute inventory, drain state, absence of
active jobs, unique instance mapping, and `BM.` shape again. It never resumes
the node or retries the power action automatically. Verify recovery before
resuming the node. Building the command performs only read operations.

### Restricted sudo for Slurm node state changes

The node-state builder keeps the displayed `scontrol update` command. With
`SLURM_CONSOLE_NODE_STATE_SUDO=1`, the backend invokes `sudo -n` on a fixed
root-owned helper for validated DRAIN/RESUME updates only. Typed confirmation
is still required. Other commands stay unprivileged; typing arbitrary `sudo`
commands is blocked. Do not grant passwordless access to all of `scontrol` or
run Gunicorn as root.

For the ubuntu deployment, install `deploy/slurm-console-node-state` as
`/usr/local/sbin/slurm-console-node-state` (root:root, mode 0755), validate
`deploy/slurm-console-node-state-sudoers` with `visudo -cf`, and install it as
`/etc/sudoers.d/slurm-console-node-state` (root:root, mode 0440). The helper
uses the root-owned Slurm binary `/usr/local/bin/scontrol`; verify this path
on each controller. Install `deploy/slurm-console-node-state.conf` under
`/etc/systemd/system/slurm-console.service.d/`, reload systemd, and restart
only `slurm-console`. This opt-in disables `NoNewPrivileges` for that service
to permit sudo. Keep access SSH-tunneled or behind authenticated access:
anyone allowed to execute commands in the console can drain/resume nodes.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python app.py
```

Visit `http://127.0.0.1:8080`. A development machine without Slurm will render the UI, but commands report as unavailable. On a controller, Slurm binaries must be in the service account's `PATH`.

## Controller deployment

1. Copy this directory to `/opt/slurm-console` on the Slurm controller and create a dedicated `slurm-console` service account.
2. Create the virtual environment and install `requirements.txt` as that account.
3. Review and install `slurm-console.service.example` as `/etc/systemd/system/slurm-console.service`. Adjust the account, paths, and allowlist for your Slurm distribution. The console does not need write access to Slurm spool or log directories.
4. Place an authenticated reverse proxy (for example, your existing EDH/SSO ingress) in front of `127.0.0.1:8080`; do not expose the Gunicorn listener directly.
5. Run `systemctl daemon-reload && systemctl enable --now slurm-console` and validate `/api/health` through the proxy.

`sbatch` and `scancel` are intentionally in the default list because they are Slurm commands, but many installations should remove one or both until RBAC and auditing match site policy.

## GitHub Actions test deployment

The included workflow, [`.github/workflows/deploy.yml`](.github/workflows/deploy.yml), tests pushes and pull requests. Deployment requires a manual workflow run with `deploy` enabled and the protected `slurm-controller-test` GitHub Environment approves it. The controller runs the application as `ubuntu`; GitHub Actions connects with a separate, least-privilege deployment key.

One-time controller setup:

```bash
mkdir -p /home/ubuntu/slurm-console/releases
cp deploy/slurm-console.service /etc/systemd/system/slurm-console.service
visudo -f /etc/sudoers.d/slurm-console-deploy
systemctl daemon-reload
systemctl enable slurm-console
```

Add the public half of the dedicated CI deployment key to `/home/ubuntu/.ssh/authorized_keys`. In GitHub, create the `slurm-controller-test` Environment, require reviewers, and add these environment secrets:

* `SLURM_DEPLOY_HOST` — controller hostname or public IP
* `SLURM_DEPLOY_USER` — `ubuntu`
* `SLURM_DEPLOY_SSH_KEY` — deployment key's private value
* `SLURM_DEPLOY_KNOWN_HOSTS` — a verified `ssh-keyscan -H <controller>` result, obtained out-of-band

Do not use `StrictHostKeyChecking=no` or store a personal SSH key in CI. The GitHub environment should remain a testing-only target until authentication and TLS are deployed.

## Local console tunnel

From this directory on macOS, use the background SSH manager:

```bash
./slurm-console-tunnel.sh start
./slurm-console-tunnel.sh start 192.0.2.10
./slurm-console-tunnel.sh status
./slurm-console-tunnel.sh stop
```

`start` opens Chrome and returns immediately; closing the terminal does not stop
SSH. Supply any controller IP, hostname, or `user@host` to select a controller.
Bare hosts default to `ubuntu`; use `user@ssh-alias` for another account.
Authentication uses your usual SSH config, keys, and agent; first-time host-key
confirmation or password/passphrase prompts may still be required. Host-key
verification remains enabled.

The most recently selected controller is remembered per local port. Starting
with a different controller switches the managed tunnel on that port. Use
`restart` to reconnect. For multiple controllers simultaneously:

```bash
./slurm-console-tunnel.sh start ubuntu@another-controller 18080
./slurm-console-tunnel.sh status 18080
./slurm-console-tunnel.sh stop 18080
```

Only tunnels created by this manager are stopped. If an earlier manual SSH
command still occupies port 8080, close it with Ctrl+C once, or select another
local port. The old LaunchAgent is unloaded and its matching installed plist is backed up
to the state directory when managing port 8080, so it cannot retry at login.
This new manager does not require it. Tunnels survive terminal closure but do not
restart automatically after a reboot or network failure; run `start` again.
`SLURM_TUNNEL_NO_OPEN=1` suppresses browser opening. State and control sockets
live in `.slurm-console-tunnel/` and are excluded from Git.

### Monitoring

The sixth section opens Grafana in a fresh Chrome Incognito session on the Mac
running your browser. The controller only serves the UI. A Mac loopback helper
receives the selected Grafana URL and credentials, creates a new browser context,
authenticates with Grafana, verifies `/api/user`, and opens the dashboard using
that context's cookies. Grafana server configuration does not need to change.
**Open login page** remains available for direct manual login.

Install and start the helper **on the Mac**, never on the controller:

```bash
.venv/bin/pip install -r requirements-grafana-helper.txt
./scripts/start-grafana-browser-helper.sh
```

Google Chrome must already be installed. No browser download, Chrome extension,
access to your normal Chrome profile, or macOS Accessibility permission is needed.
Keep the helper running while using this integration; Ctrl+C stops a foreground
helper. To manage it in the background for this Mac login session:

```bash
./scripts/manage-grafana-browser-helper.sh start
./scripts/manage-grafana-browser-helper.sh status
./scripts/manage-grafana-browser-helper.sh stop
```

The supplied job plist contains paths for this checkout; regenerate those paths
if moving the project. No plist is installed in `~/Library/LaunchAgents`, so it
does not start automatically at login. Run `start` again after a Mac restart. Each new window has a separate
Incognito context. Close that window to discard its session.

The helper listens on `127.0.0.1:8765`, accepts only the configured local console
origins, requires a per-run token for opening windows, and limits destinations to
explicit Grafana login URLs. Defaults are console origins
`http://127.0.0.1:8080` / `http://localhost:8080` and Grafana
`http://192.0.2.30:3000/login`. To use other ports or a different Grafana URL,
restart the helper with explicit values (repeat flags to allow multiple values):

```bash
./scripts/start-grafana-browser-helper.sh \
  --origin http://127.0.0.1:18080 \
  --grafana-url https://monitoring.example.com/login
```

If Chrome requests local network access for the console, allow it for this local
integration. If the helper is unavailable, the UI reports it and offers the direct
login link. At most four helper sessions can remain open at once.

**Save credentials** retains the username and password in this browser's local
storage without encryption, bound to the exact normalized Grafana login URL,
until **Clear credentials** is used or browser site data is removed. Saving
replaces any previous credential record. A different destination does not inherit
saved credentials. Clear removes the saved record and empties both fields; it
does not log out an existing Grafana session. **Show/Hide** toggles password
visibility. The password is hidden on load, save, clear, and after login attempts.
The helper does not log or persist credentials or cookies. Credentials never pass
through the Slurm controller or enter URLs, shell arguments, or environment
variables. Only save on a trusted Mac: console-origin scripts can read its browser
storage. HTTP sends credentials unencrypted between the Mac and Grafana; use
HTTPS for protected sign-in.

## Development

```bash
python -m unittest discover -s tests -v
for test in tests/test_*_ui.js; do node "$test"; done
gunicorn --check-config app:app
```

Python 3.12 and Node.js 22 are used in CI. Optional integrations require their respective requirements files and cluster tooling. Example addresses use documentation-only IP ranges; replace them with your own endpoints.
