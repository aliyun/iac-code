"""Offline exact child row/parent/region detection; no real browser or cloud."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]


@pytest.mark.parametrize("kind", ["namespace", "rule"])
@pytest.mark.parametrize(
    "change,matched",
    [
        ("none", True),
        ("service", True),
        ("wrong-host", False),
        ("wrong-region", False),
        ("wrong-parent", False),
        ("missing-attrs", False),
        ("duplicate", False),
        ("wrong-child", False),
        ("loading", False),
        ("no-table", False),
        ("wrong-headers", False),
        ("substring", False),
        ("no-rows", False),
        ("query", False),
        ("hash", False),
        ("split-row", False),
    ],
)
def test_precise_child_list(kind, change, matched):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node unavailable")
    if kind == "namespace":
        ref = {
            "resourceType": "alicloud_sae_namespace",
            "identifier": "cn-fake:test",
            "regionId": "cn-fake",
            "attributes": {
                "NamespaceId": "cn-fake:test",
                "NamespaceName": "test-name",
                "NameSpaceShortId": "test",
                "RegionId": "cn-fake",
            },
        }
        url = "https://saenext.console.aliyun.com/cn-fake/namespace"
        headers, cells = ["命名空间名称/ID", "描述"], ["test-name\ntest", "description"]
    else:
        ref = {
            "resourceType": "ALIYUN::SAG::ACLRule",
            "identifier": "acr-fake",
            "regionId": "cn-fake",
            "attributes": {
                "AclId": "acl-fake",
                "Name": "rule-name",
                "Direction": "in",
                "Policy": "accept",
                "IpProtocol": "ALL",
                "SourceCidr": "0.0.0.0/0",
                "DestCidr": "0.0.0.0/0",
                "SourcePortRange": "-1/-1",
                "DestPortRange": "-1/-1",
                "Priority": "1",
            },
        }
        url = "https://smartag.console.aliyun.com/acl/cn-fake/acls/acl-fake"
        headers = [
            "",
            "实例名称",
            "类型",
            "授权策略",
            "协议类型",
            "应用",
            "规则方向",
            "源地址",
            "源端口范围",
            "目的地址",
            "目的端口范围",
            "优先级",
            "操作",
        ]
        cells = [
            "",
            "rule-name",
            "私网",
            "允许",
            "ALL",
            "-",
            "入方向",
            "0.0.0.0/0",
            "-1/-1",
            "0.0.0.0/0",
            "-1/-1",
            "1",
            "编辑\n移除",
        ]
    state = {"headers": headers, "tableVisible": True, "visibleLoading": False, "rows": [{"cells": cells}]}
    if change == "service":
        url = url.replace(".console", "4service.console")
    elif change == "wrong-host":
        url = url.replace("aliyun.com", "alibabacloud.com")
    elif change == "wrong-region":
        url = url.replace("cn-fake", "cn-other")
    elif change == "wrong-parent":
        ref["attributes"]["NamespaceId" if kind == "namespace" else "AclId"] = "other"
    elif change == "missing-attrs":
        ref["attributes"] = {}
    elif change == "duplicate":
        state["rows"] *= 2
    elif change in ("wrong-child", "substring"):
        index = 0 if kind == "namespace" else 1
        cells[index] = "other" if change == "wrong-child" else cells[index] + "-extra"
    elif change == "loading":
        state["visibleLoading"] = True
    elif change == "no-table":
        state["tableVisible"] = False
    elif change == "wrong-headers":
        state["headers"] = ["global-search"]
    elif change == "no-rows":
        state["rows"] = []
    elif change in ("query", "hash"):
        url += "?q=fake" if change == "query" else "#fake"
    elif change == "split-row":
        state["rows"] = (
            [{"cells": ["test-name"]}, {"cells": ["test"]}]
            if kind == "namespace"
            else [{"cells": cells[:1]}, {"cells": cells[1:]}]
        )
    module = (ROOT / "scripts/web/e2e/sae_sag_child_landing.mjs").as_uri()
    script = (
        f"import {{matchChildList}} from {json.dumps(module)};console.log(JSON.stringify(matchChildList("
        + ",".join(json.dumps(v) for v in (url, ref, state))
        + ")));"
    )
    result = subprocess.run(
        [node, "--input-type=module", "-e", script], check=True, capture_output=True, encoding="utf-8"
    )
    assert json.loads(result.stdout)["matched"] is matched


def test_driver_and_schema_track_narrow_detector():
    driver = (ROOT / "scripts/web/e2e/resource_link_chrome_batch.mjs").read_text(encoding="utf-8")
    runner = (ROOT / "scripts/web/e2e/run_resource_link_acceptance.py").read_text(encoding="utf-8")
    schema = json.loads((ROOT / "scripts/web/e2e/resource_link_browser_batch.schema.json").read_text(encoding="utf-8"))
    assert "!childListKind(reference)" in driver
    assert "childList ? Boolean(childList.matched)" in driver
    assert 'with_name("sae_sag_child_landing.mjs")' in runner
    assert "childList" in schema["properties"]["items"]["items"]["properties"]["diagnostics"]["properties"]
