#!/usr/bin/env python3
"""Refresh the committed Alibaba Cloud ROS resource-index evidence snapshot."""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import html.parser
import json
import re
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urldefrag

SOURCE_URL = "https://help.aliyun.com/zh/ros/developer-reference/list-of-resource-types-by-service"
EXTRACTOR_VERSION = "official-resource-index-v1"
DETAIL_EXTRACTOR_VERSION = "official-resource-detail-v1"
DETAIL_NORMALIZATION = "embedded-document-content-v1"
RESOURCE_LINK = re.compile(
    rb'href="([^"]+)"[^>]*>\s*((?:ALIYUN|DATASOURCE)::[A-Za-z0-9]+::[A-Za-z0-9]+)\s*<'
)
DOCUMENT_CONTENT = re.compile(rb'"content":"((?:\\.|[^"\\])*)"')

DETAIL_FIXTURES: dict[str, dict[str, Any]] = {
    "DATASOURCE::CMS::Namespaces": {
        "url": "https://help.aliyun.com/zh/ros/developer-reference/datasource-cms-namespaces",
        "required_text": (
            "Namespaces：指标仓库详情列表。",
            "Namespaces List 指标仓库详情列表。",
            "CreateTime String",
            "Namespace String",
            "Specification String",
            "Description String",
            "ModifyTime String",
        ),
        "observations": {
            "output": "Namespaces",
            "documented_type": "List[Map]",
            "documented_members": ["CreateTime", "Namespace", "Specification", "Description", "ModifyTime"],
        },
    },
    "DATASOURCE::DTS::MigrationJobs": {
        "url": "https://help.aliyun.com/zh/ros/developer-reference/datasource-dts-migrationjobs",
        "required_text": (
            "DtsInstanceIds：迁移实例 ID 列表。",
            "MigrationInstances：迁移实例详情列表",
            "DtsInstanceIds List",
            "SynchronizationInstances List",
        ),
        "observations": {
            "declared_outputs": ["DtsInstanceIds", "MigrationInstances"],
            "table_outputs": ["DtsInstanceIds", "SynchronizationInstances"],
        },
    },
    "DATASOURCE::ECS::ManagedInstances": {
        "url": "https://help.aliyun.com/zh/ros/developer-reference/datasource-ecs-managedinstances",
        "required_text": (
            "Instances：托管实例详情列表。",
            "Tags Map 标签列表。",
            "TagKey",
            "TagValue",
        ),
        "observations": {
            "output": "Instances.Tags",
            "table_type": "Map",
            "example_member_keys": ["TagKey", "TagValue"],
        },
    },
}


class _TextExtractor(html.parser.HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def _document_text(content: str) -> str:
    parser = _TextExtractor()
    parser.feed(content)
    return " ".join(" ".join(parser.parts).split())


def _extract_document_content(response: bytes, resource_type: str) -> str:
    candidates: list[str] = []
    ros_article_candidates: list[str] = []
    for match in DOCUMENT_CONTENT.finditer(response):
        try:
            content = json.loads(b'"' + match.group(1) + b'"')
        except (UnicodeDecodeError, ValueError):
            continue
        if not isinstance(content, str):
            continue
        if resource_type in content:
            candidates.append(content)
        if "ROSTemplateFormatVersion" in content and "Fn::GetAtt" in content:
            ros_article_candidates.append(content)
    if candidates:
        # A page may contain several serialized ``content`` fields.  The
        # article body is the longest field that names the requested type.
        return max(candidates, key=lambda item: len(item))
    if ros_article_candidates:
        # A small number of index aliases point to an article whose canonical
        # Resource Type differs from the index label (for example the legacy
        # POLARDB DBInstance link).  The official index proves the link; these
        # two article markers ensure we still hash normalized ROS body content.
        return max(ros_article_candidates, key=lambda item: len(item))
    raise RuntimeError("official ROS detail page could not be parsed for {}".format(resource_type))


def _previous_retrieved_at(path: Path, content_sha256: str) -> str | None:
    if not path.is_file():
        return None
    try:
        previous: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if isinstance(previous, dict) and previous.get("content_sha256") == content_sha256:
        value = previous.get("retrieved_at")
        return value if isinstance(value, str) else None
    return None


def _previous_detail_retrieved_at(path: Path, resource_type: str, content_sha256: str) -> str | None:
    if not path.is_file():
        return None
    try:
        previous: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    details = previous.get("resource_details") if isinstance(previous, dict) else None
    detail = details.get(resource_type) if isinstance(details, dict) else None
    if isinstance(detail, dict) and detail.get("content_sha256") == content_sha256:
        value = detail.get("retrieved_at")
        return value if isinstance(value, str) else None
    return None


def _fetch(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "iac-code-ros-evidence/1"})
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 - fixed official HTTPS URLs
        return response.read()


def _fetch_detail(request_url: str) -> tuple[str, bytes]:
    return request_url, _fetch(request_url)


def refresh(output: Path, *, workers: int = 12) -> None:
    if not 1 <= workers <= 32:
        raise ValueError("workers must be between 1 and 32")
    content = _fetch(SOURCE_URL)
    digest = hashlib.sha256(content).hexdigest()
    resources: dict[str, str] = {}
    for raw_url, raw_resource_type in RESOURCE_LINK.findall(content):
        resources.setdefault(raw_resource_type.decode("utf-8"), raw_url.decode("utf-8"))
    if len(resources) < 1_000:
        raise RuntimeError("official ROS resource index extraction is unexpectedly small: {}".format(len(resources)))
    retrieved_at = _previous_retrieved_at(output, digest) or datetime.now(timezone.utc).isoformat()
    request_urls = sorted({urldefrag(url).url for url in resources.values()})
    responses: dict[str, bytes] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(_fetch_detail, url) for url in request_urls]
        for future in concurrent.futures.as_completed(futures):
            request_url, response = future.result()
            responses[request_url] = response
    if set(responses) != set(request_urls):
        raise RuntimeError("official ROS detail refresh did not fetch every resource page")

    refreshed_at = datetime.now(timezone.utc).isoformat()
    resource_details: dict[str, dict[str, Any]] = {}
    for resource_type, url in sorted(resources.items()):
        response = responses[urldefrag(url).url]
        document_content = _extract_document_content(response, resource_type)
        fixture = DETAIL_FIXTURES.get(resource_type)
        if fixture is not None:
            document_text = _document_text(document_content)
            missing = [snippet for snippet in fixture["required_text"] if snippet not in document_text]
            if missing:
                raise RuntimeError(
                    "official ROS detail page contract could not be parsed for {}: missing {}".format(
                        resource_type, missing
                    )
                )
        detail_digest = hashlib.sha256(document_content.encode("utf-8")).hexdigest()
        detail_retrieved_at = (
            _previous_detail_retrieved_at(output, resource_type, detail_digest)
            or refreshed_at
        )
        detail: dict[str, Any] = {
            "url": url,
            "locale": "zh-CN",
            "content_sha256": detail_digest,
            "extractor_version": DETAIL_EXTRACTOR_VERSION,
            "normalization": DETAIL_NORMALIZATION,
            "retrieved_at": detail_retrieved_at,
            "documented_type": resource_type,
        }
        if fixture is not None:
            detail["observations"] = fixture["observations"]
        resource_details[resource_type] = detail
    payload = {
        "schema_version": 2,
        "source_url": SOURCE_URL,
        "locale": "zh-CN",
        "content_sha256": digest,
        "extractor_version": EXTRACTOR_VERSION,
        "retrieved_at": retrieved_at,
        "resources": dict(sorted(resources.items())),
        "resource_details": resource_details,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--workers", type=int, default=12)
    args = parser.parse_args()
    refresh(args.output.resolve(), workers=args.workers)


if __name__ == "__main__":
    main()
