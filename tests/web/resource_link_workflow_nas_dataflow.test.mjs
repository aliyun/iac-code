import test from 'node:test';import assert from 'node:assert/strict';import fs from 'node:fs';
import {nasDataFlowConsoleUrl,nasDataFlowMetadataReady,inspectNasDataFlowMetadata}
 from '../../scripts/web/e2e/resource_link_workflow/console_nas_dataflow.mjs';
const expected={kind:'nas_dataflow_metadata',resource_type:'ALIYUN::NAS::DataFlow',identifier:'df-own',
 file_system_id:'cpfs-own',fileset_id:'fset-own',region:'cn-hangzhou',path:'/own/',
 source_storage:'oss://offline-bucket',source_tool_use_id:'offline-call'};
const url='https://nasnext4service.console.aliyun.com/cn-hangzhou/filesystem/cpfs-own/dataflow';
test('only exact signed parent, child and native domestic route',()=>{
 assert(nasDataFlowConsoleUrl(url,expected));
 for(const wrong of [url+'?x=1',url+'#x',url+'/',url.replace('cn-hangzhou','cn-beijing'),
  url.replace('cpfs-own','cpfs-other'),url.replace('https:','http:'),url.replace('aliyun.com','aliyun.com.evil')])
  assert.throws(()=>nasDataFlowConsoleUrl(wrong,expected));
 for(const patch of [{identifier:'own'},{fileset_id:'other'},{source_tool_use_id:''},{path:'relative'},
  {source_storage:'https://evil'},{kind:'other'},{resource_type:'other'}])
  assert.throws(()=>nasDataFlowConsoleUrl(url,{...expected,...patch}));
});
test('path pixels and disabled native source are independently required',()=>{
 const view={unique_row:true,unique_tooltip:true,tooltip_text:expected.path,parent:expected.file_system_id,
  parent_disabled:true,fileset_disabled:true,source_disabled:true,source_visible:true,dialog_error:false,
  path_fields:[expected.identifier,expected.fileset_id,expected.path].map(marker=>({marker,visible:true}))};
 assert(nasDataFlowMetadataReady(view,expected));
 for(const key of ['unique_row','unique_tooltip','parent_disabled','fileset_disabled','source_disabled','source_visible'])
  assert(!nasDataFlowMetadataReady({...view,[key]:false},expected));
 for(const patch of [{parent:'cpfs-other'},{dialog_error:true},{tooltip_text:'truncated...'},{path_fields:[]}])
  assert(!nasDataFlowMetadataReady({...view,...patch},expected));
});
for(const scenario of ['ok','ambiguous-row','clipped-path','editable-source','source-hidden','dialog-error',
 'ambiguous-dialog','route-changed','session-changed','capture-failed','background'])
 test(`native metadata observes and always cancels: ${scenario}`,async()=>{
  const actions=[];let opened=false,captures=0,reads=0;
  const fields=markers=>({fields:markers.map(marker=>({marker,visible:scenario!=='clipped-path'}))});
  const tip={waitFor:async()=>{},count:async()=>1,innerText:async()=>expected.path,evaluate:async(_,a)=>fields(a.markers)};
  const text={count:async()=>1,hover:async()=>actions.push('hover'),waitFor:async()=>{},
   evaluate:async(_,a)=>({fields:a.markers.map(marker=>({marker,visible:scenario!=='source-hidden'}))})};
  const row={count:async()=>scenario==='ambiguous-row'?2:1,isVisible:async()=>true,getByText:()=>text,
   evaluate:async(_,a)=>fields(a.markers),getByRole:()=>({count:async()=>1,click:async()=>{opened=true;actions.push('open');}})};
  const dialog={waitFor:async()=>{},count:async()=>scenario==='ambiguous-dialog'?2:1,isVisible:async()=>opened,
   getByText:()=>text,locator:selector=>({inputValue:async()=>expected.file_system_id,
    isDisabled:async()=>selector.includes('SourceStorage')?scenario!=='editable-source':true}),
   getByRole:(_,opts)=>opts?{count:async()=>1,click:async()=>{opened=false;actions.push('cancel');}}:
    {filter:()=>({count:async()=>scenario==='dialog-error'?1:0})}};
  const frame={url:()=>url,locator:()=>({filter:()=>row}),getByText:()=>({}),getByRole:role=>role==='tooltip'?{filter:()=>tip}:dialog,
   evaluate:async(fn,args)=>args?{text:expected.identifier,loading:false,facts_visible:true}:
    fn.toString().includes('document.location.href')?
     scenario==='route-changed'&&++reads>1?url.replace('cpfs-own','cpfs-other'):url:{painted:true},
   frameElement:async()=>({evaluate:async()=>({visible:true}),dispose:async()=>{}})};
  const surface={bringToFront:async()=>actions.push('focus'),mouse:{move:async()=>actions.push('move')},
   evaluate:async()=>scenario!=='background',screenshot:async options=>{assert.equal(options.fullPage,false);
    actions.push('pixels');if(scenario==='capture-failed'&&++captures===2)throw Error('capture failed');}};
  const run=()=>inspectNasDataFlowMetadata(surface,frame,expected,'offline.png',{mode:'iframe',beforeCapture:async()=>{
   if(scenario==='session-changed')throw Error('session changed');}});
  if(['ok','background'].includes(scenario)){
   const result=await run();assert.equal(result.identity_visible,scenario==='ok');
   assert.equal(result.dialog_cancelled,true);assert.equal(result.readonly_completed,true);
   assert.equal(result.cloud_writes,0);assert.deepEqual(result.supplemental_captures.map(c=>c.kind),['path','source']);
  }else await assert.rejects(run);
  assert.equal(opened,false);if(actions.includes('open'))assert(actions.includes('cancel'));
  assert(actions.every(a=>['hover','open','cancel','focus','move','pixels'].includes(a)));
 });
test('no form submission, cloud calls, URL construction or style edits',()=>{
 const source=fs.readFileSync(new URL('../../scripts/web/e2e/resource_link_workflow/console_nas_dataflow.mjs',import.meta.url),'utf8');
 for(const forbidden of ['.goto(','.fill(','.check(','.selectOption(','.setViewportSize(','.style.','aliyun_api'])
  assert(!source.includes(forbidden));
 assert.equal((source.match(/\.click\(/g)||[]).length,2); // Existing metadata + Cancel only.
});
