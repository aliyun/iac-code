"""Offline SAE Terraform identities and exact API route-source envelopes."""

import asyncio
import importlib.util
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from iac_code.tools.cloud.aliyun.resource_reference_extractors import extract_api_resource_references
from iac_code.tools.cloud.resource_references import resource_spec
from iac_code.web.resource_link_catalog import resource_link_catalog_payload

ROOT = Path(__file__).parents[2]


def test_legacy_namespace_ids_remain_separate_and_credentials_never_project():
    refs = extract_api_resource_references(
        product="sae",
        action="DescribeNamespaces",
        params={"RegionId": "cn-hangzhou"},
        body={
            "Data": {
                "Namespaces": [
                    {"NamespaceName": "Default", "RegionId": "cn-hangzhou"},
                    {
                        "NamespaceId": "cn-hangzhou:test",
                        "NamespaceName": "test",
                        "RegionId": "cn-hangzhou",
                        "AccessKey": "fake-sensitive-key",
                        "SecretKey": "fake-sensitive-secret",
                    },
                ]
            }
        },
        region_id="cn-hangzhou",
        tool_use_id="offline",
        console_partition="aliyun",
    )
    own = [ref for ref in refs if ref["resource_type"] == "alicloud_sae_namespace"]
    assert len(own) == 1
    assert own[0]["identifier"] == "cn-hangzhou:test"
    assert "fake-sensitive" not in json.dumps(refs)
    assert any(ref["resource_type"] == "ALIYUN::SAE::Namespace" for ref in refs)


@pytest.fixture
def runner():
    spec = importlib.util.spec_from_file_location(
        "offline_sae_route_sources", ROOT / "scripts/web/e2e/run_resource_link_acceptance.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_sae_tf_identity_does_not_confuse_namespace_name_and_id():
    assert resource_spec("alicloud_sae_application") is resource_spec("ALIYUN::SAE::Application")
    assert resource_spec("alicloud_sae_application").identifier_field == "AppId"
    namespace = resource_spec("alicloud_sae_namespace")
    assert namespace.resource_type == "alicloud_sae_namespace"
    assert namespace.identifier_field == "NamespaceId"
    assert namespace.scope_kind == "regional"
    assert namespace is not resource_spec("ALIYUN::SAE::Namespace")
    assert resource_spec("ALIYUN::SAE::Namespace").identifier_field == "NamespaceName"


@pytest.mark.parametrize(
    "mutation,expected_types",
    [
        (None, {"ALIYUN::SAE::Namespace", "alicloud_sae_namespace"}),
        ("missing_id", {"ALIYUN::SAE::Namespace"}),
        ("empty_id", {"ALIYUN::SAE::Namespace"}),
        ("missing_name", {"alicloud_sae_namespace"}),
        ("empty_name", {"alicloud_sae_namespace"}),
        ("missing_both", set()),
        ("action", set()),
        ("product", set()),
        ("envelope", set()),
    ],
)
def test_flat_namespace_projection_keeps_name_and_id_independent(mutation, expected_types):
    row = {
        "NamespaceId": "cn-shenzhen:test",
        "NamespaceName": "test",
        "NameSpaceShortId": "test",
        "RegionId": "cn-shenzhen",
        "AccessKey": "fake-sensitive-key",
        "SecretKey": "fake-sensitive-secret",
        "AgentInstall": "untrusted-install-command",
    }
    body = {"Data": [row]}
    product, action = "sae", "DescribeNamespaceList"
    if mutation == "action":
        action = "ListApplications"
    elif mutation == "product":
        product = "edas"
    elif mutation == "missing_id":
        row.pop("NamespaceId")
    elif mutation == "empty_id":
        row["NamespaceId"] = ""
    elif mutation == "missing_name":
        row.pop("NamespaceName")
    elif mutation == "empty_name":
        row["NamespaceName"] = ""
    elif mutation == "missing_both":
        row.pop("NamespaceId")
        row.pop("NamespaceName")
    elif mutation == "envelope":
        body = {"Data": {"Namespaces": [row]}}
    refs = extract_api_resource_references(
        product=product,
        action=action,
        body=body,
        params={"ContainCustom": True},
        region_id="cn-hangzhou",
        tool_use_id="offline",
        console_partition="aliyun",
    )
    assert {ref["resource_type"] for ref in refs} == expected_types
    assert len(refs) == len(expected_types)
    for ref in refs:
        canonical = ref["resource_type"] == "ALIYUN::SAE::Namespace"
        field = "NamespaceName" if canonical else "NamespaceId"
        assert ref["identifier_field"] == field
        assert ref["identifier_kind"] == ("name" if canonical else "id")
        assert ref["identifier"] == row[field]
        assert ref["region_id"] == "cn-shenzhen"
        assert ref["sources"][0]["source_path"] == f"/result/Data/0/{field}"
        assert ref["sources"][0]["tool_use_id"] == "offline"
        assert ref["sources"][0]["request_region_id"] == "cn-hangzhou"
    serialized = json.dumps(refs)
    assert "fake-sensitive" not in serialized
    assert "untrusted-install-command" not in serialized


def test_flat_namespace_names_never_borrow_another_rows_id():
    refs = extract_api_resource_references(
        product="sae",
        action="DescribeNamespaceList",
        params={},
        body={
            "Data": [
                {"NamespaceName": "name-only", "RegionId": "cn-shenzhen"},
                {"NamespaceId": "cn-hangzhou:id-only", "RegionId": "cn-hangzhou"},
            ]
        },
        region_id="cn-shenzhen",
        tool_use_id="offline",
        console_partition="aliyun",
    )
    assert {
        (ref["resource_type"], ref["identifier"], ref["region_id"], ref["sources"][0]["source_path"]) for ref in refs
    } == {
        ("ALIYUN::SAE::Namespace", "name-only", "cn-shenzhen", "/result/Data/0/NamespaceName"),
        ("alicloud_sae_namespace", "cn-hangzhou:id-only", "cn-hangzhou", "/result/Data/1/NamespaceId"),
    }


@pytest.mark.parametrize("kind", ["application", "namespace"])
@pytest.mark.parametrize(
    "mutation",
    [
        None,
        "string_code",
        "error_code",
        "success",
        "missing_success",
        "message",
        "missing_request",
        "product",
        "action",
        "type",
        "data_envelope",
    ],
)
def test_sae_business_success_is_not_arbitrary_http200(runner, kind, mutation):
    namespace = kind == "namespace"
    source_type = "alicloud_sae_namespace" if namespace else "alicloud_sae_application"
    action = "DescribeNamespaceList" if namespace else "ListApplications"
    data = [{"NamespaceId": "cn-hangzhou:test"}] if namespace else {"Applications": [{"AppId": "app-fake"}]}
    body = {"Code": 200, "Success": True, "Message": "success", "RequestId": "offline", "Data": data}
    event = {"product": "sae", "action": action}
    if mutation == "string_code":
        body["Code"] = "200"
    elif mutation == "error_code":
        body["Code"] = 400
    elif mutation == "success":
        body["Success"] = False
    elif mutation == "missing_success":
        body.pop("Success")
    elif mutation == "message":
        body["Message"] = "failed"
    elif mutation == "missing_request":
        body.pop("RequestId")
    elif mutation == "product":
        event["product"] = "ecs"
    elif mutation == "action":
        event["action"] = "DescribeRegions"
    elif mutation == "type":
        source_type = "alicloud_security_group"
    elif mutation == "data_envelope":
        body["Data"] = {} if namespace else []
    if mutation:
        with pytest.raises(runner.MatrixError, match="successful business"):
            runner._route_cloud_response_body(body, event, source_type)
    else:
        assert runner._route_cloud_response_body(body, event, source_type) is body


@pytest.mark.parametrize("kind", ["application", "namespace"])
def test_sae_own_route_registration_from_raw_not_shared_query_pass(runner, tmp_path, kind):
    namespace = kind == "namespace"
    source_type = "alicloud_sae_namespace" if namespace else "alicloud_sae_application"
    identifier = "cn-hangzhou:test" if namespace else "app-offline"
    action = "DescribeNamespaceList" if namespace else "ListApplications"
    row = {"NamespaceId": identifier, "NamespaceName": "test"} if namespace else {"AppId": identifier}
    data = [row] if namespace else {"Applications": [row]}
    body = {"Code": 200, "Success": True, "Message": "success", "RequestId": "offline", "Data": data}
    (tmp_path / "raw.json").write_text(json.dumps(body), encoding="utf-8")
    report = {
        "purpose": "bounded_cloud_source_preparation_only",
        "profile": "test-guima",
        "observedAt": "2026-10-04T00:00:00Z",
        "regionsCovered": ["cn-hangzhou"],
        "results": [
            {
                "region": "cn-hangzhou",
                "events": [
                    {
                        "product": "sae",
                        "action": action,
                        "profile": "test-guima",
                        "region": "cn-hangzhou",
                        "params": {},
                        "status": "ok",
                        "exitCode": 0,
                        "rawFile": "raw.json",
                    }
                ],
                "samples": {
                    source_type: [
                        {
                            "id": identifier,
                            "region": "cn-hangzhou",
                            "rawFile": "raw.json",
                        }
                    ]
                },
            }
        ],
    }
    path = tmp_path / "report.json"
    path.write_text(json.dumps(report), encoding="utf-8")
    runner.ROUTE_CLOUD_SOURCE_PATH = tmp_path / "registry.json"
    proof = runner.register_route_cloud_source(path, source_type, "cn-hangzhou")
    assert proof["contractId"] == f"route_catalog:{source_type}:aliyun"
    assert proof["sampleLevel"] == "L2"
    assert proof["actualApiCalled"] is True
    assert proof["canonicalResourceType"] == (source_type if namespace else "ALIYUN::SAE::Application")


def test_frontend_tf_sae_alias_and_independent_namespace_route(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node unavailable")
    script = tmp_path / "sae-identities.mjs"
    script.write_text(
        "globalThis.__IAC_RESOURCE_LINKS__="
        + json.dumps(resource_link_catalog_payload())
        + ";"
        + "const m=await import("
        + json.dumps((ROOT / "src/iac_code/web/static/js/resource_link_rules.js").as_uri())
        + ");import assert from 'node:assert/strict';"
        + """
const base={provider:'aliyun',referenceId:'ref-offline',semantic:true,identifierKind:'id',
  regionId:'cn-hangzhou',accountScope:'fake-account',consolePartition:'aliyun',attributes:{},aliases:[]};
const resolve=ref=>m.resolveResourceOccurrence(ref,{candidates:()=>[ref],bindingFor:()=>null,contextFor:()=>({})});
assert.equal(m.canonicalResourceType('alicloud_sae_application'),'ALIYUN::SAE::Application');
assert.equal(m.resourceSpec('alicloud_sae_application').identifierField,'AppId');
const app={...base,resourceType:'alicloud_sae_application',identifierField:'AppId',identifier:'app-offline'};
assert.equal(resolve(app).status,'resolved');
assert.ok(resolve(app).url.includes('app-offline'));
const ns={...base,resourceType:'alicloud_sae_namespace',identifierField:'NamespaceId',
  identifier:'cn-hangzhou:test',attributes:{NamespaceId:'cn-hangzhou:test',NamespaceName:'test'}};
assert.equal(m.canonicalResourceType(ns.resourceType),ns.resourceType);
assert.equal(resolve(ns).status,'resolved');
assert.equal(resolve(ns).url,'https://saenext.console.aliyun.com/cn-hangzhou/namespace');
assert.equal(resolve(ns).ruleId,'sae-terraform-namespace-list-cn');
const canonical={...base,resourceType:'ALIYUN::SAE::Namespace',identifierKind:'name',
  identifierField:'NamespaceName',identifier:'test',attributes:{NamespaceName:'test'}};
assert.equal(m.canonicalResourceType(canonical.resourceType),canonical.resourceType);
assert.equal(resolve(canonical).status,'resolved');
assert.equal(resolve(canonical).url,'https://saenext.console.aliyun.com/cn-hangzhou/namespace');
assert.equal(resolve(canonical).ruleId,'sae-namespace-list-cn');
assert.notEqual(resolve(canonical).ruleId,resolve(ns).ruleId);
assert.notEqual(resolve({...canonical,identifierField:'NamespaceId'}).status,'resolved');
for(const resourceType of [canonical.resourceType,ns.resourceType])
  assert.ok(globalThis.__IAC_RESOURCE_LINKS__.directApiContracts.some(
    item=>item.product==='sae'&&item.action==='DescribeNamespaceList'&&item.resourceType===resourceType));
for(const patch of [{regionId:''},{accountScope:''},{identifierField:'NamespaceName'},{identifier:'https://evil.invalid'}])
  assert.notEqual(resolve({...ns,...patch}).status,'resolved');
""",
        encoding="utf-8",
    )
    subprocess.run([node, str(script)], check=True, capture_output=True, encoding="utf-8")


@pytest.mark.parametrize(
    "name",
    [
        "China South 1 (Shenzhen)",
        "China East 1 (Hangzhou)",
    ],
)
def test_default_namespace_name_survives_live_metadata_replay_and_route(tmp_path, name):
    from iac_code.tools.cloud.aliyun.resource_reference_extractors import collect_api_resource_references
    from iac_code.tools.cloud.resource_references import resource_reference_metadata
    from iac_code.web.resource_reference_controller import ResourceReferenceController
    from iac_code.web.resource_references import resource_fact_to_web
    from iac_code.web.resource_sources import read_visible_source, source_contains_locator

    request = {
        "product": "sae",
        "action": "DescribeNamespaceList",
        "params": {"RegionId": "cn-shenzhen"},
        "region_id": "cn-shenzhen",
    }
    body = {
        "Code": 200,
        "Success": True,
        "RequestId": "offline",
        "Data": [
            {
                "NamespaceName": name,
                "NamespaceId": "cn-shenzhen",
                "RegionId": "cn-shenzhen",
                "AgentInstall": "untrusted-install-command",
            }
        ],
    }
    facts = collect_api_resource_references(
        credential=None,
        **request,
        body=body,
        tool_use_id="sae-tool",
        account_scope="fake-account",
        console_partition="aliyun",
    )
    assert {fact["resource_type"] for fact in facts} == {"ALIYUN::SAE::Namespace", "alicloud_sae_namespace"}
    canonical = next(fact for fact in facts if fact["resource_type"] == "ALIYUN::SAE::Namespace")
    assert canonical["identifier"] == name
    assert canonical["attributes"]["NamespaceName"] == name
    assert canonical["sources"][0]["source_path"] == "/result/Data/0/NamespaceName"
    history = {
        "messages": [],
        "tools": {
            "sae-tool": {
                "toolName": "aliyun_api",
                "toolUseId": "sae-tool",
                "input": request,
                "turnId": "own-turn",
                "messageId": "own-message",
                "results": [
                    {
                        "content": json.dumps(body),
                        "resourceReferences": [
                            resource_fact_to_web(fact) for fact in resource_reference_metadata(facts)["items"]
                        ],
                    }
                ],
            }
        },
    }
    controller = ResourceReferenceController(tmp_path / "references.json", lambda *args: None)
    snapshot = controller.hydrate(history)
    own = next(item for item in snapshot["items"] if item["resourceType"] == "ALIYUN::SAE::Namespace")
    assert own["identifier"] == name
    assert own["sources"][0]["toolUseId"] == "sae-tool"
    assert own["sources"][0]["turnId"] == "own-turn"
    source = read_visible_source(history, {"tool_use_id": "sae-tool", "source_path": "/tool"})
    assert source_contains_locator(source, name, "name", resource_spec("ALIYUN::SAE::Namespace"))
    assert not source_contains_locator(source, name + " extra", "name", resource_spec("ALIYUN::SAE::Namespace"))

    async def bind_and_close():
        await controller.resolve_choice(
            history,
            {
                "expectedRevision": snapshot["revision"],
                "source": {"toolUseId": "sae-tool", "sourcePath": "/tool"},
                "sourceDigest": source.digest,
                "group": [
                    "name",
                    name,
                    "NamespaceName",
                    "ALIYUN::SAE::Namespace",
                    "fake-account",
                    "aliyun",
                    "cn-shenzhen",
                    [],
                    {"kind": "field", "field": "NamespaceName"},
                    None,
                ],
                "identifier": name,
                "identifierKind": "name",
                "referenceId": own["referenceId"],
                "context": {},
            },
        )
        await controller.close()

    asyncio.run(bind_and_close())

    node = shutil.which("node")
    if not node:
        pytest.skip("Node unavailable")
    script = tmp_path / "sae-default-name.mjs"
    js_dir = ROOT / "src/iac_code/web/static/js"
    script.write_text(
        "globalThis.__IAC_RESOURCE_LINKS__="
        + json.dumps(resource_link_catalog_payload())
        + ";\n"
        + "import assert from 'node:assert/strict';\n"
        + "const rules=await import("
        + json.dumps((js_dir / "resource_link_rules.js").as_uri())
        + ");\n"
        + "const {ResourceIndex}=await import("
        + json.dumps((js_dir / "resource_index.js").as_uri())
        + ");\n"
        + "const {scanResourceOccurrences}=await import("
        + json.dumps((js_dir / "resource_occurrences.js").as_uri())
        + ");\n"
        + "const snapshot="
        + json.dumps(snapshot)
        + ";const name="
        + json.dumps(name)
        + ";\n"
        + """
const index=new ResourceIndex({canonicalType:rules.canonicalResourceType});
await index.loadSnapshot(snapshot,()=>assert.fail());
const occurrences=scanResourceOccurrences(name,{index,context:{toolUseId:'sae-tool'},complete:true});
const occurrence=occurrences.find(item=>item.identifier===name&&item.resourceType==='ALIYUN::SAE::Namespace');
assert.ok(occurrence,'the full observed default namespace name must form one occurrence');
assert.equal(occurrence.displayText,name);
const resolved=rules.resolveResourceOccurrence(occurrence,index);
assert.equal(resolved.status,'resolved');
assert.equal(resolved.reference.resourceType,'ALIYUN::SAE::Namespace');
assert.equal(resolved.url,'https://saenext.console.aliyun.com/cn-shenzhen/namespace');
assert.equal(resolved.ruleId,'sae-namespace-list-cn');
assert.equal(rules.literalIdentifier(name),false,'generic locators remain strict');
assert.equal(scanResourceOccurrences(name,{index,context:{toolUseId:'other-tool'},complete:true})
  .filter(item=>item.identifier===name).length,0,'another source cannot lend this name');
""",
        encoding="utf-8",
    )
    subprocess.run([node, str(script)], check=True, capture_output=True, encoding="utf-8")


@pytest.mark.parametrize(
    "name",
    [
        " name",
        "name ",
        "name\tother",
        "name\nother",
        "name*other",
        "name<other",
        "name" + chr(96) + "other",
        "$" + "{name}",
        "$(name)",
        "https://example.invalid/name",
        "name...other",
        "name…other",
        "",
        "x" * 1025,
    ],
)
def test_default_namespace_space_exception_rejects_nonliteral_values(name):
    refs = extract_api_resource_references(
        product="sae",
        action="DescribeNamespaceList",
        params={},
        body={"Data": [{"NamespaceName": name, "NamespaceId": "cn-shenzhen", "RegionId": "cn-shenzhen"}]},
        region_id="cn-shenzhen",
        tool_use_id="offline",
        console_partition="aliyun",
    )
    assert {ref["resource_type"] for ref in refs} == {"alicloud_sae_namespace"}


def test_custom_namespace_tool_card_resolves_exact_primary_source_field(tmp_path):
    from iac_code.tools.cloud.aliyun.resource_reference_extractors import collect_api_resource_references
    from iac_code.tools.cloud.resource_references import resource_reference_metadata
    from iac_code.web.resource_reference_controller import ResourceReferenceController
    from iac_code.web.resource_references import resource_fact_to_web

    node = shutil.which("node")
    if not node:
        pytest.skip("Node unavailable")
    request = {
        "product": "sae",
        "action": "DescribeNamespaceList",
        "params": {"RegionId": "cn-shenzhen", "ContainCustom": True},
        "region_id": "cn-shenzhen",
    }
    row = {
        "NamespaceName": "test",
        "NamespaceId": "cn-shenzhen:test",
        "NameSpaceShortId": "test",
        "RegionId": "cn-shenzhen",
        "Custom": True,
    }
    body = {
        "Code": 200,
        "Success": True,
        "RequestId": "offline",
        "Data": [
            {"NamespaceName": "China South 1 (Shenzhen)", "NamespaceId": "cn-shenzhen", "RegionId": "cn-shenzhen"},
            row,
        ],
    }
    facts = collect_api_resource_references(
        credential=None,
        **request,
        body=body,
        tool_use_id="sae-tool",
        account_scope="fake-account",
        console_partition="aliyun",
    )
    tool = {
        "toolName": "aliyun_api",
        "toolUseId": "sae-tool",
        "input": request,
        "turnId": "own-turn",
        "messageId": "own-message",
        "results": [
            {
                "content": json.dumps(body),
                "resourceReferences": [
                    resource_fact_to_web(fact) for fact in resource_reference_metadata(facts)["items"]
                ],
            }
        ],
    }
    controller = ResourceReferenceController(tmp_path / "references.json", lambda *args: None)
    snapshot = controller.hydrate({"messages": [], "tools": {"sae-tool": tool}})
    asyncio.run(controller.close())
    js_dir = ROOT / "src/iac_code/web/static/js"
    modules = {
        "rules": "resource_link_rules.js",
        "occ": "resource_occurrences.js",
        "links": "resource_linkify.js",
        "idx": "resource_index.js",
    }
    script = tmp_path / "sae-custom-tool-card.mjs"
    script.write_text(
        "globalThis.__IAC_RESOURCE_LINKS__="
        + json.dumps(resource_link_catalog_payload())
        + ";\n"
        + "import assert from 'node:assert/strict';\n"
        + "".join(
            f"const {key}=await import({json.dumps((js_dir / filename).as_uri())});\n"
            for key, filename in modules.items()
        )
        + "const snapshot="
        + json.dumps(snapshot)
        + ";\n"
        + "const row="
        + json.dumps(row)
        + ";const tool="
        + json.dumps(tool)
        + ";\n"
        + """
const canonical='ALIYUN::SAE::Namespace', terraform='alicloud_sae_namespace';
const index=new idx.ResourceIndex({canonicalType:rules.canonicalResourceType});
await index.loadSnapshot(snapshot,()=>assert.fail());
const refs=snapshot.items.filter(ref=>ref.identifier==='test'||ref.identifier==='cn-shenzhen:test');
assert.equal(refs.length,2,'canonical and Terraform facts stay independent');
assert.notEqual(refs[0].referenceId,refs[1].referenceId);
const toolContext=occ.resourceToolContext(tool);
assert.equal(toolContext.resourceType,undefined,'multi-contract API does not globally force a type');
function scan(field, patch={}) {
  // Match renderConclusionValue's scalar context, including its exact object/field path.
  const context={...occ.resourceContextFromObject(row,{...toolContext,turnId:'own-turn'}),field,
    structuralScope:'$/Data/1/'+field,...patch};
  return occ.scanResourceOccurrences(row[field],{index,context,field:context.field,complete:true})[0];
}
function resolve(field, patch={}) {
  const occurrence=scan(field,patch);
  assert.ok(occurrence,JSON.stringify({field,patch}));
  return links.resolveResourceOccurrenceAlternatives(occurrence,index);
}
const name=scan('NamespaceName');
assert.ok(name.alternatives?.some(item=>item.resourceType===terraform),
  'the same name is also a real Terraform alias, so full alternatives resolution is required');
const resolved=resolve('NamespaceName');
assert.equal(resolved.status,'resolved');
assert.equal(resolved.reference.resourceType,canonical);
assert.equal(resolved.reference.identifier,'test');
assert.equal(resolved.ruleId,'sae-namespace-list-cn');
assert.equal(resolved.url,'https://saenext.console.aliyun.com/cn-shenzhen/namespace');
const id=resolve('NamespaceId');
assert.equal(id.status,'resolved');
assert.equal(id.reference.resourceType,terraform);
assert.equal(id.reference.identifier,'cn-shenzhen:test');
assert.equal(id.ruleId,'sae-terraform-namespace-list-cn');

// Equal destinations, aliases, a sibling row or a different tool are not primary-source proof.
assert.equal(resolve('NameSpaceShortId').status,'ambiguous');
assert.equal(resolve('NamespaceName',{structuralScope:'$/Data/0/NamespaceName'}).status,'ambiguous');
assert.equal(resolve('NamespaceName',{toolUseId:'other-tool'}).status,'ambiguous');
assert.equal(resolve('NamespaceName',{structuralScope:undefined}).status,'ambiguous');
const prose=occ.scanResourceOccurrences('test',{index,context:toolContext,complete:true})[0];
assert.equal(links.resolveResourceOccurrenceAlternatives(prose,index).status,'ambiguous');

// Same-source primary names under conflicting accounts must still require a choice.
const primary=refs.find(ref=>ref.resourceType===canonical);
const other={...primary,referenceId:'ref-other-account',accountScope:'fake-other-account'};
assert.equal(await index.loadSnapshot({...snapshot,revision:snapshot.revision+1,items:[...snapshot.items,other]},
  ()=>assert.fail()),true);
assert.equal(resolve('NamespaceName').status,'ambiguous');
""",
        encoding="utf-8",
    )
    result = subprocess.run([node, str(script)], capture_output=True, encoding="utf-8")
    assert result.returncode == 0, result.stdout + result.stderr
