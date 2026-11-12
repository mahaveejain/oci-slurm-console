// Run with: node tests/test_confirmation_ui.js
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../static/app.js'), 'utf8');
const confirmationCode = source.slice(source.indexOf('function closeLifecycleConfirmation'), source.indexOf('async function runCommand'));
function dialog(clipboard, legacyCopy) {
  const elements = new Map();
  const get = id => {
    if (!elements.has(id)) elements.set(id, {value: '', textContent: '', hidden: true, handlers: {},
      addEventListener(type, handler) { this.handlers[type] = handler; },
      focus() {}, select() {}, setAttribute() {}});
    return elements.get(id);
  };
  const context = vm.createContext({navigator: {clipboard}, document: {querySelector: get, execCommand: legacyCopy},
    window: {setTimeout: fn => fn()}, command: {value: 'bm-hard-reboot worker ocid1.instance.example'},
    confirmationModal: get('#confirmation-modal'), confirmationPhrase: get('#confirmation-phrase'),
    confirmationInput: get('#confirmation-input'), confirmationError: get('#confirmation-error'), confirmationResolver: undefined});
  vm.runInContext(confirmationCode, context);
  return {context, get};
}
(async () => {
  for (const mode of ['modern', 'rejected', 'missing', 'unavailable']) {
    let copied;
    const clipboard = mode === 'modern' ? {writeText: async text => { copied = text; }}
      : mode === 'rejected' ? {writeText: async () => {throw new Error('denied');}} : undefined;
    const {context, get} = dialog(clipboard, () => {
      if (mode === 'unavailable') return false;
      copied = get('#confirmation-input').value;
      return true;
    });
    let finished = false;
    const result = context.requestLifecycleConfirmation('confirm');
    result.then(() => {finished = true;});
    get('#confirm-lifecycle-action').handlers.click();
    assert.equal(get('#confirmation-modal').hidden, false, 'empty input must be rejected');
    await get('#copy-confirmation').handlers.click();
    assert.equal(get('#confirmation-input').value, 'confirm');
    assert.equal(finished, false, 'copy must not execute the action');
    assert.equal(get('#confirmation-modal').hidden, false);
    assert.equal(get('#confirmation-command').textContent, context.command.value);
    if (mode !== 'unavailable') assert.equal(copied, 'confirm');
    else assert.match(get('#confirmation-error').textContent, /Clipboard access is unavailable/);
    get('#confirm-lifecycle-action').handlers.click();
    assert.equal(await result, 'confirm');
  }
  const {context, get} = dialog(undefined, () => false);
  const canceled = context.requestLifecycleConfirmation('confirm');
  get('#cancel-confirmation').handlers.click();
  assert.equal(await canceled, '');
  for (const command of [
    'bm-hard-reboot worker ocid1.instance.example',
    'mgmt nodes terminate --nodes worker',
    'mgmt nodes tag-and-terminate --nodes worker',
    'mgmt clusters add node --count 2 --cluster cluster',
    'scontrol update NodeName=worker State=DRAIN Reason=maintenance',
    'scontrol update NodeName=worker State=RESUME',
    'rdma_per_link_bw_test.py worker other',
    'sbatch --nodes=1 --ntasks=1 --cpus-per-task=2 --mem=2G --gres=gpu:1 --time=00:02:00 --nodelist=worker --partition=compute gpu-test-30s.sh',
    'sbatch --nodes=2 --gpus-per-node=8 --ntasks-per-node=8 --exclusive --nodelist=worker,other --partition=compute nccl_run_allreduce.sbatch',
    'sbatch --nodes=1 --ntasks=1 --exclusive --time=00:20:00 --gres=gpu:8 --nodelist=worker --partition=compute gpu-fryer-120s.sbatch',
  ]) assert.equal(context.lifecycleConfirmationFor(command), 'confirm', command);
  assert.equal(context.lifecycleConfirmationFor('sinfo'), '');
  console.log('Confirmation UI: clipboard success, rejection, missing API, blocked fallback, explicit confirmation, cancellation, and all action types passed.');
})().catch(error => {console.error(error); process.exitCode = 1;});
