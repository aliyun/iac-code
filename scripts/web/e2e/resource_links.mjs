#!/usr/bin/env node
// Offline real-DOM checks. Reuses the optional browser runtime, never cloud APIs.
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import fs from "node:fs";
import http from "node:http";
import https from "node:https";
import net from "node:net";
import { execFileSync, spawn } from "node:child_process";
import { createInterface } from "node:readline";
import { createHash } from "node:crypto";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { verifyResourceApplication } from './resource_application.mjs';
import {
  verifyIncrementalResourceLinks,
  verifyResourceSchedulerCancellation,
  verifyResourceSchedulerInteractionPause,
  verifyLastRequestRemoval,
} from './resource_incremental.mjs';

const require = createRequire(import.meta.url);
const modules = process.env.IAC_RESOURCE_TEST_NODE_MODULES;
const { chromium } = require(modules ? path.join(modules, "playwright-core") : "playwright-core");
const repo = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../..");
const staticRoot = path.join(repo, "src/iac_code/web/static");
const rawCatalog = JSON.parse(fs.readFileSync(path.join(staticRoot,"ros_resource_links.json"),"utf8"));
const catalogPayload = {
  schemaVersion:rawCatalog.schemaVersion, checkedAt:rawCatalog.checkedAt,
  hashes:{aliyun:rawCatalog.official.sha256,alibabacloud:rawCatalog.sin.sha256},
  officialGroups:rawCatalog.official.groups,
  sinHostSubstitution:rawCatalog.sin.hostSubstitution,
  sinOverrides:Object.fromEntries(rawCatalog.sin.overrides),
};
let apiOrigin = null;
const server = http.createServer((request, response) => {
  const url = new URL(request.url, "http://localhost");
  if (apiOrigin && (url.pathname.startsWith('/api/') || url.pathname.startsWith('/fixture/'))) {
    // This target is the loopback child fixture, never a client-supplied URL.
    const upstream=http.request(new URL(request.url,apiOrigin),{method:request.method,headers:request.headers},(incoming)=>{
      response.writeHead(incoming.statusCode,incoming.headers); incoming.pipe(response);
    });
    upstream.on('error',()=>{response.writeHead(502);response.end();});
    request.pipe(upstream); return;
  }
  if (url.pathname === "/") {
    response.setHeader("Content-Type", "text/html");
    response.end('<!doctype html><html><head><link rel="stylesheet" href="/static/styles.css"><script>globalThis.__IAC_RESOURCE_LINKS__='+
      JSON.stringify(catalogPayload).replaceAll('<','\\u003c')+'</script></head><body><main id="fixture"></main><script src="/static/js/vendor/markdown-it.min.js"></script></body></html>');
    return;
  }
  const file = path.resolve(staticRoot, `.${url.pathname.replace(/^\/static/, "")}`);
  if (!file.startsWith(staticRoot + path.sep) || !fs.existsSync(file)) { response.writeHead(404); response.end(); return; }
  response.setHeader("Content-Type", file.endsWith(".css") ? "text/css" : "text/javascript");
  response.end(fs.readFileSync(file));
});
await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
const origin = `http://127.0.0.1:${server.address().port}`;
let browser, consoleServer, proxy, certDirectory, apiProcess, apiDirectory;
const proxySockets = new Set();
const proxyTargets = [];
async function stopApiFixture() {
  if (!apiProcess || apiProcess.exitCode !== null || apiProcess.signalCode !== null) return;
  await new Promise((resolve)=>{
    const timeout=setTimeout(()=>apiProcess.kill('SIGKILL'),5000);
    apiProcess.once('exit',()=>{clearTimeout(timeout);resolve();});
    apiProcess.kill('SIGTERM');
  });
}
async function startApiFixture() {
  const fixtureEnvironment={PATH:process.env.PATH,IAC_CODE_CONFIG_DIR:path.join(apiDirectory,'config'),PYTHONIOENCODING:'utf-8',
    IAC_CODE_DISABLE_NONESSENTIAL_TRAFFIC:'1',DISABLE_TELEMETRY:'1'};
  for (const key of ['SystemRoot','SYSTEMROOT','TEMP','TMP']) if (process.env[key]) fixtureEnvironment[key]=process.env[key];
  // Resolve the worktree's existing uv interpreter, then own the server PID
  // directly. Killing an uv wrapper can leave its Python child waiting on a
  // pending permission with closed listeners and inherited stdout/stderr pipes.
  const python=execFileSync('uv',['run','--offline','--no-sync','python','-c','import sys; print(sys.executable)'],
    {cwd:repo,env:fixtureEnvironment,encoding:'utf-8',timeout:15000}).trim();
  assert.ok(path.isAbsolute(python) && fs.existsSync(python),'uv must resolve an existing interpreter');
  apiProcess=spawn(python,['scripts/web/e2e/resource_choice_fixture.py','--state-dir',apiDirectory],
    {cwd:repo,env:fixtureEnvironment,stdio:['ignore','pipe','pipe']});
  let fixtureErrors=''; apiProcess.stderr.on('data',(chunk)=>{fixtureErrors=(fixtureErrors+chunk.toString()).slice(-2000);});
  const lines=createInterface({input:apiProcess.stdout});
  const fixturePort=await new Promise((resolve,reject)=>{
    const timeout=setTimeout(()=>reject(Error(`HTTP fixture did not start: ${fixtureErrors}`)),15000);
    apiProcess.once('error',(error)=>{clearTimeout(timeout);reject(error);});
    apiProcess.once('exit',(code)=>{clearTimeout(timeout);reject(Error(`HTTP fixture exited ${code}: ${fixtureErrors}`));});
    lines.once('line',(line)=>{clearTimeout(timeout);try {
      const ready=JSON.parse(line);assert.equal(ready.pid,apiProcess.pid,'the owned process must be the actual HTTP server');
      resolve(ready.port);
    } catch(error) {reject(error);}});
  });
  assert.ok(Number.isInteger(fixturePort) && fixturePort>0);
  apiOrigin=`http://127.0.0.1:${fixturePort}`;
  return apiOrigin;
}
async function restartApiFixture() {
  const previousPid=apiProcess.pid;
  await stopApiFixture();
  let oldServerAlive=false;
  try {await fetch(apiOrigin+'/health',{signal:AbortSignal.timeout(2000)});oldServerAlive=true;} catch { /* Closed listener. */ }
  assert.equal(oldServerAlive,false,'the old HTTP server must actually stop before restarting');
  const origin=await startApiFixture();
  assert.notEqual(apiProcess.pid,previousPid,'restart must replace the actual server process');
  return origin;
}
try {
  apiDirectory=fs.mkdtempSync(path.join(repo,'.venv/resource-choice-http.'));
  await startApiFixture();
  // Browser-level isolation is essential: a modifier-click's first request
  // can precede Playwright's page attachment/interception. The proxy NEVER
  // forwards to the Internet; CONNECT can reach only this local TLS fixture.
  certDirectory = fs.mkdtempSync(path.join(repo,'.venv/resource-links-tls.'));
  const key = path.join(certDirectory,'key.pem'), cert = path.join(certDirectory,'cert.pem');
  execFileSync('openssl',['req','-x509','-newkey','rsa:2048','-nodes','-keyout',key,'-out',cert,
    '-days','1','-subj','/CN=resource-links.test'],{stdio:'ignore'});
  const publicKey = execFileSync('openssl',['x509','-in',cert,'-pubkey','-noout']);
  const publicDer = execFileSync('openssl',['pkey','-pubin','-outform','der'],{input:publicKey});
  const testCertificate = createHash('sha256').update(publicDer).digest('base64');
  consoleServer = https.createServer({key:fs.readFileSync(key),cert:fs.readFileSync(cert)}, (_request,response) => {
    response.setHeader('Content-Type','text/html');
    response.end('<title>Offline console fixture</title>');
  });
  await new Promise((resolve) => consoleServer.listen(0,'127.0.0.1',resolve));
  proxy = http.createServer((request,response) => {
    const deny=()=>{response.writeHead(403);response.end();};
    let target;
    try {target=new URL(request.url);} catch {deny();return;}
    const allowedPorts=new Set([String(server.address().port),new URL(apiOrigin).port]);
    if (target.protocol!=='http:' || target.hostname!=='127.0.0.1' || !allowedPorts.has(target.port)) {
      deny();return;
    }
    const upstream=http.request(target,{method:request.method,headers:request.headers},incoming=>{
      response.writeHead(incoming.statusCode,incoming.headers);incoming.pipe(response);
    });
    upstream.on('error',()=>{if(!response.headersSent)response.writeHead(502);response.end();});
    request.pipe(upstream);
  });
  proxy.on('connect',(request,client,head) => {
    const offlineConsoleHosts=new Set([
      'ram.console.aliyun.com:443','ram4service.console.aliyun.com:443',
      'ros.console.aliyun.com:443','ros4service.console.aliyun.com:443',
    ]);
    if (!offlineConsoleHosts.has(request.url)) {client.end('HTTP/1.1 403 Forbidden\r\n\r\n'); return;}
    proxyTargets.push(request.url);
    const local = net.connect(consoleServer.address().port,'127.0.0.1',() => {
      client.write('HTTP/1.1 200 Connection Established\r\n\r\n');
      if (head.length) local.write(head);
      client.pipe(local); local.pipe(client);
    });
    for (const socket of [local,client]) {
      proxySockets.add(socket);
      socket.on('error',() => {local.destroy(); client.destroy();});
      socket.on('close',() => proxySockets.delete(socket));
    }
  });
  await new Promise((resolve) => proxy.listen(0,'127.0.0.1',resolve));
  // Trust only this newly generated fixture certificate before any page is
  // attached; do not install certificates or change the user's trust store.
  browser = await chromium.launch({headless: true,
    args:['--disable-quic',`--ignore-certificate-errors-spki-list=${testCertificate}`],
    proxy:{server:`http://127.0.0.1:${proxy.address().port}`,bypass:'127.0.0.1'},
    ...(process.env.IAC_RESOURCE_TEST_BROWSER ? {executablePath: process.env.IAC_RESOURCE_TEST_BROWSER} : {})});
  if (process.env.IAC_RESOURCE_TEST_APPLICATION_ONLY === '1') {
    console.log(JSON.stringify({application:await verifyResourceApplication({browser,origin:apiOrigin,restart:restartApiFixture})}));
  } else {
  const context = await browser.newContext();
  const opened = [];
  await context.route("**/*", (route) => {
    const url = route.request().url();
    if (url.startsWith(origin + "/")) return route.continue();
    opened.push(url);
    return route.fulfill({status: 200, contentType: "text/html", body: "<title>Offline console fixture</title>"});
  });
  const page = await context.newPage();
  let openedPages = 0;
  context.on('page', () => openedPages++);
  const errors = [];
  page.on("pageerror", (error) => errors.push(String(error)));
  await page.goto(origin);
  const result = await page.evaluate(async () => {
    const {ResourceIndex} = await import("/static/js/resource_index.js?v=resource-references-v11");
    const ruleModule = await import("/static/js/resource_link_rules.js?v=resource-links-v49");
    // Keep this exact URL aligned with app.js. Query-distinct ES modules have
    // separate singleton controllers, so a stale token would register rendered
    // messages with a different controller from the one exercised here.
    const links = await import("/static/js/resource_linkify.js?v=resource-links-v68");
    const {scanResourceOccurrences} = await import("/static/js/resource_occurrences.js?v=resource-links-v50");
    const app = await import("/static/js/app.js?v=resource-links-v88");
    const {renderToolCards} = await import("/static/js/components/tool_cards.js?v=resource-links-v60");
    const {closeResourceLinkPopover,openResourceLinkPopover} = await import("/static/js/components/resource_link_popover.js?v=resource-links-v50");
    const {closeResourceLinkPreview,openResourceLinkPreview} = await import("/static/js/components/resource_link_preview.js?v=resource-links-v60");
    const check = (condition, message) => { if (!condition) throw Error(message); };
    const index = new ResourceIndex({canonicalType: ruleModule.canonicalResourceType});
    index.reset("offline-session");
    const fact = (resourceType, identifier, identifierField, kind = "id", extra = {}) => ({
      provider:"aliyun", resourceType, identifier, identifierKind:kind, identifierField,
      referenceId:`ref-${identifier}`, accountScope:"fake-account", consolePartition:"aliyun",
      scopeKind:kind === "name" && resourceType.includes("RAM") ? "account" : "regional",
      regionId:"cn-hangzhou", attributes:{}, aliases:[], sources:[{toolUseId:"tool-1"}], ...extra,
    });
    const role = fact("ALIYUN::RAM::Role", "DeployRole", "RoleName", "name");
    const instance = fact("ALIYUN::ECS::Instance", "i-design0000000001", "InstanceId");
    const fn = fact("ALIYUN::FC::Function", "Handler", "FunctionName", "name", {attributes:{ServiceName:"ServiceA"}});
    const execution = fact("ALIYUN::OOS::Execution", "exec-catalog000001", "PhysicalResourceId");
    const stack = fact("ALIYUN::ROS::Stack", "a67cf255-4dae-49cd-8344-c18d2f2b374a", "StackId", "id",
      {sources:[{toolUseId:"tool-stack-current",messageId:"stack-tool-message",turnId:"stack-turn"}]});
    const historicalStack = {...stack, referenceId:"ref-stack-historical", accountScope:undefined,
      consolePartition:undefined, sources:[{toolUseId:"tool-stack-historical",messageId:"old-stack-message",turnId:"old-stack-turn"}]};
    const olderHistoricalStack = {...historicalStack,referenceId:"ref-stack-older",
      sources:[{toolUseId:"tool-stack-older",messageId:"older-stack-message",turnId:"older-stack-turn"}]};
    await index.loadSnapshot({schemaVersion:1, revision:1,
      items:[role, instance, fn, execution, olderHistoricalStack, historicalStack, stack], bindings:[]}, () => {throw Error("unexpected network");});
    // Test rules are injected here only. They do not enable unverified routes in the app.
    const rules = [role, instance, fn].map((item) => ({
      ruleId:`fixture-${item.resourceType}`, version:1, resourceType:item.resourceType,
      partition:"aliyun", releaseStatus:"verified", host:"ram.console.aliyun.com", identifierField:item.identifierField,
      provenance:{source:"offline-test-fixture"},
      build:(reference) => `https://ram.console.aliyun.com/roles/${encodeURIComponent(reference.identifier)}`,
    }));
    rules.push(ruleModule.RESOURCE_LINK_RULES.find((item) =>
      item.sourceType === execution.resourceType && item.partition === "aliyun"));
    rules.push(ruleModule.RESOURCE_LINK_RULES.find((item) =>
      item.sourceType === stack.resourceType && item.partition === "aliyun"));
    const fixture = document.getElementById("fixture");
    const aliasIndex=new ResourceIndex({canonicalType:ruleModule.canonicalResourceType});
    aliasIndex.reset('alias-probe');
    await aliasIndex.loadSnapshot({schemaVersion:1,revision:1,items:[{...role,aliases:[{
      identifier:'123456789',identifierKind:'id',identifierField:'RoleId',
    }]}],bindings:[]},()=>{throw Error('alias must not cause a query');});
    const aliasProbe=document.createElement('code');aliasProbe.textContent='123456789';
    links.decorateResourceLinks(aliasProbe,{index:aliasIndex,rules,context:{resourceType:role.resourceType,field:'RoleId'}});
    check(aliasProbe.querySelector('a')?.href==='https://ram.console.aliyun.com/roles/DeployRole',
      'a structured RoleId must resolve through the same object name, not be treated as RoleName');
    check(aliasProbe.textContent==='123456789' && aliasProbe.querySelector('a')?.getAttribute('aria-label')==='123456789',
      'stable ID alias changed the displayed/copied/accessibility value');
    let activations = 0;
    let embeddedSignin = true;
    const initialController = links.configureResourceLinks({index, rules, shouldPreview:()=>embeddedSignin,
      onActivate:(anchor, occurrence, resolution, context) => {
      activations += 1;
      if(resolution.status==='resolved'&&context.activationMode!=='context'){
        closeResourceLinkPopover();openResourceLinkPreview(anchor,occurrence,resolution,{index,rules,...context});
      }else{closeResourceLinkPreview();openResourceLinkPopover(anchor,occurrence,resolution,{index,rules,...context});}
    }});
    const cases = [
      ["user", "RAM 角色 `DeployRole` 与 i-design0000000001"],
      ["assistant", "实例 i-design0000000001；RAM role DeployRole"],
      ["table", "| RoleName | InstanceId |\n| --- | --- |\n| DeployRole | i-design0000000001 |"],
      ["inline", "`i-design0000000001`，RAM 角色 **DeployRole**"],
      ["json", '```json\n{"RoleName":"DeployRole","InstanceId":"i-design0000000001"}\n```'],
      ["yaml", '```yaml\nRoleName: DeployRole\nInstanceId: i-design0000000001\n```'],
      ["hcl", '```hcl\nresource "alicloud_ram_role" "example" { name = "DeployRole" }\n# i-design0000000001\n```'],
      ["shell", '```shell\naliyun ram GetRole --RoleName DeployRole\necho i-design0000000001\n```'],
    ];
    const expected = new Map();
    for (const [source,count] of [
      ['RAM role DeployRole',0], ['RAM role DeployRole ',1],
      ['i-design0000000001',0], ['i-design0000000001\n',1],
      ['RAM role DeployRole\n\nRAM role DeployRole',1],
    ]) {
      const node=document.createElement('div');
      app.renderMarkdownInto(node,source);
      const before=node.textContent;
      links.decorateResourceLinks(node,{index,rules,complete:false,bindingValue:source});
      check(node.querySelectorAll('[data-resource-link]').length===count,'Markdown synthetic boundary: '+JSON.stringify(source));
      check(node.textContent===before,'stream boundary handling changed source');
    }
    for (const [id, content] of cases) {
      const node = document.createElement("div"); node.id = id; fixture.append(node);
      app.renderMarkdownInto(node, content, {source:{messageId:id}, complete:true});
      expected.set(node, node.textContent);
    }
    const stackTable = document.createElement("div"); stackTable.id = "stack-table"; fixture.append(stackTable);
    app.renderMarkdownInto(stackTable,
      `| 栈名称 | 状态 | 类型 | 栈 ID |\n| --- | --- | --- | --- |\n| wait-condition-test | CREATE_COMPLETE | ROS | ${stack.identifier} |`,
      {source:{messageId:"stack-message",turnId:"stack-turn"}, complete:true});
    expected.set(stackTable, stackTable.textContent);
    const oldStackTable = document.createElement("div"); oldStackTable.id = "old-stack-table"; fixture.append(oldStackTable);
    app.renderMarkdownInto(oldStackTable,
      `| 栈名称 | 栈 ID |\n| --- | --- |\n| wait-condition-test | ${stack.identifier} |`,
      {source:{messageId:"old-stack-message"},context:{accountScope:"fake-account",consolePartition:"aliyun",
        regionId:"cn-hangzhou"},complete:true});
    expected.set(oldStackTable, oldStackTable.textContent);
    const tools = renderToolCards({tools:{"tool-1":{
      toolUseId:"tool-1", toolName:"aliyun_api", status:"completed",
      input:{product:"ram", action:"GetRole", params:{RoleName:"DeployRole"}},
      results:[{content:JSON.stringify({Role:{RoleName:"DeployRole"}, InstanceId:instance.identifier})}],
      stackProgress:{stackId:"stack-fixture", regionId:"cn-hangzhou", accountScope:"fake-account", consolePartition:"aliyun",
        resources:[{name:"LogicalOnly", resource_type:role.resourceType, attributes:{RoleName:"DeployRole"}, physical_resource_id:"DeployRole"}]},
    }}});
    tools.id = "tools"; fixture.append(tools); expected.set(tools, tools.textContent);
    const shell = renderToolCards({tools:{shell:{toolUseId:"shell",toolName:"bash",status:"completed",
      input:{command:"aliyun ram GetRole --RoleName DeployRole"},
      stdout:"RAM role DeployRole i-design0000000001", stderr:"i-design0000000001",
    }}});
    shell.id = "shell-output"; fixture.append(shell); expected.set(shell, shell.textContent);
    const highlighted = document.createElement("pre"); highlighted.id = "highlighted";
    highlighted.innerHTML = '<code>RoleName: <span>Deploy</span><span>Role</span>\n<span>i-design</span><span>0000000001</span></code>';
    fixture.append(highlighted); expected.set(highlighted, highlighted.textContent);
    links.registerResourceText(highlighted, {source:{messageId:"highlighted"}, complete:true});
    const negative = document.createElement("div"); negative.id = "negative";
    negative.textContent = 'admin DeployRole DeployRoleBackup DisplayName: DeployRole LogicalResourceId: DeployRole i-*** ${i-design0000000001} <i-design0000000001> https://example.test/i-design0000000001';
    fixture.append(negative); links.registerResourceText(negative, {complete:true});
    const catalogLink = document.createElement("div"); catalogLink.id = "catalog-link";
    catalogLink.textContent = execution.identifier; fixture.append(catalogLink);
    links.registerResourceText(catalogLink, {source:{messageId:"catalog-link"},complete:true,
      context:{resourceType:execution.resourceType,field:execution.identifierField}});
    await initialController.whenIdle();
    for (const [node, original] of expected) check(node.textContent === original, `${node.id}: changed copy text`);
    for (const [id] of cases) {
      const node = document.getElementById(id);
      check([...node.querySelectorAll("a.resource-link")].some((a) => a.textContent === "DeployRole"),
        `${id}: missing role link: ${node.innerHTML}`);
      check([...node.querySelectorAll("a.resource-link")].some((a) => a.textContent === instance.identifier), `${id}: missing ID link`);
    }
    check(negative.querySelectorAll(".resource-link").length === 0, "negative text was linked");
    check(catalogLink.querySelector("a.resource-link")?.href ===
      "https://oos.console.aliyun.com/cn-hangzhou/execution/detail/exec-catalog000001",
      `full ROS channel catalog route was not applied in the browser: ${catalogLink.innerHTML}`);
    check(stackTable.querySelector("a.resource-link")?.href ===
      `https://ros.console.aliyun.com/cn-hangzhou/stacks/${stack.identifier}`,
      "ListStacks assistant table did not link its Stack ID");
    check(oldStackTable.querySelector("a.resource-link")?.href ===
      `https://ros.console.aliyun.com/cn-hangzhou/stacks/${stack.identifier}`,
      `a scoped Stack ID fact did not collapse compatible historical duplicates: ${oldStackTable.innerHTML}`);
    check(highlighted.querySelectorAll(".resource-link").length === 4, "cross-highlight matches incomplete");
    check(highlighted.querySelectorAll('[aria-hidden="true"]').length === 2, "duplicate accessible stops");
    check(tools.querySelectorAll("a.resource-link").length >= 3, "tool input/structured result/progress not covered");
    check([...tools.querySelectorAll('.tool-stack-progress-table a.resource-link')].some((node)=>node.textContent==='DeployRole'),
      'localized progress-table headers overrode the explicit RoleName field');
    check(shell.querySelectorAll("a.resource-link").length >= 3, "shell command/stdout/stderr not covered");
    links.decorateResourceLinks(highlighted, {index, rules});
    links.decorateResourceLinks(highlighted, {index, rules});
    check(highlighted.querySelectorAll(".resource-link").length === 4, "decoration not idempotent");
    check(highlighted.textContent === expected.get(highlighted), "idempotent pass changed source");
    const partial = document.createElement("p"); partial.id = "partial"; partial.innerHTML = '<code>i-design0000000001</code>';
    fixture.append(partial); links.decorateResourceLinks(partial,{index,rules,complete:false});
    check(!partial.querySelector(".resource-link"), "stream tail linked before boundary");
    links.decorateResourceLinks(partial,{index,rules,complete:true});
    check(partial.querySelector(".resource-link"), "completed tail not linked");
    const range = document.createRange(); range.selectNodeContents(highlighted);
    const selection = window.getSelection(); selection.removeAllRanges(); selection.addRange(range);
    const before = highlighted.innerHTML;
    links.decorateResourceLinks(highlighted, {index, rules});
    check(highlighted.innerHTML === before && selection.toString() === expected.get(highlighted), "selection was damaged");
    selection.removeAllRanges();
    const names = scanResourceOccurrences('- RoleName: DeployRole\n  RegionId: cn-beijing\n- RoleName: OtherRole\n  RegionId: cn-shanghai', {index});
    check(names[0]?.regionId === "cn-beijing" && names[1]?.regionId === "cn-shanghai", "YAML siblings borrowed region");
    const beforeFact = document.createElement("p"); beforeFact.id = "backfill"; beforeFact.textContent = "11111111-2222-4333-8444-555555555555";
    fixture.append(beforeFact); links.registerResourceText(beforeFact,{source:{toolUseId:"tool-1"},complete:true});
    await initialController.whenIdle();
    index.applyDelta({schemaVersion:1,baseRevision:1,revision:2,upserts:[fact("ALIYUN::ROS::Stack",beforeFact.textContent,"StackId")],removedReferenceIds:[],bindings:[]});
    await initialController.whenIdle();
    check(beforeFact.querySelector("a"), "new fact did not backfill earlier plain UUID");
    window.resourceTest = {index, rules, links, role, instance, controller:initialController, activations:() => activations,
      setEmbeddedSignin:(enabled)=>{embeddedSignin=enabled;}};
    return {cases:cases.length, markdownStreamBoundaries:5, structuredStableId:true,
      wrappers:fixture.querySelectorAll(".resource-link").length, copyContainers:expected.size};
  });
  const first = page.locator("#user a.resource-link").first();
  await first.click();
  const preview = page.locator('.resource-link-preview');
  await preview.waitFor();
  assert.match(await preview.locator('iframe').getAttribute('src'),/^https:\/\/ram\.console\.aliyun\.com\/roles\//);
  const viewport = page.viewportSize();
  const previewBounds = await preview.boundingBox();
  assert.ok(Math.abs(previewBounds.width-viewport.width*2/3)<2,'resource preview is not two thirds of the viewport');
  assert.equal(page.url(), origin + "/");
  await page.mouse.click(8,Math.floor(viewport.height/2));
  await preview.waitFor({state:'detached'});
  const shiftPopup = context.waitForEvent("page");
  await first.click({modifiers:['Shift']});
  const shifted = await shiftPopup;
  await shifted.waitForURL(/^https:\/\/ram\.console\.aliyun\.com\/roles\//);
  await shifted.waitForLoadState();
  assert.match(shifted.url(), /^https:\/\/ram\.console\.aliyun\.com\/roles\//);
  assert.equal(await shifted.title(),'Offline console fixture');
  await shifted.close();
  await page.evaluate(()=>window.resourceTest.setEmbeddedSignin(false));
  const defaultPopup = context.waitForEvent("page");
  await first.click();
  const defaultExternal = await defaultPopup;
  await defaultExternal.waitForURL(/^https:\/\/ram\.console\.aliyun\.com\/roles\//);
  await defaultExternal.waitForLoadState();
  assert.match(defaultExternal.url(), /^https:\/\/ram\.console\.aliyun\.com\/roles\//);
  assert.equal(await page.locator('.resource-link-preview').count(),0,
    'a normal credential must use native new-tab navigation');
  await defaultExternal.close();
  await page.evaluate(()=>window.resourceTest.setEmbeddedSignin(true));
  const modifiedPopup = context.waitForEvent("page");
  await first.click({modifiers:[process.platform === "darwin" ? "Meta" : "Control"]});
  const modified = await modifiedPopup;
  await modified.bringToFront();
  await modified.waitForURL(/^https:\/\/ram\.console\.aliyun\.com\/roles\//, {timeout:10000}).catch(async (error) => {
    throw Error(JSON.stringify({stage:'modified-navigation',pages:context.pages().map((item) => item.url()),
      opened,proxyTargets,anchor:await first.evaluate((node) => node.outerHTML)}),{cause:error});
  });
  assert.equal(await modified.title(),'Offline console fixture');
  await modified.close();
  const middlePopup = context.waitForEvent("page");
  await first.click({button:"middle"});
  const middle = await middlePopup;
  await middle.bringToFront();
  await middle.waitForURL(/^https:\/\/ram\.console\.aliyun\.com\/roles\//, {timeout:10000});
  assert.equal(await middle.title(),'Offline console fixture');
  await middle.close();
  assert.equal(openedPages,4,'each external activation must create exactly one new page');
  assert.equal(page.url(),origin + '/');
  const interaction = await page.evaluate(async () => {
    const {index, rules, role, instance, links, controller} = window.resourceTest;
    const check = (condition, message) => {if (!condition) throw Error(message);};
    const idle = (label) => Promise.race([controller.whenIdle(),new Promise((_,reject) =>
      setTimeout(() => reject(Error(`resource controller did not become idle: ${label}`)),5000))]);
    const waitFor = async (predicate, message) => {
      const deadline = performance.now() + 3000;
      while (!predicate() && performance.now() < deadline) await new Promise((resolve) => setTimeout(resolve, 10));
      check(predicate(), message);
    };
    const highlighted = document.getElementById("highlighted");
    const range = document.createRange(); range.selectNodeContents(highlighted);
    const selection = window.getSelection(); selection.removeAllRanges(); selection.addRange(range);
    const click = new MouseEvent("click", {bubbles:true, cancelable:true});
    highlighted.querySelector("a").dispatchEvent(click);
    check(click.defaultPrevented, "selected text activation was not suppressed");
    selection.removeAllRanges();
    const card = document.querySelector("#tools details.tool-card");
    card.open = false;
    const title = card.querySelector("summary");
    const probe = document.createElement("span"); probe.textContent = instance.identifier; title.append(probe);
    links.decorateResourceLinks(probe, {index,rules});
    let parentClicks = 0;
    card.addEventListener("click", () => parentClicks++);
    const event = new MouseEvent("click", {bubbles:true,cancelable:true});
    // Prevent navigation at capture only for this event, so no additional popup.
    probe.querySelector("a").addEventListener("click", (e) => e.preventDefault(), {once:true,capture:true});
    probe.querySelector("a").dispatchEvent(event);
    check(parentClicks === 0 && card.open === false, "resource click toggled its tool card");
    index.applyDelta({schemaVersion:1,baseRevision:2,revision:3,
      upserts:[{...role,referenceId:"ref-role-other",accountScope:"other-account",sources:[{toolUseId:"tool-2"}]}],
      removedReferenceIds:[],bindings:[]});
    await idle('ambiguity');
    const ambiguous = document.querySelector("#user button.resource-link-pending");
    check(ambiguous, "new ambiguity did not retract a unique inferred link");
    ambiguous.click();
    check(document.querySelector('[role="dialog"].resource-link-popover'), "ambiguity did not open a dialog");
    const candidate = [...document.querySelectorAll('.resource-link-popover button')]
      .find((button) => button.textContent.includes('fake-account'));
    candidate.click();
    check(!document.querySelector('.resource-link-popover [name="regionId"]'), "RAM role incorrectly asks for region");
    check(document.querySelector('.resource-link-popover a[href]'), "chosen known account has no real target");
    const select = [...document.querySelectorAll('.resource-link-popover button')].find((button) => button.textContent === 'Select');
    check(select && !select.disabled, "explicit selection action is missing");
    select.click();
    await idle('selected-choice');
    check(document.querySelector('#user a.resource-link'), "selection did not turn the original occurrence into a link");
    check(document.querySelector('#assistant button.resource-link-pending'), "choice leaked into another message");
    const user = document.getElementById('user');
    const original = user.textContent;
    user.textContent = original;
    links.registerResourceText(user,{source:{messageId:'user'},bindingValue:'RAM 角色 `DeployRole` 与 i-design0000000001',complete:true});
    await idle('unchanged-rerender');
    check(!user.querySelector('button.resource-link-pending'), "local choice was lost in an unchanged re-render");
    const focused = user.querySelector('a.resource-link');
    focused.focus();
    index.applyDelta({schemaVersion:1,baseRevision:3,revision:4,
      upserts:[{...role,status:'UPDATE_COMPLETE'}],removedReferenceIds:[],bindings:[]});
    await idle('status-update');
    check(document.activeElement === user.querySelector('a.resource-link'), "index update lost keyboard focus");
    // A deletion delivered before the next rendering frame must already stop
    // navigation. The stored choice must not erase the live lifecycle status.
    const oldLink = user.querySelector('a.resource-link');
    index.applyDelta({schemaVersion:1,baseRevision:4,revision:5,
      upserts:[{...role,status:'DELETE_COMPLETE'}],removedReferenceIds:[],bindings:[]});
    const staleClick = new MouseEvent('click',{bubbles:true,cancelable:true});
    oldLink.dispatchEvent(staleClick);
    check(staleClick.defaultPrevented, "stale resolved anchor navigated after deletion");
    await idle('deletion');
    check(user.querySelector('button.resource-link-pending'), "selected deleted name remained an unqualified link");
    document.querySelector('.resource-link-popover button')?.click();
    user.textContent = original + ' changed source';
    links.registerResourceText(user,{source:{messageId:'user'},complete:true});
    await idle('changed-source');
    check(user.querySelector('button.resource-link-pending'), `changed-source pending missing: ${user.outerHTML}`);
    user.querySelector('button.resource-link-pending').click();
    check(document.querySelector('.resource-link-popover'), "changed source did not re-enter choice state");
    user.textContent = 'replacement without the resource';
    links.registerResourceText(user,{source:{messageId:'user'},complete:true});
    await idle('source-replacement');
    check(!document.querySelector('.resource-link-popover'), "source replacement left a stale dialog");
    // An incomplete ID has no reference yet; choose only explicit existing
    // account scope and fill the regional rule's missing fields locally.
    const missing = document.createElement('p'); missing.id = 'missing-context';
    missing.textContent = 'i-missing0000001'; document.getElementById('fixture').append(missing);
    links.registerResourceText(missing,{source:{messageId:'missing-context'},complete:true});
    await waitFor(() => missing.querySelector('button'), 'missing-context identifier was not decorated');
    missing.querySelector('button').click();
    for (const [name,value] of [['accountScope','fake-account'],['consolePartition','aliyun'],['regionId','cn-hangzhou']]) {
      const input = document.querySelector(`.resource-link-popover [name="${name}"]`);
      check(input && !input.disabled, `missing ${name} cannot be filled`);
      input.value = value; input.dispatchEvent(new Event('input',{bubbles:true}));
    }
    [...document.querySelectorAll('.resource-link-popover button')].find((button) => button.textContent === 'Select').click();
    await waitFor(() => missing.querySelector('a'), 'manual local context was not applied');
    missing.querySelector('a').dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',altKey:true,bubbles:true,cancelable:true}));
    check(document.querySelector('.resource-link-popover'), "context cannot be reopened with Alt+Enter");
    check(document.querySelector('.resource-link-popover [name="regionId"]').value === 'cn-hangzhou', 'reselect lost the local context');
    document.querySelector('.resource-link-popover button').click();
    const siblings = document.createElement('pre'); siblings.id = 'sibling-names';
    siblings.textContent = JSON.stringify([{RoleName:'DeployRole',Tag:'first'},{RoleName:'DeployRole',Tag:'second'}]);
    document.getElementById('fixture').append(siblings);
    links.registerResourceText(siblings,{source:{messageId:'sibling-names'},complete:true});
    await waitFor(() => siblings.querySelectorAll('button.resource-link-pending').length === 2, 'sibling names were not both recognized');
    siblings.querySelector('button').click();
    [...document.querySelectorAll('.resource-link-popover button')].find((button) => button.textContent.includes('other-account')).click();
    [...document.querySelectorAll('.resource-link-popover button')].find((button) => button.textContent === 'Select').click();
    await waitFor(() => siblings.querySelector('a'), 'sibling selection was not applied');
    check(siblings.querySelectorAll('button.resource-link-pending').length === 1, 'choice crossed structured-object boundaries');
    siblings.querySelector('button.resource-link-pending').click();
    index.reset("different-session");
    check(!document.querySelector('.resource-link-popover'), "popover survived a session switch");
    return {selection:true,cardClick:true,ambiguity:true,accountScopedName:true,
      localChoice:true,sourceInvalidation:true,lifecycleRecheck:true,keyboardFocus:true,manualContext:true,objectScopedChoice:true};
  });
  const persistence = await page.evaluate(async () => {
    const {index,rules,role,links} = window.resourceTest;
    const {persistResourceChoice} = await import('/static/js/resource_index.js?v=resource-references-v11');
    const {digestResourceSource,resourceBindingSourceKey} = await import('/static/js/resource_sources.js?v=resource-bindings-v5');
    const {openResourceLinkPopover} = await import('/static/js/components/resource_link_popover.js?v=resource-links-v50');
    const check = (value,message) => {if (!value) throw Error(message);};
    const waitFor = async (predicate,message) => {
      const deadline = performance.now()+4000;
      while (!predicate() && performance.now()<deadline) await new Promise((resolve)=>setTimeout(resolve,10));
      check(predicate(),message);
    };
    const fixture = document.getElementById('fixture'); fixture.replaceChildren();
    const source = {messageId:'saved-user',sourcePath:'/text'};
    const raw = 'RAM role DeployRole';
    const other = {...role,referenceId:'ref-saved-other',accountScope:'other-account',sources:[]};
    const original = {...role,sources:[],status:'CREATE_COMPLETE'};
    let stored = {schemaVersion:1,revision:1,items:[original,other],bindings:[]};
    let attempts = 0, fail = false, release = null;
    const configure = () => links.configureResourceLinks({index,rules,
      onActivate:(anchor,occurrence,resolution,options) => openResourceLinkPopover(anchor,occurrence,resolution,{index,rules,...options}),
      onPersistChoice:(payload,context) => persistResourceChoice(index,payload,{...context,
        fetchPage:async()=>stored,
        save:async(sessionId,request)=> {
          attempts++;
          check(sessionId==='saved-session','save targeted the wrong session');
          check(request.source.messageId===source.messageId,'wrong source owner');
          check(request.sourceDigest===await digestResourceSource(source,raw),'digest did not use the raw source');
          check(request.identifier==='DeployRole' && request.identifierKind==='name','wrong locator');
          check(!('regionId' in request.context),'account-scoped save sent a region');
          await new Promise((resolve)=>{release=resolve;});
          if (fail) throw Object.assign(Error('conflict'),{status:409});
          const binding = {sourceKey:resourceBindingSourceKey(request.sourceDigest,request.group),
            source:request.source,referenceId:request.referenceId,identifier:request.identifier,
            identifierKind:request.identifierKind,context:request.context};
          stored = {...stored,revision:stored.revision+1,bindings:[binding]};
          // The SSE echo can arrive before HTTP completes. It must not close
          // the pending dialog or falsely acknowledge a later failed write.
          if (index.sessionId===sessionId) index.applyDelta({schemaVersion:1,baseRevision:index.revision,revision:stored.revision,
            upserts:[],removedReferenceIds:[],bindings:[binding]});
          return {status:'saved',sourceKey:binding.sourceKey,snapshot:stored};
        },
      }),
    });
    index.reset('saved-session'); await index.loadSnapshot(stored,()=>{throw Error('unexpected fetch');});
    let controller = configure();
    const mount = (bindingValue=raw) => {
      fixture.replaceChildren();
      const node=document.createElement('p'); node.textContent=raw; fixture.append(node);
      links.registerResourceText(node,{source,bindingValue,complete:true});
      return node;
    };
    let node=mount(); await controller.whenIdle();
    const choose = async () => {
      node.querySelector('button.resource-link-pending').click();
      [...document.querySelectorAll('.resource-link-popover button')].find((button)=>button.textContent.includes('other-account')).click();
      const dialog=document.querySelector('.resource-link-popover');
      const focused=document.activeElement, previous=node;
      node=mount(); await controller.whenIdle();
      check(!previous.isConnected && document.querySelector('.resource-link-popover')===dialog,
        'unchanged transcript replacement dismissed the active choice');
      check(document.activeElement===focused && dialog.querySelector('[name="accountScope"]').value==='other-account',
        'unchanged transcript replacement lost focus or the selected account');
      // Also cover re-registering the same DOM root without a content change.
      links.registerResourceText(node,{source,bindingValue:raw,complete:true}); await controller.whenIdle();
      check(document.querySelector('.resource-link-popover')===dialog,'unchanged registration dismissed the active choice');
      [...document.querySelectorAll('.resource-link-popover button')].find((button)=>button.textContent==='Select').click();
      await waitFor(()=>release,'save did not start');
      node=mount(); await controller.whenIdle();
      check(document.querySelector('.resource-link-popover'),'dialog disappeared before save finished');
      check([...document.querySelectorAll('.resource-link-popover button')].find((button)=>button.textContent==='Select').disabled,
        'double-submit was not disabled');
    };
    await choose(); release(); release=null;
    await waitFor(()=>!document.querySelector('.resource-link-popover'),'successful save did not close');
    await controller.whenIdle();
    check(node.querySelector('a.resource-link'),'saved occurrence is not linked');
    check(document.activeElement===node.querySelector('a.resource-link'),'saved choice lost focus');
    check(attempts===1,'save was duplicated');
    // Recreate the controller and source, exactly as refresh does: no local
    // choices survive, only the authoritative snapshot can restore this link.
    controller.dispose(); index.reset('saved-session'); await index.loadSnapshot(stored,()=>{});
    controller=configure(); node=mount(); await controller.whenIdle();
    check(node.querySelector('a.resource-link') && !node.querySelector('button'),'saved binding was not restored');
    check(attempts===1,'restoring or decorating caused a write');
    // Identical rendered text can come from different raw Markdown. The old
    // source digest must not apply simply because textContent is unchanged.
    node.querySelector('a').dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',altKey:true,bubbles:true,cancelable:true}));
    check(document.querySelector('.resource-link-popover'),'restored binding cannot be edited');
    node=mount('RAM role **DeployRole**'); await controller.whenIdle();
    check(!document.querySelector('.resource-link-popover'),'raw source replacement retained an active stale choice');
    check(node.querySelector('button.resource-link-pending'),'old binding survived a raw-only source change');
    node=mount(); await controller.whenIdle();
    check(node.querySelector('a'),'original source failed to restore again');
    const removed=stored.bindings;
    stored={...stored,revision:stored.revision+1,bindings:[]};
    index.applyDelta({schemaVersion:1,baseRevision:index.revision,revision:stored.revision,
      upserts:[],removedReferenceIds:[],bindings:[],removedBindings:removed});
    await controller.whenIdle();
    check(node.querySelector('button.resource-link-pending'),'server revocation was shadowed by a local choice');
    fail=true; await choose(); release(); release=null;
    await waitFor(()=>[...document.querySelectorAll('.resource-link-popover button')].some((button)=>button.textContent==='Select'&&!button.disabled),
      'failed save cannot be retried');
    check(document.querySelector('.resource-link-popover [role="status"]').textContent==='This choice is not saved yet.',
      'failed save was acknowledged as durable');
    fail=false;
    [...document.querySelectorAll('.resource-link-popover button')].find((button)=>button.textContent==='Select').click();
    await waitFor(()=>release,'retry did not start');
    index.reset('different-session');
    release(); release=null;
    await new Promise((resolve)=>setTimeout(resolve,40));
    check(!document.querySelector('.resource-link-popover') && index.references.size===0,'late save leaked across sessions');
    controller.dispose();
    return {savedChoice:true,restoredChoice:true,rawSourceVersion:true,bindingRevocation:true,failedSaveRetry:true,
      lateSaveIsolation:true,activeSourceReplacement:true};
  });
  const httpChoices = await page.evaluate(async () => {
    const {index,rules,links} = window.resourceTest;
    const api=await import('/static/js/api.js?v=resource-references-v11');
    const {persistResourceChoice,queryResourceAttributes}=await import('/static/js/resource_index.js?v=resource-references-v11');
    const {openResourceLinkPopover}=await import('/static/js/components/resource_link_popover.js?v=resource-links-v50');
    const {renderMarkdownInto}=await import('/static/js/app.js?v=resource-links-v88');
    const {renderPermissionRequest}=await import('/static/js/components/blocking.js?v=blocking-keys-v38');
    const check=(value,message)=>{if(!value) throw Error(message);};
    const waitFor=async(predicate,message)=>{
      const until=performance.now()+5000;
      while(!predicate() && performance.now()<until) await new Promise((resolve)=>setTimeout(resolve,10));
      check(predicate(),message);
    };
    const fixture=document.getElementById('fixture');
    let state=await (await fetch('/fixture/resource-choice')).json();
    const fetchPage=(cursor)=>api.getResourceReferences(state.sessionId,cursor);
    const configure=()=>links.configureResourceLinks({index,rules,
      onActivate:(anchor,occurrence,resolution,options)=>openResourceLinkPopover(anchor,occurrence,resolution,{index,rules,...options}),
      onPersistChoice:(payload,options)=>persistResourceChoice(index,payload,{...options,save:api.resolveResourceReference,fetchPage}),
      onQueryAttributes:(payload,options)=>queryResourceAttributes(index,payload,{...options,query:api.resolveResourceReference,fetchPage}),
    });
    const mount=()=>{
      fixture.replaceChildren();
      for(const message of state.messages){
        const node=document.createElement('div');node.id=message.messageId;fixture.append(node);
        renderMarkdownInto(node,message.text,{source:{messageId:message.messageId,sourcePath:'/text'},complete:true});
      }
    };
    index.reset(state.sessionId); await index.loadSnapshot(await fetchPage(),fetchPage);
    let controller=configure();mount();await controller.whenIdle();
    const first=()=>document.getElementById('http-user-1');
    const second=()=>document.getElementById('http-user-2');
    const fn=()=>document.getElementById('http-function');
    const select=()=>[...document.querySelectorAll('.resource-link-popover button')].find((button)=>button.textContent==='Select');
    const fill=(name,value)=>{
      const field=document.querySelector(`.resource-link-popover [name="${name}"]`);
      check(field && !field.disabled,`field ${name} must be editable`);
      field.value=value;field.dispatchEvent(new Event('input',{bubbles:true}));
    };
    first().querySelector('button.resource-link-pending').click();
    fill('accountScope','fake-account');fill('consolePartition','aliyun');
    check(!document.querySelector('.resource-link-popover [name="regionId"]'),'name declaration incorrectly needs region');
    select().click();
    await waitFor(()=>!document.querySelector('.resource-link-popover'),'real HTTP name save did not complete');
    await controller.whenIdle();
    check(first().querySelector('a.resource-link'),'saved HTTP name is not linked');
    check(second().querySelector('button.resource-link-pending'),'user context leaked to another message');
    let stored=await fetchPage();
    check(stored.bindings.length===1,'HTTP sidecar did not retain the selection');
    check(stored.items.find((item)=>item.identifier==='NewHttpRole')?.evidence==='user_context','a declaration became an API observation');
    // Refresh through the real GET handler, which revalidates source versions.
    controller.dispose();index.reset(state.sessionId);await index.loadSnapshot(stored,fetchPage);
    controller=configure();mount();await controller.whenIdle();
    check(first().querySelector('a.resource-link'),'real API snapshot did not restore name binding');
    check(second().querySelector('button.resource-link-pending'),'refresh leaked the saved context');
    fn().querySelector('button').click();
    fill('accountScope','fake-account');fill('consolePartition','aliyun');fill('regionId','cn-hangzhou');fill('ServiceName','ServiceA');
    select().click();await waitFor(()=>!document.querySelector('.resource-link-popover'),'parent-context save did not finish');
    await controller.whenIdle();check(fn().querySelector('a'),'function declaration was not linked');
    stored=await fetchPage();
    const before=stored.items.find((item)=>item.identifier==='HttpHandler');
    check(before?.attributes.ServiceName==='ServiceA','parent was not saved');
    fn().querySelector('a').dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',altKey:true,bubbles:true,cancelable:true}));
    fill('ServiceName','ServiceB');select().click();
    await waitFor(()=>!document.querySelector('.resource-link-popover'),'re-selecting declared context failed');
    stored=await fetchPage();
    const after=stored.items.find((item)=>item.identifier==='HttpHandler');
    check(after?.attributes.ServiceName==='ServiceB' && after.referenceId!==before.referenceId,'parent re-selection rewrote an old identity');
    state=await (await fetch('/fixture/resource-choice',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({messageId:'http-user-1',text:'This source no longer contains a role.'})})).json();
    stored=await fetchPage();
    check(!stored.items.some((item)=>item.identifier==='NewHttpRole'),'changed source left a stale declaration');
    check(!stored.bindings.some((item)=>item.identifier==='NewHttpRole'),'changed source left a stale binding');
    check(stored.items.some((item)=>item.identifier==='SeedRole'),'source cleanup removed an independent API fact');
    await index.loadSnapshot(stored,fetchPage);await controller.whenIdle();
    const fixtureState=async()=> (await fetch('/fixture/resource-choice')).json();
    check(Object.keys((await fixtureState()).queryCalls).length===0,'rendering or saving unexpectedly called a cloud reader');
    const queryRoot=(name)=>document.getElementById('http-query-'+name);
    const lookup=()=>document.querySelector('.resource-link-popover [data-resource-query]');
    const status=()=>document.querySelector('.resource-link-popover [role="status"]');
    const dismiss=()=>[...document.querySelectorAll('.resource-link-popover button')].find((button)=>button.textContent==='Close').click();
    const replacedRoot=queryRoot('ReplacementHandler');
    const replacedText=replacedRoot.textContent;
    const previousFact=[...index.references.values()].find(item=>item.identifier==='ReplacementHandler');
    check(replacedRoot.querySelector('a.resource-link') && previousFact.attributes.ServiceName==='OriginalService',
      'initial result parent was not used');
    await fetch('/fixture/resource-choice',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({action:'replace-result-metadata'})});
    await index.refresh(fetchPage);await controller.whenIdle();
    const currentFact=[...index.references.values()].find(item=>item.identifier==='ReplacementHandler');
    check(currentFact.referenceId===previousFact.referenceId && !currentFact.attributes.ServiceName,
      'same tool metadata replacement retained the old parent or lost stable reference identity');
    check(!replacedRoot.querySelector('a.resource-link') && replacedRoot.querySelector('button.resource-link-pending'),
      'metadata replacement left a stale direct link');
    await fetch('/fixture/resource-choice',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({action:'replace-result-body'})});
    await index.refresh(fetchPage);await controller.whenIdle();
    check(!index.references.has(previousFact.referenceId),'same tool result replacement retained old evidence');
    check([...index.references.values()].some(item=>item.identifier==='SeedRole'),'replacement removed an independent source');
    check(replacedRoot.textContent===replacedText && !replacedRoot.querySelector('a.resource-link'),
      'source replacement changed copy text or kept a stale target');
    check(Object.keys((await fixtureState()).queryCalls).length===0,'source replacement triggered a cloud read');
    queryRoot('RetryHandler').querySelector('button.resource-link-pending').click();
    const pendingDialog=document.querySelector('.resource-link-popover');
    check(pendingDialog.scrollWidth<=pendingDialog.clientWidth,'query actions overflow the popover');
    check(!lookup().hidden,'missing parent has no query action');lookup().click();
    await waitFor(()=>status()?.textContent.includes('Resource lookup failed'),'query failure was not shown');
    check(!lookup().disabled && !document.querySelector('.resource-link-popover [name="ServiceName"]').disabled,'failed query blocked retry/manual input');
    // The backend deliberately keeps a three-second negative cache. Retry is
    // still an explicit click, not an automatic timer-driven cloud request.
    await new Promise(resolve=>setTimeout(resolve,3100));lookup().click();
    await waitFor(()=>status()?.textContent.includes('Resource attributes loaded'),'query retry did not load attributes');
    check((await fixtureState()).queryCalls.RetryHandler===2,'retry count does not match explicit actions');dismiss();
    await controller.whenIdle();
    queryRoot('PermissionHandler').querySelector('button.resource-link-pending').click();lookup().click();
    await waitFor(()=>status()?.textContent.includes('needs permission'),'permission result was not displayed');
    check(!lookup().disabled,'permission failure left query permanently disabled');dismiss();
    for (const [name,choice] of [['ApprovalHandler','allow_once'],['RejectedHandler','reject_once']]) {
      queryRoot(name).querySelector('button.resource-link-pending').click();lookup().click();
      let pending;
      for(let attempt=0;attempt<100 && !pending;attempt++) {
        pending=(await api.getSession(state.sessionId)).pendingPermissions?.[0];
        if(!pending) await new Promise(resolve=>setTimeout(resolve,10));
      }
      check(pending,'query did not publish an existing Web permission request');
      check(!(await fixtureState()).completedReads[name],'query read before approval');
      let answered=false;
      const panel=renderPermissionRequest(pending,{onPermissionAnswer:async(requestId,answer)=>{
        await api.answerPermission(requestId,answer);answered=true;
      }});
      document.body.append(panel);
      check(panel.querySelectorAll('[data-choice-id]').length===2,'resource lookup offered a broader grant');
      check(panel.textContent.includes('GetStackResource') && panel.textContent.includes(name),'approval omitted the query target');
      panel.querySelector(`[data-choice-id="${choice}"]`).click();
      await waitFor(()=>answered,'shared permission UI failed to submit');panel.remove();
      await waitFor(()=>status()?.textContent.includes(choice==='allow_once'?'Resource attributes loaded':'needs permission'),
        'query did not resume after explicit permission answer');
      check(((await fixtureState()).completedReads[name]||0)===(choice==='allow_once'?1:0),'permission decision did not gate the read');
      check((await api.getSession(state.sessionId)).pendingPermissions.length===0,'query left an orphan approval');
      dismiss();await controller.whenIdle();
    }
    queryRoot('LateHandler').querySelector('button.resource-link-pending').click();lookup().click();
    // Wait for the real HTTP handler, then close the originating popover before
    // its deliberately delayed fake cloud response returns.
    for(let attempt=0;attempt<30 && !(await fixtureState()).queryCalls.LateHandler;attempt++)
      await new Promise(resolve=>setTimeout(resolve,5));
    check((await fixtureState()).queryCalls.LateHandler===1,'late query never entered the backend');dismiss();
    await new Promise(resolve=>setTimeout(resolve,350));
    check(!document.querySelector('.resource-link-popover'),'late response reopened a closed popover');
    check(![...index.references.values()].find(item=>item.identifier==='LateHandler').attributes.ServiceName,'late response updated a dismissed query UI');
    // No SSE client is attached to this fixture. Synchronize the independent
    // server-side fact update before starting the next unrelated query.
    await index.refresh(fetchPage);await controller.whenIdle();
    const original=queryRoot('LookupHandler').textContent;
    queryRoot('LookupHandler').querySelector('button.resource-link-pending').click();lookup().click();
    await waitFor(()=>status()?.textContent.includes('Resource attributes loaded'),'real HTTP attribute lookup did not resolve');
    const field=document.querySelector('.resource-link-popover [name="ServiceName"]');
    check(field.value==='QueriedService' && field.disabled,'queried parent was not applied as an observed fact');
    check(lookup().hidden,'resolved resource still offers unnecessary lookup');
    const target=document.querySelector('.resource-link-popover a');
    check(!target.hidden && target.href==='https://ram.console.aliyun.com/roles/LookupHandler','queried link did not use reviewed fixture rules');
    check(document.activeElement===target,'query completion did not focus the real link');
    await controller.whenIdle();check(queryRoot('LookupHandler').textContent===original,'query changed copied source text');
    check((await fixtureState()).queryCalls.LookupHandler===1,'one query action performed multiple reads');
    window.resourceQueryCleanup=()=>controller.dispose();
    return {httpDeclaration:true,httpRestore:true,httpSourceIsolation:true,httpParentReselection:true,httpRevocation:true,httpResultReplacement:true,
      httpQuery:true,httpQueryRetry:true,httpQueryPermission:true,httpQueryApproval:true,httpQueryRejection:true,
      httpQueryDismissal:true,httpQueryCopy:true};
  });
  assert.equal(openedPages,4,'attribute lookup must not open a page automatically');
  await page.setViewportSize({width:360,height:480});
  // Viewport mutation and the browser's resize event are asynchronous. Wait
  // for the actual layout invariant, not a fixed delay or the first sample.
  await page.waitForFunction(()=>{
    const dialog=document.querySelector('.resource-link-popover');const rect=dialog.getBoundingClientRect();
    return rect.right<=360 && rect.bottom<=480 && dialog.scrollWidth<=dialog.clientWidth;
  },null,{timeout:3000});
  const layout=await page.evaluate(()=>{
    const dialog=document.querySelector('.resource-link-popover');const rect=dialog.getBoundingClientRect();
    return {right:rect.right,bottom:rect.bottom,scrollWidth:dialog.scrollWidth,width:dialog.clientWidth};
  });
  assert.ok(layout.right<=360 && layout.bottom<=480 && layout.scrollWidth<=layout.width,
    `query popover overflows a small viewport: ${JSON.stringify(layout)}`);
  await page.setViewportSize({width:1280,height:720});
  if (process.env.IAC_RESOURCE_TEST_SCREENSHOT) await page.screenshot({path:process.env.IAC_RESOURCE_TEST_SCREENSHOT});
  const queriedPopupPromise=page.waitForEvent('popup');
  await page.locator('.resource-link-popover a:not([hidden])').click();
  const queriedPopup=await queriedPopupPromise;
  await queriedPopup.waitForLoadState();
  assert.equal(queriedPopup.url(),'https://ram.console.aliyun.com/roles/LookupHandler');
  assert.equal(await queriedPopup.title(),'Offline console fixture');
  assert.equal(page.url(),origin+'/');assert.equal(openedPages,5,'query link click should open exactly one page');
  assert.equal(await page.evaluate(async()=> (await (await fetch('/fixture/resource-choice')).json()).queryCalls.LookupHandler),1,
    'opening the resolved link must not run another query');
  await queriedPopup.close();
  await page.evaluate(()=>window.resourceQueryCleanup());
  const accountChoice=await page.evaluate(async()=>{
    const {index,rules,links}=window.resourceTest;
    const api=await import('/static/js/api.js?v=resource-references-v11');
    const {persistResourceChoice,loadResourceAccountContext}=await import('/static/js/resource_index.js?v=resource-references-v11');
    const {openResourceLinkPopover}=await import('/static/js/components/resource_link_popover.js?v=resource-links-v50');
    const {renderMarkdownInto}=await import('/static/js/app.js?v=resource-links-v88');
    const check=(value,message)=>{if(!value) throw Error(message);};
    const waitFor=async(predicate,message)=>{
      const until=performance.now()+5000;
      while(!predicate() && performance.now()<until) await new Promise(resolve=>setTimeout(resolve,10));
      check(predicate(),message);
    };
    const state=await (await fetch('/fixture/resource-choice')).json();
    check(state.contextLoads===0,'rendering/history unexpectedly read credential context');
    const fetchPage=(cursor)=>api.getResourceReferences(state.emptySessionId,cursor);
    const configure=()=>links.configureResourceLinks({index,rules,
      onActivate:(anchor,occurrence,resolution,options)=>openResourceLinkPopover(anchor,occurrence,resolution,{index,rules,...options}),
      onLoadAccountContext:(options)=>loadResourceAccountContext(index,{...options,load:api.getResourceAccountContext}),
      onPersistChoice:(payload,options)=>persistResourceChoice(index,payload,{...options,save:api.resolveResourceReference,fetchPage}),
    });
    const mount=()=>{
      const fixture=document.getElementById('fixture');fixture.replaceChildren();
      for(const message of state.emptyMessages){
        const node=document.createElement('div');node.id=message.messageId;fixture.append(node);
        renderMarkdownInto(node,message.text,{source:{messageId:message.messageId,sourcePath:'/text'},complete:true});
      }
    };
    index.reset(state.emptySessionId);await index.loadSnapshot(await fetchPage(),fetchPage);
    let controller=configure();mount();await controller.whenIdle();
    check(index.references.size===0,'empty-index test contains seed resource facts');
    const first=()=>document.getElementById('empty-role');
    const other=()=>document.getElementById('empty-other');
    const original=first().textContent;
    first().querySelector('button.resource-link-pending').click();
    const load=document.querySelector('.resource-link-popover [data-resource-account]');
    check(load && !load.hidden,'empty index has no explicit account selection action');load.click();
    await waitFor(()=>document.querySelector('.resource-link-popover [name="accountScope"]')?.value==='credential:fake-empty-context',
      'configured context was not offered to the empty-index selection');
    const account=document.querySelector('.resource-link-popover [name="accountScope"]');
    check(account.selectedOptions[0].textContent.includes('account not verified'),'credential context was presented as a verified account');
    check(!document.querySelector('.resource-link-popover [name="regionId"]'),'RAM account selection requires a region');
    check(index.references.size===0,'offering account context created resource facts');
    const dialog=document.querySelector('.resource-link-popover');
    check(dialog.scrollWidth<=dialog.clientWidth,'account controls overflow the popover');
    [...dialog.querySelectorAll('button')].find(button=>button.textContent==='Select').click();
    await waitFor(()=>!document.querySelector('.resource-link-popover'),'empty-index selection failed to persist');
    await controller.whenIdle();
    check(first().querySelector('a.resource-link') && first().textContent===original,'account selection lost link/copy semantics');
    check(other().querySelector('button.resource-link-pending'),'account selection leaked to an independent same-name message');
    const stored=await fetchPage();
    check(stored.items.length===1 && stored.items[0].evidence==='user_context','configured context became cloud evidence');
    check(stored.bindings.length===1,'source selection was not persisted');
    check(!JSON.stringify(stored).includes('account-context:'),'ephemeral context handle was persisted');
    controller.dispose();index.reset(state.emptySessionId);await index.loadSnapshot(stored,fetchPage);
    controller=configure();mount();await controller.whenIdle();
    check(first().querySelector('a.resource-link') && other().querySelector('button.resource-link-pending'),'account binding did not restore');
    const after=await (await fetch('/fixture/resource-choice')).json();
    check(after.contextLoads===2,'restore queried credentials or save skipped context revalidation');
    check(JSON.stringify(after.queryCalls)===JSON.stringify(state.queryCalls),'account selection unexpectedly queried cloud resources');
    controller.dispose();
    return {emptyIndexAccount:true,accountChoiceRestore:true,accountChoiceIsolation:true};
  });
  const lifecycleChoice=await page.evaluate(async()=>{
    const {index,rules,links}=window.resourceTest;
    const api=await import('/static/js/api.js?v=resource-references-v11');
    const {persistResourceChoice}=await import('/static/js/resource_index.js?v=resource-references-v11');
    const {openResourceLinkPopover}=await import('/static/js/components/resource_link_popover.js?v=resource-links-v50');
    const {renderMarkdownInto}=await import('/static/js/app.js?v=resource-links-v88');
    const check=(value,message)=>{if(!value) throw Error(message);};
    const state=await (await fetch('/fixture/resource-choice')).json();
    const action=async(action)=>fetch('/fixture/resource-choice',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({action})});
    const fetchPage=(cursor)=>api.getResourceReferences(state.sessionId,cursor);
    const configure=()=>links.configureResourceLinks({index,rules,
      onActivate:(anchor,occurrence,resolution,options)=>openResourceLinkPopover(anchor,occurrence,resolution,{index,rules,...options}),
      onPersistChoice:(payload,options)=>persistResourceChoice(index,payload,{...options,save:api.resolveResourceReference,fetchPage}),
    });
    const mount=()=>{
      const fixture=document.getElementById('fixture');fixture.replaceChildren();
      for(const message of state.messages.filter(item=>item.messageId.startsWith('http-lifecycle-'))){
        const node=document.createElement('div');node.id=message.messageId;fixture.append(node);
        renderMarkdownInto(node,message.text,{source:{messageId:message.messageId,sourcePath:'/text'},complete:true});
      }
    };
    const root=(id)=>document.getElementById('http-lifecycle-'+id);
    const select=async()=>{
      const button=[...document.querySelectorAll('.resource-link-popover button')].find(button=>button.textContent==='Select');
      check(button && !button.disabled,'selected replacement cannot be saved');button.click();
      const until=performance.now()+5000;
      while(document.querySelector('.resource-link-popover') && performance.now()<until)
        await new Promise(resolve=>setTimeout(resolve,10));
      check(!document.querySelector('.resource-link-popover'),'lifecycle selection did not persist');
      await controller.whenIdle();
    };
    index.reset(state.sessionId);await index.loadSnapshot(await fetchPage(),fetchPage);
    let controller=configure();mount();await controller.whenIdle();
    const original=root('old').textContent;
    const before=[...index.references.values()].find(item=>item.identifier==='RecreatedRole');
    for(const id of ['old','peer']){
      root(id).querySelector('a').dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',altKey:true,bubbles:true,cancelable:true}));
      await select();
    }
    await action('recreate-name');await index.refresh(fetchPage);await controller.whenIdle();
    const roles=[...index.references.values()].filter(item=>item.identifier==='RecreatedRole');
    check(roles.length===2 && roles.every(item=>!item.aliases.length && !item.regionId),'name-only lifecycle identities were merged or given a region');
    const current=roles.find(item=>item.status==='CREATE_COMPLETE');
    check(current && current.referenceId!==before.referenceId,'recreation did not receive a new identity');
    for(const id of ['old','peer']) check(!root(id).querySelector('a') && root(id).querySelector('button'),
      'old name binding silently followed the new object');
    root('old').querySelector('button').click();
    check(document.querySelector('.resource-link-popover').textContent.includes('deleted resource'),'old lifecycle has no explanation');
    const replacement=document.querySelector(`[data-resource-replacement="${current.referenceId}"]`);
    check(replacement,'confirmed replacement is not offered for explicit selection');replacement.click();
    check(!document.querySelector('.resource-link-popover [name="regionId"]'),'replacement requires a fictitious RAM region');
    await select();
    check(root('old').querySelector('a') && !root('peer').querySelector('a'),'explicit replacement changed an independent historical binding');
    const saved=await fetchPage();
    const bindings=saved.bindings.filter(item=>item.identifier==='RecreatedRole');
    check(bindings.find(item=>item.source.messageId==='http-lifecycle-old')?.referenceId===current.referenceId,'new choice was not persisted');
    check(bindings.find(item=>item.source.messageId==='http-lifecycle-peer')?.referenceId===before.referenceId,'old peer binding was overwritten');
    // Discard both server controller and frontend index. Restore from the real
    // sidecar/current transcript, without carrying any optimistic local choice.
    controller.dispose();await action('restore-resource-controller');index.reset(state.sessionId);
    const restored=await fetchPage();await index.loadSnapshot(restored,fetchPage);controller=configure();mount();await controller.whenIdle();
    check(JSON.stringify(restored.items)===JSON.stringify(saved.items),'controller restore changed resource identities');
    check(root('old').querySelector('a') && root('peer').querySelector('button'),'lifecycle bindings did not restore independently');
    check(root('old').textContent===original && root('peer').textContent===original,'lifecycle selection changed copied text');
    const after=await (await fetch('/fixture/resource-choice')).json();
    check(JSON.stringify(after.queryCalls)===JSON.stringify(state.queryCalls),'lifecycle selection triggered a cloud read');
    window.resourceLifecycleCleanup=()=>controller.dispose();
    return {httpNameLifecycle:true,httpReplacementChoice:true,httpLifecycleRestore:true};
  });
  assert.equal(openedPages,5,'choosing/restoring a replacement must not open a page automatically');
  await page.locator('#http-lifecycle-peer button.resource-link-pending').click();
  if (process.env.IAC_RESOURCE_TEST_SCREENSHOT) {
    const output=process.env.IAC_RESOURCE_TEST_SCREENSHOT;
    await page.locator('.resource-link-popover').screenshot({path:output.replace(/\.png$/,'-lifecycle.png')});
  }
  await page.keyboard.press('Escape');
  const [replacementPopup]=await Promise.all([
    context.waitForEvent('page'),page.locator('#http-lifecycle-old a.resource-link').click({modifiers:['Shift']}),
  ]);
  await replacementPopup.waitForURL('https://ram.console.aliyun.com/roles/RecreatedRole');
  await replacementPopup.waitForLoadState();
  assert.equal(replacementPopup.url(),'https://ram.console.aliyun.com/roles/RecreatedRole');
  assert.equal(await replacementPopup.title(),'Offline console fixture');
  assert.equal(page.url(),origin+'/');assert.equal(openedPages,6,'Shift activation of a replacement must open exactly one page');
  await replacementPopup.close();await page.evaluate(()=>window.resourceLifecycleCleanup());
  const incremental = await verifyIncrementalResourceLinks(page);
  const cancellation = await verifyResourceSchedulerCancellation(page);
  const interactionPause = await verifyResourceSchedulerInteractionPause(page);
  const lastRequest = await verifyLastRequestRemoval(page);
  const profiler = process.env.IAC_RESOURCE_TEST_PROFILE ? await context.newCDPSession(page) : null;
  if (profiler) {await profiler.send('Profiler.enable'); await profiler.send('Profiler.start');}
  const performanceResult = process.env.IAC_RESOURCE_TEST_PERFORMANCE ? await page.evaluate(async () => {
    const {index, rules, role, instance, links} = window.resourceTest;
    const fixture = document.getElementById('fixture');
    fixture.replaceChildren();
    const facts = Array.from({length:500}, (_,i) => i % 2
      ? {...role,referenceId:`ref-perf-${i}`,identifier:`RoleDesign${String(i).padStart(4,'0')}`,sources:[]}
      : {...instance,referenceId:`ref-perf-${i}`,identifier:`i-design${String(i).padStart(10,'0')}`,sources:[]});
    index.reset('performance-session');
    if (!await index.loadSnapshot({schemaVersion:1,revision:1,items:facts,bindings:[]},
      () => {throw Error('unexpected network');})) throw Error('performance snapshot did not load');
    const originalFrame = window.requestAnimationFrame.bind(window);
    const originalSelection = document.getSelection.bind(document);
    const longTasks = [];
    const longTaskObserver = globalThis.PerformanceObserver?.supportedEntryTypes.includes('longtask')
      ? new PerformanceObserver(list=>longTasks.push(...list.getEntries())) : null;
    longTaskObserver?.observe({type:'longtask'});
    const selectionReads = [];
    document.getSelection = () => {
      const start = performance.now(); const result = originalSelection();
      if (result) {void result.isCollapsed; void result.anchorNode; void result.focusNode;}
      selectionReads.push(performance.now() - start); return result;
    };
    const slices = [];
    window.requestAnimationFrame = (callback) => originalFrame((time) => {
      const start = performance.now(); callback(time); slices.push(performance.now() - start);
    });
    const controller = links.configureResourceLinks({index,rules});
    const waitIdle = () => new Promise((resolve,reject) => {
      const timer = setTimeout(() => reject(Error('decoration did not settle')),45000);
      controller.whenIdle().then(() => {clearTimeout(timer); resolve();},reject);
    });
    const stats = [];
    try {
      for (const [name, containers, perContainer] of [['messages',200,25],['large-yaml',1,10000]]) {
        fixture.replaceChildren(); slices.length = 0; selectionReads.length = 0;
        const start = performance.now(); let chars = 0;
        const originalText = new Map();
        for (let n = 0; n < containers; n++) {
          const node = document.createElement(name === 'messages' ? 'p' : 'pre');
          node.textContent = Array.from({length:perContainer}, (_,j) => {
            const fact = facts[(n * perContainer + j) % facts.length];
            if (name === 'large-yaml') return `- ${fact.identifierField}: ${fact.identifier}\n  Note: ${'x'.repeat(75)}\n`;
            return fact.identifierKind === 'name' ? `RAM role ${fact.identifier};` : fact.identifier;
          }).join('\n');
          chars += node.textContent.length;
          originalText.set(node,node.textContent);
          fixture.append(node);
          links.registerResourceText(node,{source:{sourceId:`perf-${name}-${n}`},complete:true});
        }
        const expected = containers * perContainer;
        await waitIdle();
        const count = fixture.querySelectorAll('a.resource-link[data-resource-focus]').length;
        if (count !== expected) throw Error(`${name}: ${count}/${expected} occurrences decorated`);
        for (const [node,text] of originalText) if (node.textContent !== text) throw Error(`${name}: copy text changed`);
        // Include the final scheduler slice and report, rather than using
        // hardware-dependent milliseconds as brittle CI assertions.
        await new Promise(originalFrame);
        const sorted = [...slices].sort((a,b) => a-b);
        stats.push({name,chars,occurrences:count,domNodes:fixture.querySelectorAll('*').length,
          began:start,ended:performance.now(),totalMs:Math.round(performance.now()-start),
          slices:slices.length,maxSliceMs:Number(Math.max(...slices).toFixed(2)),
          p95SliceMs:Number((sorted[Math.floor(sorted.length*0.95)] || 0).toFixed(2)),
          maxSelectionReadMs:Number(Math.max(...selectionReads).toFixed(2)),
          slowSlices:slices.map((ms,i) => ({ms:Number(ms.toFixed(2)),i})).filter((row) => row.ms > 16),
          over16ms:slices.filter((value) => value > 16).length});
        if (name === 'large-yaml') {
          for (const stage of ['fact-update','same-version']) {
            slices.length = 0;
            const previousNodes=[...fixture.querySelectorAll('[data-resource-link]')];
            const mutations={childList:0,attributes:0};
            const countMutations=records=>{for(const record of records) mutations[record.type]++;};
            const observer=new MutationObserver(countMutations);
            observer.observe(fixture,{subtree:true,childList:true,attributes:true});
            const began = performance.now();
            if (stage === 'fact-update') {
              index.applyDelta({schemaVersion:1,baseRevision:1,revision:2,upserts:[{...facts[0],status:'UPDATE_COMPLETE'}],
                removedReferenceIds:[],bindings:[]});
            } else {
              for (const node of originalText.keys()) controller.invalidate(node);
            }
            await waitIdle();
            countMutations(observer.takeRecords());observer.disconnect();
            if (fixture.querySelectorAll('a.resource-link[data-resource-focus]').length !== expected) throw Error(`${stage}: lost occurrences`);
            for (const [node,text] of originalText) if (node.textContent !== text) throw Error(`${stage}: copy text changed`);
            const retainedWrappers=previousNodes.filter(node=>fixture.contains(node)).length;
            if (retainedWrappers!==previousNodes.length || mutations.childList || mutations.attributes)
              throw Error(`${stage}: unchanged targets caused DOM churn`);
            stats.push({name:stage,occurrences:expected,domNodes:fixture.querySelectorAll('*').length,
              wrappers:previousNodes.length,retainedWrappers,mutations,began,ended:performance.now(),
              totalMs:Math.round(performance.now()-began),slices:slices.length,
              maxSliceMs:Number(Math.max(...slices).toFixed(2)),over16ms:slices.filter((value) => value > 16).length});
          }
        }
      }
      // Give observer delivery a task boundary. Long-task data includes the
      // fixture/assertion work too; it is not claimed as pure decorator CPU time.
      await new Promise(resolve=>setTimeout(resolve,0));
      if (longTaskObserver) longTasks.push(...longTaskObserver.takeRecords());
      return stats.map(({began,ended,...row})=>{
        const tasks=longTasks.filter(task=>task.startTime<ended && task.startTime+task.duration>began);
        return {...row,longTasks:longTaskObserver ? tasks.length : null,
          maxLongTaskMs:longTaskObserver ? Math.max(0,...tasks.map(task=>task.duration)) : null};
      });
    } finally {
      longTaskObserver?.disconnect();controller.dispose();
      window.requestAnimationFrame = originalFrame; document.getSelection = originalSelection;
    }
  }) : null;
  let profile = null;
  if (profiler) {
    const {profile:result} = await profiler.send('Profiler.stop');
    profile = result.nodes.filter((node) => node.hitCount).sort((a,b) => b.hitCount-a.hitCount).slice(0,15)
      .map((node) => ({function:node.callFrame.functionName,hits:node.hitCount,file:node.callFrame.url.split('/').at(-1)}));
  }
  const application = await verifyResourceApplication({browser,origin:apiOrigin,restart:restartApiFixture});
  assert.equal(errors.length, 0, errors.join("\n"));
  console.log(JSON.stringify({...result, ...interaction, ...persistence, ...httpChoices, ...accountChoice, ...lifecycleChoice, ...incremental, ...cancellation, ...interactionPause, ...lastRequest, openedPages, offlineRequests:opened.length,
    localProxyConnections:proxyTargets.length,errors:errors.length, application, performance:performanceResult, profile}));
  }
} finally {
  await browser?.close();
  for (const socket of proxySockets) socket.destroy();
  for (const listener of [proxy,consoleServer]) if (listener) await new Promise((resolve) => listener.close(resolve));
  await new Promise((resolve) => server.close(resolve));
  if (certDirectory) fs.rmSync(certDirectory,{recursive:true,force:true});
  await stopApiFixture();
  if (apiDirectory) fs.rmSync(apiDirectory,{recursive:true,force:true});
}
