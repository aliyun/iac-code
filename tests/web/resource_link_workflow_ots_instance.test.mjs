import test from 'node:test';import assert from 'node:assert/strict';import fs from 'node:fs';
import {otsInstanceRouteMatches,otsEndpointReady,otsEndpointView} from '../../scripts/web/e2e/resource_link_workflow/console_ots_instance.mjs';
const expected={identifier:'offline-instance',region:'cn-hangzhou',source_tool_use_id:'offline-call',
 public_endpoint:'https://offline-instance.cn-hangzhou.ots.aliyuncs.com'};
test('exact own domestic route and service frame only',()=>{
 const u='https://ots.console.aliyun.com/cn-hangzhou/offline-instance/list';
 assert(otsInstanceRouteMatches(u,expected));assert(otsInstanceRouteMatches(u.replace('ots.console','ots4service.console'),expected));
 for(const bad of [u.replace('https:','http:'),u.replace('offline-instance','other'),u.replace('cn-hangzhou','cn-beijing'),
   u.replace('aliyun.com','example.com'),u+'/guessed-child',u.replace('https://','https://user@')])
   assert.equal(otsInstanceRouteMatches(bad,expected),false);
});
test('full original endpoint and name must both be painted and uniquely source-bound',()=>{
 const view={endpoint:{value:expected.public_endpoint,visible:true,count:1},name:{value:expected.identifier,visible:true,count:1},loading:false};
 assert(otsEndpointReady(view,expected));
 for(const field of ['endpoint','name'])for(const property of ['value','visible','count']){
  const v=structuredClone(view);v[field][property]=property==='value'?'wrong':property==='visible'?false:2;
  assert.equal(otsEndpointReady(v,expected),false);
 }
 assert.equal(otsEndpointReady({...view,loading:true},expected),false);
 assert.equal(otsEndpointReady(view,{...expected,source_tool_use_id:''}),false);
});
test('input values without visible, untruncated pixels cannot satisfy readiness',()=>{
 const original={document:globalThis.document,innerWidth:globalThis.innerWidth,innerHeight:globalThis.innerHeight,getComputedStyle:globalThis.getComputedStyle};
 const input=value=>({value,scrollLeft:0,scrollWidth:330,clientWidth:340,parentElement:null,
  getClientRects:()=>[{}],getBoundingClientRect:()=>({left:20,top:20,right:360,bottom:50}),contains:()=>false});
 const endpoint=input(expected.public_endpoint),name=input(expected.identifier);let hit=endpoint;
 globalThis.innerWidth=1280;globalThis.innerHeight=900;
 globalThis.getComputedStyle=()=>({opacity:'1',display:'block',visibility:'visible'});
 globalThis.document={querySelectorAll:s=>s==='input'?[endpoint,name]:[],elementFromPoint:()=>hit};
 try{
  assert.equal(otsEndpointView({...expected,loadingSelector:'.loading'}).endpoint.visible,true);
  endpoint.scrollWidth=400;assert.equal(otsEndpointView({...expected,loadingSelector:'.loading'}).endpoint.visible,false);
  endpoint.scrollWidth=330;hit={};assert.equal(otsEndpointView({...expected,loadingSelector:'.loading'}).endpoint.visible,false);
 }finally{Object.assign(globalThis,original);}
});
test('native sampling does not mutate URL, viewport, styles, fields or cloud',()=>{
 const code=fs.readFileSync(new URL('../../scripts/web/e2e/resource_link_workflow/console_ots_instance.mjs',import.meta.url),'utf8');
 assert(!/\.goto\(|setViewport|setWindowBounds|addStyleTag|\.fill\(|\.value\s*=(?!=)/.test(code));
 assert(code.includes('nav-collapse-trigger'));assert(code.includes('finally'));
});
