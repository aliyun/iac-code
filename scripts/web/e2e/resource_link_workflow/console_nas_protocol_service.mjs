/** Read only the own native ProtocolService ID tooltip on the original landing. */
import {withForegroundLock,captureNavigationPixels} from './browser.mjs';
import {routerInterfaceRowView} from './console_router_interface.mjs';

export function nasProtocolServiceConsoleUrl(raw,expected) {
  if(expected.kind!=='nas_protocol_service_identity'||expected.resource_type!=='ALIYUN::NAS::ProtocolService'||
      !/^ptc-[a-z0-9]+$/.test(expected.identifier)||!/^cpfs-[a-z0-9]+$/.test(expected.file_system_id)||
      !/^cn-[a-z0-9-]+$/.test(expected.region)||typeof expected.description!=='string'||
      !expected.description.trim()||expected.description===expected.identifier||!expected.source_tool_use_id)
    throw Error('invalid source-bound NAS ProtocolService inspection');
  let url;try{url=new URL(raw);}catch{throw Error('unbound NAS ProtocolService console');}
  if(url.protocol!=='https:'||!['nasnext.console.aliyun.com','nasnext4service.console.aliyun.com'].includes(url.hostname)||
      url.port||url.username||url.password||url.search||url.hash||
      url.pathname!==`/${expected.region}/filesystem/${expected.file_system_id}/protocolservice`)
    throw Error('unbound NAS ProtocolService console');
  return url;
}

export function nasProtocolServiceIdentityReady(view,expected) {
  return view.unique_row===true&&view.unique_tooltip===true&&view.tooltip_text===expected.identifier&&
    [expected.description,expected.identifier].every(marker=>
      view.fields?.some(field=>field.marker===marker&&field.visible===true));
}

export async function inspectNasProtocolServiceIdentity(surface,frame,expected,file,options={}) {
  if(!frame||!['iframe','open'].includes(options.mode))throw Error('NAS ProtocolService requires its real clicked frame');
  const location=()=>frame.evaluate(()=>document.location.href);
  const initial=await location();nasProtocolServiceConsoleUrl(initial,expected);
  return withForegroundLock(options.lockFile,async()=>{
    await surface.bringToFront();
    const assertConsole=async()=>{
      if(frame.isDetached())throw Error('NAS ProtocolService frame detached');
      if(options.beforeCapture)await options.beforeCapture();
      const actual=await location();nasProtocolServiceConsoleUrl(actual,expected);
      if(actual!==initial)throw Error('NAS ProtocolService original landing changed');
    };
    await assertConsole();
    const row=frame.locator('tr').filter({has:frame.getByText(expected.identifier,{exact:true})});
    if(await row.count()!==1||!await row.isVisible())throw Error('NAS ProtocolService unique own row missing');
    const label=row.getByText(expected.identifier,{exact:true});
    if(await label.count()!==1)throw Error('NAS ProtocolService exact ID label ambiguous');
    try{
      await label.hover({timeout:5000});
      const tip=frame.getByRole('tooltip').filter({hasText:expected.identifier});
      await tip.waitFor({state:'visible',timeout:4000});
      const view=async()=>({unique_row:await row.count()===1,unique_tooltip:await tip.count()===1,
        tooltip_text:await tip.innerText(),fields:[
          ...(await row.evaluate(routerInterfaceRowView,{markers:[expected.description]})).fields,
          ...(await tip.evaluate(routerInterfaceRowView,{markers:[expected.identifier]})).fields,
        ]});
      const sync=await captureNavigationPixels(surface,file,{consoleFrame:frame,mode:options.mode,
        identifier:expected.identifier,markers:[expected.description],beforeCapture:async()=>{
          await assertConsole();
          if(!nasProtocolServiceIdentityReady(await view(),expected)||!await tip.isVisible())
            throw Error('NAS ProtocolService native tooltip or own description is clipped');
        }});
      const fields=await view();
      return {...expected,fields,screenshot:file,capture_sync:sync,
        identity_visible:sync.foreground_visible===true&&
          (options.mode!=='iframe'||sync.frame_embedding?.visible===true)&&nasProtocolServiceIdentityReady(fields,expected),
        readonly_completed:true,original_landing_unchanged:true,cloud_writes:0};
    }finally{await surface.mouse.move(5,5);}
  });
}
