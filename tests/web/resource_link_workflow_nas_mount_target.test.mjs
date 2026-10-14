import test from 'node:test';import assert from 'node:assert/strict';import fs from 'node:fs';
import {nasMountTargetConsoleUrl,nasMountTargetAddressReady,inspectNasMountTargetAddress}
  from '../../scripts/web/e2e/resource_link_workflow/console_nas_mount_target.mjs';
const expected={kind:'nas_mount_target_address',resource_type:'ALIYUN::NAS::MountTarget',
  identifier:'offlinefs-target.cn-hangzhou.nas.aliyuncs.com',file_system_id:'offlinefs',region:'cn-hangzhou',
  account:'1234567890123456',vpc_id:'vpc-offline',vswitch_id:'vsw-offline',source_tool_use_id:'offline-call'};
const url='https://nasnext4service.console.aliyun.com/cn-hangzhou/filesystem/offlinefs/mount';
test('strict own parent route, signed identity and reviewed region required',()=>{
  assert(nasMountTargetConsoleUrl(url,expected));assert(nasMountTargetConsoleUrl(url.replace('nasnext4service','nasnext'),expected));
  for(const wrong of [url+'?other=1',url+'#other',url+'/',url.replace('https:','http:'),url.replace('offlinefs','other'),
    url.replace('aliyun.com','aliyun.com.example'),url.replace('nasnext4service','evil'),
    url.replace('https://','https://user:pass@'),url.replace('.com/','.com:444/')])
    assert.throws(()=>nasMountTargetConsoleUrl(wrong,expected),/unbound/);
  for(const patch of [{kind:'other'},{resource_type:'ALIYUN::NAS::ProtocolMountTarget'},{file_system_id:'other'},
    {identifier:'wrong.cn-hangzhou.nas.aliyuncs.com'},{region:'cn-beijing'},{account:'wrong'},
    {source_tool_use_id:''},{vpc_id:'wrong'},{vswitch_id:'wrong'}])
    assert.throws(()=>nasMountTargetConsoleUrl(url,{...expected,...patch}),/invalid/);
});
test('both full visible fields and unique native row/icon/tooltip are mandatory',()=>{
  const view={unique_row:true,unique_address_icon:true,unique_tooltip:true,
    fields:[expected.identifier,expected.vpc_id].map(marker=>({marker,visible:true}))};
  assert(nasMountTargetAddressReady(view,expected));
  for(const patch of [{unique_row:false},{unique_address_icon:false},{unique_tooltip:false},{fields:[]},
    ...view.fields.map(f=>({fields:view.fields.map(x=>({...x,visible:x.marker!==f.marker}))}))])
    assert(!nasMountTargetAddressReady({...view,...patch},expected));
});
for(const scenario of ['open','iframe','ambiguous-row','hidden-row','ambiguous-table','missing-header','duplicate-header',
  'wrong-cell-count','ambiguous-icon','ambiguous-tooltip','clipped-domain','clipped-vpc','session-changed','route-changed',
  'detached','background','capture-failed','hover-failed','scroll-failed','oopif-empty-cache','hidden-tooltip',
  'hidden-icon','embedding-clipped','late-row-changed'])
  test(`native address scroll/hover, no navigation or write: ${scenario}`,async()=>{
    const actions=[];let reads=0,rowReads=0;
    const icon={count:async()=>scenario==='ambiguous-icon'?2:1,isVisible:async()=>scenario!=='hidden-icon',hover:async()=>{
      actions.push('hover');if(scenario==='hover-failed')throw Error('hover failed');}};
    const cell={locator:()=>icon,scrollIntoViewIfNeeded:async()=>{
      actions.push('scroll');if(scenario==='scroll-failed')throw Error('scroll failed');}};
    const cells={count:async()=>scenario==='wrong-cell-count'?2:3,nth:()=>cell};
    const row={filter(){return this;},count:async()=>scenario==='ambiguous-row'||scenario==='late-row-changed'&&++rowReads>1?2:1,
      isVisible:async()=>scenario!=='hidden-row',locator:()=>cells,evaluate:async(_,args)=>({fields:args.markers.map(marker=>({marker,
        visible:scenario!=='clipped-vpc'}))})};
    const table={filter(){return this;},count:async()=>scenario==='ambiguous-table'?2:1,locator:()=>({allTextContents:async()=>
      scenario==='missing-header'?['VPC','other','status']:scenario==='duplicate-header'?['挂载点地址','挂载点地址','status']:
        ['VPC','挂载点地址','status']})};
    const tip={filter(){return this;},waitFor:async()=>{},count:async()=>scenario==='ambiguous-tooltip'?2:1,
      isVisible:async()=>scenario!=='hidden-tooltip',evaluate:async(_,args)=>({fields:args.markers.map(marker=>({marker,
        visible:scenario!=='clipped-domain'}))})};
    const frame={isDetached:()=>scenario==='detached',locator:selector=>selector==='tbody tr'?row:table,
      getByText:()=>({}),getByRole:()=>tip,url:()=>scenario==='oopif-empty-cache'?'':url,
      evaluate:async(fn,args)=>args?{text:expected.identifier+' '+expected.vpc_id,loading:false,facts_visible:true}:
        fn.toString().includes('document.location.href')?scenario==='route-changed'&&++reads>1?url.replace('offlinefs','other'):url:
          {painted:true},frameElement:async()=>({evaluate:async()=>({visible:scenario!=='embedding-clipped'}),dispose:async()=>{}})};
    const surface={bringToFront:async()=>actions.push('focus'),mouse:{move:async()=>actions.push('restore')},
      evaluate:async()=>scenario!=='background',screenshot:async options=>{
        assert.equal(options.fullPage,false);actions.push('pixels');if(scenario==='capture-failed')throw Error('capture failed');}};
    const options={mode:['iframe','embedding-clipped'].includes(scenario)?'iframe':'open',beforeCapture:async()=>{
      if(scenario==='session-changed')throw Error('session changed');}};
    if(!['open','iframe','background','oopif-empty-cache','embedding-clipped'].includes(scenario))
      await assert.rejects(()=>inspectNasMountTargetAddress(surface,frame,expected,'offline.png',options));
    else{
      const result=await inspectNasMountTargetAddress(surface,frame,expected,'offline.png',options);
      assert.equal(result.identity_visible,!['background','embedding-clipped'].includes(scenario));
      assert.equal(result.readonly_completed,true);assert.equal(result.original_landing_unchanged,true);
      assert.equal(result.cloud_writes,0);assert(!('pass' in result));
    }
    assert(actions.every(action=>['focus','scroll','hover','restore','pixels'].includes(action)));
    if(actions.includes('scroll'))assert.equal(actions.at(-1),'restore');
  });
test('explicit inspector dispatch, no clicks, edits, writes or viewport changes',()=>{
  const source=fs.readFileSync(new URL('../../scripts/web/e2e/resource_link_workflow/console_nas_mount_target.mjs',import.meta.url),'utf8');
  for(const forbidden of ['.goto(','.click(','.fill(','.setViewportSize(','.style.','aliyun_api'])assert(!source.includes(forbidden));
  const browser=fs.readFileSync(new URL('../../scripts/web/e2e/resource_link_workflow/browser.mjs',import.meta.url),'utf8');
  assert(browser.includes("(await import('./console_nas_mount_target.mjs')).inspectNasMountTargetAddress"));
});
