/** Source-bound VBR child lookup, preserving the original landing pixels. */
import {routerInterfaceLanding,vbrParentRouteMatches} from '../router_interface_landing.mjs';
import {withForegroundLock,captureNavigationPixels} from './browser.mjs';

export function routerInterfaceReference(expected) {
  if(expected.kind!=='router_interface_vbr_child'||
      !['ALIYUN::VPC::RouterInterface','alicloud_router_interface'].includes(expected.resource_type)||
      !/^ri-[a-z0-9]+$/.test(expected.identifier)||!/^vbr-[a-z0-9]+$/.test(expected.parent_router_id)||
      !/^vpc-[a-z0-9]+$/.test(expected.peer_vpc)||!/^cn-[a-z0-9-]+$/.test(expected.region)||
      typeof expected.name!=='string'||!expected.name.trim()||!expected.source_tool_use_id)
    throw Error('invalid source-bound RouterInterface child inspection');
  return {resourceType:expected.resource_type,identifier:expected.identifier,regionId:expected.region,
    attributes:{RouterId:expected.parent_router_id,RouterType:'VBR'}};
}

export function routerInterfaceChildReady(view, expected) {
  return view.tab_selected===true&&view.same_child_cell===true&&view.loading===false&&
    [expected.identifier,expected.parent_router_id,expected.name,expected.peer_vpc]
      .every(marker=>view.fields?.some(field=>field.marker===marker&&field.visible===true));
}

export function routerInterfaceHorizontalDelta(view) {
  const fields=view.fields||[],scroll=view.scroll;
  if(!scroll||!fields.length||![scroll.left,scroll.right,scroll.current,scroll.maximum].every(Number.isFinite)||
      scroll.right<=scroll.left||fields.some(field=>!Number.isFinite(field.left)||!Number.isFinite(field.right)||
        field.right<=field.left))
    throw Error('RouterInterface fields or native scroll unavailable');
  const left=Math.min(...fields.map(field=>field.left)),right=Math.max(...fields.map(field=>field.right));
  if(right-left>scroll.right-scroll.left-4)throw Error('RouterInterface row viewport too narrow');
  const delta=right>scroll.right-2?right-scroll.right+2:left<scroll.left+2?left-scroll.left-2:0;
  if(scroll.current+delta<0||scroll.current+delta>scroll.maximum)
    throw Error('RouterInterface native horizontal scroll out of range');
  return delta;
}

export function routerInterfaceRowView(el,{markers}) {
  const fields=markers.map(marker=>{
    const walker=document.createTreeWalker(el,NodeFilter.SHOW_TEXT),nodes=[];
    while(walker.nextNode())if(walker.currentNode.textContent.trim()===marker)nodes.push(walker.currentNode);
    if(nodes.length!==1)return {marker,visible:false};
    const node=nodes[0],range=document.createRange();range.selectNodeContents(node);
    const rect=range.getBoundingClientRect(),pieces=[...range.getClientRects()].filter(r=>r.width>0&&r.height>0);
    let opacity=1,hidden=false,clipped=false;
    for(let parent=node.parentElement;parent;parent=parent.parentElement){
      const style=getComputedStyle(parent),box=parent.getBoundingClientRect();opacity*=Number(style.opacity);
      hidden ||= style.display==='none'||style.visibility==='hidden';
      for(const r of pieces){
        if(['hidden','clip','auto','scroll'].includes(style.overflowX))clipped ||= r.left<box.left||r.right>box.right;
        if(['hidden','clip','auto','scroll'].includes(style.overflowY))clipped ||= r.top<box.top||r.bottom>box.bottom;
      }
    }
    const visible=pieces.length>0&&!hidden&&!clipped&&opacity>=0.8&&pieces.every(r=>
      r.left>=0&&r.right<=innerWidth&&r.top>=0&&r.bottom<=innerHeight&&[0.01,0.5,0.99].every(x=>{
        const hit=document.elementFromPoint(r.left+r.width*x,(r.top+r.bottom)/2);
        return hit&&(hit===node.parentElement||node.parentElement.contains(hit));
      }));
    return {marker,left:rect.left,right:rect.right,top:rect.top,bottom:rect.bottom,visible};
  });
  let container=el.parentElement;
  while(container&&!(container.scrollWidth>container.clientWidth&&container.clientWidth>0))container=container.parentElement;
  let scroll=null;
  if(container){
    const box=container.getBoundingClientRect(),a=Math.max(0,box.left),b=Math.min(innerWidth,box.right);
    const ys=fields.filter(f=>Number.isFinite(f.top)).map(f=>(f.top+f.bottom)/2);
      // Sticky cells may belong to this very TR. Read the painted scrolling
      // gap, excluding both sticky edges and their hidden underlying cells.
    let start=null,best={left:a,right:a};
    for(let x=a+2;x<b;x+=2){
      const painted=ys.length>0&&ys.every(y=>{
          const hit=document.elementFromPoint(x,y),cell=hit?.closest('td');
          return hit&&(hit===el||el.contains(hit))&&cell&&
            !['sticky','fixed'].includes(getComputedStyle(cell).position);
        });
      if(painted&&start===null)start=x;
      if((!painted||x+2>=b)&&start!==null){
        const end=painted?x: x-2;if(end-start>best.right-best.left)best={left:start,right:end};start=null;
      }
    }
    scroll={...best,current:container.scrollLeft,maximum:container.scrollWidth-container.clientWidth};
  }
  const childCells=[...el.querySelectorAll('td')].filter(cell=>{
    const lines=cell.innerText.split('\n').map(s=>s.trim());return lines.includes(markers[0])&&lines.includes(markers[1]);
  });
  const rect=el.getBoundingClientRect();
  return {fields,scroll,same_child_cell:childCells.length===1,row_rect:{left:rect.left,top:rect.top}};

}

export async function inspectRouterInterfaceChild(surface, frame, expected, file, options={}) {
  const reference=routerInterfaceReference(expected);
  if(!frame)throw Error('RouterInterface console frame unavailable');
  const location=()=>frame.evaluate(()=>document.location.href);
  const initial=await location();
  if(!vbrParentRouteMatches(initial,reference))throw Error('unbound RouterInterface own VBR route');
  return withForegroundLock(options.lockFile,async()=>{
    await surface.bringToFront();
    const assertConsole=async()=>{
      if(frame.isDetached())throw Error('RouterInterface frame detached');
      if(options.beforeCapture)await options.beforeCapture();
      const actual=await location();
      if(!vbrParentRouteMatches(actual,reference)||new URL(actual).origin!==new URL(initial).origin)
        throw Error('RouterInterface own VBR route or origin changed');
      return actual;
    };
    await assertConsole();
    // OOPIF cached URL may be empty. The strict document URL is checked before
    // and after the helper and again at capture; no other frame/URL fallback.
    const scoped={getByRole:(...args)=>frame.getByRole(...args),locator:(...args)=>frame.locator(...args),
      url:()=>frame.url()||initial};
    const landing=await routerInterfaceLanding(scoped,initial,reference,Date.now()+(options.renderTimeoutMs??10000));
    await assertConsole();
    const row=frame.locator('tbody tr').filter({has:frame.getByText(expected.identifier,{exact:true})});
    if(!landing.matched||await row.count()!==1||!await row.isVisible())
      throw Error('RouterInterface unique same-parent child row unavailable');
    const markers=[expected.identifier,expected.parent_router_id,expected.name,expected.peer_vpc];
    const readView=async()=>{
      const tab=frame.getByRole('tab',{name:'对等连接',exact:true});
      const tabSelected=await tab.count()===1&&await tab.getAttribute('aria-selected')==='true';
      const view=await row.evaluate(routerInterfaceRowView,{markers});
      const readiness=await landingState();
      return {...view,tab_selected:tabSelected,loading:readiness.visibleLoading};
    };
    const landingState=async()=>{
      const state=await routerInterfaceLanding(scoped,await assertConsole(),reference,
        Date.now()+(options.renderTimeoutMs??10000));
      if(!state.matched)throw Error('RouterInterface row changed or is loading');return state;
    };
    let view=await readView(),collapsed=false,toggle;
    const stages=[{step:'source_bound_parent_landing',url:initial},
      {step:'native_peer_connection_tab',url:await assertConsole(),clicked:landing.tabClicked}];
    try{
      if(!routerInterfaceChildReady(view,expected)){
        let delta;
        try{delta=routerInterfaceHorizontalDelta(view);}catch{
          toggle=frame.locator('.nav-collapse-trigger');
          if(await toggle.count()!==1||!await toggle.isVisible())throw Error('RouterInterface native nav collapse unavailable');
          await toggle.click({timeout:5000});collapsed=true;
          await new Promise(resolve=>setTimeout(resolve,100));view=await readView();
          delta=routerInterfaceHorizontalDelta(view);stages.push({step:'native_nav_collapse'});
        }
        for(let i=0;i<2&&delta;i++){
          await assertConsole();
          const field=view.fields.find(field=>Number.isFinite(field.top));
          await row.hover({position:{x:(view.scroll.left+view.scroll.right)/2-view.row_rect.left,
            y:(field.top+field.bottom)/2-view.row_rect.top}});
          await surface.mouse.wheel(delta,0);await new Promise(resolve=>setTimeout(resolve,150));
          view=await readView();stages.push({step:'native_horizontal_scroll',delta});
          delta=routerInterfaceChildReady(view,expected)?0:routerInterfaceHorizontalDelta(view);
        }
      }
      const capture=await captureNavigationPixels(surface,file,{consoleFrame:frame,mode:options.mode,
        identifier:expected.identifier,markers:[expected.name,expected.peer_vpc],beforeCapture:async()=>{
          await assertConsole();if(await row.count()!==1||!await row.isVisible())throw Error('RouterInterface child row changed');
          view=await readView();
        }});
      return {kind:expected.kind,source_tool_use_id:expected.source_tool_use_id,
        identifier:expected.identifier,parent_router_id:expected.parent_router_id,region:expected.region,
        identity_visible:routerInterfaceChildReady(view,expected)&&capture.foreground_visible===true&&
          (options.mode!=='iframe'||capture.frame_embedding?.visible===true),
        fields:view,screenshot:file,capture_sync:capture,stages,readonly_completed:true,
        original_landing_unchanged:true,cloud_writes:0};
    }finally{
      if(collapsed){await assertConsole();if(await toggle.count()!==1||!await toggle.isVisible())throw Error('RouterInterface native nav restore unavailable');
        await toggle.click({timeout:5000});}
    }
  });
}
