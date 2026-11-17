"""Exact visible account-resource identity, no clouds or user configuration."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    "kind,change,matched",
    [
        ("record", {}, True),
        ("record", {"host": "dnsnext4service.console.aliyun.com"}, True),
        ("record", {"host": "dnsnext.console.alibabacloud.com"}, False),
        ("record", {"zone": "other"}, False),
        ("record", {"query": "?Rr=123"}, False),
        ("record", {"rows": []}, False),
        ("record", {"duplicates": True}, False),
        ("record", {"rows": [{"cells": ["*", "A", "192.0.2.2"]}]}, False),
        ("record", {"missingTuple": True}, False),
        ("group", {}, True),
        ("group", {"route": "/groups/ops"}, True),
        ("group", {"route": "/groups/detail?groupName=other&activeTab=MemberUser"}, False),
        ("group", {"route": "/groups/detail?groupName=ops&groupName=other"}, False),
        ("group", {"route": "/groups/detail?groupName=ops&resourceId=other"}, False),
        ("group", {"host": "ram.console.alibabacloud.com"}, False),
        ("group", {"names": []}, False),
        ("group", {"names": ["ops-other"]}, False),
        ("group", {"names": ["ops", "ops-other"]}, False),
        ("group", {"ids": []}, False),
        ("group", {"ids": ["g-api-id"]}, False),
        ("group", {"wrongApiId": True}, False),
    ],
)
def test_visible_account_resource_identity(kind, change, matched):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node unavailable")
    module = Path(__file__).parents[2] / "scripts/web/e2e/account_resource_landing.mjs"
    script = (
        """
    import {matchAccountResourceLanding} from MODULE;
    const c=CHANGE,k=KIND,record=k==='record';
    const ref=record?{resourceType:'ALIYUN::PVTZ::ZoneRecord',identifier:'123',
      attributes:{ZoneId:'zone-fake',Rr:'*',Type:'A',Value:'192.0.2.1'}}:
      {resourceType:'ALIYUN::RAM::Group',identifier:'g-api-id',
       attributes:{GroupId:c.wrongApiId?'g-other':'g-api-id',GroupName:'ops'}};
    if(c.missingTuple)delete ref.attributes.Value;
    const host=record?'dnsnext.console.aliyun.com':'ram.console.aliyun.com';
    const path=record?'/privateDNS/zones/zone-fake/records':'/groups/ops';
    const route=record?'/privateDNS/zones/'+(c.zone||'zone-fake')+'/records'+(c.query||''):
      (c.route||'/groups/detail?groupName=ops&activeTab=MemberUser');
    const row={cells:['*','A','默认','192.0.2.1','60']};
    const evidence={rows:c.rows||(c.duplicates?[row,row]:[row]),
      groupNames:c.names||['ops'],groupIds:c.ids||['123456789']};
    console.log(JSON.stringify(matchAccountResourceLanding('https://'+(c.host||host)+route,
      'https://'+host+path,ref,evidence)));
    """.replace("MODULE", json.dumps(module.as_uri()))
        .replace("CHANGE", json.dumps(change))
        .replace("KIND", json.dumps(kind))
    )
    result = subprocess.run(
        [node, "--input-type=module", "-e", script], check=True, capture_output=True, encoding="utf-8", timeout=10
    )
    assert json.loads(result.stdout)["matched"] is matched


def test_special_account_paths_never_use_generic_search_or_url_echo():
    runner = Path(__file__).parents[2] / "scripts/web/e2e/resource_link_chrome_batch.mjs"
    source = runner.read_text(encoding="utf-8")
    assert "!isAccountResourceLanding(reference) &&" in source
    assert "accountResource ? Boolean(accountResource.matched)" in source
    schema = json.loads(runner.with_name("resource_link_browser_batch.schema.json").read_text(encoding="utf-8"))
    assert "accountResource" in schema["properties"]["items"]["items"]["properties"]["diagnostics"]["properties"]
