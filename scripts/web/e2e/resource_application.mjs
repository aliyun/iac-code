// Complete application bootstrap, real storage recovery, and live SSE checks.
// Called with the isolated browser/proxy and local API fixture from resource_links.
import assert from 'node:assert/strict';

export async function verifyResourceApplication({browser, origin, restart}) {
  const context = await browser.newContext({viewport:{width:1440,height:1000}});
  const errors = [], responses = [], streams = [], failedRequests = [], navigationTrace = [];
  const page = await context.newPage();
  let openedPages=0;
  let rejectedPayloadUrls=0;
  let signinRequests=0;
  let signinCapabilityRequests=0;
  let signinCapabilityMode='embedded';
  const signinRelays=new Map();
  context.on('page',()=>openedPages++);
  page.on('pageerror', error => errors.push(String(error)));
  page.on('requestfailed', request => failedRequests.push({url:request.url(),error:request.failure()?.errorText || ''}));
  page.on('response', response => {
    const pathname = new URL(response.url()).pathname;
    if (pathname.endsWith('/events')) streams.push(response.status());
    if (pathname.endsWith('/messages')) responses.push(response.status());
  });
  // Prepend deterministic offline destinations for this browser only. The
  // application's controller/index/popover/bootstrap stay intact; production
  // builders have separate exact-route tests, and no navigation escapes the proxy.
  await context.route('**/*', async route => {
    const url = new URL(route.request().url());
    if (url.pathname.includes('signin-relay') || url.hostname.includes('signin.') || url.hostname.includes('console.')) {
      navigationTrace.push(url.href);
    }
    if (url.origin === 'https://signin.aliyun.com') {
      const destination=url.searchParams.get('Destination');
      assert.ok(destination,'signin fixture did not receive a destination');
      return route.fulfill({status:302,headers:{location:destination}});
    }
    if (['https://ram.console.aliyun.com','https://ram4service.console.aliyun.com',
      'https://ros.console.aliyun.com','https://ros4service.console.aliyun.com'].includes(url.origin)) {
      return route.fulfill({status:200,contentType:'text/html',body:'<title>Offline application console fixture</title>'});
    }
    if (url.origin !== origin) return route.abort();
    const relay=/\/api\/sessions\/[^/]+\/resource-references\/signin-relay\/([a-f0-9]{64})$/.exec(url.pathname);
    if (relay) {
      const destination=signinRelays.get(relay[1]);
      assert.ok(destination,'unknown or already consumed sign-in relay');
      signinRelays.delete(relay[1]);
      // The backend relay normally redirects through Alibaba Cloud federation.
      // The surrounding offline browser fixture already verifies the exact
      // one-use relay contract, so jump directly to its reviewed destination.
      return route.fulfill({status:302,headers:{location:destination}});
    }
    if (url.pathname.endsWith('/resource-references/signin-capability')) {
      signinCapabilityRequests++;
      return route.fulfill({status:200,contentType:'application/json',body:JSON.stringify({openMode:signinCapabilityMode})});
    }
    if (url.pathname.endsWith('/resource-references/signin')) {
      const payload=JSON.parse(route.request().postData() || '{}');
      const destination=payload.destinations?.[payload.preferredPartition];
      assert.ok(destination && payload.accountScope,'signin request omitted its verified resource context');
      signinRequests++;
      const token=signinRequests.toString(16).padStart(64,'0');
      signinRelays.set(token,destination);
      return route.fulfill({status:200,contentType:'application/json',body:JSON.stringify({
        status:'ready',consolePartition:payload.preferredPartition,destination,
        url:`${origin}/api/sessions/offline/resource-references/signin-relay/${token}`,
      })});
    }
    if (url.pathname.endsWith('/outputs')) {
      const response=await route.fetch();
      const payload=await response.json();
      for (const stack of payload.stacks || []) {
        stack.consoleUrl='https://untrusted-output.invalid/redirect';
        rejectedPayloadUrls++;
      }
      return route.fulfill({response,json:payload});
    }
    if (url.pathname === '/static/js/resource_link_rules.js') {
      const response = await route.fetch();
      const source = await response.text();
      const marker = 'export const RESOURCE_LINK_RULES = Object.freeze([';
      assert.equal(source.split(marker).length, 2);
      const fields={'RAM::Role':'RoleName','ECS::Instance':'InstanceId','FC::Function':'FunctionName'};
      const requiredFields={'RAM::Role':[], 'ECS::Instance':[], 'FC::Function':['ServiceName']};
      const rules = Object.entries(fields).map(([type,field]) => `Object.freeze({
        ruleId:'full-app-fixture-${type}',version:1,resourceType:'ALIYUN::${type}',sourceType:'ALIYUN::${type}',
        partition:'aliyun',releaseStatus:'verified',host:'ram.console.aliyun.com',identifierField:'${field}',
        requiredFields:Object.freeze(${JSON.stringify(requiredFields[type])}),
        provenance:{source:'offline-full-application-fixture'},
        build:reference=>'https://ram.console.aliyun.com/fixture/'+encodeURIComponent(reference.identifier),
      }),`).join('\n');
      return route.fulfill({response,body:source.replace(marker,marker+'\n'+rules)});
    }
    return route.continue();
  });
  const getState = async () => {
    const response = await context.request.get(origin+'/fixture/resource-application');
    assert.equal(response.status(),200);
    return response.json();
  };
  const mutate = async (mode,action) => {
    const response = await context.request.post(origin+'/fixture/resource-application',{data:{mode,action}});
    assert.equal(response.status(),200,await response.text());
  };
  const closeOutputDrawers = async () => {
    for (const selector of ['.output-preview-close','[data-app-shell="output-panel-close"]']) {
      const button=page.locator(selector);
      if (await button.isVisible()) await button.click();
    }
  };
  const waitPreviewDestination = async (url) => {
    const deadline=Date.now()+10000;
    while(Date.now()<deadline) {
      const frame=page.frames().find(candidate=>candidate.parentFrame()===page.mainFrame() && candidate.url()===url);
      if(frame) return frame;
      await new Promise(resolve=>setTimeout(resolve,25));
    }
    const iframeSrc=await page.locator('.resource-link-preview iframe').getAttribute('src').catch(()=>null);
    throw Error(`preview did not reach ${url}: ${JSON.stringify({frames:page.frames().map(frame=>frame.url()),iframeSrc,
      failedRequests,navigationTrace,signinRequests,pendingRelays:[...signinRelays.keys()]})}`);
  };
  const embeddedFixtureUrl = (url) => url.replace('://ram.console.aliyun.com/',
    '://ram4service.console.aliyun.com/').replace('://ros.console.aliyun.com/',
    '://ros4service.console.aliyun.com/');
  const activate = async (anchor,url) => {
    const before=page.url(), count=openedPages, openPageCount=context.pages().length, signinCount=signinRequests;
    if (await anchor.getAttribute('data-resource-link')!==null) {
      await anchor.click();
      const preview=page.locator('.resource-link-preview');await preview.waitFor();
      await waitPreviewDestination(embeddedFixtureUrl(url));
      assert.equal(await preview.locator('header a').getAttribute('href'),url);
      assert.equal(signinRequests,signinCount+1,'one preview must request exactly one single-use sign-in URL');
      assert.equal(page.url(),before,'resource preview navigated the application');
      assert.equal(context.pages().length,openPageCount,'resource preview left an external page open');
      await page.keyboard.press('Escape');await preview.waitFor({state:'detached'});
      return;
    }
    const ready=page.waitForEvent('popup');
    await anchor.click();
    const popup=await ready;
    await popup.waitForURL(url);
    assert.equal(page.url(),before,'resource activation navigated the application');
    assert.equal(openedPages,count+1,'one activation opened multiple windows');
    await popup.close();
  };
  const expandTranscript = async () => {
    for (let step=0;step<40;step++) {
      const summaries=await page.locator('.message-stack details:not([open]) > summary').all();
      let opened=false,changed=false;
      for (const summary of summaries) {
        if (await summary.isVisible()) {
          const key=await summary.evaluate(node=>node.parentElement.dataset.openKey || '');
          const target=key ? page.locator(`.message-stack details[data-open-key=${JSON.stringify(key)}] > summary`) : summary;
          const box=await summary.boundingBox();
          if (!box) {changed=true;break;}
          // Click the disclosure edge, not an ID/name link embedded in its label.
          await target.click({position:{x:Math.min(5,box.width/2),y:box.height/2}});
          opened=true;break;
        }
      }
      if (!opened && !changed) return;
    }
    throw Error('transcript disclosure controls did not settle');
  };
  const start = async sessionId => {
    await page.goto(origin,{waitUntil:'domcontentloaded'});
    await page.locator('#iac-code-web-root[data-ready="true"]').waitFor();
    await page.locator(`[data-session-id="${sessionId}"]`).first().click();
    await page.locator('.message-body').filter({hasText:'Application paragraph:'}).first().waitFor({state:'attached'});
    await closeOutputDrawers();
    await expandTranscript();
    await page.locator('.message-body').filter({hasText:'Application paragraph:'}).first().waitFor();
  };
  const waitLinks = async () => page.waitForFunction(() => {
    const body=[...document.querySelectorAll('.message-body')].find(node=>node.textContent.includes('Application paragraph:'));
    return body && [...body.querySelectorAll('a.resource-link')].some(node=>node.textContent==='ApplicationRole') &&
      [...body.querySelectorAll('a.resource-link')].some(node=>node.textContent==='i-application000001');
  });
  const sessionSnapshot=async sessionId=>{
    const response=await context.request.get(origin+`/api/sessions/${encodeURIComponent(sessionId)}`);
    assert.equal(response.status(),200);return response.json();
  };
  const resourceSnapshot=async sessionId=>{
    const response=await context.request.get(origin+`/api/sessions/${encodeURIComponent(sessionId)}/resource-references`);
    assert.equal(response.status(),200);return response.json();
  };
  const reloadSession=async sessionId=>{
    await page.reload({waitUntil:'domcontentloaded'});
    await page.locator('#iac-code-web-root[data-ready="true"]').waitFor();
    await page.locator(`[data-session-id="${sessionId}"]`).first().click();
    await waitLinks();await closeOutputDrawers();await expandTranscript();
  };
  const choiceBody=()=>page.locator('.message-user .message-body').filter({hasText:'Application choice:'});
  const peerBody=()=>page.locator('.message-user .message-body').filter({hasText:'Application peer:'});
  const queryBody=kind=>page.locator(`[data-tool-use-id="application-query-${kind}"] .tool-card-results`);
  const queryLink=(kind,name)=>queryBody(kind).locator('a.resource-link').filter({hasText:name}).first();
  const assertChoice=async(name,original)=>{
    await choiceBody().locator('a.resource-link').filter({hasText:name}).waitFor();
    await peerBody().locator('button.resource-link-pending').filter({hasText:name}).waitFor();
    assert.equal(await peerBody().locator('a.resource-link').count(),0,'saved user context leaked to a different message');
    assert.equal(await choiceBody().textContent(),original,'name selection changed source/copy text');
  };
  const beginQuery=async(sessionId,kind,name)=>{
    await expandTranscript();
    await queryBody(kind).locator('button.resource-link-pending').filter({hasText:name}).first().click();
    const lookup=page.locator('.resource-link-popover [data-resource-query]');
    await lookup.waitFor();
    await lookup.click();
    const panel=page.locator('.blocking-panel-permission');await panel.waitFor();
    await page.locator('.resource-link-popover').waitFor({state:'hidden'});
    const id=await panel.getAttribute('data-request-id');
    assert.ok((await panel.textContent()).includes('GetStackResource') && (await panel.textContent()).includes(name),
      'the actual approval panel must show the fixed API and target');
    assert.deepEqual(await panel.locator('[data-choice-id]').evaluateAll(nodes=>nodes.map(node=>node.dataset.choiceId)),
      ['allow_once','reject_once'],'lookup must not offer a broad grant');
    const session=await sessionSnapshot(sessionId);
    assert.equal(session.currentTurnActive,false,'resource lookup must not start an Agent/pipeline turn');
    assert.equal(session.pendingPermissions.length,1);assert.equal(session.pendingPermissions[0].requestId,id);
    return id;
  };
  const approveQuery=async(sessionId,requestId,kind,name)=>{
    const panel=page.locator(`.blocking-panel-permission[data-request-id="${requestId}"]`);
    await panel.waitFor();await panel.locator('[data-choice-id="allow_once"]').click();
    await panel.waitFor({state:'detached'});
    // After a reload no popover/promise remains. The normal SSE index update
    // must still make the resource usable without repeating the cloud lookup.
    await queryLink(kind,name).waitFor();
    const session=await sessionSnapshot(sessionId);
    assert.equal(session.pendingPermissions.length,0);assert.equal(session.currentTurnActive,false);
    if (await page.locator('.resource-link-popover').count())
      await page.locator('.resource-link-popover').getByRole('button',{name:'Close',exact:true}).click();
  };
  const waitStreamLink = async () => page.waitForFunction(() => {
    const body=[...document.querySelectorAll('.message-body')].find(node=>node.textContent.startsWith('Application streaming:'));
    return body && ['i-streaming000001','ApplicationRole'].every(value=>
      [...body.querySelectorAll('a.resource-link')].some(node=>node.textContent===value));
  });
  const auditAuxiliaryRequests = async (mode,sessionId) => {
    await mutate(mode,'auxiliary-start');
    const permission=page.locator('.blocking-panel-permission');
    const first=page.locator('.blocking-panel-question').filter({hasText:'Auxiliary first:'});
    const second=page.locator('.blocking-panel-question').filter({hasText:'Auxiliary second:'});
    const form=page.locator('.blocking-panel-elicitation');
    await first.waitFor();await second.waitFor();await form.waitFor();
    const names=['ApplicationRole','i-application000001'];
    for(const container of [permission.locator('h3'),permission.locator('.blocking-detail-command'),form.locator('h3')]) {
      for(const name of names) await container.locator('a.resource-link').filter({hasText:name}).waitFor();
    }
    for (const label of await form.locator('.blocking-elicitation-label').all()) {
      for(const name of names) await label.locator('a.resource-link').filter({hasText:name}).waitFor();
    }
    const original=await first.locator('h3').textContent();
    const before=await sessionSnapshot(sessionId);
    assert.equal(before.pendingPermissions.length,1);
    assert.equal(before.pendingQuestions.length,2);
    assert.equal(before.pendingElicitations.length,1);
    const role='AuxiliaryRole-'+mode;
    const selectName=async anchor=>{
      await anchor.click();
      const popover=page.locator('.resource-link-popover');await popover.waitFor();
      assert.equal(await popover.locator('[name="regionId"]').count(),0);
      await popover.locator('[name="accountScope"]').selectOption('fake-application-account');
      await popover.locator('[name="consolePartition"]').selectOption('aliyun');
      // This key must activate the resource choice, never submit permission.
      await popover.getByRole('button',{name:'Select',exact:true}).press('Enter');
      await popover.waitFor({state:'detached'});
    };
    await selectName(first.locator('button.resource-link-pending').filter({hasText:role}));
    await first.locator('a.resource-link').filter({hasText:role}).waitFor();
    assert.equal(await first.locator('h3').textContent(),original);
    await second.locator('button.resource-link-pending').filter({hasText:role}).waitFor();
    const descriptions=form.locator('.blocking-elicitation-desc');
    for(const description of await descriptions.all())
      await description.locator('a.resource-link').filter({hasText:names[1]}).waitFor();
    await selectName(descriptions.nth(0).locator('button.resource-link-pending').filter({hasText:role}));
    await descriptions.nth(0).locator('a.resource-link').filter({hasText:role}).waitFor();
    await descriptions.nth(1).locator('button.resource-link-pending').waitFor();
    const snapshot=await resourceSnapshot(sessionId);
    const saved=snapshot.bindings.filter(binding=>binding.identifier===role);
    assert.equal(saved.length,2,'each request/field must persist its own choice');
    assert.notEqual(saved[0].source.sourceId,saved[1].source.sourceId);
    assert.equal((await sessionSnapshot(sessionId)).pendingPermissions[0].requestId,before.pendingPermissions[0].requestId,
      'resource popover keyboard submission approved an unrelated permission');
    assert.equal(await form.locator('input [data-resource-link],textarea [data-resource-link],button.blocking-action [data-resource-link]').count(),0);
    const editable=form.locator('input.blocking-elicitation-input').first();
    await editable.fill('RAM role AuxiliaryRole-'+mode+'; '+names[1]);
    assert.equal(await editable.inputValue(),'RAM role AuxiliaryRole-'+mode+'; '+names[1],
      'link decoration changed the editable field value');
    const idAnchor=permission.locator('h3 a.resource-link').filter({hasText:names[1]});
    await idAnchor.focus();
    const openPageCount=context.pages().length,mainUrl=page.url();
    await idAnchor.press('Enter');
    const preview=page.locator('.resource-link-preview');await preview.waitFor();
    await waitPreviewDestination('https://ram4service.console.aliyun.com/fixture/'+names[1]);
    assert.equal(context.pages().length,openPageCount);assert.equal(page.url(),mainUrl);
    await page.keyboard.press('Escape');await preview.waitFor({state:'detached'});
    assert.equal((await sessionSnapshot(sessionId)).pendingPermissions.length,1,'resource link Enter approved permission');
    await reloadSession(sessionId);
    await first.locator('a.resource-link').filter({hasText:role}).waitFor();
    await second.locator('button.resource-link-pending').filter({hasText:role}).waitFor();
    await descriptions.nth(0).locator('a.resource-link').filter({hasText:role}).waitFor();
    await descriptions.nth(1).locator('button.resource-link-pending').waitFor();
    assert.deepEqual((await resourceSnapshot(sessionId)).bindings.filter(binding=>binding.identifier===role),saved,
      'refresh lost active-request field choices');
    await first.locator('a.resource-link').press('Alt+Enter');
    await page.locator('.resource-link-popover').waitFor();
    await mutate(mode,'auxiliary-change');
    await page.locator('.resource-link-popover').waitFor({state:'detached'});
    await first.locator('button.resource-link-pending').filter({hasText:role}).waitFor();
    await descriptions.nth(0).locator('a.resource-link').filter({hasText:role}).waitFor();
    const changed=await resourceSnapshot(sessionId);
    assert.equal(changed.bindings.filter(binding=>binding.identifier===role).length,1,
      'changing one request field failed to retract only its own choice');
    await mutate(mode,'auxiliary-reorder');
    for(const description of await descriptions.all())
      await description.locator('button.resource-link-pending').filter({hasText:role}).waitFor();
    assert.equal((await resourceSnapshot(sessionId)).bindings.filter(binding=>binding.identifier===role).length,0,
      'reordered parameters inherited a choice from the previous field at that position');
    await descriptions.nth(1).locator('button.resource-link-pending').click();
    await page.locator('.resource-link-popover').waitFor();
    await mutate(mode,'auxiliary-finish');
    await first.waitFor({state:'detached'});await form.waitFor({state:'detached'});
    await page.locator('.resource-link-popover').waitFor({state:'detached'});
    const removed=await resourceSnapshot(sessionId);
    assert.equal(removed.bindings.filter(binding=>binding.identifier===role).length,0,'finished requests retained their choices');
    assert.equal(removed.items.filter(item=>item.identifier===role).length,0,'finished requests retained unproven declarations');
    await waitLinks();
    return {auxiliaryRequests:true,auxiliaryChoiceRefresh:true,auxiliaryFieldIsolation:true,auxiliaryKeyboard:true,
      auxiliarySourceChange:true,auxiliaryFieldReorder:true,auxiliaryRevocation:true};
  };
  const auditErrors = async (mode,sessionId) => {
    await mutate(mode,'error-start');
    const body=page.locator('.message-error .message-body');
    const role='ErrorRole-'+mode;
    const pending=()=>body.locator('button.resource-link-pending').filter({hasText:role});
    const anchor=()=>body.locator('a.resource-link').filter({hasText:role});
    await pending().waitFor();
    for(const value of ['ApplicationRole','i-application000001'])
      await body.locator('a.resource-link').filter({hasText:value}).waitFor();
    const original=await body.textContent();
    assert.equal(original.includes('HiddenErrorRole'),false,'hidden error details became visible');
    await pending().click();
    const popover=page.locator('.resource-link-popover');await popover.waitFor();
    assert.equal(await popover.locator('[name="regionId"]').count(),0);
    await popover.locator('[name="accountScope"]').selectOption('fake-application-account');
    await popover.locator('[name="consolePartition"]').selectOption('aliyun');
    await popover.getByRole('button',{name:'Select',exact:true}).click();
    await popover.waitFor({state:'detached'});await anchor().waitFor();
    const saved=(await resourceSnapshot(sessionId)).bindings.filter(binding=>binding.identifier===role);
    assert.equal(saved.length,1);assert.match(saved[0].source.sourceId,/^session-error:/);
    const oldBody=await body.elementHandle();
    await mutate(mode,'error-redraw');
    await page.waitForFunction(node=>!node.isConnected,oldBody);
    await oldBody.dispose();await anchor().waitFor();
    assert.equal(await body.textContent(),original,'error rendering/choice changed copy text');
    assert.deepEqual((await resourceSnapshot(sessionId)).bindings.filter(binding=>binding.identifier===role),saved);
    // The existing product replays buffered pipeline events on reload, but
    // intentionally does not replay completed normal turns. Do not resurrect
    // transient error text from the resource sidecar to hide that boundary.
    if(mode!=='normal') {
      await reloadSession(sessionId);await anchor().waitFor();
      assert.equal(await body.textContent(),original);
      assert.deepEqual((await resourceSnapshot(sessionId)).bindings.filter(binding=>binding.identifier===role),saved);
    }
    await activate(anchor(),'https://ram.console.aliyun.com/fixture/'+role);
    await anchor().press('Alt+Enter');await popover.waitFor();
    await mutate(mode,'error-repeat');
    await popover.waitFor({state:'detached'});await pending().waitFor();
    assert.equal(await body.textContent(),original,'repeat must have identical text but new event identity');
    assert.equal((await resourceSnapshot(sessionId)).bindings.filter(binding=>binding.identifier===role).length,0,
      'new error event inherited an old event choice');
    await pending().click();await popover.waitFor();
    await mutate(mode,'error-clear');
    await body.waitFor({state:'detached'});await popover.waitFor({state:'detached'});
    const cleared=await resourceSnapshot(sessionId);
    assert.equal(cleared.items.some(item=>item.identifier===role),false,'cleared errors retained a declaration');
    await reloadSession(sessionId);
    assert.equal(await body.count(),0,'resource sidecar resurrected a cleared transient error');
    await waitLinks();
    return {errorText:true,errorChoiceRedraw:true,errorPipelineRefresh:mode!=='normal',errorEventIsolation:true,errorRevocation:true};
  };
  const waitProgress = async identified => {
    await expandTranscript();
    const result=await page.waitForFunction(identified => {
      const card=document.querySelector('[data-tool-use-id="application-deployment"]');
      const title=card?.querySelector('.tool-stack-progress-title');
      const id='11111111-2222-4333-8444-555555555556';
      if (!title?.checkVisibility() || title.querySelector('a')?.href!==
        'https://ros.console.aliyun.com/cn-beijing/stacks/'+id) return false;
      const rows=[...card.querySelectorAll('.tool-stack-progress-table tbody tr')];
      if (rows.length!==2) return false;
      if (rows.some(row=>row.children[0].querySelector('[data-resource-link]'))) throw Error('logical name linked');
      const targets=rows.flatMap(row=>[...row.querySelectorAll('a.resource-link')].map(link=>link.textContent));
      return identified ? targets.includes('DeploymentRole') && targets.includes('i-deployment000001') : targets.length===0;
    },identified);
    return result.jsonValue();
  };
  const auditDeployment = async (mode,{progress=false}={}) => {
    await page.locator('[data-tool-use-id="application-deployment"] .tool-card-results').waitFor({state:'attached'});
    await page.locator('[data-tool-use-id="application-deployment"]:not(.is-active)').waitFor({state:'attached'});
    await closeOutputDrawers();
    await expandTranscript();
    if (progress || mode!=='normal') await waitProgress(true);
    const result=await page.waitForFunction(() => {
      const result=document.querySelector('[data-tool-use-id="application-deployment"] .tool-card-results');
      const outputs=[...(result?.querySelectorAll('.conclusion-field') || [])].find(row=>
        row.querySelector(':scope > dt')?.textContent==='Outputs')?.querySelector(':scope > dd');
      if (!outputs?.checkVisibility()) return false;
      const links=[...outputs.querySelectorAll('a.resource-link')];
      if ([...result.querySelectorAll('[data-resource-link]')].some(node=>node.textContent==='—')) {
        throw Error('display placeholder became a resource');
      }
      if (!['DeploymentRole','i-deployment000001'].every(value=>links.some(node=>node.textContent===value))) return false;
      return {text:outputs.textContent,hrefs:links.map(node=>node.href)};
    });
    if (progress) {
      await activate(page.locator('[data-tool-use-id="application-deployment"] .tool-stack-progress-table a.resource-link')
        .filter({hasText:'DeploymentRole'}),'https://ram.console.aliyun.com/fixture/DeploymentRole');
    }
    const toggle=page.locator('[data-app-shell="output-toggle"]');
    await toggle.waitFor();
    if (!await page.locator('[data-app-shell="output-panel"]').isVisible()) await toggle.click();
    const stack=page.locator('.output-stack-row');
    await stack.waitFor();
    assert.equal(await stack.getAttribute('href'),
      'https://ros.console.aliyun.com/cn-beijing/stacks/11111111-2222-4333-8444-555555555556',
      'output row must use actual observed region, not the tool input region');
    if (progress) await activate(stack,'https://ros.console.aliyun.com/cn-beijing/stacks/11111111-2222-4333-8444-555555555556');
    await page.locator('.output-file-row').filter({hasText:'resource-links-'+mode+'.json'}).click();
    const preview=page.locator('.output-preview-code');
    await preview.waitFor();
    await page.waitForFunction(()=>{
      const pre=document.querySelector('.output-preview-code');
      if (!pre?.checkVisibility()) return false;
      const text=JSON.parse(pre.textContent);
      const links=[...pre.querySelectorAll('a.resource-link')];
      return text.Metadata.RoleName==='DeploymentRole' && text.Metadata.InstanceId==='i-deployment000001' &&
        links.filter(node=>node.textContent!=='PreviewOnlyRole').length===2 &&
        links.some(node=>node.textContent==='DeploymentRole') &&
        links.some(node=>node.textContent==='i-deployment000001');
    });
    const previewText=await preview.textContent();
    if (progress && mode==='normal' && process.env.IAC_RESOURCE_TEST_SCREENSHOT) {
      await page.screenshot({path:process.env.IAC_RESOURCE_TEST_SCREENSHOT.replace(/\.png$/,'-deployment.png')});
    }
    if (progress) await activate(preview.locator('a.resource-link').filter({hasText:'DeploymentRole'}),
      'https://ram.console.aliyun.com/fixture/DeploymentRole');
    await closeOutputDrawers();
    return {outputs:await result.jsonValue(),previewText,stackUrl:await stack.getAttribute('href')};
  };
  const previewChoice = async (mode,sessionId,{save=false,expectedId=null,changed=false}={}) => {
    await closeOutputDrawers();
    await page.locator('[data-app-shell="output-toggle"]').click();
    await page.locator('.output-file-row').filter({hasText:'resource-links-'+mode+'.json'}).click();
    const preview=page.locator('.output-preview-code');
    await preview.waitFor();
    const original=await preview.textContent();
    const pending=preview.locator('button.resource-link-pending').filter({hasText:'PreviewOnlyRole'});
    const link=preview.locator('a.resource-link').filter({hasText:'PreviewOnlyRole'});
    if (save || changed) {
      await pending.waitFor();
      if (changed) {
        assert.equal((await resourceSnapshot(sessionId)).bindings.some(binding=>
          binding.identifier==='PreviewOnlyRole'),false,'changed preview retained its old binding');
        await closeOutputDrawers();return null;
      }
      await pending.click();
      const popover=page.locator('.resource-link-popover');
      await popover.waitFor();
      assert.equal(await popover.locator('[name="regionId"]').count(),0);
      await popover.locator('[name="accountScope"]').selectOption('fake-application-account');
      await popover.locator('[name="consolePartition"]').selectOption('aliyun');
      await popover.getByRole('button',{name:'Select',exact:true}).click();
      await popover.waitFor({state:'detached'});
    }
    await link.waitFor();
    assert.equal(await preview.textContent(),original,'preview choice changed the template text');
    const bindings=(await resourceSnapshot(sessionId)).bindings.filter(binding=>binding.identifier==='PreviewOnlyRole');
    assert.equal(bindings.length,1,'preview choice leaked to a matching tool input');
    assert.match(bindings[0].source.sourceId,/^output-preview:[0-9a-f]{64}$/);
    if (expectedId) assert.equal(bindings[0].referenceId,expectedId,'preview recovery retargeted the choice');
    if (save) await activate(link,'https://ram.console.aliyun.com/fixture/PreviewOnlyRole');
    await closeOutputDrawers();
    return bindings[0].referenceId;
  };
  const previewRaces = async (mode,sessionId,otherSessionId) => {
    const within=async promise=>{
      let timer;
      try { return await Promise.race([promise,new Promise((_,reject)=>{
        timer=setTimeout(()=>reject(Error('preview race fixture timed out')),15000);
      })]); } finally { clearTimeout(timer); }
    };
    const preview=page.locator('[data-app-shell="output-file-preview"]');
    const code=page.locator('.output-preview-code');
    const open=async()=>{
      if (!await page.locator('[data-app-shell="output-panel"]').isVisible())
        await page.locator('[data-app-shell="output-toggle"]').click();
      await page.locator('.output-file-row').filter({hasText:'resource-links-'+mode+'.json'}).click();
    };
    const switchTo=async id=>{
      await page.locator(`[data-session-id="${id}"]`).first().click();
      await page.locator(`.session-item.is-active[data-session-id="${id}"]`).waitFor();
    };
    const matcher=url=>url.origin===origin && url.pathname===`/api/sessions/${encodeURIComponent(sessionId)}/outputs/file`;
    for (const scenario of ['close','reopen','switch-back']) {
      await closeOutputDrawers();
      let release,ready,finished;
      const held=new Promise(resolve=>release=resolve), received=new Promise(resolve=>ready=resolve);
      const delivered=new Promise(resolve=>finished=resolve);
      let first=true;
      const handler=async route=>{
        if (!first) return route.fallback();
        first=false;
        const response=await route.fetch(), payload=await response.json();
        ready();
        await held;
        // Mark only this delayed browser response; the actual file and API
        // facts remain untouched. Applying it is observably incorrect.
        try { await route.fulfill({response,json:{...payload,content:'STALE_PREVIEW_RESPONSE'}}); }
        finally { finished(); }
      };
      await context.route(matcher,handler);
      try {
        await open();await within(received);
        await page.locator('.output-preview-close').click();
        if (scenario==='switch-back') { await switchTo(otherSessionId);await switchTo(sessionId); }
        let latest=null;
        if (scenario!=='close') { await open();await code.waitFor();latest=await code.textContent(); }
        release();await within(delivered);
        await page.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
        if (scenario==='close') {
          assert.equal(await preview.isVisible(),false,'late response reopened a closed preview');
          assert.equal(await code.count(),0,'late response recreated removed preview text');
        } else assert.equal(await code.textContent(),latest,'late same-path response replaced the current preview');
        assert.equal(await page.getByText('STALE_PREVIEW_RESPONSE',{exact:true}).count(),0);
      } finally {
        release();await context.unroute(matcher,handler);
        await closeOutputDrawers();
      }
    }
    await open();await code.waitFor();
    await code.locator('button.resource-link-pending').filter({hasText:'PreviewOnlyRole'}).click();
    await page.locator('.resource-link-popover').waitFor();
    await page.locator('.output-preview-close').click();
    await page.locator('.resource-link-popover').waitFor({state:'detached'});
    await closeOutputDrawers();
  };
  const snapshotQuestion = async (mode,sessionId) => {
    await mutate(mode,'snapshot-question-start');await reloadSession(sessionId);
    assert.equal((await sessionSnapshot(sessionId)).pendingQuestions.length,0,
      'snapshot fallback must not be replaced with a registered test request');
    const question=page.locator('.blocking-panel-question[data-request-id="snapshot-question"]');
    const pending=question.locator('button.resource-link-pending').filter({hasText:'SnapshotOnlyRole'});
    const anchor=question.locator('a.resource-link').filter({hasText:'SnapshotOnlyRole'});
    await pending.waitFor();
    const original=await question.locator('h3').textContent();
    await question.locator('a.resource-link').filter({hasText:'i-application000001'}).waitFor();
    await pending.click();
    const popover=page.locator('.resource-link-popover');await popover.waitFor();
    assert.equal(await popover.locator('[name="regionId"]').count(),0);
    await popover.locator('[name="accountScope"]').selectOption('fake-application-account');
    await popover.locator('[name="consolePartition"]').selectOption('aliyun');
    await popover.getByRole('button',{name:'Select',exact:true}).click();
    await popover.waitFor({state:'detached'});await anchor.waitFor();
    assert.equal(await question.locator('h3').textContent(),original);
    const binding=(await resourceSnapshot(sessionId)).bindings.find(value=>value.identifier==='SnapshotOnlyRole');
    assert.equal(binding.source.sourceId,'blocking:question:snapshot-question');
    await reloadSession(sessionId);await anchor.waitFor();
    assert.equal((await resourceSnapshot(sessionId)).bindings.find(value=>
      value.identifier==='SnapshotOnlyRole')?.referenceId,binding.referenceId);
    await activate(anchor,'https://ram.console.aliyun.com/fixture/SnapshotOnlyRole');
    await mutate(mode,'snapshot-question-change');await reloadSession(sessionId);await pending.waitFor();
    assert.equal((await resourceSnapshot(sessionId)).bindings.some(value=>value.identifier==='SnapshotOnlyRole'),false);
    await mutate(mode,'snapshot-question-finish');await reloadSession(sessionId);
    assert.equal(await question.count(),0,'completed snapshot question resurrected after reload');
    assert.equal((await resourceSnapshot(sessionId)).items.some(value=>value.identifier==='SnapshotOnlyRole'),false);
  };
  const audit = async () => {
    const result = await page.waitForFunction(() => {
    try {
    const all = [...document.querySelectorAll('.message-body')];
    const body = all.find(node=>node.textContent.includes('Application paragraph:'));
    if (!body) throw Error('missing actual assistant body');
    const role='ApplicationRole', id='i-application000001';
    const linked = (node,value) => [...node.querySelectorAll('a.resource-link')].some(link=>
      link.getAttribute('aria-label')===value || link.textContent===value);
    const positive = [...body.querySelectorAll('p,li,tbody tr,pre')].filter(node=>
      !node.closest('li') || node.tagName==='LI').filter(node=>
      !node.textContent.includes('Negative controls') && !node.textContent.includes('DisplayName'));
    const checked = positive.filter(node=>node.textContent.includes(role) && node.textContent.includes(id));
    for (const node of checked) {
      if (!node.checkVisibility() || !linked(node,role) || !linked(node,id)) throw Error('unlinked application content: '+node.textContent);
    }
    if (checked.length < 8) throw Error('missing paragraph/list/inline/table/code locations: '+checked.length);
    const negative=[...body.querySelectorAll('p,pre')].filter(node=>
      node.textContent.includes('Negative controls') || node.textContent.includes('DisplayName'));
    for (const node of negative) if (node.querySelector('[data-resource-link]')) throw Error('negative control linked');
    const user=document.querySelector('.message-user .message-body');
    if (!user?.checkVisibility() || !linked(user,role) || !linked(user,id)) throw Error('user message not linked');
    const thinking=[...document.querySelectorAll('.message-thinking pre')].find(node=>node.textContent.includes('Application thinking:'));
    if (!thinking?.checkVisibility() || !linked(thinking,role) || !linked(thinking,id)) throw Error('thinking not linked');
    for (const selector of ['.tool-card-input','.tool-card-results']) {
      const container=document.querySelector('[data-tool-use-id="application-tool"] '+selector);
      if (!container?.checkVisibility() || !linked(container,id)) throw Error('tool content ID not linked: '+selector);
    }
    const shell=document.querySelector('[data-tool-use-id="application-shell"]');
    const shellFields=[...(shell?.querySelectorAll('.tool-shell-command,.tool-shell-output') || [])];
    if (shellFields.length!==3 || shellFields.some(node=>!node.checkVisibility() || !linked(node,role) || !linked(node,id))) {
      throw Error('shell command/stdout/stderr not linked');
    }
    const conclusion=document.querySelector('[data-tool-use-id="application-conclusion"] .tool-complete-step-detail');
    if (!conclusion?.checkVisibility() || !linked(conclusion,id)) throw Error('conclusion ID not linked');
    const json=[...body.querySelectorAll('pre code')].find(node=>node.textContent.startsWith('{"RoleName"'));
    if (!json || JSON.parse(json.textContent).RoleName!==role) throw Error('JSON copy changed');
    if (body.querySelector('a a,[data-resource-link] [data-resource-link]')) throw Error('nested links');
    return {locations:checked.length,user:true,thinking:true,toolInput:true,toolResult:true,shell:true,conclusion:true,body:body.textContent,
      hrefs:[...body.querySelectorAll('a.resource-link')].map(node=>node.href)};
    } catch (error) {globalThis.__IAC_LAST_RESOURCE_AUDIT_ERROR__=String(error);return false;}
    },undefined,{timeout:10000});
    return result.jsonValue();
  };
  try {
    const initial=await getState(), modes=[], expected=new Map(), deployments=new Map(), recovery=new Map();
    const previewChoices=new Map();
    const counts=async()=>{
      const response=await context.request.get(origin+'/fixture/resource-choice');
      const value=await response.json();
      return {contextLoads:value.contextLoads,queryCalls:value.queryCalls,completedReads:value.completedReads};
    };
    const initialCounts=await counts();
    const expectedCounts=structuredClone(initialCounts);
    for (const [mode,sessionId] of Object.entries(initial.sessions)) {
      console.log(JSON.stringify({applicationMode:mode,stage:'begin'}));
      await start(sessionId);
      const before=await page.locator('.message-body').filter({hasText:'Application paragraph:'}).first().textContent();
      assert.equal(await page.locator('.message-body a.resource-link').count(),0,'empty facts must not invent targets');
      await mutate(mode,'facts');
      await waitLinks();
      await expandTranscript();
      const live=await audit();
      assert.equal(live.body,before,'fact backfill changed source text');
      if (mode==='normal') {
        signinCapabilityMode='external';
        await reloadSession(sessionId);
        const anchor=page.locator('.message-user .message-body a.resource-link').filter({hasText:'ApplicationRole'}).first();
        const beforeSignin=signinRequests, beforePages=openedPages;
        const popupReady=page.waitForEvent('popup');
        await anchor.click();
        const popup=await popupReady;
        await popup.waitForURL('https://ram.console.aliyun.com/fixture/ApplicationRole');
        assert.equal(signinRequests,beforeSignin,'external default unexpectedly requested a sign-in token');
        assert.equal(openedPages,beforePages+1,'external default did not open exactly one new tab');
        assert.equal(await page.locator('.resource-link-preview').count(),0,'external default opened an iframe panel');
        await popup.close();
        signinCapabilityMode='embedded';
        await reloadSession(sessionId);
      }
      if (mode==='normal' && process.env.IAC_RESOURCE_TEST_SCREENSHOT) {
        await page.locator('.message-body').filter({hasText:'Application paragraph:'}).first().scrollIntoViewIfNeeded();
        await page.screenshot({path:process.env.IAC_RESOURCE_TEST_SCREENSHOT});
      }
      for (const [selector,identifier] of [
        ['.message-user .message-body','ApplicationRole'],
        ['[data-tool-use-id="application-shell"] .tool-shell-output','i-application000001'],
      ]) {
        await expandTranscript();
        const anchor=page.locator(selector+' a.resource-link').filter({hasText:identifier}).first();
        const beforeUrl=page.url(), previousOpenPages=context.pages().length;
        await anchor.click();
        const preview=page.locator('.resource-link-preview');await preview.waitFor();
        await waitPreviewDestination('https://ram4service.console.aliyun.com/fixture/'+identifier);
        assert.equal(page.url(),beforeUrl,'resource activation navigated the application');
        assert.equal(context.pages().length,previousOpenPages,'resource preview left an external page open');
        await page.keyboard.press('Escape');await preview.waitFor({state:'detached'});
        if (selector.startsWith('[data-tool-use-id')) {
          assert.equal(await page.locator('[data-tool-use-id="application-shell"]').getAttribute('open'),'');
        }
      }
      const auxiliary=await auditAuxiliaryRequests(mode,sessionId);
      const errorAudit=await auditErrors(mode,sessionId);
      await mutate(mode,'stream-start');
      await page.getByText('Application streaming: i-stream',{exact:false}).first().waitFor();
      assert.equal(await page.locator('.message-body').filter({hasText:'Application streaming:'})
        .locator('a.resource-link').filter({hasText:'i-stream'}).count(),0,'partial ID linked');
      await mutate(mode,'stream-name');
      await page.waitForFunction(()=>[...document.querySelectorAll('.message-body')].some(node=>
        node.textContent.startsWith('Application streaming:') && node.textContent.trim().endsWith('RAM role Applica')));
      assert.equal(await page.locator('.message-body').filter({hasText:'Application streaming:'})
        .locator('[data-resource-link]').filter({hasText:'Applica'}).count(),0,'partial resource name linked');
      await mutate(mode,'stream-end');
      await waitStreamLink();
      await mutate(mode,'deploy-start');
      await waitProgress(false);
      await mutate(mode,'deploy-identifiers');
      await waitProgress(true);
      const outputsReady=page.waitForResponse(response=>new URL(response.url()).pathname.endsWith('/outputs') && response.status()===200);
      await mutate(mode,'deploy-finish');
      await outputsReady;
      const deployment=await auditDeployment(mode,{progress:true});
      deployments.set(mode,deployment);
      previewChoices.set(mode,await previewChoice(mode,sessionId,{save:true}));
      await page.reload({waitUntil:'domcontentloaded'});
      // Product startup deliberately opens a new-session draft, so reopen through
      // its actual sidebar rather than calling an internal loadSession function.
      await page.locator('#iac-code-web-root[data-ready="true"]').waitFor();
      await page.locator(`[data-session-id="${sessionId}"]`).first().click();
      await waitLinks();
      await closeOutputDrawers();
      await expandTranscript();
      const restored=await audit();
      assert.deepEqual(restored,live,'real history load must preserve text and exact targets');
      await waitStreamLink();
      assert.deepEqual(await auditDeployment(mode),deployment,'deployment outputs/file preview changed after refresh');
      await previewChoice(mode,sessionId,{expectedId:previewChoices.get(mode)});
      await mutate(mode,'recovery-facts');await start(sessionId);
      const names=initial.recoveryNames[mode];
      const originalChoice=await choiceBody().textContent();
      await choiceBody().locator('button.resource-link-pending').filter({hasText:names.role}).click();
      assert.equal(await page.locator('.resource-link-popover [name="regionId"]').count(),0,'RAM choice requires a region');
      await page.locator('.resource-link-popover [name="accountScope"]').selectOption('fake-application-account');
      await page.locator('.resource-link-popover [name="consolePartition"]').selectOption('aliyun');
      await page.locator('.resource-link-popover').getByRole('button',{name:'Select',exact:true}).click();
      await page.locator('.resource-link-popover').waitFor({state:'detached'});
      await assertChoice(names.role,originalChoice);
      const choiceSnapshot=await resourceSnapshot(sessionId);
      const binding=choiceSnapshot.bindings.find(item=>item.identifier===names.role);
      assert.ok(binding,'the actual application did not persist the name choice');
      const refreshRequest=await beginQuery(sessionId,'refresh',names.refresh);
      if (mode==='normal' && process.env.IAC_RESOURCE_TEST_SCREENSHOT) {
        await page.screenshot({path:process.env.IAC_RESOURCE_TEST_SCREENSHOT.replace(/\.png$/,'-approval.png')});
      }
      assert.equal((await counts()).queryCalls[names.refresh],1,'one click created multiple lookup attempts');
      assert.equal((await counts()).completedReads[names.refresh],undefined,'lookup read before approval');
      await reloadSession(sessionId);
      await assertChoice(names.role,originalChoice);
      assert.equal((await sessionSnapshot(sessionId)).pendingPermissions[0]?.requestId,refreshRequest,
        'reload lost or replaced the still-live approval');
      assert.equal((await counts()).completedReads[names.refresh],undefined,'reload bypassed approval');
      await approveQuery(sessionId,refreshRequest,'refresh',names.refresh);
      assert.equal((await counts()).queryCalls[names.refresh],1,'reload/approval repeated a lookup');
      assert.equal((await counts()).completedReads[names.refresh],1,'approval must perform exactly one read');
      const afterRefresh=await resourceSnapshot(sessionId);
      assert.equal(afterRefresh.items.find(item=>item.identifier===names.refresh)?.attributes.ServiceName,'QueriedService');
      assert.equal(afterRefresh.bindings.find(item=>item.identifier===names.role)?.referenceId,binding.referenceId);
      const restartRequest=await beginQuery(sessionId,'restart',names.restart);
      recovery.set(mode,{names,originalChoice,binding,refreshRequest,restartRequest});
      expectedCounts.queryCalls[names.refresh]=1;expectedCounts.queryCalls[names.restart]=1;
      expectedCounts.completedReads[names.refresh]=1;
      assert.equal((await counts()).completedReads[names.restart],undefined,'restart lookup read before approval');
      assert.equal(await page.locator('.resource-link-popover').isVisible(),false,'lookup must yield to the pending approval');
      if (mode!=='normal') {
        await mutate(mode,'handoff');
        await start(sessionId);
        await waitLinks();
        assert.deepEqual(await audit(),live,'pipeline to normal handoff lost resource history');
        assert.deepEqual(await auditDeployment(mode),deployment,'handoff changed deployment targets');
        await assertChoice(names.role,originalChoice);
        assert.equal((await sessionSnapshot(sessionId)).pendingPermissions[0]?.requestId,restartRequest);
      }
      modes.push({mode,...auxiliary,...errorAudit,locations:live.locations+8,history:true,stream:true,deployment:true,stackOutputs:true,
        outputPanel:true,filePreview:true,previewChoiceRefresh:true,
        handoff:mode!=='normal',choiceRefresh:true,approvalRefresh:true,approvalOverlay:true});
      expected.set(mode,live);
      console.log(JSON.stringify({applicationMode:mode,stage:'passed'}));
    }
    assert.deepEqual(await counts(),expectedCounts,'non-query actions invoked credentials or extra queries');
    await page.goto('about:blank');
    origin=await restart();
    assert.deepEqual((await getState()).sessions,initial.sessions,'restart must reuse the persisted session directories');
    assert.ok(Object.values((await getState()).eventsSent).every(events=>events.length===0),'restart replayed fixture operations');
    for (const [mode,sessionId] of Object.entries(initial.sessions)) {
      await start(sessionId);
      const beforePreview=await resourceSnapshot(sessionId);
      assert.equal(beforePreview.bindings.some(binding=>binding.identifier==='PreviewOnlyRole'),false,
        'restart activated a saved preview choice before the file was displayed');
      assert.deepEqual(await audit(),expected.get(mode),'server restart changed recovered targets: '+mode);
      await waitStreamLink();
      assert.deepEqual(await auditDeployment(mode),deployments.get(mode),'restart lost deployment links');
      await previewChoice(mode,sessionId,{expectedId:previewChoices.get(mode)});
      const response=await context.request.get(origin+`/api/sessions/${encodeURIComponent(sessionId)}/messages`);
      const snapshot=(await response.json()).resourceReferences;
      assert.equal(snapshot.items.filter(item=>item.identifier!=='PreviewOnlyRole').length,10,
        'facts and the scoped declaration must survive without replaying fake operations');
      assert.equal(snapshot.items.filter(item=>item.identifier==='PreviewOnlyRole').length,1);
      await mutate(mode,'preview-change');
      await previewChoice(mode,sessionId,{changed:true});
      if (mode==='normal') {
        await previewRaces(mode,sessionId,initial.sessions.selling);
        Object.assign(modes.find(item=>item.mode===mode),{previewLateResponse:true,previewSessionSwitch:true,previewPopoverCleanup:true});
      }
      Object.assign(modes.find(item=>item.mode===mode),{previewChoiceRestart:true,previewChoiceRevocation:true});
      assert.ok(snapshot.items.some(item=>item.identifier==='ApplicationRole' && !item.regionId));
      const {names,originalChoice,binding,restartRequest}=recovery.get(mode);
      await assertChoice(names.role,originalChoice);
      assert.equal(snapshot.bindings.find(item=>item.identifier===names.role)?.referenceId,binding.referenceId);
      await queryLink('refresh',names.refresh).waitFor();
      assert.equal(snapshot.items.find(item=>item.identifier===names.refresh)?.attributes.ServiceName,'QueriedService',
        'completed attributes were lost across process restart');
      assert.equal((await sessionSnapshot(sessionId)).pendingPermissions.length,0,'an ephemeral approval survived process restart');
      const stale=await context.request.post(origin+`/api/permissions/${encodeURIComponent(restartRequest)}/answer`,
        {data:{sessionId,choice:'allow_once'}});
      assert.equal(stale.status(),404,'an old approval could authorize work in the new process');
      assert.equal((await counts()).queryCalls[names.restart],undefined,'recovery automatically restarted a lookup');
      const retryRequest=await beginQuery(sessionId,'restart',names.restart);
      assert.notEqual(retryRequest,restartRequest,'retry reused the expired approval');
      await approveQuery(sessionId,retryRequest,'restart',names.restart);
      assert.equal((await counts()).queryCalls[names.restart],1,'explicit retry did not perform one new attempt');
      await activate(queryLink('restart',names.restart),'https://ram.console.aliyun.com/fixture/'+encodeURIComponent(names.restart));
      await activate(choiceBody().locator('a.resource-link'),'https://ram.console.aliyun.com/fixture/'+encodeURIComponent(names.role));
      modes.find(item=>item.mode===mode).processRestart=true;
      Object.assign(modes.find(item=>item.mode===mode),{choiceRestart:true,attributesRestart:true,approvalRestart:true});
    }
    for (const mode of ['selling','selling_solution_first']) {
      await snapshotQuestion(mode,initial.sessions[mode]);
      Object.assign(modes.find(item=>item.mode===mode),{snapshotQuestionChoice:true,snapshotQuestionRefresh:true,snapshotQuestionRevocation:true});
    }
    const expectedRetries=Object.fromEntries([...recovery.values()].map(({names})=>[names.restart,1]));
    assert.deepEqual(await counts(),{contextLoads:0,queryCalls:expectedRetries,completedReads:expectedRetries},
      'restart recovery performed unrequested queries');
    assert.ok(rejectedPayloadUrls>=3,'actual output payload URL bypass must be exercised in each mode');
    assert.ok(signinCapabilityRequests>0,'the application did not load the resource-link open capability');
    assert.ok(signinRequests>0,'resource previews must use the single-use sign-in endpoint');
    assert.equal(errors.length,0,errors.join('\n'));
    assert.ok(streams.length>=3 && streams.every(status=>status===200),'actual SSE connections required');
    assert.ok(responses.length>=6 && responses.every(status=>status===200),'actual messages HTTP required');
    return {modes,messageLoads:responses.length,sseConnections:streams.length,openedPages,signinRequests,
      signinCapabilityRequests,rejectedPayloadUrls,
      errors:errors.length};
  } catch (error) {
    let state = null;
    try { state = await page.evaluate(()=>({
      bodies:[...document.querySelectorAll('.message-body')].map(node=>({
        text:node.textContent.slice(0,180),links:[...node.querySelectorAll('[data-resource-link]')].map(link=>({
          text:link.textContent,tag:link.tagName,label:link.getAttribute('aria-label'),href:link.getAttribute('href'),
        })),
      })),
      appError:document.querySelector('[data-shell="app-error"]')?.textContent,
      popover:document.querySelector('.resource-link-popover')?.textContent,
      deployment:document.querySelector('[data-tool-use-id="application-deployment"]')?.textContent,
      details:[...document.querySelectorAll('.message-stack details')].map(node=>({key:node.dataset.openKey,open:node.open,visible:node.checkVisibility()})),
      deploymentResults:[...document.querySelectorAll('[data-tool-use-id="application-deployment"] .conclusion-field')].map(row=>({
        key:row.querySelector(':scope > dt')?.textContent,visible:row.checkVisibility(),
        links:[...row.querySelectorAll('[data-resource-link]')].map(node=>({text:node.textContent,href:node.getAttribute('href')})),
      })),
      progress:[...document.querySelectorAll('.tool-stack-progress [data-resource-link]')].map(node=>({text:node.textContent,href:node.getAttribute('href')})),
      outputs:document.querySelector('[data-app-shell="output-panel-body"]')?.textContent,
      preview:document.querySelector('.output-preview-code')?.textContent,
      auditError:globalThis.__IAC_LAST_RESOURCE_AUDIT_ERROR__,
    })); } catch (inspectionError) { state = {inspectionError:String(inspectionError),url:page.url()}; }
    console.error(JSON.stringify({applicationError:String(error),applicationFailure:state}));
    throw error;
  } finally {await context.close();}
}
