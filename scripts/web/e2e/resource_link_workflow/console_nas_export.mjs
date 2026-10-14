/** Native, source-bound NAS export drawer and tooltip pixels. Never edit DOM or URLs. */
import {withForegroundLock,captureNavigationPixels} from './browser.mjs';
import {routerInterfaceRowView} from './console_router_interface.mjs';

export function nasExportRouteMatches(url, expected) {
  try {const value=new URL(url);return value.protocol==='https:'&&
    ['nasnext.console.aliyun.com','nasnext4service.console.aliyun.com'].includes(value.hostname)&&
    value.pathname===`/${expected.region}/filesystem/${expected.file_system_id}/protocolservice`;
  }catch{return false;}
}
export function nasExportReady(captures, expected) {
  return ['export_id','path','mount'].every(kind=>captures.some(c=>c.kind===kind&&c.fields_visible===true&&
    c.capture_sync?.foreground_visible===true&&
    (c.mode!=='iframe'||c.capture_sync?.frame_embedding?.visible===true)))&&
    expected.source_tool_use_id&&captures.every(c=>c.cloud_writes===0);
}

export async function inspectNasExport(surface,frame,expected,file,options={}) {
  if(!frame||expected.kind!=='nas_protocol_mount_target_child'||
      expected.resource_type!=='ALIYUN::NAS::ProtocolMountTarget'||
      !/^exp-[a-z0-9]+$/.test(expected.export_id)||!/^ptc-[a-z0-9]+$/.test(expected.protocol_service_id)||
      !/^cpfs-[a-z0-9]+$/.test(expected.file_system_id)||!expected.source_tool_use_id||
      !expected.identifier.endsWith(`.${expected.region}.cpfs.aliyuncs.com`))throw Error('unbound NAS export');
  const location=()=>frame.evaluate(()=>document.location.href);
  const initial=await location();if(!nasExportRouteMatches(initial,expected))throw Error('wrong NAS parent route');
  return withForegroundLock(options.lockFile,async()=>{
    await surface.bringToFront();
    const assertConsole=async()=>{
      if(frame.isDetached())throw Error('NAS frame detached');
      if(options.beforeCapture)await options.beforeCapture();
      if(await location()!==initial||!nasExportRouteMatches(await location(),expected))throw Error('NAS parent changed');
    };
    await assertConsole();
    const parent=frame.locator('tr').filter({has:frame.getByText(expected.protocol_service_id,{exact:true})});
    if(await parent.count()!==1||!await parent.isVisible())throw Error('NAS exact parent row missing');
    const native=parent.getByText('导出目录',{exact:true});
    if(await native.count()!==1)throw Error('NAS native export control ambiguous');
    await native.click({timeout:5000});await assertConsole();
    const row=frame.locator('tr').filter({has:frame.getByText(expected.export_id,{exact:true})});
    await row.waitFor({state:'visible',timeout:10000});
    if(await row.count()!==1)throw Error('NAS exact export row ambiguous');
    const captures=[],stages=[{step:'native_export_directory',url:initial}];
    const visible=async(locator,marker)=>{
      const view=await locator.evaluate(routerInterfaceRowView,{markers:[marker]});
      return view.fields.length===1&&view.fields[0].visible===true;
    };
    const capture=async(kind,tooltip,marker,destination)=>{
      const sync=await captureNavigationPixels(surface,destination,{consoleFrame:frame,mode:options.mode,
        identifier:expected.export_id,markers:[expected.name,expected.path],beforeCapture:async()=>{
          await assertConsole();if(await row.count()!==1||!await row.isVisible()||
            !await tooltip.isVisible()||!await visible(tooltip,marker))throw Error('NAS native tooltip not fully painted');
        }});
      captures.push({kind,screenshot:destination,mode:options.mode,fields_visible:true,
        capture_sync:sync,cloud_writes:0});
    };
    const exportId=row.getByText(expected.export_id,{exact:true});await exportId.hover({timeout:5000});
    const idTip=frame.getByRole('tooltip').filter({hasText:new RegExp('^'+expected.export_id+'$')});
    await idTip.waitFor({state:'visible',timeout:3000});
    if(!await visible(row,expected.protocol_service_id)||!await visible(row,expected.name))throw Error('NAS parent/name clipped');
    await capture('export_id',idTip,expected.export_id,file.replace(/\.png$/,'.export-id.png'));
    const path=row.getByText(expected.path,{exact:true});await path.hover({timeout:5000});
    const pathTip=frame.getByRole('tooltip').filter({hasText:expected.path});
    await pathTip.waitFor({state:'visible',timeout:3000});
    if((await pathTip.innerText()).trim()!==expected.path)throw Error('NAS path tooltip mismatch');
    await capture('path',pathTip,expected.path,file.replace(/\.png$/,'.path.png'));
    const icon=row.locator('i.next-icon-data-set');
    if(await icon.count()!==1)throw Error('NAS address icon ambiguous');
    const expectedMount=expected.identifier+':/share'+expected.path.replace(/\/$/,'');
    for(let attempt=0;attempt<2;attempt++){
      await icon.hover({timeout:5000});
      const tip=frame.getByRole('tooltip').filter({hasText:expected.identifier});
      await tip.waitFor({state:'visible',timeout:3000});
      if((await tip.innerText()).trim()!==expectedMount)throw Error('NAS native mount address mismatch');
      if(await visible(tip,expectedMount)){
        await capture('mount',tip,expectedMount,file);break;
      }
      if(attempt===1)throw Error('NAS native mount tooltip clipped');
      await assertConsole();await icon.hover();await surface.mouse.wheel(450,0);
      stages.push({step:'native_horizontal_wheel',delta:450});
    }
    return {...expected,identity_visible:Boolean(nasExportReady(captures,expected)),
      screenshot:file,supplemental_captures:captures,stages,readonly_completed:true,
      original_landing_unchanged:true,cloud_writes:0};
  });
}
