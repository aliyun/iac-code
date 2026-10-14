"""Offline contracts for packet preparation, never real browser/cloud/LLM."""

import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    "case",
    [
        "ready",
        "streaming",
        "wrong_session",
        "missing_button",
        "missing_target",
        "closed",
        "invisible",
        "non_domestic",
        "false_handle",
        "target_gate",
    ],
)
def test_ui_readiness(case):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is unavailable")
    module = Path(__file__).parents[2] / "scripts/web/e2e/resource_link_ui_readiness.cjs"
    javascript = r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const kind=process.argv[2];
const state={key:'ws~own',button:true,stopping:kind==='streaming',closed:kind==='closed',
 visible:kind!=='invisible',target:kind==='missing_target'?'other':'real',
 href:kind==='non_domestic'?'https://example.invalid/':'https://polardb.console.aliyun.com/'};
if(kind==='wrong_session')state.key='ws~other';
if(kind==='missing_button')state.button=false;
const document={visibilityState:'visible',
 querySelector(sel){return sel.startsWith('.session-item')?{getAttribute:()=>state.key}:
 (state.button?{classList:{contains:()=>state.stopping}}:null);},
 querySelectorAll(){return [{getAttribute:()=>state.target,href:state.href,closest:()=>state.closed?{}:null,
 getClientRects:()=>state.visible?[{}]:[]}];}};
const module={exports:{}};
vm.runInNewContext(fs.readFileSync(process.argv[1],'utf8'),{module,document,
 location:{origin:'http://127.0.0.1:8767'}});
const {completedOwnReply,readinessSnapshot,waitForOwnTargets,ownReplyEnded,waitForOwnReplyEnd}=module.exports;
const args={key:'ws~own',targets:['real']};
assert.equal(completedOwnReply.constructor.name,'Function');
assert.equal(completedOwnReply(args),['ready','false_handle','target_gate'].includes(kind));
const snapshot=readinessSnapshot(args);
assert.equal(snapshot.scope,'readiness_only_not_click_evidence');
assert.equal(snapshot.origin,'http://127.0.0.1:8767');
assert(!('actualUiClick' in snapshot));
assert(!('href' in snapshot));
assert.equal(snapshot.targets[0].openVisibleCount,
 ['missing_target','closed','invisible','non_domestic'].includes(kind)?0:1);
let disposed=0,calls=0;
const page={async waitForFunction(predicate,arg,options){
 calls++;assert.equal(predicate.constructor.name,'Function');
 assert.equal(options.polling,100);assert.equal(options.timeout,5000);
 assert.equal(arg.key,'ws~own');assert.deepEqual(arg.targets,['real']);
 const value=predicate(arg);
 assert.equal(typeof value,'boolean');
 if(!value)throw Error('timeout');
 return {jsonValue:async()=>kind==='false_handle'?false:value,dispose:async()=>disposed++};}};
(async()=>{
 if(kind==='false_handle')await assert.rejects(()=>waitForOwnTargets(page,'ws~own',['real'],5000),/non-true/);
 else if(['wrong_session','missing_target','non_domestic'].includes(kind))
  await assert.rejects(()=>waitForOwnTargets(page,'ws~own',['real'],5000),/timeout/);
 else await waitForOwnTargets(page,'ws~own',['real'],5000);
 assert.equal(calls,1);assert.equal(disposed,['wrong_session','missing_target','non_domestic'].includes(kind)?0:1);
 const endExpected=!['streaming','wrong_session','missing_button','missing_target','non_domestic'].includes(kind);
 assert.equal(ownReplyEnded(args),endExpected);
 if(kind==='false_handle')await assert.rejects(()=>waitForOwnReplyEnd(page,'ws~own',['real'],5000),/non-true/);
 else if(!endExpected)await assert.rejects(()=>waitForOwnReplyEnd(page,'ws~own',['real'],5000),/timeout/);
 else await waitForOwnReplyEnd(page,'ws~own',['real'],5000);
 console.log('passed');
})().catch(e=>{console.error(e);process.exitCode=1});
"""
    result = subprocess.run(
        [node, "-e", javascript, str(module), case],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "passed"


@pytest.mark.parametrize("case", ["partial", "all", "none", "wrong_session", "streaming", "false_handle", "unstable"])
def test_missing_contract_does_not_suppress_independent_ready_subset(case):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is unavailable")
    module = Path(__file__).parents[2] / "scripts/web/e2e/resource_link_ui_readiness.cjs"
    script = r"""
const fs=require('fs'),vm=require('vm'),assert=require('node:assert/strict');
const kind=process.argv[2];let waits=0,disposed=0;
const nodes=(kind==='none'?[]:kind==='all'?['one','two']:['one']).map(target=>({
 getAttribute:()=>target,href:'https://ecs.console.aliyun.com/',closest:()=>null,getClientRects:()=>[{}]}));
const document={visibilityState:'visible',querySelector:s=>s.startsWith('.session-item')?
 {getAttribute:()=>kind==='wrong_session'?'ws~other':'ws~own'}:
 {classList:{contains:()=>kind==='streaming'||kind==='unstable'&&waits>=2}},querySelectorAll:()=>nodes};
const module={exports:{}};
vm.runInNewContext(fs.readFileSync(process.argv[1],'utf8'),{module,document,Date,Set,
 location:{origin:'http://127.0.0.1:8767'}});
const {prepareOwnTargetSubset}=module.exports;
const page={waitForTimeout:async()=>{waits++;},evaluate:async(fn,args)=>fn(args),
 locator:()=>{throw Error('already visible; no clicks needed');},
 waitForFunction:async(fn,args,options)=>{
 assert.equal(fn.constructor.name,'Function');assert.equal(options.polling,100);
 const value=fn(args);assert.equal(typeof value,'boolean');if(!value)throw Error('timeout');
 return {jsonValue:async()=>kind==='false_handle'?false:value,dispose:async()=>{disposed++;}};}};
(async()=>{
 if(!['partial','all'].includes(kind)) {
  await assert.rejects(()=>prepareOwnTargetSubset(page,'ws~own',['one','two'],Date.now()+5000));
 }else{
  const result=await prepareOwnTargetSubset(page,'ws~own',['one','two'],Date.now()+5000);
  assert.equal(result.readyTargets.join(','),kind==='all'?'one,two':'one');
  assert.equal(result.missingTargets.length,kind==='all'?0:1);
  assert.equal(result.snapshot.exactSessionActive,true);assert.equal(result.snapshot.stopping,false);
  assert(result.snapshot.targets.every(t=>t.openVisibleCount>0));
  assert(!('actualUiClick' in result));assert.equal(disposed,2);
 }
})().catch(e=>{console.error(e);process.exitCode=1});
"""
    result = subprocess.run(
        [node, "-e", script, str(module), case],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("case", ["nested", "open_outer", "ambiguous", "none"])
def test_unfold_chooses_unique_outer_closed_ancestor(case):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is unavailable")
    module = Path(__file__).parents[2] / "scripts/web/e2e/resource_link_ui_readiness.cjs"
    result = subprocess.run(
        [
            node,
            "-e",
            """
const {outermostClosedDetailIndex}=require(process.argv[1]);
const kind=process.argv[2],inner={contains:()=>false},outer={contains:n=>n===inner};
const nodes=kind==='nested'?[inner,outer]:kind==='open_outer'?[inner]:
 kind==='ambiguous'?[inner,{contains:()=>false}]:[];
if(outermostClosedDetailIndex(nodes)!==({nested:1,open_outer:0,ambiguous:-1,none:-1}[kind]))process.exit(1);
""",
            str(module),
            case,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("case", ["visible_duplicate", "duplicate_contract_targets", "ambiguous_hidden", "unfolded"])
def test_unfold_uses_existing_exact_visible_target_before_touching_hidden_duplicate(case):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is unavailable")
    module = Path(__file__).parents[2] / "scripts/web/e2e/resource_link_ui_readiness.cjs"
    script = r"""
const assert=require('node:assert/strict');
const {unfoldOwnVisibleTargets,completedOwnReply}=require(process.argv[1]);
const kind=process.argv[2];let calls=0,clicks=0,visible=kind==='visible_duplicate'||kind==='duplicate_contract_targets';
const summary={isVisible:async()=>true,click:async()=>{clicks++;visible=true;}};
const outer={contains:()=>false};
const closed={count:async()=>visible?0:1,evaluateAll:async fn=>kind==='ambiguous_hidden'?-1:fn([outer]),
 nth:()=>({locator:()=>summary})};
const anchor={locator:()=>closed};
const page={evaluate:async(fn,args)=>{
 assert.equal(fn,completedOwnReply);assert.equal(args.key,'ws~own');assert.deepEqual(args.targets,['real']);
 calls++;return visible;},locator:()=>{
 if(visible)throw Error('must not touch hidden duplicate when exact own target already visible');
 return {count:async()=>1,nth:()=>anchor};}};
(async()=>{
 const targets=kind==='duplicate_contract_targets'?['real','real']:['real'];
 if(kind==='ambiguous_hidden')
  await assert.rejects(()=>unfoldOwnVisibleTargets(page,'ws~own',targets,Date.now()+5000),/ambiguous/);
 else await unfoldOwnVisibleTargets(page,'ws~own',targets,Date.now()+5000);
 assert.equal(clicks,kind==='unfolded'?1:0);
 assert.equal(calls,kind==='unfolded'?2:1);
})().catch(e=>{console.error(e);process.exitCode=1});
"""
    result = subprocess.run(
        [node, "-e", script, str(module), case],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
