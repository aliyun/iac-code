"""Execute the real frontend query client without a DOM, browser or network."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest


def test_resource_attribute_query_snapshot_and_late_response_contract(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is unavailable")
    module = Path(__file__).parents[2] / "src/iac_code/web/static/js/resource_index.js"
    script = tmp_path / "query-test.mjs"
    script.write_text(
        "import assert from 'node:assert/strict';\n"
        + "const {ResourceIndex,queryResourceAttributes}=await import("
        + json.dumps(module.as_uri())
        + ");\n"
        + r"""
const index=new ResourceIndex();
const ref={referenceId:'ref-function',provider:'aliyun',resourceType:'ALIYUN::FC::Function',
  identifier:'Handler',identifierKind:'name',identifierField:'FunctionName',scopeKind:'regional',
  accountScope:'fake-account',consolePartition:'aliyun',regionId:'cn-hangzhou',attributes:{},aliases:[],sources:[{toolUseId:'tool-1'}]};
const snapshot=(revision,items=[ref],extra={})=>({schemaVersion:1,revision,items,bindings:[],...extra});
const payload={identifier:'Handler',identifierKind:'name',referenceId:'ref-function',queryMissingAttributes:true};
const fetched={...ref,attributes:{ServiceName:'ParentA'}};
const initialize=async()=>{index.reset('session-a');await index.loadSnapshot(snapshot(1),()=>assert.fail());};
await initialize();
let requests=0;
const result=await queryResourceAttributes(index,payload,{sessionId:'session-a',isCurrent:()=>true,
  query:async(id,request)=>{requests++;assert.equal(id,'session-a');assert.deepEqual(request,payload);
    return {status:'attributes_loaded',referenceId:ref.referenceId,url:'javascript:injected',
      snapshot:snapshot(2,[],{nextCursor:'2:0'})};},
  fetchPage:()=>snapshot(2,[fetched])});
assert.equal(result.status,'attributes_loaded');assert.equal(requests,1);
assert.equal(index.references.get(ref.referenceId).attributes.ServiceName,'ParentA');
assert.equal(result.url,undefined);

for(const reason of ['resource_query_timeout','resource_query_permission_required',
  'resource_query_permission_denied','resource_query_account_changed']){
  await initialize();
  const result=await queryResourceAttributes(index,payload,{sessionId:'session-a',isCurrent:()=>true,
    query:async()=>({status:'needs_context',reason}),fetchPage:()=>assert.fail()});
  assert.deepEqual(result,{status:'needs_context',reason});assert.deepEqual(index.references.get(ref.referenceId).attributes,{});
}
for(const change of ['session','source']){
  await initialize();let resolve;let visible=true;
  const pending=queryResourceAttributes(index,payload,{sessionId:'session-a',isCurrent:()=>visible,
    query:()=>new Promise(done=>{resolve=done;}),fetchPage:()=>assert.fail()});
  if(change==='session') index.reset('session-b');else visible=false;
  resolve({status:'attributes_loaded',referenceId:ref.referenceId,snapshot:snapshot(2,[fetched])});
  assert.equal((await pending).reason,'resource_query_source_changed');
  assert.equal(index.references.get(ref.referenceId)?.attributes.ServiceName,undefined);
}
await initialize();let refreshed=0;
const conflict=await queryResourceAttributes(index,payload,{sessionId:'session-a',isCurrent:()=>true,
  query:async()=>{throw Object.assign(Error('stale'),{status:409});},fetchPage:()=>{refreshed++;return snapshot(3);}});
assert.equal(conflict.reason,'resource_query_source_changed');assert.equal(refreshed,1);
const wrong=await queryResourceAttributes(index,payload,{sessionId:'session-a',isCurrent:()=>true,
  query:async()=>({status:'attributes_loaded',referenceId:'ref-other',snapshot:snapshot(4)}),fetchPage:()=>assert.fail()});
assert.equal(wrong.reason,'resource_query_source_changed');
await initialize();
const otherAccount=await queryResourceAttributes(index,payload,{sessionId:'session-a',isCurrent:()=>true,
  query:async()=>({status:'attributes_loaded',referenceId:ref.referenceId,
    snapshot:snapshot(2,[{...fetched,accountScope:'other-account'}])}),fetchPage:()=>assert.fail()});
assert.equal(otherAccount.reason,'resource_query_source_changed','lookup must not silently select another account');
console.log('passed');
""",
        encoding="utf-8",
    )
    result = subprocess.run([node, str(script)], capture_output=True, text=True, encoding="utf-8", timeout=20)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "passed"


def test_explicit_account_context_client_is_isolated_and_does_not_create_facts(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is unavailable")
    module = Path(__file__).parents[2] / "src/iac_code/web/static/js/resource_index.js"
    script = tmp_path / "account-test.mjs"
    script.write_text(
        "import assert from 'node:assert/strict';\n"
        + "const {ResourceIndex,loadResourceAccountContext}=await import("
        + json.dumps(module.as_uri())
        + ");\n"
        + r"""
const index=new ResourceIndex(); index.reset('session-a');
const choice={accountContextId:'account-context:'+ 'a'.repeat(32),
  accountScope:'credential:fake',consolePartition:'aliyun'};
let calls=0;
const load=async(sessionId)=>{assert.equal(sessionId,'session-a');calls++;
  return {status:'available',choice:{...choice,secret:'not-to-copy',url:'javascript:bad'}};};
const result=await loadResourceAccountContext(index,{sessionId:'session-a',isCurrent:()=>true,load});
assert.deepEqual(result,{status:'available',choice});assert.equal(calls,1);
assert.deepEqual(index.contextFor({}),{accountScope:'credential:fake',consolePartition:'aliyun'},
  'validated configured credentials become the default scope for already-observed facts');
assert.equal(index.revision,0);assert.equal(index.references.size,0);assert.equal(index.bindings.size,0);
const closed=await loadResourceAccountContext(index,{sessionId:'session-a',isCurrent:()=>false,load});
assert.equal(closed.status,'unavailable');assert.equal(calls,1);
for(const change of ['session','source']){
  index.reset('session-a');let done,visible=true;
  const pending=loadResourceAccountContext(index,{sessionId:'session-a',isCurrent:()=>visible,
    load:()=>new Promise(resolve=>{done=resolve;})});
  if(change==='session') index.reset('session-b');else visible=false;
  done({status:'available',choice});assert.equal((await pending).status,'unavailable');
  assert.equal(index.references.size,0);
}
index.reset('session-a');
for(const offered of [{},{...choice,accountContextId:'forged'}, {...choice,accountScope:''},
  {...choice,consolePartition:'unknown'}, {...choice,accountScope:'bad\ncontext'}]){
  const result=await loadResourceAccountContext(index,{sessionId:'session-a',isCurrent:()=>true,
    load:async()=>({status:'available',choice:offered})});
  assert.equal(result.status,'unavailable');
}
assert.equal((await loadResourceAccountContext(index,{sessionId:'session-a',isCurrent:()=>true,
  load:async()=>{throw Error('private exception');}})).status,'unavailable');
console.log('passed');
""",
        encoding="utf-8",
    )
    result = subprocess.run([node, str(script)], capture_output=True, text=True, encoding="utf-8", timeout=20)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "passed"
