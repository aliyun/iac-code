#!/usr/bin/env python3
"""Evaluate the fixed ROS visual corpus through the public preview API and layout worker."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MANIFEST = REPOSITORY_ROOT / "src/iac_code/web/static/diagram-preview-corpus.json"
DEFAULT_WORKER = REPOSITORY_ROOT / "src/iac_code/web/static/js/eraser_layout_worker.js"


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--templates-root", type=Path, required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:8766")
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--worker", type=Path, default=DEFAULT_WORKER)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--allow-template-revision",
        action="store_true",
        help="run against a template repository revision other than the pinned corpus revision",
    )
    return parser.parse_args()


def _git_revision(root: Path) -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        capture_output=True,
        check=True,
        encoding="utf-8",
        text=True,
        timeout=10,
    )
    return completed.stdout.strip()


def _request_json(url: str, *, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        method="GET" if body is None else "POST",
        headers={"Accept": "application/json", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise RuntimeError("{} returned {}: {}".format(url, error.code, detail)) from error


def _assert_preview_page(base_url: str) -> None:
    with urllib.request.urlopen(base_url.rstrip("/") + "/diagram-preview", timeout=15) as response:
        html = response.read().decode("utf-8")
    if 'data-diagram-preview="dropzone"' not in html:
        raise RuntimeError("/diagram-preview did not return the template preview page")


def _run_worker(worker: Path, graphs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    node = shutil.which("node")
    if node is None:
        raise RuntimeError("node is required to execute the Eraser layout worker")
    harness_source = """
const fs = require("node:fs");
const vm = require("node:vm");
const outputs = [];
const context = { self: { postMessage: (value) => outputs.push(value) } };
vm.runInNewContext(fs.readFileSync(process.argv[2], "utf8"), context);
for (const [index, graph] of JSON.parse(fs.readFileSync(process.argv[3], "utf8")).entries()) {
  context.self.onmessage({ data: { requestId: String(index), graph } });
}
process.stdout.write(JSON.stringify(outputs));
""".strip()
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        harness = directory / "worker-harness.cjs"
        graph_file = directory / "graphs.json"
        harness.write_text(harness_source, encoding="utf-8")
        graph_file.write_text(json.dumps(graphs, ensure_ascii=False), encoding="utf-8")
        completed = subprocess.run(
            [node, str(harness), str(worker), str(graph_file)],
            capture_output=True,
            check=False,
            encoding="utf-8",
            text=True,
            timeout=60,
        )
    if completed.returncode != 0:
        raise RuntimeError("layout worker failed: {}".format(completed.stderr.strip()))
    outputs = json.loads(completed.stdout)
    failures = [item.get("error", "unknown worker error") for item in outputs if not item.get("ok")]
    if failures:
        raise RuntimeError("layout worker rejected corpus: {}".format("; ".join(failures)))
    return [item["layout"] for item in outputs]


def _overlaps(left: dict[str, Any], right: dict[str, Any], padding: float = 0) -> bool:
    return (
        left["x"] + padding < right["x"] + right["width"] - padding
        and right["x"] + padding < left["x"] + left["width"] - padding
        and left["y"] + padding < right["y"] + right["height"] - padding
        and right["y"] + padding < left["y"] + left["height"] - padding
    )


def _segment_crosses_rect(start: list[float], end: list[float], rectangle: dict[str, Any], margin: float = 3) -> bool:
    left = rectangle["x"] + margin
    right = rectangle["x"] + rectangle["width"] - margin
    top = rectangle["y"] + margin
    bottom = rectangle["y"] + rectangle["height"] - margin
    if start[0] == end[0]:
        return left < start[0] < right and max(start[1], end[1]) > top and min(start[1], end[1]) < bottom
    if start[1] == end[1]:
        return top < start[1] < bottom and max(start[0], end[0]) > left and min(start[0], end[0]) < right
    return False


def _layout_metrics(graph: dict[str, Any], layout: dict[str, Any]) -> dict[str, Any]:
    items = {item["id"]: item for item in layout["items"]}
    values = list(items.values())
    sibling_overlaps: list[list[str]] = []
    for index, left in enumerate(values):
        for right in values[index + 1 :]:
            if left.get("parentId") == right.get("parentId") and _overlaps(left, right):
                sibling_overlaps.append([left["id"], right["id"]])
    containment_failures = []
    for item in values:
        parent = items.get(item.get("parentId"))
        if parent and not (
            item["x"] >= parent["x"]
            and item["y"] >= parent["y"]
            and item["x"] + item["width"] <= parent["x"] + parent["width"]
            and item["y"] + item["height"] <= parent["y"] + parent["height"]
        ):
            containment_failures.append(item["id"])
    outside = [
        item["id"]
        for item in values
        if item["x"] < 0
        or item["y"] < 0
        or item["x"] + item["width"] > layout["width"]
        or item["y"] + item["height"] > layout["height"]
    ]
    edge_node_intersections = []
    for edge in layout["edges"]:
        for item in values:
            if item["kind"] != "node" or item["id"] in {edge["from"], edge["to"]}:
                continue
            if any(_segment_crosses_rect(start, end, item) for start, end in zip(edge["points"], edge["points"][1:])):
                edge_node_intersections.append([edge["id"], item["id"]])
    return {
        "layoutVersion": layout["layoutVersion"],
        "width": layout["width"],
        "height": layout["height"],
        "aspectRatio": round(layout["width"] / layout["height"], 3),
        "nodeCount": len(graph["nodes"]),
        "containerCount": len(graph["containers"]),
        "edgeCount": len(graph["edges"]),
        "siblingOverlaps": sibling_overlaps,
        "containmentFailures": containment_failures,
        "outsideCanvas": outside,
        "edgeNodeIntersections": edge_node_intersections,
    }


def _markdown(report: dict[str, Any]) -> str:
    summary = report["summary"]
    lines = [
        "# Eraser visual corpus evaluation",
        "",
        "- Template revision: `{}`".format(report["templateRepositoryCommit"]),
        "- Layout version: `{}`".format(summary["layoutVersion"]),
        "- Templates: {}/{} passed".format(summary["passedTemplates"], summary["templateCount"]),
        "- Views: {}/{} passed".format(summary["passedViews"], summary["viewCount"]),
        "- Sibling overlaps: {}".format(summary["siblingOverlaps"]),
        "- Containment failures: {}".format(summary["containmentFailures"]),
        "- Outside canvas: {}".format(summary["outsideCanvas"]),
        "- Edge/node intersections: {}".format(summary["edgeNodeIntersections"]),
        "- Extreme aspect ratios: {}".format(summary["extremeAspectRatios"]),
        "",
        "| # | Template | Views | Layout size(s) | Result |",
        "| ---: | --- | ---: | --- | --- |",
    ]
    for index, item in enumerate(report["templates"], 1):
        sizes = ", ".join("{}×{}".format(view["width"], view["height"]) for view in item["views"])
        lines.append(
            "| {} | `{}` | {} | {} | {} |".format(index, item["path"], len(item["views"]), sizes, item["result"])
        )
    return "\n".join(lines) + "\n"


def main() -> int:
    arguments = _arguments()
    manifest = json.loads(arguments.manifest.read_text(encoding="utf-8"))
    expected_revision = manifest["templateRepositoryCommit"]
    actual_revision = _git_revision(arguments.templates_root)
    if actual_revision != expected_revision and not arguments.allow_template_revision:
        raise RuntimeError(
            "template repository is at {}; expected {} (use --allow-template-revision to override)".format(
                actual_revision, expected_revision
            )
        )
    _assert_preview_page(arguments.base_url)
    templates = []
    graphs = []
    graph_owners = []
    endpoint = arguments.base_url.rstrip("/") + "/api/diagram-preview"
    for relative_path in manifest["templates"]:
        path = arguments.templates_root / relative_path
        payload = _request_json(endpoint, payload={"filename": path.name, "content": path.read_text(encoding="utf-8")})
        views = payload.get("views") or []
        if not views:
            raise RuntimeError("{} returned no architecture views".format(relative_path))
        template = {"path": relative_path, "filename": payload.get("filename"), "views": [], "result": "pending"}
        template_index = len(templates)
        templates.append(template)
        for view_index, view in enumerate(views):
            graph = view.get("graph")
            if not isinstance(graph, dict):
                raise RuntimeError("{} view {} returned no graph".format(relative_path, view_index))
            graphs.append(graph)
            graph_owners.append((template_index, view_index, graph))
    layouts = _run_worker(arguments.worker, graphs)
    for layout, (template_index, _view_index, graph) in zip(layouts, graph_owners):
        templates[template_index]["views"].append(_layout_metrics(graph, layout))
    for template in templates:
        failed = any(
            view[metric]
            for view in template["views"]
            for metric in ("siblingOverlaps", "containmentFailures", "outsideCanvas", "edgeNodeIntersections")
        )
        template["result"] = "fail" if failed else "pass"
    all_views = [view for template in templates for view in template["views"]]
    summary = {
        "templateCount": len(templates),
        "passedTemplates": sum(template["result"] == "pass" for template in templates),
        "viewCount": len(all_views),
        "passedViews": sum(
            not any(
                view[metric]
                for metric in ("siblingOverlaps", "containmentFailures", "outsideCanvas", "edgeNodeIntersections")
            )
            for view in all_views
        ),
        "layoutVersion": all_views[0]["layoutVersion"] if all_views else "",
        "siblingOverlaps": sum(len(view["siblingOverlaps"]) for view in all_views),
        "containmentFailures": sum(len(view["containmentFailures"]) for view in all_views),
        "outsideCanvas": sum(len(view["outsideCanvas"]) for view in all_views),
        "edgeNodeIntersections": sum(len(view["edgeNodeIntersections"]) for view in all_views),
        "extremeAspectRatios": sum(view["aspectRatio"] < 0.55 or view["aspectRatio"] > 3.8 for view in all_views),
    }
    report = {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "baseUrl": arguments.base_url,
        "templateRepository": str(arguments.templates_root.resolve()),
        "templateRepositoryCommit": actual_revision,
        "manifest": str(arguments.manifest.resolve()),
        "worker": str(arguments.worker.resolve()),
        "summary": summary,
        "templates": templates,
    }
    arguments.output.mkdir(parents=True, exist_ok=True)
    (arguments.output / "evaluation.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (arguments.output / "evaluation.md").write_text(_markdown(report), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["passedTemplates"] == summary["templateCount"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
