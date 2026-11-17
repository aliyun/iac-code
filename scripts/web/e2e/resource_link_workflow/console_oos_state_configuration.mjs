/** Preserve actual edit landing; follow only its native read-only inventory-list link. */
import {withForegroundLock,captureNavigationPixels,LOADING_SELECTOR} from './browser.mjs';
import {essTaskRowView} from './console_ess_scheduled_task.mjs';

export function oosStateConsoleUrl(raw,expected,stage='landing') {
  if(expected.kind!=='oos_state_configuration_list'||expected.resource_type!=='ALIYUN::OOS::StateConfiguration'||
      !/^sc-[a-z0-9]+$/.test(expected.identifier)||!/^cn-[a-z0-9-]+$/.test(expected.region)||
      !/^[0-9]{16}$/.test(expected.account)||!expected.source_tool_use_id||
      !expected.description?.trim()||!expected.template_name?.trim())
    throw Error('invalid source-bound OOS state inspection');
  let url;try{url=new URL(raw);}catch{throw Error('unbound OOS state console');}
  const landing=stage==='landing',list=stage==='list';
  if((!landing&&!list)||!['https://oos.console.aliyun.com','https://oos4service.console.aliyun.com'].includes(url.origin)||
      url.username||url.password||!['','#/'].includes(url.hash)||
      url.pathname!==`/${expected.region}/inventory${landing?'/modify/'+expected.identifier:''}`||
      url.search!==(landing?'':'?tabType=detail'))throw Error('unbound OOS state console');
  return url;
}

export function oosStateListReady(view,expected) {
  return view.loading===false&&view.own_link===true&&view.fields?.length===1&&
    view.fields[0].marker===expected.identifier&&view.fields[0].visible===true;
}

export async function inspectOosStateConfigurationList(surface,frame,expected,file,options={}) {
  if(!frame||!['iframe','open'].includes(options.mode))throw Error('OOS state requires its actual clicked frame');
  const documentUrl=()=>frame.evaluate(()=>document.location.href);
  const initial=oosStateConsoleUrl(await documentUrl(),expected);
  return withForegroundLock(options.lockFile,async()=>{
    await surface.bringToFront();
    let stage='landing';
    const assertConsole=async()=>{
      if(frame.isDetached())throw Error('OOS state frame detached');
      if(options.beforeCapture)await options.beforeCapture();
      const current=oosStateConsoleUrl(await documentUrl(),expected,stage);
      if(current.origin!==initial.origin)throw Error('OOS state frame origin changed');
      return current;
    };
    await assertConsole();
    // The sidebar and breadcrumb share a label, but only the observed breadcrumb
    // has this native detail-list href. Do not guess a text-only occurrence.
    const anchor=frame.locator(`a[href="/${expected.region}/inventory?tabType=detail"]:visible`)
      .filter({hasText:/^配置清单管理$/});
    await anchor.waitFor({state:'visible',timeout:10000});
    if(await anchor.count()!==1)throw Error('OOS native inventory link ambiguous');
    // Validate the real DOM href before clicking it. Never construct a destination or click edit/save/delete.
    const destination=oosStateConsoleUrl(new URL(await anchor.getAttribute('href'),initial).href,expected,'list');
    if(destination.origin!==initial.origin)throw Error('OOS native inventory link origin changed');
    await assertConsole();
    await anchor.click();stage='list';
    // A native SPA transition may be asynchronous; only its exact list route is allowed.
    await frame.waitForFunction(({region})=>document.location.pathname===`/${region}/inventory`&&
      document.location.search==='?tabType=detail',{region:expected.region},{timeout:10000});
    await assertConsole();
    const ownLink=frame.getByRole('link',{name:expected.identifier,exact:true});
    const row=frame.locator('tbody tr').filter({has:ownLink});
    await row.waitFor({state:'visible',timeout:10000});
    const readView=async()=>{
      const current=await assertConsole();
      if(await row.count()!==1||!await row.isVisible()||await ownLink.count()!==1||!await ownLink.isVisible())
        throw Error('OOS own state row missing or ambiguous');
      const href=new URL(await ownLink.getAttribute('href'),current);
      oosStateConsoleUrl(href.href,expected,'landing');
      if(href.origin!==initial.origin)throw Error('OOS state row link origin changed');
      return {...await row.evaluate(essTaskRowView,{markers:[expected.identifier],loadingSelector:LOADING_SELECTOR}),
        own_link:true};
    };
    await row.scrollIntoViewIfNeeded();
    let view=await readView();
    for(let i=0;i<20&&!oosStateListReady(view,expected);i++){
      await surface.waitForTimeout(250);view=await readView();
    }
    const capture=await captureNavigationPixels(surface,file,{consoleFrame:frame,mode:options.mode,
      identifier:expected.identifier,markers:[expected.identifier],beforeCapture:async()=>{view=await readView();}});
    return {kind:expected.kind,identifier:expected.identifier,region:expected.region,account:expected.account,
      source_tool_use_id:expected.source_tool_use_id,description:expected.description,template_name:expected.template_name,
      identity_visible:oosStateListReady(view,expected)&&capture.foreground_visible===true,
      fields:view,screenshot:file,capture_sync:capture,
      stages:[{step:'original_edit_landing_preserved',url:initial.href},
        {step:'native_readonly_inventory_link',url:(await assertConsole()).href}],
      readonly_completed:true,original_landing_unchanged:true,original_landing_preserved:true,cloud_writes:0};
  });
}
