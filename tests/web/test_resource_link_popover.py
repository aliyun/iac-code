"""Offline popover interactions with native select value matching semantics."""

import json
import subprocess
from pathlib import Path

import pytest

from iac_code.web.resource_link_catalog import resource_link_catalog_payload

STATIC = Path(__file__).parents[2] / "src/iac_code/web/static/js"


@pytest.mark.parametrize("preloaded", [False, True])
def test_legacy_fact_can_explicitly_load_account_and_complete_parent(tmp_path, preloaded):
    script = tmp_path / "popover.mjs"
    script.write_text(
        "import assert from 'node:assert/strict';\n"
        + "globalThis.__IAC_RESOURCE_LINKS__=" + json.dumps(resource_link_catalog_payload()) + ";\n"
        + "const {ResourceIndex,loadResourceAccountContext}=await import("
        + json.dumps((STATIC / "resource_index.js").as_uri()) + ");\n"
        + "const {resolveResourceOccurrence}=await import("
        + json.dumps((STATIC / "resource_link_rules.js").as_uri()) + ");\n"
        + "const {openResourceLinkPopover,closeResourceLinkPopover}=await import("
        + json.dumps((STATIC / "components/resource_link_popover.js").as_uri()) + ");\n"
        + "const preloaded=" + json.dumps(preloaded) + ";\n"
        + r"""
class Element {
  constructor(tag) {this.tagName=tag.toUpperCase();this.children=[];this.listeners={};this.dataset={};
    this.style={};this._value='';this.hidden=false;this.disabled=false;}
  append(...nodes) {for(const node of nodes){node.parent=this;this.children.push(node);}}
  replaceChildren(...nodes) {for(const node of this.children)node.parent=null;this.children=[];this.append(...nodes);}
  set value(value) {this._value=this.tagName==='SELECT'&&!this.children.some(node=>node.value===value)?'':value;}
  get value() {return this._value;}
  setAttribute(name,value) {this[name]=value;}
  removeAttribute(name) {delete this[name];}
  addEventListener(name,fn) {this.listeners[name]=fn;}
  remove() {if(this.parent)this.parent.children=this.parent.children.filter(node=>node!==this);this.parent=null;}
  get isConnected() {return this===doc.body||Boolean(this.parent?.isConnected);}
  focus() {}
  getBoundingClientRect() {return {left:10,bottom:20};}
}
const doc={body:new Element('body'),createElement:tag=>new Element(tag),
  documentElement:{clientWidth:1200,clientHeight:900},addEventListener(){},removeEventListener(){},
  defaultView:{addEventListener(){},removeEventListener(){}}};
const walk=node=>[node,...node.children.flatMap(walk)];
const index=new ResourceIndex();index.reset('session-a');
const ref={referenceId:'ref-legacy',provider:'aliyun',resourceType:'ALIYUN::ALB::Listener',
  identifier:'lsn-offline123456',identifierKind:'id',identifierField:'ListenerId',scopeKind:'regional',
  regionId:'cn-hangzhou',attributes:{},sources:[{toolUseId:'legacy-tool'}]};
await index.loadSnapshot({schemaVersion:1,revision:1,items:[ref],bindings:[]});
const choice={accountContextId:'account-context:'+'a'.repeat(32),
  accountScope:'credential:fake',consolePartition:'aliyun'};
const load=isCurrent=>loadResourceAccountContext(index,{sessionId:'session-a',isCurrent,
  load:async()=>({status:'available',choice})});
if(preloaded)await load(()=>true);
const occurrence={...ref,semantic:{kind:'field',field:'ListenerId'},toolUseId:'legacy-tool',sourceKey:'source'};
const anchor=new Element('button');anchor.ownerDocument=doc;doc.body.append(anchor);
let chosen;
const open=()=>openResourceLinkPopover(anchor,occurrence,resolveResourceOccurrence(occurrence,index),
  {index,onChoose:(selected,resolution)=>{chosen={selected,resolution};return true;},loadAccountContext:load});
open();
let dialog=doc.body.children.at(-1);
const field=name=>walk(dialog).find(node=>node.name===name);
const accountButton=()=>walk(dialog).find(node=>node.dataset.resourceAccount);
assert.equal(field('accountScope').disabled,false,'inferred scope is not a locked observed account');
assert.equal(accountButton().hidden,false,'explicit credential loading remains available');
assert.equal(field('regionId').disabled,true,'the observed region remains locked');
await accountButton().listeners.click();
assert.equal(dialog.isConnected,true,'the account load must survive its own index notification');
assert.equal(field('accountScope').value,choice.accountScope);
assert.equal(field('consolePartition').value,'aliyun');
field('LoadBalancerId').value='alb-offline123456';field('LoadBalancerId').listeners.input();
const target=walk(dialog).find(node=>node.tagName==='A');
assert.equal(target.hidden,false);
assert.equal(new URL(target.href).hostname,'slb.console.aliyun.com');
await walk(dialog).find(node=>node.tagName==='BUTTON'&&node.textContent==='Select').listeners.click();
assert.equal(chosen.resolution.status,'resolved');
assert.equal(chosen.selected.accountContextId,choice.accountContextId);
assert.equal(chosen.selected.attributes.LoadBalancerId,'alb-offline123456');
assert.equal(index.references.get(ref.referenceId).accountScope,undefined,'completion does not rewrite observed facts');
assert.deepEqual(index.references.get(ref.referenceId).attributes,{});

// Real observed identity remains locked, unlike the configured default above.
await index.loadSnapshot({schemaVersion:1,revision:2,items:[{...ref,...choice,
  attributes:{LoadBalancerId:'alb-offline123456'}}],bindings:[]});
open();dialog=doc.body.children.at(-1);
for(const name of ['accountScope','consolePartition','regionId','LoadBalancerId'])
  assert.equal(field(name).disabled,true);
assert.equal(accountButton().hidden,true);
closeResourceLinkPopover();

// A generation change still invalidates a pending explicit account load.
await index.loadSnapshot({schemaVersion:1,revision:3,items:[ref],bindings:[]});
let done;
openResourceLinkPopover(anchor,occurrence,resolveResourceOccurrence(occurrence,index),{index,
  loadAccountContext:isCurrent=>loadResourceAccountContext(index,{sessionId:'session-a',isCurrent,
    load:()=>new Promise(resolve=>{done=resolve;})})});
dialog=doc.body.children.at(-1);
const pending=accountButton().listeners.click();
index.reset('session-b');done({status:'available',choice});await pending;
assert.equal(dialog.isConnected,false);
assert.equal(index.accountContext,null);
""", encoding="utf-8",
    )
    result = subprocess.run(["node", str(script)], capture_output=True, text=True, encoding="utf-8", timeout=20)
    assert result.returncode == 0, result.stderr
