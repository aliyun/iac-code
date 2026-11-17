import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import {oosStateConsoleUrl,oosStateListReady,inspectOosStateConfigurationList}
  from '../../scripts/web/e2e/resource_link_workflow/console_oos_state_configuration.mjs';

const expected={kind:'oos_state_configuration_list',resource_type:'ALIYUN::OOS::StateConfiguration',
  identifier:'sc-offline',description:'fixture description',template_name:'ACS-Fixture',
  account:'1234567890123456',region:'cn-hangzhou',source_tool_use_id:'offline-call'};
const origin='https://oos4service.console.aliyun.com';
const edit='/cn-hangzhou/inventory/modify/sc-offline',list='/cn-hangzhou/inventory?tabType=detail';

test('only own domestic edit and native detail-list routes are allowed',()=>{
  assert.equal(oosStateConsoleUrl(origin+edit,expected).pathname,edit);
  assert.equal(oosStateConsoleUrl(origin+list,expected,'list').search,'?tabType=detail');
  for(const wrong of [origin+edit+'?edit=1',origin+edit+'/other',origin+edit+'#/execute',
    origin+edit.replace('cn-hangzhou','cn-beijing'),origin+edit.replace('sc-offline','sc-other'),
    (origin+edit).replace('https:','http:'),(origin+edit).replace('oos4service.','evil.')])
    assert.throws(()=>oosStateConsoleUrl(wrong,expected));
  for(const wrong of [origin+'/cn-hangzhou/inventory',origin+list+'&NextToken=other',origin+edit])
    assert.throws(()=>oosStateConsoleUrl(wrong,expected,'list'));
  for(const patch of [{account:'wrong'},{resource_type:'ALIYUN::OOS::Template'},{source_tool_use_id:''},
    {identifier:'../other'},{template_name:''},{description:''}])
    assert.throws(()=>oosStateConsoleUrl(origin+edit,{...expected,...patch}));
});

test('only a full visible own ID in its unique native row is ready',()=>{
  const view={loading:false,own_link:true,fields:[{marker:expected.identifier,visible:true}]};
  assert(oosStateListReady(view,expected));
  for(const patch of [{loading:true},{own_link:false},{fields:[]},
    {fields:[{marker:expected.identifier,visible:false}]},{fields:[{marker:'sc-other',visible:true}]}])
    assert(!oosStateListReady({...view,...patch},expected));
});

for(const scenario of ['open','iframe','duplicate-sidebar-label','wrong-href','foreign-href','ambiguous-anchor','ambiguous-row',
  'wrong-row-href','detached','session-changed','route-changed','occluded','loading','background','capture-failed'])
  test(`native list capture, no cloud writes: ${scenario}`,async()=>{
    let current=origin+edit;const actions=[];
    const ownLink={count:async()=>1,isVisible:async()=>true,
      getAttribute:async()=>scenario==='wrong-row-href'?edit.replace('sc-offline','sc-other'):edit};
    const anchor={waitFor:async()=>{},count:async()=>scenario==='ambiguous-anchor'?2:1,
      getAttribute:async()=>scenario==='wrong-href'?'/cn-hangzhou/inventory/delete/sc-offline':
        scenario==='foreign-href'?'https://oos.console.aliyun.com'+list:list,
      click:async()=>{actions.push('native-list-click');current=origin+list;}};
    const row={waitFor:async()=>{},count:async()=>scenario==='ambiguous-row'?2:1,isVisible:async()=>true,
      scrollIntoViewIfNeeded:async()=>actions.push('native-scroll'),evaluate:async()=>({loading:scenario==='loading',
        fields:[{marker:expected.identifier,visible:scenario!=='occluded'}]})};
    const frame={url:()=>current,isDetached:()=>scenario==='detached',getByRole:(_,opts)=>{
      assert.notEqual(opts.name,'配置清单管理','text-only selection would include the duplicate sidebar');return ownLink;},
      locator:selector=>({filter:opts=>{
        if(selector.startsWith('a[href=')){
          assert.equal(selector,'a[href="/cn-hangzhou/inventory?tabType=detail"]:visible');
          assert(opts.hasText.test('配置清单管理'));assert(!opts.hasText.test('配置清单管理其他'));return anchor;
        }
        assert.equal(selector,'tbody tr');return row;
      }}),waitForFunction:async()=>{},
      evaluate:async(fn,args)=>args?{text:expected.identifier,loading:false,facts_visible:true}:
        fn.toString().includes('document.location.href')?
          (scenario==='route-changed'&&current.includes('?')?origin+'/cn-beijing/inventory?tabType=detail':current):{painted:true},
      frameElement:async()=>({evaluate:async()=>({visible:true}),dispose:async()=>{}})};
    const surface={bringToFront:async()=>actions.push('focus'),waitForTimeout:async()=>{},
      evaluate:async()=>scenario!=='background',screenshot:async opts=>{
        assert.equal(opts.fullPage,false);actions.push('pixels');if(scenario==='capture-failed')throw Error('capture failed');}};
    const options={mode:scenario==='iframe'?'iframe':'open',beforeCapture:async()=>{
      if(scenario==='session-changed')throw Error('session changed');}};
    const failures=['wrong-href','foreign-href','ambiguous-anchor','ambiguous-row','wrong-row-href',
      'detached','session-changed','route-changed','capture-failed'];
    if(failures.includes(scenario))await assert.rejects(()=>inspectOosStateConfigurationList(surface,frame,expected,'offline.png',options));
    else{
      const result=await inspectOosStateConfigurationList(surface,frame,expected,'offline.png',options);
      assert.equal(result.identity_visible,!['occluded','loading','background'].includes(scenario));
      assert.equal(result.cloud_writes,0);assert.equal(result.readonly_completed,true);
      assert.equal(result.original_landing_preserved,true);assert(!('pass' in result));
    }
    if(['wrong-href','foreign-href','ambiguous-anchor','detached','session-changed'].includes(scenario))
      assert(!actions.includes('native-list-click'));
    assert(actions.every(x=>['focus','native-list-click','native-scroll','pixels'].includes(x)));
  });

test('browser dispatch is explicit; no generated URLs, cloud API, edits or altered pixels',()=>{
  const source=fs.readFileSync(new URL('../../scripts/web/e2e/resource_link_workflow/console_oos_state_configuration.mjs',import.meta.url),'utf8');
  for(const forbidden of ['.goto(','.fill(','.setViewportSize(','.style.','aliyun_api'])assert(!source.includes(forbidden));
  const browser=fs.readFileSync(new URL('../../scripts/web/e2e/resource_link_workflow/browser.mjs',import.meta.url),'utf8');
  assert(browser.includes("(await import('./console_oos_state_configuration.mjs')).inspectOosStateConfigurationList"));
});
