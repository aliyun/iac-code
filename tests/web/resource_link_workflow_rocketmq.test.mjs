import test from 'node:test';
import assert from 'node:assert/strict';
import {rocketmqNoticeUrl,ROCKETMQ_NOTICE_TITLE,inspectRocketmqNotice}
  from '../../scripts/web/e2e/resource_link_workflow/console_rocketmq.mjs';

const expected={kind:'rocketmq_instance_notice',resource_type:'ALIYUN::ROCKETMQ::Instance',dimension:'ROS',
  account:'1234567890123456',identifier:'MQ_INST_1234567890123456_Own',region:'cn-hangzhou',name:'offline-own',
  source_tool_use_id:'offline-member-tool'};
const url='https://ons.console.aliyun.com/region/cn-hangzhou/instance/MQ_INST_1234567890123456_Own/detail';
test('RocketMQ announcement capture remains bound to exact native clicked instance',()=>{
  assert.equal(rocketmqNoticeUrl(url,expected).href,url);
  assert.equal(ROCKETMQ_NOTICE_TITLE,'RocketMQ for AI：全新轻量主题模型上线');
  assert.equal(rocketmqNoticeUrl(url,{...expected,dimension:'QUERY'}).href,url);
});
for(const raw of [url.replace('Own','Business'),url.replace('cn-hangzhou','cn-beijing'),
  url.replace('https:','http:'),url.replace('ons.console.aliyun.com','example.com'),url+'?instance=other',
  url+'#other',url.replace('/detail','/release')]){
  test('reject unrelated URL '+raw,()=>assert.throws(()=>rocketmqNoticeUrl(raw,expected)));
}
for(const changed of [{account:'9999999999999999'},{name:''},{source_tool_use_id:''},
  {resource_type:'ALIYUN::VPC::VSwitch'},{dimension:'CREATE'}]){
  test('reject unbound notice identity '+JSON.stringify(changed),()=>
    assert.throws(()=>rocketmqNoticeUrl(url,{...expected,...changed})));
}
for(const scenario of ['notice','no-notice','ambiguous','close-hidden','close-failed','wrong-route']){
  test('native notice closes only its own close button: '+scenario,async()=>{
    const actions=[];let visible=scenario!=='no-notice';
    const close={count:async()=>1,isVisible:async()=>scenario!=='close-hidden',isEnabled:async()=>true,
      click:async()=>{actions.push('close-only');if(scenario==='close-failed')throw Error('close failed');visible=false;}};
    const dialog={count:async()=>scenario==='ambiguous'?2:scenario==='no-notice'?0:1,
      isVisible:async()=>visible,
      // The real Fusion DOM has two accessible 关闭 controls (footer + X).
      // Only the footer is an HTML button with that exact text.
      getByRole:()=>{throw Error('accessible close name is ambiguous');},
      locator:selector=>{assert.equal(selector,'button');return {filter:args=>{
        assert(args.hasText.test('关闭'));assert(!args.hasText.test('\ue626'));return close;}};},
      waitFor:async()=>assert.equal(visible,false)};
    const frame={isDetached:()=>false,url:()=>url,locator:()=>({filter:()=>dialog}),getByText:()=>({}),
      evaluate:async(fn,args)=>args?{text:expected.identifier+' '+expected.name,loading:false,facts_visible:true}:
        fn.toString().includes('document.location.href')?scenario==='wrong-route'?url+'/wrong':url:{painted:true}};
    const surface={bringToFront:async()=>actions.push('focus'),evaluate:async()=>true,screenshot:async opts=>{
      assert.equal(opts.fullPage,false);actions.push('pixels');}};
    if(['ambiguous','close-hidden','close-failed','wrong-route'].includes(scenario))
      await assert.rejects(()=>inspectRocketmqNotice(surface,frame,expected,'offline.png',{mode:'open'}));
    else{
      const result=await inspectRocketmqNotice(surface,frame,expected,'offline.png',{mode:'open'});
      assert.equal(result.readonly_completed,true);assert.equal(result.cloud_writes,0);
      assert.equal(result.identity_visible,true);
      assert.equal(result.announcement_dismissed,scenario==='notice');assert(!('pass' in result));
    }
    assert(actions.every(action=>['focus','close-only','pixels'].includes(action)));
  });
}
