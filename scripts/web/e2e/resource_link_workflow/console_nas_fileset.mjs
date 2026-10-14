/** Preserve the original landing; hover only its own native Fileset description. */
import {withForegroundLock,captureNavigationPixels} from './browser.mjs';
import {routerInterfaceRowView} from './console_router_interface.mjs';

export function nasFilesetConsoleUrl(raw,expected) {
  if(expected.kind!=='nas_fileset_description'||expected.resource_type!=='ALIYUN::NAS::Fileset'||
      !/^fset-[a-z0-9]+$/.test(expected.identifier)||!/^cpfs-[a-z0-9]+$/.test(expected.file_system_id)||
      !/^cn-[a-z0-9-]+$/.test(expected.region)||typeof expected.description!=='string'||
      !expected.description.trim()||typeof expected.path!=='string'||!expected.path.startsWith('/')||
      !expected.source_tool_use_id)throw Error('invalid source-bound NAS Fileset inspection');
  let url;try{url=new URL(raw);}catch{throw Error('unbound NAS Fileset console');}
  if(url.protocol!=='https:'||!['nasnext.console.aliyun.com','nasnext4service.console.aliyun.com'].includes(url.hostname)||
      url.port||url.username||url.password||url.search||url.hash||
      url.pathname!==`/${expected.region}/filesystem/${expected.file_system_id}/fileset`)
    throw Error('unbound NAS Fileset console');
  return url;
}

export function nasFilesetDescriptionReady(view,expected) {
  return view.unique_row===true&&view.unique_tooltip===true&&view.tooltip_text===expected.description&&
    [expected.identifier,expected.path,expected.description].every(marker=>
      view.fields?.some(field=>field.marker===marker&&field.visible===true));
}

export async function inspectNasFilesetDescription(surface,frame,expected,file,options={}) {
  if(!frame||!['iframe','open'].includes(options.mode))throw Error('NAS Fileset requires its real clicked frame');
  const location=()=>frame.evaluate(()=>document.location.href);
  const initial=await location();nasFilesetConsoleUrl(initial,expected);
  return withForegroundLock(options.lockFile,async()=>{
    await surface.bringToFront();
    const assertConsole=async()=>{
      if(frame.isDetached())throw Error('NAS Fileset frame detached');
      if(options.beforeCapture)await options.beforeCapture();
      const actual=await location();nasFilesetConsoleUrl(actual,expected);
      if(actual!==initial)throw Error('NAS Fileset original landing changed');
    };
    await assertConsole();
    const row=frame.locator('tr').filter({has:frame.getByText(expected.identifier,{exact:true})});
    if(await row.count()!==1||!await row.isVisible())throw Error('NAS Fileset unique own row missing');
    const description=row.getByText(expected.description,{exact:true});
    if(await description.count()!==1)throw Error('NAS Fileset exact description label ambiguous');
    await description.hover({timeout:5000});
    const tip=frame.getByRole('tooltip').filter({hasText:expected.description});
    try{
      await tip.waitFor({state:'visible',timeout:4000});
      const view=async()=>({unique_row:await row.count()===1,unique_tooltip:await tip.count()===1,
        tooltip_text:await tip.innerText(),fields:[
          ...(await row.evaluate(routerInterfaceRowView,{markers:[expected.identifier,expected.path]})).fields,
          ...(await tip.evaluate(routerInterfaceRowView,{markers:[expected.description]})).fields,
        ]});
      const sync=await captureNavigationPixels(surface,file,{consoleFrame:frame,mode:options.mode,
        identifier:expected.identifier,markers:[expected.description,expected.path],beforeCapture:async()=>{
          await assertConsole();
          if(!nasFilesetDescriptionReady(await view(),expected)||!await tip.isVisible())
            throw Error('NAS Fileset native tooltip or own row is clipped');
        }});
      return {...expected,fields:await view(),screenshot:file,capture_sync:sync,
        identity_visible:sync.foreground_visible===true&&
          (options.mode!=='iframe'||sync.frame_embedding?.visible===true)&&nasFilesetDescriptionReady(await view(),expected),
        readonly_completed:true,original_landing_unchanged:true,cloud_writes:0};
    }finally{await surface.mouse.move(5,5);}
  });
}
