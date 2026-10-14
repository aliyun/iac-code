import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import {essTaskConsoleUrl,essTaskMetadataReady,essTaskHorizontalDelta,essTaskRowView,inspectEssScheduledTaskMetadata}
  from '../../scripts/web/e2e/resource_link_workflow/console_ess_scheduled_task.mjs';

const expected={kind:'ess_scheduled_task_metadata',resource_type:'ALIYUN::ESS::ScheduledTask',identifier:'task123',
  name:'offline-task',scaling_group_id:'asg-offline',region:'cn-hangzhou',source_tool_use_id:'offline-call'};
const markers=[expected.identifier,expected.name,expected.scaling_group_id];
const url='https://essnew.console.aliyun.com/#/task/v3/timer/cn-hangzhou';

test('only source-bound task/name/full group and exact domestic timer route',()=>{
  assert.equal(essTaskConsoleUrl(url,expected).href,url);
  for(const patch of [{kind:'other'},{resource_type:'ALIYUN::ESS::ScalingRule'},{identifier:'asg-other'},
    {name:''},{scaling_group_id:'asr-other'},{region:'us-east-1'},{source_tool_use_id:''}])
    assert.throws(()=>essTaskConsoleUrl(url,{...expected,...patch}));
  for(const wrong of [url.replace('essnew.','evil.'),url.replace('cn-hangzhou','cn-beijing'),
    url.replace('/timer/','/group/'),url.replace('/#','/?anything=1#'),url.replace('https:','http:')])
    assert.throws(()=>essTaskConsoleUrl(wrong,expected),/unbound/);
});

test('DOM presence or partial group pixels never count as full row readiness',()=>{
  const view={loading:false,same_task_cell:true,fields:markers.map(marker=>({marker,visible:true}))};
  assert(essTaskMetadataReady(view,expected));
  for(const patch of [{loading:true},{same_task_cell:false},...markers.map(marker=>({fields:
    view.fields.map(f=>({...f,visible:f.marker!==marker}))}))])assert(!essTaskMetadataReady({...view,...patch},expected));
});

test('native scroll accounts for sticky task and action columns without moving the DOM',()=>{
  const view={fields:[{marker:expected.scaling_group_id,left:914,right:1077}],
    scroll:{left:522,right:945,current:0,maximum:776}};
  assert.equal(essTaskHorizontalDelta(view,expected),262);
  for(const patch of [{fields:[]},{fields:[{marker:expected.scaling_group_id,left:0,right:1000}]},
    {scroll:{...view.scroll,left:NaN}},{scroll:{...view.scroll,maximum:200}}])
    assert.throws(()=>essTaskHorizontalDelta({...view,...patch},expected),/ESS task/);
});

for(const scenario of ['ready','scroll','occluded','background','wrong-region','route-changed','session-changed',
  'ambiguous','late-row-changed','detached','capture-failed','iframe','missing-group'])
  test(`real native task sampling and untouched original landing: ${scenario}`,async()=>{
    let scrolled=false,checks=0,reads=0,urls=0;const actions=[];
    const row={waitFor:async()=>{},count:async()=>scenario==='ambiguous'||scenario==='late-row-changed'&&++reads>1?2:1,
      isVisible:async()=>true,hover:async()=>actions.push('hover'),evaluate:async()=>({loading:false,same_task_cell:true,
        fields:markers.filter(m=>scenario!=='missing-group'||m!==expected.scaling_group_id).map(marker=>({marker,
          left:marker===expected.scaling_group_id?914:290,right:marker===expected.scaling_group_id?1077:455,
          visible:scenario!=='occluded'&&(scenario!=='scroll'||scrolled||marker!==expected.scaling_group_id)})),
        scroll:{left:522,right:945,current:scrolled?262:0,maximum:776}})};
    const actual=()=>scenario==='wrong-region'?url.replace('cn-hangzhou','cn-beijing'):
      scenario==='route-changed'&&++urls>1?url.replace('/timer/','/other/'):url;
    const frame={url:actual,isDetached:()=>scenario==='detached',locator:()=>({filter:()=>row}),getByText:()=>({}),
      evaluate:async(fn,args)=>args?{text:markers.join(' '),loading:false,facts_visible:true}:
        fn.toString().includes('document.location.href')?actual():{painted:true}};
    const surface={bringToFront:async()=>actions.push('focus'),evaluate:async()=>scenario!=='background',
      mouse:{wheel:async(dx,dy)=>{assert.equal(dx,262);assert.equal(dy,0);scrolled=true;actions.push('scroll');}},
      screenshot:async opts=>{assert.equal(opts.fullPage,false);actions.push('pixels');if(scenario==='capture-failed')throw Error('capture failed');}};
    const options={mode:scenario==='iframe'?'iframe':'open',beforeCapture:async()=>{
      checks++;if(scenario==='session-changed')throw Error('session changed');}};
    const failures={'wrong-region':/unbound/,'route-changed':/unbound/,'session-changed':/session changed/,
      ambiguous:/ambiguous/,'late-row-changed':/ambiguous/,detached:/detached/,'capture-failed':/capture failed/,
      iframe:/direct-open/,'missing-group':/viewport unavailable/};
    if(scenario in failures)await assert.rejects(()=>inspectEssScheduledTaskMetadata(surface,frame,expected,'task.png',options),failures[scenario]);
    else{
      const result=await inspectEssScheduledTaskMetadata(surface,frame,expected,'task.png',options);
      assert.equal(result.identity_visible,!['occluded','background'].includes(scenario));
      assert.equal(result.readonly_completed,true);assert.equal(result.cloud_writes,0);
      assert.equal(result.original_landing_unchanged,true);assert.equal(result.source_tool_use_id,'offline-call');
      assert(!('pass' in result));assert(checks>1);
    }
    assert(actions.every(a=>['hover','scroll','pixels','focus'].includes(a)));
  });

test('actual fixed action column hides the full source group until native scrolling',()=>{
  const rect=(left,right)=>({left,right,top:240,bottom:257,width:right-left,height:17});
  const container={parentElement:null,scrollWidth:1668,clientWidth:892,scrollLeft:0,
    getBoundingClientRect:()=>rect(234,1126)};
  const task={innerText:markers.slice(0,2).join('\n'),position:'sticky',left:'0px',right:'auto',
    getBoundingClientRect:()=>rect(234,522)};
  const actions={innerText:'operations',position:'sticky',left:'auto',right:'0px',
    getBoundingClientRect:()=>rect(945,1126)};
  const el={parentElement:container,getBoundingClientRect:()=>rect(234,1126),querySelectorAll:()=>[task,actions]};
  const nodes=markers.map((text,index)=>{const wrapper={parentElement:el,
    contains:h=>h===wrapper,getBoundingClientRect:()=>index===2?rect(914,1077):rect(290,455)};
    return {textContent:text,parentElement:wrapper};});
  const replacements={innerWidth:1200,innerHeight:830,NodeFilter:{SHOW_TEXT:4},
    getComputedStyle:n=>({opacity:'1',visibility:'visible',display:'block',overflowX:'visible',overflowY:'visible',
      position:n.position||'static',left:n.left||'auto',right:n.right||'auto'}),document:{
      createTreeWalker:()=>{let i=-1;return {nextNode:()=>++i<nodes.length,get currentNode(){return nodes[i];}};},
      createRange:()=>{let n;return {selectNodeContents:v=>n=v,getBoundingClientRect:()=>n.parentElement.getBoundingClientRect(),
        getClientRects:()=>[n.parentElement.getBoundingClientRect()]};},querySelectorAll:()=>[],
      elementFromPoint:x=>x>=945?actions:x>=914?nodes[2].parentElement:nodes[0].parentElement}};
  const originals=new Map(Object.keys(replacements).map(k=>[k,Object.getOwnPropertyDescriptor(globalThis,k)]));
  try{
    for(const [k,value] of Object.entries(replacements))Object.defineProperty(globalThis,k,{value,configurable:true});
    const view=essTaskRowView(el,{markers,loadingSelector:'loading'});
    assert.equal(view.same_task_cell,true);assert.equal(view.fields[2].visible,false);
    assert.deepEqual(view.scroll,{left:522,right:945,current:0,maximum:776});
    assert.equal(essTaskHorizontalDelta(view,expected),262);
  }finally{for(const [k,d] of originals)if(d)Object.defineProperty(globalThis,k,d);else delete globalThis[k];}
});

test('sampler cannot create, edit, delete, invent a route, resize or hide DOM',()=>{
  const source=fs.readFileSync(new URL('../../scripts/web/e2e/resource_link_workflow/console_ess_scheduled_task.mjs',import.meta.url),'utf8');
  for(const forbidden of ['.goto(','.fill(','.setViewportSize(','.style.','aliyun_api','name:\'删除\''])assert(!source.includes(forbidden));
  const browser=fs.readFileSync(new URL('../../scripts/web/e2e/resource_link_workflow/browser.mjs',import.meta.url),'utf8');
  assert(browser.includes("(await import('./console_ess_scheduled_task.mjs')).inspectEssScheduledTaskMetadata"));
});
