"""Offline Table panel readiness never accepts a shell or permission dialog."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest


def _node(script):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node unavailable")
    module = Path(__file__).parents[2] / "scripts/web/e2e/ots_table_landing.mjs"
    result = subprocess.run(
        [node, "--input-type=module", "-e", script.replace("MODULE", json.dumps(module.as_uri()))],
        check=True,
        capture_output=True,
        encoding="utf-8",
    )
    return json.loads(result.stdout)


@pytest.mark.parametrize(
    "change,ready",
    [
        ({}, True),
        ({"prefix": "Loading...\n产品与服务\n"}, True),
        ({"name": "other-table"}, False),
        ({"name": "fake-table-extra"}, False),
        ({"type": "ALIYUN::OTS::Instance"}, False),
        ({"omit": "数据生命周期TTL：永久"}, False),
        ({"omit": "主键"}, False),
        ({"omit": "查询数据"}, False),
        ({"omit": "插入数据"}, False),
        ({"omit": "基本详情"}, False),
        ({"suffix": "OTSNoPermissionAccess"}, False),
        ({"suffix": "You have no permission to access the requested resource"}, False),
        ({"suffix": "请登录"}, False),
        ({"suffix": "加载中"}, False),
        ({"text": "Tablestore fake-table 表管理 数据管理 Loading..."}, False),
    ],
)
def test_exact_table_panel(change, ready):
    script = """
    import {otsTableContentReady} from MODULE;
    const c=CHANGE,ref={resourceType:c.type||'ALIYUN::OTS::Table',identifier:'fake-table'};
    const text=c.text||((c.prefix||'')+(c.name||'fake-table')+
      '\\n数据生命周期TTL：永久\\n基本详情\\n数据管理\\n插入数据\\n查询数据\\nkey(主键)'+(c.suffix||''));
    console.log(JSON.stringify({ready:otsTableContentReady(text.replace(c.omit||'never-present',''),ref)}));
    """.replace("CHANGE", json.dumps(change))
    assert _node(script)["ready"] is ready


@pytest.mark.parametrize(
    "kind,ready",
    [
        ("outside-loader", True),
        ("inside-loader", False),
        ("overlay", False),
        ("error-dialog", False),
        ("no-panel", False),
        ("duplicate-panel", False),
    ],
)
def test_panel_loader_and_error_scope(kind, ready):
    script = """
    import {otsTableSurfaceState} from MODULE;
    const kind=KIND,rect={left:300,right:800,top:100,bottom:500,width:500,height:400};
    const make=(text,area=rect)=>({innerText:text,children:[],parentElement:null,
      getBoundingClientRect:()=>area,querySelectorAll:()=>[{}],
      style:{visibility:'visible',display:'block',opacity:'1'}});
    const body=make('console'),heading=make('fake-table');
    const panel=make('fake-table\\n数据生命周期TTL：永久\\n基本详情\\n数据管理\\n插入数据\\n查询数据\\nkey(主键)');
    heading.parentElement=panel;panel.parentElement=body;panel.children=[heading];
    const other=make(panel.innerText),heading2=make('fake-table');
    other.parentElement=body;other.children=[heading2];heading2.parentElement=other;
    const outside={left:0,right:100,top:0,bottom:20,width:100,height:20};
    const spinner=make('Loading...',kind==='outside-loader'?outside:rect);
    spinner.parentElement=body;
    if(kind==='overlay')spinner.getBoundingClientRect=()=>({left:0,right:1000,top:0,bottom:800,width:1000,height:800});
    const dialog=make('错误提示 OTSNoPermissionAccess');dialog.parentElement=body;
    body.querySelectorAll=selector=>selector==='*'?(kind==='no-panel'?[]:
      kind==='duplicate-panel'?[heading,panel,heading2,other]:[heading,panel]):
      selector.includes('dialog')?(kind==='error-dialog'?[dialog]:[]):
      ['inside-loader','outside-loader','overlay'].includes(kind)?[spinner]:[];
    globalThis.getComputedStyle=n=>n.style;
    const surface={locator:()=>({evaluate:async(fn,id)=>fn(body,id)})};
    const result=await otsTableSurfaceState(surface,{resourceType:'ALIYUN::OTS::Table',identifier:'fake-table'});
    console.log(JSON.stringify({ready:result.contentReady}));
    """.replace("KIND", json.dumps(kind))
    assert _node(script)["ready"] is ready
