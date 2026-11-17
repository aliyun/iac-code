"""Offline exact loaded-card readiness; console shell text is not evidence."""

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
        ({"name": "other-instance"}, False),
        ({"name": "fake-instance-extra"}, False),
        ({"type": "ALIYUN::OTS::Table"}, False),
        ({"date": "加载中"}, False),
        ({"omit": "存储规格：容量无限制"}, False),
        ({"omit": "冗余类型：同城冗余"}, False),
        ({"omit": "实例模型：CU模式"}, False),
        ({"omit": "CU模式"}, False),
        ({"omit": "容量无限制"}, False),
        ({"omit": "同城冗余"}, False),
        ({"name": "fake-\ninstance"}, True),
        ({"text": "Tablestore 实例管理 fake-instance 实例详情 网络管理 Loading..."}, False),
        ({"text": "登录 阿里云账号 fake-instance"}, False),
    ],
)
def test_ots_exact_loaded_card(change, ready):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node unavailable")
    module = Path(__file__).parents[2] / "scripts/web/e2e/ots_instance_landing.mjs"
    script = """
    import {otsInstanceContentReady,otsInstanceSurfaceState} from MODULE;
    const c=CHANGE, ref={resourceType:c.type||'ALIYUN::OTS::Instance',identifier:'fake-instance'};
    const text=c.text||((c.prefix||'')+'实例基础信息\\n实例名称：'+(c.name||'fake-instance')+
      '\\n实例别名：\\n实例模型：CU模式\\n创建时间：'+(c.date||'2025年11月5日 14:44:08')+
      '\\n存储规格：容量无限制\\n冗余类型：同城冗余');
    const cleaned=text.replace(c.omit||'never-present','');
    console.log(JSON.stringify({ready:otsInstanceContentReady(cleaned,ref)}));
    for(const state of [{text:cleaned,scoped:false,visibleLoading:false},
      {text:cleaned,scoped:true,visibleLoading:true}]){
      const surface={locator:()=>({evaluate:async()=>state})};
      if((await otsInstanceSurfaceState(surface,ref)).contentReady)throw Error('shell or loader accepted');
    }
    """.replace("MODULE", json.dumps(module.as_uri())).replace("CHANGE", json.dumps(change))
    result = subprocess.run(
        [node, "--input-type=module", "-e", script], check=True, capture_output=True, encoding="utf-8"
    )
    assert json.loads(result.stdout)["ready"] is ready


@pytest.mark.parametrize(
    "loader,ready",
    [("outside", True), ("inside", False), ("transparent", True), ("blocking-overlay", False), ("no-card", False)],
)
def test_ots_card_scope_keeps_visible_loading_blockers(loader, ready):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node unavailable")
    module = Path(__file__).parents[2] / "scripts/web/e2e/ots_instance_landing.mjs"
    script = """
    import {otsInstanceSurfaceState} from MODULE;
    const kind=KIND, rect={left:300,right:800,top:100,bottom:500,width:500,height:400};
    const make=(text,area=rect)=>({innerText:text,children:[],parentElement:null,
      getBoundingClientRect:()=>area,style:{visibility:'visible',display:'block',opacity:'1'}});
    const body=make('console Loading...'), heading=make('实例基础信息');
    const card=make('实例基础信息\\n实例名称：fake-instance\\n实例别名：\\n实例模型：CU模式'+
      '\\n创建时间：2025年11月5日 14:44:08\\n存储规格：容量无限制\\n冗余类型：同城冗余');
    card.children=[heading];card.parentElement=body;heading.parentElement=card;
    const spinner=make('Loading...',kind==='outside'?{left:0,right:100,top:0,bottom:20,width:100,height:20}:rect);
    spinner.parentElement=body;if(kind==='transparent')spinner.style.opacity='0';
    if(kind==='blocking-overlay')spinner.getBoundingClientRect=()=>({left:0,right:1000,top:0,bottom:800,width:1000,height:800});
    body.querySelectorAll=selector=>selector==='*'?(kind==='no-card'?[]:[heading,card]):[spinner];
    globalThis.getComputedStyle=node=>node.style;
    const surface={locator:()=>({evaluate:async fn=>fn(body)})};
    const ref={resourceType:'ALIYUN::OTS::Instance',identifier:'fake-instance'};
    const result=await otsInstanceSurfaceState(surface,ref);
    console.log(JSON.stringify(result));
    """.replace("MODULE", json.dumps(module.as_uri())).replace("KIND", json.dumps(loader))
    result = subprocess.run(
        [node, "--input-type=module", "-e", script], check=True, capture_output=True, encoding="utf-8"
    )
    assert json.loads(result.stdout)["contentReady"] is ready
