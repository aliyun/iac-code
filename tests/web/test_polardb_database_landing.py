"""Offline strict PolarDB readiness and exact child rows."""

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
        ({"host": "evil.example"}, False),
        ({"parent": "other"}, False),
        ({"region": "cn-beijing"}, False),
        ({"tail": "?search=db-fake"}, False),
        ({"rows": []}, False),
        ({"rows": [["global search db-fake", "运行中", "UTF8", "owner", "SQL查询"]]}, False),
        ({"rows": [["db-fake", "加载中", "UTF8", "owner", "SQL查询"]]}, False),
        ({"rows": [["db-fake", "运行中", "", "owner", "SQL查询"]]}, False),
        ({"duplicate": True}, False),
        ({"missingParent": True}, False),
    ],
)
def test_database_row_and_cluster_shell(change, matched):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node unavailable")
    module = Path(__file__).parents[2] / "scripts/web/e2e/polardb_database_landing.mjs"
    script = """
    import {matchPolardbDatabase,polardbClusterContentReady} from MODULE;
    const c=CHANGE, ref={resourceType:'alicloud_polardb_database',identifier:'db-fake',regionId:'cn-hangzhou',
      attributes:c.missingParent?{}:{DBClusterId:'pc-fake'}};
    let rows=c.rows||[['db-fake','运行中','UTF8','owner','SQL查询']];if(c.duplicate)rows=[...rows,...rows];
    const url='https://'+(c.host||'polardb.console.aliyun.com')+'/'+(c.region||'cn-hangzhou')+
      '/cluster/'+(c.parent||'pc-fake')+'/database/list'+(c.tail||'');
    const cluster={resourceType:'ALIYUN::POLARDB::DBCluster',identifier:'pc-fake',regionId:'cn-hangzhou'};
    const loaded='基本信息\\n集群ID\\npc-fake\\n地域\\n杭州\\n创建时间\\n2026-01-01\\n兼容性\\nPostgreSQL\\n配置信息';
    if(!polardbClusterContentReady(loaded,cluster))throw Error('loaded rejected');
    for(const date of ['2026年6月22日 16:58:49','2026-06-22 16:58:49','2026年6月22日']){
      if(!polardbClusterContentReady(loaded.replace('2026-01-01',date),cluster))throw Error('date rejected');
    }
    for(const date of ['2026年13月22日','2026年2月30日','2026-02-30','-']){
      if(polardbClusterContentReady(loaded.replace('2026-01-01',date),cluster))throw Error('invalid date accepted');
    }
    if(polardbClusterContentReady(loaded.replace('创建时间','维护日期'),cluster))throw Error('unbound date accepted');
    if(!polardbClusterContentReady(loaded.replace('杭州','上海'),{...cluster,regionId:'cn-shanghai'}))
      throw Error('other covered region rejected');
    if(polardbClusterContentReady(loaded,{...cluster,regionId:'cn-shanghai'}))throw Error('wrong region accepted');
    for(const text of ['pc-fake\\n基本信息\\n集群ID\\n地域\\n兼容性\\n-',loaded.replace('PostgreSQL','-'),
      loaded.replace('2026-01-01','-'),loaded.replace('pc-fake','pc-other')]){
      if(polardbClusterContentReady(text,cluster))throw Error('empty/wrong detail accepted');
    }
    console.log(JSON.stringify(matchPolardbDatabase(url,ref,rows)));
    """.replace("MODULE", json.dumps(module.as_uri())).replace("CHANGE", json.dumps(change))
    result = subprocess.run(
        [node, "--input-type=module", "-e", script], check=True, capture_output=True, encoding="utf-8"
    )
    assert json.loads(result.stdout)["matched"] is matched
