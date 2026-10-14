"""Offline endpoint cards: a parent heading/global search never proves a child."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[2]
MODULE = ROOT / "scripts/web/e2e/polardb_endpoint_landing.mjs"


def run_node(script):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node unavailable")
    return subprocess.run(
        [node, "--input-type=module", "-e", script.replace("MODULE", json.dumps(MODULE.as_uri()))],
        check=True,
        capture_output=True,
        encoding="utf-8",
    )


@pytest.mark.parametrize("mode", ["panel", "unlabelled_id", "hidden", "offscreen", "transparent", "large", "bounded"])
def test_hover_diagnostics_find_non_tooltip_panel_without_crediting_identity(mode):
    script = """
    import {collectConnectionHoverEvidence, matchPolardbEndpoint} from MODULE;
    const mode=MODE;
    const doc={defaultView:{innerWidth:1000,innerHeight:800}};
    globalThis.getComputedStyle=n=>({display:'block',visibility:n.hidden?'hidden':'visible',
      opacity:n.transparent?'0':'1'});
    function node(text,cls,parent=null){
      const n={tagName:'DIV',innerText:text,parentElement:parent,ownerDocument:doc,
        getAttribute:k=>k==='class'?cls:null,
        getBoundingClientRect:()=>({x:20,y:30,left:20,top:30,right:220,bottom:130,width:200,height:100}),
        contains:other=>{for(let p=other.parentElement;p;p=p.parentElement)if(p===n)return true;return false;}};
      return n;
    }
    const body=node('网络信息 '+ 'x'.repeat(4000),'page');body.tagName='BODY';
    const text=mode==='unlabelled_id'?'pe-fake12345678':'网络信息 私网 地址: cluster.pg.polardb.rds.aliyuncs.com:5432';
    const panel=node(text,'polardb-floating-config',body),leaf=node(text,null,panel);
    if(mode==='hidden')panel.hidden=leaf.hidden=true;
    if(mode==='transparent')panel.transparent=leaf.transparent=true;
    if(mode==='offscreen')for(const n of [panel,leaf])n.getBoundingClientRect=()=>
      ({x:20,y:-200,left:20,top:-200,right:220,bottom:-100,width:200,height:100});
    if(mode==='large')panel.innerText=leaf.innerText=text+'x'.repeat(4000);
    const nodes=mode==='bounded'?
      [...Array.from({length:4096},()=>node('irrelevant',null)),panel,leaf]:[body,panel,leaf];
    const result=collectConnectionHoverEvidence(nodes);
    const ref={resourceType:'alicloud_polardb_endpoint',identifier:'pe-fake12345678',regionId:'cn-hangzhou',
      attributes:{DBClusterId:'pc-fake'}};
    const verdict=matchPolardbEndpoint('https://polardb.console.aliyun.com/cn-hangzhou/cluster/pc-fake/baseInfo',
      ref,result.samples);
    console.log(JSON.stringify({result,verdict}));
    """.replace("MODE", json.dumps(mode))
    output = json.loads(run_node(script).stdout)
    result = output["result"]
    assert result["diagnosticOnly"] is True
    assert result["scanned"] <= 4096
    assert len(result["samples"]) <= 8
    assert output["verdict"]["matched"] is False
    if mode in {"panel", "unlabelled_id"}:
        assert any(sample["class"] == "polardb-floating-config" for sample in result["samples"])
        assert all(sample["tag"] != "BODY" for sample in result["samples"])
    else:
        assert result["samples"] == []
    assert result["scanTruncated"] is (mode == "bounded")


@pytest.mark.parametrize("occluded", [False, True])
def test_canvas_probe_reports_overlay_interception_without_an_identity_verdict(occluded):
    script = """
    import {connectionCanvasProbeHit} from MODULE;
    let point;
    const overlay={tagName:'DIV',innerText:'网络信息',getAttribute:()=> 'floating-panel'};
    const canvas={tagName:'CANVAS',innerText:'',getAttribute:()=>null,contains:()=>false,
      getBoundingClientRect:()=>({left:50,top:100}),
      ownerDocument:{elementFromPoint:(x,y)=>{point={x,y};return OCCLUDED?overlay:canvas;}}};
    console.log(JSON.stringify({point:(connectionCanvasProbeHit(canvas,{x:170,y:144}),point),
      result:connectionCanvasProbeHit(canvas,{x:170,y:144})}));
    """.replace("OCCLUDED", json.dumps(occluded))
    output = json.loads(run_node(script).stdout)
    assert output["point"] == {"x": 220, "y": 244}
    assert output["result"]["canvasHit"] is not occluded
    assert output["result"]["diagnosticOnly"] is True
    assert "matched" not in output["result"]


@pytest.mark.parametrize(
    "change,matched",
    [
        ({}, True),
        ({"type": "ALIYUN::POLARDB::DBClusterEndpointAddress"}, True),
        ({"type": "alicloud_polardb_endpoint", "identifier": "pe-fake"}, True),
        ({"type": "ALIYUN::POLARDB::DBClusterEndpoint", "identifier": "pe-fake"}, True),
        ({"host": "polardb4service.console.aliyun.com"}, True),
        ({"host": "evil.example"}, False),
        ({"parent": "pc-other"}, False),
        ({"region": "cn-beijing"}, False),
        ({"tail": "?search=pe-fake"}, False),
        ({"tail": "#pe-fake"}, False),
        ({"port": ":444"}, False),
        ({"credentials": "fake@"}, False),
        ({"missingParent": True}, False),
        ({"missingEndpoint": True}, False),
        ({"hidden": True}, False),
        ({"unscoped": True}, False),
        ({"duplicate": True}, False),
        ({"text": "基本信息 pc-fake 运行中 创建时间 2026-01-01"}, False),
        ({"text": "Endpoint ID: pe-fake-long 主地址: cluster.pg.example.com 端口: 5432"}, False),
        ({"text": "Endpoint ID: pe-other 主地址: cluster.pg.example.com 端口: 5432"}, False),
        ({"text": "Endpoint ID: pe-fake 主地址: other.pg.example.com 端口: 5432"}, False),
        ({"text": "Endpoint ID: pe-fake 主地址: cluster.pg.example.com-long 端口: 5432"}, False),
        ({"text": "Endpoint ID: pe-fake 主地址: cluster.pg.example.com 端口: -"}, False),
        ({"text": "EndpointId: pe-fake 地址: cluster.pg.example.com:5432"}, True),
        ({"text": "EndpointId: pe-fake 地址: cluster.pg.example.com-long:5432"}, False),
        ({"text": "EndpointId: pe-other 地址: cluster.pg.example.com:5432"}, False),
        ({"text": "地址: cluster.pg.example.com:5432"}, False),
        ({"splitCards": True}, False),
        ({"type": "alicloud_polardb_cluster"}, False),
    ],
)
def test_exact_scoped_child_card(change, matched):
    script = """
    import {matchPolardbEndpoint} from MODULE;
    const c=CHANGE;
    const ref={resourceType:c.type||'alicloud_polardb_endpoint_address',
      identifier:c.identifier||'cluster.pg.example.com',regionId:'cn-hangzhou',
      attributes:{...(c.missingParent?{}:{DBClusterId:'pc-fake'}),
        ...(c.missingEndpoint?{}:{DBEndpointId:'pe-fake'})}};
    let cards=[{visible:!c.hidden,connectionScoped:!c.unscoped,
      text:c.text||'数据库连接\\nEndpoint ID: pe-fake\\n主地址: cluster.pg.example.com\\n端口: 5432'}];
    if(c.splitCards)cards=[{visible:true,connectionScoped:true,text:'Endpoint ID: pe-fake'},
      {visible:true,connectionScoped:true,text:'主地址: cluster.pg.example.com 端口: 5432'}];
    if(c.duplicate)cards=[...cards,...cards];
    const url='https://'+(c.credentials||'')+(c.host||'polardb.console.aliyun.com')+(c.port||'')+
      '/'+(c.region||'cn-hangzhou')+'/cluster/'+(c.parent||'pc-fake')+'/baseInfo'+(c.tail||'');
    console.log(JSON.stringify(matchPolardbEndpoint(url,ref,cards)));
    """.replace("CHANGE", json.dumps(change))
    assert json.loads(run_node(script).stdout)["matched"] is matched


@pytest.mark.parametrize(
    "mode",
    [
        "hover",
        "already",
        "wrong_route",
        "ambiguous",
        "hidden",
        "read_error",
        "expired",
        "lazy_section",
        "recommended",
        "parent_only",
        "late_graph",
    ],
)
def test_read_only_bounded_hover(mode):
    script = """
    import {polardbEndpointLanding} from MODULE;
    const mode=MODE, actions=[];
    const ref={resourceType:'alicloud_polardb_endpoint_address',identifier:'cluster.pg.example.com',
      regionId:'cn-hangzhou',attributes:{DBClusterId:'pc-fake',DBEndpointId:'pe-fake'}};
    let hovered=mode==='already';
    let scrolled=false;
    const surface={locator:()=>({innerText:async()=>"数据库连接 parent body without exact child",
      evaluateAll:async()=>{
      if(mode==='read_error')throw Error('read failed');
      return hovered?[{visible:true,connectionScoped:true,
        text:'Endpoint ID: pe-fake 主地址: cluster.pg.example.com 端口: 5432'}]:[];
    }}),getByText:(label)=>({
      waitFor:async()=>{
        const primary=String(label).includes('主地址');
        if(primary&&mode==='late_graph'){actions.push('graph-ready');hovered=true;return;}
        if(mode!=='lazy_section')throw Error('section missing');
      },
      count:async()=>{
        const section=typeof label!=='string'&&String(label).includes('数据库连接');
        if(section)return mode==='lazy_section'?1:0;
        const recommended=typeof label!=='string'&&String(label).includes('推荐');
        if(mode==='parent_only')return 0;
        if(mode==='recommended')return recommended?1:0;
        if(mode==='lazy_section'&&!scrolled)return 0;
        return label==='主地址'?(mode==='ambiguous'?2:1):0;
      },locator:()=>({count:async()=>0}),isVisible:async()=>mode!=='hidden',
      scrollIntoViewIfNeeded:async()=>{actions.push('scroll');scrolled=true;},
      hover:async()=>{actions.push('hover');hovered=true;}}),
      waitForTimeout:async()=>actions.push('wait')};
    const url='https://polardb.console.aliyun.com/cn-hangzhou/cluster/'+
      (mode==='wrong_route'?'pc-other':'pc-fake')+'/baseInfo';
    const result=await polardbEndpointLanding(surface,url,ref,Date.now()+(mode==='expired'?-1:3000));
    console.log(JSON.stringify({result,actions}));
    """.replace("MODE", json.dumps(mode))
    result = json.loads(run_node(script).stdout)
    assert result["result"]["matched"] is (mode in {"hover", "already", "lazy_section", "recommended", "late_graph"})
    expected = ["scroll"] if mode == "lazy_section" else []
    if mode in {"hover", "lazy_section", "recommended"}:
        expected += ["scroll", "hover", "wait"]
    if mode == "late_graph":
        expected += ["graph-ready", "scroll", "hover", "wait"]
        assert result["result"]["connectionContent"]["ready"] is True
    assert result["actions"] == expected
    if mode == "parent_only":
        assert "数据库连接" in result["result"]["bodyTail"]
        assert result["result"]["matched"] is False
    assert result["result"]["readOnly"] is True
    assert result["result"]["searchFallbackSuppressed"] is True


@pytest.mark.parametrize(
    "mode",
    [
        "match",
        "wrong_route",
        "no_heading",
        "ambiguous_scope",
        "ambiguous_canvas",
        "hidden",
        "small",
        "expired",
        "no_tooltip",
        "wrong_child",
        "stale",
        "split_address",
        "truncated",
        "duplicate",
    ],
)
def test_canvas_hover_requires_new_precise_dom_identity(mode):
    script = """
    import {hoverPolardbConnectionCanvas} from MODULE;
    const mode=MODE, actions=[];
    const card={visible:true,connectionScoped:true,
      text:'Endpoint ID: pe-fake 主地址: cluster.pg.example.com 端口: 5432'};
    let hovered=false;
    const canvas={count:async()=>mode==='ambiguous_canvas'?2:1,isVisible:async()=>mode!=='hidden',
      scrollIntoViewIfNeeded:async()=>actions.push('scroll'),
      boundingBox:async()=>({width:mode==='small'?100:850,height:480}),
      hover:async({position})=>{actions.push(position);hovered=true;}};
    const scope={count:async()=>mode==='ambiguous_scope'?2:1,locator:selector=>selector==='canvas'?canvas:
      {evaluateAll:async()=>({diagnosticOnly:true,controls:[]})}};
    const surface={getByText:()=>({count:async()=>mode==='no_heading'?0:1,locator:()=>scope}),
      waitForTimeout:async()=>{},locator:()=>{throw Error('unexpected DOM read');}};
    const readCards=async()=>{
      if(mode==='stale')return [card];
      if(!hovered||mode==='no_tooltip')return [];
      if(mode==='wrong_child')return [{...card,text:card.text.replace('pe-fake','pe-other')}];
      if(mode==='truncated')return [{...card,text:card.text.replace('pe-fake','pe-fa...')}];
      if(mode==='duplicate')return [card,card];
      if(mode==='split_address')return [{...card,text:'Endpoint ID: pe-fake'},
        {...card,text:'主地址: cluster.pg.example.com 端口: 5432'}];
      return [card];
    };
    const ref={resourceType:'alicloud_polardb_endpoint_address',identifier:'cluster.pg.example.com',
      regionId:'cn-hangzhou',attributes:{DBClusterId:'pc-fake',DBEndpointId:'pe-fake'}};
    const result=await hoverPolardbConnectionCanvas(surface,
      'https://polardb.console.aliyun.com/cn-hangzhou/cluster/'+
        (mode==='wrong_route'?'pc-other':'pc-fake')+'/baseInfo',
      ref,Date.now()+(mode==='expired'?-1:3000),readCards,async()=>[],
      async()=>({diagnosticOnly:true,samples:[{text:card.text}]}));
    console.log(JSON.stringify({result,actions}));
    """.replace("MODE", json.dumps(mode))
    result = json.loads(run_node(script).stdout)
    assert result["result"]["matched"] is (mode == "match")
    probes = result["result"]["probes"]
    assert len(probes) <= 8
    if mode in {"wrong_route", "no_heading", "ambiguous_scope", "ambiguous_canvas", "hidden", "expired"}:
        assert result["actions"] == []
    if mode in {"no_tooltip", "wrong_child", "stale", "split_address", "truncated", "duplicate"}:
        assert len(probes) == 8
    if mode == "match":
        assert len(probes) == 1
    assert result["result"]["readOnly"] is True


def test_canvas_hover_geometry_is_css_relative_and_bounded():
    result = json.loads(
        run_node("""
    import {connectionCanvasHoverPositions} from MODULE;
    console.log(JSON.stringify({points:connectionCanvasHoverPositions({width:850,height:480}),
      invalid:[null,{width:Infinity,height:480},{width:850,height:NaN},
        {width:850,height:100}].map(connectionCanvasHoverPositions)}));
    """).stdout
    )
    assert len(result["points"]) == 8
    assert all(0 < point["x"] < 850 and 0 < point["y"] < 480 for point in result["points"])
    assert result["invalid"] == [[], [], [], []]


def test_runner_child_has_no_generic_identity_fallback():
    runner = (ROOT / "scripts/web/e2e/resource_link_chrome_batch.mjs").read_text(encoding="utf-8")
    assert "!isPolardbEndpoint(reference) &&" in runner
    assert "polardbEndpoint ? Boolean(polardbEndpoint.matched)" in runner
    assert "baseRouteMatched && loginPassed ? result.interactionSurface : null" in runner
    assert "...(polardbEndpoint ? { polardbEndpoint } : {})" in runner
    assert "identifier: reference.attributes?.DBClusterId, regionId: reference.regionId" in runner
    module = MODULE.read_text(encoding="utf-8")
    for forbidden in [".fill(", ".goto(", ".dispatchEvent(", "textContent =", "innerHTML ="]:
        assert forbidden not in module
    assert module.count(".click(") == 1
    assert "if (!semanticLabel) continue;" in module


@pytest.mark.parametrize("mode", ["list", "hidden", "offscreen", "transparent", "bounded"])
def test_rendered_connection_controls_are_bounded_diagnostics_only(mode):
    script = """
    import {collectConnectionViewControls, matchPolardbEndpoint} from MODULE;
    const mode=MODE, doc={defaultView:{innerWidth:1000,innerHeight:800}};
    globalThis.getComputedStyle=()=>({display:'block',visibility:mode==='hidden'?'hidden':'visible',
      opacity:mode==='transparent'?'0':'1'});
    const node={tagName:'I',innerText:'',textContent:'',ownerDocument:doc,
      getAttribute:k=>({'title':'列表视图','class':'observed-list-control','aria-label':'列表视图'}[k]||null),
      getBoundingClientRect:()=>({width:20,height:20,top:mode==='offscreen'?900:20,
        left:20,right:40,bottom:mode==='offscreen'?920:40})};
    const result=collectConnectionViewControls(mode==='bounded'?Array(100).fill(node):[node]);
    const verdict=matchPolardbEndpoint('https://polardb.console.aliyun.com/cn-hangzhou/cluster/pc-fake/baseInfo',
      {resourceType:'alicloud_polardb_endpoint',identifier:'pe-fake',regionId:'cn-hangzhou',
        attributes:{DBClusterId:'pc-fake'}},result.controls);
    console.log(JSON.stringify({result,verdict}));
    """.replace("MODE", json.dumps(mode))
    output = json.loads(run_node(script).stdout)
    assert output["result"]["diagnosticOnly"] is True
    assert output["verdict"]["matched"] is False
    controls = output["result"]["controls"]
    assert len(controls) == (16 if mode == "bounded" else 1 if mode == "list" else 0)
    assert all(control["title"] == "列表视图" for control in controls)


def test_real_topology_panel_is_captured_without_relaxing_identity():
    module = MODULE.read_text(encoding="utf-8")
    assert module.count(".topology-balloon-wrap") >= 3
    assert "/Endpoint\\s*ID|端点\\s*ID/i.test(card.text)" in module
    assert "state.viewControls" in module
    assert module.count(".click(") == 1


@pytest.mark.parametrize(
    "mode",
    [
        "title",
        "hover",
        "blank",
        "edit",
        "copy",
        "apply",
        "below",
        "left",
        "wrong_parent",
        "expired",
        "ambiguous",
        "stale",
        "large",
        "observed",
        "wrong_binding",
        "nodes_list",
        "duplicate_controls",
    ],
)
def test_list_switch_requires_rendered_semantics_and_connection_header(mode):
    script = """
    import {switchConnectionListView} from MODULE;
    const mode=MODE, log=[];
    let tooltips=mode==='stale'?[{text:'列表视图'}]:[];
    const label={title:'列表视图',edit:'编辑',copy:'复制',apply:'申请'}[mode]||'';
    const autolog=['observed','wrong_binding','nodes_list','duplicate_controls'].includes(mode)?
      'key=polardb_clusterDetailList&feature='+
      (mode==='nodes_list'?'clusterNodeList':'clusterDetailList')+'&c1='+
      (mode==='wrong_binding'?'pc-other':'pc-fake'):null;
    const rect={x:300,y:100,width:30,height:24};
    if(mode==='below')rect.y=300;
    if(mode==='left')rect.x=20;
    if(mode==='large')rect.width=200;
    const button={isVisible:async()=>true,boundingBox:async()=>rect,
      evaluate:async()=>({title:label,ariaLabel:null,text:'',autolog,structure:'<button></button>'}),
      hover:async()=>{log.push('hover');if(mode==='hover'||mode==='stale')tooltips=[{text:'列表视图'}];},
      click:async()=>log.push('click')};
    const scope={count:async()=>1,locator:()=>({count:async()=>mode==='ambiguous'?9:
      mode==='duplicate_controls'?2:1,nth:()=>button})};
    const heading={count:async()=>1,locator:()=>scope,
      boundingBox:async()=>({x:100,y:100,width:100,height:24})};
    const surface={getByText:()=>heading,locator:()=>({evaluateAll:async()=>tooltips}),
      waitForTimeout:async()=>{}};
    const ref={resourceType:'alicloud_polardb_endpoint',identifier:'pe-fake',regionId:'cn-hangzhou',
      attributes:{DBClusterId:'pc-fake'}};
    const url='https://polardb.console.aliyun.com/cn-hangzhou/cluster/'+
      (mode==='wrong_parent'?'pc-other':'pc-fake')+'/baseInfo';
    console.log(JSON.stringify({state:await switchConnectionListView(surface,url,ref,
      Date.now()+(mode==='expired'?-1:10000)),log}));
    """.replace("MODE", json.dumps(mode))
    output = json.loads(run_node(script).stdout)
    assert output["state"]["readOnly"] is True
    assert output["state"]["switched"] is (mode in {"title", "hover", "observed"})
    assert output["log"].count("click") == (1 if mode in {"title", "hover", "observed"} else 0)


@pytest.mark.parametrize(
    "autolog,label,accepted",
    [
        ("key=polardb_clusterDetailList&feature=clusterDetailList&c1=pc-fake", "", True),
        ("key=polardb_clusterDetailTopology&feature=clusterDetailTopology&c1=pc-fake", "", False),
        ("key=polardb_clusterDetailList&feature=clusterDetailList&c1=pc-other", "", False),
        ("key=polardb_clusterDetailList&feature=clusterDetailList&c1=pc-fake&c1=pc-other", "", False),
        ("key=polardb_clusterDetailList&feature=clusterDetailList&c1=pc-fake&edit=1", "", False),
        ("key=polardb_clusterDetailList&feature=clusterDetailList&c1=pc-fake", "编辑", False),
        ("key=polardb_clusterDetailList&feature=clusterDetailList&c1=pc-fake", "复制", False),
        ("key=polardb_clusterDetailList&feature=clusterDetailList&c1=pc-fake", "申请", False),
    ],
)
def test_observed_control_binding_never_supplies_resource_identity(autolog, label, accepted):
    script = """
    import {isObservedConnectionListControl, matchPolardbEndpoint} from MODULE;
    const metadata={autolog:AUTOLOG,title:LABEL,text:''};
    console.log(JSON.stringify({accepted:isObservedConnectionListControl(metadata,'pc-fake'),
      identity:matchPolardbEndpoint('https://polardb.console.aliyun.com/cn-hangzhou/cluster/pc-fake/baseInfo',
        {resourceType:'alicloud_polardb_endpoint',identifier:'pe-fake',regionId:'cn-hangzhou',
          attributes:{DBClusterId:'pc-fake'}},[metadata]).matched}));
    """.replace("AUTOLOG", json.dumps(autolog)).replace("LABEL", json.dumps(label))
    result = json.loads(run_node(script).stdout)
    assert result["accepted"] is accepted
    assert result["identity"] is False


@pytest.mark.parametrize("mode", ["exact", "hidden", "no_header", "truncated", "split_rows", "wrong_id"])
def test_connection_list_cells_preserve_exact_same_row_identity(mode):
    script = """
    import {collectConnectionTableCards, matchPolardbEndpoint} from MODULE;
    const mode=MODE;
    globalThis.getComputedStyle=n=>({display:'block',visibility:n.hidden?'hidden':'visible'});
    const node=(text)=>({innerText:text,getBoundingClientRect:()=>({width:100,height:20})});
    const row=cells=>({...node(''),querySelectorAll:()=>cells.map(node)});
    let rows=[row([mode==='wrong_id'?'pe-other':mode==='truncated'?'pe-fa...':'pe-fake',
      'cluster.pg.example.com:5432'])];
    if(mode==='split_rows')rows=[row(['pe-fake','other.pg.example.com:5432']),
      row(['pe-other','cluster.pg.example.com:5432'])];
    if(mode==='hidden')rows[0].hidden=true;
    const table={...node(''),querySelectorAll:s=>s==='thead th'?
      [mode==='no_header'?'名称':'EndpointId','地址'].map(node):rows};
    const cards=collectConnectionTableCards([table]);
    const verdict=matchPolardbEndpoint('https://polardb.console.aliyun.com/cn-hangzhou/cluster/pc-fake/baseInfo',
      {resourceType:'alicloud_polardb_endpoint_address',identifier:'cluster.pg.example.com',regionId:'cn-hangzhou',
        attributes:{DBClusterId:'pc-fake',DBEndpointId:'pe-fake'}},cards);
    console.log(JSON.stringify({cards,verdict}));
    """.replace("MODE", json.dumps(mode))
    output = json.loads(run_node(script).stdout)
    assert output["verdict"]["matched"] is (mode == "exact")


@pytest.mark.parametrize(
    "mode",
    [
        "exact",
        "hidden",
        "transparent",
        "truncated",
        "wrong_id",
        "split",
        "parent",
        "primary",
        "two_cards",
        "nested",
        "wrong_address",
        "wrong_route",
        "transparent_child",
    ],
)
def test_default_endpoint_list_card_requires_visible_complete_same_card_identity(mode):
    script = """
    import {collectDefaultEndpointCards, matchPolardbEndpoint} from MODULE;
    const mode=MODE;
    globalThis.getComputedStyle=n=>({display:'block',visibility:n.hidden?'hidden':'visible',
      opacity:n.transparent?'0':'1'});
    const text='默认地址（'+(mode==='wrong_id'?'pe-other':mode==='truncated'?'pe-fa...':'pe-fake')+
      '）\\n'+(mode==='wrong_address'?'other.example.com':'cluster.pg.example.com')+':5432';
    const node=text=>({innerText:text,getBoundingClientRect:()=>({width:100,height:50}),contains:()=>false,
      querySelectorAll:()=>[]});
    let nodes=[node(text)];
    if(mode==='hidden')nodes[0].hidden=true;
    if(mode==='transparent')nodes[0].transparent=true;
    if(mode==='transparent_child'){const child=node('pe-fake');child.transparent=true;
      nodes[0].querySelectorAll=()=>[child];}
    if(mode==='split')nodes=[node('默认地址（pe-fake）'),node('cluster.pg.example.com:5432')];
    if(mode==='parent')nodes=[node('数据库连接 主地址 other.example.com:5432 '+text)];
    if(mode==='primary')nodes=[node('主地址 pe-fake cluster.pg.example.com:5432')];
    if(mode==='two_cards')nodes.push(node(text));
    if(mode==='nested'){const child=nodes[0],outer=node(text);outer.contains=n=>n===child;nodes.unshift(outer);}
    const cards=collectDefaultEndpointCards(nodes);
    const verdict=matchPolardbEndpoint('https://polardb.console.aliyun.com/cn-hangzhou/cluster/'+
      (mode==='wrong_route'?'pc-other':'pc-fake')+'/baseInfo',
      {resourceType:'alicloud_polardb_endpoint_address',identifier:'cluster.pg.example.com',regionId:'cn-hangzhou',
        attributes:{DBClusterId:'pc-fake',DBEndpointId:'pe-fake'}},cards);
    console.log(JSON.stringify({cards,verdict}));
    """.replace("MODE", json.dumps(mode))
    output = json.loads(run_node(script).stdout)
    assert output["verdict"]["matched"] is (mode in {"exact", "nested"})
