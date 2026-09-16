"""Model-facing progressive-disclosure resource selector tools."""

from __future__ import annotations

import asyncio
import contextvars
import json
import re
import uuid
from collections.abc import Callable
from contextlib import contextmanager
from typing import Any, cast

from iac_code.tools.base import Tool, ToolContext, ToolRegistry, ToolResult
from iac_code.types.stream_events import CloudResourceSelectionEvent

from .profiles import (
    PROFILE_HASH,
    SelectorProfile,
    get_profile,
    get_profile_by_association_property,
    is_out_of_scope,
    iter_profiles,
)
from .validation import normalize_metadata, validate_answer_value, validate_source

_resume_answers: contextvars.ContextVar[dict[str, dict[str, Any]] | None] = contextvars.ContextVar(
    "iac_code_resource_selector_resume_answers",
    default=None,
)


@contextmanager
def resource_selection_resume_scope(answers: dict[str, dict[str, Any]]):
    token = _resume_answers.set(answers)
    try:
        yield
    finally:
        _resume_answers.reset(token)


def json_result(value: dict[str, Any], *, error: bool = False) -> ToolResult:
    return ToolResult(content=json.dumps(value, ensure_ascii=False, sort_keys=True), is_error=error)


def profile_output_kind(profile: SelectorProfile, normalized: dict[str, Any]) -> str:
    if profile.output_kind_by_attribute is None:
        return profile.output_kind
    attribute = normalized.get("Attribute")
    return profile.output_kind_by_attribute.get(attribute if isinstance(attribute, str) else "", profile.output_kind)


def profile_interaction(profile: SelectorProfile) -> dict[str, Any]:
    interaction: dict[str, Any] = {
        "kind": profile.interaction_kind,
        "single_tool_call": True,
        "source_policy": "required" if profile.source_selector_id else "forbidden",
    }
    if profile.interaction_steps:
        interaction["steps"] = list(profile.interaction_steps)
    return interaction


def profile_contract(
    profile: SelectorProfile,
    normalized: dict[str, Any],
    *,
    include_metadata_schema: bool = False,
) -> dict[str, Any]:
    source_schema = None
    if profile.source_selector_id:
        source_schema = {
            "type": "object",
            "additionalProperties": False,
            "required": ["selector_id", "value"],
            "properties": {
                "selector_id": {"const": profile.source_selector_id},
                "value": {"type": "string", "maxLength": 1024},
                "association_property_metadata": {"type": "object", "maxProperties": 32},
            },
        }
    result: dict[str, Any] = {
        "selector_id": profile.selector_id,
        "association_property": profile.association_property,
        "title": profile.title,
        "description": profile.description,
        "selection_kind": profile.selection_kind,
        "output_kind": profile_output_kind(profile, normalized),
        "resource_type": profile.resource_type,
        "interaction": profile_interaction(profile),
    }
    if source_schema is not None:
        result["source_schema"] = source_schema
    if include_metadata_schema:
        result["association_property_metadata_schema"] = profile.metadata_schema
    if profile.usage_hint:
        result["usage_hint"] = profile.usage_hint
    return result


def metadata_repair(
    profile: SelectorProfile,
    normalized: dict[str, Any],
    *,
    default_region_provider: Callable[[], str | None] | None,
) -> dict[str, Any] | None:
    properties = profile.metadata_schema.get("properties", {})
    unexpected = sorted(key for key in normalized if key not in properties)
    repaired_input = {key: value for key, value in normalized.items() if key in properties}
    repaired = normalize_metadata(
        profile,
        repaired_input,
        default_region_provider=default_region_provider,
    )
    repair: dict[str, Any] = {}
    if unexpected:
        repair["remove_metadata"] = unexpected
    if repaired.valid:
        repair["retry_metadata"] = repaired.normalized
    return repair or None


def metadata_issue_schema(
    profile: SelectorProfile,
    missing_required: tuple[str, ...],
    invalid_parameters: tuple[dict[str, str], ...],
) -> dict[str, Any] | None:
    properties = profile.metadata_schema.get("properties", {})
    relevant = set(missing_required)
    for error in invalid_parameters:
        key = str(error.get("path") or "").split(".", 1)[0]
        if key in properties:
            relevant.add(key)
    if not relevant:
        return None
    schema: dict[str, Any] = {
        "type": "object",
        "additionalProperties": False,
        "properties": {key: properties[key] for key in sorted(relevant)},
    }
    required = [key for key in profile.metadata_schema.get("required", []) if key in relevant]
    if required:
        schema["required"] = required
    return schema


def normalized_search_text(value: object) -> str:
    return " ".join(part for part in re.split(r"[^\w]+", str(value).lower()) if part)


def profile_search_score(profile: SelectorProfile, query: str) -> int:
    terms = (
        profile.selector_id,
        profile.association_property,
        profile.title,
        profile.description,
        *profile.search_terms,
    )
    normalized_terms = tuple(normalized_search_text(term) for term in terms)
    if query in normalized_terms:
        return 10_000
    query_tokens = set(query.split())
    haystack_tokens = set(" ".join(normalized_terms).split())
    overlap = query_tokens & haystack_tokens
    if not overlap:
        return 0
    return len(overlap) * 100 - max(0, len(query_tokens) - len(overlap))


def metadata_with_source(profile: SelectorProfile, metadata: object, source: object) -> object:
    if not isinstance(source, dict) or profile.source_parameter_key is None:
        return metadata
    source = cast(dict[str, Any], source)
    properties = profile.metadata_schema.get("properties", {})
    merged: dict[str, Any] = {}
    if isinstance(metadata, dict):
        merged.update(cast(dict[str, Any], metadata))
    source_metadata = source.get("association_property_metadata")
    if isinstance(source_metadata, dict):
        merged.update({key: value for key, value in source_metadata.items() if key in properties})
    source_value = source.get("value")
    if isinstance(source_value, str) and profile.source_parameter_key in properties:
        merged[profile.source_parameter_key] = (
            [source_value] if profile.selector_id == "ess.eci_container" else source_value
        )
    return merged


class ResolveCloudResourceSelectorTool(Tool):
    def __init__(self, default_region_provider: Callable[[], str | None] | None = None) -> None:
        self._default_region_provider = default_region_provider

    @property
    def name(self) -> str:
        return "resolve_cloud_resource_selector"

    @property
    def description(self) -> str:
        return (
            "Resolve one Alibaba Cloud AssociationProperty into a supported selector contract. "
            "Prefer this flow when the user explicitly wants to choose one existing Alibaba Cloud resource or "
            "derived value themselves. "
            "Use this before select_cloud_resource when the current contract is not already known. "
            "Do not pre-list candidates with Alibaba Cloud APIs; fall back to them only when this resolver reports "
            "that the selector is unavailable, or when the user asked to list, inspect, or analyze resources. "
            "The default response is compact; request detail_level=full only when optional metadata fields are needed."
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "association_property": {"type": "string", "maxLength": 256},
                "query": {"type": "string", "maxLength": 100},
                "product": {"type": "string", "maxLength": 32},
                "association_property_metadata": {"type": "object", "maxProperties": 32},
                "detail_level": {"type": "string", "enum": ["auto", "full"], "default": "auto"},
            },
            "anyOf": [{"required": ["association_property"]}, {"required": ["query"]}],
        }

    def is_read_only(self, input: dict | None = None) -> bool:
        return True

    async def execute(self, *, tool_input: dict[str, Any], context: ToolContext) -> ToolResult:
        del context
        association_property = tool_input.get("association_property")
        if isinstance(association_property, str):
            profile = get_profile_by_association_property(association_property, include_aliases=True)
            if profile is None:
                return json_result({"status": "not_found", "association_property": association_property})
            return json_result(
                self._resolve_profile(
                    profile,
                    association_property=association_property,
                    metadata=tool_input.get("association_property_metadata"),
                    detail_level=tool_input.get("detail_level"),
                )
            )

        query = normalized_search_text(tool_input.get("query") or "")
        product = normalized_search_text(tool_input.get("product") or "")
        if not query:
            return json_result({"status": "not_found", "query": query})
        matches: list[tuple[int, SelectorProfile]] = []
        for profile in iter_profiles():
            score = profile_search_score(profile, query)
            if score <= 0:
                continue
            product_haystack = normalized_search_text(
                " ".join((profile.selector_id, profile.association_property, profile.title, *profile.search_terms))
            )
            if product and product not in product_haystack.split():
                continue
            matches.append((score, profile))
        matches.sort(key=lambda item: (-item[0], item[1].selector_id))
        if not matches:
            return json_result({"status": "not_found", "query": query})
        exact_matches = [profile for score, profile in matches if score == 10_000]
        if len(exact_matches) == 1:
            profile = exact_matches[0]
            return json_result(
                self._resolve_profile(
                    profile,
                    association_property=profile.association_property,
                    metadata=tool_input.get("association_property_metadata"),
                    detail_level=tool_input.get("detail_level"),
                )
            )
        compatible_matches = [
            (score, profile)
            for score, profile in matches
            if normalize_metadata(
                profile,
                tool_input.get("association_property_metadata"),
                default_region_provider=self._default_region_provider,
            ).valid
        ]
        resolution_pool = compatible_matches or matches
        best_score = resolution_pool[0][0]
        best_matches = [profile for score, profile in resolution_pool if score == best_score]
        if len(best_matches) == 1 and (best_score == 10_000 or len(resolution_pool) == 1):
            profile = best_matches[0]
            return json_result(
                self._resolve_profile(
                    profile,
                    association_property=profile.association_property,
                    metadata=tool_input.get("association_property_metadata"),
                    detail_level=tool_input.get("detail_level"),
                )
            )
        candidates = []
        for _, profile in matches[:10]:
            candidates.append(
                {
                    "selector_id": profile.selector_id,
                    "association_property": profile.association_property,
                    "title": profile.title,
                    "interaction": profile_interaction(profile),
                }
            )
        return json_result({"status": "ambiguous", "candidates": candidates})

    def _resolve_profile(
        self,
        profile: SelectorProfile,
        *,
        association_property: str,
        metadata: object,
        detail_level: object,
    ) -> dict[str, Any]:
        validation = normalize_metadata(
            profile,
            metadata,
            default_region_provider=self._default_region_provider,
        )
        if is_out_of_scope(association_property):
            status = "known_but_out_of_scope"
        elif not profile.enabled:
            status = "unsupported_backend"
        elif not validation.valid:
            status = "selector_contract_invalid"
        else:
            status = "resolved"
        result = profile_contract(
            profile,
            validation.normalized,
            include_metadata_schema=detail_level == "full"
            or status in {"known_but_out_of_scope", "unsupported_backend"},
        )
        result["status"] = status
        if validation.valid:
            result["normalized_metadata"] = validation.normalized
        else:
            result["missing_required"] = list(validation.missing_required)
            result["invalid_parameters"] = list(validation.invalid_parameters)
            repair = metadata_repair(
                profile,
                validation.normalized,
                default_region_provider=self._default_region_provider,
            )
            if repair:
                result["repair"] = repair
            issue_schema = metadata_issue_schema(
                profile,
                validation.missing_required,
                validation.invalid_parameters,
            )
            if issue_schema:
                result["association_property_metadata_schema"] = issue_schema
        if status in {"known_but_out_of_scope", "unsupported_backend"}:
            result["reason"] = profile.unsupported_reason
        if status == "resolved":
            result["next_tool"] = "select_cloud_resource"
        return result


class SelectCloudResourceTool(Tool):
    def __init__(self, default_region_provider: Callable[[], str | None] | None = None) -> None:
        self._default_region_provider = default_region_provider

    @property
    def name(self) -> str:
        return "select_cloud_resource"

    @property
    def description(self) -> str:
        return (
            "Ask the user to choose one supported cloud resource or one derived value. "
            "Pass the stable selector_id and normalized metadata returned by the resolver. "
            "Pass source only when the resolver interaction.source_policy is required. "
            "A canceled result is the user's final decision; do not retry unless the user explicitly asks."
        )

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "required": ["question", "selector_id"],
            "additionalProperties": False,
            "properties": {
                "question": {"type": "string", "maxLength": 500},
                "selector_id": {
                    "type": "string",
                    "maxLength": 128,
                    "pattern": r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$",
                },
                "association_property_metadata": {"type": "object", "maxProperties": 32},
                "source": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["selector_id", "value"],
                    "properties": {
                        "selector_id": {"type": "string", "maxLength": 128},
                        "value": {"type": "string", "maxLength": 1024},
                        "association_property_metadata": {"type": "object", "maxProperties": 32},
                    },
                },
            },
        }

    def needs_event_queue(self) -> bool:
        return True

    def has_unbounded_execution_wait(self, tool_input: dict[str, Any]) -> bool:
        del tool_input
        return True

    def is_read_only(self, input: dict | None = None) -> bool:
        return True

    async def execute(self, *, tool_input: dict[str, Any], context: ToolContext) -> ToolResult:
        profile = get_profile(str(tool_input.get("selector_id") or ""))
        if profile is None or not profile.enabled:
            return json_result(
                {
                    "schema_version": 1,
                    "kind": "cloud_resource_selection",
                    "status": "selector_contract_invalid",
                    "selector_id": tool_input.get("selector_id"),
                },
                error=True,
            )
        source_input = tool_input.get("source")
        target_validation = normalize_metadata(
            profile,
            tool_input.get("association_property_metadata"),
            default_region_provider=self._default_region_provider,
        )
        source, source_error = validate_source(
            profile,
            source_input,
            target_metadata=target_validation.normalized,
            default_region_provider=self._default_region_provider,
        )
        validation = normalize_metadata(
            profile,
            metadata_with_source(profile, tool_input.get("association_property_metadata"), source_input),
            default_region_provider=self._default_region_provider,
        )
        if not validation.valid or source_error:
            result: dict[str, Any] = {
                "schema_version": 1,
                "kind": "cloud_resource_selection",
                "status": source_error or "selector_contract_invalid",
                "selector_id": profile.selector_id,
                "interaction": profile_interaction(profile),
                "missing_required": list(validation.missing_required),
                "invalid_parameters": list(validation.invalid_parameters),
            }
            if source_error:
                if profile.source_selector_id:
                    result["repair"] = {
                        "retry_same_selector": True,
                        "expected_source_selector_id": profile.source_selector_id,
                    }
                else:
                    result["repair"] = {
                        "retry_same_selector": True,
                        "remove_arguments": ["source"],
                    }
            else:
                repair = metadata_repair(
                    profile,
                    validation.normalized,
                    default_region_provider=self._default_region_provider,
                )
                if repair:
                    result["repair"] = repair
            return json_result(result, error=True)
        resume = (_resume_answers.get() or {}).get(context.tool_use_id or "")
        if resume is not None:
            if resume.get("selector_id") != profile.selector_id or resume.get("profile_hash") != PROFILE_HASH:
                return json_result(
                    {"status": "selector_profile_mismatch", "selector_id": profile.selector_id},
                    error=True,
                )
            resumed_event = CloudResourceSelectionEvent(
                tool_use_id=context.tool_use_id or "",
                input_id=str(resume.get("input_id") or ""),
                question=str(tool_input["question"]),
                selector_id=profile.selector_id,
                association_property=profile.association_property,
                output_kind=profile_output_kind(profile, validation.normalized),
                association_property_metadata=validation.normalized,
                source=source,
                profile_hash=PROFILE_HASH,
            )
            response = resume.get("response")
            return selection_result(profile, resumed_event, response if isinstance(response, dict) else None)
        if context.event_queue is None:
            return json_result(
                {
                    "schema_version": 1,
                    "kind": "cloud_resource_selection",
                    "status": "selector_surface_unavailable",
                    "selector_id": profile.selector_id,
                },
                error=True,
            )

        loop = asyncio.get_running_loop()
        response_future: asyncio.Future[dict[str, Any] | None] = loop.create_future()
        input_id = "resource-" + uuid.uuid4().hex
        event = CloudResourceSelectionEvent(
            tool_use_id=context.tool_use_id or input_id,
            input_id=input_id,
            question=str(tool_input["question"]),
            selector_id=profile.selector_id,
            association_property=profile.association_property,
            output_kind=profile_output_kind(profile, validation.normalized),
            association_property_metadata=validation.normalized,
            source=source,
            profile_hash=PROFILE_HASH,
            response_future=response_future,
        )
        await context.event_queue.put(event)
        response = await asyncio.shield(response_future)
        return selection_result(profile, event, response)


def selection_result(
    profile: SelectorProfile,
    event: CloudResourceSelectionEvent,
    response: dict[str, Any] | None,
) -> ToolResult:
    if response is not None and response.get("status") == "selector_surface_unavailable":
        return json_result(
            {
                "schema_version": 1,
                "kind": "cloud_resource_selection",
                "status": "selector_surface_unavailable",
                "selector_id": profile.selector_id,
                "should_retry": False,
            },
            error=True,
        )
    if response is None or response.get("status") == "canceled":
        result: dict[str, Any] = {
            "schema_version": 1,
            "kind": "cloud_resource_selection",
            "status": "canceled",
            "selector_id": profile.selector_id,
            "reason": "user_canceled" if response is not None else "selection_interrupted",
            "should_retry": False,
        }
        if response is not None and response.get("options_empty") is True:
            result["options_empty"] = True
        return json_result(result)
    if response.get("input_id") != event.input_id or response.get("selector_id") != profile.selector_id:
        return json_result({"status": "selector_profile_mismatch", "selector_id": profile.selector_id}, error=True)
    value = response.get("value")
    value_error = validate_answer_value(profile, value, metadata=event.association_property_metadata)
    if value_error:
        return json_result({"status": value_error, "selector_id": profile.selector_id}, error=True)
    label = response.get("label")
    if label is not None and (not isinstance(label, str) or len(label) > 1024):
        return json_result({"status": "selector_value_invalid", "selector_id": profile.selector_id}, error=True)
    result: dict[str, Any] = {
        "schema_version": 1,
        "kind": profile.selection_kind,
        "selector_id": profile.selector_id,
        "association_property": profile.association_property,
        "value_kind": event.output_kind,
        "value": value,
        "label": label or value,
    }
    if profile.resource_type:
        result["resource_type"] = profile.resource_type
    region = event.association_property_metadata.get("RegionId")
    if isinstance(region, str):
        result["region_id"] = region
    if event.source:
        result["source"] = {"selector_id": event.source["selector_id"], "value": event.source["value"]}
    return json_result(result)


def register_resource_selector_tools(
    registry: ToolRegistry,
    *,
    default_region_provider: Callable[[], str | None] | None = None,
) -> None:
    registry.register(ResolveCloudResourceSelectorTool(default_region_provider))
    registry.register(SelectCloudResourceTool(default_region_provider))
