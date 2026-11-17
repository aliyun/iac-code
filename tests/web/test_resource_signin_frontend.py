"""Frontend resource sign-in destinations use iframe-capable console hosts."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from iac_code.web.resource_link_catalog import resource_link_catalog_payload

STATIC = Path(__file__).parents[2] / "src/iac_code/web/static/js"


def test_slow_bootstrap_survives_iframe_load_and_retry_mints_a_fresh_signin(tmp_path):
    script = tmp_path / "slow-resource-signin.mjs"
    script.write_text(
        "globalThis.__IAC_RESOURCE_LINKS__=" + json.dumps(resource_link_catalog_payload(), ensure_ascii=False) + ";\n"
        + "import assert from 'node:assert/strict';\n"
        + "const rules=await import(" + json.dumps((STATIC / "resource_link_rules.js").as_uri()) + ");\n"
        + "const preview=await import(" + json.dumps((STATIC / "components/resource_link_preview.js").as_uri()) + ");\n"
        + r"""
const timers=[],popups=[];
globalThis.setTimeout=(fn,ms)=>{timers.push({fn,ms});return timers.length;};
class Element {
  constructor(tag){this.tagName=tag.toUpperCase();this.childNodes=[];this.listeners={};
    this.classList={add(){},remove(){}};}
  append(...nodes){this.childNodes.push(...nodes);}
  setAttribute(){} remove(){} focus(){}
  addEventListener(name,fn){this.listeners[name]=fn;}
}
const doc={body:new Element('body'),createElement:tag=>new Element(tag),
  addEventListener(){},removeEventListener(){},defaultView:{requestAnimationFrame:fn=>fn(),open(){
    const popup={closed:false,navigationCompleted:false,document:{body:{}},
      location:{replace(url){popup.requestedUrl=url;}},close(){this.closed=true;}};
    popups.push(popup);return popup;
  }}};
const background=new Element('main'),alreadyInert=new Element('aside');
background.inert=false;alreadyInert.inert=true;
doc.body.append(background,alreadyInert);
Object.defineProperty(doc.body,'children',{get(){return this.childNodes;}});
const anchor={ownerDocument:doc,isConnected:true,focus(){}};
const reference={resourceType:'ALIYUN::ECS::SecurityGroup',identifier:'sg-offline123456',
  identifierField:'SecurityGroupId',identifierKind:'id',accountScope:'fake-account',
  consolePartition:'aliyun',regionId:'cn-hangzhou',attributes:{}};
const resolution=rules.resolveResourceOccurrence(reference,{candidates:()=>[reference],bindingFor:()=>null});
let mintCount=0,request;
preview.resetTopLevelSigninBootstrapForTests();
assert.equal(preview.openResourceLinkPreview(anchor,reference,resolution,{createSigninUrl:async value=>{
  request=value;mintCount++;
  return {status:'ready',consolePartition:'aliyun',destination:value.destinations.aliyun,
    url:'/api/sessions/offline/resource-references/signin-relay/'+String(mintCount).repeat(64)};
}}),true);
assert.equal(background.inert,true,'the preview blocks background focus and activation');
await Promise.resolve();await Promise.resolve();
const firstWait=timers.shift();assert.equal(firstWait.ms,2500);
firstWait.fn();await Promise.resolve();await Promise.resolve();
let panel=doc.body.childNodes.at(-1).childNodes[0];
let frame=panel.childNodes[1].childNodes[1];
assert.equal(popups[0].navigationCompleted,false,'the controlled login has not completed');
assert.equal(popups[0].closed,false,'elapsed time must not abort the login');
assert.equal(frame.src,request.destinations.aliyun);
frame.listeners.load();
assert.equal(popups[0].closed,false,'an iframe login/error load is not completion proof');
assert.equal(preview.needsTopLevelSigninBootstrap(request),true,'do not cache unverified success');
const retry=panel.childNodes[0].childNodes[1].childNodes[1];
assert.equal(retry.className,'resource-link-preview-retry');
retry.listeners.click();await Promise.resolve();await Promise.resolve();
assert.equal(popups[0].closed,true,'retry must dispose the original helper');
assert.equal(mintCount,2);assert.equal(popups.length,2);
assert.notEqual(popups[0].requestedUrl,popups[1].requestedUrl,'one-use relay must not be replayed');
const secondWait=timers.shift();secondWait.fn();await Promise.resolve();await Promise.resolve();
popups[1].navigationCompleted=true;
panel=doc.body.childNodes.at(-1).childNodes[0];frame=panel.childNodes[1].childNodes[1];
frame.listeners.load();
assert.equal(preview.needsTopLevelSigninBootstrap(request),true,'load alone must never grant a lease');
panel.childNodes[0].childNodes[1].childNodes[2].listeners.click();
assert.equal(popups[1].closed,true,'dismissing the preview still cleans up its helper');
assert.equal(background.inert,false,'dismiss restores the original background state');
assert.equal(alreadyInert.inert,true,'dismiss preserves pre-existing inert state');
""", encoding="utf-8",
    )
    result = subprocess.run(["node", str(script)], capture_output=True, text=True, encoding="utf-8", timeout=20)
    assert result.returncode == 0, result.stderr


def test_verified_cn_product_previews_use_4service_hosts(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is unavailable")
    script = tmp_path / "resource-signin-test.mjs"
    script.write_text(
        "globalThis.__IAC_RESOURCE_LINKS__="
        + json.dumps(resource_link_catalog_payload(), ensure_ascii=False)
        + ";\n"
        + "import assert from 'node:assert/strict';\n"
        + "const rules = await import("
        + json.dumps((STATIC / "resource_link_rules.js").as_uri())
        + ");\n"
        + "const preview = await import("
        + json.dumps((STATIC / "components" / "resource_link_preview.js").as_uri())
        + ");\n"
        + r"""
const stackRule = rules.RESOURCE_LINK_RULES.find((item) =>
  item.resourceType === 'ALIYUN::ROS::Stack' && item.partition === 'aliyun');
const stackId = 'a67cf255-4dae-49cd-8344-c18d2f2b374a';
const reference = {resourceType:'ALIYUN::ROS::Stack', identifier:stackId,
  identifierField:'StackId', identifierKind:'id',
  accountScope:'fake-account', consolePartition:'aliyun', regionId:'cn-hangzhou', attributes:{}};
const resolution = {ruleId:stackRule.ruleId, reference,
  url:`https://ros.console.aliyun.com/cn-hangzhou/stacks/${stackId}`};
const request = preview.buildEmbeddedSigninRequest(resolution);
assert.equal(request.preferredPartition,'aliyun');
assert.equal(request.destinations.aliyun,
  `https://ros4service.console.aliyun.com/cn-hangzhou/stacks/${stackId}`);
assert.equal(request.destinations.alibabacloud,
  `https://ros4service.console.alibabacloud.com/cn-hangzhou/stacks/${stackId}`);
assert.equal(preview.supportsEmbeddedSignin(resolution),true);
preview.resetTopLevelSigninBootstrapForTests();
assert.equal(preview.needsTopLevelSigninBootstrap(request,1000),true);
preview.rememberTopLevelSigninBootstrap(request,1000);
assert.equal(preview.needsTopLevelSigninBootstrap(request,1001),false);
const sameConsoleHost = {...request,destinations:{...request.destinations,
  aliyun:`https://ros4service.console.aliyun.com/cn-shanghai/stacks/${stackId}`}};
assert.equal(preview.needsTopLevelSigninBootstrap(sameConsoleHost,1001),false);
const anotherConsoleHost = {...request,destinations:{...request.destinations,
  aliyun:'https://cloudmonitor4service.console.aliyun.com/app-group/61020318/overview'}};
assert.equal(preview.needsTopLevelSigninBootstrap(anotherConsoleHost,1001),true);
assert.equal(preview.needsTopLevelSigninBootstrap(request,1000 + 30 * 60 * 1000),true);
assert.equal(preview.needsTopLevelSigninBootstrap({...request,accountScope:'another-account'},1001),true);
const popup={document:{title:'',body:{textContent:''}},opener:{},closed:false,close(){this.closed=true;}};
const popupCalls=[];
const popupDoc={defaultView:{open(...args){popupCalls.push(args);return popup;}}};
assert.equal(preview.openTopLevelSigninBootstrap(popupDoc),popup);
assert.deepEqual(popupCalls,[[
  'about:blank','_blank','popup,width=420,height=160,resizable=yes,scrollbars=no',
]]);
assert.equal(popup.document.title,'Loading…');
assert.equal(popup.document.body.textContent,'Loading…');
assert.deepEqual(popup.opener,{});
assert.equal(preview.openTopLevelSigninBootstrap({defaultView:{open(){return null;}}}),null);

const ecsRule = rules.RESOURCE_LINK_RULES.find((item) =>
  item.resourceType === 'ALIYUN::ECS::Instance' && item.partition === 'aliyun');
const ecs = {ruleId:ecsRule.ruleId,
  reference:{...reference,resourceType:'ALIYUN::ECS::Instance',identifier:'i-fixture123456',identifierField:'InstanceId'},
  url:'https://ecs.console.aliyun.com/server/i-fixture123456/detail?regionId=cn-hangzhou'};
assert.equal(preview.buildEmbeddedSigninRequest(ecs).destinations.aliyun,
  'https://ecs4service.console.aliyun.com/server/i-fixture123456/detail?regionId=cn-hangzhou');
assert.equal(preview.supportsEmbeddedSignin(ecs),true);

const roleRule = rules.RESOURCE_LINK_RULES.find((item) =>
  item.resourceType === 'ALIYUN::RAM::Role' && item.partition === 'aliyun');
const roleReference = {...reference,resourceType:'ALIYUN::RAM::Role',identifier:'HivotStackDeletionRole',
  identifierField:'RoleName',identifierKind:'name'};
const roleUrl = rules.buildResourceConsoleUrl(roleRule,roleReference);
const roleResolution = {ruleId:roleRule.ruleId,reference:roleReference,url:roleUrl};
assert.equal(roleUrl,'https://ram.console.aliyun.com/roles/detail?roleName=HivotStackDeletionRole');
assert.equal(preview.buildEmbeddedSigninRequest(roleResolution).destinations.aliyun,
  'https://ram4service.console.aliyun.com/roles/detail?roleName=HivotStackDeletionRole');
assert.equal(preview.supportsEmbeddedSignin(roleResolution),true);
const roleFact = {...roleReference,identifierField:'PhysicalResourceId',identifierKind:'id',
  aliases:[{identifier:'HivotStackDeletionRole',identifierField:'RoleName',identifierKind:'name'}],sources:[]};
const resolvedRole = rules.resolveResourceOccurrence(
  {...roleFact,displayText:roleFact.identifier},
  {candidates:()=>[roleFact],bindingFor:()=>null},
);
assert.equal(resolvedRole.status,'resolved');
assert.equal(resolvedRole.reference.identifierField,'RoleName');
assert.equal(preview.supportsEmbeddedSignin(resolvedRole),true);

for (const [resourceType, identifier, identifierField, ordinaryHost, embeddedHost] of [
  ['ALIYUN::VPC::VPC','vpc-fixture123456','VpcId','vpc.console.aliyun.com','vpc4service.console.aliyun.com'],
  ['ALIYUN::VPC::VSwitch','vsw-fixture123456','VSwitchId','vpc.console.aliyun.com','vpc4service.console.aliyun.com'],
  ['ALIYUN::ECS::SecurityGroup','sg-fixture123456','SecurityGroupId','ecs.console.aliyun.com','ecs4service.console.aliyun.com'],
  ['ALIYUN::RAM::ManagedPolicy','FixturePolicy','PhysicalResourceId','ram.console.aliyun.com','ram4service.console.aliyun.com'],
]) {
  const rule = rules.RESOURCE_LINK_RULES.find(
    (item) => item.resourceType === resourceType && item.partition === 'aliyun',
  );
  const itemReference = {...reference,resourceType,identifier,identifierField,
    attributes:resourceType === 'ALIYUN::RAM::ManagedPolicy'
      ? {PolicyName:identifier,PolicyType:'Custom'} : {}};
  const url = rules.buildResourceConsoleUrl(rule,itemReference);
  assert.equal(new URL(url).hostname,ordinaryHost);
  const itemResolution = {ruleId:rule.ruleId,reference:itemReference,url};
  const itemRequest = preview.buildEmbeddedSigninRequest(itemResolution);
  assert.equal(new URL(itemRequest.destinations.aliyun).hostname,embeddedHost);
  assert.equal(preview.supportsEmbeddedSignin(itemResolution),true);
    if (resourceType === 'ALIYUN::RAM::ManagedPolicy') {
    assert.equal(url,
      'https://ram.console.aliyun.com/policies/detail?policyType=Custom&policyName=FixturePolicy');
    assert.equal(itemRequest.destinations.aliyun,
      'https://ram4service.console.aliyun.com/policies/detail?policyType=Custom&policyName=FixturePolicy');
    }
  }

const systemPolicyReference = {...reference,resourceType:'ALIYUN::RAM::ManagedPolicy',
  identifier:'AdministratorAccess',identifierField:'PhysicalResourceId',
  attributes:{PolicyName:'AdministratorAccess',PolicyType:'System'}};
const systemPolicyRule = rules.RESOURCE_LINK_RULES.find((item) =>
  item.resourceType === systemPolicyReference.resourceType && item.partition === 'aliyun');
const systemPolicy = {ruleId:systemPolicyRule.ruleId,reference:systemPolicyReference,
  url:rules.buildResourceConsoleUrl(systemPolicyRule,systemPolicyReference)};
assert.equal(preview.buildEmbeddedSigninRequest(systemPolicy).destinations.aliyun,
  'https://ram4service.console.aliyun.com/policies/detail?policyType=System&policyName=AdministratorAccess');

for (const [resourceType,identifier,attributes] of [
  ['ALIYUN::RAM::Group','g-test123456',{GroupName:'ops-team'}],
  ['ALIYUN::RAM::User','user-name',{UserId:'200000000000000000'}],
]) {
  const rule = rules.RESOURCE_LINK_RULES.find((item) =>
    item.resourceType === resourceType && item.partition === 'aliyun');
  const itemReference = {...reference,resourceType,identifier,identifierField:rule.identifierField,
    identifierKind:rules.resourceSpec(resourceType).identifierKind,attributes};
  const itemResolution = {ruleId:rule.ruleId,reference:itemReference,
    url:rules.buildResourceConsoleUrl(rule,itemReference)};
  const itemRequest = preview.buildEmbeddedSigninRequest(itemResolution);
  assert.equal(itemRequest,null,`${resourceType}: ${itemResolution.url}`);
  assert.equal(preview.supportsEmbeddedSignin(itemResolution),false);
}

const albReferences = {
  ...reference,
  regionId: 'cn-hangzhou',
};
for (const [resourceType, identifier, embedded] of [
  ['ALIYUN::ALB::LoadBalancer', 'alb-fixture123456', true],
  ['ALIYUN::ALB::ServerGroup', 'sg-fixture123456', true],
  ['ALIYUN::ALB::SecurityPolicy', 'sp-fixture123456', false],
]) {
  const rule = rules.RESOURCE_LINK_RULES.find((item) =>
    item.resourceType === resourceType && item.partition === 'aliyun');
  assert.ok(rule, resourceType);
  const itemReference = {...albReferences, resourceType, identifier,
    identifierField:rule.identifierField, identifierKind:rules.resourceSpec(resourceType).identifierKind};
  const url = rules.buildResourceConsoleUrl(rule, itemReference);
  const resolution = {ruleId:rule.ruleId, reference:itemReference, url};
  const request = preview.buildEmbeddedSigninRequest(resolution);
  assert.equal(preview.supportsEmbeddedSignin(resolution), embedded, resourceType);
  if (embedded) {
    assert.ok(request, resourceType);
    assert.equal(new URL(request.destinations.aliyun).hostname, 'slb4service.console.aliyun.com');
  } else {
    assert.equal(request, null, resourceType);
  }
}

// The runner uses this frontend capability to choose ordinary navigation.
const securityPolicyRule = rules.RESOURCE_LINK_RULES.find((item) =>
  item.resourceType === 'ALIYUN::ALB::SecurityPolicy' && item.partition === 'aliyun');
const securityPolicyReference = {...albReferences, resourceType:'ALIYUN::ALB::SecurityPolicy',
  identifier:'sp-fixture123456', identifierField:securityPolicyRule.identifierField,
  identifierKind:rules.resourceSpec('ALIYUN::ALB::SecurityPolicy').identifierKind};
const securityPolicyResolution = {ruleId:securityPolicyRule.ruleId,
  reference:securityPolicyReference,
  url:rules.buildResourceConsoleUrl(securityPolicyRule, securityPolicyReference)};
const embeddedCapability = 'embedded';
const expectedOrdinaryNavigation = preview.supportsEmbeddedSignin(securityPolicyResolution) &&
  embeddedCapability === 'embedded'
  ? {expectedNavigation:'iframe', expectedNavigationReason:'configured_signin_role'}
  : {expectedNavigation:'new_tab', expectedNavigationReason:'product_external_only'};
assert.deepEqual(expectedOrdinaryNavigation,
  {expectedNavigation:'new_tab', expectedNavigationReason:'product_external_only'});

const groupListRule = rules.RESOURCE_LINK_RULES.find((item) => item.ruleId === 'ram-group-list-cn');
const groupListReference = {...reference,resourceType:'ALIYUN::RAM::Group',identifier:'g-test123456',
  identifierField:'PhysicalResourceId',attributes:{}};
const groupListResolution = {ruleId:groupListRule.ruleId,reference:groupListReference,
  url:rules.buildResourceConsoleUrl(groupListRule,groupListReference)};
assert.equal(preview.buildEmbeddedSigninRequest(groupListResolution),null);

for (const [resourceType,identifier,embeddedHost] of [
  ['ALIYUN::CEN::CenInstance','cen-fixture123456','cen4service.console.aliyun.com'],
  ['ALIYUN::ADB::DBCluster','amv-fixture123456','ads4service.console.aliyun.com'],
]) {
  const rule = rules.RESOURCE_LINK_RULES.find((item) =>
    item.resourceType === resourceType && item.partition === 'aliyun');
  assert.ok(rule,resourceType);
  const itemReference = {...reference,resourceType,identifier,identifierField:rule.identifierField,
    identifierKind:rules.resourceSpec(resourceType).identifierKind};
  const itemResolution = {ruleId:rule.ruleId,reference:itemReference,
    url:rules.buildResourceConsoleUrl(rule,itemReference)};
  assert.ok(itemResolution.url,resourceType);
  const itemRequest = preview.buildEmbeddedSigninRequest(itemResolution);
  assert.ok(itemRequest,resourceType);
  assert.equal(new URL(itemRequest.destinations.aliyun).hostname,embeddedHost);
  assert.equal(preview.supportsEmbeddedSignin(itemResolution),true);
}

const cenBandwidthPackageRule = rules.RESOURCE_LINK_RULES.find((item) =>
  item.resourceType === 'ALIYUN::CEN::CenBandwidthPackage' && item.partition === 'aliyun');
const cenBandwidthPackageReference = {...reference,
  resourceType:'ALIYUN::CEN::CenBandwidthPackage', identifier:'cenbwp-fixture123456',
  identifierField:cenBandwidthPackageRule.identifierField,
  identifierKind:rules.resourceSpec('ALIYUN::CEN::CenBandwidthPackage').identifierKind};
const cenBandwidthPackageUrl = rules.buildResourceConsoleUrl(
  cenBandwidthPackageRule, cenBandwidthPackageReference);
assert.equal(cenBandwidthPackageUrl,'https://cen.console.aliyun.com/cen/bandwidth');
const cenBandwidthPackageResolution = {ruleId:cenBandwidthPackageRule.ruleId,
  reference:cenBandwidthPackageReference, url:cenBandwidthPackageUrl};
assert.equal(preview.buildEmbeddedSigninRequest(cenBandwidthPackageResolution),null);
assert.equal(preview.supportsEmbeddedSignin(cenBandwidthPackageResolution),false);

const cenBandwidthLimitRule = rules.RESOURCE_LINK_RULES.find((item) =>
  item.resourceType === 'ALIYUN::CEN::CenBandwidthLimit' && item.partition === 'aliyun');
const cenBandwidthLimitReference = {...reference,
  resourceType:'ALIYUN::CEN::CenBandwidthLimit', identifier:'cen-fixture123456',
  identifierField:cenBandwidthLimitRule.identifierField,
  identifierKind:rules.resourceSpec('ALIYUN::CEN::CenBandwidthLimit').identifierKind};
const cenBandwidthLimitResolution = {ruleId:cenBandwidthLimitRule.ruleId,
  reference:cenBandwidthLimitReference,
  url:rules.buildResourceConsoleUrl(cenBandwidthLimitRule, cenBandwidthLimitReference)};
assert.equal(preview.supportsEmbeddedSignin(cenBandwidthLimitResolution),true);
assert.equal(new URL(preview.buildEmbeddedSigninRequest(cenBandwidthLimitResolution).destinations.aliyun).hostname,
  'cen4service.console.aliyun.com');

const elasticsearchRule = rules.RESOURCE_LINK_RULES.find((item) =>
  item.sourceType === 'alicloud_elasticsearch_instance' && item.partition === 'aliyun');
const elasticsearchReference = {...reference,resourceType:'alicloud_elasticsearch_instance',
  identifier:'es-fixture123456',identifierField:elasticsearchRule.identifierField,
  identifierKind:rules.resourceSpec('alicloud_elasticsearch_instance').identifierKind};
const elasticsearch = {ruleId:elasticsearchRule.ruleId,reference:elasticsearchReference,
  url:rules.buildResourceConsoleUrl(elasticsearchRule,elasticsearchReference)};
assert.equal(new URL(elasticsearch.url).hostname,'elasticsearch-cn-hangzhou.console.aliyun.com');
assert.equal(new URL(preview.buildEmbeddedSigninRequest(elasticsearch).destinations.aliyun).hostname,
  'elasticsearch4service-cn-hangzhou.console.aliyun.com');

const externalOnlyHosts = new Set([
  'acmnext.console.aliyun.com',
  'apigateway.console.aliyun.com',
  'appflow.console.aliyun.com',
  'dhsnext.console.aliyun.com',
  'dms.console.aliyun.com',
  'ehpc.console.aliyun.com',
  'emr.console.aliyun.com',
  'emr-next.console.aliyun.com',
  'essnew.console.aliyun.com',
  'gws.console.aliyun.com',
  'cloudmonitor.console.aliyun.com',
]);
    const externalOnlyResourceTypes = new Set([
      'ALIYUN::ALB::SecurityPolicy',
      'ALIYUN::CEN::CenBandwidthPackage',
      'ALIYUN::RAM::Group',
      'ALIYUN::RAM::User',
]);
let domesticEmbeddedRules = 0;
let domesticExternalRules = 0;
for (const rule of rules.RESOURCE_LINK_RULES.filter((item) =>
  item.partition === 'aliyun' && item.provenance.kind === 'production-channel-config')) {
  const spec = rules.resourceSpec(rule.sourceType);
  const attributes = Object.fromEntries(rule.requiredFields.map((field) => [field,
    field === 'DestinationRegionId' ? 'cn-shanghai' :
    field === 'RouterType' ? 'VBR' :
    field === 'NetworkType' ? 'intranet' :
    field === 'NatGatewayId' ? 'ngw-fixture123456' : `${field}-value`]));
  if (rule.sourceType === 'alicloud_slb_listener') Object.assign(attributes,
    {load_balancer_id:'lb-fixture',LoadBalancerId:'lb-fixture',ListenerProtocol:'http',ListenerPort:'80'});
  if (rule.sourceType === 'alicloud_slb_rule') Object.assign(attributes,
    {LoadBalancerId:'lb-fixture',ListenerProtocol:'http',ListenerPort:'80'});
  const identifier = rule.sourceType === 'alicloud_slb_listener' ? 'lb-fixture:http:80' :
    rule.sourceType === 'ALIYUN::CEN::RouteEntry' ? 'cen-fixture123456/vpc-fixture123456/vtb-fixture123456' :
    spec.resourceType === 'ALIYUN::ECS::NetworkInterface' ? 'eni-fixture123456' :
    spec.identifierKind === 'name' ? 'resource-name' : 'resource-id-1';
  const itemReference = {resourceType:rule.sourceType,identifierField:spec.identifierField,
    identifierKind:spec.identifierKind,identifier,
    regionId:'cn-hangzhou',accountScope:'fake-account',consolePartition:'aliyun',attributes,
    logicalResourceId:'LogicalResource'};
  const url = rules.buildResourceConsoleUrl(rule,itemReference);
  assert.ok(url,rule.sourceType);
  const ordinaryHost = new URL(url).hostname;
  if (!ordinaryHost.endsWith('.console.aliyun.com')) continue;
  const request = preview.buildEmbeddedSigninRequest({ruleId:rule.ruleId,reference:itemReference,url});
  if (externalOnlyHosts.has(ordinaryHost) || externalOnlyResourceTypes.has(rule.resourceType)) {
    assert.equal(request,null,`${rule.sourceType}: ${url}`);
    domesticExternalRules += 1;
    continue;
  }
  assert.ok(request,`${rule.sourceType}: ${url}`);
  assert.ok(new URL(request.destinations.aliyun).hostname.includes('4service'),
    `${rule.sourceType}: ${request.destinations.aliyun}`);
  domesticEmbeddedRules += 1;
}
    assert.equal(domesticEmbeddedRules,342);
    assert.equal(domesticExternalRules,55);
""",
        encoding="utf-8",
    )
    result = subprocess.run([node, str(script)], capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
