"""Offline NAS mount detector: exact visible child and exact parent, no cloud."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    "change,matched",
    [
        ({}, True),
        ({"host": "nasnext4service.console.aliyun.com"}, True),
        ({"domain": "other.cn-hangzhou.nas.aliyuncs.com"}, False),
        ({"parent": "other-fs"}, False),
        ({"region": "cn-beijing"}, False),
        ({"host": "attacker.example"}, False),
        ({"duplicates": True}, False),
        ({"rows": []}, False),
        ({"hash": "#wrong"}, False),
        ({"missingParent": True}, False),
    ],
)
def test_exact_mount_match(change, matched):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is unavailable")
    module = Path(__file__).parents[2] / "scripts/web/e2e/nas_mount_target_landing.mjs"
    script = """
      import { matchNasMountTarget } from MODULE;
      const c = CHANGE;
      const ref = {resourceType:'ALIYUN::NAS::MountTarget',identifier:'mount.cn-hangzhou.nas.aliyuncs.com',
        regionId:'cn-hangzhou',attributes:c.missingParent ? {} : {FileSystemId:'fs-a'}};
      const expected='https://nasnext.console.aliyun.com/cn-hangzhou/filesystem/fs-a/mount';
      const actual='https://'+(c.host||'nasnext.console.aliyun.com')+'/'+(c.region||'cn-hangzhou')+
        '/filesystem/'+(c.parent||'fs-a')+'/mount'+(c.hash||'');
      const row={cells:[{values:[c.domain||ref.identifier]}]};
      const rows=c.rows|| (c.duplicates ? [row,row] : [row]);
      process.stdout.write(JSON.stringify(matchNasMountTarget(actual,expected,ref,rows)));
    """.replace("MODULE", json.dumps(module.as_uri())).replace("CHANGE", json.dumps(change))
    result = subprocess.run(
        [node, "--input-type=module", "-e", script], check=True, capture_output=True, encoding="utf-8"
    )
    assert json.loads(result.stdout)["matched"] is matched
