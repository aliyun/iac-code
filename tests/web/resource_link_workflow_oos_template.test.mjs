import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import {oosTemplateConsoleUrl,oosTemplateMetadataReady,oosDescriptionColumn,inspectOosTemplateMetadata}
  from '../../scripts/web/e2e/resource_link_workflow/console_oos_template.mjs';

const expected={kind:'oos_template_metadata',resource_type:'ALIYUN::OOS::Template',identifier:'offline-template',
  template_id:'t-offline',description:'offline description',region:'cn-hangzhou',source_tool_use_id:'offline-call'};
const url='https://oos.console.aliyun.com/cn-hangzhou/template/private/detail/offline-template';

test('only exact canonical private template on the two observed domestic hosts',()=>{
  assert.equal(oosTemplateConsoleUrl(url,expected).href,url);
  assert.equal(oosTemplateConsoleUrl(url.replace('oos.','oos4service.')+'#/',expected).hash,'#/');
  for(const patch of [{kind:'other'},{resource_type:'alicloud_oos_template'},{identifier:'../other'},
    {description:''},{description:expected.identifier},{template_id:''},{region:'us-east-1'},{source_tool_use_id:''}])
    assert.throws(()=>oosTemplateConsoleUrl(url,{...expected,...patch}));
  for(const wrong of [url.replace('oos.','evil.'),url.replace('cn-hangzhou','cn-beijing'),
    url.replace('/private/','/public/'),url+'?other=1',url+'#/execute',url.replace('https:','http:'),url+'/'])
    assert.throws(()=>oosTemplateConsoleUrl(wrong,expected),/unbound/);
});

test('labeled, unoccluded and completely rendered fields are mandatory',()=>{
  const view={loading:false,labeled_fields:true,fields:[expected.identifier,expected.description].map(marker=>({marker,visible:true}))};
  assert(oosTemplateMetadataReady(view,expected));
  for(const patch of [{loading:true},{labeled_fields:false},{fields:[]},
    ...view.fields.map(f=>({fields:view.fields.map(x=>({...x,visible:x.marker!==f.marker}))}))])
    assert(!oosTemplateMetadataReady({...view,...patch},expected));
});

test('only the corresponding column in the real vertical description table is sampled',()=>{
  const values={tagName:'TR',cells:[0,1,2].map(()=>({tagName:'TD',colSpan:1}))};
  const row={tagName:'TR',cells:[0,1,2].map(cellIndex=>({tagName:'TH',colSpan:1,cellIndex})),nextElementSibling:values};
  row.cells.forEach(th=>th.parentElement=row);
  const label={closest:()=>row.cells[1]};assert.equal(oosDescriptionColumn(label),1);
  values.cells[1].colSpan=2;
  assert.throws(()=>oosDescriptionColumn(label),/layout changed/);
  values.cells[1].colSpan=1;values.cells.pop();
  assert.throws(()=>oosDescriptionColumn(label),/layout changed/);
});

for(const scenario of ['collapsed','expanded','ambiguous-header','ambiguous-label','detached','route-changed',
  'session-changed','occluded','loading','background','capture-failed','iframe'])
  test(`native disclosure preserves original landing and never writes: ${scenario}`,async()=>{
    let expanded=scenario==='expanded',reads=0,urls=0;const actions=[];
    const actual=()=>scenario==='route-changed'&&++urls>1?url.replace('/private/','/public/'):url;
    const cell=marker=>({count:async()=>1,isVisible:async()=>expanded,evaluate:async()=>({loading:scenario==='loading',
      fields:[{marker,visible:scenario!=='occluded'}]})});
    const header={waitFor:async()=>{},count:async()=>scenario==='ambiguous-header'?2:1,
      locator:()=>({count:async()=>expanded?0:1}),click:async()=>{actions.push('native_expand');expanded=true;}};
    const frame={url:actual,isDetached:()=>scenario==='detached',getByText:()=>header,
      locator:()=>({filter:({hasText})=>({count:async()=>scenario==='ambiguous-label'?2:1,isVisible:async()=>expanded,
        evaluate:async()=>0,locator:()=>({locator:()=>({nth:()=>
          cell(hasText.source.includes('名称')?expected.identifier:expected.description)})})})}),
      evaluate:async(fn,args)=>args?{text:expected.identifier+' '+expected.description,loading:false,facts_visible:true}:
        fn.toString().includes('document.location.href')?actual():{painted:true},
      frameElement:async()=>({evaluate:async()=>({visible:true}),dispose:async()=>{}})};
    const surface={bringToFront:async()=>actions.push('focus'),waitForTimeout:async()=>{},
      evaluate:async()=>scenario!=='background',screenshot:async opts=>{
        assert.equal(opts.fullPage,false);actions.push('pixels');if(scenario==='capture-failed')throw Error('capture failed');}};
    const options={mode:scenario==='iframe'?'iframe':'open',beforeCapture:async()=>{
      reads++;if(scenario==='session-changed')throw Error('session changed');}};
    const failures={'ambiguous-header':/ambiguous/,detached:/detached/,'route-changed':/unbound/,
      'session-changed':/session changed/,'capture-failed':/capture failed/};
    if(scenario in failures)await assert.rejects(()=>inspectOosTemplateMetadata(surface,frame,expected,'offline.png',options),failures[scenario]);
    else{
      const result=await inspectOosTemplateMetadata(surface,frame,expected,'offline.png',options);
      assert.equal(result.identity_visible,!['ambiguous-label','occluded','loading','background'].includes(scenario));
      assert.equal(result.original_landing_unchanged,true);assert.equal(result.readonly_completed,true);
      assert.equal(result.cloud_writes,0);assert(!('pass' in result));assert(reads>1);
      assert.equal(actions.filter(x=>x==='native_expand').length,scenario==='expanded'?0:1);
    }
    assert(actions.every(x=>['focus','pixels','native_expand'].includes(x)));
  });

test('sampler has no URL navigation, editing, resizing or hidden DOM path',()=>{
  const source=fs.readFileSync(new URL('../../scripts/web/e2e/resource_link_workflow/console_oos_template.mjs',import.meta.url),'utf8');
  for(const forbidden of ['.goto(','.fill(','.setViewportSize(','.style.','aliyun_api','执行模板'])assert(!source.includes(forbidden));
  const browser=fs.readFileSync(new URL('../../scripts/web/e2e/resource_link_workflow/browser.mjs',import.meta.url),'utf8');
  assert(browser.includes("(await import('./console_oos_template.mjs')).inspectOosTemplateMetadata"));
});
