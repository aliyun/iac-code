/** Explicit OPS-only synthetic Chrome probe. Not cloud acceptance or an ordinary offline test. */
import fs from 'node:fs';
import path from 'node:path';
import {createRequire} from 'node:module';
import {createHash} from 'node:crypto';
import {captureNavigationPixels} from '../../scripts/web/e2e/resource_link_workflow/browser.mjs';

const output=process.argv[2];
if(!output||!path.isAbsolute(output)||!process.env.RESOURCE_LINK_PLAYWRIGHT_ROOT)
  throw Error('absolute new output directory and explicit Playwright root required');
fs.mkdirSync(output,{recursive:false,mode:0o700});
const require=createRequire(import.meta.url);
const {chromium}=require(path.join(process.env.RESOURCE_LINK_PLAYWRIGHT_ROOT,'playwright'));
const browser=await chromium.connectOverCDP('http://127.0.0.1:9225');
const pages=[];
const results=[];
try{
  const context=browser.contexts()[0];
  for(let index=0;index<4;index++){
    const page=await context.newPage();pages.push(page);
    // Fulfill every request locally: these reserved .invalid hosts never reach a server.
    await page.route('**/*',route=>{
      const frame=new URL(route.request().url()).hostname==='console-probe.invalid';
      const html=frame?`<body style="font:28px sans-serif;background:#eef2ff;color:#18243d;padding:32px">
        <h1>CAPTURE PROBE ${index}</h1><p>synthetic-resource-${index}</p><p>synthetic-name-${index}</p>
        <p>Cross-origin iframe pixels — not Alibaba Cloud evidence</p></body>`:
        `<body style="margin:24px;background:#fff;font:20px sans-serif;color:#18243d">
        <h2>OPS local screenshot synchronization probe ${index}</h2>
        <iframe src="http://console-probe.invalid/${index}" style="width:90vw;height:60vh;border:2px solid #3742a8"></iframe></body>`;
      return route.fulfill({status:200,contentType:'text/html; charset=utf-8',body:html});
    });
    await page.goto(`http://capture-probe.invalid/${index}`,{waitUntil:'load'});
  }
  await Promise.all(pages.map(async(page,index)=>{
    const frame=await page.locator('iframe').elementHandle();
    const consoleFrame=await frame.contentFrame();await frame.dispose();
    const screenshot=path.join(output,`synthetic-${index}.png`);
    const stages=await captureNavigationPixels(page,screenshot,{
      lockFile:path.join(output,'foreground.lock'),mode:'iframe',consoleFrame,
      identifier:`synthetic-resource-${index}`,markers:[`synthetic-name-${index}`]
    });
    results[index]={index,synthetic_only:true,cloud_acceptance:false,
      screenshot:path.basename(screenshot),sha256:createHash('sha256').update(fs.readFileSync(screenshot)).digest('hex'),stages};
  }));
  const report={kind:'ops_synthetic_capture_probe',cloud_calls:0,cloud_acceptance:false,results};
  fs.writeFileSync(path.join(output,'probe.json'),JSON.stringify(report,null,2),{flag:'wx',mode:0o600});
  process.stdout.write(JSON.stringify(report));
}finally{
  for(const page of pages)await page.close();
  // Disconnect this CDP client only; never close the shared Chrome instance.
  process.exitCode=results.length===4?0:1;
}
process.exit(process.exitCode||0);
