import test from 'node:test';
import assert from 'node:assert/strict';
import {startAgentBridge,shellQuote} from '../../scripts/web/e2e/resource_link_workflow/agent_bridge.mjs';

const request=async(bridge,body,token=bridge.token)=>{
  const response=await fetch(bridge.origin+'/invoke',{method:'POST',headers:{authorization:'Bearer '+token,
    'content-type':'application/json'},body:JSON.stringify(body)});
  return {status:response.status,...await response.json()};
};
test('four script bridges isolate exact jobs/turns and credentials',async()=>{
  const events=[],calls=[],bridges=[];
  try{
    for(let i=0;i<4;i++)bridges.push(await startAgentBridge({
      ready:Promise.resolve(),binding:()=>({jobId:'job-'+i,threadId:'thread-'+i,turnId:'turn-'+i}),
      record:(method,params)=>events.push({method,params}),post:async(operation,args)=>{
        calls.push({i,operation,args});return {resource:'R000'+i};
      }}));
    assert.equal(new Set(bridges.map(x=>x.port)).size,4);
    assert.equal(new Set(bridges.map(x=>x.token)).size,4);
    const results=await Promise.all(bridges.map(bridge=>request(bridge,{operation:'context',arguments:{}})));
    assert.deepEqual(results.map(x=>x.result.resource),['R0000','R0001','R0002','R0003']);
    assert.equal(calls.length,4);
    assert.equal((await request(bridges[1],{operation:'request',arguments:{}},bridges[0].token)).status,403);
    for(const body of [{operation:'_terminal',arguments:{}},{operation:'request',arguments:{},job_id:'other'},
      {operation:'context',arguments:[]}])assert.equal((await request(bridges[0],body)).status,502);
    assert.equal(calls.length,4);
    for(const event of events.filter(x=>x.method==='script/request')){
      const i=event.params.jobId.split('-')[1];
      assert.equal(event.params.threadId,'thread-'+i);assert.equal(event.params.turnId,'turn-'+i);
    }
    for(const bridge of bridges)assert(!JSON.stringify(events).includes(bridge.token));
  }finally{await Promise.all(bridges.map(x=>x.close()));}
});
test('script call waits for durable turn, rejects overlaps and never retries unknown writes',async()=>{
  let bind,finish,calls=0,turnId;
  const ready=new Promise(resolve=>bind=resolve),done=new Promise(resolve=>finish=resolve);
  const bridge=await startAgentBridge({ready,binding:()=>({threadId:'exact',turnId}),record:()=>{},post:async()=>{
    calls++;await done;throw Error('lost response after write');
  }});
  try{
    const pending=request(bridge,{operation:'request',arguments:{dimension:'CREATE'}});
    // Allow HTTP arrival, without any model/resource wait.
    await new Promise(resolve=>setTimeout(resolve,30));
    assert.equal(calls,0);
    assert.equal((await request(bridge,{operation:'request',arguments:{}})).status,409);
    turnId='exact-turn';bind();finish();
    assert.equal((await pending).status,502);assert.equal(calls,1);
  }finally{await bridge.close();}
});
test('controlled Server rejection stays distinguishable from transport unknown',async()=>{
  const bridge=await startAgentBridge({ready:Promise.resolve(),binding:()=>({threadId:'t',turnId:'u'}),record:()=>{},
    post:async()=>{const error=Error('approved quota exhausted');error.name='OperationRejected';throw error;}});
  try{const result=await request(bridge,{operation:'cleanup',arguments:{dimension:'QUERY'}});
    assert.equal(result.status,422);assert.equal(result.outcome,'rejected');assert.match(result.error,/quota exhausted/);
  }finally{await bridge.close();}
});
test('shell command quotes exact absolute paths including apostrophes',()=>{
  assert.equal(shellQuote("/fixture/it's here/python"),"'/fixture/it'\\''s here/python'");
});
