import test from 'node:test';import assert from 'node:assert/strict';import fs from 'node:fs';
import {otsVpcBindingConsoleUrl,otsVpcBindingRowReady,inspectOtsVpcBindingRow}
  from '../../scripts/web/e2e/resource_link_workflow/console_ots_vpc_binding.mjs';
import {waitForNavigationIdentity} from '../../scripts/web/e2e/resource_link_workflow/browser.mjs';
const expected={kind:'ots_vpc_binding_row',resource_type:'ALIYUN::OTS::VpcBinder',identifier:'offline-binding',
  instance_name:'offline-parent',vpc_id:'vpc-offline',region:'cn-hangzhou',source_tool_use_id:'offline-call'};
const url='https://ots4service.console.aliyun.com/cn-hangzhou/offline-parent/networkManagement';
test('only exact own native OTS network-management route',()=>{
  assert(otsVpcBindingConsoleUrl(url,expected));
  assert(otsVpcBindingConsoleUrl(url.replace('ots4service','ots'),expected));
  for(const wrong of [url+'?x=1',url+'#x',url+'/',url.replace('https:','http:'),url.replace('cn-hangzhou','cn-beijing'),
    url.replace('offline-parent','other-parent'),url.replace('aliyun.com','aliyun.com.evil'),
    url.replace('https://','https://user@'),url.replace('console.aliyun.com','console.aliyun.com:444')])
    assert.throws(()=>otsVpcBindingConsoleUrl(wrong,expected),/unbound/);
  for(const patch of [{kind:'other'},{resource_type:'other'},{instance_name:'../other'},{identifier:''},
    {vpc_id:'other'},{region:'global'},{source_tool_use_id:''}])
    assert.throws(()=>otsVpcBindingConsoleUrl(url,{...expected,...patch}),/invalid/);
});
test('full parent, name and VPC must be in same unique painted native row',()=>{
  const view={unique_row:true,fields:[expected.identifier,expected.instance_name,expected.vpc_id].map(marker=>({marker,visible:true}))};
  assert(otsVpcBindingRowReady(view,expected));
  assert(!otsVpcBindingRowReady({...view,unique_row:false},expected));
  for(const field of view.fields)assert(!otsVpcBindingRowReady({...view,fields:view.fields.filter(f=>f!==field)},expected));
});
for(const scenario of ['open','iframe','ambiguous','clipped','session-changed','route-changed','detached',
  'background','scroll-failed','capture-failed','late-row-change','oopif-empty-cache'])
  test(`native source-bound row capture: ${scenario}`,async()=>{
    let locations=0,counts=0;const actions=[];
    const row={count:async()=>scenario==='ambiguous'||scenario==='late-row-change'&&++counts>1?2:1,
      isVisible:async()=>true,scrollIntoViewIfNeeded:async()=>{actions.push('scroll');if(scenario==='scroll-failed')throw Error('scroll');},
      evaluate:async(_,args)=>({fields:args.markers.map(marker=>({marker,visible:scenario!=='clipped'}))})};
    const frame={url:()=>scenario==='oopif-empty-cache'?'':url,isDetached:()=>scenario==='detached',
      locator:()=>({filter:()=>row}),getByText:()=>({}),evaluate:async(fn,args)=>args?
        {text:[expected.identifier,expected.instance_name,expected.vpc_id].join(' '),facts_visible:true,loading:false}:
        fn.toString().includes('document.location.href')?
          scenario==='route-changed'&&++locations>1?url.replace('offline-parent','other-parent'):url:{painted:true},
      frameElement:async()=>({evaluate:async()=>({visible:true,left:0,top:0,right:1200,bottom:850}),dispose:async()=>{}})};
    const surface={bringToFront:async()=>actions.push('focus'),evaluate:async()=>scenario!=='background',
      screenshot:async options=>{assert.equal(options.fullPage,false);actions.push('pixels');if(scenario==='capture-failed')throw Error('capture');}};
    const options={mode:scenario==='iframe'?'iframe':'open',beforeCapture:async()=>{
      if(scenario==='session-changed')throw Error('session');}};
    if(['ambiguous','clipped','session-changed','route-changed','detached','scroll-failed','capture-failed','late-row-change'].includes(scenario))
      await assert.rejects(()=>inspectOtsVpcBindingRow(surface,frame,expected,'offline.png',options));
    else{
      const result=await inspectOtsVpcBindingRow(surface,frame,expected,'offline.png',options);
      assert.equal(result.identity_visible,scenario!=='background');assert.equal(result.cloud_writes,0);
      assert.equal(result.original_landing_unchanged,true);assert(!('pass' in result));
    }
    assert(actions.every(action=>['focus','scroll','pixels'].includes(action)));
  });
for(const scenario of ['full','bottom','right','left','top','wrapped-bottom','ancestor-y','ancestor-x','edge-occluded','embedded-bottom'])
  test(`actual Range readiness rejects partial pixels: ${scenario}`,async()=>{
    const keys=['document','NodeFilter','getComputedStyle','innerWidth','innerHeight'];
    const saved=new Map(keys.map(key=>[key,Object.getOwnPropertyDescriptor(globalThis,key)]));
    const rect={left:10,top:10,right:110,bottom:30};
    const parent={parentElement:null,getClientRects:()=>[{}],contains:()=>false,
      getBoundingClientRect:()=>({left:0,top:0,right:scenario==='ancestor-x'?50:200,bottom:scenario==='ancestor-y'?20:100})};
    const pieces=[rect];
    if(scenario==='bottom')rect.bottom=110;
    if(scenario==='right')rect.right=210;
    if(scenario==='left')rect.left=-1;
    if(scenario==='top')rect.top=-1;
    if(scenario==='wrapped-bottom')pieces.push({...rect,top:95,bottom:115});
    let first=true;const node={textContent:'offline-binding',parentElement:parent};
    try{
      Object.defineProperty(globalThis,'innerWidth',{value:200,configurable:true});
      Object.defineProperty(globalThis,'innerHeight',{value:100,configurable:true});
      Object.defineProperty(globalThis,'NodeFilter',{value:{SHOW_TEXT:4},configurable:true});
      Object.defineProperty(globalThis,'getComputedStyle',{value:()=>({opacity:'1',display:'block',visibility:'visible',
        overflowX:scenario==='ancestor-x'?'hidden':'visible',overflowY:scenario==='ancestor-y'?'auto':'visible'}),configurable:true});
      Object.defineProperty(globalThis,'document',{value:{body:{innerText:node.textContent},querySelectorAll:()=>[],
        createTreeWalker:()=>({nextNode(){if(!first)return false;first=false;this.currentNode=node;return true;}}),
        createRange:()=>({setStart(){},setEnd(){},getClientRects:()=>pieces}),
        elementFromPoint:x=>scenario==='edge-occluded'&&x<15?{}:parent},configurable:true});
      const frame={url:()=>url,evaluate:async(fn,args)=>fn(args)};
      if(scenario==='embedded-bottom')frame.frameElement=async()=>({evaluate:async()=>({left:0,top:0,right:200,bottom:20}),dispose:async()=>{}});
      const result=await waitForNavigationIdentity({mainFrame:()=>frame},scenario==='embedded-bottom'?'iframe':'open',node.textContent,0,[],[frame]);
      assert.equal(result.identity_visible,scenario==='full');
    }finally{for(const [key,descriptor] of saved)if(descriptor)Object.defineProperty(globalThis,key,descriptor);else delete globalThis[key];}
  });
test('native inspector never changes URLs, styles, viewport or cloud state',()=>{
  const source=fs.readFileSync(new URL('../../scripts/web/e2e/resource_link_workflow/console_ots_vpc_binding.mjs',import.meta.url),'utf8');
  for(const forbidden of ['.goto(','.fill(','.click(','.setViewportSize(','.style.','aliyun_api'])assert(!source.includes(forbidden));
});
