"""ROS resource type inventory used by architecture rule extraction."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from iac_code.pipeline.engine.architecture_meta import ArchitectureMetaRepository, ResourceMeta
from iac_code.tools.base import ToolContext


@dataclass(frozen=True)
class RosResourceTypeDetail:
    resource_type: str
    entity_type: str | None
    provider: str | None
    properties: dict[str, dict[str, Any]]
    attributes: dict[str, Any]
    support_drift_detection: bool | None = None
    support_scratch_detection: bool | None = None
    description: str | None = None

    @classmethod
    def from_api_response(cls, raw: dict[str, Any]) -> RosResourceTypeDetail | None:
        resource_type = raw.get("ResourceType")
        if not isinstance(resource_type, str) or not resource_type:
            return None
        properties = raw.get("Properties")
        attributes = raw.get("Attributes")
        return cls(
            resource_type=resource_type,
            entity_type=raw.get("EntityType") if isinstance(raw.get("EntityType"), str) else None,
            provider=raw.get("Provider") if isinstance(raw.get("Provider"), str) else None,
            properties=_dict_of_dicts(properties),
            attributes=attributes if isinstance(attributes, dict) else {},
            support_drift_detection=_bool_or_none(raw.get("SupportDriftDetection")),
            support_scratch_detection=_bool_or_none(raw.get("SupportScratchDetection")),
            description=raw.get("Description") if isinstance(raw.get("Description"), str) else None,
        )

    @classmethod
    def from_dict(cls, raw: Any) -> RosResourceTypeDetail | None:
        if not isinstance(raw, dict):
            return None
        resource_type = raw.get("resource_type")
        if not isinstance(resource_type, str) or not resource_type:
            return None
        return cls(
            resource_type=resource_type,
            entity_type=raw.get("entity_type") if isinstance(raw.get("entity_type"), str) else None,
            provider=raw.get("provider") if isinstance(raw.get("provider"), str) else None,
            properties=_dict_of_dicts(raw.get("properties")),
            attributes=raw.get("attributes") if isinstance(raw.get("attributes"), dict) else {},
            support_drift_detection=_bool_or_none(raw.get("support_drift_detection")),
            support_scratch_detection=_bool_or_none(raw.get("support_scratch_detection")),
            description=raw.get("description") if isinstance(raw.get("description"), str) else None,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "resource_type": self.resource_type,
            "entity_type": self.entity_type,
            "provider": self.provider,
            "properties": self.properties,
            "attributes": self.attributes,
            "support_drift_detection": self.support_drift_detection,
            "support_scratch_detection": self.support_scratch_detection,
            "description": self.description,
        }


@dataclass(frozen=True)
class ResourceInventoryItem:
    resource_type: str
    product_code: str
    source_state: str
    detail: RosResourceTypeDetail | None
    meta: ResourceMeta | None
    name_zh: str | None
    name_en: str | None
    category_code: str | None


@dataclass(frozen=True)
class ResourceInventorySnapshot:
    items: dict[str, ResourceInventoryItem]
    api_resource_types: tuple[str, ...]
    local_resource_types: tuple[str, ...]
    api_only_resource_types: tuple[str, ...]
    local_only_resource_types: tuple[str, ...]
    fetched_at: str | None
    fetch_errors: dict[str, str]


class RosResourceTypeClient(Protocol):
    async def list_resource_types(self) -> list[str]:
        """Return ROS resource type names from ListResourceTypes."""

    async def get_resource_type(self, resource_type: str) -> dict[str, Any]:
        """Return one ROS GetResourceType response."""


class AliyunRosResourceTypeClient:
    """ROS resource type client backed by the project AliyunApi tool."""

    def __init__(self, internal_caller: Any) -> None:
        self._internal_caller = internal_caller

    async def list_resource_types(self) -> list[str]:
        body = await self._call("ListResourceTypes", {})
        values = body.get("ResourceTypes")
        if not isinstance(values, list):
            return []
        return sorted(item for item in values if isinstance(item, str))

    async def get_resource_type(self, resource_type: str) -> dict[str, Any]:
        return await self._call("GetResourceType", {"ResourceType": resource_type})

    async def _call(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        result = await self._internal_caller.call(
            tool_input={"product": "ros", "action": action, "params": params},
            context=ToolContext(),
        )
        if result.is_error:
            raise RuntimeError(result.content)
        body = json.loads(result.content)
        return body if isinstance(body, dict) else {}


async def collect_ros_resource_inventory(
    *,
    client: RosResourceTypeClient | None = None,
    cache_path: Path | None = None,
    meta_repository: ArchitectureMetaRepository | None = None,
    fetched_at: str | None = None,
    refresh: bool = False,
    max_concurrency: int = 8,
    internal_caller: Any = None,
) -> ResourceInventorySnapshot:
    """Collect an API-authoritative ROS inventory and merge local metadata.

    The cache stores only resource type details and errors. It never stores credentials.
    """

    if client is None:
        if internal_caller is None:
            raise RuntimeError("aliyun_internal_caller_required")
        client = AliyunRosResourceTypeClient(internal_caller)
    meta_repository = meta_repository or ArchitectureMetaRepository.load_default()
    api_resource_types = tuple(sorted(await client.list_resource_types()))
    cached_details, cached_errors = _load_inventory_cache(cache_path)
    details_by_type = dict(cached_details)
    fetch_errors: dict[str, str] = dict(cached_errors)

    missing_types = [
        resource_type
        for resource_type in api_resource_types
        if refresh or resource_type not in details_by_type or resource_type in fetch_errors
    ]
    semaphore = asyncio.Semaphore(max(1, max_concurrency))

    async def fetch_one(resource_type: str) -> None:
        async with semaphore:
            try:
                detail = RosResourceTypeDetail.from_api_response(await client.get_resource_type(resource_type))
            except Exception as exc:  # pragma: no cover - exact SDK exceptions are integration-specific
                fetch_errors[resource_type] = str(exc)
                return
            if detail is not None:
                details_by_type[resource_type] = detail
            fetch_errors.pop(resource_type, None)

    if missing_types:
        await asyncio.gather(*(fetch_one(resource_type) for resource_type in missing_types))

    if cache_path is not None:
        _write_inventory_cache(
            cache_path=cache_path,
            resource_types=api_resource_types,
            details_by_type=details_by_type,
            fetch_errors=fetch_errors,
        )

    return build_resource_inventory_snapshot(
        api_resource_types=api_resource_types,
        details_by_type=details_by_type,
        meta_repository=meta_repository,
        fetched_at=fetched_at,
        fetch_errors=fetch_errors,
    )


def build_resource_inventory_snapshot(
    *,
    api_resource_types: list[str] | tuple[str, ...],
    details_by_type: Mapping[str, RosResourceTypeDetail | dict[str, Any]],
    meta_repository: ArchitectureMetaRepository,
    fetched_at: str | None = None,
    fetch_errors: dict[str, str] | None = None,
) -> ResourceInventorySnapshot:
    api_types = tuple(sorted(dict.fromkeys(api_resource_types)))
    local_types = tuple(sorted(meta_repository._resources))
    api_set = set(api_types)
    local_set = set(local_types)

    normalized_details: dict[str, RosResourceTypeDetail] = {}
    for resource_type, detail in details_by_type.items():
        parsed = detail if isinstance(detail, RosResourceTypeDetail) else RosResourceTypeDetail.from_dict(detail)
        if parsed is not None:
            normalized_details[resource_type] = parsed

    items: dict[str, ResourceInventoryItem] = {}
    for resource_type in sorted(api_set | local_set):
        meta = meta_repository.get_resource(resource_type)
        detail = normalized_details.get(resource_type)
        if resource_type in api_set and meta is not None:
            source_state = "api+local"
        elif resource_type in api_set:
            source_state = "api-only"
        else:
            source_state = "local-only"
        items[resource_type] = ResourceInventoryItem(
            resource_type=resource_type,
            product_code=_product_code(resource_type, meta),
            source_state=source_state,
            detail=detail,
            meta=meta,
            name_zh=meta.name_zh if meta is not None else None,
            name_en=meta.name_en if meta is not None else None,
            category_code=meta.category_code if meta is not None else None,
        )

    return ResourceInventorySnapshot(
        items=items,
        api_resource_types=api_types,
        local_resource_types=local_types,
        api_only_resource_types=tuple(sorted(api_set - local_set)),
        local_only_resource_types=tuple(sorted(local_set - api_set)),
        fetched_at=fetched_at,
        fetch_errors=dict(fetch_errors or {}),
    )


def _load_inventory_cache(cache_path: Path | None) -> tuple[dict[str, RosResourceTypeDetail], dict[str, str]]:
    if cache_path is None or not cache_path.exists():
        return {}, {}
    raw = json.loads(cache_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        return {}, {}
    details: dict[str, RosResourceTypeDetail] = {}
    raw_details = raw.get("details")
    if isinstance(raw_details, dict):
        for resource_type, value in raw_details.items():
            if not isinstance(resource_type, str):
                continue
            detail = RosResourceTypeDetail.from_dict(value)
            if detail is not None:
                details[resource_type] = detail
    errors = {
        key: value
        for key, value in (raw.get("errors") or {}).items()
        if isinstance(key, str) and isinstance(value, str)
    }
    return details, errors


def _write_inventory_cache(
    *,
    cache_path: Path,
    resource_types: tuple[str, ...],
    details_by_type: dict[str, RosResourceTypeDetail],
    fetch_errors: dict[str, str],
) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "resource_types": list(resource_types),
        "details": {
            resource_type: detail.to_dict()
            for resource_type, detail in sorted(details_by_type.items())
            if resource_type in resource_types
        },
        "errors": dict(sorted(fetch_errors.items())),
    }
    cache_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def _dict_of_dicts(value: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(value, dict):
        return {}
    return {key: item for key, item in value.items() if isinstance(key, str) and isinstance(item, dict)}


def _bool_or_none(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _product_code(resource_type: str, meta: ResourceMeta | None) -> str:
    if meta is not None and meta.product_code:
        return meta.product_code
    parts = resource_type.split("::")
    return parts[1].lower() if len(parts) >= 2 else "unknown"
