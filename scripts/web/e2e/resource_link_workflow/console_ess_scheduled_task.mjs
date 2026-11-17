/** Exact loaded ESS task row; only native horizontal scrolling, never writes. */
import {withForegroundLock,captureNavigationPixels,LOADING_SELECTOR} from './browser.mjs';

export function essTaskConsoleUrl(raw,expected) {
  if(expected.kind!=='ess_scheduled_task_metadata'||
      !['ALIYUN::ESS::ScheduledTask','alicloud_ess_scheduled_task'].includes(expected.resource_type)||
      !/^[A-Za-z0-9]{1,128}$/.test(expected.identifier)||!/^asg-[a-z0-9]+$/.test(expected.scaling_group_id)||
      !/^cn-[a-z0-9-]+$/.test(expected.region)||typeof expected.name!=='string'||!expected.name.trim()||
      !expected.source_tool_use_id)throw Error('invalid source-bound ESS task inspection');
  let url;try{url=new URL(raw);}catch{throw Error('unbound ESS task console');}
  if(url.origin!=='https://essnew.console.aliyun.com'||url.username||url.password||url.pathname!=='/'||
      url.search||url.hash!=='#/task/v3/timer/'+expected.region)throw Error('unbound ESS task console');
  return url;
}

export function essTaskMetadataReady(view,expected) {
  return view.loading===false&&view.same_task_cell===true&&
    [expected.identifier,expected.name,expected.scaling_group_id].every(marker=>
      view.fields?.some(field=>field.marker===marker&&field.visible===true));
}

export function essTaskHorizontalDelta(view,expected) {
  const field=view.fields?.find(f=>f.marker===expected.scaling_group_id),gap=view.scroll;
  if(!field||!gap||![field.left,field.right,gap.left,gap.right,gap.current,gap.maximum].every(Number.isFinite)||
      field.right<=field.left||gap.right-gap.left<field.right-field.left+4)
    throw Error('ESS task native unoccluded group viewport unavailable');
  const delta=(field.left+field.right-gap.left-gap.right)/2;
  if(gap.current+delta<0||gap.current+delta>gap.maximum)throw Error('ESS task native scroll out of range');
  return delta;
}

export function essTaskRowView(el,{markers,loadingSelector}) {
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
    const rect=container.getBoundingClientRect();let left=Math.max(0,rect.left),right=Math.min(innerWidth,rect.right);
    for(const cell of el.querySelectorAll('td')){
      const style=getComputedStyle(cell),box=cell.getBoundingClientRect();
      if(style.position!=='sticky')continue;
      if(style.left!=='auto')left=Math.max(left,box.right);
      if(style.right!=='auto')right=Math.min(right,box.left);
    }
    scroll={left,right,current:container.scrollLeft,maximum:container.scrollWidth-container.clientWidth};
  }
  const taskCells=[...el.querySelectorAll('td')].filter(cell=>{
    const text=cell.innerText.split('\n').map(t=>t.trim());return text.includes(markers[0])&&text.includes(markers[1]);
  });
  const loading=[...document.querySelectorAll(loadingSelector)].some(n=>{
    const style=getComputedStyle(n);return n.getClientRects().length&&style.display!=='none'&&
      style.visibility!=='hidden'&&Number(style.opacity)>0.01;
  });
  return {fields,scroll,loading,same_task_cell:taskCells.length===1};
}

export async function inspectEssScheduledTaskMetadata(surface,frame,expected,file,options={}) {
  if(!frame||options.mode!=='open')throw Error('ESS task inspection requires the bound direct-open frame');
  const documentUrl=()=>frame.evaluate(()=>document.location.href);
  const initial=essTaskConsoleUrl(await documentUrl(),expected);
  return withForegroundLock(options.lockFile,async()=>{
    await surface.bringToFront();
    const assertConsole=async()=>{
      if(frame.isDetached())throw Error('ESS task frame detached');
      if(options.beforeCapture)await options.beforeCapture();
      if(essTaskConsoleUrl(await documentUrl(),expected).href!==initial.href)throw Error('ESS task route changed');
    };
    await assertConsole();
    const row=frame.locator('tbody tr').filter({has:frame.getByText(expected.identifier,{exact:true})});
    await row.waitFor({state:'visible',timeout:10000});
    const markers=[expected.identifier,expected.name,expected.scaling_group_id];
    const readView=async()=>{
      await assertConsole();
      if(await row.count()!==1||!await row.isVisible())throw Error('ESS exact task row changed or ambiguous');
      return row.evaluate(essTaskRowView,{markers,loadingSelector:LOADING_SELECTOR});
    };
    let view=await readView();
    const stages=[{step:'original_task_list_landing',url:initial.href}];
    for(let i=0;i<2&&!essTaskMetadataReady(view,expected);i++){
      // The real ESS task/name column is sticky-left, and actions sticky-right.
      // Move only the scrolling group column into that native painted gap.
      const delta=essTaskHorizontalDelta(view,expected);
      if(Math.abs(delta)<1)break;
      await row.hover();await surface.mouse.wheel(delta,0);
      await frame.evaluate(()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
      view=await readView();stages.push({step:'native_horizontal_scroll',delta});
    }
    const capture=await captureNavigationPixels(surface,file,{consoleFrame:frame,mode:'open',
      identifier:expected.identifier,markers:[expected.name,expected.scaling_group_id],
      beforeCapture:async()=>{view=await readView();}});
    return {kind:expected.kind,identifier:expected.identifier,region:expected.region,
      source_tool_use_id:expected.source_tool_use_id,identity_visible:essTaskMetadataReady(view,expected)&&
        capture.foreground_visible===true,fields:view,screenshot:file,capture_sync:capture,stages,
      readonly_completed:true,original_landing_unchanged:true,cloud_writes:0};
  });
}
