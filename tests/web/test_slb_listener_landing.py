"""Exact frontend tuple acceptance; offline fake DOM, no cloud or user state."""

import json
import shutil
import subprocess
from pathlib import Path

import jsonschema
import pytest

ROOT = Path(__file__).parents[2]
REF = {
    "resourceType": "alicloud_slb_listener",
    "identifier": "lb-fake:http:8080",
    "regionId": "cn-fake",
    "attributes": {"LoadBalancerId": "lb-fake", "ListenerPort": "8080", "ListenerProtocol": "http"},
}
URL = "https://slb.console.aliyun.com/slb/cn-fake/slbs/lb-fake/listeners"


def node_eval(body, data, reference=None):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node unavailable")
    module = (ROOT / "scripts/web/e2e/slb_listener_landing.mjs").as_uri()
    script = (
        "import * as m from "
        + json.dumps(module)
        + ";const ref="
        + json.dumps(reference or REF)
        + ",data="
        + json.dumps(data)
        + ";"
        + body
    )
    result = subprocess.run(
        [node, "--input-type=module", "-e", script], check=True, capture_output=True, encoding="utf-8"
    )
    return json.loads(result.stdout)


@pytest.mark.parametrize(
    "url,matched",
    [
        (URL, True),
        (URL.replace(".console", "4service.console"), True),
        (URL + "?Description=lb-fake%3Ahttp%3A8080", False),
        (URL + "#lb-fake:http:8080", False),
        (URL.replace("lb-fake", "lb-other"), False),
        (URL.replace("cn-fake", "cn-other"), False),
        (URL.replace("aliyun.com", "alibabacloud.com"), False),
        (URL.replace("aliyun.com", "aliyun.com.evil.invalid"), False),
        (URL.replace("https:", "http:"), False),
    ],
)
def test_exact_parent_route(url, matched):
    assert node_eval("console.log(JSON.stringify(m.slbListenerParentRouteMatches(data,ref)));", url) is matched


@pytest.mark.parametrize(
    "cell,header,loading,count",
    [
        ("HTTP:8080", "前端协议/端口", False, 1),
        ("HTTP\n8080", "前端协议/端口", False, 1),
        ("http / 8080", "前端协议 / 端口", False, 1),
        ("HTTP:80800", "前端协议/端口", False, 0),
        ("HTTPS:8080", "前端协议/端口", False, 0),
        ("HTTP:8080", "后端协议/端口", False, 0),
        ("HTTP:8080", "监听名称", False, 0),
        ("lb-fake:http:8080", "前端协议/端口", False, 0),
        ("没有数据", "前端协议/端口", False, 0),
        ("HTTP:8080", "前端协议/端口", True, 0),
    ],
)
def test_frontend_column_only(cell, header, loading, count):
    state = {"visibleLoading": loading, "rows": [{"headers": [header], "cells": [cell]}]}
    assert node_eval("console.log(JSON.stringify(m.slbListenerMatchingRows(data,ref).length));", state) == count


@pytest.mark.parametrize("mutation", ["parent", "protocol", "port", "large-port", "type", "region"])
def test_invalid_reference_is_fail_closed(mutation):
    ref = json.loads(json.dumps(REF))
    if mutation == "parent":
        ref["attributes"]["LoadBalancerId"] = "lb-other"
    elif mutation == "protocol":
        ref["attributes"]["ListenerProtocol"] = "udp"
    elif mutation == "port":
        ref["attributes"]["ListenerPort"] = "80"
    elif mutation == "large-port":
        ref["identifier"] = "lb-fake:http:65536"
    elif mutation == "type":
        ref["resourceType"] = "alicloud_slb_load_balancer"
    else:
        ref["regionId"] = ""
    assert node_eval("console.log(JSON.stringify(m.slbListenerIdentity(ref)));", None, ref) is None


@pytest.mark.parametrize(
    "kind,matched",
    [
        ("ready", True),
        ("outside-spinner", True),
        ("overlay", False),
        ("hidden-row", False),
        ("backend-only", False),
        ("duplicate", False),
        ("wrong-parent", False),
        ("query-echo", False),
        ("empty", False),
        ("header-cell-misaligned", False),
        ("art-split", True),
        ("unrelated-sibling-header", False),
    ],
)
def test_read_only_stable_table_flow(kind, matched):
    script = """
    const rect={left:100,right:500,top:100,bottom:200,width:400,height:100};
    const make=(text,area=rect)=>({innerText:text,parentElement:null,
      style:{visibility:'visible',display:'block',opacity:'1'},getBoundingClientRect:()=>area});
    const body=make('filter lb-fake:http:8080'),row=make('row'),cell=make('HTTP:8080');
    row.parentElement=body;cell.parentElement=row;
    if(data==='hidden-row')body.style.display='none';
    if(data==='empty')cell.innerText='没有数据';
    const table={querySelectorAll:()=>[make(data==='backend-only'?'后端协议/端口':'前端协议/端口')]};
    const emptyTable={querySelectorAll:()=>[]};
    row.closest=selector=>data==='art-split'
      ?(selector.includes('.art-table')?table:emptyTable)
      :data==='unrelated-sibling-header'?emptyTable:table;
    row.querySelectorAll=()=>data==='header-cell-misaligned'?[cell,make('HTTP:8080')]:[cell];
    const spinner=make('Loading...',data==='outside-spinner'
      ?{left:0,right:50,top:0,bottom:50,width:50,height:50}:rect);
    body.querySelectorAll=s=>s==='tbody tr'?(data==='duplicate'?[row,row]:[row]):
      ['overlay','outside-spinner'].includes(data)?[spinner]:[];
    globalThis.getComputedStyle=n=>n.style;
    const base='https://slb.console.aliyun.com/slb/cn-fake/slbs/lb-fake/listeners';
    const url=data==='wrong-parent'?base.replace('lb-fake','lb-other'):
      data==='query-echo'?base+'?Description=lb-fake%3Ahttp%3A8080':base;
    const surface={url:()=>url,locator:()=>({evaluate:async f=>f(body),innerText:async()=>cell.innerText})};
    const result=await m.slbListenerLanding(surface,url,ref,Date.now()+450);
    console.log(JSON.stringify(result));
    """
    result = node_eval(script, kind)
    assert result["matched"] is matched
    assert result["searchFallbackSuppressed"] is True
    schema = json.loads((ROOT / "scripts/web/e2e/resource_link_browser_batch.schema.json").read_text(encoding="utf-8"))
    jsonschema.validate(result, schema["$defs"]["slbListenerDiagnostics"])


def test_driver_and_revision_include_strict_listener_flow():
    driver = (ROOT / "scripts/web/e2e/resource_link_chrome_batch.mjs").read_text(encoding="utf-8")
    runner = (ROOT / "scripts/web/e2e/run_resource_link_acceptance.py").read_text(encoding="utf-8")
    assert "!isSlbListener(reference)" in driver
    assert "slbListener ? Boolean(slbListener.matched)" in driver
    assert 'with_name("slb_listener_landing.mjs")' in runner
