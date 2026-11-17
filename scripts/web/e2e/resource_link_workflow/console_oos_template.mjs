/** Preserve the real landing, then expand only its native read-only basic information. */
import {withForegroundLock,captureNavigationPixels,LOADING_SELECTOR} from './browser.mjs';
import {essTaskRowView} from './console_ess_scheduled_task.mjs';

export function oosTemplateConsoleUrl(raw,expected) {
  if(expected.kind!=='oos_template_metadata'||expected.resource_type!=='ALIYUN::OOS::Template'||
      !/^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/.test(expected.identifier)||
      !/^cn-[a-z0-9-]+$/.test(expected.region)||typeof expected.description!=='string'||
      !expected.description.trim()||expected.description===expected.identifier||
      !/^t-[a-z0-9]+$/.test(expected.template_id)||!expected.source_tool_use_id)
    throw Error('invalid source-bound OOS template inspection');
  let url;try{url=new URL(raw);}catch{throw Error('unbound OOS template console');}
  if(!['https://oos.console.aliyun.com','https://oos4service.console.aliyun.com'].includes(url.origin)||
      url.username||url.password||url.search||!['','#/'].includes(url.hash)||
      url.pathname!==`/${expected.region}/template/private/detail/${expected.identifier}`)
    throw Error('unbound OOS template console');
  return url;
}

export function oosTemplateMetadataReady(view,expected) {
  return view.loading===false&&view.labeled_fields===true&&
    [expected.identifier,expected.description].every(marker=>
      view.fields?.some(field=>field.marker===marker&&field.visible===true));
}

export function oosDescriptionColumn(label) {
  const th=label.closest('th'),row=th?.parentElement,values=row?.nextElementSibling;
  if(!th||row?.tagName!=='TR'||values?.tagName!=='TR'||row.cells.length!==values.cells.length||
      [...row.cells].some(cell=>cell.tagName!=='TH'||cell.colSpan!==1)||
      [...values.cells].some(cell=>cell.tagName!=='TD'||cell.colSpan!==1))
    throw Error('OOS labeled vertical description layout changed');
  return th.cellIndex;
}

export async function inspectOosTemplateMetadata(surface,frame,expected,file,options={}) {
  if(!frame||!['iframe','open'].includes(options.mode))throw Error('OOS template requires its real clicked frame');
  const documentUrl=()=>frame.evaluate(()=>document.location.href);
  const initial=oosTemplateConsoleUrl(await documentUrl(),expected);
  return withForegroundLock(options.lockFile,async()=>{
    await surface.bringToFront();
    const assertConsole=async()=>{
      if(frame.isDetached())throw Error('OOS template frame detached');
      if(options.beforeCapture)await options.beforeCapture();
      const current=oosTemplateConsoleUrl(await documentUrl(),expected);
      // OOS adds its empty SPA root hash during native hydration; resource,
      // region, host and path must stay exact. No resource-changing hash allowed.
      if(current.origin!==initial.origin||current.pathname!==initial.pathname)
        throw Error('OOS template route changed');
    };
    const readView=async()=>{
      await assertConsole();
      const fields=[];let loading=false;
      for(const [title,marker] of [['模板名称',expected.identifier],['模板描述',expected.description]]){
        const label=frame.locator('.oos-descriptions-item-label').filter({hasText:new RegExp(`^${title}$`)});
        if(await label.count()!==1||!await label.isVisible())return {loading,labeled_fields:false,fields};
        const column=await label.evaluate(oosDescriptionColumn);
        const cell=label.locator('xpath=ancestor::tr[1]/following-sibling::tr[1]').locator('td').nth(column);
        if(await cell.count()!==1||!await cell.isVisible())return {loading,labeled_fields:false,fields};
        const view=await cell.evaluate(essTaskRowView,{markers:[marker],loadingSelector:LOADING_SELECTOR});
        fields.push(...view.fields);loading ||= view.loading;
      }
      return {loading,labeled_fields:true,fields};
    };
    await assertConsole();
    const stages=[{step:'original_template_detail_landing',url:initial.href}];
    const header=frame.getByText('基本信息',{exact:true});
    await header.waitFor({state:'visible',timeout:10000});
    if(await header.count()!==1)throw Error('OOS basic information disclosure ambiguous');
    // The actual native header has an angle-right icon only while collapsed.
    // Never toggle an already-expanded header or interact with execution/edit controls.
    const closed=header.locator('i.erc-icon-angle-right');
    if(await closed.count()===1){
      await assertConsole();await header.click();stages.push({step:'native_basic_information_expanded'});
    }
    let view=await readView();
    for(let i=0;i<20&&!oosTemplateMetadataReady(view,expected);i++){
      await surface.waitForTimeout(250);view=await readView();
    }
    const capture=await captureNavigationPixels(surface,file,{consoleFrame:frame,mode:options.mode,
      identifier:expected.identifier,markers:[expected.identifier,expected.description],
      beforeCapture:async()=>{view=await readView();}});
    return {kind:expected.kind,identifier:expected.identifier,template_id:expected.template_id,region:expected.region,
      source_tool_use_id:expected.source_tool_use_id,identity_visible:oosTemplateMetadataReady(view,expected)&&
        capture.foreground_visible===true,fields:view,screenshot:file,capture_sync:capture,stages,
      readonly_completed:true,original_landing_unchanged:true,cloud_writes:0};
  });
}
