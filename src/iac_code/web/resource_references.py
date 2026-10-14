"""Session-local resource identities, user bindings and durable snapshots.

The caller owns session authorization and validates occurrence sources before
binding. This module never resolves credentials, calls cloud APIs or builds URLs.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import re
import threading
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from iac_code.tools.cloud.resource_references import (
    EVIDENCE_LEVELS,
    RESOURCE_REFERENCE_SCHEMA_VERSION,
    SOURCE_VERSION_FIELDS,
    resource_spec,
    sanitize_resource_reference,
)
from iac_code.utils.state_io import atomic_write_text
from iac_code.web.resource_previews import clean_preview_choice

RESOURCE_REFERENCES_FILENAME = "web-resource-references.json"
DECLARATION_SOURCE_PREFIX = "web-choice:"
logger = logging.getLogger(__name__)


class ResourceRevisionConflictError(ValueError):
    """A resolve request or page belongs to an obsolete snapshot."""


def resource_fact_to_web(value: Any) -> Any:
    """Translate contract keys, preserving case-sensitive cloud field names."""
    if isinstance(value, list):
        return [resource_fact_to_web(item) for item in value]
    if not isinstance(value, dict):
        return value
    result = {}
    for key, item in value.items():
        parts = key.split("_")
        web_key = parts[0] + "".join(part[:1].upper() + part[1:] for part in parts[1:])
        result[web_key] = copy.deepcopy(item) if key == "attributes" else resource_fact_to_web(item)
    return result


def resource_fact_from_web(value: Any) -> Any:
    """Decode our dedicated projection, never arbitrary tool response bodies."""
    if isinstance(value, list):
        return [resource_fact_from_web(item) for item in value]
    if not isinstance(value, dict):
        return value
    return {
        re.sub(r"(?<!^)(?=[A-Z])", "_", key).lower(): (
            copy.deepcopy(item) if key == "attributes" else resource_fact_from_web(item)
        )
        for key, item in value.items()
    }


def _locators(reference: dict[str, Any]) -> set[tuple[str, str, str]]:
    spec = resource_spec(reference["resource_type"])
    assert spec is not None
    return {
        (spec.resource_type, item["identifier_kind"], item["identifier"])
        for item in [reference, *reference.get("aliases", [])]
    }


def _source_identity(source: dict[str, Any]) -> tuple[str, ...]:
    return (
        source.get("tool_use_id") or source.get("message_id") or source.get("source_id") or "",
        source.get("source_path", ""),
        source.get("evidence", ""),
        *(source.get(key, "") for key in SOURCE_VERSION_FIELDS),
        source.get("query_source_digest", ""),
    )


def _same_source(left: dict[str, Any], right: dict[str, Any]) -> bool:
    def origin(source: dict[str, Any]) -> tuple[str, str]:
        owner, path = _source_identity(source)[:2]
        if source.get("query_source_digest") and source.get("query_parent_path"):
            path = source["query_parent_path"]
        return owner, path

    left_ids = {origin(source) for source in left["sources"]}
    return any(origin(source) in left_ids for source in right["sources"])


def _stable_identifier(reference: dict[str, Any]) -> str:
    spec = resource_spec(reference["resource_type"])
    assert spec is not None
    stable_field = spec.stable_field
    if not stable_field and spec.identifier_kind == "id":
        # For ID-addressed resources, the canonical locator is itself the
        # stable object identity.  A display-name alias can be shared by many
        # objects, so it must never merge two different primary IDs.
        stable_field = spec.identifier_field
    if not stable_field:
        return ""
    for item in [reference, *reference.get("aliases", [])]:
        if item["identifier_field"] == stable_field:
            return item["identifier"]
    return reference.get("attributes", {}).get(stable_field, "")


def _observation_time(reference: dict[str, Any]) -> float:
    return max((source.get("observed_at", 0) for source in reference["sources"]), default=0)


def _deleted(reference: dict[str, Any]) -> bool:
    return reference.get("status") == "DELETE_COMPLETE"


def _observation_rank(reference: dict[str, Any], incoming: dict[str, Any]) -> tuple[int, float]:
    """Prefer the recorded observation, not merely its reusable field path.

    Progress snapshots represent the latest frame of a request. Unlike ordinary
    historical results, an untimed progress replay may select the most recent
    matching version. Positive timestamps distinguish repeated identical frames
    before and after a name's deletion/recreation.
    """
    rank = (0, 0.0)
    for source in incoming["sources"]:
        when = source.get("observed_at", 0)
        for known in reference["sources"]:
            if _source_identity(source) != _source_identity(known):
                continue
            observed = known.get("observed_at", 0)
            if when and when == observed:
                score = (3, 0.0)
            elif not when and source.get("source_kind") == "stack_progress":
                score = (2, float(observed))
            elif source.get("source_revision"):
                # Matching content alone cannot select the closest lifecycle.
                score = (2, 0.0) if not when and not observed else (1, 0.0)
            else:
                continue
            rank = max(rank, score)
    return rank


def _status_observation(facts: list[dict[str, Any]]) -> dict[str, Any] | None:
    # A later request that merely mentions a name is not a later status report.
    return max((fact for fact in reversed(facts) if fact.get("status")), key=_observation_time, default=None)


def _compatible(left: dict[str, Any], right: dict[str, Any], *, deleted_at: float) -> bool:
    same_source = _same_source(left, right)
    # An unknown account is not a shared default account. Missing dimensions may
    # only be filled from the exact same source, not a nearby message or row.
    for key in ("account_scope", "console_partition", "region_id"):
        left_value, right_value = left.get(key), right.get(key)
        if left_value and right_value and left_value != right_value:
            return False
        if left_value != right_value and not same_source:
            return False
    if not left.get("account_scope") and not same_source:
        # One API invocation is one unknown credential context, but not one
        # resource object: missing regional/parent fields still require the
        # exact source below. This joins a RAM request name to its own response.
        left_tools = {source["tool_use_id"] for source in left["sources"] if source.get("tool_use_id")}
        if not any(source.get("tool_use_id") in left_tools for source in right["sources"]):
            return False
    spec = resource_spec(left["resource_type"])
    assert spec is not None
    if spec.resource_type == "ALIYUN::OTS::Table" and (
        left.get("stack_id") != right.get("stack_id")
        or left.get("stack_id")
        and left.get("logical_resource_id") != right.get("logical_resource_id")
    ):
        # A matching direct ListTable row corroborates a stack binding, but
        # cannot replace its provenance or keep a retracted template join alive.
        return False
    for key in spec.parent_fields:
        left_value = left["attributes"].get(key)
        right_value = right["attributes"].get(key)
        if left_value != right_value and (left_value and right_value or not same_source):
            return False
    left_stable, right_stable = _stable_identifier(left), _stable_identifier(right)
    if left_stable and right_stable and left_stable != right_stable:
        return False
    if (
        _deleted(left)
        and not _deleted(right)
        and (not same_source or spec.identifier_kind == "name" and _observation_time(right) > deleted_at)
        and _observation_time(right) >= deleted_at
    ):
        # A field path is reusable after a name is deleted. A later existence
        # response is a new lifecycle unless a stable object ID proves otherwise;
        # an input alone does not prove existence.
        if right["evidence"] == "api_response" and not (left_stable and left_stable == right_stable):
            return False
    return True


def _merged_reference(previous: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(previous)
    newer = _observation_time(incoming) >= _observation_time(previous)
    for key in ("account_scope", "console_partition", "region_id", "logical_resource_id", "stack_id"):
        if not merged.get(key) and incoming.get(key):
            merged[key] = incoming[key]
    if incoming.get("status") and (newer or not merged.get("status")):
        merged["status"] = incoming["status"]
    merged["attributes"] = (
        {**merged["attributes"], **incoming["attributes"]}
        if newer
        else {**incoming["attributes"], **merged["attributes"]}
    )
    for alias in incoming.get("aliases", []):
        if alias not in merged["aliases"]:
            merged["aliases"].append(copy.deepcopy(alias))
    if incoming["identifier"] != previous["identifier"] or incoming["identifier_kind"] != previous["identifier_kind"]:
        incoming_locator = {key: incoming[key] for key in ("identifier", "identifier_kind", "identifier_field")}
        previous_locator = {key: previous[key] for key in ("identifier", "identifier_kind", "identifier_field")}
        spec = resource_spec(previous["resource_type"])
        assert spec is not None
        if incoming["identifier_field"] == spec.identifier_field:
            merged.update(incoming_locator)
            if previous_locator not in merged["aliases"]:
                merged["aliases"].append(previous_locator)
        elif incoming_locator not in merged["aliases"]:
            merged["aliases"].append(incoming_locator)
    primary = {key: merged[key] for key in ("identifier", "identifier_kind", "identifier_field")}
    merged["aliases"] = [alias for alias in merged["aliases"] if alias != primary]
    if EVIDENCE_LEVELS[incoming["evidence"]] > EVIDENCE_LEVELS[merged["evidence"]]:
        merged["evidence"] = incoming["evidence"]
    source_positions = {_source_identity(source): index for index, source in enumerate(merged["sources"])}
    for source in incoming["sources"]:
        position = source_positions.get(_source_identity(source))
        if position is None:
            source_positions[_source_identity(source)] = len(merged["sources"])
            merged["sources"].append(copy.deepcopy(source))
        else:
            previous_source = merged["sources"][position]
            # Replay can recover Web-owned message/turn provenance that an
            # older persisted contribution did not contain. Preserve existing
            # fields while accepting that enrichment even when both historical
            # observations intentionally use timestamp zero.
            if source.get("observed_at", 0) >= previous_source.get("observed_at", 0):
                merged["sources"][position] = {**previous_source, **copy.deepcopy(source)}
    return merged


def _semantic_reference(reference: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(reference)
    for source in result["sources"]:
        source.pop("observed_at", None)
    return result


class ResourceReferenceStore:
    """One writer per Web session; observe in-loop, flush off-loop atomically."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.revision = 0
        self.persisted_revision = 0
        self.persistence_status = "saved"
        self.references: dict[str, dict[str, Any]] = {}
        self._contributions: dict[str, list[dict[str, Any]]] = {}
        self.bindings: dict[str, dict[str, Any]] = {}
        self.preview_choices: dict[str, dict[str, Any]] = {}
        self._by_locator: dict[tuple[str, str, str], set[str]] = {}
        self._write_lock = asyncio.Lock()
        self._file_lock = threading.Lock()
        self._closed = False
        self._read_only_file = False
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError, UnicodeError):
            self.persistence_status = "recovery_required"
            return
        if not isinstance(data, dict) or data.get("schema_version") != RESOURCE_REFERENCE_SCHEMA_VERSION:
            self._read_only_file = True
            self.persistence_status = "unsupported_version"
            return
        revision = data.get("revision")
        rows = data.get("references")
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0 or not isinstance(rows, list):
            self.persistence_status = "recovery_required"
            return
        self.revision = self.persisted_revision = revision
        for row in rows:
            clean = sanitize_resource_reference(row)
            reference_id = row.get("reference_id") if isinstance(row, dict) else None
            if clean is None or not isinstance(reference_id, str) or not reference_id.startswith("ref-"):
                continue
            clean["reference_id"] = reference_id
            self.references[reference_id] = clean
            contributions = data.get("contributions", {})
            raw_facts = contributions.get(reference_id) if isinstance(contributions, dict) else None
            self._contributions[reference_id] = (
                [fact for raw in raw_facts if (fact := sanitize_resource_reference(raw)) is not None]
                if isinstance(raw_facts, list)
                else [copy.deepcopy(clean)]
            )
            self._index(clean)
        bindings = data.get("bindings")
        for binding in bindings if isinstance(bindings, list) else []:
            if not isinstance(binding, dict):
                continue
            clean = {
                key: binding[key]
                for key in ("source_key", "identifier", "identifier_kind", "reference_id")
                if isinstance(binding.get(key), str)
            }
            if len(clean) == 4 and self._binding_matches(clean):
                for key in ("source", "source_digest", "group", "context", "declaration_source_id"):
                    if key in binding:
                        clean[key] = copy.deepcopy(binding[key])
                if not self._binding_matches(clean):
                    continue
                self.bindings[self._binding_key(clean)] = clean
        preview_choices = data.get("preview_choices", [])
        for value in preview_choices if isinstance(preview_choices, list) else []:
            if (choice := clean_preview_choice(value)) is not None:
                self.preview_choices[choice["source_key"]] = choice

    def _index(self, reference: dict[str, Any]) -> None:
        for locator in _locators(reference):
            self._by_locator.setdefault(locator, set()).add(reference["reference_id"])

    def _record_contribution(self, reference_id: str, incoming: dict[str, Any]) -> None:
        facts = self._contributions.setdefault(reference_id, [])
        source_keys = {_source_identity(source) for source in incoming["sources"]}
        if all(source.get("source_kind") == "stack_progress" for source in incoming["sources"]):
            # Keep the endpoints of identical progress, not every poll. The
            # first observation locates old replay within this lifecycle; the
            # last observation orders status if a version recurs after another
            # status. An untimed latest snapshot supplies neither new endpoint.
            identical = [
                index for index, fact in enumerate(facts) if _semantic_reference(fact) == _semantic_reference(incoming)
            ]
            if identical:
                when = _observation_time(incoming)
                timed = [index for index in identical if _observation_time(facts[index]) > 0]
                if not when:
                    return
                if not timed:
                    facts[identical[0]] = copy.deepcopy(incoming)
                    return
                first = min(timed, key=lambda index: _observation_time(facts[index]))
                last = max(timed, key=lambda index: _observation_time(facts[index]))
                if _observation_time(facts[first]) <= when <= _observation_time(facts[last]):
                    return
                if first == last:
                    facts.append(copy.deepcopy(incoming))
                else:
                    facts[first if when < _observation_time(facts[first]) else last] = copy.deepcopy(incoming)
                return
        for index, fact in enumerate(facts):
            if source_keys == {_source_identity(source) for source in fact["sources"]} and _locators(fact).intersection(
                _locators(incoming)
            ):
                facts[index] = _merged_reference(fact, incoming)
                return
        facts.append(copy.deepcopy(incoming))

    def _with_related_api_context(self, incoming: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Add provenance-bound parent fields that a child list cannot return.

        NAS rule rows and Terraform group IDs do not contain Classic/Vpc type.
        Join only an exact group observed in the same turn and cloud context.
        Keep the base child contribution separate so retracting the detail
        source removes its enrichment without removing the child observation.
        """
        parent_facts = [
            fact
            for facts in self._contributions.values()
            for fact in facts
            if fact["resource_type"] == "ALIYUN::NAS::AccessGroup"
        ]
        parent_facts.extend(fact for fact in incoming if fact["resource_type"] == "ALIYUN::NAS::AccessGroup")
        if not parent_facts:
            return incoming

        children = list(incoming)
        if any(fact["resource_type"] == "ALIYUN::NAS::AccessGroup" for fact in incoming):
            children.extend(
                fact
                for facts in self._contributions.values()
                for fact in facts
                if fact["resource_type"] == "alicloud_nas_access_group"
                and not fact["attributes"].get("AccessGroupType")
            )
        result: list[dict[str, Any]] = []
        for child in children:
            result.append(child)
            if child["resource_type"] not in {"ALIYUN::NAS::AccessRule", "alicloud_nas_access_group"}:
                continue
            if child["evidence"] != "api_response":
                continue
            attributes = child["attributes"]
            group_name = attributes.get("AccessGroupName")
            file_system_type = attributes.get("FileSystemType")
            child_turns = {source.get("turn_id") for source in child["sources"] if source.get("turn_id")}
            if not group_name or not file_system_type or not child_turns or attributes.get("AccessGroupType"):
                continue

            matches = []
            for parent in parent_facts:
                parent_attributes = parent["attributes"]
                parent_turns = {source.get("turn_id") for source in parent["sources"] if source.get("turn_id")}
                if (
                    parent["evidence"] == "api_response"
                    and (
                        child["resource_type"] != "alicloud_nas_access_group"
                        or any(
                            source.get("product") == "nas"
                            and source.get("action") == "DescribeAccessGroups"
                            and source.get("evidence") == "api_response"
                            for source in parent["sources"]
                        )
                    )
                    and child_turns.intersection(parent_turns)
                    and all(
                        child.get(field) and child.get(field) == parent.get(field)
                        for field in ("account_scope", "console_partition", "region_id")
                    )
                    and group_name
                    in {
                        parent["identifier"],
                        parent_attributes.get("AccessGroupName"),
                    }
                    and parent_attributes.get("FileSystemType") == file_system_type
                    and parent_attributes.get("AccessGroupType")
                ):
                    matches.append(parent)
            access_group_types = {parent["attributes"]["AccessGroupType"] for parent in matches}
            if len(access_group_types) != 1:
                continue

            parent = max(matches, key=_observation_time)
            enriched = copy.deepcopy(child)
            enriched["attributes"]["AccessGroupType"] = next(iter(access_group_types))
            source_keys = {_source_identity(source) for source in enriched["sources"]}
            for source in parent["sources"]:
                if _source_identity(source) not in source_keys:
                    enriched["sources"].append(copy.deepcopy(source))
            result.append(enriched)
        return result

    def _lifecycle_bounds(self, reference: dict[str, Any]) -> tuple[float, float | None]:
        facts = self._contributions.get(reference["reference_id"], [reference])
        status = _status_observation(facts)
        times = [_observation_time(fact) for fact in facts if fact["evidence"] == "api_response"]
        start = min((when for when in times if when > 0), default=0)
        end = _observation_time(status) if status and _deleted(status) else None
        return start, end

    @staticmethod
    def _restore_observed_status(reference: dict[str, Any], facts: list[dict[str, Any]]) -> None:
        if status := _status_observation(facts):
            reference["status"] = status["status"]

    def retain_sources(self, is_visible: Callable[[dict[str, Any]], bool]) -> dict[str, Any] | None:
        """Retract hidden sources and their attributes, not just provenance labels.

        Contributions are retained separately because a merged reference alone
        cannot prove which deleted source supplied an alias or parent attribute.
        """
        changed, removed = [], []
        for reference_id, previous in list(self.references.items()):
            contributions = self._contributions.get(reference_id, [previous])
            visible = [fact for fact in contributions if all(is_visible(source) for source in fact["sources"])]
            if len(visible) == len(contributions):
                continue
            if not visible:
                self.references.pop(reference_id)
                self._contributions.pop(reference_id, None)
                removed.append(reference_id)
                continue
            updated = copy.deepcopy(visible[0])
            for fact in visible[1:]:
                updated = _merged_reference(updated, fact)
            self._restore_observed_status(updated, visible)
            updated["reference_id"] = reference_id
            self.references[reference_id] = updated
            self._contributions[reference_id] = visible
            changed.append(updated)
        if not changed and not removed:
            return None
        self._by_locator.clear()
        for reference in self.references.values():
            self._index(reference)
        removed_bindings = [value for value in self.bindings.values() if not self._binding_matches(value)]
        for binding in removed_bindings:
            del self.bindings[self._binding_key(binding)]
        base_revision = self.revision
        self.revision += 1
        if not self._read_only_file:
            self.persistence_status = "pending"
        return {
            "schemaVersion": RESOURCE_REFERENCE_SCHEMA_VERSION,
            "baseRevision": base_revision,
            "revision": self.revision,
            "upserts": resource_fact_to_web(changed),
            "removedReferenceIds": removed,
            "bindings": [],
            "removedBindings": resource_fact_to_web(removed_bindings),
        }

    def reconcile(
        self, incoming: list[dict[str, Any]], is_current: Callable[[dict[str, Any]], bool]
    ) -> dict[str, Any] | None:
        """Replace source revisions without publishing a transient union.

        Match incoming observations before retracting obsolete contributions so
        compatible objects retain their reference IDs and surviving bindings.
        No await or publication can expose the intermediate state.
        """
        if self._closed:
            return None
        previous = self.references.copy()
        bindings = self.bindings.copy()
        revision, status = self.revision, self.persistence_status
        self.observe(incoming)
        self.retain_sources(is_current)
        self.retain_bindings(lambda binding: True)
        changed = [
            value
            for key, value in self.references.items()
            if key not in previous or _semantic_reference(value) != _semantic_reference(previous[key])
        ]
        removed = list(previous.keys() - self.references.keys())
        removed_bindings = [value for key, value in bindings.items() if key not in self.bindings]
        self.revision = revision
        if not changed and not removed and not removed_bindings:
            self.persistence_status = status
            return None
        self.revision += 1
        if not self._read_only_file:
            self.persistence_status = "pending"
        return {
            "schemaVersion": RESOURCE_REFERENCE_SCHEMA_VERSION,
            "baseRevision": revision,
            "revision": self.revision,
            "upserts": resource_fact_to_web(changed),
            "removedReferenceIds": removed,
            "bindings": [],
            "removedBindings": resource_fact_to_web(removed_bindings),
        }

    def observe(
        self, references: list[dict[str, Any]], *, restored_declaration_id: str | None = None
    ) -> dict[str, Any] | None:
        if self._closed:
            return None
        base_revision = self.revision
        changed: dict[str, dict[str, Any]] = {}
        cleaned = [fact for raw in references if (fact := sanitize_resource_reference(raw)) is not None]
        for incoming in self._with_related_api_context(cleaned):
            candidate_ids: set[str] = set()
            for locator in _locators(incoming):
                candidate_ids.update(self._by_locator.get(locator, ()))
            candidates = [
                self.references[reference_id]
                for reference_id in sorted(candidate_ids)
                if _compatible(
                    self.references[reference_id],
                    incoming,
                    deleted_at=self._lifecycle_bounds(self.references[reference_id])[1] or 0,
                )
            ]
            # Stable object identity outranks a provisional same-name record.
            stable_id = _stable_identifier(incoming)
            stable_matches = [
                candidate for candidate in candidates if stable_id and _stable_identifier(candidate) == stable_id
            ]
            if stable_matches:
                candidates = stable_matches
            same_sources = [candidate for candidate in candidates if _same_source(candidate, incoming)]
            if same_sources:
                candidates = same_sources
            spec = resource_spec(incoming["resource_type"])
            assert spec is not None
            when = _observation_time(incoming)
            if spec.identifier_kind == "name" and when > 0 and not stable_matches:
                # An old DELETE_COMPLETE observation must not absorb a later
                # deletion of an already-observed replacement under the same
                # name/path. Conversely, old replay cannot delete that new object.
                bounded = [
                    candidate
                    for candidate in candidates
                    if (bounds := self._lifecycle_bounds(candidate))[0] > 0
                    and bounds[0] <= when
                    and (bounds[1] is None or when <= bounds[1])
                ]
                if bounded:
                    candidates = bounded
            ranks = [(candidate, _observation_rank(candidate, incoming)) for candidate in candidates]
            best = max((rank for _, rank in ranks), default=(0, 0.0))
            if best[0]:
                candidates = [candidate for candidate, rank in ranks if rank == best]
            if len(candidates) == 1:
                previous = candidates[0]
                updated = _merged_reference(previous, incoming)
                reference_id = previous["reference_id"]
                self._record_contribution(reference_id, incoming)
                self._restore_observed_status(updated, self._contributions[reference_id])
                self.references[reference_id] = updated
                if _semantic_reference(previous) == _semantic_reference(updated):
                    continue
            else:
                reference_id = "ref-" + uuid.uuid4().hex
                # Only a revalidated dormant user declaration may restore its
                # old handle. Never overwrite an occupied ID or an API object.
                if (
                    len(references) == 1
                    and isinstance(restored_declaration_id, str)
                    and re.fullmatch(r"ref-[0-9a-f]{32}", restored_declaration_id)
                    and restored_declaration_id not in self.references
                    and incoming["evidence"] == "user_context"
                    and all(
                        source.get("source_id", "").startswith(DECLARATION_SOURCE_PREFIX)
                        for source in incoming["sources"]
                    )
                ):
                    reference_id = restored_declaration_id
                updated = {**incoming, "reference_id": reference_id}
                self.references[reference_id] = updated
                self._record_contribution(reference_id, incoming)
            self._index(updated)
            changed[reference_id] = updated
        if not changed:
            return None
        self.revision += 1
        if not self._read_only_file:
            self.persistence_status = "pending"
        return {
            "schemaVersion": RESOURCE_REFERENCE_SCHEMA_VERSION,
            "baseRevision": base_revision,
            "revision": self.revision,
            "upserts": resource_fact_to_web(list(changed.values())),
            "removedReferenceIds": [],
            "bindings": [],
        }

    @staticmethod
    def _binding_key(binding: dict[str, Any]) -> str:
        return json.dumps(
            [binding["source_key"], binding["identifier_kind"], binding["identifier"]], ensure_ascii=False
        )

    def _binding_matches(self, binding: dict[str, Any]) -> bool:
        reference = self.references.get(binding["reference_id"])
        if reference is None or not any(
            item["identifier"] == binding["identifier"] and item["identifier_kind"] == binding["identifier_kind"]
            for item in [reference, *reference["aliases"]]
        ):
            return False
        context = binding.get("context")
        if context is not None:
            if not isinstance(context, dict):
                return False
            spec = resource_spec(reference["resource_type"])
            assert spec is not None
            for field in ("account_scope", "console_partition", "region_id"):
                if reference.get(field) and context.get(field) and reference[field] != context[field]:
                    return False
            attributes = context.get("attributes", {})
            if not isinstance(attributes, dict):
                return False
            for field in spec.parent_fields:
                if reference["attributes"].get(field) and attributes.get(field) != reference["attributes"][field]:
                    if attributes.get(field):
                        return False
        return True

    def retain_bindings(self, is_current: Callable[[dict[str, Any]], bool]) -> dict[str, Any] | None:
        """Revoke choices whose source version or selected identity has changed."""
        if self._closed:
            return None
        removed = []
        for key, binding in list(self.bindings.items()):
            if not self._binding_matches(binding) or not is_current(binding):
                removed.append(resource_fact_to_web(binding))
                del self.bindings[key]
        if not removed:
            return None
        base_revision = self.revision
        self.revision += 1
        if not self._read_only_file:
            self.persistence_status = "pending"
        return {
            "schemaVersion": RESOURCE_REFERENCE_SCHEMA_VERSION,
            "baseRevision": base_revision,
            "revision": self.revision,
            "upserts": [],
            "removedReferenceIds": [],
            "bindings": [],
            "removedBindings": removed,
        }

    def bind(
        self,
        *,
        source_key: str,
        identifier: str,
        identifier_kind: str,
        reference_id: str,
        expected_revision: int,
        provenance: dict[str, Any] | None = None,
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Persist a caller-validated source selection, not a session-wide default."""
        if self._closed:
            raise ValueError("resource_session_closed")
        if expected_revision != self.revision:
            raise ResourceRevisionConflictError("resource_revision_conflict")
        binding: dict[str, Any] = {
            "source_key": source_key,
            "identifier": identifier,
            "identifier_kind": identifier_kind,
            "reference_id": reference_id,
        }
        if provenance:
            binding.update({key: copy.deepcopy(provenance[key]) for key in ("source", "source_digest", "group")})
            if "declaration_source_id" in provenance:
                binding["declaration_source_id"] = provenance["declaration_source_id"]
        if context:
            binding["context"] = copy.deepcopy(context)
        if not source_key or not self._binding_matches(binding):
            raise ValueError("resource_binding_invalid")
        key = self._binding_key(binding)
        if self.bindings.get(key) == binding:
            return None
        self.bindings[key] = binding
        base_revision = self.revision
        self.revision += 1
        if not self._read_only_file:
            self.persistence_status = "pending"
        return {
            "schemaVersion": RESOURCE_REFERENCE_SCHEMA_VERSION,
            "baseRevision": base_revision,
            "revision": self.revision,
            "upserts": [],
            "removedReferenceIds": [],
            "bindings": [resource_fact_to_web(binding)],
        }

    def update_preview_choices(self, choices: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
        """Save dormant metadata with the same writer, never in public snapshots."""
        if self._closed:
            return None
        clean = {}
        for value in choices.values():
            if (choice := clean_preview_choice(value)) is not None:
                clean[choice["source_key"]] = choice
        if clean == self.preview_choices:
            return None
        self.preview_choices = clean
        base_revision = self.revision
        self.revision += 1
        if not self._read_only_file:
            self.persistence_status = "pending"
        return {
            "schemaVersion": RESOURCE_REFERENCE_SCHEMA_VERSION,
            "baseRevision": base_revision,
            "revision": self.revision,
            "upserts": [],
            "removedReferenceIds": [],
            "bindings": [],
        }

    def snapshot(self, *, cursor: str | None = None, limit: int = 500) -> dict[str, Any]:
        if not 1 <= limit <= 2000:
            raise ValueError("resource_page_size_invalid")
        offset = 0
        if cursor:
            try:
                revision, offset = (int(part) for part in cursor.split(":"))
            except (TypeError, ValueError) as error:
                raise ValueError("resource_cursor_invalid") from error
            if revision != self.revision:
                raise ResourceRevisionConflictError("resource_revision_conflict")
            if offset < 0 or offset > len(self.references):
                raise ValueError("resource_cursor_invalid")
        references = list(self.references.values())
        end = offset + limit
        return {
            "schemaVersion": RESOURCE_REFERENCE_SCHEMA_VERSION,
            "revision": self.revision,
            "items": resource_fact_to_web(references[offset:end]),
            "bindings": resource_fact_to_web(list(self.bindings.values())) if offset == 0 else [],
            "nextCursor": f"{self.revision}:{end}" if end < len(references) else None,
            "persistenceStatus": self.persistence_status,
        }

    def close_for_delete(self) -> None:
        """Fence queued and active thread writes before session storage is removed."""
        with self._file_lock:
            self._closed = True

    def _write_snapshot(self, data: str) -> bool:
        with self._file_lock:
            if self._closed:
                return False
            atomic_write_text(self.path, data)
            return True

    async def flush(self) -> bool:
        if self._read_only_file or self._closed:
            return False
        async with self._write_lock:
            if self.persisted_revision == self.revision and self.persistence_status == "saved":
                return True
            revision = self.revision
            data = json.dumps(
                {
                    "schema_version": RESOURCE_REFERENCE_SCHEMA_VERSION,
                    "revision": revision,
                    "references": list(self.references.values()),
                    "contributions": self._contributions,
                    "bindings": list(self.bindings.values()),
                    "preview_choices": list(self.preview_choices.values()),
                },
                ensure_ascii=False,
                allow_nan=False,
            )
            try:
                if not await asyncio.to_thread(self._write_snapshot, data):
                    return False
            except OSError:
                self.persistence_status = "write_failed"
                logger.warning("Resource reference snapshot write failed")
                return False
            self.persisted_revision = revision
            self.persistence_status = "saved" if self.revision == revision else "pending"
            return self.persisted_revision == self.revision
