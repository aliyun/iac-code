/** Own NAS mount-address icon tooltip only; retain the original clicked PNG. */
import {withForegroundLock,captureNavigationPixels} from './browser.mjs';
import {routerInterfaceRowView} from './console_router_interface.mjs';

export function nasMountTargetConsoleUrl(raw,expected) {
  if(expected.kind!=='nas_mount_target_address'||
      !['ALIYUN::NAS::MountTarget','alicloud_nas_mount_target'].includes(expected.resource_type)||
      !/^[a-z0-9]+$/.test(expected.file_system_id)||expected.region!=='cn-hangzhou'||
      !/^[a-z0-9-]+\.cn-hangzhou\.nas\.aliyuncs\.com$/.test(expected.identifier)||
      !expected.identifier.startsWith(expected.file_system_id+'-')||
      !/^vpc-[a-z0-9]+$/.test(expected.vpc_id)||!/^vsw-[a-z0-9]+$/.test(expected.vswitch_id)||
      !/^[0-9]{16}$/.test(expected.account)||!expected.source_tool_use_id)
    throw Error('invalid source-bound NAS mount address inspection');
  let url;try{url=new URL(raw);}catch{throw Error('unbound NAS mount console');}
  if(url.protocol!=='https:'||!['nasnext.console.aliyun.com','nasnext4service.console.aliyun.com'].includes(url.hostname)||
      url.port||url.username||url.password||url.search||url.hash||
      url.pathname!==`/${expected.region}/filesystem/${expected.file_system_id}/mount`)
    throw Error('unbound NAS mount console');
  return url;
}

export function nasMountTargetAddressReady(view,expected) {
  return view.unique_row===true&&view.unique_address_icon===true&&view.unique_tooltip===true&&
    [expected.identifier,expected.vpc_id].every(marker=>
      view.fields?.some(field=>field.marker===marker&&field.visible===true));
}

export async function inspectNasMountTargetAddress(surface,frame,expected,file,options={}) {
  if(!frame||!['iframe','open'].includes(options.mode))throw Error('NAS mount requires its real clicked frame');
  const location=()=>frame.evaluate(()=>document.location.href);
  const initial=await location();nasMountTargetConsoleUrl(initial,expected);
  return withForegroundLock(options.lockFile,async()=>{
    await surface.bringToFront();
    const assertConsole=async()=>{
      if(frame.isDetached())throw Error('NAS mount frame detached');
      if(options.beforeCapture)await options.beforeCapture();
      const actual=await location();nasMountTargetConsoleUrl(actual,expected);
      if(actual!==initial)throw Error('NAS mount original landing changed');
    };
    await assertConsole();
    const row=frame.locator('tbody tr').filter({has:frame.getByText(expected.vpc_id,{exact:true})})
      .filter({has:frame.getByText(expected.vswitch_id,{exact:true})});
    if(await row.count()!==1||!await row.isVisible())throw Error('NAS mount exact own row ambiguous or missing');
    const table=frame.locator('.next-table').filter({has:row});
    if(await table.count()!==1)throw Error('NAS mount native table ambiguous');
    const headers=(await table.locator('thead th').allTextContents()).map(text=>text.trim());
    const index=headers.indexOf('挂载点地址');
    if(index<0||headers.lastIndexOf('挂载点地址')!==index||await row.locator('td').count()!==headers.length)
      throw Error('NAS mount native address column ambiguous or missing');
    const cell=row.locator('td').nth(index),icon=cell.locator('i.next-icon-database_fill');
    if(await icon.count()!==1)throw Error('NAS mount native address icon ambiguous or missing');
    try{
      // Native scrolling reveals the existing column, without altering DOM or viewport.
      await cell.scrollIntoViewIfNeeded({timeout:5000});
      await assertConsole();
      await icon.hover({timeout:5000});
      const tip=frame.getByRole('tooltip').filter({has:frame.getByText(expected.identifier,{exact:true})});
      await tip.waitFor({state:'visible',timeout:4000});
      const view=async()=>({unique_row:await row.count()===1,unique_address_icon:await icon.count()===1,
        unique_tooltip:await tip.count()===1,fields:[
          ...(await tip.evaluate(routerInterfaceRowView,{markers:[expected.identifier]})).fields,
          ...(await row.evaluate(routerInterfaceRowView,{markers:[expected.vpc_id]})).fields]});
      const capture=await captureNavigationPixels(surface,file,{consoleFrame:frame,mode:options.mode,
        identifier:expected.identifier,markers:[expected.vpc_id],beforeCapture:async()=>{
          await assertConsole();
          if(!await icon.isVisible()||!await tip.isVisible()||!nasMountTargetAddressReady(await view(),expected))
            throw Error('NAS mount native address or own VPC clipped or changed');
        }});
      const fields=await view();
      return {...expected,fields,screenshot:file,capture_sync:capture,
        identity_visible:capture.foreground_visible===true&&
          (options.mode!=='iframe'||capture.frame_embedding?.visible===true)&&nasMountTargetAddressReady(fields,expected),
        readonly_completed:true,original_landing_unchanged:true,cloud_writes:0};
    }finally{await surface.mouse.move(5,5);}
  });
}
