"""The reply gate waits for the selected session and fails closed without clicks."""

import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    "case",
    [
        "completed",
        "streaming_then_completed",
        "wrong_session",
        "missing_button",
        "changed_during_stability",
        "timeout",
        "evaluation_error",
        "false_handle",
    ],
)
def test_session_bound_reply_gate(case):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is unavailable")
    script = Path(__file__).parents[2] / "scripts/web/e2e/resource_link_chrome_batch.mjs"
    javascript = r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const source=fs.readFileSync(process.argv[1],'utf8'),kind=process.argv[2];
const text=source.slice(source.indexOf('async function waitForWebReplyCompleted'),
 source.indexOf('async function visibleLink'));
const state={key:'ws~own',button:true,stopping:kind==='streaming_then_completed'};
if(kind==='wrong_session')state.key='ws~other';
if(kind==='missing_button')state.button=false;
const document={querySelector(sel){return sel.startsWith('.session-item')?{getAttribute:()=>state.key}:
 (state.button?{classList:{contains:()=>state.stopping}}:null);}};
let now=0,calls=0,disposed=0,stabilityChecks=0;
const fn=vm.runInNewContext(text+';waitForWebReplyCompleted',{document,Date:{now:()=>now}});
const timeout=()=>{throw Object.assign(new Error('timeout'),{name:'TimeoutError'});};
const page={async waitForFunction(predicate,arg,options){
 calls++;assert.equal(arg.sessionId,'ws~own');assert(options.timeout>0&&options.timeout<=15000);
 assert.equal(predicate.constructor.name,'Function','Playwright polling predicates must be synchronous');
 if(kind==='timeout')timeout();
 if(kind==='evaluation_error')throw new Error('unreadable DOM');
 if(kind==='streaming_then_completed'){assert.equal(predicate(arg),false);now+=100;state.stopping=false;}
 const value=predicate(arg);assert.equal(typeof value,'boolean');
 if(!value){now+=options.timeout;timeout();}
 return {jsonValue:async()=>kind==='false_handle'?false:value,dispose:async()=>{disposed++;}};
},async waitForTimeout(ms){assert.equal(ms,1200);now+=ms;
 if(kind==='changed_during_stability')state.stopping=true;
},async evaluate(predicate,arg){stabilityChecks++;return predicate(arg);
}};
(async()=>{
 if(kind==='evaluation_error')await assert.rejects(()=>fn(page,'ws~own',55000),/could not be observed/);
 else assert.equal(await fn(page,'ws~own',55000),['completed','streaming_then_completed'].includes(kind));
 assert(calls>=1);assert.equal(disposed,['timeout','evaluation_error','wrong_session','missing_button'].includes(kind)?0:1);
 if(['completed','streaming_then_completed','changed_during_stability'].includes(kind))assert.equal(stabilityChecks,1);
 const main=source.slice(source.indexOf('async function main()'));
 assert(main.indexOf('const sessionId = await activeSessionId')<main.indexOf('waitForWebReplyCompleted'));
 assert(main.indexOf('if (!replyCompleted) throw new Error')<main.indexOf('await resourceReferences'));
 console.log('passed');
})().catch(e=>{console.error(e);process.exitCode=1});
"""
    result = subprocess.run(
        [node, "-e", javascript, str(script), case],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "passed"


def test_reply_state_trace_stays_outside_click_artifacts():
    script = Path(__file__).parents[2] / "scripts/web/e2e/resource_link_chrome_batch.mjs"
    source = script.read_text(encoding="utf-8")
    snapshot = source[
        source.index("async function webReplyReadinessSnapshot") : source.index("async function visibleLink")
    ]
    assert 'scope: "readiness_only_not_click_evidence"' in snapshot
    assert "exactSessionActive:" in snapshot
    assert "composerButtonCount:" in snapshot
    assert "stopping:" in snapshot
    assert "location.origin" in snapshot
    assert "location.href" not in snapshot
    assert "\n      sessionId:" not in snapshot
    assert "\n      sessionId," not in snapshot
    assert "actualUiClick" not in snapshot
    assert 'path.resolve(args.output + ".reply-readiness.json")' in source
    main = source[source.index("async function main()") :]
    assert main.index("before: readinessBefore") < main.index("if (!replyCompleted) throw new Error")
