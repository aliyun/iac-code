function completedOwnReply({key,targets}) {
 const matches=()=>{
  const active=document.querySelector('.session-item.is-active[data-session-id]');
  const send=document.querySelector('[data-app-shell=composer-send]');
  return active?.getAttribute('data-session-id')===key&&Boolean(send)&&!send.classList.contains('is-stopping')&&
   targets.every(target=>[...document.querySelectorAll('a.resource-link[data-resource-link][href]')].some(n=>
    n.getAttribute('aria-label')===target&&n.href.includes('console.aliyun.com')&&!n.closest('details:not([open])')&&n.getClientRects().length>0));
 };
 return matches();
}
function readinessSnapshot({key,targets}) {
 const active=document.querySelector('.session-item.is-active[data-session-id]')?.getAttribute('data-session-id');
 const send=document.querySelector('[data-app-shell=composer-send]');
 const anchors=[...document.querySelectorAll('a.resource-link[data-resource-link][href]')];
 return {scope:'readiness_only_not_click_evidence',origin:location.origin,visibility:document.visibilityState,
  exactSessionActive:active===key,composerButtonCount:send?1:0,stopping:send?.classList.contains('is-stopping')??null,
  targets:targets.map(target=>{const exact=anchors.filter(n=>n.getAttribute('aria-label')===target&&n.href.includes('console.aliyun.com'));
   return {target,exactCount:exact.length,openVisibleCount:exact.filter(n=>!n.closest('details:not([open])')&&n.getClientRects().length>0).length};})};
}
async function waitForOwnTargets(page,key,targets,timeout) {
 const handle=await page.waitForFunction(({key,targets})=>
  document.querySelector('.session-item.is-active[data-session-id]')?.getAttribute('data-session-id')===key&&
  targets.every(target=>[...document.querySelectorAll('a.resource-link[data-resource-link][href]')].some(n=>
   n.getAttribute('aria-label')===target&&n.href.includes('console.aliyun.com'))),
  {key,targets},{timeout,polling:100});
 try {if(await handle.jsonValue()!==true)throw Error('exact target gate returned non-true');}
 finally {await handle.dispose();}
}

// References can arrive in a streaming tool result before the surrounding
// details tree is final. Wait for the same session to finish before unfolding
// it; this is preparation only, never a resource click or a PASS condition.
function ownReplyEnded({key,targets}) {
 const active=document.querySelector('.session-item.is-active[data-session-id]');
 const send=document.querySelector('[data-app-shell=composer-send]');
 return active?.getAttribute('data-session-id')===key&&Boolean(send)&&!send.classList.contains('is-stopping')&&
  targets.every(target=>[...document.querySelectorAll('a.resource-link[data-resource-link][href]')].some(n=>
   n.getAttribute('aria-label')===target&&n.href.includes('console.aliyun.com')));
}
async function waitForOwnReplyEnd(page,key,targets,timeout) {
 const handle=await page.waitForFunction(ownReplyEnded,{key,targets},{timeout,polling:100});
 try {if(await handle.jsonValue()!==true)throw Error('own reply end gate returned non-true');}
 finally {await handle.dispose();}
}

function outermostClosedDetailIndex(nodes) {
 const candidates=nodes.map((node,index)=>({node,index})).filter(({node})=>
  !nodes.some(other=>other!==node&&other.contains(node)));
 return candidates.length===1?candidates[0].index:-1;
}

async function unfoldOwnVisibleTargets(page,key,targets,deadline) {
 // Streaming output can leave a duplicate reference inside a collapsed tool
 // card while the exact same own-session reference is already visible in the
 // completed answer. Do not unfold an irrelevant duplicate (or click links).
 for(const target of [...new Set(targets)]) {
  if(await page.evaluate(completedOwnReply,{key,targets:[target]})===true)continue;
  const anchors=page.locator('a.resource-link[aria-label='+JSON.stringify(target)+']');
  let visible=false;
  for(let i=0;i<await anchors.count();i++) {
   const anchor=anchors.nth(i);
   for(let depth=0;depth<8;depth++) {
    const closed=anchor.locator('xpath=ancestor::details[not(@open)]');
    if(!await closed.count())break;
    const outerIndex=await closed.evaluateAll(outermostClosedDetailIndex);
    if(outerIndex<0)throw Error('ambiguous closed ancestors');
    const summary=closed.nth(outerIndex).locator(':scope > summary');
    if(!await summary.isVisible())break;
    await summary.click({timeout:Math.max(1,deadline-Date.now())});
   }
   // Preserve domestic URL, own complete session, completed reply and actual
   // uncollapsed visibility, rather than trusting isVisible on a summary.
   if(await page.evaluate(completedOwnReply,{key,targets:[target]})===true){visible=true;break;}
  }
  if(!visible)throw Error('own exact target not visibly unfolded');
 }
}

async function prepareOwnTargetSubset(page,key,targets,deadline) {
 // At least one intended reference must arrive before checking reply end.
 // One missing sample cannot suppress the other independently valid contracts.
 const handle=await page.waitForFunction(({key,targets})=>
  document.querySelector('.session-item.is-active[data-session-id]')?.getAttribute('data-session-id')===key&&
  targets.some(target=>[...document.querySelectorAll('a.resource-link[data-resource-link][href]')].some(n=>
   n.getAttribute('aria-label')===target&&n.href.includes('console.aliyun.com'))),
  {key,targets},{timeout:Math.max(1,deadline-Date.now()),polling:100});
 try {if(await handle.jsonValue()!==true)throw Error('any exact target gate returned non-true');}
 finally {await handle.dispose();}
 await waitForOwnReplyEnd(page,key,[],Math.max(1,deadline-Date.now()));
 await page.waitForTimeout(1200);
 if(await page.evaluate(ownReplyEnded,{key,targets:[]})!==true)throw Error('own reply end not stable');
 const snapshot=await page.evaluate(readinessSnapshot,{key,targets});
 const readyTargets=[],missingTargets=[];
 for(const target of [...new Set(targets)]) {
  if(!snapshot.targets.some(row=>row.target===target&&row.exactCount>0)) {
   missingTargets.push({target,reason:'exact own source sample absent; unexecuted'});continue;
  }
  try {
   await unfoldOwnVisibleTargets(page,key,[target],deadline);
   if(await page.evaluate(completedOwnReply,{key,targets:[target]})!==true)throw Error('not visible');
   readyTargets.push(target);
  }catch {
   missingTargets.push({target,reason:'exact own target not visibly unfolded; unexecuted'});
  }
 }
 if(!readyTargets.length)throw Error('no independently ready own targets');
 await page.waitForTimeout(1200);
 if(await page.evaluate(completedOwnReply,{key,targets:readyTargets})!==true)
  throw Error('own ready subset not stably completed');
 return {readyTargets,missingTargets,snapshot:await page.evaluate(readinessSnapshot,{key,targets:readyTargets})};
}

module.exports={completedOwnReply,readinessSnapshot,waitForOwnTargets,ownReplyEnded,waitForOwnReplyEnd,
 outermostClosedDetailIndex,unfoldOwnVisibleTargets,prepareOwnTargetSubset};
