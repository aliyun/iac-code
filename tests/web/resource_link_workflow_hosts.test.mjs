import test from 'node:test';
import assert from 'node:assert/strict';
import {jobBackend,readJob,readTransportEvidence} from '../../scripts/web/e2e/resource_link_workflow/adapter.mjs';
import {cdpConnection} from '../../scripts/web/e2e/resource_link_workflow/browser.mjs';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

const config = {worker_host:'macmini',bailian_endpoint:'ws://127.0.0.1:4501',sol_endpoint:'ws://127.0.0.1:4502'};
const remote = {id:'alilang-macmini',endpoint:'ws://127.0.0.1:4510',host:'macmini',cwd:'/remote/workers/job',
  link_fingerprint:'a'.repeat(64)};

test('missing/empty pre-submission Sol trace permits only full original history read, never fabricates trace',()=>{
  const root=fs.mkdtempSync(path.join(os.tmpdir(),'workflow-pretrace-read-'));
  const file=path.join(root,'raw-rollout.jsonl');
  const binding={role:'sol',thread_id:'original-sol',turn_id:null,submission:'not_submitted'};
  try {
    assert.deepEqual(readTransportEvidence(file,binding),{state:'missing',events:[]});
    assert.equal(fs.existsSync(file),false);
    fs.writeFileSync(file,'\n ');
    assert.deepEqual(readTransportEvidence(file,binding),{state:'empty',events:[]});
    for (const patch of [{role:'glm'},{thread_id:null},{turn_id:'bound-turn'},
      {submission:'submitted'},{submission:undefined}])
      assert.throws(()=>readTransportEvidence(file,{...binding,...patch}),/unavailable/);
    fs.writeFileSync(file,'not-json\n');
    assert.throws(()=>readTransportEvidence(file,binding));
    assert.throws(()=>readTransportEvidence(root,binding));
  } finally {fs.rmSync(root,{recursive:true,force:true});}
});

test('pretrace Sol read retains complete original paginated turns and explicit missing-trace fact',async()=>{
  const root=fs.mkdtempSync(path.join(os.tmpdir(),'workflow-sol-missing-read-'));
  const jobId='b'.repeat(32),dir=path.join(root,'jobs',jobId),calls=[];
  const backend={id:'sol-local',host:'local',endpoint:config.sol_endpoint};
  fs.mkdirSync(dir,{recursive:true});
  const write=(file,value)=>fs.writeFileSync(file,JSON.stringify(value));
  write(path.join(root,'environment.json'),config);
  write(path.join(root,'server.json'),{origin:'http://127.0.0.1:16099',token:'fixture-private'});
  write(path.join(dir,'launch.json'),{job_id:jobId,role:'sol',thread_id:'original-sol',worker_backend:backend});
  const oldFetch=globalThis.fetch,oldSocket=globalThis.WebSocket;
  globalThis.fetch=async()=>({ok:true,json:async()=>({id:jobId,role:'sol',thread_id:'original-sol',turn_id:null,
    submission:'not_submitted',worker_backend:backend})});
  globalThis.WebSocket=class extends EventTarget {
    constructor(url){super();assert.equal(url,backend.endpoint);queueMicrotask(()=>this.dispatchEvent(new Event('open')));}
    send(raw){const request=JSON.parse(raw);calls.push(request.method);
      assert(['initialize','initialized','thread/read','thread/turns/list'].includes(request.method));
      if(request.id==null)return;
      let result={};
      if(request.method==='thread/read') result={thread:{id:'original-sol',status:{type:'notLoaded'}}};
      if(request.method==='thread/turns/list') {
        const full=request.params.itemsView==='full';
        result={data:[{id:'original-turn',status:'completed',itemsView:full?'full':'notLoaded',
          ...(full?{items:[{type:'userMessage',content:[{type:'text',text:'original input'}]}]}:{})}],nextCursor:null};
      }
      queueMicrotask(()=>{const event=new Event('message');event.data=JSON.stringify({id:request.id,result});this.dispatchEvent(event);});
    }
    close(){}
  };
  try {
    const value=await readJob(root,jobId);
    const raw=JSON.parse(fs.readFileSync(value.file,'utf8'));
    assert.equal(raw.evidence_scope,'all_turns_full_paginated_files');
    assert.equal(raw.transport_evidence.state,'missing');
    assert.equal(fs.existsSync(path.join(dir,'raw-rollout.jsonl')),false);
    const saved=raw.original_responses.pages.find(page=>page.path);
    const original=JSON.parse(fs.readFileSync(path.join(root,saved.path),'utf8'));
    assert.equal(original.result.data[0].items[0].content[0].text,'original input');
    assert.equal(fs.statSync(value.file).mode & 0o777,0o600);
    assert.equal(calls.filter(method=>method==='thread/turns/list').length,2);
  } finally {globalThis.fetch=oldFetch;globalThis.WebSocket=oldSocket;fs.rmSync(root,{recursive:true,force:true});}
});

test('legacy reads keep original local backend despite new machine selection',()=>{
  assert.equal(jobBackend(config,{},'glm').endpoint,'ws://127.0.0.1:4501');
  assert.equal(jobBackend(config,{},'sol').id,'sol-local');
});

test('frozen remote binding ignores current local selection',()=>{
  assert.deepEqual(jobBackend({...config,worker_host:'local'},{worker_backend:remote},'glm',remote),remote);
});

test('durable backend disagreement rejected before opening a socket',()=>{
  assert.throws(()=>jobBackend(config,{worker_backend:remote},'glm',{...remote,host:'local'}),/conflict/);
});

test('binding object key ordering does not change the backend',()=>{
  assert.deepEqual(jobBackend(config,{worker_backend:remote},'glm',
    {link_fingerprint:remote.link_fingerprint,cwd:remote.cwd,host:remote.host,endpoint:remote.endpoint,id:remote.id}),remote);
});

for (const patch of [{id:'sol-local'},{host:'local'},{endpoint:'ws://192.0.2.1:4500'},
  {endpoint:'ws://127.0.0.1:4500'},{cwd:'/remote/../elsewhere'},{cwd:'relative'},
  {endpoint:'ws://127.0.0.1:0'},{endpoint:'ws://127.0.0.1:65536'}])
  test(`remote binding fails closed ${JSON.stringify(patch)}`,()=>{
    assert.throws(()=>jobBackend(config,{worker_backend:{...remote,...patch}},'glm'));
  });

test('Sol cannot move with GLM or be read from Mac mini',()=>{
  assert.throws(()=>jobBackend(config,{worker_backend:remote},'sol'));
});

test('remote CDP rewrites advertised9225 to owned9226, never local Chrome',async()=>{
  const request = {remote_cdp:true,cdp_endpoint:'http://127.0.0.1:9226'};
  const result = await cdpConnection(request,async url=>{
    assert.equal(url,'http://127.0.0.1:9226/json/version');
    return {ok:true,json:async()=>({webSocketDebuggerUrl:'ws://127.0.0.1:9225/devtools/browser/abc-123'})};
  });
  assert.equal(result,'ws://127.0.0.1:9226/devtools/browser/abc-123');
  assert.equal(await cdpConnection({cdp_endpoint:'http://127.0.0.1:9225'}),'http://127.0.0.1:9225');
});

test('remote CDP accepts Chrome advertised forwarded Host port',async()=>{
  assert.equal(await cdpConnection({remote_cdp:true,cdp_endpoint:'http://127.0.0.1:9226'},async()=>
    ({ok:true,json:async()=>({webSocketDebuggerUrl:'ws://127.0.0.1:9226/devtools/browser/abc'})})),
  'ws://127.0.0.1:9226/devtools/browser/abc');
});

for (const url of ['ws://elsewhere:9225/devtools/browser/abc','ws://127.0.0.1:9227/devtools/browser/abc',
  'wss://127.0.0.1:9225/devtools/browser/abc','ws://user:secret@127.0.0.1:9225/devtools/browser/abc',
  'ws://127.0.0.1:9225/devtools/page/abc','ws://127.0.0.1:9225/devtools/browser/abc?token=fixture'])
  test(`remote CDP rejects unowned identity ${url}`,async()=>{
    await assert.rejects(()=>cdpConnection({remote_cdp:true,cdp_endpoint:'http://127.0.0.1:9226'},async()=>
      ({ok:true,json:async()=>({webSocketDebuggerUrl:url})})));
  });

test('wrong forward cannot fall back to local Chrome',async()=>{
  let fetched = false;
  await assert.rejects(()=>cdpConnection({remote_cdp:true,cdp_endpoint:'http://127.0.0.1:9225'},async()=>{fetched=true;}));
  assert.equal(fetched,false);
});

for (const scenario of ['exact','database-conflict','trace-conflict','missing-database','wrong-link'])
  test(`owning Mini read is frozen and never submits/resumes: ${scenario}`,async()=>{
    const root=fs.mkdtempSync(path.join(os.tmpdir(),'workflow-mini-read-'));
    const jobId='a'.repeat(32),dir=path.join(root,'jobs',jobId),calls=[];
    fs.mkdirSync(dir,{recursive:true});
    const write=(file,value)=>fs.writeFileSync(file,JSON.stringify(value));
    write(path.join(root,'environment.json'),{...config,worker_host:'local'});
    write(path.join(root,'server.json'),{origin:'http://127.0.0.1:16099',token:'fixture-private'});
    write(path.join(root,'macmini-link.json'),{fingerprint:scenario==='wrong-link'?'b'.repeat(64):remote.link_fingerprint});
    write(path.join(dir,'launch.json'),{job_id:jobId,role:'glm',thread_id:'exact-thread',worker_backend:remote});
    fs.writeFileSync(path.join(dir,'raw-rollout.jsonl'),[
      {method:'transport/backend_selected',params:{role:'glm',worker_backend:scenario==='trace-conflict'? {...remote,host:'local'}:remote}},
      {method:'transport/thread_created',params:{threadId:'exact-thread'}},
      {method:'transport/turn_created',params:{threadId:'exact-thread',turnId:'exact-turn'}},
    ].map(JSON.stringify).join('\n')+'\n');
    const oldFetch=globalThis.fetch,oldSocket=globalThis.WebSocket;
    globalThis.fetch=async()=>({ok:true,json:async()=>({id:jobId,role:'glm',thread_id:'exact-thread',turn_id:'exact-turn',
      ...(scenario==='missing-database'?{}:{worker_backend:scenario==='database-conflict'?{...remote,host:'local'}:remote})})});
    globalThis.WebSocket=class extends EventTarget {
      constructor(url){super();assert.equal(url,remote.endpoint);calls.push('socket');queueMicrotask(()=>this.dispatchEvent(new Event('open')));}
      send(raw){const request=JSON.parse(raw);calls.push(request.method);
        assert(['initialize','initialized','thread/read'].includes(request.method));
        if(request.id==null)return;
        const result=request.method==='initialize'?{}:{thread:{id:'exact-thread',turns:[{id:'exact-turn',status:'completed',items:[]}]}};
        queueMicrotask(()=>{const event=new Event('message');event.data=JSON.stringify({id:request.id,result});this.dispatchEvent(event);});
      }
      close(){}
    };
    try {
      if(scenario==='exact') {
        const value=await readJob(root,jobId);
        const raw=JSON.parse(fs.readFileSync(value.file,'utf8'));
        assert.deepEqual(raw.job_binding.worker_backend,remote);
        assert.equal(fs.statSync(value.file).mode & 0o777,0o600);
        assert(calls.includes('thread/read'));
      } else {
        await assert.rejects(()=>readJob(root,jobId));
        assert.deepEqual(calls,[]);
      }
    } finally {
      globalThis.fetch=oldFetch;globalThis.WebSocket=oldSocket;
      fs.rmSync(root,{recursive:true,force:true});
    }
  });
