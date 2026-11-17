"""Run the actual shared frontend index in Node with fake resource facts."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

MODULE = Path(__file__).parents[2] / "src/iac_code/web/static/js/resource_index.js"


def test_resource_index_protocol_scope_alias_and_race_contracts(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is unavailable")
    script = tmp_path / "resource-index-test.mjs"
    script.write_text(
        "import assert from 'node:assert/strict';\n"
        + "const { ResourceIndex } = await import("
        + json.dumps(MODULE.as_uri())
        + ");\n"
        + r"""
const ref = (id, changes = {}) => ({
  referenceId: `ref-${id}`, provider: 'aliyun', resourceType: 'ALIYUN::RAM::Role',
  identifier: 'DeployRole', identifierKind: 'name', identifierField: 'RoleName',
  scopeKind: 'account', accountScope: 'account-one', consolePartition: 'aliyun',
  sources: [{toolUseId: 'tool-1'}], attributes: {}, aliases: [], ...changes,
});
const snapshot = (revision, items, extra = {}) => ({schemaVersion: 1, revision, items, bindings: [], ...extra});
const delta = (base, revision, items = [], extra = {}) => ({
  schemaVersion: 1, baseRevision: base, revision, upserts: items,
  removedReferenceIds: [], bindings: [], ...extra,
});
const role = {identifierKind: 'name', identifier: 'DeployRole'};
const index = new ResourceIndex();
index.reset('session-a');
index.setAccountContext({accountScope:'configured-account',consolePartition:'aliyun',accountContextId:'ignored'});
assert.deepEqual(index.contextFor({}),{accountScope:'configured-account',consolePartition:'aliyun'});
index.setAccountContext({accountScope:'configured-account',consolePartition:'invalid'});
assert.equal(index.contextFor({}).accountScope,undefined,'invalid defaults are discarded');
index.setAccountContext({accountScope:'configured-account',consolePartition:'aliyun'});
const notices = [];
index.subscribe((change) => notices.push(change));
index.subscribe(() => {throw Error('decorator failed');});
assert.equal(await index.loadSnapshot(
  snapshot(1, [ref(1), ref(2, {accountScope: 'account-two'})]), () => assert.fail()
), true);
assert.equal(index.candidates(role).length, 2);
assert.equal(index.candidates({...role, accountScope:'account-one', regionId:'cn-beijing'}).length, 1);
assert.equal(index.candidates({...role, resourceType:'ALIYUN::RAM::User'}).length, 0);
assert.equal(index.applyDelta(delta(1, 2, [ref(1, {regionId: 'cn-hangzhou', aliases: [
  {identifier: '123456', identifierKind: 'id', identifierField: 'RoleId'}
]})], {bindings: [{...role, sourceKey:'message-1:hash', referenceId:'ref-1'}]})), 'applied');
assert.equal(index.references.get('ref-1').regionId, undefined);
assert.equal(index.candidates({identifierKind: 'id', identifier: '123456'}).length, 1);
assert.equal(index.bindingFor({...role, sourceKey:'message-1:hash'}).referenceId, 'ref-1');
assert.equal(index.bindingFor({...role, sourceKey:'message-2:hash'}), null);
assert.equal(index.applyDelta(delta(0, 1, [ref(9)])), 'stale');
assert.equal(index.references.has('ref-9'), false);
assert.equal(index.applyDelta(delta(2, 3, [ref(1)])), 'applied');
assert.equal(index.candidates({identifierKind:'id', identifier:'123456'}).length, 0,
  'removed alias must leave reverse index');
assert.equal(index.applyDelta(delta(3, 4, [], {removedReferenceIds:['ref-1']})), 'applied');
assert.equal(index.bindingFor({...role, sourceKey:'message-1:hash'}), null);
assert.equal(index.applyDelta(delta(7, 8)), 'gap');
assert.equal(index.revision, 4);
assert.equal(index.needsSnapshot, true);
assert.equal(index.applyDelta(delta(4, 5)), 'gap', 'partial catch-up cannot forget a later observed revision');
assert.equal(index.needsSnapshot, true);
await index.refresh(() => snapshot(8, [ref(8)]));
assert.equal(index.revision, 8);
assert.equal(index.needsSnapshot, false);
assert.equal(index.references.has('ref-2'), false);

index.reset('paged-session');
assert.deepEqual(index.contextFor({}),{},'credential context is session-local');
let reads = 0;
await index.loadSnapshot(snapshot(1, [ref(1)], {nextCursor:'1:1'}), (cursor) => {
  reads += 1;
  if (cursor === '1:1') throw Object.assign(Error('conflict'), {status:409});
  if (cursor === null) return snapshot(2, [ref(2)], {nextCursor:'2:1'});
  assert.equal(index.references.size, 0, 'partial pages are not authoritative');
  return snapshot(2, [ref(3)]);
});
assert.equal(reads, 3);
assert.deepEqual([...index.references.keys()], ['ref-2','ref-3']);

index.reset('switch-a');
let release;
const old = index.loadSnapshot(snapshot(1, [ref(1)], {nextCursor:'1:1'}),
  () => new Promise((resolve) => {release = resolve;}));
index.reset('switch-b');
await index.loadSnapshot(snapshot(3, [ref(3)]), () => assert.fail());
release(snapshot(1, [ref(2)]));
assert.equal(await old, false);
assert.deepEqual([...index.references.keys()], ['ref-3']);

index.reset('delta-during-page');
let finishPage;
let pageCalls = 0;
const loading = index.loadSnapshot(snapshot(1, [ref(1)], {nextCursor:'1:1'}), (cursor) => {
  pageCalls += 1;
  if (cursor) return new Promise((resolve) => {finishPage = resolve;});
  return snapshot(3, [ref(3)]);
});
assert.equal(index.applyDelta(delta(2, 3)), 'gap');
finishPage(snapshot(1, [ref(2)]));
assert.equal(await loading, true);
assert.equal(pageCalls, 2);
assert.deepEqual([...index.references.keys()], ['ref-3']);

index.reset('failed-fetch');
assert.equal(await index.loadSnapshot(null, () => {throw Error('offline');}), false);
assert.equal(index.needsSnapshot, true);
assert.equal(index.references.size, 0);
assert.equal(index.applyDelta(delta(0, 1, [{...ref(1), consoleUrl:'javascript:bad'}])), 'applied');
assert.equal(index.references.get('ref-1').consoleUrl, undefined);
assert.ok(notices.some((notice) => notice.changed.has(JSON.stringify(['id','123456']))));

const namespaced = new ResourceIndex();
await namespaced.loadSnapshot(snapshot(1, ['a','b'].map((parent, i) => ref(i, {
  resourceType:'ALIYUN::FC::Function', scopeKind:'regional', regionId:'cn-hangzhou',
  attributes:{ServiceName: parent}
}))), () => assert.fail());
assert.equal(namespaced.candidates({...role, attributes:{ServiceName:'a'}}).length, 1);
assert.equal(namespaced.candidates({...role, regionId:'cn-beijing'}).length, 0);
const regionalWitness = (id, accountScope, regionId, turnId) => ref(id, {
  resourceType:'ALIYUN::ECS::Instance', identifier:`i-${id}`, identifierField:'InstanceId',
  identifierKind:'id', scopeKind:'regional', accountScope, consolePartition:'aliyun', regionId,
  sources:[{turnId}], aliases:[],
});
const observedIndex = new ResourceIndex();
await observedIndex.loadSnapshot(snapshot(1, [
  regionalWitness('hz-a','account-a','cn-hangzhou','turn-context'),
  regionalWitness('sh-b','account-b','cn-shanghai','turn-context'),
]), () => assert.fail());
const hangzhouOccurrence = {turnId:'turn-context',regionId:'cn-hangzhou',scopeKind:'regional'};
assert.deepEqual(observedIndex.observedContextFor(hangzhouOccurrence),
  {accountScope:'account-a',consolePartition:'aliyun',regionId:'cn-hangzhou'},
  'the occurrence region selects its same-turn observed account context');
assert.deepEqual(observedIndex.contextFor(hangzhouOccurrence),observedIndex.observedContextFor(hangzhouOccurrence));
observedIndex.reset('same-region-multiple-accounts');
await observedIndex.loadSnapshot(snapshot(1, [
  regionalWitness('hz-a','account-a','cn-hangzhou','turn-context'),
  regionalWitness('hz-b','account-b','cn-hangzhou','turn-context'),
]), () => assert.fail());
assert.deepEqual(observedIndex.observedContextFor(hangzhouOccurrence),{},
  'two accounts observed in the same turn and region remain ambiguous');
assert.deepEqual(observedIndex.contextFor(hangzhouOccurrence),{},
  'configured context must not override conflicting same-turn observations');
observedIndex.reset('no-observed-context');
observedIndex.setAccountContext({accountScope:'configured-account',consolePartition:'aliyun'});
await observedIndex.loadSnapshot(snapshot(1, [
  regionalWitness('old','account-a','cn-hangzhou','turn-old'),
]), () => assert.fail());
assert.deepEqual(observedIndex.observedContextFor(hangzhouOccurrence),{},
  'session credentials are not source-observed facts');
assert.deepEqual(observedIndex.contextFor(hangzhouOccurrence),
  {accountScope:'configured-account',consolePartition:'aliyun'},
  'ordinary context completion may still use the configured session account when no observation exists');
index.reset('saved-context');
const partial = ref('partial',{resourceType:'ALIYUN::FC::Function',scopeKind:'regional'});
const binding = {...role,sourceKey:'current-source',referenceId:partial.referenceId,
  source:{messageId:'m1',sourcePath:'/text'},context:{regionId:'cn-hangzhou',attributes:{ServiceName:'chosen-parent'}}};
await index.loadSnapshot(snapshot(1,[partial],{bindings:[binding]}),()=>assert.fail());
const occurrence = {...role,sourceKey:'current-source'};
assert.equal(index.bindingFor(occurrence).regionId,'cn-hangzhou');
assert.equal(index.bindingFor(occurrence).attributes.ServiceName,'chosen-parent');
assert.equal(index.references.get(partial.referenceId).regionId,undefined,'a choice must not rewrite observed facts');
assert.deepEqual(index.references.get(partial.referenceId).attributes,{});
assert.equal(index.hasSourceBindings({messageId:'m1',sourcePath:'/text'}),true);
assert.equal(index.hasSourceBindings({messageId:'m2',sourcePath:'/text'}),false);
index.applyDelta(delta(1,2,[{...partial,regionId:'cn-beijing'}]));
assert.equal(index.bindingFor(occurrence),null,'an observed scope conflict must invalidate saved context');
index.applyDelta(delta(2,3,[],{removedBindings:[binding]}));
assert.equal(index.hasSourceBindings({messageId:'m1',sourcePath:'/text'}),false);
index.reset('declarations');
const declared=ref('declared',{evidence:'user_context',sources:[{sourceId:'web-choice:fake-source'}]});
const declaredBinding={...role,sourceKey:'declared-source',referenceId:declared.referenceId};
await index.loadSnapshot(snapshot(1,[declared],{bindings:[declaredBinding]}),()=>assert.fail());
assert.equal(index.candidates(role).length,0,'a user declaration must not act as a global observation');
assert.equal(index.candidates({...role,sourceKey:'declared-source'}).length,1);
assert.equal(index.bindingFor({...role,sourceKey:'declared-source'}).referenceId,declared.referenceId);
index.applyDelta(delta(1,2,[{...declared,evidence:'api_response',sources:[...declared.sources,{toolUseId:'verified-tool'}]}]));
assert.equal(index.candidates(role).length,1,'independent observations may subsequently backfill other occurrences');
console.log(JSON.stringify({passed: true}));
""",
        encoding="utf-8",
    )
    result = subprocess.run([node, str(script)], capture_output=True, text=True, encoding="utf-8", timeout=20)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"passed": True}
