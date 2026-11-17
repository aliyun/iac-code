"""Offline exact read-only VBR child lookup; no cloud, browser or user state."""

import json
import shutil
import subprocess
from pathlib import Path

import jsonschema
import pytest

ROOT = Path(__file__).parents[2]
REF = {
    "resourceType": "ALIYUN::VPC::RouterInterface",
    "identifier": "ri-fake",
    "regionId": "cn-fake",
    "attributes": {"RouterId": "vbr-fake", "RouterType": "VBR"},
}
URL = "https://expressconnect.console.aliyun.com/vbr/cn-fake/detail/vbr-fake"


def node_eval(body, data):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node unavailable")
    module = (ROOT / "scripts/web/e2e/router_interface_landing.mjs").as_uri()
    script = (
        "import * as m from "
        + json.dumps(module)
        + ";const ref="
        + json.dumps(REF)
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
        (URL.replace(".console", "4service.console") + "?RouterId=vbr-fake&TabKey=vbr2vpc", True),
        (URL + "?TabKey=vbr2vpc", True),
        (URL + "?RouterId=vbr-other", False),
        (URL + "?TabKey=ports", False),
        (URL + "?TabKey=vbr2vpc&TabKey=vbr2vpc", False),
        (URL + "?RouterId=vbr-fake&RouterId=vbr-fake", False),
        (URL + "?unreviewed=true", False),
        (URL + "#ri-fake", False),
        (URL.replace("cn-fake", "cn-other"), False),
        (URL.replace("/vbr-fake", "/vbr-other"), False),
        (URL.replace("aliyun.com", "alibabacloud.com"), False),
        (URL.replace("aliyun.com", "aliyun.com.evil.invalid"), False),
        (URL.replace("https:", "http:"), False),
    ],
)
def test_exact_parent_route(url, matched):
    assert node_eval("console.log(JSON.stringify(m.vbrParentRouteMatches(data,ref)));", url) is matched


@pytest.mark.parametrize(
    "cells,selected,loading,matched",
    [
        ([["vbr-fake\nri-fake\nname", "opposite-vpc"]], True, False, True),
        ([["vbr-fake\nri-fake-extra"]], True, False, False),
        ([["vbr-other\nri-fake"]], True, False, False),
        ([["vbr-fake", "ri-fake"]], True, False, False),
        ([["vbr-fake"], ["ri-fake"]], True, False, False),
        ([["vbr-fake\nri-fake"], ["vbr-fake\nri-fake"]], True, False, False),
        ([["vbr-fake\nri-fake"]], False, False, False),
        ([["vbr-fake\nri-fake"]], True, True, False),
        ([], True, False, False),
    ],
)
def test_same_visible_child_cell(cells, selected, loading, matched):
    state = {"rows": [{"cells": row} for row in cells], "tabSelected": selected, "visibleLoading": loading}
    assert node_eval("console.log(JSON.stringify(m.routerInterfaceRowsMatch(data,ref)));", state) is matched


@pytest.mark.parametrize(
    "kind,matched",
    [
        ("ready", True),
        ("outside-spinner", True),
        ("overlay", False),
        ("hidden-row", False),
        ("other-parent", False),
        ("no-tab", False),
        ("click-error", False),
    ],
)
def test_read_only_tab_flow(kind, matched):
    # Small fake DOM exercises the production evaluator and ancestor visibility,
    # not a test-only replacement of its returned state.
    script = """
    let selected=false,clicks=0;
    const rect={left:100,right:500,top:100,bottom:200,width:400,height:100};
    const make=(text,area=rect)=>({innerText:text,parentElement:null,
      style:{visibility:'visible',display:'block',opacity:'1'},
      getBoundingClientRect:()=>area});
    const body=make('shell'),row=make('row'),cell=make('vbr-fake\\nri-fake');
    row.parentElement=body;cell.parentElement=row;
    row.querySelectorAll=()=>[cell];
    if(data==='hidden-row')body.style.display='none';
    const tab=make('对等连接');tab.getAttribute=()=>String(selected);
    const spinner=make('Loading...',data==='outside-spinner'
      ?{left:0,right:50,top:0,bottom:50,width:50,height:50}:rect);
    body.querySelectorAll=s=>s==='[role="tab"]'?[tab]:s==='tbody tr'?[row]:
      ['overlay','outside-spinner'].includes(data)?[spinner]:[];
    globalThis.getComputedStyle=n=>n.style;
    const url='https://expressconnect.console.aliyun.com/vbr/cn-fake/detail/vbr-fake';
    const surface={url:()=>data==='other-parent'?url.replace('vbr-fake','vbr-other'):url,
      getByRole:(role,opts)=>{
        if(role!=='tab'||opts.name!=='对等连接'||!opts.exact)throw Error('unapproved control');
        return {count:async()=>data==='no-tab'?0:1,isVisible:async()=>true,
          getAttribute:async()=>String(selected),
          click:async()=>{clicks++;if(data==='click-error')throw Error('unavailable');selected=true;}};
      },
      locator:()=>({evaluate:async f=>f(body),innerText:async()=>cell.innerText})};
    const result=await m.routerInterfaceLanding(surface,url,ref,Date.now()+450);
    console.log(JSON.stringify({...result,clicks}));
    """
    result = node_eval(script, kind)
    assert result["matched"] is matched
    assert result["clicks"] <= 1
    assert result["searchFallbackSuppressed"] is True
    if matched:
        assert result["tabSelected"] is True
        assert result["rows"] == [{"cells": ["vbr-fake\nri-fake"]}]
    schema = json.loads((ROOT / "scripts/web/e2e/resource_link_browser_batch.schema.json").read_text(encoding="utf-8"))
    result.pop("clicks")
    jsonschema.validate(result, schema["$defs"]["routerInterfaceDiagnostics"])


def test_driver_suppresses_global_search_and_digest_tracks_module():
    driver = (ROOT / "scripts/web/e2e/resource_link_chrome_batch.mjs").read_text(encoding="utf-8")
    runner = (ROOT / "scripts/web/e2e/run_resource_link_acceptance.py").read_text(encoding="utf-8")
    assert "!isVbrRouterInterface(reference)" in driver
    assert "routerInterface ? Boolean(routerInterface.matched)" in driver
    assert 'with_name("router_interface_landing.mjs")' in runner


@pytest.mark.parametrize("mutation", ["extra", "search", "missing", "row", "selected"])
def test_diagnostic_schema_fail_closed(mutation):
    schema = json.loads((ROOT / "scripts/web/e2e/resource_link_browser_batch.schema.json").read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator.check_schema(schema)
    diagnostic = {
        "matched": True,
        "parentRouteMatched": True,
        "searchFallbackSuppressed": True,
        "tabClicked": True,
        "tabSelected": True,
        "visibleLoading": False,
        "rows": [{"cells": ["vbr-fake\nri-fake"]}],
        "finalUrl": URL,
        "bodyTextAfter": "detail",
    }
    if mutation == "extra":
        diagnostic["passOverride"] = True
    elif mutation == "search":
        diagnostic["searchFallbackSuppressed"] = False
    elif mutation == "missing":
        del diagnostic["rows"]
    elif mutation == "row":
        diagnostic["rows"][0]["fake"] = True
    else:
        diagnostic["tabSelected"] = "true"
    validator = jsonschema.Draft202012Validator(schema["$defs"]["routerInterfaceDiagnostics"])
    assert list(validator.iter_errors(diagnostic))
