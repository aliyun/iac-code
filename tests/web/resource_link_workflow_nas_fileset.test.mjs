import test from 'node:test';import assert from 'node:assert/strict';import fs from 'node:fs';
import {nasFilesetConsoleUrl,nasFilesetDescriptionReady,inspectNasFilesetDescription}
  from '../../scripts/web/e2e/resource_link_workflow/console_nas_fileset.mjs';
const expected={kind:'nas_fileset_description',resource_type:'ALIYUN::NAS::Fileset',identifier:'fset-child',
  file_system_id:'cpfs-parent',region:'cn-hangzhou',description:'offline-description',path:'/offline/',
  source_tool_use_id:'offline-call'};
const url='https://nasnext4service.console.aliyun.com/cn-hangzhou/filesystem/cpfs-parent/fileset';
test('only own exact domestic Fileset route and bound fields are eligible',()=>{
  assert(nasFilesetConsoleUrl(url,expected));
  assert(nasFilesetConsoleUrl(url.replace('nasnext4service','nasnext'),expected));
  for(const wrong of [url+'?other=1',url+'#other',url+'/',url.replace('https:','http:'),
    url.replace('cn-hangzhou','cn-beijing'),url.replace('cpfs-parent','cpfs-other'),
    url.replace('aliyun.com','aliyun.com.example'),url.replace('nasnext4service','evil'),
    url.replace('console.aliyun.com','console.aliyun.com:444')])
    assert.throws(()=>nasFilesetConsoleUrl(wrong,expected),/unbound/);
  for(const patch of [{identifier:'other'},{file_system_id:'other'},{resource_type:'other'},
    {kind:'other'},{source_tool_use_id:''},{description:''},{path:'relative'},{region:'global'}])
    assert.throws(()=>nasFilesetConsoleUrl(url,{...expected,...patch}),/invalid/);
});
test('unique original row and actual fully painted tooltip are mandatory',()=>{
  const view={unique_row:true,unique_tooltip:true,tooltip_text:expected.description,
    fields:[expected.identifier,expected.path,expected.description].map(marker=>({marker,visible:true}))};
  assert(nasFilesetDescriptionReady(view,expected));
  for(const patch of [{unique_row:false},{unique_tooltip:false},{tooltip_text:'wrong'},{fields:[]},
    ...view.fields.map(f=>({fields:view.fields.map(x=>({...x,visible:x.marker!==f.marker}))}))])
    assert(!nasFilesetDescriptionReady({...view,...patch},expected));
});
for(const scenario of ['open','iframe','ambiguous-row','ambiguous-label','ambiguous-tooltip','clipped',
  'session-changed','route-changed','detached','background','capture-failed'])
  test(`native hover capture preserves landing and fields: ${scenario}`,async()=>{
    const actions=[];let reads=0;
    const actual=()=>scenario==='route-changed'&&++reads>1?url.replace('cpfs-parent','cpfs-other'):url;
    const fields=markers=>({fields:markers.map(marker=>({marker,visible:scenario!=='clipped'}))});
    const tip={waitFor:async()=>{},count:async()=>scenario==='ambiguous-tooltip'?2:1,
      innerText:async()=>expected.description,isVisible:async()=>true,evaluate:async(_,args)=>fields(args.markers)};
    const label={count:async()=>scenario==='ambiguous-label'?2:1,hover:async()=>actions.push('hover')};
    const row={count:async()=>scenario==='ambiguous-row'?2:1,isVisible:async()=>true,
      getByText:()=>label,evaluate:async(_,args)=>fields(args.markers)};
    const frame={url:()=>url,isDetached:()=>scenario==='detached',locator:()=>({filter:()=>row}),getByText:()=>({}),
      getByRole:()=>({filter:()=>tip}),evaluate:async(fn,args)=>args?
        {text:expected.identifier+' '+expected.path+' '+expected.description,loading:false,facts_visible:true}:
        fn.toString().includes('document.location.href')?actual():{painted:true},
      frameElement:async()=>({evaluate:async()=>({visible:true}),dispose:async()=>{}})};
    const surface={bringToFront:async()=>actions.push('focus'),mouse:{move:async()=>actions.push('restore')},
      evaluate:async()=>scenario!=='background',screenshot:async options=>{
        assert.equal(options.fullPage,false);actions.push('pixels');
        if(scenario==='capture-failed')throw Error('capture failed');}};
    const options={mode:scenario==='iframe'?'iframe':'open',beforeCapture:async()=>{
      if(scenario==='session-changed')throw Error('session changed');}};
    const rejected=['ambiguous-row','ambiguous-label','ambiguous-tooltip','clipped','session-changed',
      'route-changed','detached','capture-failed'];
    if(rejected.includes(scenario))await assert.rejects(()=>inspectNasFilesetDescription(surface,frame,expected,'offline.png',options));
    else{
      const result=await inspectNasFilesetDescription(surface,frame,expected,'offline.png',options);
      assert.equal(result.identity_visible,scenario!=='background');
      assert.equal(result.readonly_completed,true);assert.equal(result.original_landing_unchanged,true);
      assert.equal(result.cloud_writes,0);assert(!('pass' in result));
    }
    assert(actions.every(action=>['hover','focus','restore','pixels'].includes(action)));
  });
test('no edits, arbitrary navigation, resizing or cloud calls',()=>{
  const source=fs.readFileSync(new URL('../../scripts/web/e2e/resource_link_workflow/console_nas_fileset.mjs',import.meta.url),'utf8');
  for(const forbidden of ['.goto(','.fill(','.click(','.setViewportSize(','.style.','aliyun_api'])assert(!source.includes(forbidden));
  const browser=fs.readFileSync(new URL('../../scripts/web/e2e/resource_link_workflow/browser.mjs',import.meta.url),'utf8');
  assert(browser.includes("(await import('./console_nas_fileset.mjs')).inspectNasFilesetDescription"));
});
