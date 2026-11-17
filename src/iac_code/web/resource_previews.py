"""Already-served preview text and dormant, revalidated choice bookmarks.

No filesystem reads: the existing authorized preview endpoint supplies content.
Only choice metadata is durable; unopened files never become active sources.
"""

from __future__ import annotations

import copy
import hashlib
import os
import re
from collections import OrderedDict
from typing import Any

from iac_code.tools.cloud.resource_references import locator_text, resource_locator_text, resource_spec
from iac_code.web.resource_sources import clean_occurrence_group, clean_source, resource_binding_source_key


def preview_source(cwd: str, path: str) -> dict[str, str]:
    # Lexical normalization only. Authorization and symlink resolution belong
    # to read_output_file; no additional stat/read occurs for link handling.
    identity = os.path.normcase(os.path.abspath(os.path.join(cwd, path)))
    return {
        "source_id": "output-preview:" + hashlib.sha256(identity.encode("utf-8")).hexdigest(),
        "source_path": "/content",
    }


def is_preview_source(source: Any) -> bool:
    try:
        return bool(re.fullmatch(r"output-preview:[0-9a-f]{64}", clean_source(source).get("source_id", "")))
    except ValueError:
        return False


def clean_preview_choice(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict) or set(value) != {
        "source",
        "source_digest",
        "source_key",
        "identifier",
        "identifier_kind",
        "reference_id",
        "context",
        "group",
        "resource_type",
        "declared",
    }:
        return None
    spec = resource_spec(value.get("resource_type"))
    if (
        spec is None
        or not is_preview_source(value.get("source"))
        or not isinstance(value.get("source_digest"), str)
        or not re.fullmatch(r"[0-9a-f]{64}", value["source_digest"])
        or not isinstance(value.get("reference_id"), str)
        or not re.fullmatch(r"ref-[0-9a-f]{32}", value["reference_id"])
        or value.get("identifier_kind") not in {"id", "name"}
        or type(value.get("declared")) is not bool
    ):
        return None
    try:
        group = clean_occurrence_group(value.get("group"), value["identifier"], value["identifier_kind"], spec)
        if not resource_locator_text(value.get("identifier"), spec, group[2]):
            return None
        if value.get("source_key") != resource_binding_source_key(value["source_digest"], group):
            return None
    except (ValueError, TypeError, OverflowError, RecursionError, UnicodeError):
        return None
    context = value.get("context")
    if not isinstance(context, dict) or set(context) - {
        "account_scope",
        "console_partition",
        "region_id",
        "attributes",
    }:
        return None
    attributes = context.get("attributes", {})
    if not isinstance(attributes, dict) or set(attributes) - set(spec.parent_fields):
        return None
    if any(not locator_text(item) for key, item in context.items() if key != "attributes"):
        return None
    if any(not locator_text(item) for item in attributes.values()):
        return None
    if context.get("console_partition") not in {None, "aliyun", "alibabacloud"}:
        return None
    return copy.deepcopy(value)


class PreviewSources:
    """Bounded in-memory copies of explicitly requested preview responses."""

    def __init__(self, max_sources: int = 4, max_characters: int = 2 * 1024 * 1024) -> None:
        self.max_sources = max_sources
        self.max_characters = max_characters
        self._texts: OrderedDict[str, str] = OrderedDict()

    def observe(self, source: dict[str, str], content: str) -> None:
        if not is_preview_source(source) or not isinstance(content, str):
            raise ValueError("resource_source_invalid")
        key = source["source_id"]
        self._texts[key] = content
        self._texts.move_to_end(key)
        # Keep the latest response even when it alone exceeds the soft budget:
        # a large already-visible preview must not silently lose choices.
        while len(self._texts) > 1 and (
            len(self._texts) > self.max_sources or sum(map(len, self._texts.values())) > self.max_characters
        ):
            self._texts.popitem(last=False)

    def forget(self, source: dict[str, str]) -> None:
        self._texts.pop(source.get("source_id", ""), None)

    def project(self, transcript: dict[str, Any]) -> dict[str, Any]:
        return {
            **transcript,
            "resourcePreviewSources": {key: {"/content": value} for key, value in self._texts.items()},
        }

    def clear(self) -> None:
        self._texts.clear()
