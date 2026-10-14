import test from 'node:test';import assert from 'node:assert/strict';import fs from 'node:fs';
import {nasExportReady,nasExportRouteMatches} from '../../scripts/web/e2e/resource_link_workflow/console_nas_export.mjs';
const expected={region:'cn-beijing',file_system_id:'cpfs-parent',source_tool_use_id:'offline-call'};
test('only actual own domestic NAS parent route can open a native export drawer',()=>{
 for(const host of ['nasnext.console.aliyun.com','nasnext4service.console.aliyun.com']){
  const url=`https://${host}/cn-beijing/filesystem/cpfs-parent/protocolservice`;
  assert(nasExportRouteMatches(url,expected));
  for(const wrong of [url.replace('cpfs-parent','cpfs-other'),url.replace('cn-beijing','cn-hangzhou'),
   url.replace('https:','http:'),url.replace('aliyun.com','example.com'),url+'/guessed-child',
   url.replace(host,`${host}.example.com`),url.replace(host,`evil.${host}`),
   url.replace('aliyun.com','alibabacloud.com')])
   assert.equal(nasExportRouteMatches(wrong,expected),false);
 }
 assert.equal(nasExportRouteMatches('not a URL',expected),false);
});
test('all three complete painted captures are necessary, no clipped or background substitute',()=>{
 const captures=['export_id','path','mount'].map(kind=>({kind,fields_visible:true,mode:'iframe',cloud_writes:0,
  capture_sync:{foreground_visible:true,frame_embedding:{visible:true}}}));
 assert(nasExportReady(captures,expected));
 assert.equal(nasExportReady(captures.slice(0,2),expected),false);
 for(const field of ['fields_visible','cloud_writes']){
  const changed=structuredClone(captures);changed[0][field]=field==='cloud_writes'?1:false;
  assert.equal(nasExportReady(changed,expected),false);
 }
 const background=structuredClone(captures);background[1].capture_sync.foreground_visible=false;
 assert.equal(nasExportReady(background,expected),false);
});
test('capture strategy has no URL synthesis, viewport, DOM/style or cloud mutations',()=>{
 const code=fs.readFileSync(new URL('../../scripts/web/e2e/resource_link_workflow/console_nas_export.mjs',import.meta.url),'utf8');
 assert(!/\.goto\(|setViewport|addStyleTag|\.fill\(|evaluate\([^)]*=>[^;]*\.style/.test(code));
 assert(code.includes('mouse.wheel'));assert(code.includes('.hover('));
 assert(code.includes("getByText('导出目录'"));
});
