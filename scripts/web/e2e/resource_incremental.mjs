// Real-DOM invariants for incremental updates, independent of wall-clock speed.
export async function verifyLastRequestRemoval(page) {
  return page.evaluate(async () => {
    const {links,rules,index}=window.resourceTest;
    const {renderBlockingPanels}=await import('/static/js/components/blocking.js?v=blocking-keys-v38');
    const {openResourceLinkPopover}=await import('/static/js/components/resource_link_popover.js?v=resource-links-v50');
    const fixture=document.getElementById('fixture');fixture.replaceChildren();
    const shell=document.createElement('div');fixture.append(shell);
    const controller=links.configureResourceLinks({index,rules,onActivate:(anchor,occurrence,resolution,options)=>
      openResourceLinkPopover(anchor,occurrence,resolution,{...options,index,rules})});
    try {
      shell.append(renderBlockingPanels({questions:{last:{requestId:'last',payload:{question:'RAM role LastRequestRole'}}}}));
      links.registerResourceText(shell,{source:{sourceId:'blocking-panel'},complete:true});
      await controller.whenIdle();
      const button=shell.querySelector('button.resource-link-pending');
      if(!button) {
        const probe=document.createElement('p');probe.textContent='RAM role LastRequestRole';
        links.decorateResourceLinks(probe,{index,rules});
        throw Error('last request name was not recognized: '+JSON.stringify({html:shell.innerHTML,probe:probe.innerHTML,
          session:index.sessionId,rules:rules.map(rule=>rule.resourceType)}));
      }
      links.removeResourceLinkDecorations(shell);
      if(!button.isConnected) throw Error('parent cleanup removed an independent request field');
      button.click();
      if(!document.querySelector('.resource-link-popover')) throw Error('request popover did not open');
      // No transcript/other registered source remains to wake the scheduler.
      shell.replaceChildren(renderBlockingPanels({}));
      links.registerResourceText(shell,{source:{sourceId:'blocking-panel'},complete:true});
      await controller.whenIdle();
      if(document.querySelector('.resource-link-popover')) throw Error('last request removal retained a stale popover');
      return {lastRequestRemoval:true};
    } finally {controller.dispose();fixture.replaceChildren();}
  });
}

export async function verifyIncrementalResourceLinks(page) {
  return page.evaluate(async () => {
    const {links, rules, role, instance} = window.resourceTest;
    const {ResourceIndex} = await import('/static/js/resource_index.js?v=resource-references-v11');
    const {canonicalResourceType} = await import('/static/js/resource_link_rules.js?v=resource-links-v49');
    const index = new ResourceIndex({canonicalType:canonicalResourceType});
    index.reset('incremental-session');
    const verifiedRules = rules.map(rule => rule.resourceType === instance.resourceType ? {...rule,
      build:ref=>`https://ram.console.aliyun.com/${encodeURIComponent(ref.regionId)}/${encodeURIComponent(ref.identifier)}`,
    } : rule);
    await index.loadSnapshot({schemaVersion:1,revision:1,items:[role,instance],bindings:[]},()=>{throw Error('unexpected query');});
    const controller = links.configureResourceLinks({index,rules:verifiedRules});
    const fixture = document.getElementById('fixture');fixture.replaceChildren();
    const root = document.createElement('pre');fixture.append(root);
    root.innerHTML = '<a href="https://example.test/original">'+instance.identifier+'</a>\n'+
      '<code>RoleName: <span>Deploy</span><span>Role</span>\n' +
      Array.from({length:40},()=>`InstanceId: ${instance.identifier}`).join('\n') +
      '\n11111111-2222-4333-8444-555555555555</code>';
    const original = root.textContent;
    const originalLink = root.querySelector('a');
    const source = {messageId:'incremental-message',sourcePath:'/text'};
    links.registerResourceText(root,{source,bindingValue:original,complete:true});
    const check = (value,message) => {if (!value) throw Error(message);};
    const nodes = () => [...root.querySelectorAll('[data-resource-link]')];
    const checkText = () => check(root.textContent===original && !root.querySelector('[data-resource-link] [data-resource-link]') &&
      root.querySelector('a[href="https://example.test/original"]')===originalLink && !originalLink.querySelector('[data-resource-link]'),
      'incremental reconciliation changed source text or nested a resource wrapper');
    const delta = (upserts,removedReferenceIds=[]) => index.applyDelta({schemaVersion:1,baseRevision:index.revision,
      revision:index.revision+1,upserts,removedReferenceIds,bindings:[]});
    try {
      await controller.whenIdle();check(nodes().length===42,'initial split name and IDs must all be decorated');
      const firstNodes = nodes();firstNodes[2].focus();
      const mutations=[];
      const observer = new MutationObserver(records=>mutations.push(...records));
      observer.observe(root,{childList:true,subtree:true,attributes:true});
      delta([{...instance,status:'UPDATE_COMPLETE'}]);await controller.whenIdle();
      check(nodes().every((node,i)=>node===firstNodes[i]),'a status-only update replaced existing resource DOM');
      check(mutations.length+observer.takeRecords().length===0,'unchanged links were written again');observer.disconnect();
      check(document.activeElement===firstNodes[2],'a status-only update lost focus');checkText();
      controller.invalidate(root);await controller.whenIdle();
      check(nodes().every((node,i)=>node===firstNodes[i]),'same-version invalidation rebuilt links');

      const moved = {...instance,regionId:'cn-beijing'};
      delta([moved]);await controller.whenIdle();
      check(nodes().every((node,i)=>node===firstNodes[i]),'a changed URL replaced its anchor rather than updating it');
      check(firstNodes[2].href===`https://ram.console.aliyun.com/cn-beijing/${instance.identifier}`,'anchor URL was not updated');
      const other = {...moved,referenceId:'ref-incremental-other',accountScope:'other-account'};
      delta([other]);
      const stale = new MouseEvent('click',{bubbles:true,cancelable:true});firstNodes[2].dispatchEvent(stale);
      check(stale.defaultPrevented,'a stale anchor navigated before ambiguity was rendered');
      await controller.whenIdle();
      check(root.querySelectorAll('button.resource-link-pending').length===40,'new ambiguity did not retract every matching link');
      check(nodes()[0]===firstNodes[0] && nodes()[1]===firstNodes[1],'ID ambiguity rebuilt the unrelated split name');
      checkText();
      delta([], [other.referenceId]);await controller.whenIdle();
      check(root.querySelectorAll('a.resource-link').length===42,'removing ambiguity did not restore anchors');
      let orphanBlocked=false;
      firstNodes[2].addEventListener('click',event=>{orphanBlocked=event.defaultPrevented;event.preventDefault();},{once:true});
      firstNodes[2].dispatchEvent(new MouseEvent('click',{bubbles:true,cancelable:true}));
      check(orphanBlocked,'a detached pre-reconciliation anchor could still navigate');
      const idNodes = nodes().slice(2);
      const range = document.createRange();range.selectNodeContents(root);
      const selection = window.getSelection();selection.removeAllRanges();selection.addRange(range);
      delta([{...role,status:'DELETE_COMPLETE'}]);
      await new Promise(requestAnimationFrame);await new Promise(requestAnimationFrame);
      check(selection.toString()===original && root.querySelectorAll('a.resource-link').length===42,
        'an update changed selected text or replaced DOM while copying');
      selection.removeAllRanges();await controller.whenIdle();
      check(root.querySelectorAll('button.resource-link-pending').length===2,'deleted split name was not withdrawn');
      check(nodes().slice(2).every((node,i)=>node===idNodes[i]),'name deletion rebuilt unrelated IDs');checkText();

      const stack = {...moved,resourceType:'ALIYUN::ROS::Stack',identifierField:'StackId',referenceId:'ref-incremental-stack',
        identifier:'11111111-2222-4333-8444-555555555555',sources:[{messageId:'incremental-message'}]};
      delta([stack]);await controller.whenIdle();
      check(nodes().at(-1).textContent===stack.identifier && nodes().at(-1).tagName==='A','new fact did not backfill an unrecognized UUID');
      check(nodes().slice(2,42).every((node,i)=>node===idNodes[i]),'backfill replaced existing unrelated IDs');
      delta([], [stack.referenceId]);await controller.whenIdle();
      check(nodes().length===42,'removed evidence left an unsupported UUID wrapper');checkText();
      const sentence = document.createElement('p');sentence.textContent='RAM role DeployRole.';fixture.append(sentence);
      links.registerResourceText(sentence,{source:{messageId:'changing-span'},complete:true});await controller.whenIdle();
      check(sentence.querySelector('[data-resource-link]')?.textContent==='DeployRole','known shorter name is missing');
      const longName = {...role,referenceId:'ref-period-name',identifier:'DeployRole.',sources:[]};
      delta([longName]);await controller.whenIdle();
      const expanded=[...sentence.querySelectorAll('a.resource-link')];
      check(expanded.map(node=>node.textContent).join('')==='DeployRole.' &&
        expanded[0]?.getAttribute('aria-label')==='DeployRole.' &&
        expanded.every(node=>node.href==='https://ram.console.aliyun.com/roles/DeployRole.') &&
        !sentence.querySelector('[data-resource-link] [data-resource-link]'),'expanded occurrence lost its range or nested the old match');
      delta([], [longName.referenceId]);await controller.whenIdle();
      check(sentence.querySelector('button.resource-link-pending')?.textContent==='DeployRole' &&
        sentence.textContent==='RAM role DeployRole.','shrinking an occurrence lost its punctuation or old lifecycle');
      const alias = document.createElement('p');alias.textContent='123456789';fixture.append(alias);
      links.registerResourceText(alias,{source:{messageId:'changing-alias'},complete:true});await controller.whenIdle();
      check(!alias.querySelector('[data-resource-link]'),'unproven numeric word was recognized');
      const aliasFact = {...role,referenceId:'ref-alias-probe',identifier:'AliasProbe',sources:[{messageId:'changing-alias'}],aliases:[{
        identifier:'123456789',identifierKind:'id',identifierField:'RoleId',
      }]};
      delta([aliasFact]);await controller.whenIdle();
      check(alias.querySelector('a')?.href==='https://ram.console.aliyun.com/roles/AliasProbe','new stable ID alias did not backfill');
      delta([{...aliasFact,aliases:[]}]);await controller.whenIdle();
      check(!alias.querySelector('[data-resource-link]') && alias.textContent==='123456789','revoked alias retained a stale wrapper');
      root.textContent='No longer the original source';links.registerResourceText(root,{source,bindingValue:root.textContent,complete:true});
      await controller.whenIdle();check(nodes().length===0,'replacement source retained old decorations');
      return {incrementalDOM:true,incrementalRetraction:true,incrementalBackfill:true,incrementalSelection:true,
        incrementalSpanChange:true,incrementalAlias:true};
    } finally {window.getSelection().removeAllRanges();controller.dispose();fixture.replaceChildren();}
  });
}

// Control only the scheduler clock/frames, not the DOM or resource modules.
// This deterministically interrupts a partially applied update on fast machines.
export async function verifyResourceSchedulerCancellation(page) {
  return page.evaluate(async () => {
    const {links, rules, instance} = window.resourceTest;
    const {ResourceIndex} = await import('/static/js/resource_index.js?v=resource-references-v11');
    const {canonicalResourceType} = await import('/static/js/resource_link_rules.js?v=resource-links-v49');
    const index = new ResourceIndex({canonicalType:canonicalResourceType});
    index.reset('scheduler-session');
    await index.loadSnapshot({schemaVersion:1,revision:1,items:[instance],bindings:[]},()=>{throw Error('unexpected query');});
    const originalFrame = window.requestAnimationFrame, originalCancel = window.cancelAnimationFrame;
    const originalNow = Object.getOwnPropertyDescriptor(performance,'now');
    const frames = new Map();let clock=0,frameId=0,controller;
    window.requestAnimationFrame=callback=>{frames.set(++frameId,callback);return frameId;};
    window.cancelAnimationFrame=id=>frames.delete(id);
    Object.defineProperty(performance,'now',{configurable:true,value:()=>clock+=5});
    const fixture = document.getElementById('fixture');fixture.replaceChildren();
    const root=document.createElement('pre');fixture.append(root);
    const original=Array.from({length:300},()=>`InstanceId: ${instance.identifier}`).join('\n');
    root.textContent=original;
    const source={messageId:'scheduler-message',sourcePath:'/text'};
    const check=(condition,message)=>{if(!condition) throw Error(message);};
    const pump=()=>{
      const entry=frames.entries().next().value;check(entry,'expected queued decoration work');
      frames.delete(entry[0]);entry[1](clock);
    };
    const until=predicate=>{
      for(let count=0;!predicate();count++) {
        check(count<10000,'decoration did not converge');pump();
      }
    };
    const settle=()=>until(()=>!frames.size);
    const delta=(upserts,removedReferenceIds=[])=>index.applyDelta({schemaVersion:1,baseRevision:index.revision,
      revision:index.revision+1,upserts,removedReferenceIds,bindings:[]});
    const nodes=selector=>[...root.querySelectorAll(selector)];
    const invariant=()=>check(root.textContent===original &&
      !root.querySelector('[data-resource-link] [data-resource-link]'),'cancelled update changed text or nested wrappers');
    const blocked=node=>{
      let prevented=false;
      node.addEventListener('click',event=>{prevented=event.defaultPrevented;event.preventDefault();},{once:true});
      node.dispatchEvent(new MouseEvent('click',{bubbles:true,cancelable:true}));
      return prevented;
    };
    try {
      controller=links.configureResourceLinks({index,rules});
      links.registerResourceText(root,{source,bindingValue:original,complete:true});
      until(()=>nodes('a.resource-link').length>0);
      check(nodes('a.resource-link').length<300 && frames.size,'test must interrupt a partial initial decoration');
      const initialAnchor=nodes('a.resource-link')[0];
      const other={...instance,referenceId:'ref-scheduler-other',accountScope:'other-account'};
      delta([other]);check(blocked(initialAnchor),'cancelled initial plan left a navigable stale anchor');
      until(()=>nodes('button.resource-link-pending').length>0);
      check(nodes('button.resource-link-pending').length<300 && frames.size,'test must interrupt a partial ambiguity update');
      delta([], [other.referenceId]);settle();await controller.whenIdle();
      check(nodes('a.resource-link').length===300 && nodes('button').length===0,'a newer delta did not supersede partial ambiguity');
      invariant();

      delta([other]);until(()=>nodes('button.resource-link-pending').length>0);
      check(nodes('a.resource-link').length>0 && frames.size,'source replacement must happen during DOM application');
      const staleAnchor=nodes('a.resource-link')[0];
      root.textContent='replacement source';
      links.registerResourceText(root,{source,bindingValue:root.textContent,complete:true});
      settle();await controller.whenIdle();
      check(root.textContent==='replacement source' && !root.querySelector('[data-resource-link]'),
        'queued work wrote into a replacement source');
      check(blocked(staleAnchor),'removed source retained an active old anchor');

      root.textContent=original;links.registerResourceText(root,{source,bindingValue:original,complete:true});
      until(()=>nodes('button.resource-link-pending').length>0);
      check(nodes('button.resource-link-pending').length<300 && frames.size,'generation reset must cancel unfinished work');
      index.reset('replacement-session');
      const afterReset=root.innerHTML;settle();await controller.whenIdle();
      check(root.innerHTML===afterReset,'old generation continued changing the DOM');
      await index.loadSnapshot({schemaVersion:1,revision:1,items:[instance],bindings:[]},()=>{throw Error('unexpected query');});
      links.registerResourceText(root,{source,bindingValue:original,complete:true});settle();await controller.whenIdle();
      check(nodes('a.resource-link').length===300,'new generation could not reconcile partial old decorations');invariant();

      delta([other]);until(()=>nodes('button.resource-link-pending').length>0);
      const afterDispose=root.innerHTML;controller.dispose();
      check(frames.size===0,'dispose left a scheduled frame');
      delta([], [other.referenceId]);
      check(frames.size===0 && root.innerHTML===afterDispose,'disposed controller reacted to new facts');
      return {incrementalCancellation:true,incrementalGeneration:true,incrementalDispose:true};
    } finally {
      controller?.dispose();fixture.replaceChildren();
      window.requestAnimationFrame=originalFrame;window.cancelAnimationFrame=originalCancel;
      if(originalNow) Object.defineProperty(performance,'now',originalNow);else delete performance.now;
    }
  });
}

// Keep the exact resource-link node stable for the duration of a pointer
// gesture. A large transcript may still have cooperative reconciliation work
// queued; replacing the hovered node between pointerdown and click loses a
// trusted Shift-click before the browser can open its new tab.
export async function verifyResourceSchedulerInteractionPause(page) {
  return page.evaluate(async () => {
    const {links, rules, instance} = window.resourceTest;
    const {ResourceIndex} = await import('/static/js/resource_index.js?v=resource-references-v11');
    const {canonicalResourceType} = await import('/static/js/resource_link_rules.js?v=resource-links-v49');
    const index = new ResourceIndex({canonicalType:canonicalResourceType});
    index.reset('scheduler-pointer-session');
    await index.loadSnapshot({schemaVersion:1,revision:1,items:[instance],bindings:[]},()=>{throw Error('unexpected query');});
    const originalFrame = window.requestAnimationFrame, originalCancel = window.cancelAnimationFrame;
    const originalNow = Object.getOwnPropertyDescriptor(performance,'now');
    const frames = new Map();let clock=0,frameId=0,controller;
    window.requestAnimationFrame=callback=>{frames.set(++frameId,callback);return frameId;};
    window.cancelAnimationFrame=id=>frames.delete(id);
    Object.defineProperty(performance,'now',{configurable:true,value:()=>clock+=5});
    const fixture=document.getElementById('fixture');fixture.replaceChildren();
    const root=document.createElement('pre');fixture.append(root);
    const original=Array.from({length:300},()=>`InstanceId: ${instance.identifier}`).join('\n');
    root.textContent=original;
    const source={messageId:'scheduler-pointer-message',sourcePath:'/text'};
    const check=(condition,message)=>{if(!condition) throw Error(message);};
    const pump=()=>{
      const entry=frames.entries().next().value;check(entry,'expected queued decoration work');
      frames.delete(entry[0]);entry[1](clock);
    };
    const until=predicate=>{
      for(let count=0;!predicate();count++) {check(count<10000,'decoration did not converge');pump();}
    };
    const settle=()=>until(()=>!frames.size);
    try {
      controller=links.configureResourceLinks({index,rules});
      links.registerResourceText(root,{source,bindingValue:original,complete:true});
      until(()=>root.querySelector('a.resource-link'));
      check(root.querySelectorAll('a.resource-link').length<300 && frames.size,
        'test must hover while initial decoration is unfinished');
      const hovered=root.querySelector('a.resource-link');
      hovered.dispatchEvent(new PointerEvent('pointerdown',{bubbles:true}));
      check(frames.size===0,'pointerdown did not cancel queued reconciliation');

      const other={...instance,referenceId:'ref-pointer-other',accountScope:'other-account'};
      index.applyDelta({schemaVersion:1,baseRevision:index.revision,revision:index.revision+1,
        upserts:[other],removedReferenceIds:[],bindings:[]});
      check(frames.size===0 && hovered.isConnected && root.querySelector('a.resource-link')===hovered,
        'hovered resource link changed before the pointer gesture completed');

      let delivered=false;
      hovered.addEventListener('click',event=>{delivered=event.shiftKey && hovered.isConnected;},{once:true});
      hovered.dispatchEvent(new MouseEvent('click',{bubbles:true,cancelable:true,shiftKey:true}));
      check(delivered && hovered.isConnected,'Shift-click was not delivered to the stable hovered link');
      check(frames.size===0,'reconciliation resumed inside the click dispatch');
      await new Promise(resolve=>setTimeout(resolve,0));
      check(frames.size>0,'click did not resume queued reconciliation');
      settle();await controller.whenIdle();
      check(root.querySelectorAll('button.resource-link-pending').length===300,
        'resumed reconciliation did not apply the newer ambiguous state');
      check(root.textContent===original && !root.querySelector('[data-resource-link] [data-resource-link]'),
        'pointer pause changed source text or nested wrappers');
      return {schedulerPointerPause:true};
    } finally {
      controller?.dispose();fixture.replaceChildren();
      window.requestAnimationFrame=originalFrame;window.cancelAnimationFrame=originalCancel;
      if(originalNow) Object.defineProperty(performance,'now',originalNow);else delete performance.now;
    }
  });
}
