import test from 'node:test';import assert from 'node:assert/strict';import fs from 'node:fs';
import {nasProtocolServiceConsoleUrl,nasProtocolServiceIdentityReady,inspectNasProtocolServiceIdentity}
  from '../../scripts/web/e2e/resource_link_workflow/console_nas_protocol_service.mjs';
import {routerInterfaceRowView} from '../../scripts/web/e2e/resource_link_workflow/console_router_interface.mjs';
const expected={kind:'nas_protocol_service_identity',resource_type:'ALIYUN::NAS::ProtocolService',identifier:'ptc-child',
  file_system_id:'cpfs-parent',region:'cn-beijing',description:'offline-description',source_tool_use_id:'offline-call'};
const url='https://nasnext4service.console.aliyun.com/cn-beijing/filesystem/cpfs-parent/protocolservice';
test('only exact own domestic ProtocolService route and signed fields eligible',()=>{
  assert(nasProtocolServiceConsoleUrl(url,expected));
  assert(nasProtocolServiceConsoleUrl(url.replace('nasnext4service','nasnext'),expected));
  for(const wrong of [url+'?other=1',url+'#other',url+'/',url.replace('https:','http:'),
    url.replace('cn-beijing','cn-hangzhou'),url.replace('cpfs-parent','cpfs-other'),
    url.replace('aliyun.com','aliyun.com.example'),url.replace('nasnext4service','evil'),
    url.replace('console.aliyun.com','console.aliyun.com:444'),url.replace('https://','https://user:pass@')])
    assert.throws(()=>nasProtocolServiceConsoleUrl(wrong,expected),/unbound/);
  for(const patch of [{identifier:'other'},{file_system_id:'other'},{resource_type:'other'},
    {kind:'other'},{source_tool_use_id:''},{description:''},{description:expected.identifier},{region:'global'}])
    assert.throws(()=>nasProtocolServiceConsoleUrl(url,{...expected,...patch}),/invalid/);
});
test('unique own row, exact full ID tooltip and fully painted description mandatory',()=>{
  const view={unique_row:true,unique_tooltip:true,tooltip_text:expected.identifier,
    fields:[expected.identifier,expected.description].map(marker=>({marker,visible:true}))};
  assert(nasProtocolServiceIdentityReady(view,expected));
  for(const patch of [{unique_row:false},{unique_tooltip:false},{tooltip_text:'ptc-chi...'},{fields:[]},
    ...view.fields.map(f=>({fields:view.fields.map(x=>({...x,visible:x.marker!==f.marker}))}))])
    assert(!nasProtocolServiceIdentityReady({...view,...patch},expected));
});
for(const scenario of ['open','iframe','ambiguous-row','ambiguous-label','ambiguous-tooltip','clipped',
  'session-changed','route-changed','detached','background','capture-failed','hover-failed','late-row-changed',
  'oopif-empty-cache','wrong-tooltip','hidden-tooltip'])
  test(`native ID hover preserves original landing: ${scenario}`,async()=>{
    const actions=[];let reads=0,rowReads=0;
    const actual=()=>scenario==='route-changed'&&++reads>1?url.replace('cpfs-parent','cpfs-other'):url;
    const fields=markers=>({fields:markers.map(marker=>({marker,visible:scenario!=='clipped'}))});
    const tip={waitFor:async()=>{},count:async()=>scenario==='ambiguous-tooltip'?2:1,
      innerText:async()=>scenario==='wrong-tooltip'?'ptc-other':expected.identifier,
      isVisible:async()=>scenario!=='hidden-tooltip',evaluate:async(_,args)=>fields(args.markers)};
    const label={count:async()=>scenario==='ambiguous-label'?2:1,hover:async()=>{
      actions.push('hover');if(scenario==='hover-failed')throw Error('hover failed');}};
    const row={count:async()=>scenario==='ambiguous-row'||scenario==='late-row-changed'&&++rowReads>1?2:1,
      isVisible:async()=>true,getByText:()=>label,evaluate:async(_,args)=>fields(args.markers)};
    const frame={url:()=>scenario==='oopif-empty-cache'?'':url,isDetached:()=>scenario==='detached',
      locator:()=>({filter:()=>row}),getByText:()=>({}),getByRole:()=>({filter:()=>tip}),evaluate:async(fn,args)=>args?
        {text:expected.identifier+' '+expected.description,loading:false,facts_visible:true}:
        fn.toString().includes('document.location.href')?actual():{painted:true},
      frameElement:async()=>({evaluate:async()=>({visible:true}),dispose:async()=>{}})};
    const surface={bringToFront:async()=>actions.push('focus'),mouse:{move:async()=>actions.push('restore')},
      evaluate:async()=>scenario!=='background',screenshot:async options=>{
        assert.equal(options.fullPage,false);actions.push('pixels');
        if(scenario==='capture-failed')throw Error('capture failed');}};
    const options={mode:scenario==='iframe'?'iframe':'open',beforeCapture:async()=>{
      if(scenario==='session-changed')throw Error('session changed');}};
    const rejected=['ambiguous-row','ambiguous-label','ambiguous-tooltip','clipped','session-changed',
      'route-changed','detached','capture-failed','hover-failed','late-row-changed','wrong-tooltip','hidden-tooltip'];
    if(rejected.includes(scenario))await assert.rejects(()=>inspectNasProtocolServiceIdentity(surface,frame,expected,'offline.png',options));
    else{
      const result=await inspectNasProtocolServiceIdentity(surface,frame,expected,'offline.png',options);
      assert.equal(result.identity_visible,scenario!=='background');
      assert.equal(result.readonly_completed,true);assert.equal(result.original_landing_unchanged,true);
      assert.equal(result.cloud_writes,0);assert(!('pass' in result));
    }
    assert(actions.every(action=>['hover','focus','restore','pixels'].includes(action)));
    if(actions.includes('hover'))assert.equal(actions.at(-1),'restore');
  });
test('actual text ranges reject native ellipsis, occlusion and faded text',()=>{
  const rect=right=>({left:10,right,top:10,bottom:30,width:right-10,height:20});
  const replacements={innerWidth:200,innerHeight:100,NodeFilter:{SHOW_TEXT:4}};
  const originals=new Map(['innerWidth','innerHeight','NodeFilter','getComputedStyle','document'].map(key=>
    [key,Object.getOwnPropertyDescriptor(globalThis,key)]));
  try{
    for(const [key,value] of Object.entries(replacements))Object.defineProperty(globalThis,key,{value,configurable:true});
    for(const mode of ['full','ellipsis','occluded','faded']){
      const parent={parentElement:null,getBoundingClientRect:()=>rect(mode==='ellipsis'?40:150),contains:()=>false};
      const node={textContent:expected.identifier,parentElement:parent};
      const element={parentElement:null,querySelectorAll:()=>[],getBoundingClientRect:()=>rect(150)};
      const document={createTreeWalker:()=>{let done=false;return {nextNode(){if(done)return false;done=true;return true;},currentNode:node};},
        createRange:()=>({selectNodeContents(){},getBoundingClientRect:()=>rect(140),getClientRects:()=>[rect(140)]}),
        elementFromPoint:()=>mode==='occluded'?{}:parent};
      Object.defineProperty(globalThis,'document',{value:document,configurable:true});
      Object.defineProperty(globalThis,'getComputedStyle',{value:()=>({opacity:mode==='faded'?'0.2':'1',display:'block',
        visibility:'visible',overflowX:'hidden',overflowY:'visible'}),configurable:true});
      assert.equal(routerInterfaceRowView(element,{markers:[expected.identifier]}).fields[0].visible,mode==='full');
    }
  }finally{
    for(const [key,descriptor] of originals)if(descriptor)Object.defineProperty(globalThis,key,descriptor);else delete globalThis[key];
  }
});
test('no edits, arbitrary navigation, resizing or cloud calls; explicit dispatcher',()=>{
  const source=fs.readFileSync(new URL('../../scripts/web/e2e/resource_link_workflow/console_nas_protocol_service.mjs',import.meta.url),'utf8');
  for(const forbidden of ['.goto(','.fill(','.click(','.setViewportSize(','.style.','aliyun_api'])assert(!source.includes(forbidden));
  const browser=fs.readFileSync(new URL('../../scripts/web/e2e/resource_link_workflow/browser.mjs',import.meta.url),'utf8');
  assert(browser.includes("(await import('./console_nas_protocol_service.mjs')).inspectNasProtocolServiceIdentity"));
});
