import json
import shutil
import subprocess
from pathlib import Path

import pytest

WORKER_JS = Path(__file__).parents[2] / "src/iac_code/web/static/js/eraser_layout_worker.js"


def _run_workers(tmp_path: Path, graphs: list[dict[str, object]]) -> list[dict[str, object]]:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    harness = tmp_path / "eraser-worker-harness.cjs"
    harness.write_text(
        """
const fs = require("node:fs");
const vm = require("node:vm");
const outputs = [];
const context = { self: { postMessage: (value) => { outputs.push(value); } } };
vm.runInNewContext(fs.readFileSync(process.argv[2], "utf8"), context);
for (const graph of JSON.parse(process.argv[3])) {
  context.self.onmessage({ data: { requestId: "test", graph } });
}
process.stdout.write(JSON.stringify(outputs));
""".strip(),
        encoding="utf-8",
    )
    completed = subprocess.run(
        [node, str(harness), str(WORKER_JS), json.dumps(graphs)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def _run_worker(tmp_path: Path, graph: dict[str, object]) -> dict[str, object]:
    return _run_workers(tmp_path, [graph])[0]


def _graph() -> dict[str, object]:
    return {
        "version": 1,
        "nodes": [
            {"id": "web", "label": "Web", "parentId": "vpc", "resourceType": "ALIYUN::ECS::Instance"},
            {"id": "db", "label": "Database", "parentId": "vpc", "resourceType": "ALIYUN::RDS::DBInstance"},
            {"id": "cdn", "label": "CDN", "parentId": None, "resourceType": "ALIYUN::CDN::Domain"},
        ],
        "containers": [{"id": "vpc", "label": "VPC", "parentId": None}],
        "edges": [
            {"id": "e1", "from": "cdn", "to": "web", "label": "HTTPS", "style": "solid_arrow"},
            {"id": "e2", "from": "web", "to": "db", "label": "SQL", "style": "solid_arrow"},
        ],
        "layout": {"direction": "LR"},
    }


def _segment_crosses_rect(start, end, rect, *, margin: int = 3) -> bool:
    left = rect["x"] + margin
    right = rect["x"] + rect["width"] - margin
    top = rect["y"] + margin
    bottom = rect["y"] + rect["height"] - margin
    if start[0] == end[0]:
        return left < start[0] < right and max(start[1], end[1]) > top and min(start[1], end[1]) < bottom
    if start[1] == end[1]:
        return top < start[1] < bottom and max(start[0], end[0]) > left and min(start[0], end[0]) < right
    return False


def _edge_crosses_unrelated_node(edge, items) -> bool:
    for item in items:
        if item["kind"] != "node" or item["id"] in {edge["from"], edge["to"]}:
            continue
        if any(_segment_crosses_rect(start, end, item) for start, end in zip(edge["points"], edge["points"][1:])):
            return True
    return False


def test_layout_is_deterministic_and_routes_every_edge(tmp_path: Path) -> None:
    graph = _graph()
    reordered = {
        **graph,
        "nodes": list(reversed(graph["nodes"])),
        "containers": list(reversed(graph["containers"])),
        "edges": list(reversed(graph["edges"])),
    }
    first, second = _run_workers(tmp_path, [graph, reordered])

    assert first == second
    assert first["ok"] is True
    layout = first["layout"]
    assert layout["layoutVersion"] == "iac-layered-v7"
    assert {item["id"] for item in layout["items"]} == {"vpc", "web", "db", "cdn"}
    assert all(len(edge["points"]) >= 2 for edge in layout["edges"])


def test_layout_terminates_for_a_directed_cycle_without_overlapping_peers(tmp_path: Path) -> None:
    graph = _graph()
    graph["edges"] = [
        {"id": "e1", "from": "web", "to": "db", "style": "solid_arrow"},
        {"id": "e2", "from": "db", "to": "web", "style": "solid_arrow"},
    ]

    result = _run_worker(tmp_path, graph)

    assert result["ok"] is True
    rects = {item["id"]: item for item in result["layout"]["items"]}
    web = rects["web"]
    database = rects["db"]
    separated = (
        web["x"] + web["width"] <= database["x"]
        or database["x"] + database["width"] <= web["x"]
        or web["y"] + web["height"] <= database["y"]
        or database["y"] + database["height"] <= web["y"]
    )
    assert separated


def test_layout_balances_unranked_peers_and_uses_inferred_edges_as_fallback_order(tmp_path: Path) -> None:
    graph = {
        "version": 1,
        "nodes": [{"id": f"node-{index}", "label": f"Node {index}"} for index in range(9)],
        "containers": [],
        "edges": [
            {"id": "inferred", "from": "node-0", "to": "node-8", "style": "dotted_open"},
        ],
    }

    result = _run_worker(tmp_path, graph)

    assert result["ok"] is True
    layout = result["layout"]
    items = {item["id"]: item for item in layout["items"]}
    assert items["node-0"]["x"] < items["node-8"]["x"]
    assert 0.65 <= layout["width"] / layout["height"] <= 3.5


def test_layout_tiles_container_peers_and_sizes_long_labels(tmp_path: Path) -> None:
    graph = {
        "version": 1,
        "nodes": [
            {
                "id": f"node-{index}",
                "label": "CloudSSO::PermissionPolicyToAccessConfigurationAddition" if index == 0 else f"Worker {index}",
                "parentId": "vpc",
            }
            for index in range(5)
        ],
        "containers": [{"id": "vpc", "label": "VPC", "parentId": None}],
        "edges": [],
    }

    result = _run_worker(tmp_path, graph)

    assert result["ok"] is True
    layout = result["layout"]
    items = {item["id"]: item for item in layout["items"]}
    assert items["node-0"]["width"] > 190
    assert items["vpc"]["width"] / items["vpc"]["height"] < 3
    assert len({items[f"node-{index}"]["y"] for index in range(5)}) > 1


def test_layout_routes_cycle_edges_around_unrelated_nodes(tmp_path: Path) -> None:
    graph = {
        "version": 1,
        "nodes": [{"id": f"node-{index}", "label": f"Node {index}"} for index in range(7)],
        "containers": [],
        "edges": [
            {
                "id": f"edge-{index}",
                "from": f"node-{index}",
                "to": f"node-{(index + 1) % 7}",
                "style": "dotted_open",
            }
            for index in range(7)
        ],
    }

    result = _run_worker(tmp_path, graph)

    assert result["ok"] is True
    layout = result["layout"]
    assert all(not _edge_crosses_unrelated_node(edge, layout["items"]) for edge in layout["edges"])


def test_layout_rejects_cyclic_containment_and_size_limit(tmp_path: Path) -> None:
    cyclic = {
        "version": 1,
        "nodes": [{"id": "node", "label": "Node", "parentId": "left"}],
        "containers": [
            {"id": "left", "label": "Left", "parentId": "right"},
            {"id": "right", "label": "Right", "parentId": "left"},
        ],
        "edges": [],
    }
    oversized = {
        "version": 1,
        "nodes": [{"id": f"node-{index}", "label": "Node"} for index in range(81)],
        "containers": [],
        "edges": [],
    }

    cyclic_result, oversized_result = _run_workers(tmp_path, [cyclic, oversized])

    assert cyclic_result == {"requestId": "test", "ok": False, "error": "Cyclic architecture containment"}
    assert oversized_result == {"requestId": "test", "ok": False, "error": "Architecture graph is too large"}


def test_layout_repairs_legacy_nested_planning_groups(tmp_path: Path) -> None:
    graph = {
        "version": 1,
        "nodes": [
            {"id": "public_user", "label": "公网用户 Internet\n访问入口", "parentId": None},
            {"id": "eip", "label": "弹性公网 EIP\n唯一公网地址", "parentId": None},
            {"id": "vswitch", "label": "交换机 VSwitch\n可用区 A", "parentId": "group_vpc"},
            {"id": "ecs", "label": "iac-code Web\nECS 运行服务", "parentId": "group_vswitch"},
            {"id": "sg", "label": "安全组 SecurityGroup\n仅开放 8766", "parentId": "group_vpc"},
        ],
        "containers": [
            {"id": "group_vpc", "label": "专有网络 VPC 网络隔离", "parentId": None},
            {"id": "group_vswitch", "label": "vswitch", "parentId": None},
        ],
        "edges": [
            {"id": "e1", "from": "public_user", "to": "eip", "label": "HTTP", "style": "solid_arrow"},
            {"id": "e2", "from": "eip", "to": "ecs", "label": "8766", "style": "solid_arrow"},
            {"id": "e3", "from": "sg", "to": "ecs", "label": "仅 8766", "style": "solid_arrow"},
        ],
    }

    result = _run_worker(tmp_path, graph)

    assert result["ok"] is True
    layout = result["layout"]
    assert layout["layoutVersion"] == "iac-layered-v7"
    items = {item["id"]: item for item in layout["items"]}
    assert "vswitch" not in items
    assert items["group_vswitch"]["parentId"] == "group_vpc"
    assert items["group_vswitch"]["label"] == "交换机 VSwitch\n可用区 A"
    assert items["ecs"]["parentId"] == "group_vswitch"
    assert items["public_user"]["x"] < items["eip"]["x"] < items["group_vpc"]["x"]
    assert items["group_vpc"]["x"] < items["group_vswitch"]["x"] < items["ecs"]["x"]
    assert items["sg"]["y"] < items["group_vswitch"]["y"] < items["ecs"]["y"]
    sg_edge = next(edge for edge in layout["edges"] if edge["id"] == "e3")
    assert sg_edge["labelPlacement"]["width"] >= 40
    assert items["sg"]["y"] + items["sg"]["height"] <= sg_edge["labelPlacement"]["y"]
    assert sg_edge["labelPlacement"]["y"] + sg_edge["labelPlacement"]["height"] <= items["group_vswitch"]["y"]
    assert sg_edge["fromPort"] == "bottom"
    assert sg_edge["toPort"] in {"top", "right"}
    assert not _edge_crosses_unrelated_node(sg_edge, layout["items"])
