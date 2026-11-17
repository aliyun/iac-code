"""Offline account-child matching never accepts a cluster shell/global search."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    "change,matched",
    [
        ({}, True),
        ({"host": "polardb4service.console.aliyun.com"}, True),
        ({"host": "attacker.example"}, False),
        ({"parent": "other"}, False),
        ({"region": "cn-beijing"}, False),
        ({"rows": []}, False),
        ({"rows": [["rostest", "可用"], ["rostest", "可用"]]}, False),
        ({"rows": [["产品搜索rostest", "可用", "-", "高权限账号"]]}, False),
        ({"rows": [["other", "可用", "-", "高权限账号"]]}, False),
        ({"missingParent": True}, False),
    ],
)
def test_exact_polardb_child(change, matched):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node unavailable")
    module = Path(__file__).parents[2] / "scripts/web/e2e/polardb_account_landing.mjs"
    script = """
    import {matchPolardbAccount,polardbAccountContentReady} from MODULE;
    const c=CHANGE, ref={resourceType:'ALIYUN::POLARDB::Account',identifier:'rostest',regionId:'cn-hangzhou',
      attributes:c.missingParent?{}:{DBClusterId:'cluster-fake'}};
    const url='https://'+(c.host||'polardb.console.aliyun.com')+'/'+(c.region||'cn-hangzhou')+
      '/cluster/'+(c.parent||'cluster-fake')+'/account/user';
    const v=matchPolardbAccount(url,ref,c.rows||[['rostest','可用','-','高权限账号']]);
    if(polardbAccountContentReady('基本信息 集群cluster-fake 产品搜索rostest'))throw Error('shell ready');
    if(!polardbAccountContentReady('用户账号 账号名 状态 类型'))throw Error('table not ready');
    console.log(JSON.stringify(v));
    """.replace("MODULE", json.dumps(module.as_uri())).replace("CHANGE", json.dumps(change))
    result = subprocess.run(
        [node, "--input-type=module", "-e", script], check=True, capture_output=True, encoding="utf-8"
    )
    assert json.loads(result.stdout)["matched"] is matched


def test_polardb_diagnostics_schema_accepts_only_readonly_rows():
    from jsonschema import Draft202012Validator

    root = Path(__file__).parents[2]
    schema = json.loads((root / "scripts/web/e2e/resource_link_browser_batch.schema.json").read_text(encoding="utf-8"))
    diag_schema = schema["properties"]["items"]["items"]["properties"]["diagnostics"]["properties"]["polardbAccount"]
    value = {
        "matched": True,
        "parentRouteMatched": True,
        "rowIndex": 0,
        "rows": [["fake", "可用", "-", "高权限账号"]],
        "readOnly": True,
        "searchFallbackSuppressed": True,
    }
    validator = Draft202012Validator(diag_schema)
    assert not list(validator.iter_errors(value))
    assert list(validator.iter_errors({**value, "readOnly": False}))
