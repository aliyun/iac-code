/** Source-bound ECS KeyPair read-only UI sampling; never creates a resource or URL. */
import {withForegroundLock,captureNavigationPixels,LOADING_SELECTOR} from './browser.mjs';

export function keyPairConsoleUrl(raw, expected) {
  let url;try{url=new URL(raw);}catch{throw Error('unbound ECS KeyPair console');}
  const route='/keyPair/region/'+expected.region;
  if(url.protocol!=='https:'||!['ecs.console.aliyun.com','ecs4service.console.aliyun.com'].includes(url.hostname)||
      url.username||url.password||[...url.searchParams.keys()].some(key=>key!=='spm')||
      !(url.pathname===route&&!url.hash||url.pathname==='/'&&url.hash==='#'+route))
    throw Error('unbound ECS KeyPair console');
  return url;
}

export function keyPairMetadataReady(view, expected) {
  return view.loading===false&&view.exact_row===true&&[expected.identifier,expected.fingerprint]
    .every(marker=>view.fields?.some(field=>field.marker===marker&&field.visible===true));
}

export function keyPairHorizontalDelta(view, expected) {
  const field=view.fields?.find(item=>item.marker===expected.fingerprint),gap=view.scroll;
  if(!field||!gap||![field.left,field.right,gap.left,gap.right,gap.current,gap.maximum].every(Number.isFinite)||
      field.right<=field.left||gap.right-gap.left<field.right-field.left+4)
    throw Error('ECS KeyPair fingerprint has no unoccluded viewport');
  const delta=(field.left+field.right-gap.left-gap.right)/2;
  if(gap.current+delta<0||gap.current+delta>gap.maximum)throw Error('ECS KeyPair fingerprint scroll unavailable');
  return delta;
}

export async function inspectEcsKeyPairMetadata(surface, frame, expected, file, options={}) {
  if(!frame||expected.kind!=='ecs_keypair_metadata'||
      !/^[A-Za-z][A-Za-z0-9._-]{0,127}$/.test(expected.identifier)||
      !/^[a-fA-F0-9]{32}$/.test(expected.fingerprint)||!/^cn-[a-z0-9-]+$/.test(expected.region))
    throw Error('invalid ECS KeyPair inspection');
  const location=()=>frame.evaluate(()=>document.location.href);
  const initial=keyPairConsoleUrl(await location(),expected);
  return withForegroundLock(options.lockFile,async()=>{
    await surface.bringToFront();
    if(options.beforeCapture)await options.beforeCapture();
    const assertConsole=async()=>{
      const actual=keyPairConsoleUrl(await location(),expected);
      if(actual.origin!==initial.origin)throw Error('ECS KeyPair console origin changed');
      return actual.href;
    };
    const stages=[{step:'resource_link_landing',url:initial.href}];
    // Fixed real console controls observed by OPS. The only input is an exact
    // source name in the native read-only search; no bind/unbind/delete/creation.
    const search=frame.getByPlaceholder('请输入密钥对名称',{exact:true});
    await search.waitFor({state:'visible',timeout:10000});
    if(await search.count()!==1||!await search.isVisible())throw Error('ECS KeyPair search ambiguous');
    const mode=frame.locator('button').filter({has:frame.getByText('密钥对名称',{exact:true})});
    await mode.waitFor({state:'visible',timeout:10000});
    if(await mode.count()!==1||!await mode.isVisible())throw Error('ECS KeyPair exact name search mode required');
    await assertConsole();
    await search.fill(expected.identifier);await search.press('Enter');
    const row=frame.locator('tr[data-row-key]').filter({has:frame.getByText(expected.identifier,{exact:true})});
    await row.waitFor({state:'visible',timeout:10000});
    if(await row.count()!==1||await row.getAttribute('data-row-key')!==expected.identifier)
      throw Error('ECS KeyPair exact row ambiguous or not source-bound');
    for(const marker of [expected.identifier,expected.fingerprint])
      if(await row.getByText(marker,{exact:true}).count()!==1)throw Error('ECS KeyPair source field missing or ambiguous');
    stages.push({step:'native_exact_name_search',url:await assertConsole(),name:expected.identifier});
    const readView=()=>row.evaluate((el,{markers,selector})=>{
      const fields=markers.map(marker=>{
        const walker=document.createTreeWalker(el,NodeFilter.SHOW_TEXT);let node;
        while(walker.nextNode())if(walker.currentNode.textContent.trim()===marker){node=walker.currentNode;break;}
        if(!node)return {marker,visible:false};
        const range=document.createRange();range.selectNodeContents(node);const rect=range.getBoundingClientRect();
        let opacity=1,hidden=false,clipped=false;
        for(let parent=node.parentElement;parent;parent=parent.parentElement){
          const style=getComputedStyle(parent),box=parent.getBoundingClientRect();opacity*=Number(style.opacity);
          hidden ||= style.display==='none'||style.visibility==='hidden';
          if(['hidden','clip','auto','scroll'].includes(style.overflowX))clipped ||= rect.left<box.left||rect.right>box.right;
          if(['hidden','clip','auto','scroll'].includes(style.overflowY))clipped ||= rect.top<box.top||rect.bottom>box.bottom;
        }
        // A wrapped fingerprint has real glyph rectangles on each line; its
        // bounding-box midpoint can be blank line spacing. Test every painted
        // fragment, never accept merely a matching bounding-box center.
        const fragments=[...range.getClientRects()].filter(r=>r.width>0&&r.height>0);
        const hit=fragments.length>0&&fragments.every(fragment=>[0.01,0.25,0.5,0.75,0.99].every(fraction=>{
          const target=document.elementFromPoint(fragment.left+fragment.width*fraction,(fragment.top+fragment.bottom)/2);
          return target&&(target===node.parentElement||node.parentElement.contains(target));
        }));
        return {marker,left:rect.left,right:rect.right,top:rect.top,bottom:rect.bottom,
          visible:rect.width>0&&rect.height>0&&rect.left>=0&&rect.right<=innerWidth&&rect.top>=0&&
            rect.bottom<=innerHeight&&hit&&!hidden&&!clipped&&opacity>=0.8};
      });
      let container=el.parentElement;
      while(container&&!(container.scrollWidth>container.clientWidth&&container.clientWidth>0))container=container.parentElement;
      let scroll=null;
      if(container){const box=container.getBoundingClientRect();let left=Math.max(0,box.left),right=Math.min(innerWidth,box.right);
        for(const cell of el.querySelectorAll('td')){const style=getComputedStyle(cell),r=cell.getBoundingClientRect();
          if(style.position!=='sticky')continue;
          if(style.left!=='auto')left=Math.max(left,r.right);
          if(style.right!=='auto')right=Math.min(right,r.left);}
        scroll={left,right,current:container.scrollLeft,maximum:container.scrollWidth-container.clientWidth};}
      return {exact_row:el.getAttribute('data-row-key')===markers[0],fields,scroll,
        loading:[...document.querySelectorAll(selector)].some(n=>{const s=getComputedStyle(n);
          return n.getClientRects().length&&s.display!=='none'&&s.visibility!=='hidden'&&Number(s.opacity)>0.01;})};
    },{markers:[expected.identifier,expected.fingerprint],selector:LOADING_SELECTOR});
    let view=await readView(),collapsed=false,restore,scrollError=null;
    try{
      const fingerprint=view.fields.find(field=>field.marker===expected.fingerprint);
      if(fingerprint&&!fingerprint.visible&&view.scroll){
        try{keyPairHorizontalDelta(view,expected);}catch{
          // Only an actual native ECS nav toggle; keep honest pixels if the
          // source-bound field still does not fit. Never hide DOM or resize.
          const toggle=frame.locator('.app-layout-nav-collapse-trigger-icon');
          if(await toggle.count()===1&&await toggle.isVisible()){
            const width=view.scroll.right-view.scroll.left;
            await toggle.click({timeout:5000});collapsed=true;restore=toggle;
            await frame.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
            view=await readView();
            if(!view.scroll||view.scroll.right-view.scroll.left<=width){
              await restore.click({timeout:5000});collapsed=false;view=await readView();
            }else stages.push({step:'actual_native_nav_collapse'});
          }
        }
        if(!keyPairMetadataReady(view,expected)){
          try{
            const delta=keyPairHorizontalDelta(view,expected);
            await row.hover();await surface.mouse.wheel(delta,0);
            await frame.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
            stages.push({step:'native_horizontal_scroll',delta});
          }catch(error){scrollError=error.message;}
        }
      }
      const capture=await captureNavigationPixels(surface,file,{consoleFrame:frame,mode:options.mode,
        identifier:expected.identifier,markers:[expected.fingerprint],beforeCapture:async()=>{
          if(options.beforeCapture)await options.beforeCapture();await assertConsole();
          if(await row.count()!==1||await row.getAttribute('data-row-key')!==expected.identifier)
            throw Error('ECS KeyPair row changed before capture');
          view=await readView();
        }});
      stages.push({step:'readonly_same_row_full_fingerprint',url:await assertConsole()});
      return {kind:expected.kind,identity_visible:keyPairMetadataReady(view,expected)&&capture.foreground_visible===true&&
          (options.mode!=='iframe'||capture.frame_embedding?.visible===true),fields:view,screenshot:file,
        capture_sync:capture,stages,sampling_error:scrollError,readonly_completed:true,
        original_landing_unchanged:true,cloud_writes:0};
    }finally{
      if(collapsed){if(await restore.count()!==1||!await restore.isVisible())throw Error('ECS native nav restoration unavailable');
        await restore.click({timeout:5000});}
    }
  });
}
