"""Offline SLB link resolution, no browser, cloud, or local account state."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from iac_code.web.resource_link_catalog import resource_link_catalog_payload


def test_slb_child_links_require_exact_parent_and_listener_tuple(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node unavailable")
    rules = Path(__file__).parents[2] / "src/iac_code/web/static/js/resource_link_rules.js"
    script = tmp_path / "slb-links.mjs"
    script.write_text(
        "globalThis.__IAC_RESOURCE_LINKS__="
        + json.dumps(resource_link_catalog_payload())
        + ";\n"
        + "import assert from 'node:assert/strict';\nconst rules = await import("
        + json.dumps(rules.as_uri())
        + ");\n"
        + r"""
const base = {provider:'aliyun',referenceId:'ref-slb',semantic:true,identifierKind:'id',
  accountScope:'fake-account',consolePartition:'aliyun',regionId:'cn-beijing',aliases:[]};
const resolve = ref => rules.resolveResourceOccurrence(ref,{
  candidates:()=>[ref],bindingFor:()=>null,contextFor:()=>({})});
const ca = {...base,resourceType:'alicloud_slb_ca_certificate',identifier:'ca-fixture',
  identifierField:'CACertificateId',attributes:{CACertificateName:'example'}};
assert.equal(rules.resourceSpec(ca.resourceType).identifierField,'CACertificateId');
assert.equal(resolve(ca).status,'resolved');
assert.equal(resolve(ca).url,'https://slb.console.aliyun.com/slb/cn-beijing/certs');
const group = {...base,resourceType:'alicloud_slb_master_slave_server_group',identifier:'rsp-fixture',
  identifierField:'MasterSlaveServerGroupId',attributes:{LoadBalancerId:'lb-fixture'}};
assert.equal(resolve(group).url,'https://slb.console.aliyun.com/slb/cn-beijing/slbs/lb-fixture/ms-server-groups/');
assert.notEqual(resolve({...group,attributes:{}}).status,'resolved');
const listener = {...base,resourceType:'alicloud_slb_listener',identifier:'lb-fixture:http:80',
  identifierField:'ListenerId',attributes:{load_balancer_id:'lb-fixture',LoadBalancerId:'lb-fixture',
    ListenerPort:'80',ListenerProtocol:'http'}};
assert.equal(resolve(listener).url,'https://slb.console.aliyun.com/slb/cn-beijing/slbs/lb-fixture/listeners');
for (const attributes of [{...listener.attributes,load_balancer_id:'lb-other'},
  {...listener.attributes,LoadBalancerId:'lb-other'},{...listener.attributes,ListenerPort:'81'},
  {...listener.attributes,ListenerProtocol:'udp'},{}]) {
  assert.notEqual(resolve({...listener,attributes}).status,'resolved');
}
for (const identifier of ['80','lb-fixture:http:65536','lb-fixture:http:080','lb-fixture:smtp:80']) {
  assert.notEqual(resolve({...listener,identifier}).status,'resolved');
}
const rule = {...base,resourceType:'alicloud_slb_rule',identifier:'rule-fixture',identifierField:'RuleId',
  attributes:{LoadBalancerId:'lb-fixture',ListenerPort:'80',ListenerProtocol:'http'}};
assert.equal(resolve(rule).url,'https://slb.console.aliyun.com/slb/cn-beijing/slbs/lb-fixture/listeners');
for (const attributes of [{LoadBalancerId:'lb-fixture'}, {...rule.attributes,ListenerProtocol:'tcp'},
  {...rule.attributes,ListenerPort:'0'}]) assert.notEqual(resolve({...rule,attributes}).status,'resolved');
for (const ref of [ca,group,listener,rule]) {
  assert.notEqual(resolve({...ref,accountScope:''}).status,'resolved');
  assert.notEqual(resolve({...ref,regionId:''}).status,'resolved');
  assert.notEqual(resolve({...ref,identifier:'https://evil.invalid'}).status,'resolved');
}
"""
        + "\nconst links = await import("
        + json.dumps((rules.parent / "resource_linkify.js").as_uri())
        + ");\n"
        + r"""
const portOccurrence = {displayText:'8080'};
const exact = {...listener,identifier:'lb-offline0001:http:8080'};
assert.equal(links.resourceLinkAccessibleLabel(portOccurrence,{status:'resolved',reference:exact}),
  'lb-offline0001:http:8080');
assert.equal(links.resourceLinkAccessibleLabel(portOccurrence,{status:'pending',reference:exact}),'8080');
assert.equal(links.resourceLinkAccessibleLabel(portOccurrence,{status:'resolved',reference:group}),'8080');
assert.equal(links.resourceLinkAccessibleLabel(portOccurrence,{status:'resolved',reference:{...exact,identifier:'8080'}}),'8080');
assert.equal(links.resourceLinkAccessibleLabel(portOccurrence,{status:'resolved',reference:{...exact,identifier:'https://evil.invalid'}}),'8080');
""",
        encoding="utf-8",
    )
    subprocess.run([node, str(script)], check=True, capture_output=True, text=True, encoding="utf-8")
