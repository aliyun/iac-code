import test from 'node:test';
import assert from 'node:assert/strict';
import {refreshOriginalSession} from '../../scripts/web/e2e/resource_link_workflow/browser.mjs';

function fixture(scenario) {
  const actions=[];
  const expected=scenario==='empty'?null:'ws~recorded';
  let read=0;
  const view=(session_id,patch={})=>({app_ready:true,unlocked:true,busy:false,session_id,...patch});
  const before=view(expected,scenario==='locked'?{unlocked:false}:scenario==='busy'?{busy:true}:{});
  if(scenario==='wrong-before')before.session_id='ws~unrelated';
  const loaded=view(scenario==='already-restored'?expected:scenario==='wrong-loaded'?'ws~other':null,
    scenario==='loaded-locked'?{unlocked:false}:scenario==='loaded-busy'?{busy:true}:{});
  const after=view(scenario==='late-changed'?'ws~other':expected,
    scenario==='late-locked'?{unlocked:false}:scenario==='late-busy'?{busy:true}:{});
  const page={evaluate:async()=>[before,loaded,after][read++],
    reload:async options=>{actions.push('reload');assert.equal(options.waitUntil,'domcontentloaded');
      if(scenario==='reload-failed')throw Error('reload failed');},
    locator:selector=>({waitFor:async()=>{actions.push(selector==='#iac-code-web-root'?'ready':'existing');
      if(selector!== '#iac-code-web-root'){
        assert.equal(selector,`.session-item[data-session-id="${expected}"]`);
        if(scenario==='missing')throw Error('existing session missing');
      }},count:async()=>scenario==='ambiguous'?2:1,
      click:async()=>{actions.push('select-existing');if(scenario==='click-failed')throw Error('click failed');}}),
    waitForFunction:async(_fn,key)=>{assert.equal(key,expected);actions.push('bound');
      if(scenario==='selection-failed')throw Error('selection failed');}};
  return {page,expected,actions};
}

for(const scenario of ['restore','empty','already-restored','locked','busy','wrong-before','wrong-loaded',
  'loaded-locked','loaded-busy','late-changed','late-locked','late-busy','missing','ambiguous',
  'reload-failed','click-failed','selection-failed'])
  test(`OPS original page refresh preserves existing session without any send: ${scenario}`,async()=>{
    const {page,expected,actions}=fixture(scenario);
    if(['restore','empty','already-restored'].includes(scenario)) {
      const result=await refreshOriginalSession(page,expected);
      assert.equal(result.session_id,expected);assert(result.original_page_reloaded);
      assert.equal(result.new_session_created,false);assert.equal(result.cloud_calls,0);
      assert.deepEqual(actions,scenario==='restore'?
        ['reload','ready','existing','select-existing','bound']:['reload','ready']);
    } else {
      await assert.rejects(()=>refreshOriginalSession(page,expected));
      if(['locked','busy','wrong-before'].includes(scenario))assert.deepEqual(actions,[]);
    }
    assert(!actions.some(x=>['new-session','send','goto','other-target'].includes(x)));
  });

for(const key of ['short-display-uuid','ws~bad"selector',{},'',undefined])
  test(`OPS reload rejects unsafe/incomplete binding before browser mutation: ${JSON.stringify(key)}`,async()=>{
    const {page,actions}=fixture('restore');
    await assert.rejects(()=>refreshOriginalSession(page,key),/invalid original session binding/);
    assert.deepEqual(actions,[]);
  });
