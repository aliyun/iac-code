/** Only native scrolling of exact source-bound SAE application information fields. */
import {withForegroundLock,captureNavigationPixels,LOADING_SELECTOR,loadingSignalPending} from './browser.mjs';

export function saeApplicationConsoleUrl(raw,expected){
  if(expected.kind!=='sae_application_metadata'||expected.resource_type!=='ALIYUN::SAE::Application'||
      !/^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/.test(expected.identifier)||
      !/^cn-[a-z0-9-]+$/.test(expected.region)||!expected.name?.trim()||!expected.description?.trim()||
      !expected.source_tool_use_id)throw Error('invalid source-bound SAE application inspection');
  let url;try{url=new URL(raw);}catch{throw Error('unbound SAE application console');}
  if(url.protocol!=='https:'||!['saenext.console.aliyun.com','saenext4service.console.aliyun.com'].includes(url.hostname)||
      url.port||url.username||url.password||url.hash||
      url.pathname!==`/${expected.region}/app-list/${expected.identifier}/micro-app/base`||
      [...url.searchParams.keys()].some(key=>key!=='name')||url.searchParams.getAll('name').length>1||
      (url.searchParams.has('name')&&url.searchParams.get('name')!==expected.name))
    throw Error('unbound SAE application console');
  return url;
}

// This function runs in the actual document, and offline tests run this same probe.
// A title containing "desc" is never the distinct labeled description field.
export function saeApplicationFieldView(root,{expected,embeddingClip=null,loadingSelector}){
  const text=el=>el?.textContent?.trim();
  const visible=(node,value)=>{
    if(!node?.getClientRects().length)return false;
    let opacity=1,left=0,top=0,right=innerWidth,bottom=innerHeight;
    if(embeddingClip){left=Math.max(left,embeddingClip.left);top=Math.max(top,embeddingClip.top);
      right=Math.min(right,embeddingClip.right);bottom=Math.min(bottom,embeddingClip.bottom);}
    for(let el=node;el;el=el.parentElement){
      const style=getComputedStyle(el);opacity*=Number(style.opacity);
      if(style.display==='none'||style.visibility==='hidden')return false;
      const box=el.getBoundingClientRect();
      if(['hidden','clip','auto','scroll'].includes(style.overflowX)){left=Math.max(left,box.left);right=Math.min(right,box.right);}
      if(['hidden','clip','auto','scroll'].includes(style.overflowY)){top=Math.max(top,box.top);bottom=Math.min(bottom,box.bottom);}
    }
    if(opacity<0.8)return false;
    const walker=document.createTreeWalker(node,NodeFilter.SHOW_TEXT);let found=false;
    while(walker.nextNode()){
      const item=walker.currentNode;
      if(!item.textContent.trim())continue;
      // Exact full text of the bound field is also checked outside this probe.
      const range=document.createRange();range.selectNodeContents(item);
      const rects=[...range.getClientRects()].filter(r=>r.right>r.left&&r.bottom>r.top);
      if(!rects.length)return false;
      for(const rect of rects){
        if(rect.left<left||rect.right>right||rect.top<top||rect.bottom>bottom)return false;
        if(![0.01,0.5,0.99].every(x=>{const hit=document.elementFromPoint(rect.left+(rect.right-rect.left)*x,(rect.top+rect.bottom)/2);
          return hit&&(node===hit||node.contains(hit));}))return false;
      }
      found=true;
    }
    return found&&text(node)===value;
  };
  const values=[['应用名',expected.name],['应用ID',expected.identifier],['应用描述',expected.description]];
  const fields=values.map(([label,value])=>{
    const labels=[...root.querySelectorAll('.wind-rc-data-fields-item-label')].filter(el=>text(el)===label);
    const row=labels.length===1?labels[0].closest('.wind-rc-data-fields-item'):null;
    const nodes=row?[...row.querySelectorAll('.wind-rc-data-fields-item-value')]:[];
    const own=nodes.length===1&&text(nodes[0])===value;
    return {label,value,unique:labels.length===1&&nodes.length===1,matched:own,
      visible:own&&visible(labels[0],label)&&visible(nodes[0],value)};
  });
  const loading_signals=[...root.querySelectorAll(loadingSelector)].filter(el=>el.getClientRects().length&&
    getComputedStyle(el).display!=='none'&&getComputedStyle(el).visibility!=='hidden').map(el=>({
      role:el.getAttribute('role'),aria_busy:el.getAttribute('aria-busy'),value_now:el.getAttribute('aria-valuenow'),
      value_max:el.getAttribute('aria-valuemax'),class_name:el.className,text:el.textContent,
      explicit_loading:el.getAttribute('role')!=='progressbar'}));
  return {fields,loading_signals};
}

export function saeApplicationFieldsReady(view){
  return view.fields?.length===3&&view.fields.every(field=>field.unique&&field.matched&&field.visible)&&
    Array.isArray(view.loading_signals)&&!view.loading_signals.some(loadingSignalPending);
}

async function frameClip(frame,mode){
  if(mode!=='iframe')return null;
  const embedding=await frame.frameElement();
  try{return await embedding.evaluate(el=>{
    const r=el.getBoundingClientRect();let left=Math.max(0,r.left),top=Math.max(0,r.top);
    let right=Math.min(innerWidth,r.right),bottom=Math.min(innerHeight,r.bottom);
    for(let p=el.parentElement;p;p=p.parentElement){const s=getComputedStyle(p),b=p.getBoundingClientRect();
      if(['hidden','clip','auto','scroll'].includes(s.overflowX)){left=Math.max(left,b.left);right=Math.min(right,b.right);}
      if(['hidden','clip','auto','scroll'].includes(s.overflowY)){top=Math.max(top,b.top);bottom=Math.min(bottom,b.bottom);}}
    return {left:left-r.left-el.clientLeft,top:top-r.top-el.clientTop,
      right:right-r.left-el.clientLeft,bottom:bottom-r.top-el.clientTop};
  });}finally{await embedding.dispose();}
}

export async function inspectSaeApplicationMetadata(surface,frame,expected,file,options={}){
  if(!frame||!['iframe','open'].includes(options.mode))throw Error('SAE requires own clicked frame');
  const location=()=>frame.evaluate(()=>document.location.href);
  const initial=await location();saeApplicationConsoleUrl(initial,expected);
  return withForegroundLock(options.lockFile,async()=>{
    await surface.bringToFront();
    const assertConsole=async()=>{
      if(frame.isDetached())throw Error('SAE original frame detached');
      if(options.beforeCapture)await options.beforeCapture();
      const actual=await location();saeApplicationConsoleUrl(actual,expected);
      if(actual!==initial)throw Error('SAE original landing changed');
    };
    const view=async()=>frame.locator('body').evaluate(saeApplicationFieldView,
      {expected,embeddingClip:await frameClip(frame,options.mode),loadingSelector:LOADING_SELECTOR});
    await assertConsole();
    const deadline=Date.now()+10000;
    while(true){const current=await view();
      if(current.fields?.length===3&&current.fields.every(field=>field.unique&&field.matched))break;
      if(Date.now()>=deadline)throw Error('SAE exact labeled source fields missing or ambiguous');
      await assertConsole();await new Promise(resolve=>setTimeout(resolve,250));
    }
    const row=frame.locator('.wind-rc-data-fields-item').filter({has:frame.getByText('应用描述',{exact:true})});
    if(await row.count()!==1)throw Error('SAE description row ambiguous');
    // Only native scrolling. No edit/copy/action click, DOM/style mutation, resize or URL navigation.
    await row.scrollIntoViewIfNeeded({timeout:5000});
    const capture=await captureNavigationPixels(surface,file,{consoleFrame:frame,mode:options.mode,
      identifier:expected.identifier,markers:[expected.name,expected.description],beforeCapture:async()=>{
        await assertConsole();if(!saeApplicationFieldsReady(await view()))throw Error('SAE exact fields clipped or occluded');
      }});
    await assertConsole();
    const fields=await view();
    return {...expected,fields,screenshot:file,capture_sync:capture,
      identity_visible:saeApplicationFieldsReady(fields)&&capture.foreground_visible===true&&
        capture.render_check?.identity_visible===true&&(options.mode!=='iframe'||capture.frame_embedding?.visible===true),
      readonly_completed:true,original_landing_unchanged:true,cloud_writes:0};
  });
}
