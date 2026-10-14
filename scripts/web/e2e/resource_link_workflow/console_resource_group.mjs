/** Same resource-detail native Settings tab, without URL construction or writes. */
import {withForegroundLock,captureNavigationPixels,LOADING_SELECTOR} from './browser.mjs';

export function resourceGroupConsoleUrl(raw, expected) {
  let url;try{url=new URL(raw);}catch{throw Error('unbound ResourceGroup console');}
  const base='/resource-groups/'+expected.identifier+'/';
  if(url.protocol!=='https:'||!['resourcemanager.console.aliyun.com','resourcemanager4service.console.aliyun.com'].includes(url.hostname)||
      url.username||url.password||url.hash||[...url.searchParams.keys()].some(key=>key!=='spm')||
      !['overview','settings'].some(tab=>url.pathname===base+tab))
    throw Error('unbound ResourceGroup console');
  return url;
}

export function resourceGroupMetadataReady(view, expected) {
  return view.loading===false&&view.settings_selected===true&&
    [expected.identifier,expected.name,expected.display_name]
      .every(marker=>view.fields?.some(field=>field.marker===marker&&field.visible===true));
}

export async function inspectResourceGroupMetadata(surface, frame, expected, file, options={}) {
  if(!frame||expected.kind!=='resource_group_metadata'||!/^rg-[a-z0-9]+$/.test(expected.identifier)||
      expected.region!=='global'||!/^cn-[a-z0-9-]+$/.test(expected.api_region)||
      !/^[a-z][a-z0-9]{2,11}$/.test(expected.name)||typeof expected.display_name!=='string'||
      !expected.display_name.trim()||expected.display_name===expected.identifier)
    throw Error('invalid ResourceGroup inspection');
  const location=()=>frame.evaluate(()=>document.location.href);
  const initial=resourceGroupConsoleUrl(await location(),expected);
  return withForegroundLock(options.lockFile,async()=>{
    await surface.bringToFront();
    const assertConsole=async()=>{
      if(frame.isDetached())throw Error('ResourceGroup frame detached');
      if(options.beforeCapture)await options.beforeCapture();
      const actual=resourceGroupConsoleUrl(await location(),expected);
      if(actual.origin!==initial.origin)throw Error('ResourceGroup origin changed');
      return actual;
    };
    await assertConsole();
    const stages=[{step:'resource_link_landing',url:initial.href}];
    // Unlike the several sidebar links named 设置, only a native group detail
    // tab inside the same overview/resources/permissions tab bar is eligible.
    const tabs=await frame.getByRole('tab',{name:'设置',exact:true}).all(),eligible=[];
    for(const tab of tabs)if(await tab.isVisible()&&await tab.evaluate(el=>{
      const bar=el.closest('[role="tablist"],.next-tabs-nav');
      return !!bar&&['概览','资源管理','权限管理','设置'].every(label=>
        [...bar.querySelectorAll('[role="tab"],.next-tabs-tab')].some(node=>node.textContent.trim()===label));
    }))eligible.push(tab);
    if(eligible.length!==1)throw Error('ResourceGroup detail Settings tab missing or ambiguous');
    const settings=eligible[0];
    await assertConsole();await settings.click({timeout:10000});
    // The real console uses empty Fusion tabpanes as route placeholders.
    // Settings facts are in the separately rendered, labeled basic-info grid.
    // Bind all three labels in one visible grid, never header/breadcrumb text.
    const panel=frame.locator('.wind-rc-data-fields:visible')
      .filter({has:frame.getByText('资源组ID',{exact:true})})
      .filter({has:frame.getByText('资源组标识',{exact:true})})
      .filter({has:frame.getByText('资源组名称',{exact:true})});
    await panel.waitFor({state:'visible',timeout:10000});
    if(await panel.count()!==1)throw Error('ResourceGroup Settings content missing or ambiguous');
    const markers=[
      {marker:expected.identifier,label:'资源组ID'},
      {marker:expected.name,label:'资源组标识'},
      {marker:expected.display_name,label:'资源组名称'},
    ];
    const readView=async()=>{
      const selected=await settings.getAttribute('aria-selected')==='true';
      const view=await panel.evaluate((el,{markers,selector})=>{
        // Read only the actual labeled Settings grid, not breadcrumb/header,
        // hidden DOM or sidebar. Every painted text fragment must be unoccluded.
        const fields=markers.map(({marker,label})=>{
          const items=[...el.querySelectorAll('.wind-rc-data-fields-item')].filter(item=>{
            const labels=document.createTreeWalker(item,NodeFilter.SHOW_TEXT);
            while(labels.nextNode())if(labels.currentNode.textContent.trim()===label&&
              !labels.currentNode.parentElement.closest('.wind-rc-data-fields-item-value'))return true;
            return false;
          });
          if(items.length!==1)return {marker,label,visible:false};
          const values=items[0].querySelectorAll('.wind-rc-data-fields-item-value');
          if(values.length!==1||values[0].textContent.trim()!==marker)return {marker,label,visible:false};
          const walker=document.createTreeWalker(values[0],NodeFilter.SHOW_TEXT);let node;
          while(walker.nextNode())if(walker.currentNode.textContent.trim()===marker){node=walker.currentNode;break;}
          if(!node)return {marker,label,visible:false};
          const range=document.createRange();range.selectNodeContents(node);
          const pieces=[...range.getClientRects()].filter(r=>r.width>0&&r.height>0);
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
          return {marker,label,visible};
        });
        return {fields,loading:[...document.querySelectorAll(selector)].some(node=>{
          const style=getComputedStyle(node);
          return node.getClientRects().length&&style.display!=='none'&&style.visibility!=='hidden'&&Number(style.opacity)>0.01;
        })};
      },{markers,selector:LOADING_SELECTOR});
      return {...view,settings_selected:selected};
    };
    const deadline=Date.now()+(options.renderTimeoutMs??10000);
    let view=await readView();
    while(!resourceGroupMetadataReady(view,expected)&&Date.now()<deadline){
      await assertConsole();await new Promise(resolve=>setTimeout(resolve,50));view=await readView();
    }
    const capture=await captureNavigationPixels(surface,file,{consoleFrame:frame,mode:options.mode,
      identifier:expected.identifier,markers:[expected.name,expected.display_name],beforeCapture:async()=>{
        const actual=await assertConsole();
        if(actual.pathname!=='/resource-groups/'+expected.identifier+'/settings')
          throw Error('ResourceGroup Settings tab did not navigate to its observed route');
        if(await panel.count()!==1||!await panel.isVisible())throw Error('ResourceGroup Settings pane changed');
        view=await readView();
      }});
    const final=await assertConsole();
    stages.push({step:'actual_native_settings_click',url:final.href});
    return {kind:expected.kind,identity_visible:resourceGroupMetadataReady(view,expected)&&
      capture.foreground_visible===true&&(options.mode!=='iframe'||capture.frame_embedding?.visible===true),
      fields:view,screenshot:file,capture_sync:capture,stages,readonly_completed:true,
      original_landing_unchanged:true,cloud_writes:0};
  });
}
