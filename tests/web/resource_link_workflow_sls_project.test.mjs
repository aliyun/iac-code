import test from 'node:test';import assert from 'node:assert/strict';import fs from 'node:fs';
import {slsProjectConsoleUrl,slsProjectMetadataReady,inspectSlsProjectMetadata}
  from '../../scripts/web/e2e/resource_link_workflow/console_sls_project.mjs';
const expected={kind:'sls_project_metadata',resource_type:'alicloud_log_project',identifier:'offline-project',
  region:'cn-hangzhou',account:'1234567890123456',description:'offline-description',source_tool_use_id:'offline-call'};
const url='https://sls4service.console.aliyun.com/lognext/project/offline-project/overview';
test('exact native SLS project route, source, account and reviewed region required',()=>{
  assert(slsProjectConsoleUrl(url,expected));assert(slsProjectConsoleUrl(url.replace('sls4service','sls'),expected));
  for(const wrong of [url+'?other=1',url+'#other',url+'/',url.replace('https:','http:'),
    url.replace('offline-project','other-project'),url.replace('aliyun.com','aliyun.com.example'),
    url.replace('sls4service','evil'),url.replace('https://','https://user:pass@'),url.replace('.com/','.com:444/')])
    assert.throws(()=>slsProjectConsoleUrl(wrong,expected),/unbound/);
  for(const patch of [{kind:'other'},{resource_type:'other'},{identifier:'bad/name'},{region:'cn-beijing'},
    {account:'wrong'},{source_tool_use_id:''},{description:''},{description:expected.identifier}])
    assert.throws(()=>slsProjectConsoleUrl(url,{...expected,...patch}),/invalid/);
});
test('only own complete visible native fields qualify, never DOM text alone',()=>{
  const view={unique_title:true,unique_tooltip:true,fields:[expected.identifier,expected.description,'华东1（杭州）']
    .map(marker=>({marker,visible:true}))};
  assert(slsProjectMetadataReady(view,expected));
  for(const patch of [{unique_title:false},{unique_tooltip:false},{fields:[]},
    ...view.fields.map(f=>({fields:view.fields.map(x=>({...x,visible:x.marker!==f.marker}))}))])
    assert(!slsProjectMetadataReady({...view,...patch},expected));
});
for(const scenario of ['open','iframe','ambiguous-title','hidden-title','ambiguous-tooltip','clipped','wrong-tooltip',
  'session-changed','route-changed','detached','background','capture-failed','hover-failed','late-title-changed',
  'oopif-empty-cache','hidden-tooltip','embedding-clipped'])
  test(`source-bound hover only, original landing unchanged: ${scenario}`,async()=>{
    const actions=[];let reads=0,titleReads=0;
    const title={count:async()=>scenario==='ambiguous-title'||scenario==='late-title-changed'&&++titleReads>1?2:1,
      isVisible:async()=>scenario!=='hidden-title',hover:async()=>{
        actions.push('hover');if(scenario==='hover-failed')throw Error('hover failed');}};
    const tip={waitFor:async()=>{},count:async()=>scenario==='ambiguous-tooltip'?2:1,
      isVisible:async()=>scenario!=='hidden-tooltip',evaluate:async(_,args)=>({fields:args.markers.map(marker=>({marker,
        visible:scenario!=='clipped'&&scenario!=='wrong-tooltip'}))})};
    const frame={isDetached:()=>scenario==='detached',locator:()=>({getByText:()=>title}),
      getByRole:()=>({filter:()=>tip}),url:()=>scenario==='oopif-empty-cache'?'':url,
      evaluate:async(fn,args)=>args?{text:expected.identifier+' '+expected.description,loading:false,facts_visible:true}:
        fn.toString().includes('document.location.href')?scenario==='route-changed'&&++reads>1?
          url.replace('offline-project','other-project'):url:{painted:true},
      frameElement:async()=>({evaluate:async()=>({visible:scenario!=='embedding-clipped'}),dispose:async()=>{}})};
    const surface={bringToFront:async()=>actions.push('focus'),mouse:{move:async()=>actions.push('restore')},
      evaluate:async()=>scenario!=='background',screenshot:async options=>{
        assert.equal(options.fullPage,false);actions.push('pixels');if(scenario==='capture-failed')throw Error('capture failed');}};
    const options={mode:['iframe','embedding-clipped'].includes(scenario)?'iframe':'open',beforeCapture:async()=>{
      if(scenario==='session-changed')throw Error('session changed');}};
    const accepted=['open','iframe','background','oopif-empty-cache','embedding-clipped'];
    if(!accepted.includes(scenario))await assert.rejects(()=>inspectSlsProjectMetadata(surface,frame,expected,'offline.png',options));
    else{
      const result=await inspectSlsProjectMetadata(surface,frame,expected,'offline.png',options);
      assert.equal(result.identity_visible,!['background','embedding-clipped'].includes(scenario));
      assert.equal(result.readonly_completed,true);assert.equal(result.original_landing_unchanged,true);
      assert.equal(result.cloud_writes,0);assert(!('pass' in result));
    }
    assert(actions.every(action=>['hover','focus','restore','pixels'].includes(action)));
    if(actions.includes('hover'))assert.equal(actions.at(-1),'restore');
  });
test('explicit dispatch and no navigation, edits, writes or viewport changes',()=>{
  const source=fs.readFileSync(new URL('../../scripts/web/e2e/resource_link_workflow/console_sls_project.mjs',import.meta.url),'utf8');
  for(const forbidden of ['.goto(','.click(','.fill(','.setViewportSize(','.style.','aliyun_api'])assert(!source.includes(forbidden));
  const browser=fs.readFileSync(new URL('../../scripts/web/e2e/resource_link_workflow/browser.mjs',import.meta.url),'utf8');
  assert(browser.includes("(await import('./console_sls_project.mjs')).inspectSlsProjectMetadata"));
});
