import test from 'node:test';
import assert from 'node:assert/strict';
import {revealOriginalSourceCard} from '../../scripts/web/e2e/resource_link_workflow/browser.mjs';

for(const scenario of ['collapsed','expanded','no-owner','invalid','wrong-selector','missing','ambiguous','hidden-summary','failed-expand'])
  test(`original ROS card expansion is exact UI only: ${scenario}`,async()=>{
    let open=scenario==='expanded',clicks=0,turnOpen=false;
    const request={source_card_tool_use_id:'call_original',source_card_turn_id:'original-turn',selector:'details[data-tool-use-id="call_original"] a.resource-link'};
    if(scenario==='no-owner') delete request.source_card_tool_use_id;
    if(scenario==='invalid') request.source_card_tool_use_id='call_"other';
    if(scenario==='wrong-selector') request.selector='a.resource-link';
    const page={locator:selector=>{
      if(selector==='details.turn-process[data-open-key="turnproc:original-turn"]')return {
        count:async()=>1,getAttribute:async()=>turnOpen?'':null,
        locator:selector=>{assert.equal(selector,':scope > summary');return {
          count:async()=>1,isVisible:async()=>true,click:async()=>{turnOpen=true;},
        };},
      };
      assert.equal(selector,'details[data-tool-use-id="call_original"]');
      return {count:async()=>scenario==='missing'?0:scenario==='ambiguous'?2:1,
        getAttribute:async()=>open?'':null,
        locator:selector=>{assert.equal(selector,':scope > summary');return {
          count:async()=>1,isVisible:async()=>turnOpen&&scenario!=='hidden-summary',
          click:async()=>{clicks++;if(scenario!=='failed-expand')open=true;},
        };},
      };
    }};
    if(['collapsed','expanded','no-owner'].includes(scenario)){
      const result=await revealOriginalSourceCard(page,request);
      assert.equal(result.expanded,scenario==='collapsed');
      assert.equal(clicks,scenario==='collapsed'?1:0);
    }else await assert.rejects(()=>revealOriginalSourceCard(page,request));
  });
