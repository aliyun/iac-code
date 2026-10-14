import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import {resourceGroupConsoleUrl,resourceGroupMetadataReady,inspectResourceGroupMetadata}
  from '../../scripts/web/e2e/resource_link_workflow/console_resource_group.mjs';

const expected={kind:'resource_group_metadata',identifier:'rg-fixture123',name:'s76fixture',
  display_name:'fixture display',region:'global',api_region:'cn-hangzhou'};
const route='https://resourcemanager.console.aliyun.com/resource-groups/rg-fixture123/';
test('ResourceGroup console permits only exact domestic observed detail paths',()=>{
  for(const host of ['resourcemanager','resourcemanager4service'])for(const tab of ['overview','settings'])
    assert(resourceGroupConsoleUrl(route.replace('resourcemanager',host)+tab,expected));
  for(const url of ['http://resourcemanager.console.aliyun.com/resource-groups/rg-fixture123/overview',
    route+'overview?redirect=other',route+'overview#settings',route+'resources',
    route.replace('rg-fixture123','rg-other')+'overview',route.replace('resourcemanager','unrelated')+'overview',
    route.replace('https://','https://user@')+'overview',''])
    assert.throws(()=>resourceGroupConsoleUrl(url,expected),/unbound ResourceGroup/);
});

test('Settings metadata readiness requires all own-source fields as painted labeled facts',()=>{
  const view={loading:false,settings_selected:true,
    fields:[expected.identifier,expected.name,expected.display_name].map(marker=>({marker,visible:true}))};
  assert.equal(resourceGroupMetadataReady(view,expected),true);
  for(const patch of [{loading:true},{settings_selected:false},{fields:[]},
    {fields:view.fields.map((field,index)=>({...field,visible:index!==1}))}])
    assert.equal(resourceGroupMetadataReady({...view,...patch},expected),false);
});

for(const scenario of ['exact','iframe','mini-console','origin-changed','sidebar-settings','ambiguous-tab','no-tab','no-pane','ambiguous-pane',
  'wrong-host','wrong-id','bad-scope','detached','late-detached','session-changed','late-session-changed',
  'route-changed','no-navigation','unselected','loading','hidden-name','missing-display','capture-failed',
  'labeled-grid-dom','empty-fusion-dom','wrong-label-dom','breadcrumb-only-dom','wrong-value-dom',
  'duplicate-label-dom','covered-grid-dom','clipped-grid-dom','hidden-grid-dom'])
  test(`ResourceGroup native Settings retains original landing: ${scenario}`,async()=>{
    let current=route+'overview',checks=0,calls=0;
    const actualRoute=scenario==='mini-console'?route.replace('resourcemanager','resourcemanager4service'):route;
    if(scenario==='mini-console')current=actualRoute+'overview';
    const actions=[];
    const selected=scenario!=='unselected';
    const tab={isVisible:async()=>true,evaluate:async()=>scenario!=='sidebar-settings',
      getAttribute:async name=>{assert.equal(name,'aria-selected');return selected?'true':'false';},
      click:async()=>{actions.push('settings');if(scenario!=='no-navigation')current=actualRoute+'settings';}};
    const view={loading:scenario==='loading',fields:[expected.identifier,expected.name,expected.display_name]
      .filter(marker=>scenario!=='missing-display'||marker!==expected.display_name)
      .map(marker=>({marker,visible:!(scenario==='hidden-name'&&marker===expected.name)}))};
    const panel={waitFor:async()=>{},count:async()=>scenario==='no-pane'?0:scenario==='ambiguous-pane'?2:1,
      isVisible:async()=>true,evaluate:async(fn,args)=>{
        if(!scenario.endsWith('-dom'))return view;
        const rect={left:20,top:20,right:240,bottom:40,width:220,height:20};
        const values=args.markers.map(({marker,label},index)=>{
          const value={parentElement:null,textContent:scenario==='wrong-value-dom'&&index===1?'other':marker,
            getBoundingClientRect:()=>scenario==='clipped-grid-dom'?{...rect,right:180,width:160}:rect,
            contains:target=>target===value};
          value.nodes=[{textContent:value.textContent,parentElement:value}];
          const labelElement={closest:()=>null};
          const item={nodes:[{textContent:scenario==='wrong-label-dom'&&index===1?'标题':label,parentElement:labelElement},...value.nodes],
            querySelectorAll:selector=>{assert.equal(selector,'.wind-rc-data-fields-item-value');return [value];}};
          return item;
        });
        if(scenario==='duplicate-label-dom')values.push(values[1]);
        const el={querySelectorAll:selector=>{
          assert.equal(selector,'.wind-rc-data-fields-item');
          return ['empty-fusion-dom','breadcrumb-only-dom'].includes(scenario)?[]:values;
        }};
        const document={createTreeWalker:root=>{let index=-1;const nodes=root.nodes||[];
          return {nextNode(){return ++index<nodes.length;},get currentNode(){return nodes[index];}};},
          createRange:()=>({selectNodeContents(){},getClientRects:()=>[rect]}),
          elementFromPoint:()=>scenario==='covered-grid-dom'?el:values.find(item=>item.nodes[1].parentElement.textContent===
            // Every fake field occupies the same box; the active text walker
            // chooses its own hit target below, without testing unrelated DOM.
            currentMarker)?.nodes[1].parentElement,
          querySelectorAll:()=>[]};
        let currentMarker;
        document.createRange=()=>({selectNodeContents(node){currentMarker=node.textContent;},getClientRects:()=>[rect]});
        return vm.runInNewContext(`(${fn.toString()})(el,args)`,{el,args,document,NodeFilter:{SHOW_TEXT:4},
          innerWidth:900,innerHeight:600,getComputedStyle:()=>({opacity:'1',display:'block',
            visibility:scenario==='hidden-grid-dom'?'hidden':'visible',
            overflowX:scenario==='clipped-grid-dom'?'hidden':'visible',overflowY:'visible'})});
      },
      filter:options=>{assert(['资源组ID','资源组标识','资源组名称'].includes(options.has));return panel;}};
    const frame={url:()=>current,isDetached:()=>scenario==='detached'||scenario==='late-detached'&&checks>1,
      getByRole:(role,{name,exact})=>{
        assert.equal(role,'tab');assert.equal(name,'设置');assert.equal(exact,true);
        return {all:async()=>scenario==='no-tab'?[]:scenario==='ambiguous-tab'?[tab,tab]:[tab]};},
      getByText:(label,options)=>{assert.deepEqual(options,{exact:true});return label;},
      locator:selector=>{assert.equal(selector,'.wind-rc-data-fields:visible');return panel;},
      frameElement:async()=>({evaluate:async()=>({visible:true}),dispose:async()=>{}}),
      evaluate:async(_fn,args)=>{
        if(args)return {text:expected.identifier+' '+expected.name+' '+expected.display_name,loading:false,facts_visible:true};
        calls++;return current;
      }};
    if(scenario==='wrong-host')current=current.replace('resourcemanager','other');
    if(scenario==='wrong-id')current=current.replace('rg-fixture123','rg-other');
    const surface={bringToFront:async()=>actions.push('focus'),evaluate:async()=>true,
      screenshot:async options=>{actions.push('pixels');assert.equal(options.fullPage,false);
        if(scenario==='capture-failed')throw Error('capture failed');}};
    const beforeCapture=async()=>{
      checks++;
      if(scenario==='session-changed'||scenario==='late-session-changed'&&checks>2)throw Error('session mismatch');
      if(scenario==='route-changed'&&checks>2)current=route.replace('rg-fixture123','rg-other')+'settings';
      if(scenario==='origin-changed'&&checks>2)current=route.replace('resourcemanager','resourcemanager4service')+'settings';
    };
    const errors=['sidebar-settings','ambiguous-tab','no-tab','no-pane','ambiguous-pane','wrong-host',
      'wrong-id','bad-scope','detached','late-detached','session-changed','late-session-changed','route-changed',
      'no-navigation','capture-failed','origin-changed'];
    const execute=()=>inspectResourceGroupMetadata(surface,frame,
      {...expected,region:scenario==='bad-scope'?'cn-hangzhou':'global'},'metadata.png',
      {mode:scenario==='iframe'?'iframe':'open',beforeCapture,renderTimeoutMs:0});
    if(errors.includes(scenario))await assert.rejects(execute);
    else{
      const result=await execute();
      assert.equal(result.identity_visible,['exact','iframe','mini-console','labeled-grid-dom'].includes(scenario));
      assert.equal(result.readonly_completed,true);assert.equal(result.original_landing_unchanged,true);
      assert.equal(result.cloud_writes,0);assert.equal(result.screenshot,'metadata.png');
      assert.equal(result.stages[0].url,actualRoute+'overview');assert(!('pass' in result));
      assert(calls>0);
    }
    assert(actions.every(action=>['focus','settings','pixels'].includes(action)));
  });

test('ResourceGroup inspector does not edit, navigate arbitrary URLs or call cloud APIs',()=>{
  const source=fs.readFileSync(new URL('../../scripts/web/e2e/resource_link_workflow/console_resource_group.mjs',import.meta.url),'utf8');
  for(const forbidden of ['.fill(','.goto(','.setAttribute(','.style.','aliyun_api','name:\'删除\'','name:\'保存\''])
    assert(!source.includes(forbidden));
});
