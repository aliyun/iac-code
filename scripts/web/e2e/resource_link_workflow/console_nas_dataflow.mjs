/** Existing DataFlow: native path tooltip + disabled OSS metadata, Cancel only. */
import {withForegroundLock,captureNavigationPixels} from './browser.mjs';
import {routerInterfaceRowView} from './console_router_interface.mjs';

export function nasDataFlowConsoleUrl(raw,expected) {
  if(expected.kind!=='nas_dataflow_metadata'||expected.resource_type!=='ALIYUN::NAS::DataFlow'||
    !/^df-[a-z0-9]+$/.test(expected.identifier)||!/^cpfs-[a-z0-9]+$/.test(expected.file_system_id)||
    !/^fset-[a-z0-9]+$/.test(expected.fileset_id)||!/^cn-[a-z0-9-]+$/.test(expected.region)||
    typeof expected.path!=='string'||!expected.path.startsWith('/')||
    !/^oss:\/\/[a-z0-9][a-z0-9-]{1,62}(?:\/[^\s]*)?$/.test(expected.source_storage)||
    !expected.source_tool_use_id)throw Error('invalid signed NAS DataFlow inspection');
  let url;try{url=new URL(raw);}catch{throw Error('unbound NAS DataFlow console');}
  if(url.protocol!=='https:'||!['nasnext.console.aliyun.com','nasnext4service.console.aliyun.com'].includes(url.hostname)||
    url.port||url.username||url.password||url.search||url.hash||
    url.pathname!==`/${expected.region}/filesystem/${expected.file_system_id}/dataflow`)
    throw Error('unbound NAS DataFlow console');
  return url;
}

export function nasDataFlowMetadataReady(view,expected) {
  return view.unique_row===true&&view.unique_tooltip===true&&view.tooltip_text===expected.path&&
    view.source_disabled===true&&view.fileset_disabled===true&&view.parent_disabled===true&&
    view.parent===expected.file_system_id&&view.source_visible===true&&view.dialog_error===false&&
    [expected.identifier,expected.fileset_id,expected.path].every(marker=>
      view.path_fields?.some(field=>field.marker===marker&&field.visible===true));
}

export async function inspectNasDataFlowMetadata(surface,frame,expected,file,options={}) {
  if(!frame||options.mode!=='iframe')throw Error('NAS DataFlow requires own clicked iframe');
  const location=()=>frame.evaluate(()=>document.location.href);
  const initial=await location();nasDataFlowConsoleUrl(initial,expected);
  return withForegroundLock(options.lockFile,async()=>{
    await surface.bringToFront();
    const assertConsole=async()=>{
      if(options.beforeCapture)await options.beforeCapture();
      const actual=await location();nasDataFlowConsoleUrl(actual,expected);
      if(actual!==initial)throw Error('NAS DataFlow landing changed');
    };
    await assertConsole();
    const row=frame.locator('tr').filter({has:frame.getByText(expected.identifier,{exact:true})});
    if(await row.count()!==1||!await row.isVisible()||
      await row.getByText(expected.fileset_id,{exact:true}).count()!==1)throw Error('unique own DataFlow row missing');
    const path=row.getByText(expected.path,{exact:true});if(await path.count()!==1)throw Error('own native path missing');
    await path.hover({timeout:5000});
    const tip=frame.getByRole('tooltip').filter({hasText:expected.path});
    await tip.waitFor({state:'visible',timeout:5000});
    const pathView={unique_row:await row.count()===1,unique_tooltip:await tip.count()===1,
      tooltip_text:await tip.innerText(),path_fields:[
        ...(await row.evaluate(routerInterfaceRowView,{markers:[expected.identifier,expected.fileset_id]})).fields,
        ...(await tip.evaluate(routerInterfaceRowView,{markers:[expected.path]})).fields]};
    if(!pathView.unique_tooltip||pathView.tooltip_text!==expected.path||pathView.path_fields.some(f=>!f.visible))
      throw Error('native DataFlow path pixels clipped');
    const pathFile=file.replace(/\.png$/,'-path.png');
    const pathSync=await captureNavigationPixels(surface,pathFile,{consoleFrame:frame,mode:'iframe',
      identifier:expected.identifier,markers:[expected.fileset_id],beforeCapture:assertConsole});
    await surface.mouse.move(5,5);
    const dialog=frame.getByRole('dialog',{name:'修改数据流动',exact:true});
    const edit=row.getByRole('button',{name:'修改',exact:true});if(await edit.count()!==1)throw Error('own metadata control missing');
    let view,sync;
    try {
      // Native UI has no Detail action. Opening the existing form is observation,
      // never a mutation: no fill/check/select/Confirm/Save/API operations here.
      await edit.click({timeout:5000});await dialog.waitFor({state:'visible',timeout:10000});
      if(await dialog.count()!==1)throw Error('native metadata ambiguous');
      const source=dialog.getByText(expected.source_storage,{exact:true});
      const sourceField=dialog.locator('input[name="SourceStorage"]');
      const parent=dialog.locator('input[name="FileSystemId"]');
      const fset=dialog.locator('input[name="FsetId"]');
      await source.waitFor({state:'visible',timeout:10000});
      const sourceView=await source.evaluate(routerInterfaceRowView,{markers:[expected.source_storage]});
      view={...pathView,parent:await parent.inputValue(),parent_disabled:await parent.isDisabled(),
        source_disabled:await sourceField.isDisabled(),fileset_disabled:await fset.isDisabled(),
        source_visible:sourceView.fields.some(f=>f.marker===expected.source_storage&&f.visible),
        dialog_error:await dialog.getByRole('alert').filter({hasText:/NoSuchBucket|AccessDenied|Forbidden|Error|失败|错误/}).count()>0};
      if(!nasDataFlowMetadataReady(view,expected))throw Error('native DataFlow metadata pixels not ready');
      sync=await captureNavigationPixels(surface,file,{consoleFrame:frame,mode:'iframe',identifier:expected.identifier,
        beforeCapture:assertConsole});
    } finally {
      if(await dialog.isVisible().catch(()=>false)){
        const cancel=dialog.getByRole('button',{name:'取消',exact:true});if(await cancel.count()!==1)throw Error('native Cancel ambiguous');
        await cancel.click({timeout:5000});await dialog.waitFor({state:'hidden',timeout:5000});
      }
    }
    await assertConsole();
    return {...expected,fields:view,screenshot:file,capture_sync:sync,
      supplemental_captures:[{kind:'path',screenshot:pathFile,capture_sync:pathSync},{kind:'source',screenshot:file,capture_sync:sync}],
      identity_visible:nasDataFlowMetadataReady(view,expected)&&pathSync.foreground_visible===true&&
        pathSync.frame_embedding?.visible===true&&sync.foreground_visible===true&&sync.frame_embedding?.visible===true,
      dialog_cancelled:true,readonly_completed:true,original_landing_unchanged:true,cloud_writes:0};
  });
}
