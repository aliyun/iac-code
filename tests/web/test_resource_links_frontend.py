"""Pure recognizer and URL safety checks complement the real browser audit."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from iac_code.tools.cloud.aliyun.resource_reference_extractors import extract_api_resource_references
from iac_code.tools.cloud.resource_references import resource_spec
from iac_code.web.resource_link_catalog import resource_link_catalog, resource_link_catalog_payload
from iac_code.web.resource_references import ResourceReferenceStore, resource_fact_to_web

STATIC = Path(__file__).parents[2] / "src/iac_code/web/static/js"
CATALOG_BOOTSTRAP = (
    "globalThis.__IAC_RESOURCE_LINKS__=" + json.dumps(resource_link_catalog_payload(), ensure_ascii=False) + ";\n"
)


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["api", "ros"])
async def test_cms_spaced_contact_group_survives_api_storage_and_link_resolution(tmp_path, source):
    name = "ack_Default Contact Group"
    facts = extract_api_resource_references(
        product="cms", action="DescribeContactGroupList", params={},
        body={"ContactGroupList": {"ContactGroup": [{"Name": name}]}},
        region_id="cn-hangzhou", tool_use_id="offline-cms", account_scope="fake-account", console_partition="aliyun",
    )
    assert len(facts) == 1
    assert facts[0]["identifier"] == name
    assert facts[0]["identifier_field"] == "PhysicalResourceId"
    assert {"identifier": name, "identifier_field": "Name", "identifier_kind": "name"} in facts[0]["aliases"]
    if source == "ros":
        from iac_code.tools.cloud.resource_references import stack_resource_reference

        facts = [stack_resource_reference(
            {"ResourceType": "ALIYUN::CMS::ContactGroup", "PhysicalResourceId": name},
            stack_id="offline-stack", region_id="cn-hangzhou", tool_use_id="offline-cms", source_path="/Resources/0",
            account_scope="fake-account", console_partition="aliyun",
        )]
    path = tmp_path / "references.json"
    store = ResourceReferenceStore(path)
    store.observe(facts)
    assert await store.flush()
    snapshot = ResourceReferenceStore(path).snapshot()
    assert len(snapshot["items"]) == 1
    script = tmp_path / "cms-spaced-name.mjs"
    script.write_text(
        CATALOG_BOOTSTRAP
        + "import assert from 'node:assert/strict';\n"
        + "const {ResourceIndex}=await import(" + json.dumps((STATIC / "resource_index.js").as_uri()) + ");\n"
        + "const {scanResourceOccurrences:scan,resourceToolContext}=await import("
        + json.dumps((STATIC / "resource_occurrences.js").as_uri()) + ");\n"
        + "const {resolveResourceOccurrenceAlternatives:resolve}=await import("
        + json.dumps((STATIC / "resource_linkify.js").as_uri()) + ");\n"
        + "const snapshot=" + json.dumps(snapshot, ensure_ascii=False) + ";\n"
        + "const source=" + json.dumps(source) + ";\n"
        + r"""
const index=new ResourceIndex();
await index.loadSnapshot(snapshot,()=>assert.fail('no network'));
const field=source==='ros'?'PhysicalResourceId':'Name';
const context=resourceToolContext({toolUseId:'offline-cms',toolName:'aliyun_api',input:{
  product:source==='ros'?'ros':'cms',action:source==='ros'?'ListStackResources':'DescribeContactGroupList'}});
const row=source==='ros'?{ResourceType:'ALIYUN::CMS::ContactGroup',PhysicalResourceId:'ack_Default Contact Group'}:
  {Name:'ack_Default Contact Group'};
for(const [text,options] of [[JSON.stringify(row),{}],['ack_Default Contact Group',{field,context:{
  ...context,resourceType:'ALIYUN::CMS::ContactGroup'}}]]) {
 const matches=scan(text,{index,context,...options});
assert.equal(matches.length,1);
assert.equal(matches[0].identifier,'ack_Default Contact Group');
const resolved=resolve(matches[0],index);
assert.equal(resolved.status,'resolved');
assert.equal(resolved.reference.referenceId,snapshot.items[0].referenceId);
assert.equal(new URL(resolved.url).hostname,'cloudmonitor.console.aliyun.com');
}
""", encoding="utf-8",
    )
    result = subprocess.run(["node", str(script)], capture_output=True, text=True, encoding="utf-8", timeout=20)
    assert result.returncode == 0, result.stderr


def test_unicode_api_name_alias_keeps_locator_kind_and_visible_owner(tmp_path):
    references = extract_api_resource_references(
        product="vpc", action="DescribeVpcs", params={"RegionId": "cn-hangzhou"},
        body={"Vpcs": {"Vpc": [{"VpcId": "vpc-offline123456", "VpcName": "生产网络", "RegionId": "cn-hangzhou"}]}},
        region_id="cn-hangzhou", tool_use_id="offline-tool", account_scope="fake-account", console_partition="aliyun",
    )
    script = tmp_path / "unicode-name-alias.mjs"
    script.write_text(
        CATALOG_BOOTSTRAP
        + "import assert from 'node:assert/strict';\n"
        + "const {ResourceIndex}=await import(" + json.dumps((STATIC / "resource_index.js").as_uri()) + ");\n"
        + "const {scanResourceOccurrences:scan}=await import("
        + json.dumps((STATIC / "resource_occurrences.js").as_uri()) + ");\n"
        + "const {resolveResourceOccurrenceAlternatives:resolve}=await import("
        + json.dumps((STATIC / "resource_linkify.js").as_uri()) + ");\n"
        + "const reference="
        + json.dumps({"referenceId": "ref-unicode", **resource_fact_to_web(references[0])}, ensure_ascii=False) + ";\n"
        + r"""
const index=new ResourceIndex();
await index.loadSnapshot({schemaVersion:1,revision:1,items:[reference],bindings:[]},()=>assert.fail());
for(const text of ['生产网络','VpcName: 生产网络']) {
  const occurrences=scan(text,{index,context:{toolUseId:'offline-tool'}});
  assert.equal(occurrences.length,1);
  const occurrence=occurrences[0];
  assert.equal(occurrence.identifier,'生产网络');
  assert.equal(occurrence.identifierField,'VpcName');
  assert.equal(occurrence.identifierKind,'name');
  const resolved=resolve(occurrence,index);
  assert.equal(resolved.status,'resolved');
  assert.equal(resolved.reference.referenceId,reference.referenceId);
  assert.ok(resolved.url.includes('vpc-offline123456'),'name alias resolves through its real ID');
}
assert.equal(scan('生产网络',{index,context:{toolUseId:'another-tool'}}).length,0);
assert.equal(scan('生产网络备份',{index,context:{toolUseId:'offline-tool'}}).length,0);
""", encoding="utf-8",
    )
    result = subprocess.run(["node", str(script)], capture_output=True, text=True, encoding="utf-8", timeout=20)
    assert result.returncode == 0, result.stderr


def test_context_only_delta_refreshes_dependent_links_without_touching_other_turns(tmp_path):
    script = tmp_path / "context-delta.mjs"
    script.write_text(
        CATALOG_BOOTSTRAP
        + "import assert from 'node:assert/strict';\n"
        + "const {ResourceIndex}=await import(" + json.dumps((STATIC / "resource_index.js").as_uri()) + ");\n"
        + "const {createResourceLinkController}=await import("
        + json.dumps((STATIC / "resource_linkify.js").as_uri()) + ");\n"
        + r"""
class Node {
  constructor(doc,tag,data=''){Object.assign(this,{ownerDocument:doc,nodeType:tag?1:3,
    tagName:tag?.toUpperCase(),data,childNodes:[],parentElement:null,attrs:new Map(),handlers:new Map()});}
  get isConnected(){return this===this.ownerDocument.body||Boolean(this.parentElement?.isConnected);}
  get children(){return this.childNodes.filter(n=>n.nodeType===1);}
  get textContent(){return this.nodeType===3?this.data:this.childNodes.map(n=>n.textContent).join('');}
  get firstChild(){return this.childNodes[0];}
  get nextSibling(){const peers=this.parentElement?.childNodes||[];return peers[peers.indexOf(this)+1];}
  hasAttribute(key){return this.attrs.has(key);}
  getAttribute(key){return this.attrs.get(key)??null;}
  setAttribute(key,value){this.attrs.set(key,String(value));}
  append(...nodes){for(const n of nodes){
    if(n.parentElement){const peers=n.parentElement.childNodes;peers.splice(peers.indexOf(n),1);}
    n.parentElement=this;this.childNodes.push(n);}}
  contains(node){return this===node||this.childNodes.some(n=>n.contains(node));}
  closest(selector){return selector==='tr'||selector==='td'?null:
    this.hasAttribute(selector.slice(1,-1))?this:this.parentElement?.closest(selector);}
  querySelectorAll(selector){const matches=n=>selector.startsWith('a[')?
    n.tagName==='A'&&n.hasAttribute('data-resource-auto-link'):n.hasAttribute(selector.slice(1,-1));
    return this.childNodes.flatMap(n=>[...(matches(n)?[n]:[]),...n.querySelectorAll(selector)]);}
  replaceWith(...nodes){const parent=this.parentElement,position=parent.childNodes.indexOf(this);
    parent.childNodes.splice(position,1);this.parentElement=null;
    for(const n of nodes){if(n.parentElement){const peers=n.parentElement.childNodes;peers.splice(peers.indexOf(n),1);}
      n.parentElement=parent;}parent.childNodes.splice(position,0,...nodes);}
  splitText(offset){const tail=new Node(this.ownerDocument,null,this.data.slice(offset));
    this.data=this.data.slice(0,offset);tail.parentElement=this.parentElement;
    const peers=this.parentElement.childNodes;peers.splice(peers.indexOf(this)+1,0,tail);return tail;}
  addEventListener(type,fn){this.handlers.set(type,fn);}
}
const frames=[];
globalThis.requestAnimationFrame=fn=>{frames.push(fn);return frames.length;};
globalThis.cancelAnimationFrame=()=>{};
const doc={getSelection:()=>null,createTreeWalker(){},createElement:tag=>new Node(doc,tag),
  addEventListener(){},removeEventListener(){}};
doc.body=new Node(doc,'body');globalThis.document=doc;
function drain(){let slices=0;while(frames.length){frames.shift()();assert.ok(++slices<1000);}}
const index=new ResourceIndex();index.reset('offline-session');
const controller=createResourceLinkController({index});
const roots=['turn-one','turn-two'].map(turnId=>{
  const root=new Node(doc,'div');root.append(new Node(doc,null,'i-offline123456'));doc.body.append(root);
  controller.register(root,{source:{turnId,messageId:turnId+'-message'},complete:true});return root;
});
drain();
const link=root=>root.querySelectorAll('[data-resource-link]')[0];
assert.equal(link(roots[0]).tagName,'BUTTON');
const untouched=link(roots[1]);
let unrelatedScans=0;
const originalContains=roots[1].contains.bind(roots[1]);
roots[1].contains=(node)=>{unrelatedScans++;return originalContains(node);};
const ref={provider:'aliyun',referenceId:'ref-vpc',resourceType:'ALIYUN::VPC::VPC',
  identifier:'vpc-offline123456',identifierField:'VpcId',identifierKind:'id',scopeKind:'regional',
  regionId:'cn-hangzhou',accountScope:'fake-account',consolePartition:'aliyun',
  attributes:{},aliases:[],sources:[{turnId:'turn-one'}]};
let revision=0;
function delta(upserts=[],removedReferenceIds=[]){
  assert.equal(index.applyDelta({schemaVersion:1,baseRevision:revision,revision:++revision,
    upserts,removedReferenceIds,bindings:[]}),'applied');drain();
}
delta([ref]);
assert.equal(link(roots[0]).tagName,'A','different-locator context refreshes before first click');
assert.ok(link(roots[0]).getAttribute('href').includes('cn-hangzhou'));
let prevented=false;
link(roots[0]).handlers.get('click')({button:0,shiftKey:false,metaKey:false,ctrlKey:false,
  stopPropagation(){},preventDefault(){prevented=true;}});
assert.equal(prevented,false,'first click uses the already reconciled URL');
delta([{...ref,regionId:'cn-beijing'}]);
assert.ok(link(roots[0]).getAttribute('href').includes('cn-beijing'));
delta([{...ref,referenceId:'ref-conflict',regionId:'cn-shenzhen'}]);
assert.equal(link(roots[0]).tagName,'BUTTON','conflicting context withdraws resolved navigation');
delta([],['ref-conflict']);
assert.equal(link(roots[0]).tagName,'A');
assert.equal(link(roots[1]),untouched);
assert.equal(unrelatedScans,0,'different-turn roots are not rescanned for these deltas');
delta([{...ref,sources:[{turnId:'turn-two'}]}]);
assert.equal(link(roots[0]).tagName,'BUTTON','previous owner also loses changed context');
assert.equal(link(roots[1]).tagName,'A');
const afterMoveScans=unrelatedScans;
delta([],['ref-vpc']);
assert.equal(link(roots[1]).tagName,'BUTTON');
assert.ok(unrelatedScans>afterMoveScans,'removed owner is refreshed');
await index.loadSnapshot({schemaVersion:1,revision:++revision,items:[ref],bindings:[]},()=>assert.fail());drain();
assert.equal(link(roots[0]).tagName,'A','context-only snapshots refresh dependents too');
controller.dispose();
for(const root of roots)assert.equal(root.textContent,'i-offline123456');
""", encoding="utf-8",
    )
    result = subprocess.run(["node", str(script)], capture_output=True, text=True, encoding="utf-8", timeout=20)
    assert result.returncode == 0, result.stderr


def test_preview_keyboard_cannot_answer_pending_permission(tmp_path):
    script = tmp_path / "preview-keyboard.mjs"
    script.write_text(
        "import fs from 'node:fs'; import assert from 'node:assert/strict';\n"
        + "const source=fs.readFileSync(" + json.dumps(str(STATIC / "components/blocking.js")) + ", 'utf-8');\n"
        + r"""
const start=source.indexOf('function isEditableTarget(');
const end=source.indexOf('\nif (typeof document',start);
let submitted=0, moved=0, previewOpen=false;
const panel={_permissionKeyControl:{hasChoices:true,submitSelected(){submitted++;},move(){moved++;}}};
const doc={querySelector:selector=>selector==='.resource-link-preview'?(previewOpen?{}:null):panel};
const handle=new Function('document','text',source.slice(start,end)+'\nreturn handlePermissionKeydown;')
  (doc,value=>String(value??''));
for (const selector of ['.resource-link-preview','.resource-link-popover','[data-resource-link]']) {
  for (const key of ['Enter','ArrowDown']) {
    const event={key,target:{tagName:'BUTTON',closest:selectors=>selectors.split(', ').includes(selector)?{}:null},
      preventDefault(){assert.fail('resource controls own their default keyboard action');}};
    handle(event);
  }
}
assert.equal(submitted,0);
assert.equal(moved,0);
previewOpen=true;
for(const key of ['Enter','ArrowDown'])
  handle({key,target:{tagName:'BODY',closest:()=>null},preventDefault(){}});
assert.equal(submitted,0,'a background focus target cannot answer through an open preview');
assert.equal(moved,0);
previewOpen=false;
handle({key:'Enter',target:{tagName:'BUTTON',closest:()=>null},preventDefault(){}});
assert.equal(submitted,1,'the normal permission shortcut is preserved');
""",
        encoding="utf-8",
    )
    result = subprocess.run(["node", str(script)], capture_output=True, text=True, encoding="utf-8", timeout=20)
    assert result.returncode == 0, result.stderr


def test_table_decoration_reads_field_records_once_per_pass(tmp_path):
    script = tmp_path / "table-context-cost.mjs"
    script.write_text(
        CATALOG_BOOTSTRAP
        + "import assert from 'node:assert/strict';\n"
        + "const {ResourceIndex}=await import(" + json.dumps((STATIC / "resource_index.js").as_uri()) + ");\n"
        + "const {decorateResourceLinks}=await import("
        + json.dumps((STATIC / "resource_linkify.js").as_uri()) + ");\n"
        + r"""
class Node {
 constructor(doc,tag,data=''){Object.assign(this,{ownerDocument:doc,nodeType:tag?1:3,
   tagName:tag?.toUpperCase(),data,childNodes:[],parentElement:null});}
 get children(){return this.childNodes.filter(n=>n.nodeType===1);}
 get firstChild(){return this.childNodes[0];}
 get nextSibling(){const nodes=this.parentElement?.childNodes||[];return nodes[nodes.indexOf(this)+1];}
 get textContent(){return this.nodeType===3?this.data:this.childNodes.map(n=>n.textContent).join('');}
 append(...nodes){for(const node of nodes){node.parentElement=this;this.childNodes.push(node);}}
 contains(node){return this===node||this.childNodes.some(child=>child.contains(node));}
 closest(tag){return this.tagName===tag.toUpperCase()?this:this.parentElement?.closest(tag);}
 hasAttribute(){return false;}
 querySelectorAll(selector){
   if(this.tagName==='TABLE'&&selector==='thead th')return this.children[0].children[0].children;
   if(this.tagName==='TABLE'&&selector==='tbody tr'){this.bodyReads++;return this.children[1].children;}
   return [];
 }
}
const doc={getSelection:()=>null};doc.body=new Node(doc,'body');doc.activeElement=doc.body;
const element=(tag,...children)=>{const node=new Node(doc,tag);node.append(...children);return node;};
const cell=(tag,value)=>element(tag,new Node(doc,null,value));
let recordItems=0,scopeWrites=0,contextCharacters=0;
const entries=Object.entries,stringify=JSON.stringify;
Object.entries=function(object){
 const result=entries(object);
 if(Object.hasOwn(object,'Property0'))recordItems+=result.length;
 return result;
};
JSON.stringify=function(value,...args){
 const result=stringify(value,...args);
 if(Array.isArray(value)&&value[0]==='table-record')scopeWrites++;
 if(value?.structuralScope?.startsWith('["table-record"'))contextCharacters+=result.length;
 return result;
};
for(const columns of [3,2]){
 const headers=columns===2?['Field','Value']:['A','B','C'];
 const tbody=element('tbody');
 for(let row=0;row<100;row++)tbody.append(element('tr',...headers.map((_,col)=>
   cell('td',col===0?'Property'+row:'ordinary value'))));
 const table=element('table',element('thead',element('tr',...headers.map(name=>cell('th',name)))),tbody);
 table.bodyReads=0;const root=element('div',table);doc.body.append(root);
 recordItems=0;scopeWrites=0;contextCharacters=0;
 decorateResourceLinks(root,{index:new ResourceIndex(),complete:true});
 assert.equal(table.bodyReads,columns===2?1:0,'no repeated full-table scan for each text node');
 assert.ok(recordItems<=400,'record context extraction is linear in the number of fields');
 assert.equal(scopeWrites,columns===2?1:0,'the full record scope is serialized once per pass');
 assert.ok(contextCharacters<10000,'context comparison must not repeatedly serialize the full record scope');
 tbody.children[0].children[1].firstChild.data='updated value';
 decorateResourceLinks(root,{index:new ResourceIndex(),complete:true});
 assert.equal(table.bodyReads,columns===2?2:0,'a later pass must read the changed table again');
}
""", encoding="utf-8",
    )
    result = subprocess.run(["node", str(script)], capture_output=True, text=True, encoding="utf-8", timeout=20)
    assert result.returncode == 0, result.stderr


def test_identical_markdown_blocks_keep_their_actual_product_scope(tmp_path):
    script = tmp_path / "duplicate-product-blocks.mjs"
    script.write_text(
        CATALOG_BOOTSTRAP
        + "import assert from 'node:assert/strict';\n"
        + "const rules=await import(" + json.dumps((STATIC / "resource_link_rules.js").as_uri()) + ");\n"
        + "const {ResourceIndex}=await import(" + json.dumps((STATIC / "resource_index.js").as_uri()) + ");\n"
        + "const {decorateResourceLinks}=await import(" + json.dumps((STATIC / "resource_linkify.js").as_uri()) + ");\n"
        + r"""
class Node {
  constructor(doc,tag,data=''){Object.assign(this,{ownerDocument:doc,nodeType:tag?1:3,
    tagName:tag?.toUpperCase(),data,childNodes:[],parentElement:null,attrs:new Map()});}
  get isConnected(){return this===this.ownerDocument.body||Boolean(this.parentElement?.isConnected);}
  get textContent(){return this.nodeType===3?this.data:this.childNodes.map(n=>n.textContent).join('');}
  get firstChild(){return this.childNodes[0];}
  get nextSibling(){const peers=this.parentElement?.childNodes||[];return peers[peers.indexOf(this)+1];}
  hasAttribute(key){return this.attrs.has(key);}
  getAttribute(key){return this.attrs.get(key)??null;}
  setAttribute(key,value){this.attrs.set(key,String(value));}
  append(...nodes){for(const n of nodes){n.parentElement=this;this.childNodes.push(n);}}
  contains(node){return this===node||this.childNodes.some(n=>n.contains(node));}
  closest(selector){return selector==='tr'||selector==='td'?null:
    this.hasAttribute(selector.slice(1,-1))?this:this.parentElement?.closest(selector);}
  querySelectorAll(selector){const matches=n=>selector.startsWith('a[')?
    n.tagName==='A'&&n.hasAttribute('data-resource-auto-link'):n.hasAttribute(selector.slice(1,-1));
    return this.childNodes.flatMap(n=>[...(matches(n)?[n]:[]),...n.querySelectorAll(selector)]);}
  replaceWith(...nodes){const parent=this.parentElement,position=parent.childNodes.indexOf(this);
    parent.childNodes.splice(position,1,...nodes);this.parentElement=null;for(const n of nodes)n.parentElement=parent;}
  splitText(offset){const tail=new Node(this.ownerDocument,null,this.data.slice(offset));
    this.data=this.data.slice(0,offset);tail.parentElement=this.parentElement;
    const peers=this.parentElement.childNodes;peers.splice(peers.indexOf(this)+1,0,tail);return tail;}
  addEventListener(){}
}
const doc={getSelection:()=>null,createElement:tag=>new Node(doc,tag)};
doc.body=new Node(doc,'body');const root=new Node(doc,'div');doc.body.append(root);
for(const [value,ignored] of [['产品: actiontrail | Action: DescribeTrails',false],['shared-name',false],
  ['产品: cms | Action: DescribeMetricRuleTemplateList',false],['shared-name',true],['shared-name',false]]){
  const p=new Node(doc,'p');if(ignored)p.setAttribute('data-resource-ignore','');
  p.append(new Node(doc,null,value));root.append(p);
}
const common={provider:'aliyun',identifier:'shared-name',identifierKind:'name',identifierField:'Name',
  scopeKind:'regional',regionId:'cn-hangzhou',accountScope:'fake-account',consolePartition:'aliyun',
  aliases:[],evidence:'api_response',sources:[{turnId:'turn-1'}]};
const refs=[{...common,referenceId:'ref-trail',resourceType:'ALIYUN::ACTIONTRAIL::Trail',
    attributes:{HomeRegion:'cn-hangzhou'}},
  {...common,referenceId:'ref-cms',resourceType:'ALIYUN::CMS::MetricRuleTemplate',identifier:'100000',
    identifierField:'PhysicalResourceId',identifierKind:'id',scopeKind:'account',attributes:{},
    aliases:[{identifier:'shared-name',identifierField:'TemplateName',identifierKind:'name'}]}];
const index=new ResourceIndex({canonicalType:rules.canonicalResourceType});
await index.loadSnapshot({schemaVersion:1,revision:1,items:refs,bindings:[]},()=>assert.fail());
for(let pass=0;pass<2;pass++){
  decorateResourceLinks(root,{index,source:{messageId:'message-1',turnId:'turn-1'},bindingValue:root.textContent,complete:true});
  const links=root.querySelectorAll('[data-resource-link]');
  assert.equal(links.length,2);
  assert.equal(links[0].getAttribute('href'),'https://actiontrail.console.aliyun.com/cn-hangzhou/trail-list/shared-name');
  assert.equal(links[1].getAttribute('href'),'https://cloudmonitor.console.aliyun.com/alert-template');
}
""",
        encoding="utf-8",
    )
    result = subprocess.run(["node", str(script)], capture_output=True, text=True, encoding="utf-8", timeout=20)
    assert result.returncode == 0, result.stderr


def test_security_group_annotation_never_changes_literal_identity_or_href(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is unavailable")
    script = tmp_path / "security-group-annotation.mjs"
    script.write_text(
        CATALOG_BOOTSTRAP
        + "import assert from 'node:assert/strict';\n"
        + "const rules = await import("
        + json.dumps((STATIC / "resource_link_rules.js").as_uri())
        + ");\n"
        + "const {scanResourceOccurrences:scan} = await import("
        + json.dumps((STATIC / "resource_occurrences.js").as_uri())
        + ");\n"
        + "const {ResourceIndex} = await import("
        + json.dumps((STATIC / "resource_index.js").as_uri())
        + ");\n"
        + r"""
const reference = {provider:'aliyun',referenceId:'ref-sg',resourceType:'ALIYUN::ECS::SecurityGroup',
  identifier:'sg-offline0001',identifierField:'SecurityGroupId',identifierKind:'id',
  regionId:'cn-hangzhou',accountScope:'fake-account',consolePartition:'aliyun',scopeKind:'regional',
  attributes:{},aliases:[],sources:[{toolUseId:'offline-sg-create'}]};
const index = new ResourceIndex({canonicalType:rules.canonicalResourceType});
await index.loadSnapshot({schemaVersion:1,revision:1,items:[reference],bindings:[]},()=>assert.fail());
for (const annotation of ['（取自创建响应，非推测）','(from the creation response)',
  ' （取自创建响应，非推测）']) {
  const text = 'SecurityGroupId: '+reference.identifier+annotation;
  const occurrences = scan(text,{index});
  assert.equal(occurrences.length,1,text);
  const occurrence = occurrences[0];
  assert.equal(occurrence.identifier,reference.identifier);
  assert.equal(text.slice(occurrence.start,occurrence.end),reference.identifier);
  const result = rules.resolveResourceOccurrence(occurrence,index);
  assert.equal(result.status,'resolved',JSON.stringify(result));
  assert.equal(result.url,'https://ecs.console.aliyun.com/#/securityGroupDetail/region/cn-hangzhou/groupId/sg-offline0001/instance');
}
for (const value of ['opaque（literal-name）','opaque:composite/value']) {
  const [occurrence] = scan('PhysicalResourceId: '+value,{
    context:{resourceType:'ALIYUN::VPC::NatGateway'}});
  assert.equal(occurrence.identifier,value,'opaque locators are not globally split on punctuation');
}
const quoted = scan('{"SecurityGroupId":"sg-offline0001（literal）"}');
assert.ok(quoted.every(item=>item.identifier!=='sg-offline0001'),'quoted values remain exact literals');
""",
        encoding="utf-8",
    )
    result = subprocess.run([node, str(script)], capture_output=True, text=True, encoding="utf-8", timeout=20)
    assert result.returncode == 0, result.stderr


def test_external_console_signin_is_fresh_normal_and_source_bound(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is unavailable")
    script = tmp_path / "external-console-signin.mjs"
    script.write_text(
        CATALOG_BOOTSTRAP
        + "import assert from 'node:assert/strict';\n"
        + "const rules = await import("
        + json.dumps((STATIC / "resource_link_rules.js").as_uri())
        + ");\n"
        + "const {openResourceLinkExternal} = await import("
        + json.dumps((STATIC / "components/resource_link_preview.js").as_uri())
        + ");\n"
        + r"""
const reference = {provider:'aliyun',referenceId:'ref-sg',resourceType:'ALIYUN::ECS::SecurityGroup',
  identifier:'sg-offline0001',identifierField:'SecurityGroupId',identifierKind:'id',
  regionId:'cn-hangzhou',accountScope:'fake-account',consolePartition:'aliyun',attributes:{},aliases:[]};
const resolution = rules.resolveResourceOccurrence(reference,{
  candidates:()=>[reference],bindingFor:()=>null,contextFor:()=>({})});
const destination = resolution.url;
const pending = [], opened = [], requests = [];
const invalidators = new Set(), listeners = new Set();
const index = {generation:1,subscribe(listener){listeners.add(listener);return ()=>listeners.delete(listener);}};
const anchor = {isConnected:true,ownerDocument:{defaultView:{open(url,name){
  assert.deepEqual([url,name],['about:blank','_blank']);
  const popup = {opener:{},closed:false,history:[],
    location:{replace(url){popup.history.push(url);}},
    close(){popup.closed=true;}};
  opened.push(popup);return popup;
}}}};
const options = {index,createSigninUrl(payload){
  requests.push(payload);
  return new Promise((resolve,reject)=>pending.push({resolve,reject}));
},onInvalidate(callback){invalidators.add(callback);return ()=>invalidators.delete(callback);}};
const response = token => ({status:'ready',consolePartition:'aliyun',destination,
  url:'/api/sessions/offline/resource-references/signin-relay/'+token.repeat(64)});
const settle = () => new Promise(resolve=>setImmediate(resolve));
assert.equal(openResourceLinkExternal(anchor,resolution,options),true);
assert.equal(opened.length,1,'a real tab is reserved before the asynchronous request completes');
assert.equal(opened[0].opener,null);
assert.equal(opened[0].history.length,0);
assert.deepEqual(requests[0],{accountScope:'fake-account',preferredPartition:'aliyun',
  destinations:{aliyun:destination}});
assert.ok(!requests[0].destinations.aliyun.includes('4service'),'external uses the normal ticket destination');
pending[0].resolve(response('a'));await settle();
assert.deepEqual(opened[0].history,[new URL(response('a').url,'http://127.0.0.1').href]);
assert.equal(openResourceLinkExternal(anchor,resolution,options),true);
pending[1].resolve(response('b'));await settle();
assert.notDeepEqual(opened[0].history,opened[1].history,'each activation mints an independent relay');
assert.equal(openResourceLinkExternal(anchor,resolution,options),true);
for (const callback of invalidators) callback();
pending[2].resolve(response('c'));await settle();
assert.equal(opened[2].closed,true);
assert.deepEqual(opened[2].history,[],'source invalidation must not navigate the stale tab');
assert.equal(openResourceLinkExternal(anchor,resolution,options),true);
for (const callback of listeners) callback({changed:new Set([JSON.stringify(['id',reference.identifier])])});
pending[3].resolve(response('d'));await settle();
assert.equal(opened[3].closed,true);
assert.equal(openResourceLinkExternal(anchor,resolution,options),true);
pending[4].resolve({...response('e'),destination:destination+'/wrong'});await settle();
assert.deepEqual(opened[4].history,[destination],'a rejected signin result cannot substitute its destination');
assert.equal(openResourceLinkExternal(anchor,resolution,options),true);
pending[5].reject(Error('offline'));await settle();
assert.deepEqual(opened[5].history,[destination],'signin failure keeps the exact verified console fallback');
assert.equal(openResourceLinkExternal(anchor,{...resolution,url:'https://evil.invalid'},options),false);
anchor.ownerDocument.defaultView.open=()=>null;
assert.equal(openResourceLinkExternal(anchor,resolution,options),false,'blocked tabs keep native navigation');
assert.equal(requests.length,6,'a blocked popup never spends a token');
assert.equal(invalidators.size,0);
assert.equal(listeners.size,0);
""",
        encoding="utf-8",
    )
    result = subprocess.run([node, str(script)], capture_output=True, text=True, encoding="utf-8", timeout=20)
    assert result.returncode == 0, result.stderr


def test_shift_external_only_console_runs_the_application_signin_chain(tmp_path):
    """Exercise app configuration, decoration, the click handler and the real signin helper."""
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is unavailable")
    app = (STATIC / "app.js").read_text(encoding="utf-8")
    setup = app[app.index("let resourceSigninCapabilitySession") : app.index("const DESKTOP_RUNTIME")]
    script = tmp_path / "shift-external-only-signin.mjs"
    script.write_text(
        CATALOG_BOOTSTRAP
        + "import assert from 'node:assert/strict';\n"
        + "const rules = await import("
        + json.dumps((STATIC / "resource_link_rules.js").as_uri())
        + ");\n"
        + "const {ResourceIndex} = await import("
        + json.dumps((STATIC / "resource_index.js").as_uri())
        + ");\n"
        + "const {configureResourceLinks} = await import("
        + json.dumps((STATIC / "resource_linkify.js").as_uri())
        + ");\n"
        + "const {openResourceLinkExternal,supportsEmbeddedSignin} = await import("
        + json.dumps((STATIC / "components/resource_link_preview.js").as_uri())
        + ");\n"
        + "const applicationSetup = "
        + json.dumps(setup)
        + ";\n"
        + r"""
// A small DOM fixture runs the production controller, including source invalidation.
class Node {
  constructor(doc,tag,data='') {
    this.ownerDocument=doc;this.nodeType=tag?1:3;this.tagName=tag?.toUpperCase();
    this.data=data;this.childNodes=[];this.parentElement=null;this.attrs=new Map();this.handlers=new Map();
  }
  get isConnected(){return this===this.ownerDocument.body||Boolean(this.parentElement?.isConnected);}
  get textContent(){return this.nodeType===3?this.data:this.childNodes.map(n=>n.textContent).join('');}
  get firstChild(){return this.childNodes[0];}
  get nextSibling(){const peers=this.parentElement?.childNodes||[];return peers[peers.indexOf(this)+1];}
  hasAttribute(key){return this.attrs.has(key);}
  getAttribute(key){return this.attrs.get(key)??null;}
  setAttribute(key,value){this.attrs.set(key,String(value));}
  append(...nodes){for(const n of nodes){
    if(n.parentElement){const peers=n.parentElement.childNodes;peers.splice(peers.indexOf(n),1);}
    n.parentElement=this;this.childNodes.push(n);
  }}
  contains(node){return this===node||this.childNodes.some(n=>n.contains(node));}
  closest(selector){return selector==='tr'||selector==='td'?null:
    this.hasAttribute(selector.slice(1,-1))?this:this.parentElement?.closest(selector);}
  querySelectorAll(selector){
    const matches=n=>selector.startsWith('a[')?n.tagName==='A'&&n.hasAttribute('data-resource-auto-link'):
      n.hasAttribute(selector.slice(1,-1));
    return this.childNodes.flatMap(n=>[...(matches(n)?[n]:[]),...n.querySelectorAll(selector)]);
  }
  replaceWith(...nodes){
    const parent=this.parentElement,position=parent.childNodes.indexOf(this);
    parent.childNodes.splice(position,1);this.parentElement=null;
    for(const n of nodes){
      if(n.parentElement){const peers=n.parentElement.childNodes;peers.splice(peers.indexOf(n),1);}
      n.parentElement=parent;
    }
    parent.childNodes.splice(position,0,...nodes);
  }
  splitText(offset){
    const tail=new Node(this.ownerDocument,null,this.data.slice(offset));this.data=this.data.slice(0,offset);
    tail.parentElement=this.parentElement;
    const peers=this.parentElement.childNodes;peers.splice(peers.indexOf(this)+1,0,tail);return tail;
  }
  addEventListener(type,handler){this.handlers.set(type,handler);}
}
const opened=[],pending=[],requests=[];
let blocked=false,capability={openMode:'external'},controller,previews=0;
const doc={createTreeWalker(){},getSelection:()=>null,
  createElement(tag){return new Node(doc,tag);},
  defaultView:{open(url,target){
    assert.deepEqual([url,target],['about:blank','_blank']);
    if(blocked)return null;
    const popup={opener:{},closed:false,history:[],
      location:{replace(url){popup.history.push(url);}},close(){popup.closed=true;}};
    opened.push(popup);return popup;
  }}};
doc.body=new Node(doc,'body');
const root=new Node(doc,'p');doc.body.append(root);
const reference={provider:'aliyun',referenceId:'ref-ess',resourceType:'ALIYUN::ESS::ScheduledTask',
  identifier:'bhdOfflineScheduledTask01',identifierField:'PhysicalResourceId',identifierKind:'id',
  regionId:'cn-hangzhou',accountScope:'fake-account',consolePartition:'aliyun',scopeKind:'regional',
  attributes:{PhysicalResourceId:'bhdOfflineScheduledTask01',ScheduledTaskId:'bhdOfflineScheduledTask01',
    ScheduledTaskName:'offline-task'},
  aliases:[{identifier:'bhdOfflineScheduledTask01',identifierField:'ScheduledTaskId',identifierKind:'id'}],
  sources:[{toolUseId:'offline-query'}]};
root.append(new Node(doc,null,'ScheduledTaskId: '+reference.identifier));
const index=new ResourceIndex({canonicalType:rules.canonicalResourceType});index.reset('offline-session');
await index.loadSnapshot({schemaVersion:1,revision:1,items:[reference],bindings:[]},()=>assert.fail());
const api={getResourceConsoleSigninCapability:async()=>capability,
  createResourceConsoleSignin(session,payload){
    assert.equal(session,'offline-session');requests.push(payload);
    return new Promise((resolve,reject)=>pending.push({resolve,reject}));
  }};
const initialize=new Function('configureResourceLinks','resourceIndex','api','supportsEmbeddedSignin',
  'openResourceLinkExternal','closeResourceLinkPopover','openResourceLinkPreview','closeResourceLinkPreview',
  'openResourceLinkPopover',applicationSetup+
  '\nreturn {refresh:refreshResourceSigninCapability,stop(){clearTimeout(resourceSigninCapabilityRetryTimer);}};');
const app=initialize(options=>{controller=configureResourceLinks(options);return controller;},
  index,api,supportsEmbeddedSignin,openResourceLinkExternal,()=>{},()=>{previews++;},()=>{},()=>{});
await app.refresh(index.sessionId);app.stop();
const options={context:{resourceType:reference.resourceType},source:{toolUseId:'offline-query'},complete:true};
controller.register(root,options);await controller.whenIdle();
let anchor=root.querySelectorAll('[data-resource-link]')[0];
assert.ok(anchor,'production controller must decorate the observed task');
const destination='https://essnew.console.aliyun.com/#/task/v3/timer/cn-hangzhou';
assert.equal(anchor.getAttribute('href'),destination);
assert.equal(supportsEmbeddedSignin({status:'resolved',url:destination}),false,
  'external signin must not broaden iframe support');
const click=(shift=true)=>{
  const event={button:0,shiftKey:shift,metaKey:false,ctrlKey:false,prevented:false,
    stopPropagation(){},preventDefault(){this.prevented=true;}};
  anchor.handlers.get('click')(event);return event;
};
const settle=()=>new Promise(resolve=>setImmediate(resolve));
const response=token=>({status:'ready',consolePartition:'aliyun',destination,
  url:'/api/sessions/offline-session/resource-references/signin-relay/'+token.repeat(64)});
assert.equal(click().prevented,true);
assert.deepEqual(opened[0].history,[destination]);
assert.equal(requests.length,0,'unconfirmed capability must keep native navigation');
capability={openMode:'embedded'};await app.refresh(index.sessionId,{force:true});
assert.equal(click(false).prevented,false);
assert.equal(previews,0,'ordinary external-only activation must not open an iframe');
assert.equal(click().prevented,true);
assert.equal(requests.length,1,'ESS Shift click must reach the app signin API despite iframe exclusion');
assert.deepEqual(requests[0],{accountScope:'fake-account',preferredPartition:'aliyun',
  destinations:{aliyun:destination}});
assert.deepEqual(opened[1].history,[]);assert.equal(opened[1].opener,null);
pending[0].resolve(response('a'));await settle();
assert.deepEqual(opened[1].history,[new URL(response('a').url,'http://127.0.0.1').href]);
click();pending[1].resolve(response('b'));await settle();
assert.notDeepEqual(opened[1].history,opened[2].history,'each click gets a fresh ordinary ticket');
click();pending[2].reject(Error('offline signin failure'));await settle();
assert.deepEqual(opened[3].history,[destination],'failed issuance keeps the reviewed destination');
click();pending[3].resolve({...response('c'),destination:'https://evil.invalid'});await settle();
assert.deepEqual(opened[4].history,[destination],'wrong destination cannot replace the source target');
blocked=true;
assert.equal(click().prevented,false,'blocked popups preserve the native link');
assert.equal(requests.length,4,'a blocked popup must not issue a ticket');blocked=false;
click();
root.append(new Node(doc,null,' changed'));
const count=opened.length;
assert.equal(click().prevented,true);
assert.equal(opened.length,count,'a changed source must block a stale anchor');
controller.register(root,options);await controller.whenIdle();
pending[4].resolve(response('d'));await settle();
assert.equal(opened[5].closed,true);
assert.deepEqual(opened[5].history,[],'source invalidation cancels a pending signed tab');
anchor=root.querySelectorAll('[data-resource-link]')[0];
click();index.reset('different-session');
pending[5].resolve(response('e'));await settle();
assert.equal(opened[6].closed,true);
assert.deepEqual(opened[6].history,[],'session changes must discard a pending ticket');
controller.dispose();app.stop();
""",
        encoding="utf-8",
    )
    result = subprocess.run([node, str(script)], capture_output=True, text=True, encoding="utf-8", timeout=20)
    assert result.returncode == 0, result.stderr


def test_network_interface_alias_uses_exact_detail_route_and_preserves_scope(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is unavailable")
    script = tmp_path / "network-interface-alias.mjs"
    script.write_text(
        CATALOG_BOOTSTRAP
        + "import assert from 'node:assert/strict';\n"
        + "const rules = await import("
        + json.dumps((STATIC / "resource_link_rules.js").as_uri())
        + ");\n"
        + r"""
const base = {provider:'aliyun',referenceId:'ref-eni',semantic:true,identifier:'eni-offline0001',
  identifierField:'NetworkInterfaceId',identifierKind:'id',accountScope:'fake-account',
  attributes:{},aliases:[]};
const resolve = reference => rules.resolveResourceOccurrence(reference,{
  candidates:()=>[reference],bindingFor:()=>null,contextFor:()=>({})});
for (const [consolePartition,host,regions] of [
  ['aliyun','ecs.console.aliyun.com',['cn-hangzhou','cn-shenzhen']],
  ['alibabacloud','ecs.console.alibabacloud.com',['ap-southeast-1','ap-southeast-2']]]) {
  for (const regionId of regions) {
    const reference={...base,consolePartition,regionId,resourceType:'alicloud_ecs_network_interface'};
    const canonical={...reference,resourceType:'ALIYUN::ECS::NetworkInterface'};
    const expected=`https://${host}/networkInterfaces/region/${regionId}/detail/eni-offline0001`;
    assert.equal(rules.canonicalResourceType(reference.resourceType),canonical.resourceType);
    assert.equal(resolve(reference).status,'resolved');
    assert.equal(resolve(reference).url,expected);
    assert.equal(resolve(canonical).url,expected);
    for (const resourceType of [reference.resourceType,canonical.resourceType]) {
      for (const patch of [{regionId:''},{accountScope:''},{identifier:'eni-short'},
        {identifier:'i-offline0001'},{identifier:'https://evil.invalid'},{identifier:'eni-offline0001/extra'}]) {
        assert.notEqual(resolve({...reference,resourceType,...patch}).status,'resolved',JSON.stringify({resourceType,...patch}));
      }
      const scoped={...reference,resourceType};
      const otherAccount={...scoped,accountScope:'other-fake-account'};
      assert.notEqual(rules.resolveResourceOccurrence(scoped,{
        candidates:()=>[scoped,otherAccount],bindingFor:()=>null,contextFor:()=>({})}).status,'resolved');
      assert.equal(resolve(scoped).reference.accountScope,'fake-account');
    }
  }
}
""",
        encoding="utf-8",
    )
    result = subprocess.run([node, str(script)], capture_output=True, text=True, encoding="utf-8", check=False)
    assert result.returncode == 0, result.stderr


def test_rds_clone_upgrade_aliases_resolve_same_exact_instance_not_parameter_group(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is unavailable")
    script = tmp_path / "rds-instance-aliases.mjs"
    script.write_text(
        CATALOG_BOOTSTRAP
        + "import assert from 'node:assert/strict';\n"
        + "const rules = await import("
        + json.dumps((STATIC / "resource_link_rules.js").as_uri())
        + ");\n"
        + r"""
const base = {provider:'aliyun',referenceId:'ref-rds',semantic:true,identifier:'rm-offline0001',
  identifierField:'DBInstanceId',identifierKind:'id',regionId:'cn-hangzhou',
  accountScope:'fake-account',consolePartition:'aliyun',attributes:{},aliases:[]};
const resolve = ref => rules.resolveResourceOccurrence(ref,{
  candidates:()=>[ref],bindingFor:()=>null,contextFor:()=>({})});
for (const resourceType of ['alicloud_rds_clone_db_instance','alicloud_rds_upgrade_db_instance']) {
  assert.equal(rules.canonicalResourceType(resourceType),'ALIYUN::RDS::DBInstance');
  const ref={...base,resourceType};
  const canonical=resolve({...ref,resourceType:'ALIYUN::RDS::DBInstance'});
  assert.equal(resolve(ref).status,'resolved');
  assert.equal(resolve(ref).url,canonical.url);
  assert.ok(canonical.url.includes('rm-offline0001'));
  assert.notEqual(resolve({...ref,regionId:''}).status,'resolved');
  assert.notEqual(resolve({...ref,accountScope:''}).status,'resolved');
  assert.notEqual(resolve({...ref,identifier:'https://evil.invalid'}).status,'resolved');
}
assert.notEqual(rules.canonicalResourceType('alicloud_rds_parameter_group'),'ALIYUN::RDS::DBInstance');
assert.equal(rules.canonicalResourceType('alicloud_resource_manager_resource_group'),
  'ALIYUN::ResourceManager::ResourceGroup');
assert.equal(rules.canonicalResourceType('alicloud_router_interface'),'ALIYUN::VPC::RouterInterface');
assert.equal(rules.resourceSpec('alicloud_resource_manager_resource_group').identifierField,'Id');
assert.equal(rules.resourceSpec('alicloud_router_interface').identifierField,'PhysicalResourceId');
""",
        encoding="utf-8",
    )
    result = subprocess.run([node, str(script)], capture_output=True, text=True, encoding="utf-8", timeout=20)
    assert result.returncode == 0, result.stderr


def test_rds_custom_parameter_template_uses_observed_list_not_instance_route(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is unavailable")
    script = tmp_path / "rds-parameter-template.mjs"
    script.write_text(
        CATALOG_BOOTSTRAP
        + "import assert from 'node:assert/strict';\n"
        + "const rules = await import("
        + json.dumps((STATIC / "resource_link_rules.js").as_uri())
        + ");\n"
        + r"""
const base = {provider:'aliyun',referenceId:'ref-template',semantic:true,
  resourceType:'alicloud_rds_parameter_group',identifier:'rpg-offline0001',
  identifierField:'ParameterGroupId',identifierKind:'id',regionId:'cn-hangzhou',
  accountScope:'fake-account',consolePartition:'aliyun',attributes:{ParameterGroupType:'1'},aliases:[]};
const resolve = ref => rules.resolveResourceOccurrence(ref,{
  candidates:()=>[ref],bindingFor:()=>null,contextFor:()=>({})});
assert.equal(rules.canonicalResourceType(base.resourceType),base.resourceType);
const result=resolve(base);
assert.equal(result.status,'resolved');
assert.equal(result.ruleId,'rds-custom-parameter-template-cn');
assert.equal(result.url,'https://rdsnext.console.aliyun.com/paramGroup/cn-hangzhou/paramGroups/custom');
assert.equal(resolve({...base,regionId:'cn-shenzhen'}).url,
  'https://rdsnext.console.aliyun.com/paramGroup/cn-shenzhen/paramGroups/custom');
for (const patch of [{attributes:{}},{attributes:{ParameterGroupType:'0'}},
  {attributes:{ParameterGroupType:'2'}},{identifierField:'DBInstanceId'},
  {regionId:''},{accountScope:''},{identifier:'https://evil.invalid'}]) {
  assert.notEqual(resolve({...base,...patch}).status,'resolved');
}
assert.notEqual(rules.canonicalResourceType('ALIYUN::RDS::DBInstanceParameterGroup'),base.resourceType);
""",
        encoding="utf-8",
    )
    subprocess.run([node, str(script)], check=True, capture_output=True, text=True, encoding="utf-8")


def test_nas_mount_target_provider_composite_and_api_parent_fail_closed(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is unavailable")
    script = tmp_path / "nas-mount-test.mjs"
    script.write_text(
        CATALOG_BOOTSTRAP
        + "import assert from 'node:assert/strict';\n"
        + "const rules = await import("
        + json.dumps((STATIC / "resource_link_rules.js").as_uri())
        + ");\n"
        + r"""
const base = {provider:'aliyun',referenceId:'ref-mount',resourceType:'alicloud_nas_mount_target',
  identifier:'fs-explicit:other-prefix.nas.aliyuncs.com',identifierField:'PhysicalResourceId',identifierKind:'id',
  regionId:'cn-hangzhou',accountScope:'fake-account',consolePartition:'aliyun',attributes:{},aliases:[]};
const resolve = (ref) => rules.resolveResourceOccurrence(ref,{
  candidates:()=>[ref],bindingFor:()=>null,contextFor:()=>({})});
assert.equal(resolve(base).url,'https://nasnext.console.aliyun.com/cn-hangzhou/filesystem/fs-explicit/mount');
for (const attributes of [{FileSystemId:'conflicting-parent'},{MountTargetDomain:'other.nas.aliyuncs.com'}]) {
  assert.notEqual(resolve({...base,attributes}).status,'resolved');
}
for (const identifier of ['fs-explicit:other-prefix.nas.aliyuncs.com:extra','fs-explicit:arbitrary.example.com']) {
  assert.notEqual(resolve({...base,identifier}).status,'resolved');
}
const api = {...base,resourceType:'ALIYUN::NAS::MountTarget',identifier:'other-prefix.nas.aliyuncs.com',
  identifierField:'MountTargetDomain'};
assert.notEqual(resolve(api).status,'resolved','do not guess filesystem from a domain prefix');
assert.equal(resolve({...api,attributes:{FileSystemId:'fs-explicit'}}).url,
  'https://nasnext.console.aliyun.com/cn-hangzhou/filesystem/fs-explicit/mount');
""",
        encoding="utf-8",
    )
    subprocess.run([node, str(script)], check=True, capture_output=True, text=True, encoding="utf-8")


def test_polardb_account_requires_exact_cluster_parent_for_console_route(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is unavailable")
    script = tmp_path / "polardb-account-test.mjs"
    script.write_text(
        CATALOG_BOOTSTRAP
        + "import assert from 'node:assert/strict';\n"
        + "const rules = await import("
        + json.dumps((STATIC / "resource_link_rules.js").as_uri())
        + ");\n"
        + r"""
const base = {provider:'aliyun',referenceId:'ref-account',resourceType:'ALIYUN::POLARDB::Account',
  identifier:'fake-db-user',identifierField:'AccountName',identifierKind:'name',semantic:true,regionId:'cn-hangzhou',
  accountScope:'fake-account',consolePartition:'aliyun',attributes:{},aliases:[]};
const resolve = ref => rules.resolveResourceOccurrence(ref,{
  candidates:()=>[ref],bindingFor:()=>null,contextFor:()=>({})});
for (const resourceType of ['ALIYUN::POLARDB::Account','alicloud_polardb_account']) {
  assert.notEqual(resolve({...base,resourceType}).status,'resolved','never use account name as cluster ID');
  const ref = {...base,resourceType,attributes:{DBClusterId:'pc-fake-parent'}};
  assert.equal(resolve(ref).url,'https://polardb.console.aliyun.com/cn-hangzhou/cluster/pc-fake-parent/account');
  assert.equal(resolve({...ref,identifier:'another-db-user'}).url,resolve(ref).url);
  assert.equal(resolve({...ref,attributes:{DBClusterId:'pc-other-parent'}}).url,
    'https://polardb.console.aliyun.com/cn-hangzhou/cluster/pc-other-parent/account');
}
""",
        encoding="utf-8",
    )
    subprocess.run([node, str(script)], check=True, capture_output=True, text=True, encoding="utf-8")


def test_polardb_children_never_use_child_locator_as_cluster_id(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is unavailable")
    script = tmp_path / "polardb-children.mjs"
    script.write_text(
        CATALOG_BOOTSTRAP
        + "import assert from 'node:assert/strict';\n"
        + "const rules = await import("
        + json.dumps((STATIC / "resource_link_rules.js").as_uri())
        + ");\n"
        + r"""
const base = {provider:'aliyun',referenceId:'ref-child',semantic:true,regionId:'cn-hangzhou',
  accountScope:'fake-account',consolePartition:'aliyun',attributes:{},aliases:[]};
const resolve = ref => rules.resolveResourceOccurrence(ref,{
  candidates:()=>[ref],bindingFor:()=>null,contextFor:()=>({})});
for (const [resourceType,identifier,identifierField,identifierKind,page,parents] of [
  ['alicloud_polardb_database','app','DBName','name','database',{}],
  ['alicloud_polardb_endpoint','pe-fixture','DBEndpointId','id','baseInfo',{}],
  ['alicloud_polardb_endpoint_address','child.example.com','ConnectionString','name','baseInfo',
    {DBEndpointId:'pe-fixture'}]]) {
  const ref = {...base,resourceType,identifier,identifierField,identifierKind};
  assert.notEqual(resolve(ref).status,'resolved');
  assert.equal(resolve({...ref,attributes:{DBClusterId:'pc-parent',...parents}}).url,
    `https://polardb.console.aliyun.com/cn-hangzhou/cluster/pc-parent/${page}`);
}
""",
        encoding="utf-8",
    )
    subprocess.run([node, str(script)], check=True, capture_output=True, text=True, encoding="utf-8")


def test_frontend_identity_payload_covers_every_production_link_type():
    catalog = resource_link_catalog()
    identities = resource_link_catalog_payload()["identities"]
    by_type = {name: item for item in identities for name in (item["resourceType"], *item["aliases"])}
    assert set(catalog.resource_types) <= set(by_type)
    for resource_type in catalog.resource_types:
        spec = resource_spec(resource_type)
        assert spec is not None
        assert by_type[resource_type]["resourceType"] == spec.resource_type
        assert by_type[resource_type]["identifierField"] == spec.identifier_field
        assert by_type[resource_type]["identifierKind"] == spec.identifier_kind
        assert by_type[resource_type]["scopeKind"] == spec.scope_kind


def test_resource_recognition_scope_name_semantics_and_url_safety(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is unavailable")
    script = tmp_path / "recognition-test.mjs"
    script.write_text(
        CATALOG_BOOTSTRAP
        + "import assert from 'node:assert/strict';\n"
        + "const rules = await import("
        + json.dumps((STATIC / "resource_link_rules.js").as_uri())
        + ");\n"
        + "const {scanResourceOccurrences:scan,resourceContextFromObject,resourceToolContext} = await import("
        + json.dumps((STATIC / "resource_occurrences.js").as_uri())
        + ");\n"
        + "const {ResourceIndex} = await import("
        + json.dumps((STATIC / "resource_index.js").as_uri())
        + ");\n"
        + "const linkify = await import("
        + json.dumps((STATIC / "resource_linkify.js").as_uri())
        + ");\n"
        + r"""
const index = new ResourceIndex({canonicalType:rules.canonicalResourceType});
const role = {provider:'aliyun', referenceId:'ref-role', resourceType:'ALIYUN::RAM::Role',
  identifier:'DeployRole', identifierField:'RoleName', identifierKind:'name', scopeKind:'account',
  accountScope:'fake-account', consolePartition:'aliyun', aliases:[], attributes:{}, sources:[{toolUseId:'t1'}]};
const instance = {...role, referenceId:'ref-instance', resourceType:'ALIYUN::ECS::Instance',
  identifier:'i-design0000000001', identifierField:'InstanceId', identifierKind:'id', scopeKind:'regional',
  regionId:'cn-hangzhou'};
const slsProject = {...role, referenceId:'ref-sls-project', resourceType:'alicloud_log_project',
  identifier:'sls-test-project', identifierField:'projectName', identifierKind:'name',
  scopeKind:'account', attributes:{projectName:'sls-test-project'}, sources:[{toolUseId:'sls-list'}]};
const contactGroup = {...role, referenceId:'ref-contact-group', resourceType:'ALIYUN::CMS::ContactGroup',
  identifier:'云账号报警联系人', identifierField:'PhysicalResourceId', identifierKind:'id', scopeKind:'account',
  aliases:[{identifier:'ack_Default Contact Group',identifierField:'Name',identifierKind:'name'}]};
const cmsSpaced = {...role, referenceId:'ref-cms-spaced', resourceType:'ALIYUN::CMS::ContactGroup',
  identifier:'ack_Default Contact Group', identifierField:'Name', identifierKind:'name', scopeKind:'account'};
const stackGroup = {...instance, referenceId:'ref-stack-group', resourceType:'ALIYUN::ROS::StackGroup',
  identifier:'29dd2de2-4872-429a-bb82-95930182fa94', identifierField:'StackGroupId',
  attributes:{StackGroupName:'ros-fvt-pre-cfff5744'},
  sources:[{toolUseId:'stack-group-tool'}]};
const templateScratch = {...instance, referenceId:'ref-template-scratch',
  resourceType:'ALIYUN::ROS::TemplateScratch', identifier:'ts-15ed16b0b0db460c8806',
  identifierField:'TemplateScratchId', sources:[{toolUseId:'template-scratch-tool'}]};
const privateZoneRecord = {...role, referenceId:'ref-private-zone-record',
  resourceType:'ALIYUN::PVTZ::ZoneRecord', identifier:'211532251000481',
  identifierField:'PhysicalResourceId', identifierKind:'id', scopeKind:'account',
  aliases:[{identifier:'211532251000481',identifierField:'RecordId',identifierKind:'id'}],
  attributes:{PhysicalResourceId:'211532251000481',RecordId:'211532251000481',
    ZoneId:'b9ad817604c668dd36001474fe75d5ab'},sources:[{turnId:'turn-private-network'}]};
const endpointService = {...instance, referenceId:'ref-endpoint-service',
  resourceType:'ALIYUN::PrivateLink::VpcEndpointService', identifier:'epsrv-2ze276sayga2zdgif5w4',
  identifierField:'PhysicalResourceId', identifierKind:'id', regionId:'cn-beijing',
  aliases:[{identifier:'epsrv-2ze276sayga2zdgif5w4',identifierField:'ServiceId',identifierKind:'id'},
    {identifier:'com.aliyuncs.privatelink.cn-beijing.epsrv-2ze276sayga2zdgif5w4',
      identifierField:'ServiceName',identifierKind:'name'}],
  attributes:{PhysicalResourceId:'epsrv-2ze276sayga2zdgif5w4',ServiceId:'epsrv-2ze276sayga2zdgif5w4',
    ServiceName:'com.aliyuncs.privatelink.cn-beijing.epsrv-2ze276sayga2zdgif5w4'},
  sources:[{turnId:'turn-private-network'}]};
const nlbServerGroup = {...instance, referenceId:'ref-nlb-server-group',
  resourceType:'ALIYUN::NLB::ServerGroup', identifier:'sgp-qfcsy6baqme9z3tnwd',
  identifierField:'PhysicalResourceId', regionId:'cn-hangzhou',
  aliases:[{identifier:'sgp-qfcsy6baqme9z3tnwd',identifierField:'ServerGroupId',identifierKind:'id'},
    {identifier:'demo7912',identifierField:'ServerGroupName',identifierKind:'name'}],
  attributes:{PhysicalResourceId:'sgp-qfcsy6baqme9z3tnwd',ServerGroupId:'sgp-qfcsy6baqme9z3tnwd',
    ServerGroupName:'demo7912'},sources:[{turnId:'turn-inventory'}]};
const ramGroup = {...role, referenceId:'ref-ram-group',resourceType:'ALIYUN::RAM::Group',
  identifier:'g-665d013b517901b3',identifierField:'PhysicalResourceId',identifierKind:'id',
  aliases:[{identifier:'g-665d013b517901b3',identifierField:'GroupId',identifierKind:'id'},
    {identifier:'admin',identifierField:'GroupName',identifierKind:'name'}],
  attributes:{PhysicalResourceId:'g-665d013b517901b3',GroupId:'g-665d013b517901b3',GroupName:'admin'},
  sources:[{turnId:'turn-inventory'}]};
const cloudConnectNetwork = {...instance, referenceId:'ref-ccn',
  resourceType:'ALIYUN::SAG::CloudConnectNetwork',identifier:'ccn-o23oyrd657wye6qu1n',
  identifierField:'CcnId',regionId:'cn-shanghai',sources:[{turnId:'turn-sag-api'}]};
const smartagCcnNamed = {...cloudConnectNetwork, referenceId:'ref-ccn-named',
  identifier:'ccn-test111111', aliases:[{identifier:'ros-fvt-pre',identifierField:'Name',identifierKind:'name'}],
  attributes:{CcnId:'ccn-test111111',Name:'ros-fvt-pre'}, sources:[{turnId:'turn-sag-api'}]};
const actionTrailCollision = {...role, referenceId:'ref-actiontrail-collision',
  resourceType:'ALIYUN::ACTIONTRAIL::Trail', identifier:'ros-fvt-pre', identifierField:'Name',
  identifierKind:'name', scopeKind:'regional', regionId:'cn-shanghai', sources:[{turnId:'turn-other'}]};
const ramRoleSummary = {...role, referenceId:'ref-summary-role',identifier:'HivotStackDeletionRole',
  sources:[{turnId:'turn-ram-api'}]};
await index.loadSnapshot(
  {schemaVersion:1,revision:1,
    items:[role,instance,slsProject,contactGroup,cmsSpaced,stackGroup,templateScratch,privateZoneRecord,endpointService,nlbServerGroup,
      ramGroup,cloudConnectNetwork,smartagCcnNamed,actionTrailCollision,ramRoleSummary],bindings:[]},
  () => assert.fail());
const matches = (value, context = {}) => scan(value, {index,context});
const slsContext = resourceToolContext({toolName:'aliyun_api',input:{product:'sls',action:'ListProject'}});
assert.equal(slsContext.resourceType,'alicloud_log_project',
  'one ListProject response must identify exactly one project type');
assert.equal(rules.resourceSpec('alicloud_log_project').identifierField,'projectName');
const slsOccurrence = matches('projectName: sls-test-project', slsContext).find(item =>
  item.identifier==='sls-test-project' && item.resourceType==='ALIYUN::SLS::Project');
assert.ok(slsOccurrence,
  'the reviewed project-name response must resolve to its resource fact');
const slsResolution = rules.resolveResourceOccurrence(slsOccurrence,index);
assert.equal(slsResolution.status,'resolved',
  'the observed project name must become a clickable product link');
assert.equal(slsResolution.url,
  'https://sls.console.aliyun.com/lognext/project/sls-test-project/overview');
const cmsTemplateContext = resourceToolContext({
  toolName:'aliyun_api',input:{product:'cms',action:'DescribeMetricRuleTemplateList'},
});
assert.equal(cmsTemplateContext.resourceType,'ALIYUN::CMS::MetricRuleTemplate',
  'the direct API contract must establish the CMS template identity');
assert.equal(resourceToolContext({toolName:'aliyun_api',args:{product:'cms',action:'DescribeMetricRuleTemplateList'}}).resourceType,
  'ALIYUN::CMS::MetricRuleTemplate', 'tool args snapshots must retain the direct API identity');
assert.equal(scan('{"Name":"shared-template-name"}',{index,context:cmsTemplateContext}).length,0,
  'a generic Name in a typed CMS template response must not reuse ActionTrail');
assert.equal(scan('产品：cms | Action：DescribeMetricRuleTemplateList\n{"Name":"ros-fvt-pre"}',{index}).length,0,
  'the nearest product/action label must scope an untyped API transcript');
assert.equal(scan('产品: VPC（vpc） / Action: DescribeCustomerGateways\nName: ros-fvt-pre',{index}).length,0,
  'localized product labels with a parenthesized product code must scope generic names');
assert.equal(scan('3. 云监控 CMS 联系组 — DescribeContactGroupList (cms, 2019-01-01, cn-hangzhou)\n'+
  'Name: ack_Default Contact Group',{index}).length,0,
  'batch API headings must scope generic names to the current product');
const cmsNamed=scan('3. 云监控 CMS 联系组 — DescribeContactGroupList (cms, 2019-01-01, cn-hangzhou)\n'+
  'Name=ack_Default Contact Group；联系人 ack@example.com',{index});
assert.ok(cmsNamed.some(item=>item.identifier==='ack_Default Contact Group' &&
  item.resourceType==='ALIYUN::CMS::ContactGroup'),
  'an observed CMS name containing spaces must remain linkable as one resource');
const cmsTableName=scan('3. 云监控 CMS 联系组 — DescribeContactGroupList (cms, 2019-01-01, cn-hangzhou)\n'+
  'ack_Default Contact Group | ack@example.com',{index,context:{sectionProduct:'CMS'}});
assert.ok(cmsTableName.some(item=>item.identifier==='ack_Default Contact Group' &&
  item.resourceType==='ALIYUN::CMS::ContactGroup'),
  'an observed spaced CMS name in a compact table must remain linkable');
const cmsTypedField=scan('ack_Default Contact Group',{index,
  context:{resourceType:'ALIYUN::CMS::ContactGroup',field:'Name',sectionProduct:'CMS'}});
assert.ok(cmsTypedField.some(item=>item.identifier==='ack_Default Contact Group'),
  'a typed Name field must link a spaced resource name without a prior index fact');
assert.equal(scan('路由器接口（RouterInterface）\n名称: ros-fvt-pre',{index}).length,0,
  'a generic name without a product scope must not borrow an unrelated ActionTrail fact');
const privateZoneRecordOccurrence=matches('Record ID: 211532251000481',
  {turnId:'turn-private-network'}).find(item=>item.identifier==='211532251000481');
assert.ok(privateZoneRecordOccurrence,
  'a two-word API identity label must preserve RecordId semantics for an observed numeric resource');
assert.equal(linkify.resolveResourceOccurrenceAlternatives(privateZoneRecordOccurrence,index).url,
  'https://dnsnext.console.aliyun.com/privateDNS/zones/b9ad817604c668dd36001474fe75d5ab/records');
const endpointServiceText='RegionId=cn-beijing + ServiceId=epsrv-2ze276sayga2zdgif5w4；'+
  'ServiceName=com.aliyuncs.privatelink.cn-beijing.epsrv-2ze276sayga2zdgif5w4';
const endpointServiceOccurrence=matches(endpointServiceText,{turnId:'turn-private-network'})
  .find(item=>item.identifier==='epsrv-2ze276sayga2zdgif5w4');
assert.ok(endpointServiceOccurrence,'the endpoint-service ID remains recognized in a multi-field sentence');
for (let attempt=0;attempt<2;attempt+=1) {
  const resolved=linkify.resolveResourceOccurrenceAlternatives(endpointServiceOccurrence,index);
  assert.equal(resolved.status,'resolved');
  assert.equal(resolved.reference.referenceId,'ref-endpoint-service');
  assert.equal(resolved.url,
    'https://vpc.console.aliyun.com/endpointservice/cn-beijing/endpointservices/epsrv-2ze276sayga2zdgif5w4');
}
for (const [text, displayField, expectedReference, expectedUrl] of [
  ['sgp-qfcsy6baqme9z3tnwd（ServerGroupId）','完整标识','ref-nlb-server-group',
    'https://slb.console.aliyun.com/nlb/cn-hangzhou/server-groups/sgp-qfcsy6baqme9z3tnwd'],
  ['g-665d013b517901b3（GroupId）','完整标识','ref-ram-group','https://ram.console.aliyun.com/groups/admin'],
  ['admin（GroupName）','名称','ref-ram-group','https://ram.console.aliyun.com/groups/admin'],
]) {
  const occurrence=scan(text,{index,field:displayField,context:{turnId:'turn-later-summary'}})
    .find(item=>text.startsWith(item.identifier));
  assert.ok(occurrence,`a postfix formal API field must identify an observed resource: ${text}`);
  const resolved=linkify.resolveResourceOccurrenceAlternatives(occurrence,index);
  assert.equal(resolved.reference.referenceId,expectedReference,text);
  assert.equal(resolved.url,expectedUrl,text);
}
const ramGroupTest = {...ramGroup, referenceId:'ref-ram-group-test',
  aliases:[{identifier:'test',identifierField:'GroupName',identifierKind:'name'}],
  attributes:{...ramGroup.attributes,GroupName:'test'},sources:[{turnId:'turn-current'}]};
const mixedTuple = scan('test（GroupName；ReplicaGroupId pg-bRA80MNkNqQYRkQZ）',
  {index:{candidates:({identifier,identifierKind}) => identifierKind === 'name' && identifier === 'test'
    ? [ramGroupTest] : []},context:{turnId:'turn-current'}});
assert.equal(mixedTuple.length,0,
  'a name tuple containing a foreign formal field must not resolve to a RAM group');
const sectionCollision = scan('## 1. EBS 云盘异步复制组\\n名称字段 (GroupName) : `test`',
  {index:{candidates:({identifier,identifierKind}) => identifierKind === 'name' && identifier === 'test'
    ? [ramGroupTest] : []},context:{turnId:'turn-current'}});
assert.equal(sectionCollision.length,0,
  'a name under an EBS section must not resolve to a RAM group with the same GroupName');
for (const [text, expectedReference] of [
  ['标识: ccn-o23oyrd657wye6qu1n','ref-ccn'],
  ['名称: HivotStackDeletionRole','ref-summary-role'],
]) {
  const occurrence=scan(text,{index,context:{turnId:'turn-later-summary'}})
    .find(item=>text.endsWith(item.identifier));
  assert.ok(occurrence,`a generic display identity must reuse an exact prior API fact: ${text}`);
  assert.equal(linkify.resolveResourceOccurrenceAlternatives(occurrence,index).reference.referenceId,
    expectedReference,text);
}
assert.equal(scan('ccn-o23oyrd657wye6qu1n',{index,context:{turnId:'turn-later-summary'}}).length,0,
  'an opaque locator from another turn remains ordinary text without an explicit identity label');
const smartagNameOccurrence = scan('Name: ros-fvt-pre；CcnId: ccn-test111111',
  {index,context:{turnId:'turn-sag-api'}}).find(item=>item.identifier==='ros-fvt-pre');
assert.ok(smartagNameOccurrence, 'a SmartAG name alias remains recognized in its API row');
assert.equal(linkify.resolveResourceOccurrenceAlternatives(smartagNameOccurrence,index).reference.referenceId,
  'ref-ccn-named', 'a SmartAG name must not resolve to a same-named ActionTrail trail');
for (const value of ['{"InstanceId":"i-design0000000001"}', 'InstanceId: i-design0000000001']) {
  const [knownField] = matches(value);
  assert.equal(knownField.resourceType,'ALIYUN::ECS::Instance');
  assert.equal(knownField.alternatives,undefined,
    'an observed resource type must win before context-only candidates for a shared field');
}
for (const [text, expectedType] of [
  ['StackGroupId=29dd2de2-4872-429a-bb82-95930182fa94','ALIYUN::ROS::StackGroup'],
  ['TemplateScratchId=ts-15ed16b0b0db460c8806','ALIYUN::ROS::TemplateScratch'],
]) {
  const [inventoryId] = matches(text);
  assert.equal(inventoryId.resourceType,expectedType,text);
  assert.equal(linkify.resolveResourceOccurrenceAlternatives(inventoryId,index).status,'resolved',text);
}
const explicitRedis=matches('{"Type":"ALIYUN::REDIS::Instance","InstanceId":"i-design0000000001"}');
assert.equal(explicitRedis[0].resourceType,'ALIYUN::REDIS::Instance','an explicit local type must win');
const redis={...instance,referenceId:'ref-redis',resourceType:'ALIYUN::REDIS::Instance'};
const bothIndex={candidates:({identifier,identifierKind,resourceType})=>[instance,redis].filter(item=>
  item.identifier===identifier && item.identifierKind===identifierKind &&
  (!resourceType || rules.canonicalResourceType(item.resourceType)===rules.canonicalResourceType(resourceType)))};
const [ambiguousField]=scan('{"InstanceId":"i-design0000000001"}',{index:bothIndex});
assert.deepEqual([ambiguousField.resourceType,...ambiguousField.alternatives.map(item=>item.resourceType)],
  ['ALIYUN::ECS::Instance','ALIYUN::REDIS::Instance'],
  'multiple observed resource types must remain explicit alternatives');
const emptyIndex={candidates:()=>[],bindingFor:()=>null};
const [unknownSharedField]=scan('{"InstanceId":"unknown-shared-value"}',{index:emptyIndex});
const unknownResolution=linkify.resolveResourceOccurrenceAlternatives(unknownSharedField,emptyIndex);
assert.equal(unknownResolution.status,'ambiguous');
const unknownTypes=unknownResolution.candidates.map(item=>item.resourceType);
assert.ok(unknownTypes.includes('ALIYUN::ECS::Instance') && unknownTypes.includes('ALIYUN::REDIS::Instance') &&
    unknownTypes.length >= 2,
  'all catalog identity contracts sharing a field must be offered instead of silently defaulting to ECS');
const tairId='r-bp1kvjfcyrelznrn14';
const tair={...instance,referenceId:'ref-tair',resourceType:'ALIYUN::REDIS::Instance',
  identifier:tairId,identifierField:'InstanceId',sources:[{toolUseId:'kv-tool'}]};
const unrelatedRamUser={...role,referenceId:'ref-other-user',resourceType:'ALIYUN::RAM::User',
  identifier:'actual-user',identifierField:'UserName',identifierKind:'name',sources:[{toolUseId:'ram-tool'}]};
const toolFieldIndex=new ResourceIndex({canonicalType:rules.canonicalResourceType});
await toolFieldIndex.loadSnapshot({schemaVersion:1,revision:1,items:[tair,unrelatedRamUser],bindings:[]},
  ()=>assert.fail());
const kvToolFields=scan(JSON.stringify({UserName:tairId,InstanceId:tairId}),
  {index:toolFieldIndex,context:{toolUseId:'kv-tool'}});
assert.equal(kvToolFields.some(item=>item.resourceType==='ALIYUN::RAM::User'),false,
  'a foreign UserName field in an API response must not invent a RAM user');
assert.equal(kvToolFields.filter(item=>item.resourceType==='ALIYUN::REDIS::Instance').length,2,
  'both response occurrences backed by the same-tool fact identify the Tair instance');
assert.equal(scan(JSON.stringify({UserName:'actual-user'}),
  {index:toolFieldIndex,context:{toolUseId:'ram-tool'}})[0].resourceType,'ALIYUN::RAM::User',
  'a genuine same-tool RAM user fact remains linkable');
    const amqpInstance={...instance,referenceId:'ref-amqp',resourceType:'ALIYUN::AMQP::Instance',
      identifier:'1234567890123456',identifierField:'InstanceId',
      aliases:[{identifier:'1234567890123456',identifierField:'InstanceName',identifierKind:'name'}],
  sources:[{turnId:'turn-amqp'}]};
const amqpIndex=new ResourceIndex({canonicalType:rules.canonicalResourceType});
await amqpIndex.loadSnapshot({schemaVersion:1,revision:1,items:[amqpInstance],bindings:[]},()=>assert.fail());
assert.equal(scan('1234567890123456',{index:amqpIndex,field:'完整 ID（AccountId）',
  context:{turnId:'turn-amqp'}}).length,0,
  'an account ID field must not be relinked as a same-turn AMQP instance with the same numeric value');
assert.equal(scan('1234567890123456',{index:amqpIndex,field:'完整 ID',
  context:{turnId:'turn-amqp'}})[0].resourceType,'ALIYUN::AMQP::Instance',
  'a generic localized ID column may still use its same-turn API fact');
const kmsAlias={...instance,referenceId:'ref-kms-alias',resourceType:'ALIYUN::KMS::Alias',
  identifier:'alias/design-test',identifierField:'AliasName',identifierKind:'name',
  aliases:[{identifier:'acs:kms:cn-hangzhou:1234567890123456:alias/design-test',
    identifierField:'AliasArn',identifierKind:'id'}],
  attributes:{AliasName:'alias/design-test',AliasArn:'acs:kms:cn-hangzhou:1234567890123456:alias/design-test',
    KeyId:'64ebb055-f83f-4cbe-8955-4aec41098e44'},sources:[{turnId:'turn-kms'}]};
const kmsAliasIndex=new ResourceIndex({canonicalType:rules.canonicalResourceType});
await kmsAliasIndex.loadSnapshot({schemaVersion:1,revision:1,items:[kmsAlias],bindings:[]},()=>assert.fail());
const kmsArnOccurrence=scan(kmsAlias.aliases[0].identifier,{index:kmsAliasIndex,
  field:'完整标识（AliasArn）',context:{turnId:'turn-kms'}})[0];
assert.equal(kmsArnOccurrence.identifierField,'AliasArn');
assert.equal(linkify.resolveResourceOccurrenceAlternatives(kmsArnOccurrence,kmsAliasIndex).url,
  'https://kms.console.aliyun.com/cn-hangzhou/key/detail/64ebb055-f83f-4cbe-8955-4aec41098e44');
const ensInstance={...instance,referenceId:'ref-ens',resourceType:'ALIYUN::ENS::Instance',
  identifier:'i-58euzynt1puvupqeq7bq4ys5d',identifierField:'PhysicalResourceId',scopeKind:'account'};
delete ensInstance.regionId;
const ensIndex=new ResourceIndex({canonicalType:rules.canonicalResourceType});
await ensIndex.loadSnapshot(
  {schemaVersion:1,revision:1,items:[ensInstance],bindings:[]},()=>assert.fail());
const ensOccurrence=scan('i-58euzynt1puvupqeq7bq4ys5d',{index:ensIndex})[0];
const ensResolution=linkify.resolveResourceOccurrenceAlternatives(ensOccurrence,ensIndex);
assert.equal(ensResolution.status,'resolved');
assert.equal(ensResolution.reference.resourceType,'ALIYUN::ENS::Instance',
  'a real API fact must beat an unbound ECS prefix guess for the same i-* token');
assert.equal(ensResolution.url,
  'https://ens.console.aliyun.com/#/instance/detail/i-58euzynt1puvupqeq7bq4ys5d');
const correctedTypeIndex=new ResourceIndex({canonicalType:rules.canonicalResourceType});
await correctedTypeIndex.loadSnapshot({schemaVersion:1,revision:1,items:[
  {...instance,identifier:ensInstance.identifier,referenceId:'ref-historical-ecs',sources:[{turnId:'turn-old'}]},
  {...ensInstance,referenceId:'ref-current-ens',sources:[{turnId:'turn-current'}]},
],bindings:[]},()=>assert.fail());
const correctedOccurrence=scan(ensInstance.identifier,
  {index:correctedTypeIndex,context:{turnId:'turn-current'}})[0];
const correctedResolution=linkify.resolveResourceOccurrenceAlternatives(correctedOccurrence,correctedTypeIndex);
assert.equal(correctedResolution.status,'resolved');
assert.equal(correctedResolution.reference.referenceId,'ref-current-ens',
  'a current-turn API fact must beat a conflicting historical type for the same locator');
const sharedPhysical='asg-design0000000001';
const scalingGroup={...instance,referenceId:'ref-scaling-group',resourceType:'ALIYUN::ESS::ScalingGroup',
  identifier:sharedPhysical,identifierField:'PhysicalResourceId',attributes:{}};
const scalingEnable={...scalingGroup,referenceId:'ref-scaling-enable',resourceType:'ALIYUN::ESS::ScalingGroupEnable',
  identifierField:'PhysicalResourceId',attributes:{}};
const sharedIndex=new ResourceIndex({canonicalType:rules.canonicalResourceType});
await sharedIndex.loadSnapshot(
  {schemaVersion:1,revision:1,items:[scalingGroup,scalingEnable],bindings:[]},()=>assert.fail());
const typedShared=scan('{"ResourceType":"ALIYUN::ESS::ScalingGroup","PhysicalResourceId":"'+
  sharedPhysical+'"}',{index:sharedIndex}).find(item=>item.identifier===sharedPhysical);
assert.equal(typedShared.semantic.typed,true);
const typedResolution=linkify.resolveResourceOccurrenceAlternatives(typedShared,sharedIndex);
assert.equal(typedResolution.status,'resolved','a same-object ResourceType must select its matching shared-ID route');
assert.ok(typedResolution.url.includes('/group/detail/'));
const untypedShared=scan('{"PhysicalResourceId":"'+sharedPhysical+'"}',{index:sharedIndex})
  .find(item=>item.identifier===sharedPhysical);
assert.equal(linkify.resolveResourceOccurrenceAlternatives(untypedShared,sharedIndex).status,'ambiguous',
  'the same shared ID without a local type must still require a choice');
for (const value of ['DeployRole','DisplayName: DeployRole','LogicalResourceId: DeployRole',
  'name = "DeployRole"', 'admin', 'RAM role DeployRoleBackup']) {
  assert.equal(matches(value).filter((item) => item.identifier === 'DeployRole').length, 0, value);
}
assert.equal(matches('DeployRole',{toolUseId:'t1'})[0].semantic.kind,'observed_fact',
  'an exact opaque name observed by the same visible owner is recognized');
assert.equal(matches('云账号报警联系人',{toolUseId:'t1'})[0].identifier,'云账号报警联系人',
  'an observed Unicode resource identifier is recognized');
assert.equal(matches('DeployRole',{toolUseId:'another-turn'}).length,0,
  'an opaque name fact cannot claim matching prose from another owner');
const opaqueIdIndex = new ResourceIndex({canonicalType:rules.canonicalResourceType});
const oldOts = {...instance,referenceId:'ref-old-ots',resourceType:'ALIYUN::OTS::Instance',
  identifier:'fvt-ros',identifierField:'PhysicalResourceId',identifierKind:'id',
  sources:[{turnId:'turn-old'}]};
const oldContact = {...contactGroup,referenceId:'ref-old-contact',identifier:'ros-ut',
  sources:[{turnId:'turn-other'}]};
const oldOtsCollision = {...oldOts,referenceId:'ref-old-ots-collision',identifier:'ros-ut'};
await opaqueIdIndex.loadSnapshot({schemaVersion:1,revision:1,
  items:[oldOts,oldContact,oldOtsCollision],bindings:[]},()=>assert.fail());
assert.equal(scan('磁盘名称 fvt-ros',{index:opaqueIdIndex,context:{turnId:'turn-current'}}).length,0,
  'a unique opaque ID fact from another turn cannot claim ordinary prose');
assert.equal(scan('北京 ros-ut',{index:opaqueIdIndex,context:{turnId:'turn-current'}}).length,0,
  'conflicting opaque ID facts from other turns cannot create a context button');
assert.equal(scan('实例 fvt-ros',{index:opaqueIdIndex,context:{turnId:'turn-old'}})[0].identifier,'fvt-ros',
  'the same-owner API fact still recognizes an opaque identifier');
const sameTurnRamUser={...role,referenceId:'ref-qitan-user',resourceType:'ALIYUN::RAM::User',
  identifier:'qitan',identifierField:'UserName',identifierKind:'name',sources:[{turnId:'turn-shared'}]};
const sameTurnMonitorGroup={...contactGroup,referenceId:'ref-qitan-monitor-group',
  resourceType:'ALIYUN::CMS::MonitorGroup',identifier:'61020318',identifierField:'PhysicalResourceId',
  identifierKind:'id',aliases:[{identifier:'qitan',identifierField:'GroupName',identifierKind:'name'}],
  sources:[{turnId:'turn-shared'}]};
const sameTurnCollisionIndex=new ResourceIndex({canonicalType:rules.canonicalResourceType});
await sameTurnCollisionIndex.loadSnapshot({schemaVersion:1,revision:1,
  items:[sameTurnRamUser,sameTurnMonitorGroup],bindings:[]},()=>assert.fail());
const rowScope=JSON.stringify(['table-row',{name:'qitan',id:'61020318'}]);
const sameTurnName=scan('qitan',{index:sameTurnCollisionIndex,
  context:{turnId:'turn-shared',structuralScope:rowScope}})[0];
const sameTurnResolution=linkify.resolveResourceOccurrenceAlternatives(sameTurnName,sameTurnCollisionIndex);
assert.equal(sameTurnResolution.status,'resolved');
assert.equal(sameTurnResolution.reference.referenceId,'ref-qitan-monitor-group',
  'an exact primary ID in the same table row selects the matching same-object name alias');
assert.equal(sameTurnResolution.url,'https://cloudmonitor.console.aliyun.com/app-group/61020318/overview');
const nameOnly=scan('qitan',{index:sameTurnCollisionIndex,
  context:{turnId:'turn-shared',structuralScope:JSON.stringify(['table-row',{name:'qitan'}])}})[0];
assert.equal(linkify.resolveResourceOccurrenceAlternatives(nameOnly,sameTurnCollisionIndex).status,'ambiguous',
  'a same-turn name collision without another exact row locator must remain explicit');
const duplicateNameBase={...instance,identifierField:'InstanceId',
  aliases:[{identifier:'duplicate-name',identifierField:'InstanceName',identifierKind:'name'}],
  sources:[{turnId:'turn-duplicate-name'}]};
const duplicateNameIndex=new ResourceIndex({canonicalType:rules.canonicalResourceType});
await duplicateNameIndex.loadSnapshot({schemaVersion:1,revision:1,items:[
  {...duplicateNameBase,identifier:'i-design0000000002',referenceId:'ref-duplicate-name-2'},
  {...duplicateNameBase,identifier:'i-design0000000003',referenceId:'ref-duplicate-name-3'},
],bindings:[]},()=>assert.fail());
const duplicateNameOccurrence=scan('duplicate-name',{index:duplicateNameIndex,context:{
  turnId:'turn-duplicate-name',regionId:'cn-hangzhou',
  structuralScope:JSON.stringify(['table-row',{name:'duplicate-name',id:'i-design0000000002',region:'cn-hangzhou'}]),
}})[0];
const duplicateNameResolution=linkify.resolveResourceOccurrenceAlternatives(
  duplicateNameOccurrence,duplicateNameIndex);
assert.equal(duplicateNameResolution.status,'resolved');
assert.equal(duplicateNameResolution.reference.referenceId,'ref-duplicate-name-2',
  'an exact primary ID in the same row disambiguates resources of the same type and region');
const duplicateNameOnly=scan('duplicate-name',{index:duplicateNameIndex,context:{
  turnId:'turn-duplicate-name',regionId:'cn-hangzhou',
  structuralScope:JSON.stringify(['table-row',{name:'duplicate-name',region:'cn-hangzhou'}]),
}})[0];
assert.equal(linkify.resolveResourceOccurrenceAlternatives(
  duplicateNameOnly,duplicateNameIndex).status,'ambiguous',
  'same-type duplicate names remain ambiguous without an exact sibling locator');
const duplicateNameAssignedId=scan('duplicate-name',{index:duplicateNameIndex,context:{
  turnId:'turn-duplicate-name',regionId:'cn-hangzhou',
  structuralScope:JSON.stringify(['table-row',{name:'duplicate-name',id:'InstanceId=i-design0000000002',region:'cn-hangzhou'}]),
}})[0];
assert.equal(linkify.resolveResourceOccurrenceAlternatives(
  duplicateNameAssignedId,duplicateNameIndex).reference.referenceId,'ref-duplicate-name-2',
  'a field-assignment cell exposes its exact value as a same-row locator');
const keyValueRecord=linkify.resourceTableRecordFromRows(['字段','值'],[
  ['名称（InstanceName）','duplicate-name'],
  ['完整 ID（InstanceId）','i-design0000000002'],
  ['地域（RegionId）','cn-hangzhou（可用区 cn-hangzhou-j）'],
]);
assert.deepEqual(keyValueRecord,{
  '名称（InstanceName）':'duplicate-name',
  '完整 ID（InstanceId）':'i-design0000000002',
  '地域（RegionId）':'cn-hangzhou（可用区 cn-hangzhou-j）',
});
const duplicateNameKeyValue=scan('duplicate-name',{index:duplicateNameIndex,context:{
  turnId:'turn-duplicate-name',regionId:'cn-hangzhou',
  structuralScope:JSON.stringify(['table-record',keyValueRecord]),
}})[0];
assert.equal(linkify.resolveResourceOccurrenceAlternatives(
  duplicateNameKeyValue,duplicateNameIndex).reference.referenceId,'ref-duplicate-name-2',
  'a two-column field/value table is one structured resource record across rows');
assert.equal(linkify.resourceTableContextFromObject(
  {'地域':'cn-hangzhou（可用区 cn-hangzhou-j）'}).regionId,'cn-hangzhou',
  'a display suffix does not become part of the region identifier');
assert.equal(linkify.resourceTableContextFromObject(
  {'地域':'HomeRegion=cn-hangzhou（TrailRegion=All）'}).regionId,'cn-hangzhou',
  'an ActionTrail region label is reduced to the exact console region identifier');
assert.equal(linkify.resourceTableContextFromObject(
  {RegionId:'HomeRegion=cn-hangzhou（TrailRegion=All）'}).regionId,'cn-hangzhou',
  'a formal RegionId column cannot bypass strict display-value parsing');
assert.equal(linkify.resourceTableRecordFromRows(['名称','值'],[['实例','duplicate-name']]),null,
  'an ordinary two-column table is not treated as a single resource record');
const duplicateNameOtherAccount={...duplicateNameBase,referenceId:'ref-duplicate-name-other-account',
  identifier:'i-design0000000002',accountScope:'other-account'};
const duplicateNameCrossAccountIndex=new ResourceIndex({canonicalType:rules.canonicalResourceType});
await duplicateNameCrossAccountIndex.loadSnapshot({schemaVersion:1,revision:1,items:[
  {...duplicateNameBase,identifier:'i-design0000000002',referenceId:'ref-duplicate-name-current-account'},
  duplicateNameOtherAccount,
],bindings:[]},()=>assert.fail());
const duplicateNameCrossAccountOccurrence=scan('duplicate-name',{index:duplicateNameCrossAccountIndex,context:{
  turnId:'turn-duplicate-name',regionId:'cn-hangzhou',
  structuralScope:JSON.stringify(['table-row',{name:'duplicate-name',id:'i-design0000000002',region:'cn-hangzhou'}]),
}})[0];
assert.equal(linkify.resolveResourceOccurrenceAlternatives(
  duplicateNameCrossAccountOccurrence,duplicateNameCrossAccountIndex).status,'ambiguous',
  'a sibling ID must not silently choose between accounts that reuse the same resource identity');
const keyPairBase={...instance,resourceType:'ALIYUN::ECS::SSHKeyPair',identifier:'ha',
  identifierField:'KeyPairName',identifierKind:'name',sources:[{turnId:'turn-keypairs'}]};
const keyPairIndex=new ResourceIndex({canonicalType:rules.canonicalResourceType});
await keyPairIndex.loadSnapshot({schemaVersion:1,revision:1,items:[
  {...keyPairBase,referenceId:'ref-keypair-hangzhou',regionId:'cn-hangzhou'},
  {...keyPairBase,referenceId:'ref-keypair-shanghai',regionId:'cn-shanghai'},
],bindings:[]},()=>assert.fail());
const keyPairRowContext=linkify.resourceTableContextFromObject(
  {类别:'SSH 密钥对',名称:'ha',地域:'cn-hangzhou'}, {turnId:'turn-keypairs'});
const keyPairOccurrence=scan('ha',{index:keyPairIndex,context:keyPairRowContext})[0];
const keyPairResolution=linkify.resolveResourceOccurrenceAlternatives(keyPairOccurrence,keyPairIndex);
assert.equal(keyPairResolution.status,'resolved');
assert.equal(keyPairResolution.reference.referenceId,'ref-keypair-hangzhou',
  'an exact localized region column disambiguates same-name resources from different regions');
assert.equal(keyPairResolution.url,'https://ecs.console.aliyun.com/#/keyPair/region/cn-hangzhou');
const keyPairWithoutRegion=scan('ha',{index:keyPairIndex,context:{turnId:'turn-keypairs'}})[0];
assert.equal(linkify.resolveResourceOccurrenceAlternatives(keyPairWithoutRegion,keyPairIndex).status,'ambiguous',
  'the same-name resource remains ambiguous when the row has no region column');
const unrelatedRegion=linkify.resourceTableContextFromObject(
  {名称:'ha',备注:'cn-hangzhou'}, {turnId:'turn-keypairs'});
assert.equal(unrelatedRegion.regionId,undefined,
  'an exact region value in an unrelated column is not treated as structural region context');
const proseRegion=linkify.resourceTableContextFromObject(
  {名称:'ha',地域:'样本 cn-hangzhou'}, {turnId:'turn-keypairs'});
assert.equal(proseRegion.regionId,undefined,
  'a region substring inside prose is not accepted as an exact region value');
const sameRegionOtherAccount={...keyPairBase,referenceId:'ref-keypair-other-account',
  regionId:'cn-hangzhou',accountScope:'other-account'};
const crossAccountKeyPairIndex=new ResourceIndex({canonicalType:rules.canonicalResourceType});
await crossAccountKeyPairIndex.loadSnapshot({schemaVersion:1,revision:1,items:[
  {...keyPairBase,referenceId:'ref-keypair-current-account',regionId:'cn-hangzhou'},
  sameRegionOtherAccount,
],bindings:[]},()=>assert.fail());
const crossAccountOccurrence=scan('ha',{index:crossAccountKeyPairIndex,context:keyPairRowContext})[0];
assert.equal(linkify.resolveResourceOccurrenceAlternatives(crossAccountOccurrence,crossAccountKeyPairIndex).status,
  'ambiguous','a region column must not silently choose between accounts in the same region');
const vpcIdentifier='vpc-design0000000001';
const vpcBase={...instance,referenceId:'ref-vpc-account-a',resourceType:'ALIYUN::ECS::VPC',
  identifier:vpcIdentifier,identifierField:'VpcId',accountScope:'account-a',
  sources:[{turnId:'turn-old-a'}]};
const vpcOtherAccount={...vpcBase,referenceId:'ref-vpc-account-b',accountScope:'account-b',
  sources:[{turnId:'turn-old-b'}]};
const accountWitness={...instance,referenceId:'ref-account-witness',identifier:'i-design0000000009',
  accountScope:'account-a',sources:[{turnId:'turn-context'}]};
const observedAccountIndex=new ResourceIndex({canonicalType:rules.canonicalResourceType});
await observedAccountIndex.loadSnapshot({schemaVersion:1,revision:1,
  items:[vpcBase,vpcOtherAccount,accountWitness],bindings:[]},()=>assert.fail());
const observedAccountOccurrence=scan(vpcIdentifier,{index:observedAccountIndex,
  context:{turnId:'turn-context',regionId:'cn-hangzhou'}})[0];
const observedAccountResolution=linkify.resolveResourceOccurrenceAlternatives(
  observedAccountOccurrence,observedAccountIndex);
assert.equal(observedAccountResolution.status,'resolved');
assert.equal(observedAccountResolution.reference.referenceId,'ref-vpc-account-a',
  'a real same-turn fact scopes a historical cross-account parent ID');
const conflictingAccountIndex=new ResourceIndex({canonicalType:rules.canonicalResourceType});
await conflictingAccountIndex.loadSnapshot({schemaVersion:1,revision:1,items:[
  vpcBase,vpcOtherAccount,accountWitness,
  {...accountWitness,referenceId:'ref-other-account-witness',identifier:'i-design0000000010',accountScope:'account-b'},
],bindings:[]},()=>assert.fail());
const conflictingAccountOccurrence=scan(vpcIdentifier,{index:conflictingAccountIndex,
  context:{turnId:'turn-context',regionId:'cn-hangzhou'}})[0];
assert.equal(linkify.resolveResourceOccurrenceAlternatives(
  conflictingAccountOccurrence,conflictingAccountIndex).status,'ambiguous',
  'two accounts genuinely observed in one turn must not be silently selected');
assert.equal(matches('RAM 角色 `DeployRole`')[0].identifierKind,'name');
assert.equal(matches('RAM roles and RAM users').length,0,'plural words must not produce names');
for (const value of ['RAM role is <RoleName>','RAM role named','RAM role name: ${ROLE}']) {
  assert.equal(matches(value).length,0,'connectors are not names: '+value);
}
assert.equal(matches('RAM role named DeployRole')[0].identifier,'DeployRole');
assert.equal(matches('RAM role name: DeployRole')[0].identifier,'DeployRole');
assert.equal(matches('RAM role DeployRole.')[0].identifier,'DeployRole','known prose name excludes the period');
assert.equal(matches('RAM role DeployRole...').filter(item=>item.identifier==='DeployRole').length,0);
assert.equal(matches('RAM role "DeployRole."').filter(item=>item.identifier==='DeployRole').length,0);
assert.equal(matches('RAM role UnknownRole.')[0].identifier,'UnknownRole.','unknown spelling is not guessed');
const dotted = {...role,identifier:'DeployRole.',referenceId:'dotted-role'};
assert.equal(scan('RAM role DeployRole.',{index:{candidates:({identifier})=>
  [role,dotted].filter(item=>item.identifier===identifier)}})[0].identifier,'DeployRole.','full name wins');
const pendingNames=[];
scan('RAM role DeployRole.',{trackCandidate:item=>pendingNames.push(item)});
assert.ok(pendingNames.some(item=>item.identifier==='DeployRole'),'later name facts invalidate the prose occurrence');
assert.equal(matches('x'.repeat(100000)).length,0,'long non-URL words are handled without suffix backtracking');
assert.equal(matches('{"RoleName":"DeployRole"}')[0].identifierKind,'name');
assert.equal(matches('resource "alicloud_ram_role" "LogicalOnly" { name = "DeployRole" }')[0].identifier,'DeployRole');
assert.equal(matches('aliyun ram GetRole --RoleName DeployRole')[0].identifier,'DeployRole');
assert.equal(matches('resource "alicloud_ram_role" "DeployRole" {}').length,0);
assert.equal(matches('{"Type":"ALIYUN::RAM::Role","PhysicalResourceId":"UnverifiedName"}').length,0);
const rawFunction={ResourceType:'ALIYUN::FC::Function',PhysicalResourceId:'RealFunction',
  ResourceAttributes:{FunctionName:'RealFunction'}};
const namedPhysical=scan('RealFunction',{context:resourceContextFromObject(rawFunction),field:'PhysicalResourceId'});
assert.equal(namedPhysical[0].identifierKind,'name');assert.equal(namedPhysical[0].identifierField,'FunctionName');
const stableRole=scan('123456789',{context:{resourceType:'ALIYUN::RAM::Role'},field:'RoleId'});
assert.equal(stableRole[0].identifierKind,'id','an explicit stable ID field is not the preferred name field');
assert.equal(stableRole[0].identifierField,'RoleId');
const nasRule={...instance,referenceId:'ref-nas-rule-zero',resourceType:'ALIYUN::NAS::AccessRule',
  identifier:'0',identifierField:'PhysicalResourceId',identifierKind:'id',
  attributes:{PhysicalResourceId:'0',AccessRuleId:'0',AccessGroupName:'DEFAULT_VPC_GROUP_NAME',
    AccessGroupType:'Vpc',FileSystemType:'standard'},sources:[{turnId:'turn-nas-rule'}]};
const nasRuleIndex=new ResourceIndex({canonicalType:rules.canonicalResourceType});
await nasRuleIndex.loadSnapshot({schemaVersion:1,revision:1,items:[nasRule],bindings:[]},()=>assert.fail());
for (const source of [
  ['0','规则完整 ID（AccessRuleId）'],
  ['AccessRuleId: 0',null],
]) {
  const occurrence=scan(source[0],{index:nasRuleIndex,context:{turnId:'turn-nas-rule'},field:source[1]})
    .find(item=>item.identifier==='0');
  assert.ok(occurrence,'a short numeric resource ID is recognized in its explicit identity field');
  assert.equal(linkify.resolveResourceOccurrenceAlternatives(occurrence,nasRuleIndex).status,'resolved');
}
for (const text of ['0','PairNumber=0','MessageCount: 0','TotalCount=0']) {
  assert.equal(scan(text,{index:nasRuleIndex,context:{turnId:'turn-nas-rule'}})
    .filter(item=>item.identifier==='0').length,0,
  'a same-turn short numeric resource fact must not claim unrelated numbers: '+text);
}
assert.equal(matches(JSON.stringify(rawFunction)).filter(item=>item.identifier==='RealFunction').length,2,
  'physical and explicit name values in the same object both have name semantics');
const yamlFunction='ResourceType: ALIYUN::FC::Function\nPhysicalResourceId: RealFunction\n'+
  'ResourceAttributes:\n  FunctionName: RealFunction';
assert.equal(matches(yamlFunction)
  .filter(item=>item.identifier==='RealFunction' && item.identifierKind==='name').length,2,
  'YAML direct resource attributes must explain the same physical name');
const multiDocumentYaml='---\nResourceType: ALIYUN::ACTIONTRAIL::Trail\nName: shared-name\nRegionId: cn-shanghai\n'+
  '---\nResourceType: ALIYUN::RAM::Role\nRoleName: shared-name\nRegionId: cn-hangzhou\n';
const multiDocumentMatches=scan(multiDocumentYaml,{index});
assert.ok(multiDocumentMatches.some(item=>item.identifier==='shared-name' &&
  item.resourceType==='ALIYUN::ACTIONTRAIL::Trail' && item.regionId==='cn-shanghai'),
  'YAML document separators must preserve the first resource type and region');
assert.ok(multiDocumentMatches.some(item=>item.identifier==='shared-name' &&
  item.resourceType==='ALIYUN::RAM::Role' && item.regionId==='cn-hangzhou'),
  'YAML document separators must isolate the second resource scope');
for (const yaml of [
  yamlFunction.replace('ResourceAttributes:', 'Unrelated:'),
  yamlFunction.replace('  FunctionName:', '  - FunctionName:'),
  'ResourceType: ALIYUN::FC::Function\nPhysicalResourceId: RealFunction\n'+
    'ResourceAttributes:\n  Nested:\n    FunctionName: RealFunction',
  '- ResourceType: ALIYUN::FC::Function\n  PhysicalResourceId: RealFunction\n'+
    '- ResourceAttributes:\n    FunctionName: RealFunction',
]) assert.ok(!matches(yaml).some(item=>item.semantic?.field==='PhysicalResourceId' && item.identifierKind==='name'),
  'YAML name proof cannot cross a sibling, unrelated object, array or deeper property');
for (const value of [
  {...rawFunction,ResourceAttributes:{FunctionName:'DifferentFunction'}},
  {...rawFunction,ResourceAttributes:{DisplayName:'RealFunction'}},
  {...rawFunction,FunctionName:'ConflictingName'},
]) {
  const unproved=scan('RealFunction',{context:resourceContextFromObject(value),field:'PhysicalResourceId'});
  assert.equal(unproved[0].identifierKind,'id','an unproved physical value must not become a name');
}
const sibling=[{...rawFunction,ResourceAttributes:{}},
  {ResourceType:'ALIYUN::FC::Function',FunctionName:'RealFunction'}];
assert.equal(matches(JSON.stringify(sibling)).length,1,'name proof cannot cross array elements');
const nested=resourceContextFromObject({ResourceType:'ALIYUN::FC::Function'},resourceContextFromObject(rawFunction));
assert.equal(scan('RealFunction',{context:nested,field:'PhysicalResourceId'})[0].identifierKind,'id',
  'name proof cannot leak into another nested object');
assert.equal(matches('实例i-design0000000001已创建')[0].identifier,instance.identifier);
for (const value of ['Instance i-design0000000001.', 'Instance i-design0000000001. Next sentence.',
  'Instance (i-design0000000001.)']) {
  const [item] = matches(value);
  assert.equal(item.identifier,instance.identifier,value);
  assert.equal(value.slice(item.start,item.end),instance.identifier,'copy excludes prose punctuation');
}
assert.equal(scan('i-design0000000001.',{index,complete:false}).length,0,'unfinished suffix can still grow');
assert.equal(scan('i-design0000000001. ',{index,complete:false})[0].identifier,instance.identifier);
const tracked = [];
scan('i-design0000000001.',{trackCandidate:item=>tracked.push(item)});
assert.ok(tracked.some(item=>item.identifier===instance.identifier),'late facts backfill the trimmed ID');
for (const value of ['i-design0000000001...', 'i-design0000000001.suffix.',
  'i-design0000000001/child.', '"i-design0000000001."', 'https://example.test/i-design0000000001.',
  '{"InstanceId":"i-design0000000001."}', 'InstanceId: i-design0000000001.']) {
  assert.equal(matches(value).filter(item=>item.identifier===instance.identifier).length,0,
    'longer literal must not become an inner ID: '+value);
}
for (const value of ['i-***','<i-design0000000001>','${i-design0000000001}',
  'https://example.test/i-design0000000001','i-design0000000001-suffix/child']) {
  assert.equal(matches(value).length,0,value);
}
const rows = matches('[{"RoleName":"DeployRole","RegionId":"cn-beijing"},'
  +'{"RoleName":"AnotherRole","RegionId":"cn-shanghai"}]');
assert.deepEqual(rows.map((item) => item.regionId),['cn-beijing','cn-shanghai']);
const escaped = matches('{"RoleName":"Deploy\\u0052ole"}');
assert.equal(escaped[0].identifier,'DeployRole');
assert.equal(escaped[0].displayText,'Deploy\\u0052ole');
const fixtureRule = rules.RESOURCE_LINK_RULES.find((item) =>
  item.sourceType === 'ALIYUN::RAM::Role' && item.partition === 'aliyun');
assert.equal(fixtureRule.releaseStatus,'verified');
const occurrence = {identifier:'DeployRole', identifierKind:'name', resourceType:role.resourceType,
  semantic:{kind:'explicit_type'}};
assert.equal(rules.resolveResourceOccurrence(occurrence,index,[fixtureRule]).status,'resolved');
assert.equal(rules.resolveResourceOccurrence({...occurrence,semantic:null},index,[fixtureRule]).status,'not_resource');
assert.equal(rules.resolveResourceOccurrence(occurrence,index).status,'resolved',
  'production RAM route must be enabled');
const noRegion = rules.resolveResourceOccurrence(occurrence,index,[fixtureRule]);
assert.ok(!noRegion.reference.regionId);
const noAccount = {candidates:()=>[{...role,accountScope:undefined}],bindingFor:()=>null};
assert.deepEqual(rules.resolveResourceOccurrence(occurrence,noAccount,[fixtureRule]).missing,['accountScope']);
const contextIndex = new ResourceIndex({canonicalType:rules.canonicalResourceType});
const turnInstance = {...instance,referenceId:'ref-turn-instance',sources:[{turnId:'turn-current'}]};
await contextIndex.loadSnapshot({schemaVersion:1,revision:1,items:[turnInstance],bindings:[]}, () => assert.fail());
const contextualDisk = {identifier:'d-context123456',identifierKind:'id',identifierField:'DiskId',
  resourceType:'ALIYUN::ECS::Disk',turnId:'turn-current'};
assert.equal(rules.resolveResourceOccurrence(contextualDisk,contextIndex).status,'resolved',
  'a recognized ID reuses the one cloud context proved in its own turn');
contextIndex.applyDelta({schemaVersion:1,baseRevision:1,revision:2,upserts:[
  {...turnInstance,referenceId:'ref-turn-other-region',regionId:'cn-beijing'}],removedReferenceIds:[],bindings:[]});
assert.deepEqual(rules.resolveResourceOccurrence(contextualDisk,contextIndex).missing,
  ['accountScope','consolePartition','regionId'],
  'conflicting turn contexts still require an explicit choice');
const deleted = {candidates:()=>[{...role,status:'DELETE_COMPLETE'}],bindingFor:()=>null};
assert.equal(rules.resolveResourceOccurrence(occurrence,deleted,[fixtureRule]).reason,'deleted_name');
const other = {...role,referenceId:'ref-other',accountScope:'other-account',sources:[{toolUseId:'t2'}]};
index.applyDelta({schemaVersion:1,baseRevision:1,revision:2,upserts:[other],removedReferenceIds:[],bindings:[]});
assert.equal(rules.resolveResourceOccurrence(occurrence,index,[fixtureRule]).status,'ambiguous');
assert.equal(rules.resolveResourceOccurrence({...occurrence,toolUseId:'t1'},index,[fixtureRule]).status,'resolved');
const stackId='a67cf255-4dae-49cd-8344-c18d2f2b374a';
const stack={...instance,referenceId:'ref-stack-current',resourceType:'ALIYUN::ROS::Stack',identifier:stackId,
  identifierField:'StackId',accountScope:'current-account',consolePartition:'aliyun',regionId:'cn-hangzhou'};
const oldStack={...stack,referenceId:'ref-stack-old',accountScope:undefined,consolePartition:undefined};
const olderStack={...oldStack,referenceId:'ref-stack-older'};
const stackOccurrence={identifier:stackId,identifierKind:'id',resourceType:stack.resourceType,
  semantic:{kind:'explicit_type'}};
const stackRule=rules.RESOURCE_LINK_RULES.find(item=>item.resourceType===stack.resourceType&&item.partition==='aliyun');
const repeatedStack=rules.resolveResourceOccurrence(stackOccurrence,
  {candidates:()=>[oldStack,olderStack,stack],bindingFor:()=>null},[stackRule]);
assert.equal(repeatedStack.status,'resolved','a later scoped ID fact must dominate compatible historical duplicates');
assert.equal(repeatedStack.url,`https://ros.console.aliyun.com/cn-hangzhou/stacks/${stackId}`);
assert.equal(rules.resolveResourceOccurrence(stackOccurrence,
  {candidates:()=>[oldStack,stack,{...stack,referenceId:'ref-stack-other',accountScope:'other-account'}],
    bindingFor:()=>null},[stackRule]).status,'ambiguous','conflicting known accounts must still require a choice');
assert.equal(rules.resolveResourceOccurrence(occurrence,
  {candidates:()=>[{...role,accountScope:undefined},role],bindingFor:()=>null},[fixtureRule]).status,'ambiguous',
  'a later scoped name cannot claim an unknown-account historical name');
assert.equal(rules.resolveResourceChoice(occurrence,role,index,[fixtureRule]).status,'resolved');
assert.equal(rules.resolveResourceChoice(occurrence,{...role,accountScope:'other-account'},index,[fixtureRule]),null);
assert.equal(rules.resolveResourceChoice({...occurrence,accountScope:'other-account'},role,index,[fixtureRule]),null);
assert.equal(rules.resolveResourceChoice(occurrence,{...role,referenceId:'ref-not-present'},index,[fixtureRule]),null);
index.applyDelta({schemaVersion:1,baseRevision:2,revision:3,upserts:[{...role,status:'DELETE_COMPLETE'}],removedReferenceIds:[],bindings:[]});
assert.equal(rules.resolveResourceChoice(occurrence,role,index,[fixtureRule]).reason,'deleted_name',
  'old UI state cannot revive a deleted name');
const oldRole={...role,status:'DELETE_COMPLETE'};
const recreatedRole={...role,referenceId:'ref-recreated',status:'CREATE_COMPLETE',evidence:'api_response'};
const lifecycleIndex={candidates:()=>[oldRole,recreatedRole,other],bindingFor:()=>oldRole};
const historical=rules.resolveResourceOccurrence(occurrence,lifecycleIndex,[fixtureRule]);
assert.equal(historical.reason,'deleted_name','a saved old binding must not follow the new same-name object');
assert.equal(historical.reference.referenceId,oldRole.referenceId);
assert.deepEqual(historical.replacements.map(item=>item.referenceId),['ref-recreated'],
  'explicit replacement choices must stay in the old account and namespace');
for (const extra of [
  {...recreatedRole,evidence:'user_context'}, {...recreatedRole,status:'DELETE_COMPLETE'},
  {...recreatedRole,accountScope:'other-account'}, {...recreatedRole,consolePartition:'alibabacloud'},
  {...recreatedRole,resourceType:'ALIYUN::RAM::User'}, {...recreatedRole,identifier:'AnotherRole'},
]) {
  assert.deepEqual(rules.resolveResourceOccurrence(occurrence,
    {candidates:()=>[oldRole,extra],bindingFor:()=>oldRole},[fixtureRule]).replacements,[]);
}
const aliasOccurrence={...occurrence,identifier:'111111111111111111',identifierKind:'id'};
assert.deepEqual(rules.resolveResourceOccurrence(aliasOccurrence,lifecycleIndex,[fixtureRule]).replacements,[],
  'an old object ID must not offer another object via its reused name');
assert.deepEqual(rules.resolveResourceOccurrence(occurrence,
  {candidates:()=>[{...oldRole,accountScope:undefined},recreatedRole],
    bindingFor:()=>({...oldRole,accountScope:undefined})},[fixtureRule]).replacements,[],
  'unknown historical account is not proof of a shared namespace');
const oldFunction={...oldRole,resourceType:'ALIYUN::FC::Function',identifierField:'FunctionName',
  scopeKind:'regional',regionId:'cn-hangzhou',attributes:{ServiceName:'ParentA'}};
const newFunction={...oldFunction,referenceId:'ref-function-new',status:'CREATE_COMPLETE',evidence:'api_response'};
const functionOccurrence={...occurrence,resourceType:oldFunction.resourceType};
const functionRule={...fixtureRule,resourceType:oldFunction.resourceType,identifierField:'FunctionName'};
for (const candidate of [newFunction,{...newFunction,regionId:'cn-beijing'},
  {...newFunction,attributes:{ServiceName:'ParentB'}},{...newFunction,attributes:{}}]) {
  const result=rules.resolveResourceOccurrence(functionOccurrence,
    {candidates:()=>[oldFunction,candidate],bindingFor:()=>oldFunction},[functionRule]);
  assert.deepEqual(result.replacements.map(item=>item.referenceId),candidate===newFunction ? ['ref-function-new'] : [],
    'replacement must have the same observed region and parent namespace');
}
assert.equal(rules.buildResourceConsoleUrl(fixtureRule,{...role,identifier:'A?x=1&y#z'}),
  'https://ram.console.aliyun.com/roles/detail?roleName=A%3Fx%3D1%26y%23z');
for (const value of ['..','.','<name>','role\nname','https://evil.test/name','${name}','r***']) {
  assert.equal(rules.buildResourceConsoleUrl(fixtureRule,{...role,identifier:value}),null,value);
}
for (const build of [()=> 'javascript:alert(1)', ()=> 'https://ram.console.aliyun.com.evil.test/roles/x',
  ()=> 'https://attacker@ram.console.aliyun.com/roles/x', ()=> 'https://ram.console.aliyun.com:1234/roles/x']) {
  assert.equal(rules.buildResourceConsoleUrl({...fixtureRule,build},role),null);
}
assert.equal(rules.buildResourceConsoleUrl({...fixtureRule,host:'evil.test',build:()=> 'https://evil.test'},role),null);
assert.equal(rules.buildResourceConsoleUrl(fixtureRule,{...role,accountScope:undefined}),null,
  'legacy output navigation must not relax new resource identity checks');
console.log(JSON.stringify({passed:true}));
""",
        encoding="utf-8",
    )
    result = subprocess.run([node, str(script)], capture_output=True, text=True, encoding="utf-8", timeout=20)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"passed": True}


def test_production_channel_routes_and_ros_network_aliases(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is unavailable")
    script = tmp_path / "production-route-test.mjs"
    script.write_text(
        CATALOG_BOOTSTRAP
        + "import assert from 'node:assert/strict';\n"
        + "const rules = await import("
        + json.dumps((STATIC / "resource_link_rules.js").as_uri())
        + ");\n"
        + r"""
const {createHash}=await import('node:crypto');
assert.equal(rules.canonicalResourceType('ALIYUN::ECS::VPC'),'ALIYUN::VPC::VPC');
assert.equal(rules.canonicalResourceType('ALIYUN::ECS::VSwitch'),'ALIYUN::VPC::VSwitch');
assert.equal(Object.keys(rules.ROS_RESOURCE_LINK_CATALOG.official).length,407);
assert.equal(Object.keys(rules.ROS_RESOURCE_LINK_CATALOG.sin).length,407);
    assert.equal(rules.RESOURCE_LINK_RULES.length,883,
  '406 source routes and one reviewed alias per channel plus runtime overrides');
const digest=(value)=>createHash('sha256').update(JSON.stringify(
  Object.entries(value).filter(([type])=>type!=='alicloud_slb_acl').sort(([a],[b])=>a.localeCompare(b)))).digest('hex');
assert.equal(digest(rules.ROS_RESOURCE_LINK_CATALOG.official),
      'f2c15f86f1f0753f5a14494fc3e97ea8a443ce89ea6c6c6751a6a4eeb4f39d05');
assert.equal(digest(rules.ROS_RESOURCE_LINK_CATALOG.sin),
  '290fc4fbbb8f87f04d9cca4c72d166f34474dd50c9c8d564aa2f777ab0dab7b7');
for (const partition of ['aliyun','alibabacloud']) {
  const channelRules=rules.RESOURCE_LINK_RULES.filter(item=>item.provenance.kind==='production-channel-config'&&
    item.partition===partition);
      assert.equal(channelRules.length,partition==='aliyun' ? 405 : 407);
  const channelCatalog=partition==='aliyun'
    ? rules.ROS_RESOURCE_LINK_CATALOG.official
    : rules.ROS_RESOURCE_LINK_CATALOG.sin;
  assert.deepEqual(new Set(channelRules.map(item=>item.sourceType)),
        new Set(Object.keys(channelCatalog).filter(type=>
          partition!=='aliyun'||!['alicloud_rds_parameter_group','alicloud_nas_access_group'].includes(type))));
  for (const rule of channelRules) {
    const spec=rules.resourceSpec(rule.sourceType);
    const attributes=Object.fromEntries(rule.requiredFields.map(field=>
      [field,field==='DestinationRegionId' ? 'cn-shanghai' :
        (field==='RouterType' ? 'VBR' : (field==='NetworkType' ? 'intranet' :
          (field==='NatGatewayId' ? 'ngw-test123456' : `${field}-value`)))]));
    if (rule.sourceType==='alicloud_slb_listener') Object.assign(attributes,
      {load_balancer_id:'lb-fixture',LoadBalancerId:'lb-fixture',ListenerProtocol:'http',ListenerPort:'80'});
    if (rule.sourceType==='alicloud_slb_rule') Object.assign(attributes,
      {LoadBalancerId:'lb-fixture',ListenerProtocol:'http',ListenerPort:'80'});
    const identifier=rule.sourceType==='alicloud_slb_listener' ? 'lb-fixture:http:80'
      : rule.sourceType==='ALIYUN::CEN::RouteEntry'
      ? 'cen-test123456/vpc-test123456/vtb-test123456'
      : spec.resourceType==='ALIYUN::ECS::NetworkInterface'
      ? 'eni-offline0001'
      : (spec.identifierKind==='name' ? 'resource-name' : 'resource-physical-1');
    const url=rules.buildResourceConsoleUrl(rule,{
      resourceType:rule.sourceType,identifierField:spec.identifierField,identifierKind:spec.identifierKind,
      identifier,
      regionId:'cn-hangzhou',accountScope:'fake-account',consolePartition:partition,attributes,
      logicalResourceId:'LogicalResource',
    });
    assert.ok(url,`${partition}:${rule.sourceType}`);
    assert.ok(!url.includes('undefined')&&!/[{}]/.test(url),url);
  }
}

function build(sourceType,partition,identifier,regionId='cn-hangzhou',attributes={},sources=[]) {
  const matching=rules.RESOURCE_LINK_RULES.filter(item=>
    item.sourceType===sourceType&&item.partition===partition);
  assert.ok(matching.length,`${partition}:${sourceType}`);
  const spec=rules.resourceSpec(sourceType);
  for (const rule of matching) {
    assert.equal(rule.releaseStatus,'verified');
    assert.ok(['production-channel-config','live-console-route'].includes(rule.provenance.kind));
    const url=rules.buildResourceConsoleUrl(rule,{
      resourceType:sourceType,identifierField:spec.identifierField,identifierKind:spec.identifierKind,
      identifier,regionId,accountScope:'fake-account',consolePartition:partition,attributes,sources,
    });
    if (url) return url;
  }
  return null;
}
    const fixtures = [
      ['ALIYUN::ACTIONTRAIL::Trail','aliyun','audit-trail','cn-hangzhou',{HomeRegion:'cn-hangzhou'},
        'https://actiontrail.console.aliyun.com/cn-hangzhou/trail-list/audit-trail'],
      ['ALIYUN::CR::Namespace','aliyun','crn-test123456','cn-beijing',{InstanceId:'cri-test123456'},
        'https://cr.console.aliyun.com/cn-beijing/instance/cri-test123456/namespaces'],
      ['ALIYUN::CR::Repository','aliyun','crr-test123456','cn-beijing',
        {InstanceId:'cri-test123456',RepoNamespaceName:'production',RepoName:'backend'},
        'https://cr.console.aliyun.com/repository/cn-beijing/cri-test123456/production/backend/details'],
      ['ALIYUN::ElasticSearch::Instance','aliyun','es-test123456','cn-hangzhou',{},
        'https://elasticsearch.console.aliyun.com/cn-hangzhou/instances/es-test123456'],
      ['ALIYUN::KMS::Alias','aliyun','alias/example','cn-hangzhou',{KeyId:'key-test123456'},
        'https://kms.console.aliyun.com/cn-hangzhou/key/detail/key-test123456'],
      ['ALIYUN::ResourceManager::ResourceGroup','aliyun','rg-test123456',null,{},
        'https://resourcemanager.console.aliyun.com/resource-groups/rg-test123456'],
      ['ALIYUN::SAE::Application','aliyun','app-test123456','cn-hangzhou',{},
        'https://saenext.console.aliyun.com/cn-hangzhou/app-list/app-test123456/micro-app/base'],
      ['ALIYUN::SAE::Namespace','aliyun','test','cn-hangzhou',{},
        'https://saenext.console.aliyun.com/cn-hangzhou/namespace'],
      ['ALIYUN::AMQP::Instance','aliyun','amqp-cn-test123456','cn-hangzhou',{},
        'https://amqp.console.aliyun.com/region/cn-hangzhou/instance/amqp-cn-test123456/instance-detail'],
      ['ALIYUN::ECS::Instance','aliyun','i-test123456','cn-hangzhou',{},
    'https://ecs.console.aliyun.com/server/i-test123456/detail?regionId=cn-hangzhou'],
  ['ALIYUN::ECS::SecurityGroup','aliyun','sg-test123456','cn-hangzhou',{},
    'https://ecs.console.aliyun.com/#/securityGroupDetail/region/cn-hangzhou/groupId/sg-test123456/instance'],
  ['ALIYUN::ECS::SecurityGroup','alibabacloud','sg-test123456','ap-southeast-1',{},
    'https://ecs.console.alibabacloud.com/#/securityGroupDetail/region/ap-southeast-1/groupId/sg-test123456/instance'],
  ['ALIYUN::ECS::Disk','aliyun','d-test123456','cn-hangzhou',{},
    'https://ecs.console.aliyun.com/diskdetail/d-test123456/detail?regionId=cn-hangzhou'],
  ['ALIYUN::ECS::Snapshot','aliyun','s-test123456','cn-hangzhou',{},
    'https://ecs.console.aliyun.com/snapshotDetail/cn-hangzhou/s-test123456/detail'],
  ['ALIYUN::ECS::Image','aliyun','m-test123456','cn-hangzhou',{},
    'https://ecs.console.aliyun.com/imageDetail/region/cn-hangzhou/imageId/m-test123456'],
  ['ALIYUN::ECS::NetworkInterface','aliyun','eni-test123456','cn-hangzhou',{},
    'https://ecs.console.aliyun.com/networkInterfaces/region/cn-hangzhou/detail/eni-test123456'],
  ['ALIYUN::ECS::SSHKeyPair','aliyun','deployment-key','cn-hangzhou',{},
    'https://ecs.console.aliyun.com/#/keyPair/region/cn-hangzhou'],
  ['ALIYUN::ECS::AutoProvisioningGroup','aliyun','apg-test123456','cn-hangzhou',{},
    'https://ecs.console.aliyun.com/fleet/region/cn-hangzhou/autoProvisioningGroupId/apg-test123456/groupConfig'],
  ['ALIYUN::ECS::AutoSnapshotPolicy','aliyun','sp-test123456','cn-hangzhou',{},
    'https://ecs.console.aliyun.com/autoSnapshotPolicyDetail/cn-hangzhou/sp-test123456/detail'],
  ['ALIYUN::ECS::LaunchTemplate','aliyun','lt-test123456','cn-hangzhou',{},
    'https://ecs.console.aliyun.com/launch-template-detail/region/cn-hangzhou/launchTemplateId/lt-test123456'],
  ['ALIYUN::ECS::DedicatedHost','aliyun','dh-test123456','cn-beijing',{},
    'https://ecs.console.aliyun.com/ddh/host/dh-test123456/detail?regionId=cn-beijing'],
  ['ALIYUN::ROS::StackGroup','aliyun','29dd2de2-4872-429a-bb82-95930182fa94','cn-hangzhou',
    {StackGroupName:'ros-fvt-pre-cfff5744'},
    'https://ros.console.aliyun.com/cn-hangzhou/stackGroup/ros-fvt-pre-cfff5744'],
  ['ALIYUN::ECS::VPC','aliyun','vpc-test123456','cn-hangzhou',{},
    'https://vpc.console.aliyun.com/#/vpc/cn-hangzhou/detail/vpc-test123456/info'],
  ['ALIYUN::ECS::VSwitch','alibabacloud','vsw-test123456','ap-southeast-1',{},
    'https://vpc.console.alibabacloud.com/vpc/ap-southeast-1/switches/vsw-test123456'],
  ['ALIYUN::VPC::EIP','aliyun','eip-test123456','cn-hangzhou',{},
    'https://vpc.console.aliyun.com/eip/cn-hangzhou/eips?AllocationId=eip-test123456'],
  ['ALIYUN::RDS::DBInstance','alibabacloud','rm-test123456','ap-southeast-1',{},
    'https://rds.console.alibabacloud.com/#/detail/rm-test123456/basicInfo?region=ap-southeast-1'],
  ['ALIYUN::RAM::Role','aliyun','DeployRole',null,{},
    'https://ram.console.aliyun.com/roles/detail?roleName=DeployRole'],
  ['ALIYUN::OSS::Bucket','alibabacloud','example-bucket','ap-southeast-1',{},
    'https://oss.console.alibabacloud.com/bucket/oss-ap-southeast-1/example-bucket/overview'],
  ['ALIYUN::SLB::LoadBalancer','aliyun','lb-test123456','cn-hangzhou',{},
    'https://slb.console.aliyun.com/slb/cn-hangzhou/slbs/lb-test123456'],
  ['ALIYUN::ALB::LoadBalancer','alibabacloud','alb-test123456','ap-southeast-1',{},
    'https://slb.console.alibabacloud.com/alb/ap-southeast-1/albs/alb-test123456'],
  ['ALIYUN::NLB::LoadBalancer','aliyun','nlb-test123456','cn-hangzhou',{},
    'https://slb.console.aliyun.com/nlb/cn-hangzhou/nlbs/nlb-test123456'],
  ['ALIYUN::ALB::HealthCheckTemplate','aliyun','hct-test123456','cn-hangzhou',{},
    'https://slb.console.aliyun.com/alb/cn-hangzhou/health-check-templates/hct-test123456'],
  ['ALIYUN::REDIS::Instance','alibabacloud','r-test123456','ap-southeast-1',{},
    'https://kvstore.console.alibabacloud.com/#/detail/r-test123456/Normal/CLASSIC/info?regionId=ap-southeast-1'],
  ['ALIYUN::MEMCACHE::Instance','aliyun','r-test123456','cn-hangzhou',{},
    'https://kvstore.console.aliyun.com/Memcache/instance/cn-hangzhou/r-test123456/Memcache/CLASSIC/default/info'],
  ['ALIYUN::ECI::ContainerGroup','aliyun','eci-test123456','cn-hangzhou',{},
    'https://eci.console.aliyun.com/?regionId=cn-hangzhou#/eci/cn-hangzhou/detail/eci-test123456/containers'],
  ['ALIYUN::ECI::ImageCache','aliyun','imc-test123456','cn-hangzhou',
    {ContainerGroupId:'eci-test123456'},
    'https://eci.console.aliyun.com/?#/eci/cn-hangzhou/image/eci-test123456/'+
      'imc-test123456/productevents'],
  ['ALIYUN::MNS::Queue','aliyun','queue-name','cn-hangzhou',{},
    'https://mns.console.aliyun.com/region/cn-hangzhou/queue/queue-name/detail'],
  ['ALIYUN::MNS::Topic','aliyun','topic-name','cn-hangzhou',{},
    'https://mns.console.aliyun.com/region/cn-hangzhou/topic/topic-name/detail'],
  ['ALIYUN::OOS::Parameter','aliyun','p-test123456','cn-hangzhou',
    {Name:'folder/normal-parameter'},
    'https://oos.console.aliyun.com/cn-hangzhou/parameter/detail/'+
      'folder%2Fnormal-parameter?routerType=detail'],
  ['ALIYUN::OOS::SecretParameter','aliyun','p-secret123456','cn-hangzhou',
    {Name:'folder/secret-parameter'},
    'https://oos.console.aliyun.com/cn-hangzhou/parameter/detailSecret/'+
      'folder%2Fsecret-parameter?routerType=detail'],
  ['ALIYUN::OOS::PatchBaseline','aliyun','pb-test123456','cn-hangzhou',
    {Name:'ACS-Ubuntu-DefaultPatchBaseline'},
    'https://oos.console.aliyun.com/cn-hangzhou/patch/baseline/detail/'+
      'ACS-Ubuntu-DefaultPatchBaseline'],
  ['ALIYUN::OTS::Instance','aliyun','instance-name','cn-hangzhou',{},
    'https://ots.console.aliyun.com/cn-hangzhou/instance-name/list'],
  ['ALIYUN::OTS::VpcBinder','aliyun','vpc-binding','cn-hangzhou',{InstanceName:'instance-name'},
    'https://ots.console.aliyun.com/cn-hangzhou/instance-name/networkManagement'],
  ['ALIYUN::OTS::Table','aliyun','table-name','cn-hangzhou',{InstanceName:'instance-name'},
    'https://ots.console.aliyun.com/cn-hangzhou/instance-name/table-name/dataManage'],
  ['ALIYUN::MONGODB::Instance','aliyun','dds-test123456','cn-hangzhou',{},
    'https://mongodb.console.aliyun.com/replicate/cn-hangzhou/instances/dds-test123456/basicInfo'],
  ['ALIYUN::MSE::Cluster','aliyun','mse-cn-test123456','cn-hangzhou',{},
    'https://mse.console.aliyun.com/#/Instance/BasicInfo?&InstanceId=mse-cn-test123456&region=cn-hangzhou'],
  ['alicloud_mse_cluster','aliyun','mse-cn-test123456','cn-hangzhou',{},
    'https://mse.console.aliyun.com/#/Instance/BasicInfo?&InstanceId=mse-cn-test123456&region=cn-hangzhou'],
  ['ALIYUN::VPC::NatGateway','aliyun','ngw-test123456','cn-hangzhou',{},
    'https://vpc.console.aliyun.com/nat/cn-hangzhou/nats/ngw-test123456'],
  ['alicloud_nat_gateway','aliyun','ngw-test123456','cn-shenzhen',{},
    'https://vpc.console.aliyun.com/nat/cn-shenzhen/nats/ngw-test123456'],
  ['ALIYUN::NLB::ServerGroup','aliyun','sgp-test123456','cn-hangzhou',{},
    'https://slb.console.aliyun.com/nlb/cn-hangzhou/server-groups/sgp-test123456'],
  ['alicloud_nlb_server_group','aliyun','sgp-test123456','cn-shenzhen',{},
    'https://slb.console.aliyun.com/nlb/cn-shenzhen/server-groups/sgp-test123456'],
  ['ALIYUN::ClickHouse::DBCluster','aliyun','cc-community123456','cn-hangzhou',{},
    'https://clickhouse.console.aliyun.com/clickhouse/cn-hangzhou/list/nav/cc-community123456/basic'],
  ['ALIYUN::ClickHouse::DBCluster','aliyun','cc-enterprise123456','cn-hangzhou',{},
    'https://clickhouse.console.aliyun.com/clickhouse/cn-hangzhou/list/enterprise/cc-enterprise123456/basic',
    [{product:'clickhouse',action:'DescribeDBInstances',evidence:'api_response'}]],
  ['ALIYUN::TSDB::HiTSDBInstance','aliyun','ld-test123456','cn-hangzhou',{},
    'https://lindorm.console.aliyun.com/cn-hangzhou/cluster/lindorm/ld-test123456/info'],
  ['ALIYUN::Lindorm::Instance','aliyun','ld-test123456','cn-hangzhou',{},
    'https://lindorm.console.aliyun.com/cn-hangzhou/cluster/lindorm/ld-test123456/info'],
  ['ALIYUN::ROCKETMQ::Instance','aliyun','MQ_INST_test123456','cn-hangzhou',{},
    'https://ons.console.aliyun.com/region/cn-hangzhou/instance/MQ_INST_test123456/detail'],
  ['alicloud_ons_instance','aliyun','MQ_INST_test123456','cn-shenzhen',{},
    'https://ons.console.aliyun.com/region/cn-shenzhen/instance/MQ_INST_test123456/detail'],
  ['ALIYUN::FC::Function','aliyun','Handler','cn-hangzhou',{ServiceName:'ServiceA'},
    'https://fcnext.console.aliyun.com/cn-hangzhou/services/ServiceA/function-detail/Handler/LATEST?tab=code'],
  ['ALIYUN::FNF::Flow','alibabacloud','flow-physical','ap-southeast-1',{FlowName:'OrderFlow'},
    'https://fnf.console.alibabacloud.com/fnf/ap-southeast-1/flows/item/OrderFlow'],
  ['ALIYUN::CEN::TransitRouterVpcAttachment','aliyun','tr-attach-1','cn-hangzhou',
    {CenId:'cen-1',TransitRouterId:'tr-1'},
    'https://cen.console.aliyun.com/cen/attachment/cn-hangzhou/cen-1/tr-1/attachInstance'],
  ['ALIYUN::CEN::RouteEntry','aliyun','cen-1/vpc-1/vtb-1','cn-hangzhou',{},
    'https://vpc.console.aliyun.com/vpc/cn-hangzhou/route-tables/vtb-1'],
  ['ALIYUN::CEN::RouteEntry','aliyun','cen-1/vpc-1/vtb-1',null,{},
    'https://vpc.console.aliyun.com/vpc/cn-hangzhou/route-tables/vtb-1',
    [{product:'ros',action:'ListStackResources',requestRegionId:'cn-hangzhou',evidence:'api_response'}]],
  ['ALIYUN::CEN::CenBandwidthPackage','aliyun','cenbwp-1','cn-hangzhou',{},
    'https://cen.console.aliyun.com/cen/bandwidth'],
  ['ALIYUN::ALB::BackendServerAttachment','aliyun','sgp-1','cn-hangzhou',
    {ServerGroupId:'sgp-1'},'https://slb.console.aliyun.com/alb/cn-hangzhou/server-groups/sgp-1'],
  ['alicloud_slb_listener','alibabacloud','lb-1:http:80','ap-southeast-1',
    {load_balancer_id:'lb-1',LoadBalancerId:'lb-1',ListenerPort:'80',ListenerProtocol:'http'},
    'https://slb.console.alibabacloud.com/slb/ap-southeast-1/slbs/lb-1/listeners'],
  ['ALIYUN::NAS::MountTarget','aliyun','12345678-cn-hangzhou.nas.aliyuncs.com','cn-hangzhou',
    {FileSystemId:'fs-explicit-parent'},
    'https://nasnext.console.aliyun.com/cn-hangzhou/filesystem/fs-explicit-parent/mount'],
  ['alicloud_nas_mount_target','aliyun','12345678-cn-hangzhou.nas.aliyuncs.com','cn-hangzhou',
    {FileSystemId:'fs-explicit-parent'},
    'https://nasnext.console.aliyun.com/cn-hangzhou/filesystem/fs-explicit-parent/mount'],
  ['ALIYUN::ECS::ImageDetail','aliyun','m-image-1','cn-hangzhou',{DestinationRegionId:'cn-shanghai'},
    'https://ecs.console.aliyun.com/#/imageDetail/region/cn-shanghai/imageId/m-image-1'],
  ['alicloud_elasticsearch_instance','alibabacloud','es-1','ap-southeast-1',{},
    'https://elasticsearch-ap-southeast-1.console.aliyun.com/#/instances/es-1/base'],
  ['ALIYUN::VPC::VpcPeerConnection','aliyun','pcc-1','cn-hangzhou',{},
    'https://vpc.console.aliyun.com/vpcpeer/cn-hangzhou/vpcpeers/pcc-1'],
  ['ALIYUN::VPC::RouteTable','aliyun','vtb-test123456','cn-hangzhou',{},
    'https://vpc.console.aliyun.com/vpc/cn-hangzhou/route-tables/vtb-test123456'],
  ['ALIYUN::VPC::VpnConnection','aliyun','vco-test123456','cn-huhehaote',{},
    'https://vpc.console.aliyun.com/vpn/cn-huhehaote/vpn-connections/vco-test123456'],
  ['ALIYUN::VPC::RouterInterface','aliyun','ri-test123456','cn-wulanchabu',{RouterType:'VBR'},
    'https://expressconnect.console.aliyun.com/peerconnection/cn-wulanchabu/vbr2vpc?'+
      'ConnectionType=vbr2vpc&ProductForm=expressconnect&TabKey=vbr2vpc'],
  ['ALIYUN::VPC::RouterInterface','aliyun','ri-test123456','cn-wulanchabu',
    {RouterType:'VBR',RouterId:'vbr-test123456'},
    'https://expressconnect.console.aliyun.com/vbr/cn-wulanchabu/detail/vbr-test123456'],
  ['ALIYUN::VPC::SnatEntry','aliyun','snat-test123456','cn-hongkong',{NatGatewayId:'ngw-test123456'},
    'https://vpc.console.aliyun.com/nat/cn-hongkong/nats/ngw-test123456/snats'],
  ['ALIYUN::ESS::ScalingGroupEnable','aliyun','asg-test123456','cn-hangzhou',{},
    'https://essnew.console.aliyun.com/#/v3/group/detail/cn-hangzhou/asg-test123456'],
  ['ALIYUN::ESS::AlarmTask','aliyun','alarm-test123456','cn-hangzhou',{},
    'https://essnew.console.aliyun.com/#/task/v3/alarm/cn-hangzhou/system'],
  ['ALIYUN::ESS::ScheduledTask','aliyun','scheduled-test123456','cn-hangzhou',{},
    'https://essnew.console.aliyun.com/#/task/v3/timer/cn-hangzhou'],
  ['ALIYUN::ESS::LifecycleHook','aliyun','ash-test123456','cn-hangzhou',{ScalingGroupId:'asg-test123456'},
    'https://essnew.console.aliyun.com/#/v3/group/detail/cn-hangzhou/asg-test123456/lifecycleHook'],
  ['ALIYUN::ESS::ScalingConfiguration','aliyun','asc-test123456','cn-hangzhou',
    {ScalingGroupId:'asg-test123456'},
    'https://essnew.console.aliyun.com/#/v3/group/detail/cn-hangzhou/asg-test123456/scalingConfiguration'],
  ['ALIYUN::CMS::Contact','aliyun','contact-name',null,{},
    'https://cloudmonitor.console.aliyun.com/alert/contact'],
  ['ALIYUN::CMS::ContactGroup','aliyun','contact-group-name',null,{},
    'https://cloudmonitor.console.aliyun.com/alert/contact'],
  ['ALIYUN::CMS::MetricRuleTemplate','aliyun','528143',null,{},
    'https://cloudmonitor.console.aliyun.com/alert-template'],
  ['ALIYUN::CMS::MonitorGroup','aliyun','123456',null,{},
    'https://cloudmonitor.console.aliyun.com/app-group/123456/overview'],
  ['ALIYUN::SAG::SmartAccessGateway','aliyun','sag-test123456','cn-shanghai',{},
    'https://smartag.console.aliyun.com/hardware/cn-shanghai/hardwares/sag-test123456'],
  ['ALIYUN::SAG::CloudConnectNetwork','aliyun','ccn-test123456','cn-shanghai',{},
    'https://smartag.console.aliyun.com/ccn/cn-shanghai/ccns/ccn-test123456'],
  ['ALIYUN::SAG::ACL','aliyun','acl-test123456','cn-shanghai',{},
    'https://smartag.console.aliyun.com/acl/cn-shanghai/acls/acl-test123456'],
  ['ALIYUN::SAG::ACLRule','aliyun','acr-test123456','cn-shanghai',{AclId:'acl-test123456'},
    'https://smartag.console.aliyun.com/acl/cn-shanghai/acls/acl-test123456'],
  ['ALIYUN::CDN::Domain','aliyun','www.example.com',null,{},
    'https://cdn.console.aliyun.com/domain/detail/www.example.com/basic'],
  ['ALIYUN::CAS::Certificate','aliyun','cas-ivauto-test123456','cn-hangzhou',
    {CertificateId:'17203841',OrderId:'13268485'},
    'https://yundun.console.aliyun.com/?p=cas#/certExtendDetail/17203841/13268485/cn-hangzhou'],
  ['ALIYUN::CAS::Certificate','aliyun','cas-upload-test123456','cn-hangzhou',
    {CertificateId:'26927444',Upload:'true'},
    'https://yundun.console.aliyun.com/?p=cas#/uploadExtendDetail/26927444/0/cn-hangzhou'],
  ['ALIYUN::CAS::Certificate','aliyun','cas-ivauto-test123456','cn-hangzhou',{},
    'https://yundun.console.aliyun.com/?p=cas#/certExtend/buy/cn-hangzhou?adSearchParams='+
      '%7B%22BrandId%22%3A%22%22%2C%22Keyword%22%3A%22cas-ivauto-test123456%22%2C%22ShowSize%22%3A10%2C'+
      '%22CurrentPage%22%3A1%2C%22StatusCode%22%3A%22%22%7D'],
  ['ALIYUN::NAS::AccessGroup','aliyun','DEFAULT_CLASSIC_GROUP_NAME','cn-hangzhou',
    {AccessGroupType:'Classic',FileSystemType:'standard'},
    'https://nasnext.console.aliyun.com/cn-hangzhou/access-group/standard/detail/standard/'+
      'DEFAULT_CLASSIC_GROUP_NAME/Classic/rule'],
  ['ALIYUN::NAS::AccessRule','aliyun','0','cn-hangzhou',
    {AccessGroupName:'DEFAULT_VPC_GROUP_NAME',AccessGroupType:'Vpc',FileSystemType:'standard'},
    'https://nasnext.console.aliyun.com/cn-hangzhou/access-group/standard/detail/standard/'+
      'DEFAULT_VPC_GROUP_NAME/Vpc/rule'],
  ['ALIYUN::ENS::Instance','aliyun','i-test123456','cn-hangzhou',{},
    'https://ens.console.aliyun.com/#/instance/detail/i-test123456'],
  ['ALIYUN::EHPC::Cluster','aliyun','ehpc-test123456','cn-hangzhou',{},
    'https://ehpc.console.aliyun.com/#/clusterdetail/info/ehpc-test123456'+
      '?referrer=%2Fcluster&regionId=cn-hangzhou'],
  ['ALIYUN::EMR::Cluster','aliyun','C-TEST123456','cn-hangzhou',{},
    'https://emr-next.console.aliyun.com/#/region/cn-hangzhou/resource/all/ecs/detail/'+
      'C-TEST123456/overview'],
  ['ALIYUN::MSE::Cluster','alibabacloud','mse-1','ap-southeast-1',{},
    'https://mse.console.alibabacloud.com/#/Home'],
  ['alicloud_ram_user','aliyun','DeployUser',null,{},
    'https://ram.console.aliyun.com/users/detail?userId=DeployUser'],
  ['ALIYUN::RAM::ManagedPolicy','aliyun','AdministratorAccess',null,{},
    'https://ram.console.aliyun.com/policies/detail?policyType=System&policyName=AdministratorAccess'],
  ['ALIYUN::RAM::Group','aliyun','g-test123456',null,{GroupName:'ops-team'},
    'https://ram.console.aliyun.com/groups/ops-team'],
  ['ALIYUN::PVTZ::Zone','aliyun','zone-test123456',null,{},
    'https://dnsnext.console.aliyun.com/privateDNS/zones/zone-test123456/records'],
  ['ALIYUN::PVTZ::ZoneRecord','aliyun','record-test123456',null,{ZoneId:'zone-test123456'},
    'https://dnsnext.console.aliyun.com/privateDNS/zones/zone-test123456/records'],
  ['alicloud_ram_user','alibabacloud','DeployUser',null,{},
    'https://ram.console.alibabacloud.com/#/user/detail/DeployUser/info'],
];
for (const [sourceType,partition,identifier,regionId,attributes,expected,sources] of fixtures) {
  assert.equal(build(sourceType,partition,identifier,regionId,attributes,sources),expected,`${partition}:${sourceType}`);
}
assert.equal(build('ALIYUN::VPC::RouterInterface','aliyun','ri-test123456','cn-wulanchabu',{}),null,
  'router interfaces must not fall back to the unrelated route-table URL without RouterType');
assert.equal(build('ALIYUN::VPC::SnatEntry','aliyun','snat-test123456','cn-hongkong',{}),null,
  'SNAT entries must not substitute their own ID where the console requires NatGatewayId');
assert.equal(build('ALIYUN::ECI::ImageCache','aliyun','imc-test123456','cn-hangzhou',{}),null,
  'an image cache must not fall back to the list when its backing container group is unknown');
assert.equal(build('ALIYUN::OTS::VpcBinder','aliyun','vpc-binding','cn-hangzhou',{}),null,
  'a VPC binding must not fall back to the regional instance list when InstanceName is unknown');
assert.equal(build('ALIYUN::CEN::RouteEntry','aliyun','cen-1/vpc-1','cn-hangzhou',{}),null,
  'a CEN RouteEntry without the child route-table segment must fail closed');
assert.equal(build('ALIYUN::CEN::RouteEntry','aliyun','cen-1/vpc-1/tr-1','cn-hangzhou',{}),null,
  'a CEN RouteEntry must not treat a Transit Router identifier as the child VPC route table');
assert.equal(build('ALIYUN::CEN::RouteEntry','aliyun','cen-1/vpc-1/vtb-1',null,{},[
  {product:'ros',action:'ListStackResources',requestRegionId:'cn-hangzhou',evidence:'api_response'},
  {product:'ros',action:'ListStackResources',requestRegionId:'cn-beijing',evidence:'api_response'},
]),null,'a CEN RouteEntry with ambiguous source regions must fail closed');
const otsTfRule=rules.RESOURCE_LINK_RULES.find(item=>item.ruleId==='ots-table-tf-cn');
const otsTf={resourceType:'alicloud_ots_table',identifier:'offline-parent:offline_table',
  identifierKind:'id',identifierField:'PhysicalResourceId',accountScope:'fake-account',
  consolePartition:'aliyun',regionId:'cn-hangzhou',
  attributes:{InstanceName:'offline-parent',TableName:'offline_table'}};
assert.equal(rules.buildResourceConsoleUrl(otsTfRule,otsTf),
  'https://ots.console.aliyun.com/cn-hangzhou/offline-parent/offline_table/dataManage');
for (const bad of [
  {...otsTf,identifier:'offline-parent:offline_table:extra'},
  {...otsTf,identifier:'offline_table'},
  {...otsTf,attributes:{...otsTf.attributes,InstanceName:'other-parent'}},
  {...otsTf,attributes:{...otsTf.attributes,TableName:'other_table'}},
]) assert.equal(rules.buildResourceConsoleUrl(otsTfRule,bad),null,
  'managed OTS composite locator must exactly agree with both source attributes');
const nasAccessRule=rules.RESOURCE_LINK_RULES.find(item=>item.ruleId==='nas-access-rule-parent-cn');
assert.deepEqual(nasAccessRule.requiredFields,['AccessGroupName','AccessGroupType','FileSystemType']);
const noParentRule=rules.RESOURCE_LINK_RULES.find(item=>
  item.sourceType==='ALIYUN::FC::Function'&&item.partition==='aliyun');
assert.equal(build('ALIYUN::FC::Function','aliyun','Handler','cn-hangzhou',{}),null,
  'required resource attributes fail closed');
assert.ok(noParentRule.requiredFields.includes('ServiceName'));
assert.equal(build('ALIYUN::ROS::StackGroup','aliyun','29dd2de2-4872-429a-bb82-95930182fa94',
  'cn-hangzhou',{}),null,'a StackGroupId must not be substituted where the console requires StackGroupName');
const fallbackIndex={
  candidates:(occurrence)=>[{...occurrence,referenceId:'ref-1',accountScope:'fake-account',
    consolePartition:'aliyun',attributes:{}}],
  bindingFor:()=>null,contextFor:()=>({}),
};
for (const [resourceType,identifier] of [
  ['ALIYUN::ESS::LifecycleHook','ash-test123456'],
  ['ALIYUN::ESS::ScalingConfiguration','asc-test123456'],
]) {
  const resolved=rules.resolveResourceOccurrence({resourceType,identifier,identifierKind:'id',
    identifierField:'PhysicalResourceId',regionId:'cn-hangzhou',attributes:{}},fallbackIndex);
  assert.equal(resolved.status,'resolved',resourceType);
  assert.equal(resolved.url,'https://essnew.console.aliyun.com/#/v3/group/list/cn-hangzhou',resourceType);
}
for (const [resourceType,identifier,expected] of [
  ['ALIYUN::OTS::Table','table-name','https://ots.console.aliyun.com/cn-hangzhou/list?productName=otsnext'],
  ['ALIYUN::PVTZ::ZoneRecord','record-id','https://dnsnext.console.aliyun.com/privateDNS'],
  ['ALIYUN::RAM::Group','g-test123456','https://ram.console.aliyun.com/groups'],
  ['ALIYUN::RAM::ManagedPolicy','custom-policy-without-type',
    'https://ram.console.aliyun.com/policies/detail?policyType=Custom&policyName=custom-policy-without-type'],
  ['ALIYUN::NAS::AccessGroup','DEFAULT_CLASSIC_GROUP_NAME',
    'https://nasnext.console.aliyun.com/cn-hangzhou/access-group'],
]) {
  const resolved=rules.resolveResourceOccurrence({resourceType,identifier,identifierKind:'id',
    identifierField:'PhysicalResourceId',regionId:'cn-hangzhou',attributes:{}},fallbackIndex);
  assert.equal(resolved.status,'resolved',resourceType);
  assert.equal(resolved.url,expected,resourceType);
}
const stackRule=rules.RESOURCE_LINK_RULES.find(item=>item.ruleId==='ros-stack-intl');
assert.equal(rules.buildResourceConsoleUrl(stackRule,{
  resourceType:'ALIYUN::ROS::Stack',identifierField:'StackId',identifierKind:'id',
  identifier:'stack-1',regionId:'ap-southeast-1',accountScope:'fake-account',
  consolePartition:'alibabacloud',attributes:{},
}),'https://ros-intl.console.alibabacloud.com/ap-southeast-1/stacks/stack-1');
console.log(JSON.stringify({passed:true,count:fixtures.length,rules:rules.RESOURCE_LINK_RULES.length}));
""",
        encoding="utf-8",
    )
    result = subprocess.run([node, str(script)], capture_output=True, text=True, encoding="utf-8", timeout=20)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"passed": True, "count": 104, "rules": 883}


def test_resource_dom_reuse_requires_the_actual_live_text_range(tmp_path):
    """The geometry predicate is pure; real DOM mutation/focus tests run in E2E."""
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is unavailable")
    source = (STATIC / "resource_linkify.js").read_text(encoding="utf-8")
    start = source.index("function reusableMatch(")
    end = source.index("\n/** Synchronous generator", start)
    script = tmp_path / "reuse-range.mjs"
    script.write_text(
        "import assert from 'node:assert/strict';\n"
        + source[start:end]
        + r"""
const a={}, b={}, moved={};
const state={nodes:[a,b]};
const plan={segmentNumber:2,occurrence:{start:10,end:20}};
const positions=new Map([[a,{segment:2,start:10,end:14}],[b,{segment:2,start:14,end:20}]]);
assert.equal(reusableMatch(state,plan,positions),true);
assert.equal(reusableMatch(state,null,positions),false,'withdrawn occurrences must not retain wrappers');
assert.equal(reusableMatch({nodes:[]},plan,positions),false);
assert.equal(reusableMatch({nodes:[a,moved]},plan,positions),false,'disconnected/moved DOM is not reusable');
assert.equal(reusableMatch(state,{...plan,segmentNumber:3},positions),false,'equal offsets in another block differ');
for(const occurrence of [{start:9,end:20},{start:10,end:21},{start:11,end:19}])
  assert.equal(reusableMatch(state,{...plan,occurrence},positions),false,'expanded/shortened text needs new wrappers');
assert.equal(reusableMatch(state,plan,new Map([[a,positions.get(a)],[b,{segment:2,start:15,end:20}]])),false,
  'a gap between highlight fragments cannot be silently included');
assert.equal(reusableMatch(state,plan,new Map([[a,positions.get(a)],[b,{segment:3,start:14,end:20}]])),false,
  'fragments from separate logical blocks cannot be combined');
console.log('passed');
""",
        encoding="utf-8",
    )
    result = subprocess.run([node, str(script)], capture_output=True, text=True, encoding="utf-8", timeout=20)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "passed"


def test_shift_activation_opens_an_isolated_new_tab(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is unavailable")
    script = tmp_path / "shift-resource-link-test.mjs"
    script.write_text(
        CATALOG_BOOTSTRAP
        + "import assert from 'node:assert/strict';\n"
        + "const {isShiftNewTabGesture,openResourceTargetInNewTab,recoverResourceLinkActivation} = await import("
        + json.dumps((STATIC / "resource_linkify.js").as_uri())
        + ");\n"
        + r"""
const calls=[];
const target={
  opener:{unsafe:true},
  location:{replace(url){calls.push(['replace',url]);}},
  close(){calls.push(['close']);},
};
const view={open(url,name){calls.push(['open',url,name]);return target;}};
const element={ownerDocument:{defaultView:view}};
element.getAttribute=(name)=>name==='href'?'https://ecs.console.aliyun.com/#/server/i-test/detail':null;
const occurrence={identifier:'i-test'};
const previous={status:'resolved',url:'https://ecs.console.aliyun.com/#/server/i-test/detail'};
const activate=()=>{};
const recovered={occurrence,resolution:{...previous},activate};
assert.equal(isShiftNewTabGesture({button:0,shiftKey:true,metaKey:false,ctrlKey:false}),true);
assert.equal(isShiftNewTabGesture({button:0,shiftKey:true,metaKey:true,ctrlKey:false}),false);
assert.equal(isShiftNewTabGesture({button:1,shiftKey:true,metaKey:false,ctrlKey:false}),false);
assert.equal(recoverResourceLinkActivation(element,occurrence,previous,()=>recovered),recovered,
  'an index refresh may recover the exact same reviewed URL in the original click');
assert.equal(recoverResourceLinkActivation(element,occurrence,previous,()=>({
  ...recovered,resolution:{...previous,url:'https://ecs.console.aliyun.com/#/server/i-other/detail'},
})),null,'a changed URL must fail closed until the visible link is re-decorated');
assert.equal(recoverResourceLinkActivation(element,occurrence,previous,()=>{throw Error('stale source');}),null);
assert.equal(recoverResourceLinkActivation(element,occurrence,previous,null),null);
assert.equal(openResourceTargetInNewTab(element,'https://ecs.console.aliyun.com/#/server/i-test/detail'),true);
assert.equal(target.opener,null);
assert.deepEqual(calls,[['open','about:blank','_blank'],
  ['replace','https://ecs.console.aliyun.com/#/server/i-test/detail']]);
assert.equal(openResourceTargetInNewTab({ownerDocument:{}},'https://example.com'),false);
assert.equal(openResourceTargetInNewTab(element,''),false);
view.open=()=>null;
assert.equal(openResourceTargetInNewTab(element,'https://example.com'),false,'a blocked popup keeps native fallback');
view.open=()=>({opener:{},location:{replace(){throw Error('navigation failed');}},close(){calls.push(['close']);}});
assert.equal(openResourceTargetInNewTab(element,'https://example.com'),false);
assert.deepEqual(calls.at(-1),['close']);
console.log('passed');
""",
        encoding="utf-8",
    )
    result = subprocess.run([node, str(script)], capture_output=True, text=True, encoding="utf-8", timeout=20)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "passed"


def test_nas_access_group_alias_composite_source_and_exact_detail_route(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is unavailable")
    script = tmp_path / "nas-access-group.mjs"
    script.write_text(
        CATALOG_BOOTSTRAP
        + "import assert from 'node:assert/strict';\n"
        + "const rules = await import("
        + json.dumps((STATIC / "resource_link_rules.js").as_uri())
        + ");\n"
        + "const {ResourceIndex} = await import("
        + json.dumps((STATIC / "resource_index.js").as_uri())
        + ");\n"
        + "const {scanResourceOccurrences:scan} = await import("
        + json.dumps((STATIC / "resource_occurrences.js").as_uri())
        + ");\n"
        + r"""
const base = {provider:'aliyun', referenceId:'ref-group', resourceType:'alicloud_nas_access_group',
  identifier:'group-fixture:standard', identifierField:'PhysicalResourceId', identifierKind:'id',
  scopeKind:'regional', regionId:'cn-hangzhou', accountScope:'fake-account', consolePartition:'aliyun',
  evidence:'api_response',
  attributes:{AccessGroupType:'Vpc'}, aliases:[],
  sources:[{product:'ros', action:'ListStackResources', evidence:'api_response', toolUseId:'stack-tool'}]};
const resolve = ref => rules.resolveResourceOccurrence(ref,{
  candidates:()=>[ref], bindingFor:()=>null, contextFor:()=>({})});
assert.equal(rules.canonicalResourceType(base.resourceType),'ALIYUN::NAS::AccessGroup');
const expected='https://nasnext.console.aliyun.com/cn-hangzhou/access-group/standard/detail/'+
  'standard/group-fixture/Vpc/rule';
const resolved=resolve(base);
assert.equal(resolved.status,'resolved');
assert.equal(resolved.url,expected);
assert.equal(resolved.reference.identifier,'group-fixture');
assert.equal(resolved.reference.attributes.FileSystemType,'standard');
assert.equal(base.identifier,'group-fixture:standard');
assert.equal(base.attributes.FileSystemType,undefined);
const missing=resolve({...base,attributes:{}});
assert.equal(missing.status,'needs_context');
assert.ok(missing.missing.includes('AccessGroupType'));
for (const patch of [
  {identifier:'group-fixture'}, {identifier:'group-fixture:standard:extra'},
  {identifier:'group-fixture:unknown'}, {identifier:'group-fixture:https://evil.invalid'},
  {attributes:{AccessGroupType:'Vpc',AccessGroupName:'other-group'}},
  {attributes:{AccessGroupType:'Vpc',FileSystemType:'extreme'}},
  {attributes:{AccessGroupType:'invented'}}, {regionId:''}, {accountScope:''},
  {evidence:'tool_input'}, {sources:[]},
  {sources:[{product:'nas',action:'ListStackResources',evidence:'api_response'}]},
  {sources:[{product:'ros',action:'CreateStack',evidence:'api_response'}]},
  {aliases:[{identifier:'wrong-name',identifierField:'AccessGroupName',identifierKind:'name'}]}
]) assert.notEqual(resolve({...base,...patch}).status,'resolved',JSON.stringify(patch));
const api={...base,referenceId:'ref-api',resourceType:'ALIYUN::NAS::AccessGroup',identifier:'group-fixture',
  attributes:{AccessGroupType:'Vpc',FileSystemType:'standard',AccessGroupName:'group-fixture'},
  sources:[{product:'nas',action:'DescribeAccessGroups',evidence:'api_response',toolUseId:'detail-tool'}]};
assert.equal(resolve(api).url,expected);
const index=new ResourceIndex({canonicalType:rules.canonicalResourceType});
assert.equal(await index.loadSnapshot({schemaVersion:1,revision:1,items:[base],bindings:[]}),true);
const occurrences=scan('{"ResourceType":"alicloud_nas_access_group","PhysicalResourceId":"group-fixture:standard"}',
  {index,toolUseId:'stack-tool',resourceType:base.resourceType});
const occurrence=occurrences.find(item=>item.identifier===base.identifier);
assert.ok(occurrence,'actual composite physical ID remains recognizable');
assert.equal(rules.resolveResourceOccurrence(occurrence,index).url,expected);
const ambiguous={...base,referenceId:'ref-other',regionId:'cn-shanghai',sources:[]};
assert.equal(rules.resolveResourceOccurrence({...occurrence,toolUseId:''},{
  candidates:()=>[base,ambiguous],bindingFor:()=>null,contextFor:()=>({})}).status,'ambiguous');
const detailRule=rules.RESOURCE_LINK_RULES.find(item=>item.ruleId==='nas-access-group-tf-cn');
assert.equal(rules.buildResourceConsoleUrl(detailRule,base),null,'never encode raw composite as a group name');
assert.equal(rules.buildResourceConsoleUrl(detailRule,resolved.reference),expected);
""",
        encoding="utf-8",
    )
    subprocess.run([node, str(script)], check=True, capture_output=True, text=True, encoding="utf-8")


def test_nas_creation_summary_annotation_keeps_response_bound_locator(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is unavailable")
    script = tmp_path / "nas-summary-annotation.mjs"
    script.write_text(
        CATALOG_BOOTSTRAP
        + "import assert from 'node:assert/strict';\n"
        + "const rules = await import("
        + json.dumps((STATIC / "resource_link_rules.js").as_uri())
        + ");\n"
        + "const {ResourceIndex} = await import("
        + json.dumps((STATIC / "resource_index.js").as_uri())
        + ");\n"
        + "const {scanResourceOccurrences:scan} = await import("
        + json.dumps((STATIC / "resource_occurrences.js").as_uri())
        + ");\n"
        + "const links = await import("
        + json.dumps((STATIC / "resource_linkify.js").as_uri())
        + ");\n"
        + r"""
const group='group-fixture';
const source={turnId:'summary-turn',toolUseId:'stack-tool',product:'ros',
  action:'ListStackResources',evidence:'api_response'};
const reference={provider:'aliyun',referenceId:'ref-summary',resourceType:'alicloud_nas_access_group',
  identifier:group+':standard',identifierField:'PhysicalResourceId',identifierKind:'id',
  scopeKind:'regional',regionId:'cn-hangzhou',accountScope:'fake-account',consolePartition:'aliyun',
  evidence:'api_response',attributes:{AccessGroupName:group,FileSystemType:'standard',AccessGroupType:'Vpc'},
  aliases:[{identifier:group,identifierKind:'name',identifierField:'AccessGroupName'}],
  sources:[source,{...source,toolUseId:'detail-tool',product:'nas',action:'DescribeAccessGroups'}]};
const api={...reference,referenceId:'ref-summary-api',resourceType:'ALIYUN::NAS::AccessGroup',
  identifier:group,sources:[reference.sources[1]]};
const index=new ResourceIndex({canonicalType:rules.canonicalResourceType});
assert.equal(await index.loadSnapshot({schemaVersion:1,revision:1,items:[reference,api],bindings:[]}),true);
const expected='https://nasnext.console.aliyun.com/cn-hangzhou/access-group/standard/detail/standard/'+group+'/Vpc/rule';
for (const annotation of ['（真实同栈完整复合物理标识）',' (actual response identifier)']) {
  const text=reference.identifier+annotation;
  const object={ResourceType:reference.resourceType,PhysicalResourceId:text,Status:'CREATE_COMPLETE'};
  const context={...links.resourceTableContextFromObject(object),field:'PhysicalResourceId',
    turnId:'summary-turn',messageId:'summary-message',structuralScope:JSON.stringify(['table-record',object])};
  const occurrences=scan(text,{index,context,field:context.field});
  assert.equal(occurrences.length,1,JSON.stringify(occurrences));
  assert.equal(occurrences[0].identifier,reference.identifier,'annotation is not part of a physical locator');
  assert.equal(text.slice(occurrences[0].start,occurrences[0].end),reference.identifier);
  const resolved=links.resolveResourceOccurrenceAlternatives(occurrences[0],index);
  assert.equal(resolved.url,expected,JSON.stringify(resolved));
  assert.equal(resolved.reference.referenceId,reference.referenceId,'keep the TF creation source');
}
const text=group+'（原响应独立证明的名称）';
const [name]=scan(text,{index,context:{turnId:'summary-turn'},field:'AccessGroupName'});
assert.equal(name.identifier,group);
assert.equal(links.resolveResourceOccurrenceAlternatives(name,index).url,expected);
const conflicting={...reference,referenceId:'ref-conflict',regionId:'cn-shanghai'};
const conflictIndex=new ResourceIndex({canonicalType:rules.canonicalResourceType});
await conflictIndex.loadSnapshot({schemaVersion:1,revision:1,items:[reference,conflicting],bindings:[]});
const [conflict]=scan(reference.identifier+'（真实标识）',{
  index:conflictIndex,context:{turnId:'summary-turn',resourceType:reference.resourceType},field:'PhysicalResourceId'});
assert.equal(conflict.identifier,reference.identifier);
assert.equal(links.resolveResourceOccurrenceAlternatives(conflict,conflictIndex).status,'ambiguous');
const literal=group+'（literal-name）';
const literalRef={...api,referenceId:'ref-literal',identifier:literal,attributes:{},
  aliases:[{identifier:literal,identifierKind:'name',identifierField:'AccessGroupName'}]};
const literalIndex=new ResourceIndex({canonicalType:rules.canonicalResourceType});
await literalIndex.loadSnapshot({schemaVersion:1,revision:1,items:[reference,literalRef],bindings:[]});
const [whole]=scan(literal,{index:literalIndex,context:{turnId:'summary-turn'},field:'AccessGroupName'});
assert.equal(whole.identifier,literal,'an observed full literal must never be shortened');
for (const patch of [
  {evidence:'tool_input'}, {turnId:'different-turn'}, {accountScope:'other-account'},
]) {
  const weakIndex=new ResourceIndex({canonicalType:rules.canonicalResourceType});
  const weak={...reference,...(patch.evidence ? {evidence:patch.evidence} : {}),sources:reference.sources};
  await weakIndex.loadSnapshot({schemaVersion:1,revision:1,items:[weak],bindings:[]});
  const value=reference.identifier+'（注释不能提供身份）';
  const [unchanged]=scan(value,{index:weakIndex,
    context:{resourceType:reference.resourceType,turnId:'summary-turn',...patch},field:'PhysicalResourceId'});
  assert.equal(unchanged.identifier,value,'only matching current API evidence permits presentation trimming');
}
const quoted=scan(JSON.stringify({ResourceType:reference.resourceType,
  PhysicalResourceId:reference.identifier+'（literal-id）'}),{index,context:{turnId:'summary-turn'}});
assert.ok(quoted.every(item=>item.identifier!==reference.identifier),'quoted structured identities stay literal');
const opaque='unproved-group（literal-name）';
const [unproved]=scan(opaque,{index,context:{resourceType:reference.resourceType},field:'PhysicalResourceId'});
assert.equal(unproved.identifier,opaque,'annotations cannot invent a response-backed prefix');
""",
        encoding="utf-8",
    )
    result = subprocess.run([node, str(script)], capture_output=True, text=True, encoding="utf-8", timeout=20)
    assert result.returncode == 0, result.stderr


def test_slb_acl_injected_alias_and_root_contracts_resolve_only_exact_source(tmp_path):
    payload = resource_link_catalog_payload()
    canonical = "ALIYUN::SLB::AccessControl"
    identity = next(item for item in payload["identities"] if item["resourceType"] == canonical)
    assert identity["identifierField"] == "AclId"
    assert "alicloud_slb_acl" in identity["aliases"]
    for action in ("CreateAccessControlList", "DescribeAccessControlListAttribute"):
        assert {"product": "slb", "action": action, "resourceType": canonical} in payload["directApiContracts"]
    catalog = resource_link_catalog()
    for partition in ("aliyun", "alibabacloud"):
        assert catalog.templates[partition]["alicloud_slb_acl"] == catalog.templates[partition][canonical]
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is unavailable")
    script = tmp_path / "slb-acl-routes.mjs"
    script.write_text(
        CATALOG_BOOTSTRAP
        + "import assert from 'node:assert/strict';\n"
        + "const rules = await import("
        + json.dumps((STATIC / "resource_link_rules.js").as_uri())
        + ");\n"
        + "const {resourceToolContext} = await import("
        + json.dumps((STATIC / "resource_occurrences.js").as_uri())
        + ");\n"
        + r"""
const base = {provider:'aliyun',referenceId:'offline-acl',identifier:'acl-offline123',
  identifierField:'AclId',identifierKind:'id',regionId:'cn-hangzhou',accountScope:'fake-account',
  consolePartition:'aliyun',scopeKind:'regional',attributes:{AclName:'offline-acl'},aliases:[]};
const resolve = reference => rules.resolveResourceOccurrence(reference,{
  candidates:()=>[reference],bindingFor:()=>null,contextFor:()=>({})});
for (const resourceType of ['ALIYUN::SLB::AccessControl','alicloud_slb_acl']) {
  assert.equal(rules.canonicalResourceType(resourceType),'ALIYUN::SLB::AccessControl');
  assert.notEqual(resolve({...base,resourceType,identifierField:'PhysicalResourceId'}).status,'resolved');
  assert.equal(resolve({...base,resourceType}).url,
    'https://slb.console.aliyun.com/slb/cn-hangzhou/acls/acl-offline123');
  assert.equal(resolve({...base,resourceType,consolePartition:'alibabacloud'}).url,
    'https://slb.console.alibabacloud.com/slb/cn-hangzhou/acls/acl-offline123');
  for (const patch of [{identifier:''},{regionId:''},{accountScope:''}]) {
    assert.notEqual(resolve({...base,resourceType,...patch}).status,'resolved');
  }
}
for (const action of ['CreateAccessControlList','DescribeAccessControlListAttribute']) {
  const context = resourceToolContext({toolName:'aliyun_api',
    input:{product:'slb',action,params:{RegionId:'cn-hangzhou'}}});
  assert.equal(context.resourceType,'ALIYUN::SLB::AccessControl');
  const unrelated = resourceToolContext({toolName:'aliyun_api',input:{product:'vpc',action,params:{}}});
  assert.notEqual(unrelated.resourceType,'ALIYUN::SLB::AccessControl');
}
assert.notEqual(resourceToolContext({toolName:'aliyun_api',input:{product:'slb',action:'UnreviewedAccessControlList'}}).resourceType,
  'ALIYUN::SLB::AccessControl');
""",
        encoding="utf-8",
    )
    result = subprocess.run([node, str(script)], capture_output=True, text=True, encoding="utf-8", timeout=20)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("action", ["CreatePolicy", "GetPolicy"])
def test_original_custom_policy_root_reaches_exact_frontend_detail_route(tmp_path, action):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is unavailable")
    references = extract_api_resource_references(
        product="ram",
        action=action,
        params={"PolicyName": "offline-policy", "PolicyType": "Custom"},
        body={"Policy": {"PolicyName": "offline-policy", "PolicyType": "Custom"}},
        region_id="cn-hangzhou",
        tool_use_id="offline-policy-source",
        account_scope="fake-policy-account",
        console_partition="aliyun",
    )
    assert len(references) == 1
    script = tmp_path / "ram-policy-original-source.mjs"
    script.write_text(
        CATALOG_BOOTSTRAP
        + "import assert from 'node:assert/strict';\n"
        + "const rules = await import("
        + json.dumps((STATIC / "resource_link_rules.js").as_uri())
        + ");\n"
        + "const reference = "
        + json.dumps(resource_fact_to_web(references[0]))
        + ";\n"
        + r"""
for (const resourceType of ['ALIYUN::RAM::ManagedPolicy', 'alicloud_ram_policy']) {
  const current = {...reference, resourceType};
  const rule = rules.RESOURCE_LINK_RULES.find(item => item.ruleId === 'ram-managed-policy-cn');
  assert.ok(rule);
  assert.equal(rules.buildResourceConsoleUrl(rule, current),
    'https://ram.console.aliyun.com/policies/detail?policyType=Custom&policyName=offline-policy');
  assert.equal(current.sources[0].sourcePath, '/result/Policy/PolicyName');
  assert.equal(current.sources[0].toolUseId, 'offline-policy-source');
  assert.equal(current.accountScope, 'fake-policy-account');
  assert.ok(!Object.hasOwn(current, 'regionId'));
  assert.equal(rules.buildResourceConsoleUrl(rule, {...current, accountScope:''}), null);
}
""",
        encoding="utf-8",
    )
    result = subprocess.run([node, str(script)], capture_output=True, text=True, encoding="utf-8", timeout=20)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("resource_type", ["ALIYUN::ECS::SSHKeyPair", "alicloud_ecs_key_pair"])
def test_stack_key_pair_and_api_name_form_one_resolved_reference(tmp_path, resource_type):
    from iac_code.tools.cloud.resource_references import stack_resource_reference
    from iac_code.web.resource_references import ResourceReferenceStore

    node = shutil.which("node")
    if not node:
        pytest.skip("Node is unavailable")
    name = "offline-keypair"
    stack_fact = stack_resource_reference(
        {"ResourceType": resource_type, "PhysicalResourceId": name, "LogicalResourceId": "NotTheKeyName"},
        stack_id="offline-stack",
        region_id="cn-hangzhou",
        tool_use_id="offline-stack-tool",
        source_path="/Resources/0",
        account_scope="fake-account",
        console_partition="aliyun",
    )
    assert stack_fact["identifier"] == name
    assert stack_fact["identifier_kind"] == "name"
    assert stack_fact["identifier_field"] == "KeyPairName"
    assert stack_fact["stack_id"] == "offline-stack"
    assert stack_fact["logical_resource_id"] == "NotTheKeyName"
    assert stack_fact["sources"][0]["source_path"] == "/Resources/0"
    api_facts = extract_api_resource_references(
        product="ecs",
        action="DescribeKeyPairs",
        params={"KeyPairName": name},
        body={"KeyPairs": {"KeyPair": [{"KeyPairName": name}]}},
        region_id="cn-hangzhou",
        tool_use_id="offline-api-tool",
        account_scope="fake-account",
        console_partition="aliyun",
    )
    for fact in [stack_fact, *api_facts]:
        for source in fact["sources"]:
            source["turn_id"] = "offline-keypair-turn"
    store = ResourceReferenceStore(tmp_path / "references.json")
    store.observe([stack_fact, *api_facts])
    snapshot = store.snapshot()
    assert len(snapshot["items"]) == 1
    assert {source["toolUseId"] for source in snapshot["items"][0]["sources"]} == {
        "offline-stack-tool",
        "offline-api-tool",
    }
    script = tmp_path / "keypair.mjs"
    script.write_text(
        CATALOG_BOOTSTRAP
        + "import assert from 'node:assert/strict';\n"
        + "const rules = await import("
        + json.dumps((STATIC / "resource_link_rules.js").as_uri())
        + ");\n"
        + "const {ResourceIndex} = await import("
        + json.dumps((STATIC / "resource_index.js").as_uri())
        + ");\n"
        + "const {scanResourceOccurrences:scan} = await import("
        + json.dumps((STATIC / "resource_occurrences.js").as_uri())
        + ");\n"
        + "const linkify = await import("
        + json.dumps((STATIC / "resource_linkify.js").as_uri())
        + ");\n"
        + "const snapshot = "
        + json.dumps(snapshot)
        + ";\n"
        + r"""
const legacyIndex = new ResourceIndex({canonicalType:rules.canonicalResourceType});
await legacyIndex.loadSnapshot({schemaVersion:1,revision:1,bindings:[],items:[
  {...snapshot.items[0],referenceId:'ref-legacy-api'},
  {...snapshot.items[0],referenceId:'ref-legacy-stack',identifierKind:'id',
    identifierField:'PhysicalResourceId',attributes:{},aliases:[]},
]},()=>assert.fail());
assert.equal(legacyIndex.references.size,2);
const index = new ResourceIndex({canonicalType:rules.canonicalResourceType});
await index.loadSnapshot(snapshot,()=>assert.fail());
const occurrences = scan('offline-keypair',{index,context:{turnId:'offline-keypair-turn'}});
assert.equal(occurrences.length,1);
const resolution = linkify.resolveResourceOccurrenceAlternatives(occurrences[0],index);
assert.equal(resolution.status,'resolved',JSON.stringify(resolution));
assert.equal(resolution.url,'https://ecs.console.aliyun.com/#/keyPair/region/cn-hangzhou');
assert.equal(resolution.url.includes('offline-keypair'),false);
assert.equal(linkify.resourceLinkAccessibleLabel(occurrences[0],resolution),'offline-keypair');
""",
        encoding="utf-8",
    )
    result = subprocess.run([node, str(script)], capture_output=True, text=True, encoding="utf-8", timeout=20)
    assert result.returncode == 0, result.stderr


def test_legacy_snat_routes_require_parent_and_network_type(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is unavailable")
    script = tmp_path / "legacy-snat.mjs"
    script.write_text(
        CATALOG_BOOTSTRAP
        + "import assert from 'node:assert/strict';\n"
        + "const rules = await import("
        + json.dumps((STATIC / "resource_link_rules.js").as_uri())
        + ");\n"
        + r"""
const type = 'ALIYUN::ECS::SNatEntry';
const rule = rules.RESOURCE_LINK_RULES.find(item=>item.sourceType===type && item.partition==='aliyun');
const base = {resourceType:type, identifier:'snat-offline0001', identifierKind:'id',
  identifierField:'PhysicalResourceId', accountScope:'fake-account', consolePartition:'aliyun',
  regionId:'cn-hangzhou', attributes:{}};
const build = attributes=>rules.buildResourceConsoleUrl(rule,{...base,attributes});
assert.equal(build({}), null);
assert.equal(build({NatGatewayId:'ngw-offline0001'}), null);
assert.equal(build({NatGatewayId:'ngw-offline0001',NetworkType:'unknown'}), null);
assert.equal(build({NatGatewayId:'snat-offline0001',NetworkType:'intranet'}), null);
assert.equal(build({NatGatewayId:'ngw-offline0001',NetworkType:'intranet'}),
  'https://vpc.console.aliyun.com/vpc-nat/cn-hangzhou/nats/ngw-offline0001/snats');
assert.equal(build({NatGatewayId:'ngw-offline0001',NetworkType:'internet'}),
  'https://vpc.console.aliyun.com/nat/cn-hangzhou/nats/ngw-offline0001/snats');
""",
        encoding="utf-8",
    )
    result = subprocess.run([node, str(script)], capture_output=True, text=True, encoding="utf-8", timeout=20)
    assert result.returncode == 0, result.stderr


def test_maxcompute_list_project_name_reaches_existing_route_with_own_source(tmp_path):
    from iac_code.web.resource_references import ResourceReferenceStore

    node = shutil.which("node")
    if not node:
        pytest.skip("Node is unavailable")
    references = extract_api_resource_references(
        product="maxcompute",
        action="ListProjects",
        params={"region": "cn-hangzhou", "maxItem": 10},
        body={"data": {"projects": [{"name": "offline_project", "regionId": "cn-hangzhou", "status": "AVAILABLE"}]}},
        region_id="cn-hangzhou",
        tool_use_id="offline-project-source",
        account_scope="fake-project-account",
        console_partition="aliyun",
    )
    assert len(references) == 1
    store = ResourceReferenceStore(tmp_path / "maxcompute-references.json")
    store.observe(references)
    snapshot = store.snapshot()
    assert len(snapshot["items"]) == 1
    script = tmp_path / "maxcompute-original-source.mjs"
    script.write_text(
        CATALOG_BOOTSTRAP
        + "import assert from 'node:assert/strict';\n"
        + "const rules = await import("
        + json.dumps((STATIC / "resource_link_rules.js").as_uri())
        + ");\n"
        + "const {ResourceIndex} = await import("
        + json.dumps((STATIC / "resource_index.js").as_uri())
        + ");\n"
        + "const {scanResourceOccurrences:scan} = await import("
        + json.dumps((STATIC / "resource_occurrences.js").as_uri())
        + ");\n"
        + "const reference = "
        + json.dumps(snapshot["items"][0])
        + ";\n"
        + r"""
const index = new ResourceIndex({canonicalType:rules.canonicalResourceType});
await index.loadSnapshot({schemaVersion:1,revision:1,items:[reference],bindings:[]},()=>assert.fail());
assert.equal(reference.identifierField, 'name');
assert.equal(reference.identifierKind, 'name');
assert.equal(reference.sources[0].sourcePath, '/result/data/projects/0/name');
assert.equal(reference.sources[0].toolUseId, 'offline-project-source');
assert.equal(reference.accountScope, 'fake-project-account');
assert.equal(reference.regionId, 'cn-hangzhou');
assert.ok(!Object.hasOwn(reference.attributes, 'PhysicalResourceId'));
assert.deepEqual(scan('MaxCompute 项目 offline_project', {index}), [],
  'opaque names must not claim prose from an unrelated owner');
const occurrences = scan('MaxCompute 项目 offline_project', {index,context:{toolUseId:'offline-project-source'}});
assert.equal(occurrences.length, 1, JSON.stringify(occurrences));
const resolution = rules.resolveResourceOccurrence(occurrences[0], index);
assert.equal(resolution.status, 'resolved', JSON.stringify(resolution));
assert.equal(resolution.url,
  'https://maxcompute.console.aliyun.com/cn-hangzhou/project-detail/offline_project/param-config');
assert.equal(resolution.reference.referenceId, reference.referenceId);
""",
        encoding="utf-8",
    )
    result = subprocess.run([node, str(script)], capture_output=True, text=True, encoding="utf-8", timeout=20)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("action", ["ListStackResources", "GetStackResource"])
@pytest.mark.parametrize("original_row", [False, True], ids=["opaque-parent", "original-ros-row"])
@pytest.mark.parametrize("legacy_fact", [False, True], ids=["fresh-extraction", "saved-result-replay"])
def test_managed_nas_mount_target_extractor_reaches_source_owned_frontend(tmp_path, action, original_row, legacy_fact):
    from copy import deepcopy

    node = shutil.which("node")
    if not node:
        pytest.skip("Node is unavailable")
    physical = "offlinefs:unrelated-prefix.cn-hangzhou.nas.aliyuncs.com"
    # Preserve the actual ROS response order and its empty ModuleInfo object.
    # ResourceType follows the composite ID in ListStackResources, not before it.
    row = {
        "Status": "CREATE_COMPLETE",
        "LogicalResourceId": "mount_target",
        "ModuleInfo": {},
        "PhysicalResourceId": physical,
        "ResourceType": "alicloud_nas_mount_target",
        "StackName": "offline-stack-name",
        "StackId": "offline-stack",
    }
    if original_row:
        # Exact saved ListStackResources Resources/1 (source SHA e3ae41c32fb2dbd9...).
        # These resource locators are offline fixture data, not credentials or a live sample.
        row = {
            "Status": "CREATE_COMPLETE",
            "LogicalResourceId": "mount_target",
            "ModuleInfo": {},
            "PhysicalResourceId": "0023og3mkner8ecpipb:0023og3mkner8ecpipb-abx24.cn-hangzhou.nas.aliyuncs.com",
            "ResourceType": "alicloud_nas_mount_target",
            "StackName": "sol-r0039-tf-2df90af9",
            "StackId": "040a1914-1554-477c-b761-096c88ffa2f8",
        }
        physical = row["PhysicalResourceId"]
    filesystem, domain = physical.split(":")
    body = {"Resources": [row]} if action == "ListStackResources" else row
    source_path = "/result/Resources/0" if action == "ListStackResources" else "/result"
    if original_row and action == "ListStackResources":
        body = {
            "RequestId": "01A12A11-BB83-56D6-A91A-4CCF2506F89C",
            "Resources": [
                {
                    "Status": "CREATE_COMPLETE",
                    "LogicalResourceId": "filesystem",
                    "ModuleInfo": {},
                    "PhysicalResourceId": filesystem,
                    "ResourceType": "alicloud_nas_file_system",
                    "StackName": row["StackName"],
                    "StackId": row["StackId"],
                },
                row,
            ],
        }
        source_path = "/result/Resources/1"
    original = deepcopy(body)
    facts = extract_api_resource_references(
        product="ros",
        action=action,
        params={"StackId": row["StackId"], "RegionId": "cn-hangzhou"},
        body=body,
        region_id="cn-hangzhou",
        tool_use_id="offline-managed-member",
        account_scope="fake-account",
        console_partition="aliyun",
    )
    fact = next(item for item in facts if item["resource_type"] == "alicloud_nas_mount_target")
    assert body == original
    assert fact["identifier"] == physical
    assert fact["identifier_field"] == "PhysicalResourceId"
    assert fact["attributes"] == {
        "FileSystemId": filesystem,
        "MountTargetDomain": domain,
    }
    reference = resource_fact_to_web(fact)
    if legacy_fact:
        from iac_code.web.resource_reference_controller import _result_facts

        # Saved tool results predate the producer fix: replay must normalize metadata
        # without changing the literal identifier, owning tool or original response.
        legacy = deepcopy(reference)
        legacy["identifierField"] = "MountTargetDomain"
        legacy["attributes"] = {}
        saved = deepcopy(legacy)
        replayed = _result_facts({"resourceReferences": [legacy]}, "offline-managed-member")
        assert legacy == saved
        assert len(replayed) == 1
        reference = resource_fact_to_web(replayed[0])
        assert reference["identifier"] == physical
        assert reference["identifierField"] == "PhysicalResourceId"
        assert reference["attributes"] == fact["attributes"]
    reference["referenceId"] = "ref-managed-mount"
    # The tool card and decorator must share the actual versioned module instance.
    tool_cards_source = (STATIC / "components/tool_cards.js").read_text(encoding="utf-8")
    linkify_version = tool_cards_source.split("resource_linkify.js?v=", 1)[1].split('"', 1)[0]
    occurrences_version = tool_cards_source.split("resource_occurrences.js?v=", 1)[1].split('"', 1)[0]
    script = tmp_path / "managed-nas-mount.mjs"
    script.write_text(
        CATALOG_BOOTSTRAP
        + "import assert from 'node:assert/strict';\n"
        + "const rules = await import("
        + json.dumps((STATIC / "resource_link_rules.js").as_uri())
        + ");\n"
        + "const {ResourceIndex} = await import("
        + json.dumps((STATIC / "resource_index.js").as_uri())
        + ");\n"
        + "const {scanResourceOccurrences:scan,resourceToolContext} = await import("
        + json.dumps((STATIC / "resource_occurrences.js").as_uri() + "?v=" + occurrences_version)
        + ");\n"
        + "const linkify = await import("
        + json.dumps((STATIC / "resource_linkify.js").as_uri() + "?v=" + linkify_version)
        + ");\n"
        + "const toolCards = await import("
        + json.dumps((STATIC / "components/tool_cards.js").as_uri())
        + ");\n"
        + "const reference = "
        + json.dumps(reference)
        + ";\n"
        + "const row = "
        + json.dumps(row)
        + ";\n"
        + "const body = "
        + json.dumps(body)
        + ";\n"
        + "const filesystem = "
        + json.dumps(filesystem)
        + ";\n"
        + "const domain = "
        + json.dumps(domain)
        + ";\n"
        + "const sourcePath = "
        + json.dumps(source_path)
        + ";\n"
        + r"""
const index = new ResourceIndex({canonicalType:rules.canonicalResourceType});
await index.loadSnapshot({schemaVersion:1,revision:1,items:[reference],bindings:[]},()=>assert.fail());
const occurrences = scan(JSON.stringify(row),{index,context:{toolUseId:'offline-managed-member'}});
const occurrence = occurrences.find(item=>item.identifier===reference.identifier);
assert.ok(occurrence,JSON.stringify(occurrences));
const result = linkify.resolveResourceOccurrenceAlternatives(occurrence,index);
assert.equal(result.status,'resolved',JSON.stringify(result));
assert.equal(result.url,'https://nasnext.console.aliyun.com/cn-hangzhou/filesystem/'+filesystem+'/mount');
assert.equal(result.reference.sources[0].toolUseId,'offline-managed-member');
assert.equal(result.reference.sources[0].sourcePath,sourcePath);
assert.equal(result.reference.stackId,row.StackId);
assert.equal(result.reference.logicalResourceId,'mount_target');
assert.equal(reference.identifier,row.PhysicalResourceId);
assert.equal(reference.identifierField,'PhysicalResourceId');
assert.equal(result.reference.identifier,domain);

class Node {
  constructor(doc,tag,data='') {
    this.ownerDocument=doc;this.nodeType=tag?1:3;this.tagName=tag?.toUpperCase();
    this.data=data;this.childNodes=[];this.parentElement=null;this.attrs=new Map();this.handlers=new Map();
    this.dataset={};this.style={};this.className='';
  }
  get isConnected(){return this===this.ownerDocument.body||Boolean(this.parentElement?.isConnected);}
  set textContent(value){this.childNodes=[];this.append(new Node(this.ownerDocument,null,String(value??'')));}
  get children(){return this.childNodes.filter(n=>n.nodeType===1);}
  get textContent(){return this.nodeType===3?this.data:this.childNodes.map(n=>n.textContent).join('');}
  get firstChild(){return this.childNodes[0];}
  get nextSibling(){const peers=this.parentElement?.childNodes||[];return peers[peers.indexOf(this)+1];}
  hasAttribute(key){return this.attrs.has(key);}
  getAttribute(key){return this.attrs.get(key)??null;}
  setAttribute(key,value){this.attrs.set(key,String(value));}
  append(...nodes){for(const n of nodes){
    if(n.parentElement){const peers=n.parentElement.childNodes;peers.splice(peers.indexOf(n),1);}
    n.parentElement=this;this.childNodes.push(n);
  }}
  contains(node){return this===node||this.childNodes.some(n=>n.contains(node));}
  closest(selector){return selector==='tr'||selector==='td'?null:
    this.hasAttribute(selector.slice(1,-1))?this:this.parentElement?.closest(selector);}
  querySelectorAll(selector){
    const matches=n=>selector.startsWith('a[')?n.tagName==='A'&&n.hasAttribute('data-resource-auto-link'):
      n.hasAttribute(selector.slice(1,-1));
    return this.childNodes.flatMap(n=>[...(matches(n)?[n]:[]),...n.querySelectorAll(selector)]);
  }
  replaceWith(...nodes){
    const parent=this.parentElement,position=parent.childNodes.indexOf(this);
    parent.childNodes.splice(position,1);this.parentElement=null;
    for(const n of nodes){
      if(n.parentElement){const peers=n.parentElement.childNodes;peers.splice(peers.indexOf(n),1);}
      n.parentElement=parent;
    }
    parent.childNodes.splice(position,0,...nodes);
  }
  splitText(offset){
    const tail=new Node(this.ownerDocument,null,this.data.slice(offset));this.data=this.data.slice(0,offset);
    tail.parentElement=this.parentElement;
    const peers=this.parentElement.childNodes;peers.splice(peers.indexOf(this)+1,0,tail);return tail;
  }
  addEventListener(type,handler){this.handlers.set(type,handler);}
}

const doc={getSelection:()=>null,createElement(tag){return new Node(doc,tag);},
  createTextNode(value){return new Node(doc,null,String(value));}};
doc.body=new Node(doc,'body');
globalThis.document=doc;
const tool={toolName:'aliyun_api',toolUseId:'offline-managed-member',status:'completed',
  input:{product:'ros',action:reference.sources[0].action,region_id:'cn-hangzhou',
    params:{RegionId:'cn-hangzhou',StackId:row.StackId}},
  results:[{content:JSON.stringify(body,null,2)}]};
for (const text of [JSON.stringify(row),JSON.stringify(body,null,2)]) {
  const occurrence=scan(text,{index,context:resourceToolContext(tool)})
    .find(item=>item.identifier===reference.identifier);
  assert.ok(occurrence,JSON.stringify({text,context:resourceToolContext(tool)}));
  const result=linkify.resolveResourceOccurrenceAlternatives(occurrence,index);
  assert.equal(result.status,'resolved',JSON.stringify({occurrence,result}));
  assert.equal(result.reference.sources[0].sourcePath,sourcePath);
}
const root=toolCards.renderToolCards({tools:{[tool.toolUseId]:tool}});
doc.body.append(root);
const walk=node=>[node,...node.childNodes.flatMap(walk)];
const card=walk(root).find(node=>node.dataset.toolUseId===tool.toolUseId);
assert.ok(card);
assert.ok(walk(card).some(node=>node.className==='conclusion-scalar'&&node.textContent===reference.identifier),
  'real JSON tool cards render the original composite in its own structured field');
const scanCache=new Map();
linkify.decorateResourceLinks(card,{index,context:resourceToolContext(tool),source:{toolUseId:tool.toolUseId},scanCache});
const debugScans=[...scanCache.values()].filter(item=>item.text.includes(reference.identifier));

const anchors=card.querySelectorAll('[data-resource-link]').filter(node=>node.tagName==='A'&&
  node.textContent===reference.identifier);
assert.equal(anchors.length,1,JSON.stringify({debugScans,links:card.querySelectorAll('[data-resource-link]').map(node=>({
  tag:node.tagName,text:node.textContent,href:node.getAttribute('href')}))}));
assert.equal(anchors[0].getAttribute('href'),
  'https://nasnext.console.aliyun.com/cn-hangzhou/filesystem/'+filesystem+'/mount');
assert.equal(anchors[0].getAttribute('aria-label'),reference.identifier);

""",
        encoding="utf-8",
    )
    result = subprocess.run([node, str(script)], capture_output=True, text=True, encoding="utf-8", timeout=20)
    assert result.returncode == 0, result.stderr
