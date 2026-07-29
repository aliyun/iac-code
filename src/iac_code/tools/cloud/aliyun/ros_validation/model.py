"""Core immutable models for ROS validation diagnostics and source locations."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from dataclasses import dataclass, field, replace
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping, TypeAlias

from iac_code.i18n import _


class Severity(str, Enum):
    ERROR = "ERROR"
    WARNING = "WARNING"
    LIMITATION = "LIMITATION"


class Category(str, Enum):
    COMPATIBILITY = "COMPATIBILITY"
    QUALITY = "QUALITY"
    LIMITATION = "LIMITATION"


class ScalarKind(str, Enum):
    STRING = "String"
    BINARY = "Binary"
    INTEGER = "Integer"
    NUMBER = "Number"
    BOOLEAN = "Boolean"
    NULL = "Null"


@dataclass(frozen=True)
class MappingKeySegment:
    key_kind: ScalarKind
    value: Any
    occurrence_ordinal: int = 0


@dataclass(frozen=True)
class SequenceIndexSegment:
    index: int


@dataclass(frozen=True)
class SyntheticSegment:
    transform_kind: str
    argument_ordinal: int


PathSegment: TypeAlias = MappingKeySegment | SequenceIndexSegment | SyntheticSegment
RosPath: TypeAlias = tuple[PathSegment, ...]


@dataclass(frozen=True)
class SourceSpan:
    line: int
    column: int
    end_line: int
    end_column: int
    synthetic: bool = False


@dataclass(frozen=True)
class RelatedLocation:
    label: str
    source_span: SourceSpan | None
    path: RosPath = ()


@dataclass(frozen=True)
class PositionedNode:
    node_id: str
    path: RosPath
    value: object
    span: SourceSpan
    origin_node_ids: tuple[str, ...] = ()
    related_node_ids: tuple[str, ...] = ()


def scalar_kind(value: Any) -> ScalarKind:
    if value is None:
        return ScalarKind.NULL
    if isinstance(value, bool):
        return ScalarKind.BOOLEAN
    if isinstance(value, int):
        return ScalarKind.INTEGER
    if isinstance(value, float):
        return ScalarKind.NUMBER
    if isinstance(value, bytes):
        return ScalarKind.BINARY
    return ScalarKind.STRING


def mapping_segment(value: Any, occurrence: int = 0) -> MappingKeySegment:
    return MappingKeySegment(scalar_kind(value), value, occurrence)


def _safe_scalar(value: Any, kind: ScalarKind) -> str:
    if kind == ScalarKind.BINARY:
        raw = bytes(value)
        return "sha256={},len={}".format(hashlib.sha256(raw).hexdigest()[:8], len(raw))
    if kind == ScalarKind.NUMBER and isinstance(value, float) and not math.isfinite(value):
        if math.isnan(value):
            return "NAN"
        return "POSITIVE_INFINITY" if value > 0 else "NEGATIVE_INFINITY"
    if kind == ScalarKind.NULL:
        return "null"
    return sanitize_text(str(value), limit=96)


_SENSITIVE = re.compile(r"password|passwd|secret|token|credential|access.?key|private.?key|noecho", re.IGNORECASE)
_ACCESS_KEY = re.compile(r"\b(?:AKIA[0-9A-Z]{16}|LTAI[0-9A-Za-z]{12,})\b")
_ENTROPY_TOKEN = re.compile(r"[A-Za-z0-9+/=_-]{24,}")
_TEXT_TOKEN = re.compile(r"[A-Za-z0-9+/=_:.-]+")
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069]")


def _contains_high_entropy_token(value: str) -> bool:
    for match in _ENTROPY_TOKEN.finditer(value):
        token = match.group(0)
        counts = Counter(token)
        entropy = -sum((count / len(token)) * math.log2(count / len(token)) for count in counts.values())
        character_classes = sum(
            any(predicate(character) for character in token)
            for predicate in (str.islower, str.isupper, str.isdigit, lambda item: item in "+/=_-")
        )
        if entropy >= 4.0 and (character_classes >= 3 or len(token) >= 40):
            return True
    return False


def sanitize_text(value: str, *, limit: int = 512) -> str:
    value = _CONTROL.sub("?", value).replace("\r", "?").replace("\n", "?")
    if len(value) > 160:
        digest = hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()[:8]
        return "<redacted:sha256-{}>".format(digest)

    def redact_token(match: re.Match[str]) -> str:
        token = match.group(0)
        if not (_SENSITIVE.search(token) or _ACCESS_KEY.search(token) or _contains_high_entropy_token(token)):
            return token
        digest = hashlib.sha256(token.encode("utf-8", errors="replace")).hexdigest()[:8]
        return "<redacted:sha256-{}>".format(digest)

    return _TEXT_TOKEN.sub(redact_token, value)[:limit]


def _display_string_scalar(value: Any) -> str:
    raw = str(value)
    safe = sanitize_text(raw, limit=96)
    if safe == raw:
        return safe
    digest = hashlib.sha256(raw.encode("utf-8", errors="surrogatepass")).hexdigest()[:8]
    if "sha256-{}".format(digest) in safe:
        return safe
    return "{}<sha256:{}>".format(safe, digest)


def _identity_scalar(value: Any, kind: ScalarKind) -> str:
    if kind != ScalarKind.STRING:
        return _safe_scalar(value, kind)
    raw = str(value)
    safe = sanitize_text(raw, limit=96)
    if safe == raw:
        return safe
    digest = hashlib.sha256(raw.encode("utf-8", errors="surrogatepass")).hexdigest()
    return "{}#sha256={}".format(safe, digest)


def path_identity(path: RosPath) -> tuple[str, ...]:
    result: list[str] = []
    for segment in path:
        if isinstance(segment, MappingKeySegment):
            result.append(
                "k:{}:{}:{}".format(
                    segment.key_kind.value,
                    _identity_scalar(segment.value, segment.key_kind),
                    segment.occurrence_ordinal,
                )
            )
        elif isinstance(segment, SequenceIndexSegment):
            result.append("i:{}".format(segment.index))
        else:
            result.append("s:{}:{}".format(segment.transform_kind, segment.argument_ordinal))
    return tuple(result)


def display_path(path: RosPath) -> str:
    json_compatible = all(
        not isinstance(segment, MappingKeySegment) or segment.key_kind == ScalarKind.STRING for segment in path
    )
    result = "$"
    for segment in path:
        if isinstance(segment, SequenceIndexSegment):
            result += "[{}]".format(segment.index)
        elif isinstance(segment, SyntheticSegment):
            result += "[@{}:{}]".format(segment.transform_kind, segment.argument_ordinal)
        elif json_compatible:
            key = _display_string_scalar(segment.value)
            if re.fullmatch(r"[A-Za-z_$][A-Za-z0-9_$:-]*", key):
                result += ".{}".format(key)
            else:
                result += "[{}]".format(json.dumps(key, ensure_ascii=False))
        else:
            result += "[@key({},{},occ={})]".format(
                segment.key_kind.value,
                _safe_scalar(segment.value, segment.key_kind),
                segment.occurrence_ordinal,
            )
    return result


def path_kind(path: RosPath) -> str:
    return (
        "JSON_PATH"
        if all(not isinstance(segment, MappingKeySegment) or segment.key_kind == ScalarKind.STRING for segment in path)
        else "ROS_PATH"
    )


def _safe_path_value(segment: MappingKeySegment) -> Any:
    if segment.key_kind == ScalarKind.BINARY:
        raw = bytes(segment.value)
        return {"kind": "Binary", "byte_length": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
    if segment.key_kind == ScalarKind.NUMBER and isinstance(segment.value, float) and not math.isfinite(segment.value):
        return {
            "kind": "Number",
            "finiteness": (
                "NAN"
                if math.isnan(segment.value)
                else "POSITIVE_INFINITY"
                if segment.value > 0
                else "NEGATIVE_INFINITY"
            ),
        }
    if segment.key_kind == ScalarKind.STRING:
        return _display_string_scalar(segment.value)
    return segment.value


def path_segments(path: RosPath) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for segment in path:
        if isinstance(segment, MappingKeySegment):
            result.append(
                {
                    "kind": "MAPPING_KEY",
                    "key_kind": segment.key_kind.value,
                    "value": _safe_path_value(segment),
                    "occurrence_ordinal": segment.occurrence_ordinal,
                }
            )
        elif isinstance(segment, SequenceIndexSegment):
            result.append({"kind": "SEQUENCE_INDEX", "index": segment.index})
        else:
            result.append(
                {
                    "kind": "SYNTHETIC",
                    "transform_kind": sanitize_text(segment.transform_kind, limit=96),
                    "argument_ordinal": segment.argument_ordinal,
                }
            )
    return result


@dataclass(frozen=True)
class SourceMap:
    nodes: Mapping[tuple[str, ...], PositionedNode]
    occurrences: tuple[PositionedNode, ...] = ()

    def span_for(self, path: RosPath) -> SourceSpan | None:
        node = self.nodes.get(path_identity(path))
        return node.span if node is not None else None

    def node_for(self, path: RosPath) -> PositionedNode | None:
        candidate = path
        while True:
            node = self.nodes.get(path_identity(candidate))
            if node is not None:
                return node
            if not candidate:
                return None
            candidate = candidate[:-1]

    def node_by_id(self, node_id: str) -> PositionedNode | None:
        return next((node for node in self.occurrences if node.node_id == node_id), None)


@dataclass(frozen=True)
class ParsedTemplate:
    data: Any
    source_map: SourceMap
    source_kind: str
    text: str


@dataclass(frozen=True)
class Diagnostic:
    diagnostic_id: str
    code: str
    severity: Severity
    category: Category
    summary: str
    detail: str
    path: RosPath = ()
    primary_node_id: str | None = None
    subject: str | None = None
    dedup_key: tuple[str, ...] = ()
    source_span: SourceSpan | None = None
    related_locations: tuple[RelatedLocation, ...] = ()
    expected: str | None = None
    actual: str | None = None
    suggestion: str | None = None


def diagnostic_id(
    code: str,
    path: RosPath,
    subject: str | None,
    stable_args: tuple[str, ...],
    *,
    occurrence_node_id: str | None = None,
) -> str:
    occurrence = occurrence_node_id or "\x1e".join(path_identity(path))
    payload = "\x1f".join((code, occurrence, subject or "", *stable_args))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def make_diagnostic(
    *,
    code: str,
    severity: Severity,
    category: Category,
    summary: str,
    detail: str,
    path: RosPath = (),
    source_map: SourceMap | None = None,
    subject: str | None = None,
    stable_args: tuple[str, ...] = (),
    expected: str | None = None,
    actual: str | None = None,
    suggestion: str | None = None,
    related_locations: tuple[RelatedLocation, ...] = (),
) -> Diagnostic:
    node = source_map.node_for(path) if source_map is not None else None
    if not related_locations and node is not None and source_map is not None:
        related_locations = tuple(
            RelatedLocation(_("origin"), origin.span, origin.path)
            for origin_id in node.origin_node_ids
            if (origin := source_map.node_by_id(origin_id)) is not None
        )
    identifier = diagnostic_id(code, path, subject, stable_args, occurrence_node_id=node.node_id if node else None)
    dedup_path = (node.node_id,) if node else path_identity(path)
    return Diagnostic(
        diagnostic_id=identifier,
        code=code,
        severity=severity,
        category=category,
        summary=sanitize_text(summary, limit=240),
        detail=sanitize_text(detail, limit=1200),
        path=path,
        primary_node_id=node.node_id if node else None,
        subject=subject,
        dedup_key=(code, *dedup_path, subject or "", *stable_args),
        source_span=node.span if node else None,
        related_locations=related_locations,
        expected=sanitize_text(expected, limit=240) if expected else None,
        actual=sanitize_text(actual, limit=240) if actual else None,
        suggestion=sanitize_text(suggestion, limit=480) if suggestion else None,
    )


_SEVERITY_RANK = {Severity.ERROR: 0, Severity.WARNING: 1, Severity.LIMITATION: 2}


@dataclass(frozen=True)
class ValidationReport:
    diagnostics: tuple[Diagnostic, ...]
    error_count: int
    warning_count: int
    counts_by_code: Mapping[str, int]
    analysis_incomplete: bool = False

    @property
    def has_errors(self) -> bool:
        return self.error_count > 0

    @property
    def limitation_count(self) -> int:
        return sum(item.severity == Severity.LIMITATION for item in self.diagnostics)

    def to_dict(self) -> dict[str, Any]:
        def span_dict(span: SourceSpan | None) -> dict[str, Any] | None:
            if span is None:
                return None
            return {
                "line": span.line,
                "column": span.column,
                "end_line": span.end_line,
                "end_column": span.end_column,
                "synthetic": span.synthetic,
            }

        def related_dict(location: RelatedLocation) -> dict[str, Any]:
            return {
                "label": sanitize_text(location.label, limit=96),
                "source_span": span_dict(location.source_span),
                "path": display_path(location.path),
                "path_kind": path_kind(location.path),
                "path_segments": path_segments(location.path),
            }

        return {
            "error_count": self.error_count,
            "warning_count": self.warning_count,
            "limitation_count": self.limitation_count,
            "analysis_incomplete": self.analysis_incomplete,
            "counts_by_code": dict(self.counts_by_code),
            "diagnostics": [
                {
                    "diagnostic_id": item.diagnostic_id,
                    "code": item.code,
                    "severity": item.severity.value,
                    "category": item.category.value,
                    "summary": item.summary,
                    "detail": item.detail,
                    "line": item.source_span.line if item.source_span else None,
                    "column": item.source_span.column if item.source_span else None,
                    "end_line": item.source_span.end_line if item.source_span else None,
                    "end_column": item.source_span.end_column if item.source_span else None,
                    "synthetic": item.source_span.synthetic if item.source_span else False,
                    "path": display_path(item.path),
                    "path_kind": path_kind(item.path),
                    "path_segments": path_segments(item.path),
                    "related_locations": [related_dict(location) for location in item.related_locations],
                    "expected": item.expected,
                    "actual": item.actual,
                    "suggestion": item.suggestion,
                }
                for item in self.diagnostics
            ],
        }

    @classmethod
    def build(cls, diagnostics: list[Diagnostic], *, analysis_incomplete: bool = False) -> ValidationReport:
        unique: dict[tuple[str, ...], Diagnostic] = {}
        for item in diagnostics:
            key = item.dedup_key or (item.diagnostic_id,)
            existing = unique.get(key)
            if existing is None:
                unique[key] = item
                continue
            related_locations = list(existing.related_locations)
            for location in item.related_locations:
                if not any(
                    candidate.path == location.path and candidate.source_span == location.source_span
                    for candidate in related_locations
                ):
                    related_locations.append(location)
            if len(related_locations) != len(existing.related_locations):
                unique[key] = replace(existing, related_locations=tuple(related_locations))

        def sort_key(item: Diagnostic) -> tuple[Any, ...]:
            if item.source_span is not None:
                return (
                    0,
                    item.source_span.line,
                    item.source_span.column,
                    _SEVERITY_RANK[item.severity],
                    item.code,
                    item.diagnostic_id,
                )
            return (
                1,
                path_identity(item.path),
                _SEVERITY_RANK[item.severity],
                item.code,
                item.diagnostic_id,
            )

        ordered = tuple(sorted(unique.values(), key=sort_key))
        counts: dict[str, int] = {}
        for item in ordered:
            counts[item.code] = counts.get(item.code, 0) + 1
        return cls(
            diagnostics=ordered,
            error_count=sum(item.severity == Severity.ERROR for item in ordered),
            warning_count=sum(item.severity == Severity.WARNING for item in ordered),
            counts_by_code=MappingProxyType(counts),
            analysis_incomplete=analysis_incomplete,
        )


@dataclass(frozen=True)
class MaterializedTemplateSource:
    # Stage-0 validation owns the user-facing type diagnostic, so adapters may
    # pass an untrusted non-String value without lying to the type checker.
    text: Any
    kind: str = "INLINE"
    origin: str = "TemplateBody"
    origin_kind: str = "SOURCE_TEXT"


class TemplateSemanticMode(str, Enum):
    STACK = "STACK"
    MODULE_CONSUMER = "MODULE_CONSUMER"
    MODULE_REGISTRATION = "MODULE_REGISTRATION"


class EvaluationMode(str, Enum):
    DEPLOYMENT = "DEPLOYMENT"
    QUERY_PARAM = "QUERY_PARAM"
    INQUIRY = "INQUIRY"
    LOCAL_DATASOURCE = "LOCAL_DATASOURCE"


@dataclass(frozen=True)
class TrustedRosAccountContext:
    tenant_id: str
    site_owner: str
    production_account_id: str
    provenance: str


@dataclass(frozen=True)
class RequestValidationContext:
    action: str
    semantic_mode: TemplateSemanticMode = TemplateSemanticMode.STACK
    evaluation_mode: EvaluationMode = EvaluationMode.DEPLOYMENT
    source_kind: str = "INLINE"
    source_fields: frozenset[str] = frozenset()
    mode: str | None = None
    entity_type: str | None = None
    template_parameter_types: Mapping[Any, Any] = field(default_factory=dict)
    trusted_ros_account_context: TrustedRosAccountContext | None = None


class ValidationPolicy(str, Enum):
    COMPATIBLE = "COMPATIBLE"
    STRICT = "STRICT"
