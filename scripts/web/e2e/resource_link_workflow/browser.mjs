/** Scoped page operations. No eval, arbitrary URL navigation, cookies, or browser-wide close. */
import fs from 'node:fs';
import path from 'node:path';
import {createRequire} from 'node:module';
import {pathToFileURL} from 'node:url';
import {createHash,randomUUID} from 'node:crypto';

export function selectOccurrence(candidates, previous) {
  if (!candidates.length || candidates.some(item => !item.href || !item.href.startsWith('https://')) ||
      new Set(candidates.map(item => item.href)).size !== 1) throw Error('precise reference destination ambiguous or pending');
  if (previous) {
    const match = candidates.find(item => item.index === previous.index && item.focus === previous.focus && item.href === previous.href);
    if (!match) throw Error('bound product occurrence changed');
    return match;
  }
  // Repeated mentions of the same verified reference are not distinct resources.
  return candidates.at(-1);
}

export async function revealOriginalSourceCard(page, request) {
  const owner=request.source_card_tool_use_id;
  if(owner === undefined) return {expanded:false};
  if(typeof owner!=='string' || !/^[A-Za-z0-9_-]+$/.test(owner) ||
      !request.selector.startsWith(`details[data-tool-use-id="${owner}"] `))
    throw Error('invalid exact original source card');
  const turn=request.source_card_turn_id;
  if(typeof turn!=='string' || !/^[A-Za-z0-9-]+$/.test(turn)) throw Error('invalid exact original source turn');
  const process=page.locator(`details.turn-process[data-open-key="turnproc:${turn}"]`);
  if(await process.count()!==1) throw Error('exact original source turn missing or ambiguous');
  const processExpanded=await process.getAttribute('open')!==null;
  if(!processExpanded){
    const summary=process.locator(':scope > summary');
    if(await summary.count()!==1 || !await summary.isVisible()) throw Error('original source turn control missing');
    await summary.click();
    if(await process.getAttribute('open')===null) throw Error('original source turn did not expand');
  }
  const card=page.locator(`details[data-tool-use-id="${owner}"]`);
  if(await card.count()!==1) throw Error('exact original source card missing or ambiguous');
  const expanded=await card.getAttribute('open')!==null;
  if(!expanded){
    const summary=card.locator(':scope > summary');
    if(await summary.count()!==1 || !await summary.isVisible()) throw Error('original source card control missing');
    await summary.click();
    if(await card.getAttribute('open')===null) throw Error('original source card did not expand');
  }
  return {expanded:!expanded,turn_expanded:!processExpanded,tool_use_id:owner,turn_id:turn};
}

export function navigationReady(view, identifier, markers=[]) {
  return view.text.includes(identifier) && !view.loading && view.facts_visible !== false &&
    markers.every(marker=>view.text.includes(marker));
}

export function loadingSignalPending(signal) {
  // A resource's completed progress field is not a document loader. Keep
  // explicit busy/mask/spinner/skeleton signals authoritative, even at 100%.
  if(signal.explicit_loading !== false || signal.role !== 'progressbar' || signal.aria_busy === 'true') return true;
  const numeric = value => typeof value === 'string' && value.trim() !== '' && Number.isFinite(Number(value));
  const completedValues = numeric(signal.value_now) && numeric(signal.value_max) &&
    Number(signal.value_max) > 0 && Number(signal.value_now) >= Number(signal.value_max);
  // ECS's resource progress component supplies no ARIA values. Require both
  // its observed success state and complete text; indeterminate bars stay busy.
  const completedSuccess = /(?:^|\s)[\w-]*progress-status-success(?:\s|$)/.test(signal.class_name || '') &&
    /^\s*100(?:\.0+)?%\s*$/.test(signal.text || '');
  return !(completedValues || completedSuccess);
}

export function nasMetadataReady(view, expected) {
  return view.name_visible === true && view.description_visible === true && view.name_disabled === true &&
    view.name === expected.identifier && view.description === expected.description;
}

export async function inspectNasMetadata(surface, frame, expected, file, options={}) {
  // Preserve the original resource-link landing image separately. Only fixed
  // existing console controls: sidebar, unique source-bound row, edit, cancel.
  // Never fill, save, delete, construct a destination URL, or call a cloud API.
  if(!frame || expected.kind !== 'nas_access_group_metadata' ||
      !/^[A-Za-z][A-Za-z0-9_-]{2,63}$/.test(expected.identifier) ||
      !/^cn-[a-z0-9-]+$/.test(expected.region) || !expected.description) throw Error('invalid NAS inspection');
  const initial=new URL(frame.url());
  if(initial.protocol!=='https:' || !['nas.console.aliyun.com','nasnext.console.aliyun.com','nasnext4service.console.aliyun.com'].includes(initial.hostname) ||
      !initial.pathname.startsWith('/'+expected.region+'/access-group')) throw Error('unbound NAS console');
  return withForegroundLock(options.lockFile,async()=>{
    await surface.bringToFront();
    const stages=[{step:'resource_link_landing',url:frame.url()}];
    const links=await frame.getByRole('link',{name:'权限组',exact:true}).all();
    const visible=[];
    for(const link of links)if(await link.isVisible() && await link.evaluate(el=>
      !!el.closest('.next-nav-item') && !el.closest('.next-breadcrumb'))){
      const href=await link.getAttribute('href');
      const destination=href ? new URL(href,frame.url()) : null;
      // The real sidebar adds its analytics-only spm parameter. Validate its
      // observed destination, then click that element; never synthesize a URL.
      if(destination&&destination.origin===initial.origin&&!destination.username&&!destination.password&&
        !destination.hash&&[...destination.searchParams.keys()].every(key=>key==='spm')&&
        ['/'+expected.region+'/access-group','/'+expected.region+'/access-group/standard'].includes(destination.pathname))
        visible.push({link,href});
    }
    if(!visible.length || new Set(visible.map(item=>item.href)).size!==1) throw Error('NAS list control ambiguous');
    await visible[0].link.click({timeout:10000});
    stages.push({step:'actual_sidebar_click',url:frame.url()});
    const row=frame.locator('tr').filter({has:frame.getByText(expected.identifier,{exact:true})});
    await row.waitFor({state:'visible',timeout:10000});
    if(await row.count()!==1)throw Error('NAS exact row ambiguous');
    const edit=row.getByRole('button',{name:'编辑',exact:true});
    if(await edit.count()!==1)throw Error('NAS metadata control ambiguous');
    await edit.click({timeout:10000});
    const dialog=frame.locator('.next-dialog').filter({hasText:'编辑权限组'});
    let result;
    try {
      await dialog.waitFor({state:'visible',timeout:10000});
      if(await dialog.count()!==1)throw Error('NAS metadata dialog ambiguous');
      const name=dialog.locator('input[name="AccessGroupName"]');
      const description=dialog.locator('textarea[name="Description"]');
      const visibleField=locator=>locator.evaluate(el=>{
        const rect=el.getBoundingClientRect();
        const left=Math.max(0,rect.left),top=Math.max(0,rect.top);
        const right=Math.min(innerWidth,rect.right),bottom=Math.min(innerHeight,rect.bottom);
        if(right<=left||bottom<=top)return false;
        for(let node=el;node;node=node.parentElement){
          const style=getComputedStyle(node);
          if(style.visibility==='hidden'||style.display==='none'||Number(style.opacity)<0.8)return false;
        }
        const hit=document.elementFromPoint((left+right)/2,(top+bottom)/2);
        // Disabled names have pointer-events:none; their immediate input
        // wrapper is the hit target. Unrelated overlays still fail closed.
        return hit===el||el.contains(hit)||hit===el.parentElement;
      });
      const readView=async()=>({name:await name.inputValue(),description:await description.inputValue(),
        name_disabled:await name.isDisabled(),name_visible:await visibleField(name),
        description_visible:await visibleField(description)});
      // Visible attachment precedes Fusion's fade-in. Wait only for genuine
      // form pixels, not a resource/model deadline, and retain honest failure.
      const deadline=Date.now()+5000;
      let view=await readView();
      while(!(view.name_visible&&view.description_visible)&&Date.now()<deadline){
        await new Promise(resolve=>setTimeout(resolve,50));view=await readView();
      }
      const capture=await captureNavigationPixels(surface,file,{consoleFrame:frame,mode:options.mode,
        identifier:expected.identifier,beforeCapture:options.beforeCapture});
      stages.push({step:'readonly_metadata_dialog',url:frame.url()});
      result={kind:expected.kind,identity_visible:nasMetadataReady(view,expected)&&capture.foreground_visible===true&&
        (options.mode!=='iframe'||capture.frame_embedding?.visible===true),fields:view,
        screenshot:file,capture_sync:capture,stages,original_landing_unchanged:true,cloud_writes:0};
    }finally{
      // Even mismatched metadata is only observed. Cancel rather than submit.
      if(await dialog.isVisible().catch(()=>false)){
        const cancel=dialog.getByRole('button',{name:'取消',exact:true});
        if(await cancel.count()!==1)throw Error('NAS cancel control ambiguous');
        await cancel.click({timeout:10000});
        await dialog.waitFor({state:'hidden',timeout:5000});
      }
    }
    return {...result,dialog_cancelled:true};
  });
}

export function slbAclConsoleUrl(raw, expected, list=false) {
  let url;try{url=new URL(raw);}catch{throw Error('unbound SLB console');}
  const base='/slb/'+expected.region+'/acls';
  if(url.protocol!=='https:'||!['slb.console.aliyun.com','slb4service.console.aliyun.com'].includes(url.hostname)||
      url.username||url.password||url.hash||[...url.searchParams.keys()].some(key=>key!=='spm')||
      url.pathname!==(list?base:base+'/'+expected.identifier))throw Error('unbound SLB console');
  return url;
}

export function slbAclMetadataReady(view, expected) {
  const markers=[expected.identifier,expected.name,expected.address_family];
  return !view.loading&&markers.every(marker=>view.fields?.some(field=>field.marker===marker&&field.visible===true));
}

export function slbAclSidebarUrl(href, initial, expected) {
  let destination;try{destination=new URL(href,initial);}catch{throw Error('unbound SLB sidebar');}
  if(destination.origin!==initial.origin||destination.username||destination.password||destination.hash||
      [...destination.searchParams.keys()].some(key=>key!=='spm')||
      destination.pathname!=='/slb/'+expected.region+'/acls')
    throw Error('unbound SLB sidebar');
  return destination;
}

export function aclHorizontalDelta(view, expected) {
  const field=view.fields?.find(field=>field.marker===expected.address_family);
  const gap=view.scroll;
  if(!field||!gap||!Number.isFinite(gap.left)||!Number.isFinite(gap.right)||
      !Number.isFinite(field.left)||!Number.isFinite(field.right)||
      gap.right-gap.left<field.right-field.left+4||!Number.isFinite(gap.current)||
      !Number.isFinite(gap.maximum))throw Error('SLB metadata column has no unoccluded viewport');
  const delta=(field.left+field.right-gap.left-gap.right)/2;
  if(gap.current+delta<0||gap.current+delta>gap.maximum)throw Error('SLB metadata column scroll unavailable');
  return delta;
}

export async function inspectSlbAclMetadata(surface, frame, expected, file, options={}) {
  // Only the observed ACL sidebar/list and native horizontal scrolling. Keep
  // the original detail PNG; never edit, save, delete, hide DOM or invent a URL.
  if(!frame||expected.kind!=='slb_acl_metadata'||!/^acl-[a-z0-9]+$/.test(expected.identifier)||
      !/^cn-[a-z0-9-]+$/.test(expected.region)||typeof expected.name!=='string'||!expected.name.trim()||
      !['IPv4','IPv6'].includes(expected.address_family))throw Error('invalid SLB inspection');
  // Playwright's OOPIF url cache can be empty. Read this exact frame's actual
  // document location; a blank/cache URL is not an alternative host allowance.
  const documentUrl=()=>frame.evaluate(()=>location.href);
  const initial=slbAclConsoleUrl(await documentUrl(),expected);
  return withForegroundLock(options.lockFile,async()=>{
    await surface.bringToFront();
    if(options.beforeCapture)await options.beforeCapture();
    const stages=[{step:'resource_link_landing',url:initial.href}];
    const sidebar=frame.locator('a[data-spm-id="menu.acl"]');
    if(await sidebar.count()!==1||!await sidebar.isVisible())throw Error('SLB list control ambiguous');
    let href,destination;
    const menuDeadline=Date.now()+10000;
    do{
      href=await sidebar.getAttribute('href');
      // Actual full-console hydration initially renders /slb, which routes
      // to instance management if clicked early. Wait for the real regional
      // ACL href, rather than permitting/constructing that placeholder route.
      const pending=new URL(href,initial);
      if(initial.hostname==='slb.console.aliyun.com'&&pending.origin===initial.origin&&
          pending.pathname==='/slb'&&!pending.search&&!pending.hash&&!pending.username&&!pending.password){
        if(Date.now()>=menuDeadline)throw Error('SLB list control not hydrated');
        await new Promise(resolve=>setTimeout(resolve,50));continue;
      }
      destination=slbAclSidebarUrl(href,initial,expected);break;
    }while(true);
    if(!href||destination.origin!==initial.origin)throw Error('unbound SLB sidebar');
    await sidebar.click({timeout:10000});
    const row=frame.locator('tr').filter({has:frame.getByText(expected.identifier,{exact:true})});
    await row.waitFor({state:'visible',timeout:10000});
    if(await row.count()!==1)throw Error('SLB exact row ambiguous or outside loaded page');
    const assertList=async()=>{
      const actual=slbAclConsoleUrl(await documentUrl(),expected,true);
      if(actual.origin!==initial.origin)throw Error('SLB console origin changed');
      return actual.href;
    };
    stages.push({step:'actual_sidebar_click',url:await assertList()});
    const markers=[expected.identifier,expected.name,expected.address_family];
    for(const marker of markers)if(await row.getByText(marker,{exact:true}).count()!==1)
      throw Error('SLB source-bound row field missing or ambiguous');
    const readView=()=>row.evaluate((el,{markers,selector})=>{
      const fields=markers.map(marker=>{
        const walker=document.createTreeWalker(el,NodeFilter.SHOW_TEXT);let node;
        while(walker.nextNode())if(walker.currentNode.textContent.trim()===marker){node=walker.currentNode;break;}
        if(!node)return {marker,visible:false};
        const range=document.createRange();range.selectNodeContents(node);const r=range.getBoundingClientRect();
        const points=[r.left+1,(r.left+r.right)/2,r.right-1];
        const hit=points.every(x=>{const target=document.elementFromPoint(x,(r.top+r.bottom)/2);
          return target&&(target===node.parentElement||node.parentElement.contains(target)||target.contains(node.parentElement));});
        let opacity=1,hidden=false;
        for(let n=node.parentElement;n;n=n.parentElement){const style=getComputedStyle(n);
          opacity*=Number(style.opacity);hidden ||= style.display==='none'||style.visibility==='hidden';}
        return {marker,left:r.left,right:r.right,top:r.top,bottom:r.bottom,
          visible:r.width>0&&r.height>0&&r.left>=0&&r.right<=innerWidth&&r.top>=0&&r.bottom<=innerHeight&&hit&&!hidden&&opacity>=0.8};
      });
      let container=el.parentElement;
      while(container&&!(container.scrollWidth>container.clientWidth&&container.clientWidth>0))container=container.parentElement;
      let scroll=null;
      if(container){const r=container.getBoundingClientRect();let left=Math.max(0,r.left),right=Math.min(innerWidth,r.right);
        for(const cell of el.querySelectorAll('td')){const style=getComputedStyle(cell),c=cell.getBoundingClientRect();
          if(style.position!=='sticky')continue;
          if(style.left!=='auto')left=Math.max(left,c.right);
          if(style.right!=='auto')right=Math.min(right,c.left);}
        scroll={left,right,current:container.scrollLeft,maximum:container.scrollWidth-container.clientWidth};}
      return {fields,scroll,loading:[...document.querySelectorAll(selector)].some(n=>{
        const style=getComputedStyle(n);return n.getClientRects().length&&style.visibility!=='hidden'&&style.display!=='none'&&Number(style.opacity)>0.01;})};
    },{markers,selector:LOADING_SELECTOR});
    let view=await readView();
    const family=view.fields.find(field=>field.marker===expected.address_family);
    if(family&&!family.visible){
      const delta=aclHorizontalDelta(view,expected);
      await row.hover();await surface.mouse.wheel(delta,0);
      await frame.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
      stages.push({step:'native_horizontal_scroll',delta});
    }
    const capture=await captureNavigationPixels(surface,file,{consoleFrame:frame,mode:options.mode,
      identifier:expected.identifier,markers:[expected.name,expected.address_family],beforeCapture:async()=>{
        if(options.beforeCapture)await options.beforeCapture();
        await assertList();view=await readView();
      }});
    stages.push({step:'readonly_exact_row',url:await assertList()});
    return {kind:expected.kind,identity_visible:slbAclMetadataReady(view,expected)&&capture.foreground_visible===true&&
      (options.mode!=='iframe'||capture.frame_embedding?.visible===true),fields:view,
      screenshot:file,capture_sync:capture,stages,original_landing_unchanged:true,cloud_writes:0,readonly_completed:true};
  });
}

export async function withForegroundLock(lockFile, action, timeoutMs=20000) {
  if (!lockFile) return action(); // Offline fixtures; production injects OPS-owned path.
  const deadline=Date.now()+timeoutMs;
  const owner=JSON.stringify({pid:process.pid,nonce:randomUUID()});
  fs.mkdirSync(path.dirname(lockFile),{recursive:true});
  let handle;
  while (handle === undefined) {
    try {handle=fs.openSync(lockFile,'wx',0o600);fs.writeFileSync(handle,owner);}
    catch(error) {
      if(error.code!=='EEXIST')throw error;
      // Recover a crashed holder only after checking the exact unchanged file
      // and a known dead PID. Unknown owners are never guessed away.
      let stat,original;
      try {stat=fs.lstatSync(lockFile);original=fs.readFileSync(lockFile,'utf8');}
      catch(race){if(race.code==='ENOENT')continue;throw race;}
      if(!stat.isFile()||stat.isSymbolicLink()||stat.size>1024)throw Error('foreground lock owner invalid');
      try {
        const pid=JSON.parse(original).pid;
        if(!Number.isSafeInteger(pid)||pid<1)throw Error('foreground lock owner invalid');
        try {process.kill(pid,0);}
        catch(dead) {if(dead.code==='ESRCH'&&fs.readFileSync(lockFile,'utf8')===original)fs.unlinkSync(lockFile);}
      } catch(error) {if(error.message==='foreground lock owner invalid')throw error;}
      if(Date.now()>=deadline)throw Error('foreground capture busy; preserve scene for OPS');
      await new Promise(resolve=>setTimeout(resolve,25));
    }
  }
  try {return await action();}
  finally {
    fs.closeSync(handle);
    if(fs.readFileSync(lockFile,'utf8')!==owner)throw Error('foreground lock ownership changed');
    fs.unlinkSync(lockFile);
  }
}

export async function captureNavigationPixels(surface, file, options={}) {
  // DOM readiness in a background OOPIF is not proof that its current pixels
  // have been composited. Focus only this exact leased page/popup, then let
  // Chrome paint it. Do not resize/capture beyond the live visible viewport.
  return withForegroundLock(options.lockFile,async()=>{
    const stages={started_at:new Date().toISOString()};
    await surface.bringToFront();
    // A federation/OOPIF swap may detach the frame acquired at click time.
    // Resolve only the original leased preview embedding after checking its
    // sealed session. Never fall back to another frame or another tab.
    if(options.resolveConsoleFrame&&options.beforeCapture)await options.beforeCapture();
    const consoleFrame=options.resolveConsoleFrame ? await options.resolveConsoleFrame() : options.consoleFrame;
    const paint=frame=>frame.evaluate(() => new Promise(resolve => {
      const fallback = setTimeout(()=>resolve({painted:false,reason:'frame paint timeout'}),1000);
      requestAnimationFrame(() => requestAnimationFrame(() => {
        clearTimeout(fallback);resolve({painted:true});
      }));
    })).catch(()=>({painted:false,reason:'frame detached or inaccessible'}));
    stages.main_paint=await paint(surface);
    if(consoleFrame)stages.console_paint=await paint(consoleFrame);
    if(options.mode==='iframe'&&consoleFrame){
      let embedding;
      try{
        embedding=await consoleFrame.frameElement();
        stages.frame_embedding=await embedding.evaluate(el=>{
          const rect=el.getBoundingClientRect();
          const left=Math.max(0,rect.left),top=Math.max(0,rect.top);
          const right=Math.min(innerWidth,rect.right),bottom=Math.min(innerHeight,rect.bottom);
          let opacity=1,hidden=false;
          for(let parent=el;parent;parent=parent.parentElement){
            const style=getComputedStyle(parent);opacity*=Number(style.opacity);
            hidden ||= style.visibility==='hidden'||style.display==='none';
          }
          const inViewport=right>left&&bottom>top;
          return {visible:inViewport&&!hidden&&opacity>=0.8&&document.elementFromPoint((left+right)/2,(top+bottom)/2)===el,
            rect:{left:rect.left,top:rect.top,right:rect.right,bottom:rect.bottom},opacity};
        });
      }catch{stages.frame_embedding={visible:false,reason:'matched frame embedding inaccessible'};}
      finally{if(embedding)await embedding.dispose();}
    }
    if(!options.resolveConsoleFrame&&options.beforeCapture)await options.beforeCapture();
    // Recheck the source-bound console facts after foreground/frame paint, not
    // the old DOM observation made before other targets could switch focus.
    if(options.mode)stages.render_check=await waitForNavigationIdentity(surface,options.mode,
      options.identifier,0,options.markers||[],consoleFrame ? [consoleFrame] : []);
    if(options.mode)stages.foreground_visible=await surface.evaluate(()=>document.visibilityState==='visible').catch(()=>false);
    await surface.screenshot({path:file, fullPage:false});
    stages.finished_at=new Date().toISOString();
    return stages;
  });
  // Honest pixels remain authoritative; this synchronization never adds PASS.
}

// Fusion retains next/xcomponent loading wrappers after loading completes.
// Domestic OSS uses xcomponent indicators, not next-prefixed indicators. Only
// actual visible indicator/tip/mask leaves and explicit busy states are busy.
export const LOADING_SELECTOR='[aria-busy="true"],[role="progressbar"],.next-loading-tip,.next-loading-indicator,.next-loading-mask,.xcomponent-loading-tip,.xcomponent-loading-indicator,.xcomponent-loading-masker,.ant-spin-spinning,.loading-indicator,[class*="skeleton"],[class*="Skeleton"]';
const EXPLICIT_LOADING_SELECTOR=LOADING_SELECTOR.split(',').filter(selector=>selector!=='[role="progressbar"]').join(',');

export async function waitForNavigationIdentity(surface, mode, identifier, timeoutMs=20000, markers=[], boundFrames) {
  if (!identifier) return {identity_visible:false,reason:'no bound identifier',frames:[]};
  const deadline=Date.now()+timeoutMs;
  let views=[];
  do {
    // Never mistake the Web preview header for the console's rendered identity.
    const frames=typeof boundFrames==='function' ? await boundFrames() :
      boundFrames ?? (mode==='open' ? [surface.mainFrame()] : surface.frames().filter(frame=>frame!==surface.mainFrame()));
    views=await Promise.all(frames.map(async frame=>{
      let embeddingClip=null;
      if(mode==='iframe'&&typeof frame.frameElement==='function'){
        let embedding;
        try{
          embedding=await frame.frameElement();
          embeddingClip=await embedding.evaluate(el=>{
            const rect=el.getBoundingClientRect();
            let left=Math.max(0,rect.left),top=Math.max(0,rect.top);
            let right=Math.min(innerWidth,rect.right),bottom=Math.min(innerHeight,rect.bottom);
            for(let parent=el.parentElement;parent;parent=parent.parentElement){
              const style=getComputedStyle(parent),box=parent.getBoundingClientRect();
              if(['hidden','clip','auto','scroll'].includes(style.overflowX)){
                left=Math.max(left,box.left);right=Math.min(right,box.right);
              }
              if(['hidden','clip','auto','scroll'].includes(style.overflowY)){
                top=Math.max(top,box.top);bottom=Math.min(bottom,box.bottom);
              }
            }
            return {left:left-rect.left-el.clientLeft,top:top-rect.top-el.clientTop,
              right:right-rect.left-el.clientLeft,bottom:bottom-rect.top-el.clientTop};
          });
        }catch{embeddingClip={left:0,top:0,right:0,bottom:0};}
        finally{if(embedding)await embedding.dispose();}
      }
      const view=await frame.evaluate(({selector,explicitSelector,facts,embeddingClip})=>{
        const visible=(el,minOpacity=0.8)=>{
          if (!el?.getClientRects().length) return false;
          let opacity=1;
          for(let parent=el;parent;parent=parent.parentElement) {
            const style=getComputedStyle(parent);
            if(style.visibility==='hidden'||style.display==='none') return false;
            opacity*=Number(style.opacity);
          }
          return opacity>=minOpacity;
        };
        const nodes=[];
        const walker=document.createTreeWalker(document.body,NodeFilter.SHOW_TEXT);
        while(walker.nextNode()) nodes.push(walker.currentNode);
        const inViewport=(node,fact)=>{
          const start=node.textContent.indexOf(fact),range=document.createRange();
          range.setStart(node,start);range.setEnd(node,start+fact.length);
          const pieces=[...range.getClientRects()].filter(rect=>rect.right>rect.left&&rect.bottom>rect.top);
          return pieces.length>0&&pieces.every(rect=>{
            let left=0,top=0,right=innerWidth,bottom=innerHeight;
            if(embeddingClip){left=Math.max(left,embeddingClip.left);top=Math.max(top,embeddingClip.top);
              right=Math.min(right,embeddingClip.right);bottom=Math.min(bottom,embeddingClip.bottom);}
            for(let parent=node.parentElement;parent;parent=parent.parentElement){
              const style=getComputedStyle(parent);
              if(!['hidden','clip','auto','scroll'].some(value=>[style.overflowX,style.overflowY].includes(value)))continue;
              const box=parent.getBoundingClientRect();
              if(['hidden','clip','auto','scroll'].includes(style.overflowX)){
                left=Math.max(left,box.left);right=Math.min(right,box.right);
              }
              if(['hidden','clip','auto','scroll'].includes(style.overflowY)){
                top=Math.max(top,box.top);bottom=Math.min(bottom,box.bottom);
              }
            }
            if(rect.left<left||rect.right>right||rect.top<top||rect.bottom>bottom)return false;
            return [0.01,0.5,0.99].every(x=>{
              const hit=document.elementFromPoint(rect.left+(rect.right-rect.left)*x,(rect.top+rect.bottom)/2);
              return hit&&(hit===node.parentElement||node.parentElement.contains(hit));
            });
          });
        };
        return {text:document.body?.innerText || '',
          facts_visible:facts.every(fact=>nodes.some(node=>node.textContent.includes(fact)&&visible(node.parentElement)&&inViewport(node,fact))),
          loading_signals:[...document.querySelectorAll(selector)].filter(el=>visible(el,0.01)).map(el=>({
            role:el.getAttribute?.('role') || null,aria_busy:el.getAttribute?.('aria-busy') || null,
            value_now:el.getAttribute?.('aria-valuenow') ?? null,value_max:el.getAttribute?.('aria-valuemax') ?? null,
            class_name:typeof el.className==='string'?el.className:'',text:(el.innerText || '').slice(0,80),
            explicit_loading:typeof el.matches!=='function' || el.matches(explicitSelector)}))};
      },{selector:LOADING_SELECTOR,explicitSelector:EXPLICIT_LOADING_SELECTOR,facts:[identifier,...markers],embeddingClip})
        .catch(()=>({text:'',loading:true}));
      if(Array.isArray(view.loading_signals)) view.loading=view.loading_signals.some(loadingSignalPending);
      return {url:frame.url(),...view};
    }));
    // A route-derived breadcrumb appears before the actual API details. Require
    // separately approved cloud facts (name/CIDR etc), never just that breadcrumb.
    if (views.some(view=>navigationReady(view,identifier,markers)))
      return {identity_visible:true,detail_markers:markers,frames:views};
    if (Date.now()>=deadline) break;
    await new Promise(resolve=>setTimeout(resolve,250));
  } while (true);
  return {identity_visible:false,reason:'bounded console identity render timeout',frames:views};
}

export function bareOrigin(url) {
  const parsed = new URL(url);
  if (parsed.hostname !== '127.0.0.1' || parsed.protocol !== 'http:' || parsed.pathname !== '/' ||
      parsed.search || parsed.hash || parsed.username || parsed.password) throw Error('invalid slot origin');
  return parsed.origin;
}

export function allowedNaturalRequest(text) {
  return typeof text === 'string' && text.trim().length > 0 && text.length <= 6000 &&
    !/(?:https?:\/\/|ALIYUN::|alicloud_|\b(?:i|vpc|vsw|sg|r|rm)-[a-z0-9]{6,})/i.test(text);
}

export function allowedOriginalReadContinuation(text) {
  // Only Runtime's provenance-checked continuation uses this separate operation.
  // Initial model-supplied natural requests keep the stricter ID-free validator.
  if (allowedNaturalRequest(text)) return true;
  if (typeof text !== 'string' || text.length > 24000) return false;
  const split=text.lastIndexOf('\n');
  if (split<0 || !allowedNaturalRequest(text.slice(0,split))) return false;
  let delivery;
  try { delivery=JSON.parse(text.slice(split+1)); } catch { return false; }
  if (!delivery || Object.keys(delivery).sort().join(',')!=='absence_verified,allowance_changed,calls,kind' ||
      delivery.kind!=='original_missing_cleanup_reads' || delivery.absence_verified!==false ||
      delivery.allowance_changed!==false || !Array.isArray(delivery.calls) ||
      delivery.calls.length<1 || delivery.calls.length>32) return false;
  const contracts={
    'ros/GetStack/2019-09-10':['OutputOption','RegionId','StackId'],
    'mse/QueryClusterInfo/2019-05-31':['InstanceId','RegionId'],
    'vpc/DescribeVpcAttribute/2016-04-28':['RegionId','VpcId'],
    'vpc/DescribeVSwitchAttributes/2016-04-28':['RegionId','VSwitchId'],
    'mse/ListClusters/2019-05-31':['ClusterAliasName','PageNum','PageSize','RegionId'],
  };
  const ids=new Set();
  return delivery.calls.every(entry=>{
    if (!entry || Object.keys(entry).sort().join(',')!=='call,id,read_policy,remaining' ||
        typeof entry.id!=='string' || !/^[a-zA-Z0-9-]{1,128}$/.test(entry.id) || ids.has(entry.id) ||
        !Number.isInteger(entry.remaining) || entry.remaining<1 || entry.remaining>10000 ||
        !entry.read_policy || entry.read_policy.phase!=='cleanup' || entry.read_policy.required_cleanup!==true ||
        typeof entry.read_policy.after_delete!=='string' || !entry.read_policy.after_delete) return false;
    ids.add(entry.id);
    const call=entry.call;
    if (!call || Object.keys(call).sort().join(',')!=='action,params,product,region_id,version' ||
        call.region_id!=='cn-hangzhou' || !call.params || call.params.RegionId!==call.region_id) return false;
    const keys=contracts[`${call.product}/${call.action}/${call.version}`];
    return !!keys && Object.keys(call.params).sort().join(',')===keys.slice().sort().join(',') &&
      Object.values(call.params).every(value=>typeof value==='string' && value.length>0 && value.length<=256 &&
        !/https?:\/\/|ALIYUN::|alicloud_|\s/i.test(value) || Number.isInteger(value) && value>0 && value<=100);
  });
}

export function ownsTarget(target, origin, targetId) {
  return target.targetId === targetId && new URL(target.url).origin === bareOrigin(origin);
}

function playwright() {
  const require = createRequire(import.meta.url);
  if (process.env.RESOURCE_LINK_PLAYWRIGHT_ROOT) {
    return require(path.join(process.env.RESOURCE_LINK_PLAYWRIGHT_ROOT, 'playwright'));
  }
  return require('playwright');
}

async function targetId(page, context) {
  const session = await context.newCDPSession(page);
  try { return (await session.send('Target.getTargetInfo')).targetInfo.targetId; }
  finally { await session.detach(); }
}

async function slotPage(browser, request) {
  const matches = [];
  for (const context of browser.contexts()) {
    for (const page of context.pages()) {
      if (page.isClosed()) continue;
      if (await targetId(page, context) !== request.target) continue;
      if (new URL(page.url()).origin !== bareOrigin(request.origin)) throw Error('slot page origin changed');
      matches.push({page, context});
    }
  }
  if (matches.length !== 1) throw Error('exact leased target missing or ambiguous; do not select current tab');
  return matches[0];
}

function outputFile(root, name) {
  if (!/^[a-zA-Z0-9_-]+\.(png|json)$/.test(name)) throw Error('invalid artifact filename');
  fs.mkdirSync(root, {recursive:true});
  return path.join(root, name);
}

async function snapshot(page) {
  return page.evaluate(() => ({
    app_ready: !!document.querySelector('#iac-code-web-root'),
    unlocked: !!document.querySelector('#iac-code-web-root') && !document.querySelector('.token-access-gate'),
    session_id: document.querySelector('.session-item.is-active[data-session-id]')?.getAttribute('data-session-id'),
    busy: !!document.querySelector('[data-app-shell="composer-send"].is-stopping'),
    preview_open: !!document.querySelector('.resource-link-preview-backdrop'),
    text: document.body.innerText.slice(-24000),
    controls: [...document.querySelectorAll('button,textarea,input')].map(el => ({
      tag:el.tagName, id:el.id, label:el.getAttribute('aria-label') || el.textContent?.trim().slice(0,100),
      placeholder:el.getAttribute('placeholder')})).slice(0,80),
    links: [...document.querySelectorAll('[data-resource-link]')].map(el => ({
      focus:el.getAttribute('data-resource-focus'), label:el.getAttribute('aria-label'),
      text:el.textContent?.trim().slice(0,240)})).slice(0,80)
  }));
}

export async function refreshOriginalSession(page, expectedSession, options={}) {
  // OPS maintenance, not a model operation. A retained DOM can outlive its
  // restarted SSE producer and appear idle while new messages exist on disk.
  // Reload this exact page, then select only the recorded existing sidebar key.
  if (expectedSession !== null && (typeof expectedSession !== 'string' ||
      !/^ws~[A-Za-z0-9_-]+$/.test(expectedSession))) throw Error('invalid original session binding');
  const before = await snapshot(page);
  if (!before.unlocked || before.busy || (before.session_id ?? null) !== expectedSession)
    throw Error('original session not idle and unlocked');
  return withForegroundLock(options.lockFile, async()=>{
    await page.reload({waitUntil:'domcontentloaded',timeout:15000});
    await page.locator('#iac-code-web-root').waitFor({state:'attached',timeout:15000});
    const loaded = await snapshot(page);
    if (!loaded.unlocked || loaded.busy ||
        loaded.session_id && loaded.session_id !== expectedSession)
      throw Error('original session changed during refresh');
    if (expectedSession && loaded.session_id !== expectedSession) {
      const existing = page.locator(`.session-item[data-session-id="${expectedSession}"]`);
      await existing.waitFor({state:'visible',timeout:15000});
      if (await existing.count() !== 1) throw Error('original stored conversation missing or ambiguous');
      await existing.click({timeout:10000});
      await page.waitForFunction(expected =>
        document.querySelector('.session-item.is-active[data-session-id]')?.getAttribute('data-session-id') === expected,
      expectedSession, {timeout:15000});
    }
    const after = await snapshot(page);
    if (!after.unlocked || after.busy || (after.session_id ?? null) !== expectedSession)
      throw Error('original session changed during refresh');
    return {...after, original_page_reloaded:true, existing_session_restored:true,
      cloud_calls:0,new_session_created:false};
  });
}

export async function resolvePreviewConsoleFrame(page, origin, sessionId) {
  if(new URL(page.url()).origin!==bareOrigin(origin))throw Error('slot page origin changed');
  const active=page.locator('.session-item.is-active[data-session-id]');
  if(!sessionId||await active.count()!==1||await active.getAttribute('data-session-id')!==sessionId)
    throw Error('complete active session changed during preview binding');
  const preview=page.locator('.resource-link-preview-body iframe');
  const count=await preview.count();
  if(count>1)throw Error('preview console embedding ambiguous');
  if(!count||!await preview.isVisible())return null;
  let embedding;
  try {
    embedding=await preview.elementHandle();
    return await embedding?.contentFrame() || null;
  } catch {
    // Transient detach is an observation gap; the next bounded read resolves
    // this same embedding again. Session/origin errors above are never caught.
    return null;
  } finally {if(embedding)await embedding.dispose();}
}

export async function operate(browser, request) {
  if (request.operation === 'bootstrap') {
    // OPS/environment only. This is the sole navigation permission, to the slot root with a private token.
    const origin = bareOrigin(request.origin);
    const context = browser.contexts()[0];
    if (!context) throw Error('dedicated persistent Chrome context required');
    const candidates = context.pages().filter(page => {
      try { return new URL(page.url()).origin === origin; } catch { return false; }
    });
    if (candidates.length > 1) throw Error('multiple slot root pages; OPS must reconcile');
    const page = candidates[0] || await context.newPage();
    const stat = fs.lstatSync(request.token_file);
    if (!stat.isFile() || (stat.mode & 0o777) !== 0o600) throw Error('private token permissions invalid');
    const token = fs.readFileSync(request.token_file, 'utf8').trim();
    await page.goto(`${origin}/#web_token=${token}`);
    // On an already-open root this can be fragment-only navigation: the token
    // transport runs at document startup, so force that startup without exposing
    // the private URL or selecting another worker's tab.
    if (await page.evaluate(() => location.hash.includes('web_token='))) await page.reload();
    await page.locator('#iac-code-web-root').waitFor({state:'attached',timeout:20000});
    await page.waitForFunction(() => !location.hash.includes('web_token='));
    return {target:await targetId(page,context), origin};
  }
  const {page, context} = await slotPage(browser, request);
  if (request.operation === 'snapshot') return snapshot(page);
  if (request.operation === 'refresh-session')
    return refreshOriginalSession(page, request.session_id ?? null, {lockFile:request.foreground_lock});
  if (request.operation === 'close-preview') {
    // Fixed product-owned close control, not an arbitrary model-supplied selector.
    const current = await snapshot(page);
    if (!current.session_id || current.session_id !== request.session_id) throw Error('session mismatch');
    const close = page.locator('.resource-link-preview-actions > button');
    if (await close.count() !== 1 || !await close.isVisible()) throw Error('unique preview close control missing');
    await withForegroundLock(request.foreground_lock,()=>close.click());
    await page.locator('.resource-link-preview-backdrop').waitFor({state:'detached',timeout:5000});
    return {closed_preview:true,session_id:current.session_id,target:request.target};
  }
  if (request.operation === 'source') {
    const current = await snapshot(page);
    if (!current.session_id || current.session_id !== request.session_id) throw Error('source session mismatch');
    if (current.busy) throw Error('answer still running');
    const source = await page.evaluate(async session => {
      // Reuse product transport, including encrypted token mode. Never return the token.
      const {tokenFetch} = await import('/static/js/token_transport.js?v=1');
      const root = '/api/sessions/' + encodeURIComponent(session);
      const messages = await tokenFetch(root + '/messages');
      const references = await tokenFetch(root + '/resource-references?limit=500');
      if (!messages.ok || !references.ok) throw Error('product source unavailable');
      return {messages:await messages.json(), references:await references.json()};
    },current.session_id);
    const file = outputFile(request.output_dir, `${request.artifact_id}.json`);
    fs.writeFileSync(file,JSON.stringify({session_id:current.session_id,...source}));
    return {source_evidence:file, session_id:current.session_id};
  }
  if (request.operation === 'new-session') {
    // Locator is frozen in the Sol-approved packet, not chosen via arbitrary JS by GLM.
    // The product materializes a draft only on its first UI send. Keep cloud capabilities
    // closed while the worker creates that session; never create one via a hidden API.
    await withForegroundLock(request.foreground_lock,async()=>{
      // A completed worker may leave its preview over the sidebar. Recover
      // only this leased page's idle, fully identified old session before
      // clicking the real new-session control; never create through an API.
      const before = await snapshot(page);
      if (before.busy || before.unlocked === false) throw Error('slot is not idle and unlocked');
      if (before.preview_open) {
        if (!before.session_id) throw Error('preview has no complete active session');
        const close = page.locator('.resource-link-preview-actions > button');
        if (await close.count() !== 1 || !await close.isVisible())
          throw Error('unique preview close control missing before new session');
        await close.click();
        await page.locator('.resource-link-preview-backdrop').waitFor({state:'detached',timeout:5000});
        const restored = await snapshot(page);
        if (restored.session_id !== before.session_id || restored.busy || restored.preview_open
          || restored.unlocked === false) throw Error('old session changed while closing its preview');
      }
      await page.locator(request.selector).click({timeout:10000});
      await page.locator(request.input_selector).fill('这是资源链接测试对话。请先不要调用任何工具，不查询、创建、修改或删除资源，等待我的下一条具体请求。');
      await page.locator(request.send_selector).click();
    });
    await page.waitForFunction(() => !!document.querySelector('.session-item.is-active[data-session-id]')?.getAttribute('data-session-id'), null, {timeout:20000});
    return snapshot(page);
  }
  if (request.operation === 'request' || request.operation === 'original-read-continuation') {
    const valid=request.operation==='request' ? allowedNaturalRequest(request.text) :
      allowedOriginalReadContinuation(request.text);
    if (!valid) throw Error('request includes resource IDs/types/URLs or is invalid');
    if (await page.locator(request.busy_selector).isVisible().catch(() => false)) throw Error('answer still running');
    await withForegroundLock(request.foreground_lock,async()=>{
      await page.locator(request.input_selector).fill(request.text);
      await page.locator(request.send_selector).click();
    });
    return {submitted:true, target:request.target};
  }
  if (request.operation === 'native-identity-recovery') {
    const exactText='OPS已独立批准一次原会话身份续核。只执行机器契约中的 STS GetCallerIdentity，等待真实账号结果后结束。禁止同时查询资源、创建或删除。';
    if (!request.session_id || request.text !== exactText) throw Error('exact OPS identity-only request required');
    const assertOriginalIdle=async()=>{
      const current=await snapshot(page);
      if(current.session_id!==request.session_id||current.busy||!current.unlocked||current.preview_open)
        throw Error('original unlocked idle Web session required');
      if(await page.locator(request.busy_selector).isVisible().catch(()=>false))
        throw Error('answer still running');
      return current;
    };
    await assertOriginalIdle();
    return await withForegroundLock(request.foreground_lock,async()=>{
      const current=await assertOriginalIdle();
      const input=page.locator(request.input_selector),send=page.locator(request.send_selector);
      if(await input.count()!==1||!await input.isVisible()||await send.count()!==1||!await send.isVisible())
        throw Error('unique visible composer controls required');
      await input.fill(exactText);
      const beforeSend=await assertOriginalIdle();
      if(beforeSend.session_id!==current.session_id)throw Error('original session changed before send');
      await send.click();
      return {submitted:true,session_id:current.session_id,target:request.target};
    });
  }
  if (request.operation === 'click') {
    const current = await snapshot(page);
    if (!current.session_id || current.session_id !== request.session_id) throw Error('complete active session key mismatch');
    if (await page.locator(request.busy_selector).isVisible().catch(() => false)) throw Error('answer still running');
    const sourceCard=await withForegroundLock(request.foreground_lock,()=>revealOriginalSourceCard(page,request));
    if(request.source_card_tool_use_id!==undefined){
      const afterReveal=await snapshot(page);
      if(afterReveal.session_id!==current.session_id || afterReveal.busy || !afterReveal.unlocked)
        throw Error('session mismatch');
    }
    const links = page.locator(request.selector);
    const candidates = [];
    for (let index = 0; index < await links.count(); index++) {
      const element = links.nth(index);
      if (await element.isVisible()) candidates.push({index,href:await element.getAttribute('href'),
        focus:await element.getAttribute('data-resource-focus')});
    }
    const occurrenceFile = outputFile(request.output_dir,'occurrence-'+createHash('sha256')
      .update(current.session_id+'\n'+request.selector).digest('hex')+'.json');
    const chosen = selectOccurrence(candidates,fs.existsSync(occurrenceFile)?JSON.parse(fs.readFileSync(occurrenceFile,'utf8')):
      request.recovery_occurrence ?? null);
    if (!fs.existsSync(occurrenceFile)) fs.writeFileSync(occurrenceFile,JSON.stringify(chosen));
    const link = links.nth(chosen.index);
    const popup = await withForegroundLock(request.foreground_lock,async()=>{
      const popupPromise = page.waitForEvent('popup', {timeout:2000}).catch(() => null);
      await link.click(request.mode === 'open' ? {modifiers:['Shift']} : {});
      return popupPromise;
    });
    // This popup event is scoped to the leased page, including rel=noopener product links.
    // Ordinary click may spawn a federation sign-in helper. Its pixels are not iframe evidence.
    const surface = request.mode === 'open' ? (popup || page) : page;
    await withForegroundLock(request.foreground_lock,()=>surface.bringToFront());
    await surface.waitForLoadState('domcontentloaded', {timeout:15000}).catch(() => {});
    let frames = surface.frames().filter(frame => frame !== surface.mainFrame());
    for (const frame of frames) await frame.waitForLoadState('domcontentloaded', {timeout:15000}).catch(() => {});
    let consoleFrame=null;
    const resolveConsoleFrame=async()=>{
      consoleFrame=request.mode==='open' ? surface.mainFrame() :
        await resolvePreviewConsoleFrame(page,request.origin,current.session_id);
      return consoleFrame;
    };
    const boundConsoleFrames=async()=>{const frame=await resolveConsoleFrame();return frame ? [frame] : [];};
    const renderCheck=await waitForNavigationIdentity(surface,request.mode,request.render_identifier,20000,request.render_text || [],
      boundConsoleFrames);
    frames=surface.frames().filter(frame=>frame!==surface.mainFrame());
    // Capture honest current pixels, not automatic PASS. Sol decides readiness/identity from the original.
    const file = outputFile(request.output_dir, `${request.artifact_id}.png`);
    const capture=await captureNavigationPixels(surface,file,{lockFile:request.foreground_lock,resolveConsoleFrame,
      mode:request.mode,identifier:request.render_identifier,markers:request.render_text||[],
      beforeCapture:async()=>{const live=await snapshot(page);if(live.session_id!==current.session_id)throw Error('session mismatch');}});
    const result = {actual_ui_click:true, session_id:current.session_id,
      source_card:sourceCard,
      target:request.target, actual_navigation:request.mode === 'open' && popup ? 'open' : 'iframe_or_none',
      final_url:surface.url(), text:(await surface.locator('body').innerText()).slice(-24000),
      screenshot:file, capture_mode:'foreground_viewport', mode:request.mode,
      clicked_occurrence:chosen, matching_occurrences:candidates.length,
      frames:frames.map(frame=>({url:frame.url()})), render_check:capture.render_check,
      initial_render_check:renderCheck,capture_sync:capture,captured_at:new Date().toISOString()};
    if(request.console_inspection){
      try {
        const inspector=request.console_inspection.kind==='ecs_keypair_metadata'?
          (await import('./console_keypair.mjs')).inspectEcsKeyPairMetadata:
          request.console_inspection.kind==='rocketmq_instance_notice'?
          (await import('./console_rocketmq.mjs')).inspectRocketmqNotice:
          request.console_inspection.kind==='sae_application_metadata'?
          (await import('./console_sae_application.mjs')).inspectSaeApplicationMetadata:
          request.console_inspection.kind==='ots_vpc_binding_row'?
          (await import('./console_ots_vpc_binding.mjs')).inspectOtsVpcBindingRow:
          request.console_inspection.kind==='nas_fileset_description'?
          (await import('./console_nas_fileset.mjs')).inspectNasFilesetDescription:
          request.console_inspection.kind==='nas_dataflow_metadata'?
          (await import('./console_nas_dataflow.mjs')).inspectNasDataFlowMetadata:
          request.console_inspection.kind==='nas_protocol_service_identity'?
          (await import('./console_nas_protocol_service.mjs')).inspectNasProtocolServiceIdentity:
          request.console_inspection.kind==='sls_project_metadata'?
          (await import('./console_sls_project.mjs')).inspectSlsProjectMetadata:
          request.console_inspection.kind==='nas_mount_target_address'?
          (await import('./console_nas_mount_target.mjs')).inspectNasMountTargetAddress:
          request.console_inspection.kind==='ots_instance_endpoint'?
          (await import('./console_ots_instance.mjs')).inspectOtsInstanceEndpoint:
          request.console_inspection.kind==='nas_protocol_mount_target_child'?
          (await import('./console_nas_export.mjs')).inspectNasExport:
          request.console_inspection.kind==='resource_group_metadata'?
          (await import('./console_resource_group.mjs')).inspectResourceGroupMetadata:
          request.console_inspection.kind==='router_interface_vbr_child'?
          (await import('./console_router_interface.mjs')).inspectRouterInterfaceChild:
          request.console_inspection.kind==='ess_scheduled_task_metadata'?
          (await import('./console_ess_scheduled_task.mjs')).inspectEssScheduledTaskMetadata:
          request.console_inspection.kind==='oos_state_configuration_list'?
          (await import('./console_oos_state_configuration.mjs')).inspectOosStateConfigurationList:
          request.console_inspection.kind==='oos_template_metadata'?
          (await import('./console_oos_template.mjs')).inspectOosTemplateMetadata:
          request.console_inspection.kind==='slb_acl_metadata'?inspectSlbAclMetadata:inspectNasMetadata;
        result.console_inspection=await inspector(surface,await resolveConsoleFrame(),request.console_inspection,
          outputFile(request.output_dir,`${request.artifact_id}-metadata.png`),
          {lockFile:request.foreground_lock,mode:request.mode,beforeCapture:async()=>{
            const live=await snapshot(page);if(live.session_id!==current.session_id)throw Error('session mismatch');
          }});
      }catch{
        result.console_inspection={kind:request.console_inspection.kind,identity_visible:false,
          reason:'bounded source-bound read-only metadata inspection failed; original landing preserved'};
      }
    }
    if (popup && request.mode === 'open') await popup.close();
    return result;
  }
  if (request.operation === 'evidence') {
    const current = await snapshot(page);
    if (!current.session_id || current.session_id !== request.session_id) throw Error('session mismatch');
    const file = outputFile(request.output_dir, `${request.artifact_id}.png`);
    await page.screenshot({path:file, fullPage:true});
    return {...current, actual_ui_click:false, target:request.target, screenshot:file, final_url:page.url()};
  }
  throw Error('operation not allowed');
}

export async function cdpConnection(request, fetchVersion = fetch) {
  if (!request.remote_cdp) {
    if (request.cdp_endpoint !== 'http://127.0.0.1:9225') throw Error('dedicated local CDP required');
    return request.cdp_endpoint;
  }
  if (request.cdp_endpoint !== 'http://127.0.0.1:9226') throw Error('owned remote CDP forward required');
  const response = await fetchVersion(request.cdp_endpoint + '/json/version', {signal:AbortSignal.timeout(8000)});
  if (!response.ok) throw Error('remote CDP version unavailable');
  const info = await response.json();
  const url = new URL(info.webSocketDebuggerUrl);
  if (url.protocol !== 'ws:' || url.hostname !== '127.0.0.1' || !['9225','9226'].includes(url.port) ||
      url.username || url.password || url.search || url.hash ||
      !/^\/devtools\/browser\/[a-f0-9-]+$/i.test(url.pathname)) throw Error('remote CDP identity unavailable');
  // Chrome may advertise its own9225 or the forwarded HTTP Host9226. Never connect9225 unchanged: it would
  // reach the LOCAL Chrome and silently drive another machine's targets.
  url.port = '9226';
  return url.href;
}

async function main() {
  let body = '';
  for await (const part of process.stdin) body += part;
  const request = JSON.parse(body);
  const browser = await playwright().chromium.connectOverCDP(await cdpConnection(request), {timeout:8000});
  try { process.stdout.write(JSON.stringify(await operate(browser, request))); }
  finally { /* Do not call browser.close(): another leased page may be running. */ }
}

if (process.argv[1] && pathToFileURL(process.argv[1]).href === import.meta.url) {
  main().then(() => process.exit(0)).catch(error => {
    // Forward only fixed literal validation errors, never Playwright/private URLs.
    const safe = new Set(['source session mismatch','answer still running','session mismatch',
      'complete active session key mismatch','precise reference destination ambiguous or pending',
      'bound product occurrence changed']);
    if (safe.has(error.message)) process.stdout.write(JSON.stringify({workflow_error:error.message}));
    process.stderr.write('scoped browser operation failed (private URL details suppressed)'); process.exit(1);
  });
}
