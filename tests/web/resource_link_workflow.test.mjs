import test from 'node:test';
import './resource_link_workflow_keypair.test.mjs';
import './resource_link_workflow_resource_group.test.mjs';
import './resource_link_workflow_refresh.test.mjs';
import assert from 'node:assert/strict';
import {allowedNaturalRequest,bareOrigin,ownsTarget,operate,selectOccurrence,waitForNavigationIdentity,navigationReady,LOADING_SELECTOR,captureNavigationPixels,withForegroundLock} from '../../scripts/web/e2e/resource_link_workflow/browser.mjs';
import {resolvePreviewConsoleFrame,loadingSignalPending} from '../../scripts/web/e2e/resource_link_workflow/browser.mjs';
import {verifyPolicy,scriptPolicy,runAdapter,readJob,readIdleThread} from '../../scripts/web/e2e/resource_link_workflow/adapter.mjs';
import http from 'node:http';
import {createHash} from 'node:crypto';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {nasMetadataReady,inspectNasMetadata} from '../../scripts/web/e2e/resource_link_workflow/browser.mjs';
import {slbAclConsoleUrl,slbAclSidebarUrl,slbAclMetadataReady,aclHorizontalDelta,inspectSlbAclMetadata} from '../../scripts/web/e2e/resource_link_workflow/browser.mjs';

test('SLB ACL supplemental paths bind actual domestic console, exact ID and readonly list',()=>{
  const expected={identifier:'acl-fixture123',region:'cn-hangzhou'};
  for(const host of ['slb','slb4service']){
    assert(slbAclConsoleUrl(`https://${host}.console.aliyun.com/slb/cn-hangzhou/acls/acl-fixture123`,expected));
    assert(slbAclConsoleUrl(`https://${host}.console.aliyun.com/slb/cn-hangzhou/acls?spm=observed`,expected,true));
  }
  for(const raw of ['', 'https://unrelated.invalid/slb/cn-hangzhou/acls/acl-fixture123',
    'https://slb.console.aliyun.com/slb/cn-shanghai/acls/acl-fixture123',
    'https://slb.console.aliyun.com/slb/cn-hangzhou/acls/acl-other',
    'https://slb.console.aliyun.com/slb/cn-hangzhou/acls/acl-fixture123?redirect=other',
    'http://slb.console.aliyun.com/slb/cn-hangzhou/acls/acl-fixture123',
    'https://user@slb.console.aliyun.com/slb/cn-hangzhou/acls/acl-fixture123'])
    assert.throws(()=>slbAclConsoleUrl(raw,expected),/unbound SLB/);
});

test('SLB column scrolling aligns full text inside real sticky-column gap, not DOM hiding',()=>{
  const expected={identifier:'acl-fixture123',name:'fixture-acl',address_family:'IPv4'};
  const view={loading:false,scroll:{left:532,right:575,current:50,maximum:637},
    fields:[{marker:expected.identifier,visible:true},{marker:expected.name,visible:true},
      {marker:'IPv4',visible:false,left:578,right:603}]};
  assert.equal(aclHorizontalDelta(view,expected),37);
  assert.equal(slbAclMetadataReady(view,expected),false);
  assert.equal(slbAclMetadataReady({...view,fields:view.fields.map(x=>({...x,visible:true}))},expected),true);
  assert.equal(slbAclMetadataReady({...view,loading:true},expected),false);
  assert.throws(()=>aclHorizontalDelta({...view,scroll:{...view.scroll,right:540}},expected),/unoccluded/);
  assert.throws(()=>aclHorizontalDelta({...view,scroll:{...view.scroll,maximum:60}},expected),/unavailable/);
});

test('normal-console hydration placeholder is never an approved list destination',()=>{
  const expected={identifier:'acl-fixture123',region:'cn-hangzhou'};
  const normal=slbAclConsoleUrl('https://slb.console.aliyun.com/slb/cn-hangzhou/acls/acl-fixture123',expected);
  assert.throws(()=>slbAclSidebarUrl('/slb',normal,expected),/unbound/);
  assert.throws(()=>slbAclConsoleUrl('https://slb.console.aliyun.com/slb',expected,true),/unbound/);
  const mini=slbAclConsoleUrl('https://slb4service.console.aliyun.com/slb/cn-hangzhou/acls/acl-fixture123',expected);
  assert.throws(()=>slbAclSidebarUrl('/slb',mini,expected),/unbound/);
  assert.throws(()=>slbAclSidebarUrl('/slb?redirect=elsewhere',normal,expected),/unbound/);
});

for(const scenario of ['exact','normal-placeholder','oopif-empty-cache','scroll','wrong-host','wrong-id','wrong-region','bad-sidebar','sidebar-ambiguous','row-ambiguous','wrong-name','wrong-family','occluded','session-changed','late-session-changed','capture-failed'])
  test(`SLB source-bound readonly supplemental pixels: ${scenario}`,async()=>{
    const actions=[];let listed=false,scrolled=false,checks=0,hrefReads=0;
    const expected={kind:'slb_acl_metadata',identifier:'acl-fixture123',name:'fixture-acl',address_family:'IPv4',region:'cn-hangzhou'};
    const base=`https://${scenario==='normal-placeholder'?'slb':'slb4service'}.console.aliyun.com/slb/cn-hangzhou/acls`;
    const location=()=>scenario==='wrong-host'?'https://unrelated.invalid/':
      scenario==='wrong-region'?base.replace('cn-hangzhou','cn-shanghai')+'/acl-fixture123':
      base+(listed?'':'/'+(scenario==='wrong-id'?'acl-other':expected.identifier));
    const row={waitFor:async()=>{},count:async()=>scenario==='row-ambiguous'?2:1,
      getByText:marker=>({count:async()=>scenario==='wrong-name'&&marker===expected.name||
        scenario==='wrong-family'&&marker===expected.address_family?0:1}),hover:async()=>actions.push('hover'),
      evaluate:async(_fn,args)=>({loading:false,scroll:{left:532,right:575,current:50,maximum:637},
        fields:args.markers.map(marker=>({marker,visible:scenario!=='occluded'&&
          (scenario!=='scroll'||scrolled||marker!==expected.address_family),left:578,right:603}))})};
    const sidebar={count:async()=>scenario==='sidebar-ambiguous'?2:1,isVisible:async()=>true,
      getAttribute:async()=>scenario==='normal-placeholder'&&++hrefReads===1?'/slb':'/slb/cn-hangzhou/acls'+(scenario==='bad-sidebar'?'?redirect=other':'?spm=observed'),
      click:async()=>{listed=true;actions.push('sidebar');}};
    const frame={url:()=>scenario==='oopif-empty-cache'?'':location(),
      evaluate:async(_fn,args)=>args?{text:[expected.identifier,expected.name,expected.address_family].join(' '),loading:false,facts_visible:true}:
        _fn.toString().includes('location.href')?location():{painted:true},
      locator:selector=>selector==='a[data-spm-id="menu.acl"]'?sidebar:{filter:()=>row},getByText:()=>({})};
    const surface={bringToFront:async()=>actions.push('focus'),evaluate:async()=>true,
      mouse:{wheel:async(dx,dy)=>{assert.equal(dx,37);assert.equal(dy,0);scrolled=true;actions.push('scroll');}},
      screenshot:async()=>{actions.push('pixels');if(scenario==='capture-failed')throw Error('capture failed');}};
    const beforeCapture=async()=>{checks++;if(scenario==='session-changed'||scenario==='late-session-changed'&&checks>1)throw Error('session mismatch');};
    if(['wrong-host','wrong-id','wrong-region','bad-sidebar','sidebar-ambiguous','row-ambiguous','wrong-name','wrong-family','session-changed','late-session-changed','capture-failed'].includes(scenario))
      await assert.rejects(()=>inspectSlbAclMetadata(surface,frame,expected,'metadata.png',{mode:'open',beforeCapture}));
    else{
      const result=await inspectSlbAclMetadata(surface,frame,expected,'metadata.png',{mode:'open',beforeCapture});
      assert.equal(result.identity_visible,scenario!=='occluded');
      assert.equal(result.readonly_completed,true);assert.equal(result.cloud_writes,0);
      assert.equal(result.original_landing_unchanged,true);assert(!('dialog_cancelled' in result));assert(!('pass' in result));
    }
    assert(actions.every(x=>['focus','sidebar','hover','scroll','pixels'].includes(x)));
  });

test('NAS form values require visible exact source-bound name/description and disabled name',()=>{
  const expected={identifier:'fixture-group',description:'fixture-description'};
  const view={name:expected.identifier,description:expected.description,name_disabled:true,
    name_visible:true,description_visible:true};
  assert.equal(nasMetadataReady(view,expected),true);
  for(const patch of [{name:'other-group'},{description:'other'},{name_disabled:false},
    {name_visible:false},{description_visible:false}])assert.equal(nasMetadataReady({...view,...patch},expected),false);
});

for(const scenario of ['exact','shift-console','mini-console','sidebar-and-breadcrumb','breadcrumb-only','ambiguous-sidebar','analytics-sidebar','unapproved-query','wrong-description','capture-error','wrong-host'])
  test(`NAS supplemental pixels preserve real landing and cancel only: ${scenario}`,async()=>{
    const actions=[];
    const expected={kind:'nas_access_group_metadata',identifier:'fixture-group',
      description:'fixture-description',region:'cn-hangzhou'};
    const control=label=>({count:async()=>1,isVisible:async()=>true,
      click:async()=>actions.push(label),waitFor:async()=>{}});
    const field=(value,disabled=false)=>({inputValue:async()=>value,isDisabled:async()=>disabled,
      evaluate:async()=>true});
    const dialog={...control('dialog'),getByRole:(_role,{name})=>{
      assert.equal(name,'取消');return control('cancel');},
      locator:selector=>selector==='input[name="AccessGroupName"]'?field(expected.identifier,true):
        field(scenario==='wrong-description'?'wrong-description':expected.description)};
    const row={...control('row'),getByRole:(_role,{name})=>{
      assert.equal(name,'编辑');return control('edit');}};
    const frame={url:()=>scenario==='wrong-host'?'https://unrelated.invalid/':
      `https://${scenario==='mini-console'?'nasnext4service':scenario==='shift-console'?'nasnext':'nas'}.console.aliyun.com/cn-hangzhou/access-group/standard/fixture-group`,
      getByRole:(_role,{name})=>{
        assert.equal(name,'权限组');
        const sidebar={...control('sidebar'),evaluate:async fn=>fn({closest:selector=>
          selector==='.next-nav-item'?{}:null}),getAttribute:async()=>'/cn-hangzhou/access-group'+
          (scenario==='analytics-sidebar'?'?spm=actual-observed':scenario==='unapproved-query'?'?redirect=other':'')};
        const breadcrumb={...control('breadcrumb'),evaluate:async fn=>fn({closest:selector=>
          selector==='.next-breadcrumb'?{}:null}),
          getAttribute:async()=>'/cn-hangzhou/access-group/standard'};
        return {all:async()=>scenario==='sidebar-and-breadcrumb'?[sidebar,breadcrumb]:
          scenario==='breadcrumb-only'?[breadcrumb]:scenario==='ambiguous-sidebar'?
            [sidebar,{...sidebar,getAttribute:async()=>'/cn-hangzhou/access-group/standard'}]:[sidebar]};},
      getByText:(name,{exact})=>{assert.equal(name,expected.identifier);assert.equal(exact,true);return {};},
      locator:selector=>({filter:()=>selector==='tr'?row:dialog}),
      evaluate:async(_fn,args)=>args?{text:expected.identifier,loading:false,facts_visible:true}:{painted:true}};
    const surface={bringToFront:async()=>actions.push('focus'),evaluate:async()=>true,
      screenshot:async options=>{
        actions.push('pixels');assert.equal(options.fullPage,false);
        if(scenario==='capture-error')throw Error('capture failed');}};
    if(['capture-error','wrong-host','unapproved-query','breadcrumb-only','ambiguous-sidebar'].includes(scenario))
      await assert.rejects(()=>inspectNasMetadata(surface,frame,expected,'metadata.png',{mode:'open'}));
    else{
      const result=await inspectNasMetadata(surface,frame,expected,'metadata.png',{mode:'open'});
      assert.equal(result.identity_visible,['exact','shift-console','mini-console','sidebar-and-breadcrumb','analytics-sidebar'].includes(scenario));
      assert.equal(result.dialog_cancelled,true);assert.equal(result.original_landing_unchanged,true);
      assert.equal(result.cloud_writes,0);assert.equal(result.screenshot,'metadata.png');
      assert.equal(result.stages[0].step,'resource_link_landing');assert(!('pass' in result));
    }
    assert.deepEqual(actions.filter(x=>!['focus','pixels'].includes(x)),
      ['wrong-host','unapproved-query','breadcrumb-only','ambiguous-sidebar'].includes(scenario)?[]:['sidebar','edit','cancel']);
    const source=fs.readFileSync(new URL('../../scripts/web/e2e/resource_link_workflow/browser.mjs',import.meta.url),'utf8')
      .split('export async function inspectNasMetadata')[1].split('export async function withForegroundLock')[0];
    assert(!source.includes('.fill('));assert(!source.includes('.goto('));
    assert(!source.includes("name:'确定'"));assert(!source.includes("name:'删除'"));
  });

for (const scenario of ['terminal','older-active','bad-page','repeated-cursor','duplicate-turn','unknown-status'])
  test(`idle worker check uses complete metadata-only pagination: ${scenario}`,async()=>{
    const calls=[];
    const rpc={call:async(method,params)=>{
      calls.push({method,params});
      if(method==='thread/read'){
        assert.deepEqual(params,{threadId:'original-worker',includeTurns:false});
        return {thread:{id:'original-worker',status:{type:'notLoaded'},path:'/original/sessions/log'}};
      }
      assert.equal(method,'thread/turns/list');assert.equal(params.itemsView,'notLoaded');
      if(!params.cursor)return {data:[{id:'newest',status:'completed'}],nextCursor:'older'};
      if(scenario==='bad-page')return {nextCursor:null};
      return {data:[{id:scenario==='duplicate-turn'?'newest':'oldest',
        status:scenario==='older-active'?'inProgress':scenario==='unknown-status'?'unknown':'completed'}],
        nextCursor:scenario==='repeated-cursor'?'older':null};
    }};
    if(scenario==='terminal')assert.equal((await readIdleThread(rpc,'original-worker')).thread.id,'original-worker');
    else await assert.rejects(()=>readIdleThread(rpc,'original-worker'),/active or unknown|pagination conflict|history unavailable/);
    assert.equal(calls.length,3);
    assert(!calls.some(call=>call.method==='thread/resume'||call.method==='turn/start'));
  });

test('model adapters never interrupt a turn on a wall-clock deadline',()=>{
  const source=fs.readFileSync(new URL('../../scripts/web/e2e/resource_link_workflow/adapter.mjs',import.meta.url),'utf8');
  assert(!source.includes("rpc.call('turn/interrupt'"));
  assert(!source.includes('20*60*1000'));
  assert(source.includes('const completed = await terminal;'));
});

test('navigation screenshots focus the precise surface, paint, and preserve live viewport',async()=>{
  const calls=[];
  const surface={bringToFront:async()=>calls.push('focus'),
    evaluate:async()=>calls.push('paint'),screenshot:async options=>calls.push(options)};
  await captureNavigationPixels(surface,'original-evidence.png');
  assert.deepEqual(calls,['focus','paint',{path:'original-evidence.png',fullPage:false}]);
  const saved=globalThis.requestAnimationFrame;
  let painted=0;
  globalThis.requestAnimationFrame=callback=>{painted++;callback();};
  try {
    surface.evaluate=callback=>callback();
    await captureNavigationPixels(surface,'next-original.png');
    assert.equal(painted,2);
  } finally {globalThis.requestAnimationFrame=saved;}
});

test('foreground capture serializes only the short paint/screenshot across targets',async()=>{
  const root=fs.mkdtempSync(path.join(os.tmpdir(),'resource-link-foreground-'));
  const lockFile=path.join(root,'foreground.lock'),calls=[];
  let release;
  const firstPaint=new Promise(resolve=>{release=resolve;});
  const first={bringToFront:async()=>calls.push('one-focus'),evaluate:async()=>{calls.push('one-paint');await firstPaint;},
    screenshot:async()=>calls.push('one-screenshot')};
  const second={bringToFront:async()=>calls.push('two-focus'),evaluate:async()=>calls.push('two-paint'),
    screenshot:async()=>calls.push('two-screenshot')};
  try{
    const one=captureNavigationPixels(first,'one.png',{lockFile});
    const two=captureNavigationPixels(second,'two.png',{lockFile});
    await new Promise(resolve=>setTimeout(resolve,30));
    assert.deepEqual(calls,['one-focus','one-paint']);
    release();await Promise.all([one,two]);
    assert.deepEqual(calls,['one-focus','one-paint','one-screenshot','two-focus','two-paint','two-screenshot']);
    assert(!fs.existsSync(lockFile));
    await assert.rejects(()=>withForegroundLock(lockFile,async()=>{throw Error('original failure');}),/original failure/);
    assert(!fs.existsSync(lockFile));
  }finally{release();fs.rmSync(root,{recursive:true,force:true});}
});

test('capture paints matched console frame and revalidates facts after focus without manufacturing PASS',async()=>{
  const calls=[];
  const frame={url:()=> 'https://ecs.console.aliyun.com/exact',evaluate:async(_fn,args)=>{
    calls.push(args?'console-facts':'console-paint');return args?{text:'eni-real rl-real',loading:false,facts_visible:true}:{painted:false};},
    frameElement:async()=>({evaluate:async()=>({visible:true}),dispose:async()=>{}})};
  const surface={bringToFront:async()=>calls.push('focus'),evaluate:async()=>{calls.push('main');return true;},
    screenshot:async()=>calls.push('pixels'),mainFrame:()=>null,frames:()=>[frame]};
  const result=await captureNavigationPixels(surface,'original.png',{mode:'iframe',identifier:'eni-real',
    markers:['rl-real'],consoleFrame:frame,beforeCapture:async()=>calls.push('sealed-session')});
  assert.deepEqual(calls,['focus','main','console-paint','sealed-session','console-facts','main','pixels']);
  assert.equal(result.console_paint.painted,false);
  assert.equal(result.render_check.identity_visible,true);
  assert(!('pass' in result)); // DOM success and timeout never supersede independent pixels.
  const other={url:()=> 'https://unrelated.example/',evaluate:async()=>({text:'eni-real rl-real',loading:false})};
  assert.equal((await waitForNavigationIdentity({...surface,frames:()=>[other]},'iframe','eni-real',0,['rl-real'],[])).identity_visible,false);
});

test('preview binding re-resolves only the leased embedding and preserves origin/session barriers',async()=>{
  let frame={id:'old'},session='sealed',origin='http://127.0.0.1:16102',count=1,visible=true,disposals=0;
  const selectors=[];
  const page={url:()=>origin,locator:selector=>{
    selectors.push(selector);
    if(selector==='.session-item.is-active[data-session-id]')return {count:async()=>1,getAttribute:async()=>session};
    assert.equal(selector,'.resource-link-preview-body iframe');
    return {count:async()=>count,isVisible:async()=>visible,elementHandle:async()=>({
      contentFrame:async()=>{if(frame==='detached')throw Error('detached');return frame;},
      dispose:async()=>{disposals++;}
    })};
  }};
  assert.equal((await resolvePreviewConsoleFrame(page,origin,'sealed')).id,'old');
  frame={id:'replacement'};
  assert.equal((await resolvePreviewConsoleFrame(page,origin,'sealed')).id,'replacement');
  frame='detached';assert.equal(await resolvePreviewConsoleFrame(page,origin,'sealed'),null);
  assert.equal(disposals,3);
  count=0;assert.equal(await resolvePreviewConsoleFrame(page,origin,'sealed'),null);
  count=2;await assert.rejects(()=>resolvePreviewConsoleFrame(page,origin,'sealed'),/ambiguous/);
  count=1;visible=false;assert.equal(await resolvePreviewConsoleFrame(page,origin,'sealed'),null);
  session='another';await assert.rejects(()=>resolvePreviewConsoleFrame(page,origin,'sealed'),/session changed/);
  session='sealed';origin='http://127.0.0.1:16103';
  await assert.rejects(()=>resolvePreviewConsoleFrame(page,'http://127.0.0.1:16102','sealed'),/origin changed/);
  assert(selectors.every(s=>s==='.session-item.is-active[data-session-id]'||s==='.resource-link-preview-body iframe'));
});

test('bounded identity wait refreshes the exact embedding after frame detach, not arbitrary frames',async()=>{
  let reads=0;
  const detached={url:()=> 'about:blank',evaluate:async()=>{throw Error('detached');}};
  const current={url:()=> 'https://slb4service.console.aliyun.com/exact',evaluate:async()=>({text:'acl-fixture name IPv4',loading:false,facts_visible:true})};
  const unrelated={url:()=> 'https://unrelated.invalid',evaluate:async()=>{throw Error('must never inspect unrelated frame');}};
  const surface={frames:()=>[unrelated]};
  const result=await waitForNavigationIdentity(surface,'iframe','acl-fixture',1000,['name','IPv4'],
    async()=>[++reads===1 ? detached : current]);
  assert.equal(reads,2);
  assert.equal(result.identity_visible,true);
  assert.deepEqual(result.frames.map(f=>f.url),['https://slb4service.console.aliyun.com/exact']);
  const missing=await waitForNavigationIdentity(surface,'iframe','acl-fixture',0,[],async()=>[]);
  assert.equal(missing.identity_visible,false);
  assert.deepEqual(missing.frames,[]);
});

test('capture refreshes the sealed preview after focus and never paints its stale frame',async()=>{
  const calls=[];
  const stale={evaluate:async()=>{throw Error('stale frame used');}};
  const current={url:()=> 'https://slb4service.console.aliyun.com/exact',
    evaluate:async(_fn,args)=>{calls.push(args?'facts':'console-paint');return args?
      {text:'acl-fixture name IPv4',loading:false,facts_visible:true}:{painted:true};},
    frameElement:async()=>({evaluate:async()=>({visible:true}),dispose:async()=>{}})};
  const surface={bringToFront:async()=>calls.push('focus'),evaluate:async()=>{calls.push('main');return true;},
    screenshot:async()=>calls.push('pixels')};
  const result=await captureNavigationPixels(surface,'original.png',{mode:'iframe',identifier:'acl-fixture',
    markers:['name','IPv4'],consoleFrame:stale,beforeCapture:async()=>calls.push('sealed-session'),
    resolveConsoleFrame:async()=>{calls.push('exact-embedding');return current;}});
  assert.deepEqual(calls,['focus','sealed-session','exact-embedding','main','console-paint','facts','main','pixels']);
  assert.equal(result.frame_embedding.visible,true);
  assert.equal(result.render_check.identity_visible,true);
  assert(!('pass' in result));
});

test('bare origin cannot contain secrets, nonlocal hosts, paths or query',()=>{
  assert.equal(bareOrigin('http://127.0.0.1:16100'),'http://127.0.0.1:16100');
  for(const origin of ['http://example.com','http://127.0.0.1:16100/x','http://127.0.0.1:16100?token=private'])
    assert.throws(()=>bareOrigin(origin));
});
test('repeated exact links select one frozen real occurrence; different targets fail closed',()=>{
  const a={index:0,href:'https://vpc.console.aliyun.com/actual',focus:'first'};
  const b={...a,index:2,focus:'last'};
  assert.deepEqual(selectOccurrence([a,b]),b);
  assert.deepEqual(selectOccurrence([a,b],a),a);
  assert.throws(()=>selectOccurrence([a,{...b,href:'https://vpc.console.aliyun.com/other'}]));
  assert.throws(()=>selectOccurrence([{...a,href:null}]));
  assert.throws(()=>selectOccurrence([b],a));
});
test('capture waits for real console identity, not outer Web header or DOM shell',async()=>{
  const main={url:()=> 'http://127.0.0.1:16100/',evaluate:async()=> ({text:'preview vsw-real',loading:false})};
  const frame={url:()=> 'https://vpc.console.aliyun.com/actual',evaluate:async()=> ({text:'loading shell',loading:true})};
  const surface={mainFrame:()=>main,frames:()=>[main,frame]};
  assert.equal((await waitForNavigationIdentity(surface,'iframe','vsw-real',0)).identity_visible,false);
  frame.evaluate=async()=> ({text:'交换机详情 vsw-real 已登录',loading:false});
  assert.equal((await waitForNavigationIdentity(surface,'iframe','vsw-real',0)).identity_visible,true);
  main.evaluate=async()=> ({text:'loading shell',loading:true});
  assert.equal((await waitForNavigationIdentity(surface,'open','vsw-real',0)).identity_visible,false);
  main.evaluate=async()=> ({text:'交换机详情 vsw-real',loading:false});
  assert.equal((await waitForNavigationIdentity(surface,'open','vsw-real',0)).identity_visible,true);
});
test('breadcrumb and spinner cannot satisfy independently bound detail facts',()=>{
  const markers=['rl-test-name','192.168.240.56/29'];
  assert.equal(navigationReady({text:'交换机/vsw-real 删除 路由管理',loading:false},'vsw-real',markers),false);
  assert.equal(navigationReady({text:'vsw-real rl-test-name 192.168.240.56/29',loading:true},'vsw-real',markers),false);
  assert.equal(navigationReady({text:'vsw-real rl-test-name 192.168.240.56/29',loading:false},'vsw-real',markers),true);
});
test('persistent Fusion content wrapper is not an active loading indicator',()=>{
  assert(!LOADING_SELECTOR.split(',').includes('.next-loading'));
  assert(!LOADING_SELECTOR.split(',').includes('.xcomponent-loading'));
  assert(!LOADING_SELECTOR.split(',').includes('.xcomponent-loading-wrap'));
  assert(LOADING_SELECTOR.includes('.next-loading-indicator'));
  assert(LOADING_SELECTOR.includes('[aria-busy="true"]'));
});
test('completed resource progress does not suppress real detail, while pending and explicit loaders remain busy',()=>{
  const completed={role:'progressbar',explicit_loading:false,aria_busy:null,value_now:null,value_max:null,
    class_name:'ercStorage-progress ercStorage-progress-status-success ercStorage-progress-show-info',text:'100%'};
  assert.equal(loadingSignalPending(completed),false);
  assert.equal(loadingSignalPending({...completed,text:'99%'}),true);
  assert.equal(loadingSignalPending({...completed,class_name:'ercStorage-progress',text:'100%'}),true);
  assert.equal(loadingSignalPending({...completed,explicit_loading:true}),true);
  assert.equal(loadingSignalPending({...completed,aria_busy:'true'}),true);
  assert.equal(loadingSignalPending({...completed,role:'status'}),true);
  assert.equal(loadingSignalPending({...completed,class_name:'',text:'',value_now:'10',value_max:'10'}),false);
  assert.equal(loadingSignalPending({...completed,class_name:'',text:'',value_now:'9',value_max:'10'}),true);
  assert.equal(loadingSignalPending({...completed,class_name:'',text:'',value_now:'0',value_max:'0'}),true);
  assert.equal(loadingSignalPending({...completed,class_name:'',text:'',value_now:'',value_max:'100'}),true);
});
test('actual ECS completed progress DOM probe allows facts but never masks pending loading signals',async()=>{
  const saved={document:globalThis.document,NodeFilter:globalThis.NodeFilter,getComputedStyle:globalThis.getComputedStyle,
    innerWidth:globalThis.innerWidth,innerHeight:globalThis.innerHeight};
  const fact={parentElement:null,getClientRects:()=>[{}],contains:()=>false};
  let explicit=false,text='100%',success=true;
  const indicator={parentElement:null,getClientRects:()=>[{}],getAttribute:name=>name==='role'?'progressbar':null,
    matches:()=>explicit,get innerText(){return text;},
    get className(){return success?'ercStorage-progress ercStorage-progress-status-success':'ercStorage-progress';}};
  globalThis.getComputedStyle=()=>({visibility:'visible',display:'block',opacity:'1'});
  globalThis.NodeFilter={SHOW_TEXT:4};globalThis.innerWidth=1200;globalThis.innerHeight=850;
  const nodes=['s-fixture123','fixture-name','d-fixture123'].map(textContent=>({textContent,parentElement:fact}));
  globalThis.document={body:{innerText:nodes.map(n=>n.textContent).join(' ')},querySelectorAll:()=>[indicator],
    createRange:()=>({setStart:()=>{},setEnd:()=>{},getClientRects:()=>[{left:10,top:10,right:200,bottom:30}]}),
    elementFromPoint:()=>fact,
    createTreeWalker:()=>{let index=0;return {nextNode(){this.currentNode=nodes[index++];return !!this.currentNode;}};}};
  const frame={url:()=> 'https://ecs4service.console.aliyun.com/fixture',evaluate:async(callback,args)=>callback(args)};
  const surface={mainFrame:()=>frame,frames:()=>[frame]};
  const probe=()=>waitForNavigationIdentity(surface,'open','s-fixture123',0,['fixture-name','d-fixture123']);
  try{
    let result=await probe();
    assert.equal(result.frames[0].loading,false);assert.equal(result.identity_visible,true);
    assert.equal(result.frames[0].loading_signals[0].text,'100%');assert(!('pass' in result));
    explicit=true;assert.equal((await probe()).identity_visible,false);
    explicit=false;text='50%';assert.equal((await probe()).identity_visible,false);
    text='100%';success=false;assert.equal((await probe()).identity_visible,false);
  }finally{Object.assign(globalThis,saved);}
});
for(const [className,shown,busy] of [
  ['xcomponent-loading-tip',true,true],
  ['xcomponent-loading-indicator',true,true],
  ['xcomponent-loading-masker',true,true],
  ['xcomponent-loading-tip',false,false],
  ['xcomponent-loading',true,false],
  ['xcomponent-loading-wrap',true,false],
])test(`actual OSS xcomponent DOM probe preserves busy versus retained wrapper: ${className}/${shown}`,async()=>{
  const saved={document:globalThis.document,NodeFilter:globalThis.NodeFilter,getComputedStyle:globalThis.getComputedStyle,
    innerWidth:globalThis.innerWidth,innerHeight:globalThis.innerHeight};
  const fact={parentElement:null,getClientRects:()=>[{}],contains:()=>false};
  const indicator={parentElement:null,getClientRects:()=>shown?[{}]:[]};
  globalThis.getComputedStyle=()=>({visibility:'visible',display:'block',opacity:'1'});
  globalThis.NodeFilter={SHOW_TEXT:4};globalThis.innerWidth=1200;globalThis.innerHeight=850;
  const nodes=['acf-remote-run','标准存储','华东1（杭州）'].map(textContent=>({textContent,parentElement:fact}));
  globalThis.document={body:{innerText:nodes.map(n=>n.textContent).join(' ')},
    querySelectorAll:selector=>selector.split(',').includes('.'+className)?[indicator]:[],
    createRange:()=>({setStart:()=>{},setEnd:()=>{},getClientRects:()=>[{left:10,top:10,right:200,bottom:30}]}),
    elementFromPoint:()=>fact,
    createTreeWalker:()=>{let index=0;return {nextNode(){this.currentNode=nodes[index++];return !!this.currentNode;}};}};
  const frame={url:()=> 'https://oss.console.aliyun.com/actual',evaluate:async(callback,args)=>callback(args)};
  const surface={mainFrame:()=>frame,frames:()=>[frame]};
  try {
    const result=await waitForNavigationIdentity(surface,'open','acf-remote-run',0,['标准存储','华东1（杭州）']);
    assert.equal(result.frames[0].loading,busy);
    assert.equal(result.identity_visible,!busy);
    assert(!('pass' in result));
  }finally{Object.assign(globalThis,saved);}
});
test('faded facts and skeleton/overlay loading cannot count as rendered detail',()=>{
  assert.equal(navigationReady({text:'eni-real rl-test',loading:false,facts_visible:false},'eni-real',['rl-test']),false);
  assert.equal(navigationReady({text:'eni-real rl-test',loading:false,facts_visible:true},'eni-real',['rl-test']),true);
  assert(LOADING_SELECTOR.includes('.next-loading-mask'));
  assert(LOADING_SELECTOR.includes('[class*="skeleton"]'));
});
test('actual DOM probe respects faded ancestors and translucent loading masks',async()=>{
  const saved={document:globalThis.document,NodeFilter:globalThis.NodeFilter,getComputedStyle:globalThis.getComputedStyle,
    innerWidth:globalThis.innerWidth,innerHeight:globalThis.innerHeight};
  let opacity='0.1',mask=false,occluded=false;
  let rect={left:10,top:10,right:100,bottom:25};
  const ancestor={parentElement:null,getClientRects:()=>[{}],kind:'ancestor'};
  const element={parentElement:ancestor,getClientRects:()=>[{}],kind:'fact',contains:()=>false};
  const overlay={parentElement:null,getClientRects:()=>[{}],kind:'mask'};
  globalThis.getComputedStyle=el=>({visibility:'visible',display:'block',opacity:el.kind==='ancestor'?opacity:el.kind==='mask'?'0.5':'1'});
  globalThis.NodeFilter={SHOW_TEXT:4};
  globalThis.innerWidth=1000;globalThis.innerHeight=800;
  globalThis.document={body:{innerText:'eni-real rl-test'},querySelectorAll:()=>mask?[overlay]:[],
    createRange:()=>({setStart:()=>{},setEnd:()=>{},getClientRects:()=>[rect]}),
    elementFromPoint:()=>occluded?overlay:element,
    createTreeWalker:()=>{let index=0;const nodes=[{textContent:'eni-real',parentElement:element},{textContent:'rl-test',parentElement:element}];
      return {nextNode(){this.currentNode=nodes[index++];return !!this.currentNode;}};}};
  const frame={url:()=> 'https://ecs.console.aliyun.com/actual',evaluate:async(callback,args)=>callback(args)};
  const surface={mainFrame:()=>frame,frames:()=>[frame]};
  try {
    assert.equal((await waitForNavigationIdentity(surface,'open','eni-real',0,['rl-test'])).identity_visible,false);
    opacity='1';mask=true;
    assert.equal((await waitForNavigationIdentity(surface,'open','eni-real',0,['rl-test'])).identity_visible,false);
    mask=false;
    assert.equal((await waitForNavigationIdentity(surface,'open','eni-real',0,['rl-test'])).identity_visible,true);
    occluded=true;
    assert.equal((await waitForNavigationIdentity(surface,'open','eni-real',0,['rl-test'])).identity_visible,false);
    occluded=false;rect={left:10,top:900,right:100,bottom:925};
    assert.equal((await waitForNavigationIdentity(surface,'open','eni-real',0,['rl-test'])).identity_visible,false);
  } finally {Object.assign(globalThis,saved);}
});
test('natural requests must not feed known IDs/types/console URLs',()=>{
  assert(allowedNaturalRequest('请只读查询杭州的交换机。'));
  for(const text of ['', '查看 vsw-abcdefgh12345','查询 ALIYUN::VPC::VSwitch','alicloud_vswitch','https://example.com'])
    assert(!allowedNaturalRequest(text));
});
test('one profile, exact page lease: no current tab fallback',()=>{
  assert(ownsTarget({targetId:'one',url:'http://127.0.0.1:16100/'},'http://127.0.0.1:16100','one'));
  assert(!ownsTarget({targetId:'two',url:'http://127.0.0.1:16100/'},'http://127.0.0.1:16100','one'));
  assert(!ownsTarget({targetId:'one',url:'http://127.0.0.1:16101/'},'http://127.0.0.1:16100','one'));
});
test('four simultaneous slots never select another origin page',async()=>{
  const calls=[];
  const pages=Array.from({length:4},(_,i)=>({
    index:i,isClosed:()=>false,url:()=>`http://127.0.0.1:${16100+i}/`,
    evaluate:async()=>{calls.push(i);return {session_id:`ws~slot${i}`};}
  }));
  const context={pages:()=>pages,newCDPSession:async page=>({
    send:async()=>({targetInfo:{targetId:'t'+page.index}}),detach:async()=>{}
  })};
  const browser={contexts:()=>[context]};
  const results=await Promise.all(pages.map((_,i)=>operate(browser,{
    operation:'snapshot',target:'t'+i,origin:`http://127.0.0.1:${16100+i}`})));
  assert.deepEqual(results.map(value=>value.session_id),['ws~slot0','ws~slot1','ws~slot2','ws~slot3']);
  assert.deepEqual(calls,[0,1,2,3]);
  await assert.rejects(()=>operate(browser,{operation:'snapshot',target:'t0',origin:'http://127.0.0.1:16101'}));
  await assert.rejects(()=>operate(browser,{operation:'snapshot',target:'missing',origin:'http://127.0.0.1:16100'}));
});
test('adapter refuses stock full access instead of falling back',()=>{
  const cwd='/fixture/exact-job';
  verifyPolicy({sandbox:scriptPolicy(cwd),cwd,approvalPolicy:'never'},cwd);
  verifyPolicy({sandbox:{...scriptPolicy(cwd),writableRoots:[]},cwd,runtimeWorkspaceRoots:[cwd],approvalPolicy:'never'},cwd);
  for(const response of [{},{sandbox:{type:'dangerFullAccess'},approvalPolicy:'never'},
    {sandbox:{type:'readOnly',networkAccess:true},approvalPolicy:'never'},
    {sandbox:scriptPolicy('/other-job'),cwd,approvalPolicy:'never'},
    {sandbox:scriptPolicy(cwd),cwd:'/another',approvalPolicy:'never'},
    {sandbox:scriptPolicy(cwd),cwd,runtimeWorkspaceRoots:['/other'],approvalPolicy:'never'},
    {sandbox:{...scriptPolicy(cwd),networkAccess:false},cwd,approvalPolicy:'never'},
    {sandbox:{...scriptPolicy(cwd),excludeSlashTmp:false},cwd,approvalPolicy:'never'},
    {sandbox:scriptPolicy(cwd),cwd,approvalPolicy:'never',thread:{ephemeral:true}},
    {sandbox:scriptPolicy(cwd),cwd,approvalPolicy:'on-request'}]) assert.throws(()=>verifyPolicy(response,cwd));
});

for (const role of ['glm','sol']) for (const scenario of ['bound','prestart','launch-conflict','role-conflict','trace-conflict','backend-conflict','unavailable',
  ...(role==='sol'?['paged-older','paged-summary','paged-missing','paged-repeated','paged-wrong']:[])])
  test(`OPS read uses durable ${role} binding: ${scenario}`,async()=>{
    const root=fs.mkdtempSync(path.join(os.tmpdir(),'resource-workflow-read-'));
    const jobId='a'.repeat(32), directory=path.join(root,'jobs',jobId);
    fs.mkdirSync(directory,{recursive:true});
    fs.writeFileSync(path.join(root,'environment.json'),JSON.stringify({bailian_endpoint:'ws://127.0.0.1:4501',sol_endpoint:'ws://127.0.0.1:4502'}));
    fs.writeFileSync(path.join(root,'server.json'),JSON.stringify({origin:'http://127.0.0.1:16099',token:'fake-private'}));
    fs.writeFileSync(path.join(directory,'launch.json'),JSON.stringify({job_id:jobId,
      thread_id:scenario==='launch-conflict'?'another':null,role:scenario==='role-conflict'?(role==='glm'?'sol':'glm'):role}));
    const events=[{method:'transport/backend_selected',params:{role}},
      {method:'transport/thread_created',params:{threadId:'exact-thread'}}];
    if(scenario!=='prestart') events.push({method:'transport/turn_created',params:{threadId:'exact-thread',turnId:scenario==='trace-conflict'?'other-turn':'exact-turn'}});
    fs.writeFileSync(path.join(directory,'raw-rollout.jsonl'),events.map(JSON.stringify).join('\n')+'\n');
    const savedFetch=globalThis.fetch,savedSocket=globalThis.WebSocket,calls=[];
    globalThis.fetch=async (url,options)=>{
      assert.equal(url,'http://127.0.0.1:16099/control');
      assert.deepEqual(JSON.parse(options.body),{operation:'job-binding',arguments:{job_id:jobId}});
      return {ok:scenario!=='unavailable',json:async()=>({id:jobId,role,thread_id:'exact-thread',turn_id:scenario==='prestart'?null:'exact-turn'})};
    };
    globalThis.WebSocket=class extends EventTarget {
      constructor(url){super();calls.push(url);queueMicrotask(()=>this.dispatchEvent(new Event('open')));}
      send(text){const message=JSON.parse(text);calls.push(message.method);if(message.id==null)return;
        assert(['initialize','thread/read','thread/turns/list'].includes(message.method));
        const paged=role==='sol';
        if(message.method==='thread/read')assert.deepEqual(message.params,{threadId:'exact-thread',includeTurns:!paged});
        const pageNumber=calls.filter(value=>value==='thread/turns/list').length;
        if(message.method==='thread/turns/list'){
          assert.equal(message.params.threadId,'exact-thread');assert.equal(message.params.limit,1);
          assert.equal(message.params.sortDirection,'desc');
          assert.equal(message.params.itemsView,scenario==='paged-older'?(pageNumber<3?'notLoaded':'full'):
            scenario==='paged-repeated'?'notLoaded':pageNumber===1?'notLoaded':'full');
        }
        const newer=(scenario==='paged-older'&&pageNumber===1)||['paged-missing','paged-repeated'].includes(scenario);
        const result=message.method==='initialize'?{}:message.method==='thread/turns/list'?
          {data:[{id:newer?'newer-turn':scenario==='paged-wrong'&&message.params.itemsView==='full'?'wrong-turn':'exact-turn',
            status:'completed',itemsView:scenario==='paged-summary'?'summary':message.params.itemsView,
            items:message.params.itemsView==='full'?[{type:'agentMessage',text:'original full evidence'}]:[]}],
            nextCursor:(scenario==='paged-older'&&pageNumber===1)||scenario==='paged-repeated'?'cursor-newer':null}:
          {thread:{id:scenario==='backend-conflict'?'wrong-thread':'exact-thread',turns:paged?[]:[{id:'exact-turn',status:'completed'}]}};
        queueMicrotask(()=>this.dispatchEvent(new MessageEvent('message',{data:JSON.stringify({id:message.id,result})})));}
      close(){this.dispatchEvent(new Event('close'));}
    };
    try {
      if(['bound','prestart','paged-older'].includes(scenario)){
        const result=await readJob(root,jobId);
        const raw=JSON.parse(fs.readFileSync(result.file,'utf8'));
        assert.equal(raw.thread.id,'exact-thread');assert.equal(raw.job_binding.id,jobId);
        assert.equal(fs.statSync(result.file).mode & 0o777,0o600);
        assert.deepEqual(calls,[role==='glm'?'ws://127.0.0.1:4501':'ws://127.0.0.1:4502','initialize','initialized','thread/read',
          ...(role==='sol'&&['bound','prestart','paged-older'].includes(scenario)?[
            ...(scenario==='paged-older'?['thread/turns/list']:[]),'thread/turns/list','thread/turns/list']:[])]);
        if(role==='sol'&&['bound','paged-older'].includes(scenario)){
          assert.equal(raw.evidence_scope,'exact_bound_turn_full');
          assert.equal(raw.original_responses.pages.at(-1).result.data[0].items[0].text,'original full evidence');
        }
        if(role==='sol'&&scenario==='prestart'){
          assert.equal(raw.evidence_scope,'all_turns_full_paginated_files');
          assert.equal(raw.thread.turns.length,1);
          const full=JSON.parse(fs.readFileSync(path.join(root,raw.original_responses.pages[1].path),'utf8'));
          assert.equal(full.result.data[0].itemsView,'full');
          assert.equal(full.result.data[0].items[0].text,'original full evidence');
        }
      }else{
        await assert.rejects(()=>readJob(root,jobId));
        assert(!fs.readdirSync(directory).some(name=>name.startsWith('ops-thread-read-')));
        if(scenario!=='backend-conflict'&&!scenario.startsWith('paged-'))assert.deepEqual(calls,[]);
      }
      assert.equal(JSON.parse(fs.readFileSync(path.join(directory,'launch.json'),'utf8')).thread_id,scenario==='launch-conflict'?'another':null);
    }finally{globalThis.fetch=savedFetch;globalThis.WebSocket=savedSocket;fs.rmSync(root,{recursive:true,force:true});}
  });

test('worker materializes a draft through a harmless first UI send before capability activation',async()=>{
  const actions=[];
  const page={isClosed:()=>false,url:()=> 'http://127.0.0.1:16100/',
    locator:selector=>({click:async()=>actions.push(['click',selector]),
      fill:async text=>{assert(allowedNaturalRequest(text));assert.match(text,/不要调用任何工具/);actions.push(['fill',selector]);}}),
    waitForFunction:async()=>actions.push(['wait','complete-sidebar-key']),
    evaluate:async()=>({session_id:'ws~new-session'})};
  const context={pages:()=>[page],newCDPSession:async()=>({
    send:async()=>({targetInfo:{targetId:'owned'}}),detach:async()=>{}})};
  const result=await operate({contexts:()=>[context]},{operation:'new-session',target:'owned',origin:page.url(),
    selector:'button.new',input_selector:'textarea.prompt',send_selector:'button.send'});
  assert.equal(result.session_id,'ws~new-session');
  assert.deepEqual(actions,[['click','button.new'],['fill','textarea.prompt'],['click','button.send'],['wait','complete-sidebar-key']]);
});

test('new session closes only the leased idle old preview before the real UI send',async()=>{
  for(const scenario of ['idle','busy','locked','missing-session','ambiguous-close','close-failed','changed-session']){
    const actions=[];let closed=false;
    const page={isClosed:()=>false,url:()=> 'http://127.0.0.1:16101/',
      evaluate:async()=>({session_id:scenario==='missing-session'?null:
        closed&&scenario==='changed-session'?'ws~another':'ws~old-full',
        busy:scenario==='busy',unlocked:scenario!=='locked',preview_open:!closed}),
      locator:selector=>({count:async()=>scenario==='ambiguous-close'?2:1,isVisible:async()=>true,
        click:async()=>{actions.push(['click',selector]);},
        fill:async text=>{assert.match(text,/不要调用任何工具/);actions.push(['fill',selector]);},
        waitFor:async()=>{actions.push(['detached',selector]);if(scenario==='close-failed')throw Error('still covered');closed=true;}}),
      waitForFunction:async()=>actions.push(['wait','complete-sidebar-key'])};
    const context={pages:()=>[page],newCDPSession:async()=>({
      send:async()=>({targetInfo:{targetId:'leased'}}),detach:async()=>{}})};
    const request={operation:'new-session',target:'leased',origin:page.url(),selector:'button.new',
      input_selector:'textarea.prompt',send_selector:'button.send'};
    if(scenario==='idle'){
      await operate({contexts:()=>[context]},request);
      assert.deepEqual(actions,[['click','.resource-link-preview-actions > button'],
        ['detached','.resource-link-preview-backdrop'],['click','button.new'],
        ['fill','textarea.prompt'],['click','button.send'],['wait','complete-sidebar-key']]);
    }else{
      await assert.rejects(()=>operate({contexts:()=>[context]},request));
      assert(!actions.some(([,selector])=>['button.new','textarea.prompt','button.send'].includes(selector)));
    }
  }
});

test('source refuses unfinished response and mistyped session before persisting evidence',async()=>{
  let reads=0;
  const page={isClosed:()=>false,url:()=> 'http://127.0.0.1:16100/',
    evaluate:async()=>{reads++;return {session_id:'ws~exact',busy:true};}};
  const context={pages:()=>[page],newCDPSession:async()=>({
    send:async()=>({targetInfo:{targetId:'owned'}}),detach:async()=>{}})};
  const request={operation:'source',target:'owned',origin:page.url(),session_id:'ws~exact'};
  await assert.rejects(()=>operate({contexts:()=>[context]},request),/answer still running/);
  await assert.rejects(()=>operate({contexts:()=>[context]},{...request,session_id:'ws~typo'}),/source session mismatch/);
  assert.equal(reads,2);
});

test('bootstrap consumes private fragment on an already-open root without switching tabs',async()=>{
  const root=fs.mkdtempSync(path.join(os.tmpdir(),'resource-workflow-bootstrap-'));
  const token=path.join(root,'access-token');
  fs.writeFileSync(token,'fake-test-only-token',{mode:0o600});
  let reloaded=0;
  const page={url:()=> 'http://127.0.0.1:16100/',
    goto:async url=>{assert(url.startsWith('http://127.0.0.1:16100/#web_token='));},
    evaluate:async()=>true,reload:async()=>{reloaded++;},
    locator:()=>({waitFor:async()=>{}}),waitForFunction:async()=>{assert.equal(reloaded,1);}};
  const context={pages:()=>[page],newPage:async()=>{assert.fail('must keep the same target');},
    newCDPSession:async()=>({send:async()=>({targetInfo:{targetId:'owned'}}),detach:async()=>{}})};
  try{
    const value=await operate({contexts:()=>[context]},{operation:'bootstrap',origin:'http://127.0.0.1:16100',token_file:token});
    assert.deepEqual(value,{target:'owned',origin:'http://127.0.0.1:16100'});
    assert(!JSON.stringify(value).includes('fake-test-only-token'));
  }finally{fs.rmSync(root,{recursive:true,force:true});}
});

test('iframe evidence stays on leased page despite sign-in popup; preview close remains scoped',async()=>{
  const root=fs.mkdtempSync(path.join(os.tmpdir(),'resource-workflow-preview-'));
  const clicks=[],screenshots=[];
  const page={isClosed:()=>false,url:()=> 'http://127.0.0.1:16100/',
    bringToFront:async()=>{},
    evaluate:async()=>({session_id:'ws~full'}),
    locator:selector=>({count:async()=>1,isVisible:async()=>selector!=='.busy',
      getAttribute:async name=>{assert.equal(name,'data-session-id');return 'ws~full';},
      elementHandle:async()=>({contentFrame:async()=>null,dispose:async()=>{}}),
      nth:()=>({isVisible:async()=>true,getAttribute:async name=>name==='href'?'https://vpc.console.aliyun.com/actual':'one',
        click:async()=>clicks.push(selector)}),
      click:async()=>clicks.push(selector),waitFor:async()=>{},innerText:async()=> 'actual iframe page'}),
    waitForEvent:async()=>({close:async()=>{throw Error('must not close product sign-in helper');}}),
    waitForLoadState:async()=>{},frames:()=>[],mainFrame:()=>null,
    screenshot:async options=>{assert.equal(options.fullPage,false);screenshots.push('leased-page');}};
  const context={pages:()=>[page],newCDPSession:async()=>({
    send:async()=>({targetInfo:{targetId:'leased'}}),detach:async()=>{}})};
  const browser={contexts:()=>[context]};
  try{
    const result=await operate(browser,{operation:'click',origin:page.url(),target:'leased',
      session_id:'ws~full',mode:'iframe',selector:'a.exact',busy_selector:'.busy',output_dir:root,artifact_id:'scene'});
    assert.equal(result.actual_navigation,'iframe_or_none');assert.deepEqual(screenshots,['leased-page']);
    await operate(browser,{operation:'close-preview',origin:page.url(),target:'leased',session_id:'ws~full'});
    assert.deepEqual(clicks,['a.exact','.resource-link-preview-actions > button']);
    await assert.rejects(()=>operate(browser,{operation:'close-preview',origin:page.url(),target:'leased',session_id:'uuid'}));
  }finally{fs.rmSync(root,{recursive:true,force:true});}
});

for (const worker of ['glm','sol','macmini']) for (const scenario of ['terminal','disconnect','rejected','archived','loaded','writer','loaded-active',...(worker==='sol'?['diagnostic']:[])]) test(`full transport ${worker} ${scenario} binds exact turn without real models`,async()=>{
  const role = worker === 'macmini' ? 'glm' : worker;
  const selected = worker === 'macmini' ? {id:'alilang-macmini',host:'macmini',endpoint:'ws://127.0.0.1:4510',
    cwd:'/remote/workers/exact-job',link_fingerprint:'a'.repeat(64)} : null;
  const root=fs.mkdtempSync(path.join(os.tmpdir(),'resource-workflow-adapter-'));
  const job='offline-job';const steps=[];
  fs.mkdirSync(path.join(root,'jobs',job),{recursive:true});
  fs.mkdirSync(path.join(root,'scripts','web','e2e','resource_link_workflow'),{recursive:true});
  fs.writeFileSync(path.join(root,'scripts','web','e2e','resource_link_workflow',role.toUpperCase()+'.zh-CN.md'),'Offline fixture guide');
  fs.copyFileSync(new URL('../../scripts/web/e2e/resource_link_workflow/roles.json',import.meta.url),
    path.join(root,'scripts','web','e2e','resource_link_workflow','roles.json'));
  const model=role==='glm'?'glm-5.3-prime':'gpt-6.1-sol';
  const effort=role==='glm'?'xhigh':'high';
  const operation=scenario==='diagnostic'?'inspect':role==='glm'?'submit':'block';
  const json=(file,value)=>fs.writeFileSync(path.join(root,file),JSON.stringify(value));
  json('server.json',{origin:'http://127.0.0.1:16099',token:'fixture'});
  if (selected) json('macmini-link.json',{fingerprint:selected.link_fingerprint});
  json(`jobs/${job}/capability.json`,{token:'fixture-job'});
  json(`jobs/${job}/launch.json`,{thread_id:['archived','loaded','writer','loaded-active'].includes(scenario)?'exact-thread':null,
    ...(selected ? {worker_backend:selected} : {})});
  json('environment.json',{repo:root,bailian_endpoint:'ws://127.0.0.1:4501',sol_endpoint:'ws://127.0.0.1:4502'});
  const oldFetch=globalThis.fetch,oldSocket=globalThis.WebSocket;
  let policyApplied=false,client,clientClosed=false,writerRejected=false;
  let loadedInstructions=['archived','loaded','writer','loaded-active'].includes(scenario)
    ? 'Use the previous job script, never the current job script.' : null;
  let expectedInstructions;
  const cwd=selected?.cwd ?? path.join(root,'jobs',job,'sandbox');
  const setupClient=async({bridge})=>{client=bridge;return {command:'python3 /fixture/workflow-agent.py',
    close:async()=>{clientClosed=true;}};};
  globalThis.fetch=async(_url,options)=>{
    const request=JSON.parse(options.body);steps.push(request.operation);
    if(request.operation==='_bind' && request.arguments.turn) assert.equal(request.arguments.turn,'exact-turn');
    if(request.operation==='_terminal') assert.equal(request.arguments.status,'completed');
    if(scenario==='rejected' && request.operation===operation)
      return {ok:false,json:async()=>({error:'creation needs explicit OPS authorization for this resource ID'})};
    if(scenario==='diagnostic' && request.operation==='inspect')
      return {ok:true,json:async()=>({image_url:'data:image/png;base64,b2ZmbGluZQ==',
        sha256:createHash('sha256').update('offline').digest('hex'),
        evidence_role:'diagnostic_only',eligible_for_verdict:false})};
    return {ok:true,json:async()=>request.operation==='context'?{role,purpose:role==='glm'?'dispatch':'prepare',
      resource:{id:'R0001'},...(selected ? {worker_backend:selected} : {})}:{ok:true}};
  };
  class FakeSocket extends EventTarget {
    constructor(url){super();assert.equal(url,selected?.endpoint ?? (role==='glm'? 'ws://127.0.0.1:4501':'ws://127.0.0.1:4502'));
      queueMicrotask(()=>this.dispatchEvent(new Event('open')));}
    message(value){const event=new Event('message');event.data=JSON.stringify(value);this.dispatchEvent(event);}
    send(raw){const request=JSON.parse(raw);
      if(request.method==='initialize') queueMicrotask(()=>this.message({id:request.id,result:{}}));
      if(request.method==='thread/read'){
        assert(['archived','loaded','writer','loaded-active'].includes(scenario));assert.equal(request.params.includeTurns,false);
        queueMicrotask(()=>this.message({id:request.id,result:{thread:{id:'exact-thread',
          path:scenario==='archived'?'/fixture/archived_sessions/rollout.jsonl':'/fixture/sessions/rollout.jsonl',
          turns:[{id:'prior-turn',status:scenario==='loaded-active'?'inProgress':'completed'}]}}}));
      }
      if(request.method==='thread/turns/list'){
        assert.equal(request.params.itemsView,'notLoaded');assert.equal(request.params.limit,50);
        queueMicrotask(()=>this.message({id:request.id,result:{data:[{id:'prior-turn',
          status:scenario==='loaded-active'?'inProgress':'completed'}],nextCursor:null}}));
      }
      if(request.method==='thread/archive'){
        assert.equal(scenario,'writer');assert.equal(request.params.threadId,'exact-thread');
        queueMicrotask(()=>this.message({id:request.id,result:{}}));
      }
      if(request.method==='thread/unarchive'){
        assert.equal(request.params.threadId,'exact-thread');
        queueMicrotask(()=>this.message({id:request.id,result:{}}));
      }
      if(request.method==='thread/settings/update'){
        assert(['archived','loaded','writer'].includes(scenario));
        assert.equal(request.params.threadId,'exact-thread');
        assert.deepEqual(request.params.sandboxPolicy,scriptPolicy(cwd));
        assert.equal(request.params.approvalPolicy,'never');
        policyApplied=true;
        queueMicrotask(()=>this.message({id:request.id,result:{}}));
      }
      if(request.method==='thread/start'||request.method==='thread/resume'){
        assert.equal(request.params.sandbox,'workspace-write');assert.equal(request.params.approvalPolicy,'never');
        assert.equal(request.params.config['features.shell_tool'],true);
        assert.equal(request.params.config['features.unified_exec'],true);
        assert.equal(request.params.dynamicTools,undefined);
        assert.match(request.params.developerInstructions,/ordinary shell/);
        assert.match(request.params.developerInstructions,/view_image/);
        expectedInstructions=request.params.developerInstructions;
        // Match the real backend: resume keeps persisted developer messages,
        // including after archive/unarchive, even if policy updates succeed.
        if(request.method==='thread/start')loadedInstructions=expectedInstructions;
        assert.equal(request.params.model,model);
        if (selected) assert.equal(request.params.cwd,selected.cwd);
        if(request.method==='thread/start')assert.equal(request.params.ephemeral,false);
        else {assert.equal(request.params.threadId,'exact-thread');assert.equal(request.params.excludeTurns,true);}
        if(scenario==='writer'&&!writerRejected){
          writerRejected=true;
          queueMicrotask(()=>this.message({id:request.id,error:{message:'thread exact-thread already has an active writer'}}));
          return;
        }
        queueMicrotask(()=>this.message({id:request.id,result:{thread:{id:'exact-thread'},cwd,
          sandbox:scenario==='loaded' && !policyApplied ? {type:'dangerFullAccess'} : scriptPolicy(cwd),
          approvalPolicy:'never'}}));
      }
      if(request.method==='thread/name/set'){
        assert.equal(request.params.threadId,'exact-thread');
        assert.equal(request.params.name,role==='sol'?'Resource Link · Sol':'Resource Link · GLM · R0001');
        queueMicrotask(()=>this.message({id:request.id,result:{}}));
      }
      if(request.method==='thread/inject_items'){
        assert.equal(request.params.threadId,'exact-thread');
        assert.equal(request.params.items.length,1);
        const item=request.params.items[0];
        assert.equal(item.type,'message');assert.equal(item.role,'developer');
        assert.equal(item.content.length,1);assert.equal(item.content[0].type,'input_text');
        assert.match(item.content[0].text,/supersede earlier per-job client paths and modes/);
        assert(item.content[0].text.endsWith(expectedInstructions));
        loadedInstructions=expectedInstructions;
        queueMicrotask(()=>this.message({id:request.id,result:{}}));
      }
      if(request.method==='turn/start'){
        assert.equal(loadedInstructions,expectedInstructions,'current job script and mode must replace old instructions');
        assert.equal(request.params.model,model);assert.equal(request.params.effort,effort);
        assert.deepEqual(request.params.sandboxPolicy,scriptPolicy(cwd));
        assert.equal(request.params.approvalPolicy,'never');
        if(scenario==='loaded') assert.equal(policyApplied,true);
        queueMicrotask(async()=>{
          this.message({id:request.id,result:{turn:{id:'exact-turn'}}});
          // A real loopback HTTP request, as made by the ordinary Python script.
          // It waits for the durable exact turn binding before invoking Server.
          const body=JSON.stringify({operation,arguments:role==='glm'?{report:'Offline fixture'}:{reason:'Offline fixture'}});
          const result=await new Promise((resolve,reject)=>{
            const req=http.request(client.origin+'/invoke',{method:'POST',headers:{authorization:'Bearer '+client.token,
              'content-type':'application/json'}},res=>{let text='';res.on('data',part=>text+=part);
              res.on('end',()=>resolve(JSON.parse(text)));});req.on('error',reject);req.end(body);
          });
          assert.equal(result.ok,scenario!=='rejected');
          if(scenario==='diagnostic'){
            assert.equal(result.result.image_url,'data:image/png;base64,b2ZmbGluZQ==');
            assert.equal(result.result.evidence_role,'diagnostic_only');
            assert.equal(result.result.eligible_for_verdict,false);
          }
          if(scenario==='rejected')assert.match(result.error,/explicit OPS authorization/);
          this.message({method:'turn/completed',params:{threadId:'exact-thread',turn:{id:'stale-turn',status:'failed'}}});
          if(scenario==='disconnect')this.close();
          else this.message({method:'turn/completed',params:{threadId:'exact-thread',turn:{id:'exact-turn',status:'completed'}}});
        });
      }
    }
    close(){this.dispatchEvent(new Event('close'));}
  }
  globalThis.WebSocket=FakeSocket;
  try{
    if(scenario==='loaded-active'){
      await assert.rejects(()=>runAdapter(root,job,{setupClient}),/active or unknown turn/);
      const raw=fs.readFileSync(path.join(root,'jobs',job,'raw-rollout.jsonl'),'utf8');
      assert(!raw.includes('transport/thread_archived'));assert(!raw.includes('transport/turn_created'));
      return;
    }
    if(scenario==='disconnect') await assert.rejects(()=>runAdapter(root,job,{setupClient}),/disconnected/);
    else await runAdapter(root,job,{setupClient});
    assert(clientClosed);
    assert.deepEqual(steps,['context','_submission_boundary','_bind','_submission_boundary','_bind',operation,
      ...(scenario!=='disconnect'?['_terminal']:[])]);
    const raw=fs.readFileSync(path.join(root,'jobs',job,'raw-rollout.jsonl'),'utf8');
    assert(raw.includes('transport/turn_created'));assert(raw.includes('exact-turn'));
    assert(!raw.includes('fixture-job'));
    assert(!raw.includes('stale-turn'));
    if(scenario==='rejected') assert(raw.includes('script/rejected'));
    assert(raw.includes('script/request'));assert(!raw.includes(client.token));
    assert(!raw.includes('data:image/png;base64'));
    if(scenario==='archived')assert(raw.includes('transport/thread_unarchived'));
    assert(raw.includes('transport/job_instructions_injected'));
    if(scenario==='loaded')assert(!raw.includes('transport/thread_archived') && !raw.includes('transport/thread_unarchived'));
    if(scenario==='writer')assert(raw.includes('release confirmed idle desktop writer'));
  }finally{globalThis.fetch=oldFetch;globalThis.WebSocket=oldSocket;fs.rmSync(root,{recursive:true,force:true});}
});
