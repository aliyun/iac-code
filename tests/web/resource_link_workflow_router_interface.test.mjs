import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import {routerInterfaceReference,routerInterfaceChildReady,routerInterfaceHorizontalDelta,routerInterfaceRowView,
  inspectRouterInterfaceChild}
  from '../../scripts/web/e2e/resource_link_workflow/console_router_interface.mjs';

const expected={kind:'router_interface_vbr_child',resource_type:'ALIYUN::VPC::RouterInterface',identifier:'ri-child',
  parent_router_id:'vbr-own',peer_vpc:'vpc-peer',name:'offline-name',region:'cn-hangzhou',source_tool_use_id:'offline-call'};
const markers=[expected.identifier,expected.parent_router_id,expected.name,expected.peer_vpc];
const url='https://expressconnect.console.aliyun.com/vbr/cn-hangzhou/detail/vbr-own';

test('real row sampling excludes sticky cells belonging to the same TR from the painted native gap',()=>{
  const rect=(left,right)=>({left,right,top:650,bottom:665,width:right-left,height:15});
  const container={parentElement:null,scrollWidth:2670,clientWidth:894,scrollLeft:0,
    getBoundingClientRect:()=>rect(232,1126)};
  const el={parentElement:container,getBoundingClientRect:()=>rect(232,1126),contains:()=>true};
  const child={parentElement:el,innerText:markers.slice(0,3).join('\n'),position:'static',
    getBoundingClientRect:()=>rect(382,602)};
  const peer={parentElement:el,innerText:markers[3],position:'static',getBoundingClientRect:()=>rect(902,1122)};
  const stickyLeft={parentElement:el,innerText:'',position:'sticky',getBoundingClientRect:()=>rect(232,282)};
  const stickyRight={parentElement:el,innerText:'actions',position:'sticky',getBoundingClientRect:()=>rect(776,1126)};
  const rightHit={closest:()=>stickyRight},leftHit={closest:()=>stickyLeft};
  const nodes=markers.map((text,index)=>{
    const wrapper={parentElement:index===3?peer:child,closest:()=>index===3?peer:child,
      getBoundingClientRect:()=>index===3?rect(918,1072):rect(398,556)};
    wrapper.contains=hit=>hit===wrapper;
    return {textContent:text,parentElement:wrapper,index};
  });
  el.querySelectorAll=()=>[child,peer,stickyLeft,stickyRight];
  const replacements={innerWidth:1200,innerHeight:830,NodeFilter:{SHOW_TEXT:4},
    getComputedStyle:node=>({opacity:'1',display:'block',visibility:'visible',overflowX:'visible',overflowY:'visible',
      position:node.position||'static'}),
    document:{createTreeWalker:()=>{let i=-1;return {nextNode(){return ++i<nodes.length;},get currentNode(){return nodes[i];}};},
      createRange:()=>{let selected;return {selectNodeContents(node){selected=node;},
        getBoundingClientRect:()=>selected.parentElement.getBoundingClientRect(),
        getClientRects:()=>[selected.parentElement.getBoundingClientRect()]};},
      elementFromPoint:x=>x>=776?rightHit:x<282?leftHit:x>=918?nodes[3].parentElement:nodes[0].parentElement}};
  const originals=new Map(Object.keys(replacements).map(key=>[key,Object.getOwnPropertyDescriptor(globalThis,key)]));
  try{
    for(const [key,value] of Object.entries(replacements))Object.defineProperty(globalThis,key,{value,configurable:true});
    const view=routerInterfaceRowView(el,{markers});
    assert.equal(view.same_child_cell,true);
    assert.equal(view.fields[0].visible,true);
    assert.equal(view.fields[3].visible,false);
    assert.deepEqual(view.scroll,{left:282,right:774,current:0,maximum:1776});
    assert.throws(()=>routerInterfaceHorizontalDelta(view),/viewport too narrow/);
  }finally{
    for(const [key,descriptor] of originals)if(descriptor)Object.defineProperty(globalThis,key,descriptor);else delete globalThis[key];
  }
});

test('child sampling requires complete source identity, reviewed type and own VBR, never opposite router',()=>{
  assert.equal(routerInterfaceReference(expected).attributes.RouterId,'vbr-own');
  for(const patch of [{kind:'other'},{resource_type:'ALIYUN::VPC::VRouter'},{identifier:'vbr-other'},
    {parent_router_id:'vpc-peer'},{peer_vpc:'vbr-other'},{region:'us-east-1'},{name:''},{source_tool_use_id:''}])
    assert.throws(()=>routerInterfaceReference({...expected,...patch}),/invalid source-bound/);
});

test('all four exact child fields must be painted in the same selected loaded row',()=>{
  const view={tab_selected:true,same_child_cell:true,loading:false,fields:markers.map(marker=>({marker,visible:true}))};
  assert(routerInterfaceChildReady(view,expected));
  for(const patch of [{tab_selected:false},{same_child_cell:false},{loading:true},
    ...markers.map(marker=>({fields:view.fields.map(field=>({...field,visible:field.marker!==marker}))}))])
    assert.equal(routerInterfaceChildReady({...view,...patch},expected),false);
});

test('horizontal adjustment fits full source fields in the actual unoccluded native gap',()=>{
  const view={fields:[{left:400,right:550},{left:900,right:1050}],scroll:{left:240,right:1000,current:0,maximum:2000}};
  assert.equal(routerInterfaceHorizontalDelta(view),52);
  assert.equal(routerInterfaceHorizontalDelta({...view,fields:[{left:250,right:800}]}),0);
  assert.equal(routerInterfaceHorizontalDelta({...view,fields:[{left:200,right:800}],scroll:{...view.scroll,current:100}}),-42);
  for(const patch of [{fields:[]},{fields:[{left:0,right:2000}]},{fields:[{left:5,right:4}]},
    {scroll:{...view.scroll,right:NaN}},{scroll:{...view.scroll,maximum:20}}])
    assert.throws(()=>routerInterfaceHorizontalDelta({...view,...patch}),/RouterInterface/);
});

for(const scenario of ['ready','scroll','collapse','occluded','background','iframe','iframe-hidden',
  'wrong-parent','wrong-region','origin-changed','session-changed','row-ambiguous','late-row-changed',
  'detached','restore-failed','capture-failed','oopif-empty-cache','remote-latency'])
  test(`source-bound native peer tab and unchanged pixels: ${scenario}`,async()=>{
    let selected=false,collapsed=false,scrolled=false,checks=0,rowReads=0,urlReads=0;
    const actions=[],control={count:async()=>1,isVisible:async()=>true};
    const tab={...control,getAttribute:async()=>String(selected),click:async()=>{selected=true;actions.push('peer-tab');}};
    const toggle={...control,count:async()=>scenario==='restore-failed'&&collapsed?0:1,
      click:async()=>{collapsed=!collapsed;actions.push(collapsed?'collapse':'restore');}};
    const row={...control,count:async()=>scenario==='row-ambiguous'||scenario==='late-row-changed'&&++rowReads>1?2:1,
      hover:async()=>actions.push('hover'),evaluate:async()=>({same_child_cell:true,row_rect:{left:240,top:600},
        fields:markers.map((marker,index)=>({marker,left:index===3?900:400,right:index===3?1050:550,top:650,bottom:665,
          visible:scenario!=='occluded'&&(!['scroll','collapse','restore-failed'].includes(scenario)||scrolled)})),
        scroll:{left:240,right:['collapse','restore-failed'].includes(scenario)&&!collapsed?800:1000,
          current:scrolled?52:0,maximum:2000}})};
    const body={evaluate:async()=>{
      if(scenario==='remote-latency')await new Promise(resolve=>setTimeout(resolve,150));
      return {tabSelected:selected,visibleLoading:false,rows:[{cells:['vbr-own\nri-child\noffline-name','vpc-peer']}]};},
      innerText:async()=>markers.join(' ')};
    const actual=()=>scenario==='wrong-parent'?url.replace('vbr-own','vbr-other'):
      scenario==='wrong-region'?url.replace('cn-hangzhou','cn-beijing'):
        scenario==='origin-changed'&&++urlReads>1?url.replace('expressconnect.','expressconnect4service.'):url;
    const frame={isDetached:()=>scenario==='detached',url:()=>scenario==='oopif-empty-cache'?'':actual(),
      getByRole:(role,opts)=>{assert.equal(role,'tab');assert.deepEqual(opts,{name:'对等连接',exact:true});return tab;},
      getByText:()=>({}),locator:selector=>selector==='body'?body:selector==='tbody tr'?{filter:()=>row}:toggle,
      evaluate:async(fn,args)=>args?{text:markers.join(' '),loading:false,facts_visible:true}:
        fn.toString().includes('document.location.href')?actual():{painted:true},
      frameElement:async()=>({evaluate:async()=>({visible:scenario!=='iframe-hidden'}),dispose:async()=>{}})};
    const surface={bringToFront:async()=>actions.push('focus'),evaluate:async()=>scenario!=='background',
      mouse:{wheel:async(dx,dy)=>{assert.equal(dx,52);assert.equal(dy,0);scrolled=true;actions.push('scroll');}},
      screenshot:async options=>{assert.equal(options.fullPage,false);actions.push('pixels');
        if(scenario==='capture-failed')throw Error('capture failed');}};
    const options={mode:scenario.startsWith('iframe')?'iframe':'open',renderTimeoutMs:scenario==='remote-latency'?2000:500,
      beforeCapture:async()=>{checks++;if(scenario==='session-changed')throw Error('session changed');}};
    const failures={'wrong-parent':/own VBR route/,'wrong-region':/own VBR route/,'origin-changed':/origin changed/,
      'session-changed':/session changed/,'row-ambiguous':/unique same-parent/,'late-row-changed':/row changed/,
      detached:/detached/,'restore-failed':/restore unavailable/,'capture-failed':/capture failed/};
    if(scenario in failures)await assert.rejects(()=>inspectRouterInterfaceChild(surface,frame,expected,'child.png',options),failures[scenario]);
    else{
      const result=await inspectRouterInterfaceChild(surface,frame,expected,'child.png',options);
      assert.equal(result.identity_visible,!['occluded','background','iframe-hidden'].includes(scenario));
      assert.equal(result.cloud_writes,0);assert.equal(result.readonly_completed,true);
      assert.equal(result.original_landing_unchanged,true);assert(!('pass' in result));
      assert.equal(result.source_tool_use_id,'offline-call');assert(checks>1);
    }
    assert(actions.every(action=>['peer-tab','collapse','restore','hover','scroll','pixels','focus'].includes(action)));
    if(scenario==='collapse')assert.equal(collapsed,false);
  });

test('child UI sampling cannot create resources, forge routes, resize, hide DOM or edit fields',()=>{
  const source=fs.readFileSync(new URL('../../scripts/web/e2e/resource_link_workflow/console_router_interface.mjs',import.meta.url),'utf8');
  for(const forbidden of ['.goto(','.fill(','.setViewportSize(','.style.','aliyun_api','name:\'删除\''])
    assert(!source.includes(forbidden));
});
