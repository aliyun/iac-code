import test from 'node:test';import assert from 'node:assert/strict';import fs from 'node:fs';
import {saeApplicationConsoleUrl,saeApplicationFieldView,saeApplicationFieldsReady,inspectSaeApplicationMetadata}
  from '../../scripts/web/e2e/resource_link_workflow/console_sae_application.mjs';
const expected={kind:'sae_application_metadata',resource_type:'ALIYUN::SAE::Application',
  identifier:'11111111-2222-3333-4444-555555555555',region:'cn-shenzhen',name:'testdesc',description:'desc',
  source_tool_use_id:'offline-call'};
const url=`https://saenext4service.console.aliyun.com/cn-shenzhen/app-list/${expected.identifier}/micro-app/base?name=testdesc`;
test('strict own SAE actual route, no foreign parameters or resource',()=>{
  assert(saeApplicationConsoleUrl(url,expected));assert(saeApplicationConsoleUrl(url.replace('4service',''),expected));
  for(const raw of [url+'&edit=true',url+'#edit',url.replace('testdesc','other'),url.replace('https:','http:'),
    url.replace('cn-shenzhen','cn-hangzhou'),url.replace(expected.identifier,'other'),url.replace('aliyun.com','aliyun.com.evil')])
    assert.throws(()=>saeApplicationConsoleUrl(raw,expected));
  for(const patch of [{kind:'other'},{resource_type:'ALIYUN::SAE::Namespace'},{name:''},{description:''},
    {source_tool_use_id:''},{region:'global'},{identifier:'other'}])assert.throws(()=>saeApplicationConsoleUrl(url,{...expected,...patch}));
});
function ready(){return {fields:['应用名','应用ID','应用描述'].map(label=>({label,unique:true,matched:true,visible:true})),loading_signals:[]};}
for(const scenario of ['valid','below-fold','missing','hidden','duplicate','occluded','name-substring-only','wrong-value',
  'ancestor-clip','iframe-clip','wrapped-bottom','edge-occluded','loading'])
  test(`actual field probe: ${scenario}`,()=>{
    const keys=['document','NodeFilter','getComputedStyle','innerWidth','innerHeight'];
    const saved=new Map(keys.map(k=>[k,Object.getOwnPropertyDescriptor(globalThis,k)]));
    const rect={left:10,top:10,right:150,bottom:30};
    let hit;
    const make=(value,label,isDescription=false)=>{
      const node={textContent:value,parentElement:null,contains:el=>el===node,
        getClientRects:()=>scenario==='hidden'&&isDescription?[]:[{}],
        getBoundingClientRect:()=>scenario==='ancestor-clip'&&isDescription?{...rect,bottom:20}:rect};
      node.item={textContent:value,parentElement:node};node.description=isDescription;
      node.rect={...rect,...(scenario==='below-fold'&&isDescription?{top:110,bottom:130}:{})};
      node.row={querySelectorAll:()=>[node]};node.closest=()=>node.row;
      const caption={...node,textContent:label,description:isDescription};caption.item={textContent:label,parentElement:caption};
      caption.contains=el=>el===caption;caption.closest=()=>node.row;
      return {node,caption};
    };
    const fields=[make(expected.name,'应用名'),make(expected.identifier,'应用ID'),make(expected.description,'应用描述',true)];
    const labels=fields.map(f=>f.caption);
    if(['missing','name-substring-only'].includes(scenario))labels.pop();
    if(scenario==='duplicate')labels.push(fields[2].caption);
    if(scenario==='wrong-value')fields[2].node.textContent='testdesc';
    try{
      for(const [key,value] of Object.entries({innerWidth:200,innerHeight:100,NodeFilter:{SHOW_TEXT:4},
        getComputedStyle:el=>({opacity:'1',display:'block',visibility:'visible',overflowX:'visible',
          overflowY:scenario==='ancestor-clip'&&el.description?'hidden':'visible'}),
        document:{createTreeWalker:node=>{let first=true;return {nextNode(){if(!first)return false;first=false;hit=node;this.currentNode=node.item;return true;}};},
          createRange:()=>({selectNodeContents(){},getClientRects:()=>[
            hit.rect??rect,...(scenario==='wrapped-bottom'&&hit.description?[{...rect,top:95,bottom:115}]:[])]}),
          elementFromPoint:x=>['occluded','edge-occluded'].includes(scenario)&&hit.description&&
            (scenario==='occluded'||x<15)?{}:hit}}))Object.defineProperty(globalThis,key,{value,configurable:true});
      const root={querySelectorAll:selector=>selector.includes('item-label')?labels:scenario==='loading'?[{
        getClientRects:()=>[{}],getAttribute:()=>null,className:'next-loading-mask',textContent:''}]:[]};
      const view=saeApplicationFieldView(root,{expected,loadingSelector:'loading',
        embeddingClip:scenario==='iframe-clip'?{left:0,top:0,right:200,bottom:20}:null});
      assert.equal(saeApplicationFieldsReady(view),scenario==='valid');
    }finally{for(const [key,descriptor] of saved)if(descriptor)Object.defineProperty(globalThis,key,descriptor);else delete globalThis[key];}
  });
for(const scenario of ['open','iframe','route-changed','session-changed','detached','scroll-failed','clipped',
  'capture-failed','background','late-field-change'])
  test(`native bounded capture: ${scenario}`,async()=>{
    const actions=[];let locations=0,views=0;
    const frame={isDetached:()=>scenario==='detached',evaluate:async(fn,args)=>args?
      {text:[expected.identifier,expected.name,expected.description].join(' '),facts_visible:true,loading:false}:
      fn.toString().includes('document.location.href')?scenario==='route-changed'&&++locations>1?url+'&edit=true':url:{painted:true},
      url:()=>url,getByText:()=>({}),locator:selector=>selector==='body'?{evaluate:async()=>{
        const view=ready();if(scenario==='clipped'||scenario==='late-field-change'&&++views>1)view.fields[2].visible=false;return view;}}:
        {filter:()=>({count:async()=>1,scrollIntoViewIfNeeded:async()=>{actions.push('scroll');if(scenario==='scroll-failed')throw Error('scroll');}})},
      frameElement:async()=>({evaluate:async()=>({visible:true,left:0,top:0,right:1200,bottom:850}),dispose:async()=>{}})};
    const surface={bringToFront:async()=>actions.push('focus'),evaluate:async()=>scenario!=='background',
      screenshot:async args=>{assert.equal(args.fullPage,false);actions.push('pixels');if(scenario==='capture-failed')throw Error('pixels');}};
    const options={mode:scenario==='iframe'?'iframe':'open',beforeCapture:async()=>{if(scenario==='session-changed')throw Error('session');}};
    if(['route-changed','session-changed','detached','scroll-failed','clipped','capture-failed','late-field-change'].includes(scenario))
      await assert.rejects(()=>inspectSaeApplicationMetadata(surface,frame,expected,'offline.png',options));
    else{const result=await inspectSaeApplicationMetadata(surface,frame,expected,'offline.png',options);
      assert.equal(result.identity_visible,scenario!=='background');
      assert.equal(result.cloud_writes,0);assert.equal(result.readonly_completed,true);assert(!('pass' in result));}
    assert(actions.every(action=>['focus','scroll','pixels'].includes(action)));
  });
test('sampler cannot edit, call cloud, navigate, resize or hide controls',()=>{
  const source=fs.readFileSync(new URL('../../scripts/web/e2e/resource_link_workflow/console_sae_application.mjs',import.meta.url),'utf8');
  for(const forbidden of ['.goto(','.click(','.fill(','.setViewportSize(','.style.','aliyun_api'])assert(!source.includes(forbidden));
});
