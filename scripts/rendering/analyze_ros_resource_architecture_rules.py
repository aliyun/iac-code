#!/usr/bin/env python3
"""Collect ROS resource types and generate architecture rule candidate reports."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from rich.console import Console

from iac_code.config import get_config_dir
from iac_code.pipeline.engine.architecture_meta import ArchitectureMetaRepository
from iac_code.pipeline.engine.architecture_resource_inventory import (
    ResourceInventorySnapshot,
    collect_ros_resource_inventory,
)
from iac_code.pipeline.engine.architecture_rule_candidates import (
    ResourceTypeDecision,
    RuleCandidate,
    build_resource_type_decisions,
    extract_rule_candidates,
)
from iac_code.pipeline.engine.architecture_rule_facts import (
    build_resource_rule_facts,
    render_resource_rule_facts_markdown,
)


def default_output_dir() -> Path:
    return get_config_dir() / "architecture"


def default_cache_path() -> Path:
    return default_output_dir() / "ros-resource-types.json"


def snapshot_summary(snapshot: ResourceInventorySnapshot) -> dict[str, Any]:
    return {
        "api_resource_types": len(snapshot.api_resource_types),
        "local_resource_types": len(snapshot.local_resource_types),
        "api_only_resource_types": len(snapshot.api_only_resource_types),
        "local_only_resource_types": len(snapshot.local_only_resource_types),
        "fetch_errors": len(snapshot.fetch_errors),
        "fetched_at": snapshot.fetched_at,
    }


def write_analysis_outputs(
    *,
    snapshot: ResourceInventorySnapshot,
    candidates: list[RuleCandidate],
    decisions: dict[str, ResourceTypeDecision],
    json_out: Path,
    markdown_out: Path,
) -> None:
    json_out.parent.mkdir(parents=True, exist_ok=True)
    markdown_out.parent.mkdir(parents=True, exist_ok=True)

    bundle = build_resource_rule_facts(snapshot)
    payload = {
        "summary": {
            **snapshot_summary(snapshot),
            **bundle.to_dict()["summary"],
        },
        "api_only_resource_types": list(snapshot.api_only_resource_types),
        "local_only_resource_types": list(snapshot.local_only_resource_types),
        "fetch_errors": snapshot.fetch_errors,
        "resource_facts": [fact.to_dict() for fact in bundle.resource_facts],
        "rule_signals": [signal.to_dict() for signal in bundle.rule_signals],
    }
    json_out.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    markdown_out.write_text(
        render_resource_rule_facts_markdown(bundle),
        encoding="utf-8",
    )


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Analyze all ROS resource types and produce architecture rule candidate reports."
    )
    parser.add_argument(
        "--cache",
        type=Path,
        default=None,
        help="Resource type detail cache path. Defaults to ~/.iac-code/architecture/ros-resource-types.json.",
    )
    parser.add_argument("--refresh", action="store_true", help="Refresh cached GetResourceType details.")
    parser.add_argument(
        "--max-concurrency",
        type=int,
        default=8,
        help="Maximum concurrent GetResourceType calls. Default: 8.",
    )
    parser.add_argument(
        "--json-out",
        type=Path,
        default=None,
        help="JSON report output path. Defaults to ~/.iac-code/architecture/ros-resource-facts.json.",
    )
    parser.add_argument(
        "--markdown-out",
        type=Path,
        default=None,
        help="Markdown report output path. Defaults to ~/.iac-code/architecture/ros-resource-facts.md.",
    )
    return parser.parse_args(argv)


async def async_main(argv: list[str]) -> int:
    args = parse_args(argv)
    console = Console()
    output_dir = default_output_dir()
    cache_path = args.cache or default_cache_path()
    json_out = args.json_out or output_dir / "ros-resource-facts.json"
    markdown_out = args.markdown_out or output_dir / "ros-resource-facts.md"

    console.print("[dim]Collecting ROS resource type inventory...[/]")
    snapshot = await collect_ros_resource_inventory(
        cache_path=cache_path,
        meta_repository=ArchitectureMetaRepository.load_default(),
        refresh=args.refresh,
        max_concurrency=args.max_concurrency,
    )
    candidates = extract_rule_candidates(snapshot)
    decisions = build_resource_type_decisions(snapshot, candidates)
    write_analysis_outputs(
        snapshot=snapshot,
        candidates=candidates,
        decisions=decisions,
        json_out=json_out,
        markdown_out=markdown_out,
    )

    summary = snapshot_summary(snapshot)
    console.print("[bold]Summary[/]")
    console.print_json(json.dumps(summary, ensure_ascii=False))
    console.print(f"[green]Wrote JSON report:[/] {json_out}")
    console.print(f"[green]Wrote Markdown report:[/] {markdown_out}")
    console.print(f"[dim]Cache:[/] {cache_path}")
    return 0


def main() -> None:
    raise SystemExit(asyncio.run(async_main(sys.argv[1:])))


if __name__ == "__main__":
    main()
