/** Own SLS native title tooltip only; preserve the actual clicked landing PNG. */
import {withForegroundLock,captureNavigationPixels} from './browser.mjs';
import {routerInterfaceRowView} from './console_router_interface.mjs';

export function slsProjectConsoleUrl(raw,expected) {
  if(expected.kind!=='sls_project_metadata'||
      !['ALIYUN::SLS::Project','alicloud_log_project'].includes(expected.resource_type)||
      !/^[a-z][a-z0-9-]{2,62}$/.test(expected.identifier)||expected.region!=='cn-hangzhou'||
      !/^[0-9]{16}$/.test(expected.account)||!expected.source_tool_use_id||
      typeof expected.description!=='string'||!expected.description.trim()||expected.description===expected.identifier)
    throw Error('invalid source-bound SLS project inspection');
  let url;try{url=new URL(raw);}catch{throw Error('unbound SLS project console');}
  if(url.protocol!=='https:'||!['sls.console.aliyun.com','sls4service.console.aliyun.com'].includes(url.hostname)||
      url.port||url.username||url.password||url.search||url.hash||
      url.pathname!==`/lognext/project/${expected.identifier}/overview`)
    throw Error('unbound SLS project console');
  return url;
}

export function slsProjectMetadataReady(view,expected) {
  return view.unique_title===true&&view.unique_tooltip===true&&
    [expected.identifier,expected.description,'华东1（杭州）'].every(marker=>
      view.fields?.some(field=>field.marker===marker&&field.visible===true));
}

export async function inspectSlsProjectMetadata(surface,frame,expected,file,options={}) {
  if(!frame||!['iframe','open'].includes(options.mode))throw Error('SLS project requires its real clicked frame');
  const location=()=>frame.evaluate(()=>document.location.href);
  const initial=await location();slsProjectConsoleUrl(initial,expected);
  return withForegroundLock(options.lockFile,async()=>{
    await surface.bringToFront();
    const assertConsole=async()=>{
      if(frame.isDetached())throw Error('SLS project frame detached');
      if(options.beforeCapture)await options.beforeCapture();
      const actual=await location();slsProjectConsoleUrl(actual,expected);
      if(actual!==initial)throw Error('SLS project original landing changed');
    };
    await assertConsole();
    const title=frame.locator('.dark-top-project').getByText(expected.identifier,{exact:true});
    if(await title.count()!==1||!await title.isVisible())throw Error('SLS project exact native title ambiguous or missing');
    try{
      await title.hover({timeout:5000});
      const tip=frame.getByRole('tooltip').filter({hasText:expected.identifier});
      await tip.waitFor({state:'visible',timeout:4000});
      const view=async()=>({unique_title:await title.count()===1,unique_tooltip:await tip.count()===1,
        ...(await tip.evaluate(routerInterfaceRowView,{markers:[expected.identifier,expected.description,'华东1（杭州）']}))});
      const capture=await captureNavigationPixels(surface,file,{consoleFrame:frame,mode:options.mode,
        identifier:expected.identifier,markers:[expected.description],beforeCapture:async()=>{
          await assertConsole();
          if(!await title.isVisible()||!await tip.isVisible()||!slsProjectMetadataReady(await view(),expected))
            throw Error('SLS project native tooltip fields clipped or changed');
        }});
      const fields=await view();
      return {...expected,fields,screenshot:file,capture_sync:capture,
        identity_visible:capture.foreground_visible===true&&
          (options.mode!=='iframe'||capture.frame_embedding?.visible===true)&&slsProjectMetadataReady(fields,expected),
        readonly_completed:true,original_landing_unchanged:true,cloud_writes:0};
    }finally{await surface.mouse.move(5,5);}
  });
}
