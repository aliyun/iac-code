import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import {keyPairConsoleUrl,keyPairMetadataReady,keyPairHorizontalDelta,inspectEcsKeyPairMetadata}
  from '../../scripts/web/e2e/resource_link_workflow/console_keypair.mjs';

const expected={kind:'ecs_keypair_metadata',identifier:'fixture-key',fingerprint:'0123456789abcdef0123456789abcdef',region:'cn-hangzhou'};

test('KeyPair inspection parses only actual domestic same-region list URLs, never constructs a destination',()=>{
  for(const host of ['ecs','ecs4service'])for(const route of ['/keyPair/region/cn-hangzhou','/#/keyPair/region/cn-hangzhou'])
    assert(keyPairConsoleUrl(`https://${host}.console.aliyun.com${route}`,expected));
  for(const url of ['', 'https://foreign.invalid/keyPair/region/cn-hangzhou',
    'http://ecs.console.aliyun.com/keyPair/region/cn-hangzhou',
    'https://ecs.console.aliyun.com/keyPair/region/cn-shanghai',
    'https://ecs.console.aliyun.com/keyPair/region/cn-hangzhou?redirect=other',
    'https://user@ecs.console.aliyun.com/keyPair/region/cn-hangzhou',
    'https://ecs.console.aliyun.com/keyPair/region/cn-hangzhou#other',
    'https://ecs.console.aliyun.com/#/instance/region/cn-hangzhou'])
    assert.throws(()=>keyPairConsoleUrl(url,expected),/unbound ECS/);
});

test('KeyPair full fingerprint requires same exact row, loading complete and actual full visible fields',()=>{
  const view={loading:false,exact_row:true,fields:[expected.identifier,expected.fingerprint].map(marker=>({marker,visible:true}))};
  assert.equal(keyPairMetadataReady(view,expected),true);
  for(const patch of [{loading:true},{exact_row:false},{fields:[{marker:expected.identifier,visible:true}]},
    {fields:view.fields.map(f=>({...f,visible:f.marker!==expected.fingerprint}))}])
    assert.equal(keyPairMetadataReady({...view,...patch},expected),false);
  const geometry={...view,fields:[{marker:expected.fingerprint,left:450,right:690}],scroll:{left:300,right:560,current:100,maximum:400}};
  assert.equal(keyPairHorizontalDelta(geometry,expected),140);
  assert.throws(()=>keyPairHorizontalDelta({...geometry,scroll:{...geometry.scroll,right:430}},expected),/unoccluded/);
  assert.throws(()=>keyPairHorizontalDelta({...geometry,scroll:{...geometry.scroll,maximum:200}},expected),/unavailable/);
});

for(const scenario of ['exact','scroll','collapse','too-narrow','occluded','loading','multiline','multiline-occluded','iframe','iframe-hidden','iframe-detached','background','oopif-empty-cache','wrong-host','wrong-region','origin-changed',
  'search-ambiguous','wrong-search-mode','wrong-row','row-ambiguous','wrong-fingerprint','invalid-name','invalid-fingerprint',
  'session-changed','late-session-changed','capture-failed','row-changed','restore-failed'])
  test(`KeyPair native readonly sampling with original pixels retained: ${scenario}`,async()=>{
    const actions=[];let scrolled=false,collapsed=false,checks=0,urlReads=0,rowReads=0;
    const control={count:async()=>1,isVisible:async()=>true,waitFor:async()=>{}};
    const search={...control,count:async()=>scenario==='search-ambiguous'?2:1,
      fill:async value=>{assert.equal(value,expected.identifier);actions.push('search-fill');},
      press:async value=>{assert.equal(value,'Enter');actions.push('search-enter');}};
    const row={...control,count:async()=>scenario==='row-ambiguous'?2:1,
      getAttribute:async()=>scenario==='wrong-row'||scenario==='row-changed'&&++rowReads>1?'other':expected.identifier,
      getByText:marker=>({count:async()=>scenario==='wrong-fingerprint'&&marker===expected.fingerprint?0:1}),
      hover:async()=>actions.push('hover'),evaluate:async(fn,args)=>{
        if(scenario.startsWith('multiline')){
          const box=(left,top,width,height)=>({left,top,right:left+width,bottom:top+height,width,height});
          const container={parentElement:null,scrollWidth:900,clientWidth:900,getBoundingClientRect:()=>box(0,0,900,600)};
          const el={parentElement:container,getAttribute:()=>expected.identifier,querySelectorAll:()=>[]};
          const name={parentElement:container,contains:target=>target===name,getBoundingClientRect:()=>box(10,80,60,10)};
          const finger={parentElement:container,contains:target=>target===finger,getBoundingClientRect:()=>box(100,20,230,30)};
          const nodes=[{textContent:expected.identifier,parentElement:name},{textContent:expected.fingerprint,parentElement:finger}];
          const document={createTreeWalker:()=>{let index=-1;return {nextNode(){return ++index<nodes.length;},get currentNode(){return nodes[index];}};},
            createRange:()=>{let node;return {selectNodeContents(n){node=n;},getBoundingClientRect:()=>node.parentElement.getBoundingClientRect(),
              getClientRects:()=>node.parentElement===name?[name.getBoundingClientRect()]:[box(100,20,230,10),box(100,40,140,10)]};},
            elementFromPoint:(x,y)=>y>=80?name:y>=20&&y<=30||y>=40&&y<=50?
              scenario==='multiline-occluded'&&x>320?el:finger:el,querySelectorAll:()=>[]};
          return vm.runInNewContext(`(${fn.toString()})(el,args)`,{el,args,document,NodeFilter:{SHOW_TEXT:4},innerWidth:900,innerHeight:600,
            getComputedStyle:()=>({opacity:'1',display:'block',visibility:'visible',overflowX:'visible',overflowY:'visible'})});
        }
        return {exact_row:true,loading:scenario==='loading',
        fields:[{marker:expected.identifier,visible:true},
          {marker:expected.fingerprint,visible:scenario!=='occluded'&&scenario!=='too-narrow'&&
            (!['scroll','collapse','restore-failed'].includes(scenario)||scrolled),left:450,right:690}],
        scroll:{left:300,right:scenario==='too-narrow'||['collapse','restore-failed'].includes(scenario)&&!collapsed?430:560,
          current:100,maximum:400}};}};
    const toggle={...control,count:async()=>scenario==='restore-failed'&&collapsed?0:1,
      click:async()=>{collapsed=!collapsed;actions.push(collapsed?'collapse':'restore');}};
    const mode={...control,count:async()=>scenario==='wrong-search-mode'?0:1};
    const frame={url:()=>scenario==='oopif-empty-cache'?'':'https://ecs4service.console.aliyun.com/keyPair/region/cn-hangzhou',
      frameElement:async()=>{if(scenario==='iframe-detached')throw Error('frame detached');
        return {evaluate:async()=>({visible:scenario!=='iframe-hidden'}),dispose:async()=>{}};},
      evaluate:async(fn,args)=>args?{text:[expected.identifier,expected.fingerprint].join(' '),loading:false,facts_visible:true}:
      fn.toString().includes('document.location.href')?
        scenario==='wrong-host'?'https://foreign.invalid/':scenario==='wrong-region'?'https://ecs.console.aliyun.com/keyPair/region/cn-shanghai':
          `https://${scenario==='origin-changed'&&++urlReads>1?'ecs':'ecs4service'}.console.aliyun.com/keyPair/region/cn-hangzhou`:{painted:true},
      getByPlaceholder:(name,options)=>{assert.equal(name,'请输入密钥对名称');assert.equal(options.exact,true);return search;},
      getByText:(name,options)=>{assert(['fixture-key','密钥对名称'].includes(name));assert.equal(options.exact,true);return {};},
      locator:selector=>selector==='tr[data-row-key]'?{filter:()=>row}:selector==='button'?{filter:()=>mode}:toggle};
    const surface={bringToFront:async()=>actions.push('focus'),evaluate:async()=>scenario!=='background',
      mouse:{wheel:async(dx,dy)=>{assert.equal(dx,140);assert.equal(dy,0);scrolled=true;actions.push('scroll');}},
      screenshot:async options=>{assert.equal(options.fullPage,false);actions.push('pixels');if(scenario==='capture-failed')throw Error('capture failed');}};
    const beforeCapture=async()=>{checks++;if(scenario==='session-changed'||scenario==='late-session-changed'&&checks>1)throw Error('session mismatch');};
    const value={...expected,identifier:scenario==='invalid-name'?'bad[name':expected.identifier,
      fingerprint:scenario==='invalid-fingerprint'?'wrong':expected.fingerprint};
    const options={mode:scenario.startsWith('iframe')?'iframe':'open',beforeCapture};
    const failures={'wrong-host':/unbound ECS/,'wrong-region':/unbound ECS/,'origin-changed':/origin changed/,
      'search-ambiguous':/search ambiguous/,'wrong-search-mode':/exact name search mode/,
      'wrong-row':/exact row ambiguous/,'row-ambiguous':/exact row ambiguous/,
      'wrong-fingerprint':/source field missing/,'invalid-name':/invalid ECS/,'invalid-fingerprint':/invalid ECS/,
      'session-changed':/session mismatch/,'late-session-changed':/session mismatch/,
      'capture-failed':/capture failed/,'row-changed':/row changed/,'restore-failed':/restoration unavailable/};
    if(scenario in failures)
      await assert.rejects(()=>inspectEcsKeyPairMetadata(surface,frame,value,'original-metadata.png',options),failures[scenario]);
    else{
      const result=await inspectEcsKeyPairMetadata(surface,frame,value,'original-metadata.png',options);
      assert.equal(result.identity_visible,!['too-narrow','occluded','multiline-occluded','loading','iframe-hidden','iframe-detached','background'].includes(scenario));
      assert.equal(result.cloud_writes,0);assert.equal(result.original_landing_unchanged,true);
      assert.equal(result.readonly_completed,true);assert(!('pass' in result));
      if(scenario==='too-narrow')assert.match(result.sampling_error,/unoccluded/);
    }
    assert(actions.every(action=>['focus','search-fill','search-enter','collapse','restore','hover','scroll','pixels'].includes(action)));
    if(scenario==='collapse')assert.equal(collapsed,false);
  });

test('KeyPair supplemental code cannot write resources, hide console DOM, navigate a fabricated URL or enlarge screenshots',()=>{
  const source=fs.readFileSync(new URL('../../scripts/web/e2e/resource_link_workflow/console_keypair.mjs',import.meta.url),'utf8');
  assert(!source.includes('.goto('));assert(!source.includes('.setViewportSize('));assert(!source.includes('.style.'));
  assert(!source.includes("name:'删除'"));assert(!source.includes("name:'绑定密钥对'"));assert(!source.includes("name:'创建密钥对'"));
  assert.equal((source.match(/\.fill\(/g)||[]).length,1);
  assert(source.includes('search.fill(expected.identifier)'));
});
