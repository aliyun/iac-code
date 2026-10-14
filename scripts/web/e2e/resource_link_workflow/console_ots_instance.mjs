/** Read-only native OTS endpoint disclosure. No URL, viewport or pixel changes. */
import {withForegroundLock,captureNavigationPixels,LOADING_SELECTOR} from './browser.mjs';
import {routerInterfaceRowView} from './console_router_interface.mjs';

export function otsInstanceRouteMatches(url,expected) {
  try {const u=new URL(url);return u.protocol==='https:'&&!u.username&&!u.password&&!u.port&&
    ['ots.console.aliyun.com','ots4service.console.aliyun.com'].includes(u.hostname)&&
    u.pathname===`/${expected.region}/${expected.identifier}/list`;
  }catch{return false;}
}

// Execute only inside the same live console frame. Input values do not appear in
// innerText: explicitly require their entire original value to be painted in view.
export function otsEndpointView({identifier,public_endpoint,loadingSelector}) {
  const visible=e=>{
    const r=e.getBoundingClientRect();let opacity=1;
    for(let p=e;p;p=p.parentElement){const s=getComputedStyle(p);opacity*=Number(s.opacity);
      if(s.display==='none'||s.visibility==='hidden')return false;}
    const hit=document.elementFromPoint((r.left+r.right)/2,(r.top+r.bottom)/2);
    return e.getClientRects().length>0&&opacity>=0.8&&r.left>=0&&r.top>=0&&
      r.right<=innerWidth&&r.bottom<=innerHeight&&(e===hit||e.contains(hit));
  };
  const inputs=[...document.querySelectorAll('input')].filter(e=>e.value===public_endpoint);
  const endpoint=inputs.length===1?inputs[0]:null;
  const painted=e=>!!e&&visible(e)&&e.scrollLeft===0&&e.scrollWidth<=e.clientWidth+1;
  return {endpoint:{value:endpoint?.value,visible:painted(endpoint),count:inputs.length,
      client_width:endpoint?.clientWidth,scroll_width:endpoint?.scrollWidth},
    loading:[...document.querySelectorAll(loadingSelector)].some(visible)};
}

export function otsEndpointReady(view,expected) {
  return !!expected.source_tool_use_id&&view.endpoint?.value===expected.public_endpoint&&
    view.name?.value===expected.identifier&&view.endpoint.count===1&&view.name.count===1&&
    view.endpoint.visible===true&&view.name.visible===true&&view.loading===false;
}

export async function inspectOtsInstanceEndpoint(surface,frame,expected,file,options={}) {
  if(!frame||expected.kind!=='ots_instance_endpoint'||expected.resource_type!=='ALIYUN::OTS::Instance'||
    !/^[a-z][a-z0-9-]{1,62}$/.test(expected.identifier)||!/^cn-[a-z0-9-]+$/.test(expected.region)||
    expected.public_endpoint!==`https://${expected.identifier}.${expected.region}.ots.aliyuncs.com`||
    !expected.source_tool_use_id)throw Error('unbound OTS instance endpoint');
  const location=()=>frame.evaluate(()=>document.location.href),initial=await location();
  if(!otsInstanceRouteMatches(initial,expected))throw Error('wrong OTS route');
  return withForegroundLock(options.lockFile,async()=>{
    const check=async()=>{
      if(frame.isDetached())throw Error('OTS frame detached');
      if(options.beforeCapture)await options.beforeCapture();
      if(await location()!==initial)throw Error('OTS landing changed');
    };
    const read=async()=>{
      const view=await frame.evaluate(otsEndpointView,{...expected,loadingSelector:LOADING_SELECTOR});
      const text=await frame.locator('body').evaluate(routerInterfaceRowView,{markers:[expected.identifier]});
      return {...view,name:{value:expected.identifier,count:text.fields[0]?.visible===true?1:0,
        visible:text.fields[0]?.visible===true}};
    };
    let toggled=false,view,restore=false,result;
    const stages=[];
    try{
      await surface.bringToFront();await check();view=await read();
      if(!otsEndpointReady(view,expected)){
        const toggle=frame.locator('span.nav-collapse-trigger');
        if(await toggle.count()!==1||!await toggle.isVisible())throw Error('native OTS collapse unavailable');
        await toggle.click({timeout:5000});toggled=true;stages.push({step:'native_navigation_toggle'});
      }
      const capture=await captureNavigationPixels(surface,file,{consoleFrame:frame,mode:options.mode,
        identifier:expected.identifier,markers:[expected.identifier,expected.public_endpoint],beforeCapture:async()=>{
          await check();view=await read();if(!otsEndpointReady(view,expected))throw Error('OTS full endpoint clipped');
        }});
      result={...expected,fields:view,screenshot:file,capture_sync:capture,stages,
        identity_visible:otsEndpointReady(view,expected)&&capture.foreground_visible===true&&
          (options.mode!=='iframe'||capture.frame_embedding?.visible===true),cloud_writes:0};
    }finally{
      await check();if(toggled)await frame.locator('span.nav-collapse-trigger').click({timeout:5000});
      restore=true;
    }
    return {...result,native_navigation_restored:restore,readonly_completed:true,original_landing_unchanged:true};
  });
}
