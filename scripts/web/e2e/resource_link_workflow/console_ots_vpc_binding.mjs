/** Reveal only the source-bound native OTS VPC binding row on its original page. */
import {withForegroundLock,captureNavigationPixels} from './browser.mjs';
import {routerInterfaceRowView} from './console_router_interface.mjs';

export function otsVpcBindingConsoleUrl(raw,expected){
  if(expected.kind!=='ots_vpc_binding_row'||expected.resource_type!=='ALIYUN::OTS::VpcBinder'||
      !/^[A-Za-z][A-Za-z0-9_-]{0,127}$/.test(expected.identifier)||
      !/^[a-z][a-z0-9-]{1,62}$/.test(expected.instance_name)||!/^vpc-[a-z0-9]+$/.test(expected.vpc_id)||
      !/^cn-[a-z0-9-]+$/.test(expected.region)||!expected.source_tool_use_id)
    throw Error('invalid source-bound OTS VPC binding inspection');
  let url;try{url=new URL(raw);}catch{throw Error('unbound OTS VPC binding console');}
  if(url.protocol!=='https:'||!['ots.console.aliyun.com','ots4service.console.aliyun.com'].includes(url.hostname)||
      url.port||url.username||url.password||url.search||url.hash||
      url.pathname!==`/${expected.region}/${expected.instance_name}/networkManagement`)
    throw Error('unbound OTS VPC binding console');
  return url;
}

export function otsVpcBindingRowReady(view,expected){
  return view.unique_row===true&&[expected.identifier,expected.instance_name,expected.vpc_id].every(marker=>
    view.fields?.some(field=>field.marker===marker&&field.visible===true));
}

export async function inspectOtsVpcBindingRow(surface,frame,expected,file,options={}){
  if(!frame||!['iframe','open'].includes(options.mode))throw Error('OTS VPC binding requires own clicked frame');
  const location=()=>frame.evaluate(()=>document.location.href);
  const initial=await location();otsVpcBindingConsoleUrl(initial,expected);
  return withForegroundLock(options.lockFile,async()=>{
    await surface.bringToFront();
    const assertConsole=async()=>{
      if(frame.isDetached())throw Error('OTS VPC binding frame detached');
      if(options.beforeCapture)await options.beforeCapture();
      const actual=await location();otsVpcBindingConsoleUrl(actual,expected);
      if(actual!==initial)throw Error('OTS VPC binding original landing changed');
    };
    await assertConsole();
    const row=frame.locator('tr').filter({has:frame.getByText(expected.vpc_id,{exact:true})});
    const view=async()=>({unique_row:await row.count()===1,
      ...(await row.evaluate(routerInterfaceRowView,{markers:[expected.identifier,expected.instance_name,expected.vpc_id]}))});
    if(await row.count()!==1||!await row.isVisible())throw Error('OTS VPC binding unique native row missing');
    // Browser-native scrolling only. No binding actions, DOM/style writes, URLs or resize.
    await row.scrollIntoViewIfNeeded({timeout:5000});
    await assertConsole();
    const capture=await captureNavigationPixels(surface,file,{consoleFrame:frame,mode:options.mode,
      identifier:expected.identifier,markers:[expected.instance_name,expected.vpc_id],beforeCapture:async()=>{
        await assertConsole();
        if(!otsVpcBindingRowReady(await view(),expected))throw Error('OTS VPC binding own fields remain clipped');
      }});
    const fields=await view();
    return {...expected,fields,screenshot:file,capture_sync:capture,
      identity_visible:otsVpcBindingRowReady(fields,expected)&&capture.render_check?.identity_visible===true&&
        capture.foreground_visible===true&&(options.mode!=='iframe'||capture.frame_embedding?.visible===true),
      readonly_completed:true,original_landing_unchanged:true,cloud_writes:0};
  });
}
