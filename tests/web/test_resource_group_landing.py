"""Offline exact Resource Manager detail readiness, never global shell readiness."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    "change,ready",
    [
        ({}, True),
        ({"prefix": "Loading...\n产品与服务\n"}, True),
        ({"id": "rg-other"}, False),
        ({"id": "rg-fake-extra"}, False),
        ({"name": "other group"}, False),
        ({"type": "ALIYUN::RAM::Group"}, False),
        ({"omit": "权限管理"}, False),
        ({"omit": "总量排序"}, False),
        ({"omit": "资源编排\n2395"}, False),
        ({"count": "加载中"}, False),
        ({"count": "0"}, True),
        ({"text": "资源组 rg-fake 默认资源组 概览 资源管理 权限管理 设置 总量排序"}, False),
        ({"text": "登录 阿里云账号 rg-fake"}, False),
    ],
)
def test_group_loaded_detail(change, ready):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node unavailable")
    module = Path(__file__).parents[2] / "scripts/web/e2e/resource_group_landing.mjs"
    script = """
    import {resourceGroupContentReady} from MODULE;
    const c=CHANGE, ref={resourceType:c.type||'ALIYUN::ResourceManager::ResourceGroup',
      identifier:'rg-fake',attributes:{DisplayName:'默认资源组'}};
    const text=c.text||((c.prefix||'')+'资源管理/资源组/'+(c.id||'rg-fake')+
      '\\n'+(c.name||'默认资源组')+'\\n概览\\n资源管理\\n权限管理\\n设置\\n总量排序\\n资源编排\\n'+(c.count||'2395'));
    console.log(JSON.stringify({ready:resourceGroupContentReady(text.replace(c.omit||'never-present',''),ref)}));
    """.replace("MODULE", json.dumps(module.as_uri())).replace("CHANGE", json.dumps(change))
    result = subprocess.run(
        [node, "--input-type=module", "-e", script], check=True, capture_output=True, encoding="utf-8"
    )
    assert json.loads(result.stdout)["ready"] is ready


@pytest.mark.parametrize(
    "loader,ready",
    [
        ("outside", True),
        ("inside", False),
        ("transparent", True),
        ("blocking-overlay", False),
        ("no-card", False),
        ("hidden-parent", False),
    ],
)
def test_group_scoped_loading(loader, ready):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node unavailable")
    module = Path(__file__).parents[2] / "scripts/web/e2e/resource_group_landing.mjs"
    script = """
    import {resourceGroupSurfaceState} from MODULE;
    const kind=KIND, rect={left:300,right:800,top:100,bottom:500,width:500,height:400};
    const make=(text,area=rect)=>({innerText:text,children:[],parentElement:null,tagName:'DIV',
      getAttribute:()=>null,getBoundingClientRect:()=>area,style:{visibility:'visible',display:'block',opacity:'1'}});
    const body=make('console Loading...'), heading=make('默认资源组');
    const card=make('资源管理/资源组/rg-fake\\n默认资源组\\n概览\\n资源管理\\n权限管理'+
      '\\n设置\\n总量排序\\n资源编排\\n2395');
    card.children=[heading];card.parentElement=body;heading.parentElement=card;
    if(kind==='hidden-parent')card.style.display='none';
    const spinner=make('Loading...',kind==='outside'?{left:0,right:100,top:0,bottom:20,width:100,height:20}:rect);
    spinner.parentElement=body;if(kind==='transparent')spinner.style.opacity='0';
    if(kind==='blocking-overlay')spinner.getBoundingClientRect=()=>({left:0,right:1000,top:0,bottom:800,width:1000,height:800});
    body.querySelectorAll=selector=>selector==='*'?(kind==='no-card'?[]:[heading,card]):[spinner];
    globalThis.getComputedStyle=n=>n.style;
    const surface={locator:()=>({evaluate:async(fn,ref)=>fn(body,ref)})};
    console.log(JSON.stringify(await resourceGroupSurfaceState(surface,{
      resourceType:'ALIYUN::ResourceManager::ResourceGroup',identifier:'rg-fake',attributes:{DisplayName:'默认资源组'}})));
    """.replace("MODULE", json.dumps(module.as_uri())).replace("KIND", json.dumps(loader))
    result = subprocess.run(
        [node, "--input-type=module", "-e", script], check=True, capture_output=True, encoding="utf-8"
    )
    assert json.loads(result.stdout)["contentReady"] is ready
