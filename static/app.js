const command = document.querySelector('#command');
const output = document.querySelector('#output');
const state = document.querySelector('#state');
const outputTitle = document.querySelector('#output-title');
const terminalCommand = document.querySelector('#terminal-command');
const lastRun = document.querySelector('#last-run');
const runButton = document.querySelector('#run');
let outputRevision = 0;
const setState = (kind, label) => { state.className = `state ${kind}`; state.textContent = `● ${label}`; };
const confirmationModal = document.querySelector('#confirmation-modal');
const confirmationPhrase = document.querySelector('#confirmation-phrase');
const confirmationInput = document.querySelector('#confirmation-input');
const confirmationError = document.querySelector('#confirmation-error');
let confirmationResolver;

const operationsAccordion = document.querySelector('.operations-accordion');
const workspace = document.querySelector('main');
const outputPane = document.querySelector('.output-pane');
const backToWorkspace = document.querySelector('#back-to-workspace');
const openCommandConsole = document.querySelector('#open-command-console');
const returnToSection = document.querySelector('#return-to-section');
const sectionPanels = document.querySelector('.section-panels');
const workspaceViewTitle = document.querySelector('#workspace-view-title');
let lastSectionToggle;

function resetCommandOutput() {
  const url = new URL(window.location.href);
  if (url.searchParams.has('nccl_job')) {
    url.searchParams.delete('nccl_job');
    window.history.replaceState(null, '', url);
  }
  outputRevision += 1;
  output.textContent = '';
  output.scrollTop = 0;
  outputTitle.textContent = 'Ready when you are';
  terminalCommand.textContent = 'slurm-controller:~';
  lastRun.textContent = 'Not run yet';
  setState('idle', 'Idle');
}
function setCommand(value) {
  command.value = value;
  resetCommandOutput();
}

function setWorkspaceView(view) {
  workspace.dataset.view = view;
  operationsAccordion.querySelectorAll('[data-section-toggle]').forEach(toggle => {
    setOperationSectionExpanded(toggle, view === 'section' && toggle === lastSectionToggle);
  });
  sectionPanels.hidden = view !== 'section';
  outputPane.hidden = view === 'section';
  backToWorkspace.hidden = view !== 'section';
  openCommandConsole.setAttribute('aria-pressed', String(view !== 'section'));
  returnToSection.hidden = view !== 'console' || !lastSectionToggle;
  workspaceViewTitle.textContent = view === 'section' ? lastSectionToggle.querySelector('.section-label').textContent : 'Command console';
  window.scrollTo({top: 0, left: 0});
}
function reviewBuiltCommand() {
  setWorkspaceView('console');
  command.focus();
}
function setOperationSectionExpanded(toggle, expanded) {
  const section = toggle.closest('[data-operation-section]');
  const content = document.getElementById(toggle.getAttribute('aria-controls'));
  toggle.setAttribute('aria-expanded', String(expanded));
  section.classList.toggle('is-expanded', expanded);
  content.hidden = !expanded;
}
operationsAccordion.addEventListener('click', event => {
  const toggle = event.target.closest('[data-section-toggle]');
  if (!toggle || !operationsAccordion.contains(toggle)) return;
  lastSectionToggle = toggle;
  setWorkspaceView('section');
  toggle.focus();
});
backToWorkspace.addEventListener('click', () => {
  setWorkspaceView('overview');
  if (lastSectionToggle) lastSectionToggle.focus();
});
openCommandConsole.addEventListener('click', reviewBuiltCommand);
returnToSection.addEventListener('click', () => {
  setWorkspaceView('section');
  if (lastSectionToggle) lastSectionToggle.focus();
});

function closeLifecycleConfirmation(value = '') {
  confirmationModal.hidden = true;
  confirmationModal.setAttribute('aria-hidden', 'true');
  if (confirmationResolver) confirmationResolver(value);
  confirmationResolver = undefined;
}
function requestLifecycleConfirmation(phrase) {
  document.querySelector('#confirmation-command').textContent = command.value.trim();
  confirmationPhrase.textContent = phrase;
  confirmationInput.value = '';
  confirmationError.hidden = true;
  confirmationModal.hidden = false;
  confirmationModal.setAttribute('aria-hidden', 'false');
  window.setTimeout(() => confirmationInput.focus(), 0);
  return new Promise(resolve => { confirmationResolver = resolve; });
}
document.querySelector('#copy-confirmation').addEventListener('click', async () => {
  const phrase = confirmationPhrase.textContent;
  // Fill the field too, so browser clipboard restrictions cannot block confirmation.
  confirmationInput.value = phrase;
  confirmationInput.focus();
  confirmationInput.select();
  let copied = false;
  try {
    await navigator.clipboard.writeText(phrase);
    copied = true;
  } catch (_) {
    // Older browsers and HTTP pages may not expose the Clipboard API.
    try { copied = document.execCommand('copy'); } catch (_) { /* Filled below. */ }
  }
  confirmationError.hidden = false;
  confirmationError.textContent = copied
    ? 'Copied and filled in. Choose Confirm action to continue.'
    : 'Phrase filled in. Clipboard access is unavailable; choose Confirm action to continue.';
});
document.querySelector('#cancel-confirmation').addEventListener('click', () => closeLifecycleConfirmation());
document.querySelector('#confirm-lifecycle-action').addEventListener('click', () => {
  if (confirmationInput.value === confirmationPhrase.textContent) closeLifecycleConfirmation(confirmationInput.value);
  else { confirmationError.hidden = false; confirmationError.textContent = 'Paste the exact confirmation phrase to continue.'; confirmationInput.focus(); }
});
confirmationInput.addEventListener('keydown', event => { if (event.key === 'Enter') document.querySelector('#confirm-lifecycle-action').click(); });

function lifecycleConfirmationFor(value) {
  const gpuTest = value.match(/^sbatch --nodes=1 --ntasks=1 --cpus-per-task=2 --mem=2G --gres=gpu:1 --time=00:02:00 --nodelist=([A-Za-z0-9_.-]+) --partition=[A-Za-z0-9_.-]+ gpu-test-30s\.sh$/);
  if (gpuTest) return 'confirm';
  const ncclTest = value.match(/^sbatch --nodes=2 --gpus-per-node=8 --ntasks-per-node=8 --exclusive --nodelist=([A-Za-z0-9_.-]+),([A-Za-z0-9_.-]+) --partition=[A-Za-z0-9_.-]+ nccl_run_allreduce\.sbatch$/);
  if (ncclTest) return 'confirm';
  const gpuFryer = value.match(/^sbatch --nodes=1 --ntasks=1 --exclusive --time=00:20:00 --gres=gpu:[0-9]{1,2} --nodelist=([A-Za-z0-9_.-]+) --partition=[A-Za-z0-9_.-]+ gpu-fryer-120s\.sbatch$/);
  if (gpuFryer) return 'confirm';
  const rdmaTest = value.match(/^rdma_per_link_bw_test\.py ([A-Za-z0-9_.-]+) ([A-Za-z0-9_.-]+)$/);
  if (rdmaTest) return 'confirm';
  const reboot = value.match(/^bm-hard-reboot ([A-Za-z0-9_.-]+) (ocid1\.instance\.[A-Za-z0-9_.-]+)$/);
  if (reboot) return 'confirm';
  let match = value.match(/^mgmt nodes terminate --nodes (\S+)$/);
  if (match) return 'confirm';
  match = value.match(/^mgmt nodes tag-and-terminate --nodes (\S+)$/);
  if (match) return 'confirm';
  match = value.match(/^mgmt clusters add node --count (\d+) --cluster (\S+)$/);
  if (match) return 'confirm';
  match = value.match(/^scontrol update NodeName=(\S+) State=(DRAIN|RESUME)(?: Reason=.*)?$/);
  if (match) return 'confirm';
  return '';
}

async function runCommand() {
  if (runButton.disabled || confirmationResolver) return;
  const value = command.value.trim();
  if (!value) return;
  resetCommandOutput();
  const revision = outputRevision;
  const requiredConfirmation = lifecycleConfirmationFor(value);
  let confirmation = '';
  if (requiredConfirmation) {
    confirmation = await requestLifecycleConfirmation(requiredConfirmation);
    if (revision !== outputRevision) return;
    if (confirmation !== requiredConfirmation) {
      output.textContent = `Lifecycle action was not started. Required confirmation: ${requiredConfirmation}`;
      setState('error', 'Confirmation required');
      outputTitle.textContent = 'Lifecycle action was not started';
      return;
    }
  }
  outputTitle.textContent = 'Running command'; terminalCommand.textContent = `slurm-controller:~$ ${value}`;
  setState('running', 'Running'); runButton.disabled = true;
  try {
    const response = await fetch('/api/command', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({command:value, confirmation})});
    if (!response.ok) { const body = await response.json(); throw new Error(body.error || 'Unable to start command.'); }
    const reader = response.body.getReader(), decoder = new TextDecoder(); let buffer = '';
    while (true) {
      const {value: chunk, done} = await reader.read(); if (done) break;
      buffer += decoder.decode(chunk, {stream:true}); const events = buffer.split('\n\n'); buffer = events.pop();
      events.forEach(event => { const type = (event.match(/^event: (.*)$/m)||[])[1] || 'message'; const data = (event.match(/^data: ?(.*)$/m)||[])[1] || '';
        if (revision !== outputRevision) return;
        if (type === 'gpu-test') watchGpuTestOutput(JSON.parse(data).job_id);
        if (type === 'nccl-test') watchNcclOutput(JSON.parse(data).job_id);
        if (type === 'gpu-fryer') watchGpuFryerOutput(JSON.parse(data).job_id);
        // Keep consuming an earlier run, but show output only for the current command.
        if (revision !== outputRevision) return;
        if (type === 'message') output.textContent += `${data}\n`;
        if (type === 'complete') { const code = JSON.parse(data).exit_code; const submittedTest = value.startsWith('sbatch --nodes=1') || value.startsWith('sbatch --nodes=2'); setState(code ? 'error' : 'idle', code ? `Exited ${code}` : (submittedTest ? 'Submitted' : 'Complete')); outputTitle.textContent = code ? 'Command completed with errors' : (submittedTest ? 'Test job submitted' : 'Command completed'); }
        if (type === 'error') { output.textContent += `\nERROR: ${data}\n`; setState('error','Error'); outputTitle.textContent = 'Command could not complete'; }
      });
      if (revision === outputRevision) output.scrollTop = output.scrollHeight;
    }
    if (revision === outputRevision) lastRun.textContent = `Last run ${new Date().toLocaleTimeString()}`;
  } catch (error) {
    if (revision === outputRevision) { output.textContent = `ERROR: ${error.message}`; setState('error', 'Blocked'); outputTitle.textContent = 'Command was not started'; }
  } finally { runButton.disabled = false; }
}
runButton.addEventListener('click', runCommand);
document.querySelector('#clear').addEventListener('click', resetCommandOutput);
document.querySelectorAll('[data-command]').forEach(button => button.addEventListener('click', () => { setCommand(button.dataset.command); reviewBuiltCommand(); }));
command.addEventListener('input', resetCommandOutput);
command.addEventListener('keydown', e => {
  if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') { e.preventDefault(); runCommand(); }
});
const managementMessage = document.querySelector('#management-builder-message');
const managementValuePattern = /^[A-Za-z0-9][A-Za-z0-9_.:,=/@-]*$/;
function managementInput(id, label, required = false) {
  const value = document.querySelector(`#${id}`).value.trim();
  if (!value && !required) return '';
  if (!value) throw new Error(`Enter a ${label}.`);
  if (!managementValuePattern.test(value)) throw new Error(`${label} can contain letters, numbers, dots, dashes, commas, colons, equals signs, slashes, and @ only.`);
  return value;
}
async function loadManagementClusters() {
  const selects = [...document.querySelectorAll('.cluster-select')];
  try {
    const response = await fetch('/api/management/clusters');
    const body = await response.json();
    if (!response.ok || !Array.isArray(body.clusters)) throw new Error(body.error || 'Cluster inventory is unavailable.');
    selects.forEach(select => {
      const optional = select.id === 'mgmt-node-cluster';
      select.replaceChildren();
      const placeholder = new Option(optional ? 'All clusters' : 'Select cluster', '');
      select.add(placeholder);
      body.clusters.forEach(cluster => select.add(new Option(cluster, cluster)));
      select.disabled = false;
    });
    if (!body.clusters.length) managementMessage.textContent = 'No clusters were returned by the OCI-HPC management inventory.';
  } catch (error) {
    selects.forEach(select => {
      select.replaceChildren(new Option('Cluster inventory unavailable', ''));
      select.disabled = true;
    });
    managementMessage.textContent = 'Cluster dropdown unavailable. Refresh after the OCI-HPC management CLI is available.';
  }
}
function renderDrainedNodeRows(container, nodes, group) {
  container.replaceChildren();
  nodes.forEach(item => {
    const row = document.createElement('label');
    row.className = 'lifecycle-node-row';
    const radio = document.createElement('input');
    radio.type = 'radio'; radio.name = group; radio.value = item.node;
    const name = document.createElement('span');
    name.className = 'lifecycle-node-name'; name.textContent = item.node;
    const reason = document.createElement('span');
    reason.className = 'lifecycle-node-reason'; reason.textContent = `${item.state} · ${item.reason}`;
    row.append(radio, name, reason);
    container.append(row);
  });
  if (!nodes.length) container.textContent = 'No fully drained nodes.';
}
async function loadLifecycleNodes() {
  const select = document.querySelector('#lifecycle-target-nodes');
  const message = document.querySelector('#lifecycle-builder-message');
  try {
    const response = await fetch('/api/slurm/drained-nodes');
    const body = await response.json();
    if (!response.ok || !Array.isArray(body.nodes)) throw new Error(body.error || 'Drained-node inventory is unavailable.');
    renderDrainedNodeRows(select, body.nodes, 'terminate-node');
    message.textContent = !body.nodes.length ? 'No fully drained compute nodes are available. Refresh after draining a node.' :
      !body.termination_available ? 'Drained nodes are listed, but termination requires the OCI-HPC mgmt CLI and enabled lifecycle actions on this controller.' :
      'Select a drained node, build the command, then review and confirm termination. Drain status is rechecked before execution.';
  } catch (error) {
    select.textContent = 'Drained-node inventory unavailable';
    message.textContent = error.message;
  }
}
function setBuiltManagementCommand(parts) {
  setCommand(parts.join(' '));
  managementMessage.textContent = 'Command built. Review it in the console, then choose Run command.';
  reviewBuiltCommand();
}
function buildManagementCommand(kind) {
  try {
    if (kind === 'nodes') {
      const parts = ['mgmt', 'nodes', 'list'];
      const cluster = managementInput('mgmt-node-cluster', 'cluster name');
      const fields = managementInput('mgmt-node-fields', 'node filter');
      if (cluster) parts.push('--cluster', cluster);
      if (fields) parts.push('--fields', fields);
      parts.push('--format', document.querySelector('#mgmt-node-format').value);
      setBuiltManagementCommand(parts);
    }
    if (kind === 'fabrics') {
      const stateValue = document.querySelector('#mgmt-fabric-state').value;
      setBuiltManagementCommand(['mgmt', 'fabrics', 'list', ...(stateValue ? ['--rack-state', stateValue] : [])]);
    }
    if (kind === 'images') {
      const parts = ['mgmt', 'images', 'list', '--format', document.querySelector('#mgmt-image-format').value];
      if (document.querySelector('#mgmt-image-used').checked) parts.push('--used');
      setBuiltManagementCommand(parts);
    }
    if (kind === 'inventory') {
      setBuiltManagementCommand(['mgmt', document.querySelector('#mgmt-inventory-kind').value, 'list', '--format', document.querySelector('#mgmt-inventory-format').value]);
    }
    if (kind === 'network') {
      const cluster = managementInput('mgmt-network-cluster', 'cluster name', true);
      const topology = document.querySelector('#mgmt-network-kind').value;
      const parts = ['mgmt', 'network', topology, 'list', '--cluster', cluster];
      if (topology === 'rails' && document.querySelector('#mgmt-network-nodes').checked) parts.push('--nodes');
      setBuiltManagementCommand(parts);
    }
    if (kind === 'recommendations') {
      const filter = document.querySelector('#mgmt-recommendations-filter').value;
      setBuiltManagementCommand(['mgmt', 'recommendations', 'list', ...(filter ? [filter] : [])]);
    }
    if (kind === 'configurations') {
      const parts = ['mgmt', 'configurations', 'list', '--role', document.querySelector('#mgmt-config-role').value, '--format', document.querySelector('#mgmt-config-format').value];
      const shape = managementInput('mgmt-config-shape', 'shape');
      if (shape) parts.push('--shape', shape);
      setBuiltManagementCommand(parts);
    }
  } catch (error) {
    managementMessage.textContent = error.message;
  }
}
document.querySelectorAll('[data-mgmt-build]').forEach(button => button.addEventListener('click', () => buildManagementCommand(button.dataset.mgmtBuild)));
loadManagementClusters();
async function loadSlurmNodes() {
  const select = document.querySelector('#slurm-node-state-target');
  try {
    const response = await fetch('/api/slurm/nodes');
    const body = await response.json();
    if (!response.ok) throw new Error(body.error);
    select.replaceChildren(new Option('Select Slurm node', ''));
    body.nodes.forEach(node => select.add(new Option(node, node)));
    select.disabled = false;
  } catch (error) {
    select.replaceChildren(new Option('Node inventory unavailable', ''));
    document.querySelector('#slurm-node-state-message').textContent = error.message;
  }
}
loadSlurmNodes();
document.querySelector('#slurm-node-state-action').addEventListener('change', event => {
  const draining = event.target.value === 'DRAIN';
  document.querySelector('#slurm-drain-reason-field').hidden = !draining;
  document.querySelector('#slurm-drain-reason').required = draining;
});
document.querySelector('[data-slurm-node-state-build]').addEventListener('click', () => {
  const target = document.querySelector('#slurm-node-state-target').value.trim();
  const stateAction = document.querySelector('#slurm-node-state-action').value;
  const message = document.querySelector('#slurm-node-state-message');
  if (!target || !managementValuePattern.test(target) || target.toUpperCase() === 'ALL') {
    message.textContent = 'Enter one explicit node name or a comma-separated list; ALL is not permitted.';
    return;
  }
  let builtCommand = `scontrol update NodeName=${target} State=${stateAction}`;
  if (stateAction === 'DRAIN') {
    const reason = document.querySelector('#slurm-drain-reason').value.trim();
    if (!reason || !/^[A-Za-z0-9 .,:_/@()-]{1,200}$/.test(reason)) {
      message.textContent = 'Enter a reason using letters, numbers, spaces, or basic punctuation.';
      return;
    }
    builtCommand += ` Reason="${reason}"`;
  }
  setCommand(builtCommand);
  message.textContent = 'State-change command built. Review it in the console; Run command will require a typed confirmation.';
  reviewBuiltCommand();
});
function buildLifecycleCommand(kind) {
  const message = document.querySelector('#lifecycle-builder-message');
  try {
    if (kind === 'add') {
      const cluster = managementInput('lifecycle-add-cluster', 'target cluster', true);
      const count = document.querySelector('#lifecycle-add-count').value;
      if (!/^(?:[1-9]\d{0,2}|1000)$/.test(count)) throw new Error('Enter a node count from 1 to 1000.');
      setCommand(`mgmt clusters add node --count ${count} --cluster ${cluster}`);
    } else {
      const nodes = [...document.querySelectorAll('#lifecycle-target-nodes input:checked')].map(option => option.value);
      if (!nodes.length || !nodes[0]) throw new Error('Select a drained compute node.');
      setCommand(`mgmt nodes ${kind === 'tag-terminate' ? 'tag-and-terminate' : 'terminate'} --nodes ${nodes.join(',')}`);
    }
    message.textContent = 'Destructive command built. Review it in the console; Run command will require a typed confirmation.';
    reviewBuiltCommand();
  } catch (error) {
    message.textContent = error.message;
  }
}
document.querySelectorAll('[data-lifecycle-build]').forEach(button => button.addEventListener('click', () => buildLifecycleCommand(button.dataset.lifecycleBuild)));
loadLifecycleNodes();
document.querySelector('#refresh-drained-nodes').addEventListener('click', loadLifecycleNodes);

const slurmTemplate = document.querySelector('#slurm-template');
const slurmValue = document.querySelector('#slurm-template-value');
const builderMessage = document.querySelector('#builder-message');
function updateSlurmBuilder() {
  const placeholder = slurmTemplate.selectedOptions[0].dataset.placeholder;
  slurmValue.hidden = !placeholder;
  slurmValue.required = Boolean(placeholder);
  slurmValue.placeholder = placeholder || '';
  if (!placeholder) slurmValue.value = '';
}
slurmTemplate.addEventListener('change', updateSlurmBuilder);
document.querySelector('#build-slurm').addEventListener('click', () => {
  const template = slurmTemplate.value;
  const value = slurmValue.value.trim();
  if (template.includes('{value}') && !value) {
    builderMessage.textContent = `Enter a ${slurmValue.placeholder.toLowerCase()} first.`;
    slurmValue.focus();
    return;
  }
  if (/[\s;|&<>`$]/.test(value)) {
    builderMessage.textContent = 'Use a single username or job ID without spaces or shell characters.';
    slurmValue.focus();
    return;
  }
  setCommand(template.replace('{value}', value));
  builderMessage.textContent = 'Command built. Review it in the console, then choose Run command.';
  reviewBuiltCommand();
});
updateSlurmBuilder();

async function loadIdleGpuNodes() {
  const select = document.querySelector('#gpu-test-node');
  const build = document.querySelector('#build-gpu-test');
  const refresh = document.querySelector('#refresh-gpu-nodes');
  const message = document.querySelector('#gpu-test-message');
  select.disabled = true; build.disabled = true; refresh.disabled = true;
  try {
    const response = await fetch('/api/slurm/idle-gpu-nodes');
    const body = await response.json();
    if (!response.ok || !Array.isArray(body.nodes)) throw new Error(body.error || 'Idle GPU inventory is unavailable.');
    select.replaceChildren(new Option(body.nodes.length ? 'Select idle GPU node' : 'No idle GPU nodes', ''));
    body.nodes.forEach(item => select.add(new Option(`${item.node} · ${item.partition} · ${item.gres}`, `${item.node}|${item.partition}`)));
    select.disabled = !body.nodes.length;
    message.textContent = body.nodes.length ? 'Choose a node, build the test, then review and Run command. Availability is rechecked before submission; Slurm may queue the job if availability changes.' : 'No idle GPU nodes are available. Running, mixed, drained, and unavailable nodes are excluded. Refresh nodes later.';
  } catch (error) {
    select.replaceChildren(new Option('Idle GPU inventory unavailable', ''));
    message.textContent = error.message;
  } finally { refresh.disabled = false; }
}
document.querySelector('#gpu-test-node').addEventListener('change', event => {
  document.querySelector('#build-gpu-test').disabled = !event.target.value;
});
document.querySelector('#refresh-gpu-nodes').addEventListener('click', loadIdleGpuNodes);
document.querySelector('#build-gpu-test').addEventListener('click', () => {
  const [node, partition] = document.querySelector('#gpu-test-node').value.split('|');
  if (!node || !partition) return;
  setCommand(`sbatch --nodes=1 --ntasks=1 --cpus-per-task=2 --mem=2G --gres=gpu:1 --time=00:02:00 --nodelist=${node} --partition=${partition} gpu-test-30s.sh`);
  document.querySelector('#gpu-test-message').textContent = 'GPU test built. Choose Run command and confirm submission. Output will include the job ID and shared log location; submission is not job completion.';
  reviewBuiltCommand();
});
loadIdleGpuNodes();

const rdmaServer = document.querySelector('#rdma-server-node');
const rdmaClient = document.querySelector('#rdma-client-node');
const rdmaBuild = document.querySelector('#build-rdma-test');
const rdmaMessage = document.querySelector('#rdma-message');
let rdmaInventoryRequested = false;
let rdmaNodes = new Set();
function updateRdmaSelection() {
  rdmaBuild.disabled = !rdmaNodes.has(rdmaServer.value) || !rdmaNodes.has(rdmaClient.value) || rdmaServer.value === rdmaClient.value;
  if (rdmaServer.value && rdmaClient.value && rdmaBuild.disabled) rdmaMessage.textContent = 'Choose two different idle GPU nodes.';
}
async function loadRdmaNodes() {
  rdmaInventoryRequested = true;
  rdmaNodes = new Set();
  rdmaBuild.disabled = true;
  for (const select of [rdmaServer, rdmaClient]) { select.disabled = true; select.replaceChildren(new Option('Loading idle nodes…', '')); }
  const refresh = document.querySelector('#refresh-rdma-nodes');
  refresh.disabled = true;
  try {
    const response = await fetch('/api/troubleshooting/idle-rdma-nodes');
    const body = await response.json();
    if (!response.ok) throw new Error(body.error || 'Could not load RDMA nodes.');
    rdmaNodes = new Set(body.nodes.map(item => item.node).filter(node => /^[A-Za-z0-9][A-Za-z0-9_.-]*$/.test(node)));
    for (const [select, label] of [[rdmaServer, 'server'], [rdmaClient, 'client']]) {
      select.replaceChildren(new Option(rdmaNodes.size ? `Select idle ${label} node` : 'No idle GPU nodes', ''));
      rdmaNodes.forEach(node => select.add(new Option(node, node)));
      select.disabled = !rdmaNodes.size;
    }
    rdmaMessage.textContent = rdmaNodes.size < 2 ? 'At least two idle GPU nodes are required.' : 'Choose a server and a different client. The test checks each common RDMA link for 10 seconds.';
  } catch (error) {
    for (const select of [rdmaServer, rdmaClient]) select.replaceChildren(new Option('RDMA inventory unavailable', ''));
    rdmaMessage.textContent = error.message;
  } finally { refresh.disabled = false; }
}
document.addEventListener('click', event => {
  if (event.target.closest('#troubleshooting-title') && !rdmaInventoryRequested) loadRdmaNodes();
});
rdmaServer.addEventListener('change', updateRdmaSelection);
rdmaClient.addEventListener('change', updateRdmaSelection);
document.querySelector('#refresh-rdma-nodes').addEventListener('click', loadRdmaNodes);
rdmaBuild.addEventListener('click', () => {
  updateRdmaSelection();
  if (rdmaBuild.disabled) return;
  setCommand(`rdma_per_link_bw_test.py ${rdmaServer.value} ${rdmaClient.value}`);
  rdmaMessage.textContent = 'RDMA test built. Review and confirm it; Output will stream per-link bandwidth in Gb/s.';
  reviewBuiltCommand();
});

const rebootNode = document.querySelector('#bm-reboot-node');
const rebootBuild = document.querySelector('#build-bm-reboot');
const rebootRefresh = document.querySelector('#refresh-bm-reboot-nodes');
const rebootMessage = document.querySelector('#bm-reboot-message');
function selectedRebootNode() { return rebootNode.querySelector('input:checked')?.value || ''; }
async function loadRebootNodes() {
  rebootNode.textContent = 'Loading drained nodes…';
  rebootBuild.disabled = true;
  rebootRefresh.disabled = true;
  try {
    const response = await fetch('/api/slurm/drained-nodes');
    const body = await response.json();
    if (!response.ok || !Array.isArray(body.nodes)) throw new Error(body.error || 'Drained nodes unavailable.');
    renderDrainedNodeRows(rebootNode, body.nodes, 'reboot-node');
    rebootMessage.textContent = 'Choose a drained node. Build hard reboot will verify its OCI bare-metal instance before you confirm.';
  } catch (error) {
    rebootNode.textContent = 'Drained inventory unavailable';
    rebootMessage.textContent = error.message;
  } finally { rebootRefresh.disabled = false; }
}
rebootNode.addEventListener('change', () => { rebootBuild.disabled = !selectedRebootNode(); });
rebootRefresh.addEventListener('click', loadRebootNodes);
rebootBuild.addEventListener('click', async () => {
  const node = selectedRebootNode();
  if (!node) return;
  rebootBuild.disabled = true;
  rebootMessage.textContent = `Resolving OCI bare-metal instance for ${node}…`;
  try {
    const response = await fetch(`/api/utilities/bm-reboot-target/${encodeURIComponent(node)}`);
    const body = await response.json();
    if (!response.ok) throw new Error(body.error || 'Could not resolve bare-metal instance.');
    if (selectedRebootNode() !== node) return;
    const target = body.target;
    setCommand(`bm-hard-reboot ${node} ${target.instance_id}`);
    rebootMessage.textContent = `${target.display_name} · ${target.shape} · ${target.private_ip} · ${target.region}. Instance: ${target.instance_id}. Review the command and choose Run command to confirm hard reboot.`;
    reviewBuiltCommand();
  } catch (error) { rebootMessage.textContent = error.message; }
  finally { rebootBuild.disabled = !selectedRebootNode(); }
});
loadRebootNodes();

const nodeSerialNode = document.querySelector('#node-serial-node');
const nodeSerialBuild = document.querySelector('#build-node-serial');
const nodeSerialRefresh = document.querySelector('#refresh-node-serial-nodes');
document.querySelector('#run-captop').addEventListener('click', () => {
  if (document.querySelector('#run').disabled) return;
  const capacityId = document.querySelector('#captop-capacity-id').value.trim();
  const region = document.querySelector('#captop-region').value.trim();
  const message = document.querySelector('#captop-message');
  if (capacityId && !/^ocid1\.computecapacitytopology\.[A-Za-z0-9_.-]+$/.test(capacityId)) {
    message.textContent = 'Enter a valid capacity topology OCID, or leave it blank for discovery.'; return;
  }
  if (region && !/^[a-z][a-z0-9]*(?:-[a-z0-9]+)+$/.test(region)) {
    message.textContent = 'Enter a region such as eu-frankfurt-1, or leave it blank.'; return;
  }
  const parts = ['captop'];
  if (capacityId) parts.push('--capacity-id', capacityId);
  if (region) parts.push('--region', region);
  message.textContent = 'Running read-only captop. Missing topology configuration or IAM access will be reported in Output.';
  setCommand(parts.join(' '));
  reviewBuiltCommand();
  runCommand();
});
document.querySelector('#show-tenancy-id').addEventListener('click', () => {
  if (document.querySelector('#run').disabled) return;
  setCommand('tenancy_ocid.sh');
  reviewBuiltCommand();
  runCommand();
});
const nodeSerialMessage = document.querySelector('#node-serial-message');
let nodeSerialInventoryRequested = false;
let nodeSerialNodes = new Set();
async function loadNodeSerialNodes() {
  nodeSerialInventoryRequested = true;
  nodeSerialNodes = new Set();
  nodeSerialNode.disabled = true;
  nodeSerialBuild.disabled = true;
  nodeSerialRefresh.disabled = true;
  nodeSerialNode.replaceChildren(new Option('Loading worker nodes…', ''));
  nodeSerialMessage.textContent = 'Loading worker nodes…';
  try {
    const response = await fetch('/api/utilities/worker-nodes');
    const body = await response.json();
    if (!response.ok || !Array.isArray(body.nodes)) throw new Error(body.error || 'Worker node inventory is unavailable.');
    nodeSerialNodes = new Set(body.nodes.filter(node => typeof node === 'string' && /^[A-Za-z0-9][A-Za-z0-9_.-]*$/.test(node)));
    nodeSerialNode.replaceChildren(new Option(nodeSerialNodes.size ? 'Select worker node' : 'No worker nodes found', ''));
    [...nodeSerialNodes].sort((a, b) => a.localeCompare(b, undefined, {numeric: true})).forEach(node => nodeSerialNode.add(new Option(node, node)));
    nodeSerialNode.disabled = !nodeSerialNodes.size;
    nodeSerialMessage.textContent = nodeSerialNodes.size
      ? 'Choose a worker node, build the check, then review and Run command to see its serial number.'
      : 'No worker nodes were found. Refresh nodes to check again.';
  } catch (error) {
    nodeSerialNode.replaceChildren(new Option('Worker node inventory unavailable', ''));
    nodeSerialMessage.textContent = `${error.message} Refresh nodes to try again.`;
  } finally {
    nodeSerialRefresh.disabled = false;
  }
}
operationsAccordion.addEventListener('click', event => {
  if (event.target.closest('#utilities-title') && !nodeSerialInventoryRequested) loadNodeSerialNodes();
});
nodeSerialNode.addEventListener('change', () => {
  nodeSerialBuild.disabled = !nodeSerialNodes.has(nodeSerialNode.value);
});
nodeSerialRefresh.addEventListener('click', loadNodeSerialNodes);
nodeSerialBuild.addEventListener('click', () => {
  const node = nodeSerialNode.value;
  if (!nodeSerialNodes.has(node)) {
    nodeSerialBuild.disabled = true;
    nodeSerialMessage.textContent = 'Select a worker node from the list, or refresh nodes.';
    return;
  }
  setCommand(`node-serial ${node}`);
  nodeSerialMessage.textContent = `Serial check built for ${node}. Review it in the console, then choose Run command.`;
  reviewBuiltCommand();
});

const gpuHealthNode = document.querySelector('#gpu-health-node');
const gpuHealthBuild = document.querySelector('#build-gpu-health-check');
const gpuHealthRefresh = document.querySelector('#refresh-gpu-health-nodes');
const gpuHealthMessage = document.querySelector('#gpu-health-message');
let gpuHealthInventoryRequested = false;
let gpuHealthNodes = new Set();
async function loadGpuHealthNodes() {
  gpuHealthInventoryRequested = true;
  gpuHealthNodes = new Set();
  gpuHealthNode.replaceChildren(new Option('Loading GPU nodes…', ''));
  gpuHealthNode.disabled = true;
  gpuHealthBuild.disabled = true;
  gpuHealthRefresh.disabled = true;
  gpuHealthMessage.textContent = 'Loading GPU nodes…';
  try {
    const response = await fetch('/api/troubleshooting/gpu-nodes');
    const body = await response.json();
    if (!response.ok || !Array.isArray(body.nodes)) throw new Error(body.error || 'GPU node inventory is unavailable.');
    if (body.nodes.some(node => typeof node !== 'string' || !/^[A-Za-z0-9_.-]+$/.test(node))) throw new Error('GPU node inventory is unavailable.');
    gpuHealthNodes = new Set(body.nodes);
    gpuHealthNode.replaceChildren(new Option(gpuHealthNodes.size ? 'Select GPU node' : 'No GPU nodes found', ''));
    gpuHealthNodes.forEach(node => gpuHealthNode.add(new Option(node, node)));
    gpuHealthNode.disabled = !gpuHealthNodes.size;
    gpuHealthMessage.textContent = gpuHealthNodes.size
      ? 'Choose a GPU node, build the check, then review and Run command. GPU nodes in every Slurm state are listed.'
      : 'No GPU-equipped Slurm nodes were found. Refresh nodes to check again.';
  } catch (error) {
    gpuHealthNode.replaceChildren(new Option('GPU node inventory unavailable', ''));
    gpuHealthMessage.textContent = `${error.message} Refresh nodes to try again.`;
  } finally {
    gpuHealthRefresh.disabled = false;
  }
}
operationsAccordion.addEventListener('click', event => {
  if (event.target.closest('#troubleshooting-title') && !gpuHealthInventoryRequested) loadGpuHealthNodes();
});
gpuHealthNode.addEventListener('change', () => {
  gpuHealthBuild.disabled = !gpuHealthNodes.has(gpuHealthNode.value);
});
gpuHealthRefresh.addEventListener('click', loadGpuHealthNodes);
gpuHealthBuild.addEventListener('click', () => {
  const node = gpuHealthNode.value;
  if (!gpuHealthNodes.has(node)) {
    gpuHealthBuild.disabled = true;
    gpuHealthMessage.textContent = 'Select an available GPU node from the list, or refresh nodes.';
    return;
  }
  setCommand(`hrun.sh ${node}`);
  gpuHealthMessage.textContent = `Health-check built for ${node}. Review it in the console, then choose Run command to view the results.`;
  reviewBuiltCommand();
});

const ncclFirst = document.querySelector('#nccl-node-one');
const ncclSecond = document.querySelector('#nccl-node-two');
const ncclBuild = document.querySelector('#build-nccl-test');
const ncclMessage = document.querySelector('#nccl-message');
let ncclInventoryRequested = false;
let ncclNodes = [];
let ncclWatchRevision = 0;
function ncclSelection() {
  const first = ncclNodes.find(item => `${item.node}|${item.partition}` === ncclFirst.value);
  const second = ncclNodes.find(item => `${item.node}|${item.partition}` === ncclSecond.value);
  ncclBuild.disabled = !first || !second || first.node === second.node || first.partition !== second.partition || first.gres !== second.gres;
  if (first && second && ncclBuild.disabled) ncclMessage.textContent = 'Choose two different nodes in the same partition with matching GPU resources.';
}
async function loadNcclNodes() {
  ncclInventoryRequested = true;
  ncclNodes = [];
  ncclBuild.disabled = true;
  for (const select of [ncclFirst, ncclSecond]) { select.disabled = true; select.replaceChildren(new Option('Loading idle nodes…', '')); }
  const refresh = document.querySelector('#refresh-nccl-nodes');
  refresh.disabled = true;
  try {
    const response = await fetch('/api/troubleshooting/idle-nccl-nodes');
    const body = await response.json();
    if (!response.ok || !Array.isArray(body.nodes)) throw new Error(body.error || 'NCCL node inventory unavailable.');
    ncclNodes = body.nodes.filter(item => /^[A-Za-z0-9_.-]+$/.test(item.node) && /^[A-Za-z0-9_.-]+$/.test(item.partition));
    for (const select of [ncclFirst, ncclSecond]) {
      select.replaceChildren(new Option(ncclNodes.length ? 'Select idle eight-GPU node' : 'No idle eight-GPU nodes', ''));
      ncclNodes.forEach(item => select.add(new Option(`${item.node} · ${item.partition} · ${item.gres}`, `${item.node}|${item.partition}`)));
      select.disabled = !ncclNodes.length;
    }
    ncclMessage.textContent = ncclNodes.length < 2 ? 'Fewer than two eligible nodes are idle. Refresh later.' : 'Select two distinct idle nodes in the same partition, then build the NCCL test.';
  } catch (error) {
    for (const select of [ncclFirst, ncclSecond]) select.replaceChildren(new Option('NCCL inventory unavailable', ''));
    ncclMessage.textContent = error.message;
  } finally { refresh.disabled = false; }
}
operationsAccordion.addEventListener('click', event => {
  if (event.target.closest('#troubleshooting-title') && !ncclInventoryRequested) loadNcclNodes();
});
ncclFirst.addEventListener('change', ncclSelection);
ncclSecond.addEventListener('change', ncclSelection);
document.querySelector('#refresh-nccl-nodes').addEventListener('click', loadNcclNodes);
ncclBuild.addEventListener('click', () => {
  ncclSelection();
  if (ncclBuild.disabled) return;
  const [first, partition] = ncclFirst.value.split('|');
  const [second] = ncclSecond.value.split('|');
  setCommand(`sbatch --nodes=2 --gpus-per-node=8 --ntasks-per-node=8 --exclusive --nodelist=${first},${second} --partition=${partition} nccl_run_allreduce.sbatch`);
  ncclMessage.textContent = 'NCCL test built. Review the command and confirm submission. The Output section will show the log and bandwidth.';
  reviewBuiltCommand();
});
async function watchNcclOutput(jobId) {
  if (!/^\d+$/.test(String(jobId)) || Number(jobId) <= 0) return;
  const jobUrl = new URL(window.location.href);
  jobUrl.searchParams.set('nccl_job', jobId);
  window.history.replaceState(null, '', jobUrl);
  const watch = ++ncclWatchRevision;
  const revision = outputRevision;
  let terminalLogWaits = 0;
  await new Promise(resolve => window.setTimeout(resolve, 1200));
  while (watch === ncclWatchRevision && revision === outputRevision) {
    try {
      const response = await fetch(`/api/troubleshooting/nccl-output/${jobId}`, {cache: 'no-store'});
      const body = await response.json();
      if (!response.ok) throw new Error(body.error || 'NCCL output unavailable.');
      if (watch !== ncclWatchRevision || revision !== outputRevision) return;
      const terminal = /^(COMPLETED|FAILED|CANCELLED|TIMEOUT|NODE_FAIL|OUT_OF_MEMORY)$/.test(body.job_state || '');
      const waitingForLog = terminal && (!body.log_available || (body.job_state === 'COMPLETED' && !body.concluded));
      const retryLog = waitingForLog && terminalLogWaits++ < 20;
      const bandwidth = body.avg_bus_bandwidth_gbps == null
        ? (terminal && !retryLog ? 'No bandwidth result available' : 'Waiting for bandwidth result')
        : `Average bus bandwidth: ${body.avg_bus_bandwidth_gbps.toFixed(3)} GB/s`;
      const log = terminal && !body.log_available
        ? 'The job has finished, but its NCCL log is not visible to the console yet.' : body.output;
      const delayNote = waitingForLog
        ? (retryLog ? '\n\nWaiting for the final log to become visible; retrying automatically…'
          : '\n\nFinal output is still unavailable. Refresh this page to retry this job without submitting another test.') : '';
      output.textContent = `Job ${jobId} · ${body.job_state || 'status pending'}\n${bandwidth}\n\n${log}${delayNote}`;
      outputTitle.textContent = 'NCCL all-reduce output';
      terminalCommand.textContent = `NCCL job ${jobId} log`;
      const finished = body.concluded || terminal;
      const passed = body.concluded && (!terminal || body.job_state === 'COMPLETED');
      setState(retryLog ? 'running' : finished ? (passed ? 'idle' : 'error') : 'running',
        retryLog ? 'Waiting for log' : finished ? (passed ? 'Complete' : 'Check result') : 'Running');
      output.scrollTop = output.scrollHeight;
      if (finished) {
        if (!retryLog) return;
      } else terminalLogWaits = 0;
    } catch (error) {
      if (watch === ncclWatchRevision && revision === outputRevision) {
        output.textContent += `\n\n${error.message} Refresh this page to retry reading this job's output.`;
        setState('error', 'Output unavailable');
      }
      return;
    }
    await new Promise(resolve => window.setTimeout(resolve, 3000));
  }
}

const gpuFryerNode = document.querySelector('#gpu-fryer-node');
const gpuFryerBuild = document.querySelector('#build-gpu-fryer');
const gpuFryerMessage = document.querySelector('#gpu-fryer-message');
let gpuFryerInventoryRequested = false;
let gpuFryerNodes = [];
let gpuFryerWatchRevision = 0;
async function loadGpuFryerNodes() {
  gpuFryerInventoryRequested = true;
  gpuFryerNodes = [];
  gpuFryerNode.disabled = true;
  gpuFryerBuild.disabled = true;
  gpuFryerNode.replaceChildren(new Option('Loading idle GPU nodes…', ''));
  const refresh = document.querySelector('#refresh-gpu-fryer-nodes');
  refresh.disabled = true;
  try {
    const response = await fetch('/api/troubleshooting/idle-gpu-fryer-nodes');
    const body = await response.json();
    if (!response.ok || !Array.isArray(body.nodes)) throw new Error(body.error || 'gpu-fryer node inventory unavailable.');
    gpuFryerNodes = body.nodes.filter(item => /^[A-Za-z0-9_.-]+$/.test(item.node) && /^[A-Za-z0-9_.-]+$/.test(item.partition) && Number.isInteger(item.gpu_count) && item.gpu_count >= 1 && item.gpu_count <= 16);
    gpuFryerNode.replaceChildren(new Option(gpuFryerNodes.length ? 'Select idle GPU node' : 'No idle GPU nodes', ''));
    gpuFryerNodes.forEach(item => gpuFryerNode.add(new Option(`${item.node} · ${item.partition} · ${item.gpu_count} GPUs`, `${item.node}|${item.partition}|${item.gpu_count}`)));
    gpuFryerNode.disabled = !gpuFryerNodes.length;
    gpuFryerMessage.textContent = gpuFryerNodes.length ? 'Choose an idle node and build the test. The selected GPUs are reserved by Slurm during installation and the 120-second run.' : 'No idle GPU nodes are available. Refresh after running jobs finish.';
  } catch (error) {
    gpuFryerNode.replaceChildren(new Option('gpu-fryer inventory unavailable', ''));
    gpuFryerMessage.textContent = error.message;
  } finally { refresh.disabled = false; }
}
operationsAccordion.addEventListener('click', event => {
  if (event.target.closest('#troubleshooting-title') && !gpuFryerInventoryRequested) loadGpuFryerNodes();
});
gpuFryerNode.addEventListener('change', () => {
  gpuFryerBuild.disabled = !gpuFryerNodes.some(item => `${item.node}|${item.partition}|${item.gpu_count}` === gpuFryerNode.value);
});
document.querySelector('#refresh-gpu-fryer-nodes').addEventListener('click', loadGpuFryerNodes);
gpuFryerBuild.addEventListener('click', () => {
  const selected = gpuFryerNodes.find(item => `${item.node}|${item.partition}|${item.gpu_count}` === gpuFryerNode.value);
  if (!selected) { gpuFryerBuild.disabled = true; return; }
  setCommand(`sbatch --nodes=1 --ntasks=1 --exclusive --time=00:20:00 --gres=gpu:${selected.gpu_count} --nodelist=${selected.node} --partition=${selected.partition} gpu-fryer-120s.sbatch`);
  gpuFryerMessage.textContent = 'gpu-fryer test built. Review and confirm submission; Output will show installation and stress-test progress.';
  reviewBuiltCommand();
});
async function watchGpuFryerOutput(jobId) {
  const watch = ++gpuFryerWatchRevision;
  const revision = outputRevision;
  let terminalLogWaits = 0;
  await new Promise(resolve => window.setTimeout(resolve, 1200));
  while (watch === gpuFryerWatchRevision && revision === outputRevision) {
    try {
      const response = await fetch(`/api/troubleshooting/gpu-fryer-output/${jobId}`);
      const body = await response.json();
      if (!response.ok) throw new Error(body.error || 'gpu-fryer output unavailable.');
      if (watch !== gpuFryerWatchRevision || revision !== outputRevision) return;
      output.textContent = `Job ${jobId} · ${body.job_state || 'status pending'}\n${body.healthy ? 'GPU stress test passed' : 'Installing or running gpu-fryer'}\n\n${body.output}`;
      outputTitle.textContent = 'gpu-fryer output';
      terminalCommand.textContent = `gpu-fryer job ${jobId} log`;
      const finished = /^(COMPLETED|FAILED|CANCELLED|TIMEOUT|NODE_FAIL|OUT_OF_MEMORY)$/.test(body.job_state || '');
      setState(finished ? (body.job_state === 'COMPLETED' && body.healthy ? 'idle' : 'error') : 'running', finished ? (body.job_state === 'COMPLETED' && body.healthy ? 'Complete' : 'Check result') : 'Running');
      output.scrollTop = output.scrollHeight;
      if (finished) {
        if (body.log_available || terminalLogWaits++ >= 5) return;
      } else terminalLogWaits = 0;
    } catch (error) {
      if (watch === gpuFryerWatchRevision && revision === outputRevision) gpuFryerMessage.textContent = error.message;
      return;
    }
    await new Promise(resolve => window.setTimeout(resolve, 3000));
  }
}

let gpuTestWatchRevision = 0;
async function watchGpuTestOutput(jobId) {
  const watch = ++gpuTestWatchRevision;
  const revision = outputRevision;
  let terminalLogWaits = 0;
  await new Promise(resolve => window.setTimeout(resolve, 1200));
  while (watch === gpuTestWatchRevision && revision === outputRevision) {
    try {
      const response = await fetch(`/api/slurm/gpu-test-output/${jobId}`);
      const body = await response.json();
      if (!response.ok) throw new Error(body.error || 'GPU test output unavailable.');
      if (watch !== gpuTestWatchRevision || revision !== outputRevision) return;
      const finished = /^(COMPLETED|FAILED|CANCELLED|TIMEOUT|NODE_FAIL|OUT_OF_MEMORY)$/.test(body.job_state || '');
      const passed = body.job_state === 'COMPLETED' && /^PASS:/m.test(body.output);
      output.textContent = `Job ${jobId} · ${body.job_state || 'status pending'}\n\n${body.output}`;
      outputTitle.textContent = 'GPU test output';
      terminalCommand.textContent = `GPU test job ${jobId} log`;
      setState(finished ? (passed ? 'idle' : 'error') : 'running', finished ? (passed ? 'Passed' : 'Check result') : 'Running');
      output.scrollTop = output.scrollHeight;
      if (finished) {
        if (body.log_available || terminalLogWaits++ >= 5) return;
      } else terminalLogWaits = 0;
    } catch (error) {
      if (watch === gpuTestWatchRevision && revision === outputRevision) {
        output.textContent = `GPU test job ${jobId}: ${error.message}`;
        setState('error', 'Output unavailable');
      }
      return;
    }
    await new Promise(resolve => window.setTimeout(resolve, 3000));
  }
}

function refreshConsole() {
  const commandRunning = document.querySelector('#run').disabled;
  const confirmationOpen = !confirmationModal.hidden;
  if (!commandRunning && !confirmationOpen) window.location.reload();
}
document.querySelector('#refresh-console').addEventListener('click', refreshConsole);
document.addEventListener('keydown', event => {
  if (event.key !== 'Enter' || event.ctrlKey || event.metaKey || event.altKey || event.shiftKey) return;
  if (event.target.closest('input, textarea, select, button, a, [contenteditable="true"]')) return;
  event.preventDefault();
  refreshConsole();
});

// Read-only job links also restore polling after a page refresh; never resubmit.
const linkedNcclJob = new URL(window.location.href).searchParams.get('nccl_job');
if (linkedNcclJob && /^\d+$/.test(linkedNcclJob) && Number(linkedNcclJob) > 0) {
  setWorkspaceView('console');
  watchNcclOutput(linkedNcclJob);
}
