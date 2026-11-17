/** Preserve landing pixels, close only the known native announcement, then capture own identity. */
import {withForegroundLock,captureNavigationPixels} from './browser.mjs';

export const ROCKETMQ_NOTICE_TITLE='RocketMQ for AI：全新轻量主题模型上线';

export function rocketmqNoticeUrl(raw,expected){
  if(expected.kind!=='rocketmq_instance_notice'||expected.resource_type!=='ALIYUN::ROCKETMQ::Instance'||
      !['ROS','QUERY'].includes(expected.dimension)||!/^\d{16}$/.test(expected.account)||
      !new RegExp('^MQ_INST_'+expected.account+'_[A-Za-z0-9]+$').test(expected.identifier)||
      !/^cn-[a-z0-9-]+$/.test(expected.region)||!expected.name||!expected.source_tool_use_id)
    throw Error('invalid signed RocketMQ notice binding');
  const url=new URL(raw);
  if(url.protocol!=='https:'||!['ons.console.aliyun.com','ons4service.console.aliyun.com'].includes(url.hostname)||
      url.port||url.username||url.password||url.search||url.hash||
      url.pathname!==`/region/${expected.region}/instance/${expected.identifier}/detail`)
    throw Error('unbound RocketMQ native detail');
  return url;
}

export async function inspectRocketmqNotice(surface,frame,expected,file,options={}){
  if(!frame||options.mode!=='open')throw Error('RocketMQ announcement recovery is direct-open only');
  const location=()=>frame.evaluate(()=>document.location.href);
  const initial=await location();rocketmqNoticeUrl(initial,expected);
  return withForegroundLock(options.lockFile,async()=>{
    await surface.bringToFront();
    const assertConsole=async()=>{
      if(frame.isDetached())throw Error('RocketMQ frame detached');
      if(options.beforeCapture)await options.beforeCapture();
      const current=await location();rocketmqNoticeUrl(current,expected);
      if(current!==initial)throw Error('RocketMQ detail changed');
    };
    await assertConsole();
    const dialog=frame.locator('[role="dialog"],.next-dialog').filter({
      has:frame.getByText(ROCKETMQ_NOTICE_TITLE,{exact:true})});
    let dismissed=false;
    if(await dialog.count()){
      if(await dialog.count()!==1||!await dialog.isVisible())throw Error('RocketMQ announcement ambiguous');
      // Fusion also exposes the X icon as role=button, aria-label=关闭.
      // Bind the one real text button; do not confuse that second control with
      // the intended footer action or click a global close elsewhere.
      const close=dialog.locator('button').filter({hasText:/^关闭$/});
      if(await close.count()!==1||!await close.isVisible()||!await close.isEnabled())
        throw Error('unique native announcement close required');
      await close.click();await dialog.waitFor({state:'hidden',timeout:4000});dismissed=true;
    }
    await assertConsole();
    const capture=await captureNavigationPixels(surface,file,{consoleFrame:frame,mode:options.mode,
      identifier:expected.identifier,markers:[expected.name],beforeCapture:assertConsole});
    return {...expected,screenshot:file,capture_sync:capture,announcement_dismissed:dismissed,
      identity_visible:capture.foreground_visible===true&&capture.render_check?.identity_visible===true,
      readonly_completed:true,original_landing_unchanged:true,cloud_writes:0};
  });
}
