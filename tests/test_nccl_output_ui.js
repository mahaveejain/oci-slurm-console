// Run with: node tests/test_nccl_output_ui.js
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('static/app.js','utf8');
const watch = source.slice(source.indexOf('async function watchNcclOutput('),source.indexOf('const gpuFryerNode'));
const empty = {job_state:'COMPLETED',log_available:false,output:'No NCCL output yet. The job may be queued.',concluded:false,avg_bus_bandwidth_gbps:null};
const partial = {...empty,log_available:true,output:'NCCL nodes:\nnode-a\nnode-b'};
const complete = {...partial,concluded:true,avg_bus_bandwidth_gbps:463.839,output:'# Out of bounds values : 0 OK\n# Avg bus bandwidth : 463.839'};
async function scenario(results) {
  let calls=0, currentUrl, lastState;
  const context=vm.createContext({URL, Number, String, Promise, ncclWatchRevision:0,outputRevision:0,
    window:{location:{href:'http://127.0.0.1:8080/'},history:{replaceState(_s,_t,url){currentUrl=String(url);}},setTimeout(fn){fn();}},
    output:{textContent:'',scrollHeight:1},outputTitle:{},terminalCommand:{},
    setState(kind,label){lastState=[kind,label];},
    fetch:async(url,options)=>{
      assert.equal(url,'/api/troubleshooting/nccl-output/77367');
      assert.equal(options.cache,'no-store');
      const result=results[Math.min(calls++,results.length-1)];
      return {ok:true,json:async()=>result};
    },
  });
  vm.runInContext(watch,context);
  await context.watchNcclOutput(77367);
  assert.match(currentUrl,/nccl_job=77367/);
  return {calls,context,lastState};
}
(async()=>{
  for(const initial of [empty,partial]) {
    const result=await scenario([...Array(8).fill(initial),complete]);
    assert.equal(result.calls,9,'must wait for late output beyond the old five-retry limit');
    assert.match(result.context.output.textContent,/463\.839 GB\/s/);
    assert.deepEqual(result.lastState,['idle','Complete']);
  }
  const unavailable=await scenario([empty]);
  assert.equal(unavailable.calls,21,'polling must remain bounded');
  assert.match(unavailable.context.output.textContent,/Refresh this page/);
  assert.doesNotMatch(unavailable.context.output.textContent,/may be queued/);
  assert.deepEqual(unavailable.lastState,['error','Check result']);
  const failed=await scenario([{...complete,job_state:'FAILED'}]);
  assert.deepEqual(failed.lastState,['error','Check result']);
  console.log('NCCL output UI: delayed and partial logs, bounded retries, read-only reload link, bandwidth display, and failed-job status passed.');
})().catch(error=>{console.error(error);process.exitCode=1;});
