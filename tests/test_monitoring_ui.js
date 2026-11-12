// Run with: node tests/test_monitoring_ui.js
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const source = fs.readFileSync('static/monitoring.js', 'utf8');
function browser(mode, storage = new Map()) {
  const elements = new Map();
  const get = key => {
    if (!elements.has(key)) elements.set(key, {value:'', textContent:'', handlers:{},
      addEventListener(event, fn) {this.handlers[event]=fn;}, setAttribute(key,value){this[key]=value;}, focus(){}});
    return elements.get(key);
  };
  get('#grafana-url').value = '192.0.2.30';
  const stored = [], calls = [], opened = [];
  const context = vm.createContext({URL, AbortController, TypeError, setTimeout, clearTimeout,
    document:{querySelector:get}, localStorage:{getItem(key){return storage.get(key) ?? null;},setItem(key,value){stored.push([key,value]);storage.set(key,value);},removeItem(key){storage.delete(key);}},
    window:{open(url){opened.push(url); return {location:{replace(url){opened.push(url);}},close(){opened.push('closed');}};}},
    fetch: async (url, options) => {calls.push([url, options]);
      if (mode==='unavailable') throw new TypeError('blocked');
      if(url.endsWith('/status')) return {ok:true,json:async()=>({service:'slurm-grafana-browser-helper',token:'test-token'})};
      return {ok:mode!=='rejected',json:async()=>mode==='rejected'
        ? {ok:false,error:'Grafana rejected the sign-in.'} : {ok:true,signed_in:mode!=='plain'}};},
  });
  vm.runInContext(source, context);
  return {get, context, stored, calls, opened, storage};
}
async function scenario(mode) {
  const {get, context, stored, calls, opened} = browser(mode);
  assert.equal(context.normalizeGrafanaUrl('192.0.2.30').href,'http://192.0.2.30:3000/login');
  assert.equal(context.normalizeGrafanaUrl('https://monitor.example/grafana/').href,'https://monitor.example/grafana/login');
  for(const value of ['javascript:alert(1)','ftp://example.com','http://u:p@example.com','https://example.com?password=secret','https://example.com/#token','//example.com','bad host']) {
    assert.throws(()=>context.normalizeGrafanaUrl(value));
  }
  if(mode!=='plain') {get('#grafana-username').value='test-user';get('#grafana-password').value='test-secret';}
  await get('#grafana-form').handlers.submit({preventDefault(){}});
  assert.equal(stored.length,1);
  assert.equal(JSON.stringify(stored).includes('test-'),false,'credentials must never be persisted');
  assert.equal(opened.some(url=>url.includes('test-')),false,'credentials must never enter navigation URLs');
  assert.equal(get('#grafana-password').value,mode==='plain' ? '' : 'test-secret');
  assert.equal(opened.length,0,'the page must not open a regular browser tab');
  assert.equal(calls[0][0],'http://127.0.0.1:8765/status');
  if(mode==='unavailable') assert.match(get('#grafana-message').textContent,/helper is unavailable/);
  else {
    assert.equal(calls.length,2);
    assert.equal(calls[1][0],'http://127.0.0.1:8765/open');
    assert.equal(calls[1][1].headers.Authorization,'Bearer test-token');
    const sent=JSON.parse(calls[1][1].body);
    assert.equal(sent.url,'http://192.0.2.30:3000/login');
    assert.equal(sent.password,mode==='plain'?'':'test-secret');
    assert.match(get('#grafana-message').textContent,mode==='rejected'?/rejected/:/Incognito/);
  }
}
(async()=>{for(const mode of ['plain','success','unavailable','rejected']) await scenario(mode);
const storage = new Map();
const first = browser('plain',storage);
first.get('#grafana-username').value='saved-user';
first.get('#grafana-password').value='saved-password';
first.get('#grafana-save-credentials').handlers.click();
assert.equal(first.calls.length,0,'saving must not submit credentials');
const next = browser('plain',storage);
assert.equal(next.get('#grafana-username').value,'saved-user');
assert.equal(next.get('#grafana-password').value,'saved-password');
assert.equal(next.get('#grafana-password').type,'password');
next.get('#grafana-toggle-password').handlers.click();
assert.equal(next.get('#grafana-password').type,'text');
assert.equal(next.get('#grafana-toggle-password')['aria-pressed'],'true');
next.get('#grafana-toggle-password').handlers.click();
assert.equal(next.get('#grafana-password').type,'password');
next.get('#grafana-url').value='https://other.example/login';
next.get('#grafana-url').handlers.input();
assert.equal(next.get('#grafana-password').value,'','saved credentials cannot follow another destination');
next.get('#grafana-url').value='192.0.2.30';
next.get('#grafana-url').handlers.input();
assert.equal(next.get('#grafana-password').value,'saved-password');
next.get('#grafana-clear-credentials').handlers.click();
assert.equal(next.get('#grafana-username').value,'');
assert.equal(next.get('#grafana-password').value,'');
assert.equal(storage.has('slurm-console.grafana-credentials'),false);
assert.equal(storage.has('slurm-console.grafana-url'),true);
assert.equal(browser('plain',storage).get('#grafana-password').value,'','cleared credentials must not return');
console.log('Monitoring UI: save, reload, clear, destination binding, visibility toggle; URL validation, credential privacy, Mac helper routing and error handling passed.');})().catch(error=>{console.error(error);process.exitCode=1;});
