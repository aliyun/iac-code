"""ROS-compatible YAML/JSON parsing with occurrence-level source positions."""

from __future__ import annotations

import hashlib
import json
import re
from bisect import bisect_right
from dataclasses import dataclass, replace
from typing import Any

import yaml
from yaml.constructor import ConstructorError

from iac_code.i18n import _
from iac_code.tools.cloud.aliyun.ros_validation.model import (
    Category,
    Diagnostic,
    ParsedTemplate,
    PositionedNode,
    RelatedLocation,
    RosPath,
    SequenceIndexSegment,
    Severity,
    SourceMap,
    SourceSpan,
    SyntheticSegment,
    make_diagnostic,
    mapping_segment,
    path_identity,
)
from iac_code.tools.cloud.aliyun.ros_yaml import _RosYamlLoader

MIN_TEMPLATE_BYTES = 1
MAX_TEMPLATE_BYTES = 524_288
MAX_CONTAINER_DEPTH = 100
MAX_PARSE_EVENTS = 200_000
MAX_ALIAS_REFERENCES = 10_000
MAX_MERGE_EXPANSIONS = 50_000
MAX_SYNTHESIZED_OCCURRENCES = 200_000
MAX_SEMANTIC_VISITS = 500_000


@dataclass(frozen=True)
class ParseResult:
    template: ParsedTemplate | None
    diagnostics: tuple[Diagnostic, ...]
    analysis_incomplete: bool = False


def _span(
    node: yaml.Node,
    *,
    synthetic: bool = False,
    occurrence_marks: tuple[Any, Any] | None = None,
) -> SourceSpan:
    start_mark, end_mark = occurrence_marks or (node.start_mark, node.end_mark)
    return SourceSpan(
        line=start_mark.line + 1,
        column=start_mark.column + 1,
        end_line=end_mark.line + 1,
        end_column=end_mark.column + 1,
        synthetic=synthetic,
    )


def _node_id(
    source_id: str,
    node: yaml.Node,
    path: RosPath,
    ordinal: int,
    occurrence_marks: tuple[Any, Any] | None = None,
) -> str:
    start_mark, end_mark = occurrence_marks or (node.start_mark, node.end_mark)
    payload = "{}:{}:{}:{}:{}".format(
        source_id,
        start_mark.index,
        end_mark.index,
        path_identity(path),
        ordinal,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]


def _construct_scalar(loader: _RosYamlLoader, node: yaml.ScalarNode) -> Any:
    value = loader.construct_object(node, deep=True)
    return value.isoformat() if hasattr(value, "isoformat") and node.tag.endswith(":timestamp") else value


def _typed_mapping_key(value: Any) -> tuple[str, ...]:
    return path_identity((mapping_segment(value),))


class _PositionBuilder:
    def __init__(
        self,
        text: str,
        source_id: str,
        *,
        synthetic_origin: bool = False,
        alias_marks: tuple[tuple[Any, Any], ...] = (),
    ) -> None:
        self.text = text
        self.source_id = source_id
        self.synthetic_origin = synthetic_origin
        self.nodes: dict[tuple[str, ...], PositionedNode] = {}
        self.occurrences: list[PositionedNode] = []
        self.diagnostics: list[Diagnostic] = []
        self.visits = 0
        self.alias_references = 0
        self.merge_expansions = 0
        self.synthetic_occurrences = 0
        self._identities: dict[int, str] = {}
        self._alias_marks = alias_marks
        self._alias_cursor = 0

    def _next_alias_marks(self) -> tuple[Any, Any] | None:
        if self._alias_cursor >= len(self._alias_marks):
            return None
        result = self._alias_marks[self._alias_cursor]
        self._alias_cursor += 1
        return result

    def _claim_alias_marks(self, node: yaml.Node) -> tuple[Any, Any] | None:
        if id(node) not in self._identities:
            return None
        marks = self._next_alias_marks()
        if marks is None:
            return None
        self.alias_references += 1
        if self.alias_references > MAX_ALIAS_REFERENCES:
            raise _BudgetExceededError(_("alias reference budget exceeds {}").format(MAX_ALIAS_REFERENCES))
        return marks

    @staticmethod
    def _merge_sources(node: yaml.Node) -> tuple[yaml.MappingNode, ...]:
        if isinstance(node, yaml.MappingNode):
            return (node,)
        if isinstance(node, yaml.SequenceNode):
            return tuple(item for item in node.value if isinstance(item, yaml.MappingNode))
        return ()

    def _effective_mapping_entries(
        self,
        node: yaml.MappingNode,
        loader: _RosYamlLoader,
        *,
        visiting: frozenset[int] = frozenset(),
    ) -> tuple[tuple[Any, yaml.ScalarNode, yaml.Node], ...]:
        """Return the value nodes selected by PyYAML merge/last-wins rules."""

        if id(node) in visiting:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "found recursive merge alias",
                node.start_mark,
            )
        visiting = visiting | {id(node)}
        merged_entries: list[tuple[Any, yaml.ScalarNode, yaml.Node]] = []
        explicit_entries: list[tuple[Any, yaml.ScalarNode, yaml.Node]] = []
        for key_node, value_node in node.value:
            if not isinstance(key_node, yaml.ScalarNode):
                continue
            key = "<<" if key_node.value == "<<" else _construct_scalar(loader, key_node)
            if key == "<<":
                sources = self._merge_sources(value_node)
                # PyYAML reverses merge sequences before constructing the
                # Mapping, so the first source has the highest precedence.
                if isinstance(value_node, yaml.SequenceNode):
                    sources = tuple(reversed(sources))
                for source in sources:
                    for entry in self._effective_mapping_entries(source, loader, visiting=visiting):
                        merged_entries.append(entry)
                continue
            try:
                hash(key)
            except TypeError:
                continue
            explicit_entries.append((key, key_node, value_node))

        effective: dict[tuple[str, ...], tuple[Any, yaml.ScalarNode, yaml.Node]] = {}
        for entry in (*merged_entries, *explicit_entries):
            effective[_typed_mapping_key(entry[0])] = entry
        return tuple(effective.values())

    def _build_merge_occurrences(
        self,
        value_node: yaml.Node,
        loader: _RosYamlLoader,
        path: RosPath,
        depth: int,
        explicit_keys: frozenset[tuple[str, ...]],
        parent_occurrence_marks: tuple[Any, Any] | None,
    ) -> tuple[tuple[Any, RosPath], ...]:
        sources = self._merge_sources(value_node)
        marks_by_source: dict[int, tuple[Any, Any] | None] = {}
        for source_index, source in enumerate(sources):
            marks = parent_occurrence_marks
            if marks is None:
                marks = self._claim_alias_marks(source)
            marks_by_source[id(source)] = marks
            self.add(
                source,
                path + (SyntheticSegment("YamlMergeSource", source_index),),
                "<<",
                synthetic=True,
                occurrence_marks=marks,
            )

        construction_sources = tuple(reversed(sources)) if isinstance(value_node, yaml.SequenceNode) else sources
        effective: dict[tuple[str, ...], tuple[Any, yaml.Node, tuple[Any, Any] | None]] = {}
        for source in construction_sources:
            for key, _key_node, child in self._effective_mapping_entries(source, loader):
                effective[_typed_mapping_key(key)] = (key, child, marks_by_source[id(source)])

        expanded: list[tuple[Any, RosPath]] = []
        for typed_key, (key, child, occurrence_marks) in effective.items():
            if typed_key in explicit_keys:
                continue
            self.merge_expansions += 1
            if self.merge_expansions > MAX_MERGE_EXPANSIONS:
                raise _BudgetExceededError(_("merge expansion budget exceeds {}").format(MAX_MERGE_EXPANSIONS))
            child_path = path + (mapping_segment(key),)
            self.build(child, loader, child_path, depth + 1, occurrence_marks)
            expanded.append((key, child_path))
        return tuple(expanded)

    def _remap_semantic_paths(self, candidates: tuple[tuple[Any, RosPath], ...]) -> None:
        semantic_paths: dict[Any, RosPath] = {}
        for key, child_path in candidates:
            first_path = semantic_paths.get(key)
            if first_path is None:
                semantic_paths[key] = child_path
                continue
            last_wins_nodes = [
                positioned for positioned in self.nodes.values() if positioned.path[: len(child_path)] == child_path
            ]
            for positioned in last_wins_nodes:
                semantic_path = first_path + positioned.path[len(child_path) :]
                self.nodes[path_identity(semantic_path)] = replace(positioned, path=semantic_path)

    def add(
        self,
        node: yaml.Node,
        path: RosPath,
        value: Any,
        *,
        ordinal: int = 0,
        synthetic: bool = False,
        occurrence_marks: tuple[Any, Any] | None = None,
    ) -> PositionedNode:
        if synthetic:
            self.synthetic_occurrences += 1
            if self.synthetic_occurrences > MAX_SYNTHESIZED_OCCURRENCES:
                raise _BudgetExceededError(
                    "synthesized occurrence budget exceeds {}".format(MAX_SYNTHESIZED_OCCURRENCES)
                )
        node_id = _node_id(self.source_id, node, path, ordinal, occurrence_marks)
        origin = self._identities.get(id(node))
        positioned = PositionedNode(
            node_id=node_id,
            path=path,
            value=value,
            span=_span(
                node,
                synthetic=self.synthetic_origin,
                occurrence_marks=occurrence_marks,
            ),
            origin_node_ids=(origin,) if origin and origin != node_id else (),
        )
        if origin is None:
            self._identities[id(node)] = node_id
        self.nodes[path_identity(path)] = positioned
        self.occurrences.append(positioned)
        return positioned

    def build(
        self,
        node: yaml.Node,
        loader: _RosYamlLoader,
        path: RosPath = (),
        depth: int = 0,
        alias_marks: tuple[Any, Any] | None = None,
    ) -> Any:
        self.visits += 1
        if depth > MAX_CONTAINER_DEPTH:
            raise _BudgetExceededError(_("template nesting exceeds {}").format(MAX_CONTAINER_DEPTH))
        if self.visits > MAX_PARSE_EVENTS:
            raise _BudgetExceededError(_("parser event budget exceeds {}").format(MAX_PARSE_EVENTS))
        if self.alias_references > MAX_ALIAS_REFERENCES:
            raise _BudgetExceededError(_("alias reference budget exceeds {}").format(MAX_ALIAS_REFERENCES))
        if alias_marks is None:
            alias_marks = self._claim_alias_marks(node)

        if isinstance(node, yaml.ScalarNode):
            if node.tag == "!Ref":
                value = loader.construct_scalar(node)
                result = {"Ref": value}
                self.add(node, path, result, occurrence_marks=alias_marks)
                key_path = path + (mapping_segment("Ref"),)
                self.add(node, key_path, value, synthetic=True, occurrence_marks=alias_marks)
                return result
            if node.tag == "!GetAtt":
                value = loader.construct_scalar(node)
                text = str(value)
                parts = text.split(".")
                if len(parts) >= 3 and parts[-2] == "Outputs":
                    args = [".".join(parts[:-2]), ".".join(parts[-2:])]
                elif len(parts) >= 2:
                    args = [".".join(parts[:-1]), parts[-1]]
                else:
                    args = value
                result = {"Fn::GetAtt": args}
                self.add(node, path, result, occurrence_marks=alias_marks)
                fn_path = path + (mapping_segment("Fn::GetAtt"),)
                self.add(node, fn_path, args, synthetic=True, occurrence_marks=alias_marks)
                if isinstance(args, list):
                    for index, item in enumerate(args):
                        self.add(
                            node,
                            fn_path + (SyntheticSegment("GetAttShortTag", index),),
                            item,
                            synthetic=True,
                            occurrence_marks=alias_marks,
                        )
                return result
            if node.tag.startswith("!") and node.tag not in ("!", "!!"):
                value = loader.construct_scalar(node)
                fn_name = "Fn::{}".format(node.tag[1:])
                result = {fn_name: value}
                self.add(node, path, result, occurrence_marks=alias_marks)
                self.add(
                    node,
                    path + (mapping_segment(fn_name),),
                    value,
                    synthetic=True,
                    occurrence_marks=alias_marks,
                )
                return result
            value = _construct_scalar(loader, node)
            self.add(node, path, value, occurrence_marks=alias_marks)
            return value

        if isinstance(node, yaml.SequenceNode):
            result: list[Any] = []
            self.add(node, path, result, occurrence_marks=alias_marks)
            for index, child in enumerate(node.value):
                result.append(
                    self.build(
                        child,
                        loader,
                        path + (SequenceIndexSegment(index),),
                        depth + 1,
                        alias_marks,
                    )
                )
            if node.tag.startswith("!"):
                fn_name = "Fn::{}".format(node.tag[1:])
                wrapped = {fn_name: result}
                self.add(node, path, wrapped, occurrence_marks=alias_marks)
                fn_path = path + (mapping_segment(fn_name),)
                self.add(node, fn_path, result, synthetic=True, occurrence_marks=alias_marks)
                for index, child in enumerate(node.value):
                    child_value = result[index]
                    self.add(
                        child,
                        fn_path + (SequenceIndexSegment(index),),
                        child_value,
                        occurrence_marks=alias_marks,
                    )
                return wrapped
            return result

        if isinstance(node, yaml.MappingNode):
            result: dict[Any, Any] = {}
            self.add(node, path, result, occurrence_marks=alias_marks)
            seen: dict[tuple[str, ...], tuple[RosPath, SourceSpan]] = {}
            occurrences: dict[tuple[str, ...], int] = {}
            explicit_keys: set[tuple[str, ...]] = set()
            merged_candidates: list[tuple[Any, RosPath]] = []
            explicit_candidates: list[tuple[Any, RosPath]] = []
            for key_node, _value_node in node.value:
                if not isinstance(key_node, yaml.ScalarNode) or key_node.value == "<<":
                    continue
                key = _construct_scalar(loader, key_node)
                try:
                    hash(key)
                except TypeError:
                    continue
                explicit_keys.add(_typed_mapping_key(key))
            for key_node, value_node in node.value:
                if isinstance(key_node, yaml.ScalarNode) and key_node.value == "<<":
                    key = "<<"
                else:
                    key = _construct_scalar(loader, key_node) if isinstance(key_node, yaml.ScalarNode) else None
                if key == "<<":
                    # PyYAML's merge semantics are retained by the regular loader.  The
                    # expanded SourceMap fields point to this merge use and
                    # relate back to their anchor field occurrence.
                    merge_path = path + (mapping_segment("<<"),)
                    self.add(key_node, merge_path, key, occurrence_marks=alias_marks)
                    merged_candidates.extend(
                        self._build_merge_occurrences(
                            value_node,
                            loader,
                            path,
                            depth,
                            frozenset(explicit_keys),
                            alias_marks,
                        )
                    )
                    continue
                try:
                    hash(key)
                except TypeError as error:
                    raise ConstructorError(
                        "while constructing a mapping",
                        node.start_mark,
                        "found unhashable key ({})".format(type(key).__name__),
                        key_node.start_mark,
                    ) from error
                typed_key = _typed_mapping_key(key)
                ordinal = occurrences.get(typed_key, 0)
                occurrences[typed_key] = ordinal + 1
                segment = mapping_segment(key, ordinal)
                child_path = path + (segment,)
                key_alias_marks = self._claim_alias_marks(key_node) if alias_marks is None else None
                key_positioned = self.add(
                    key_node,
                    child_path,
                    key,
                    occurrence_marks=key_alias_marks if key_alias_marks is not None else alias_marks,
                )
                value = self.build(value_node, loader, child_path, depth + 1, alias_marks)
                explicit_candidates.append((key, child_path))
                if typed_key in seen:
                    first_path, first_span = seen[typed_key]
                    diagnostic_nodes = dict(self.nodes)
                    diagnostic_nodes[path_identity(child_path)] = key_positioned
                    self.diagnostics.append(
                        make_diagnostic(
                            code="ROS1003",
                            severity=Severity.ERROR,
                            category=Category.COMPATIBILITY,
                            summary=_("The template contains a duplicate Mapping key."),
                            detail=_("ROS/PyYAML uses the later value; remove the duplicate to avoid ambiguity."),
                            path=child_path,
                            source_map=SourceMap(diagnostic_nodes, tuple(self.occurrences)),
                            subject="mapping-key",
                            stable_args=(type(key).__name__,),
                            suggestion=_("Keep only one declaration of this key."),
                            related_locations=(RelatedLocation(_("first declaration"), first_span, first_path),),
                        )
                    )
                else:
                    seen[typed_key] = (child_path, _span(key_node))
                result[key] = value

            # PyYAML constructs merged entries before explicit entries and
            # applies Python Mapping equality to the semantic value. Preserve
            # every typed occurrence while pointing semantic lookups at the
            # final value selected by those rules.
            self._remap_semantic_paths(tuple((*merged_candidates, *explicit_candidates)))

            if node.tag.startswith("!"):
                fn_name = "Fn::{}".format(node.tag[1:])
                wrapped = {fn_name: result}
                self.add(node, path, wrapped, occurrence_marks=alias_marks)
                self.add(
                    node,
                    path + (mapping_segment(fn_name),),
                    result,
                    synthetic=True,
                    occurrence_marks=alias_marks,
                )
                return wrapped
            return result

        raise TypeError("unsupported YAML node {}".format(type(node).__name__))


class _BudgetExceededError(ValueError):
    pass


class _JsonParseError(ValueError):
    pass


_JSON_NUMBER = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?")


class _JsonPositionParser:
    """Parse ROS-compatible JSON while constructing occurrence-level positions."""

    def __init__(self, text: str, source_id: str, *, synthetic_origin: bool) -> None:
        self.text = text
        self.source_id = source_id
        self.synthetic_origin = synthetic_origin
        self.index = 0
        self.visits = 0
        self.nodes: dict[tuple[str, ...], PositionedNode] = {}
        self.occurrences: list[PositionedNode] = []
        self.diagnostics: list[Diagnostic] = []
        self._line_starts = [0]
        self._line_starts.extend(index + 1 for index, character in enumerate(text) if character == "\n")

    def parse(self) -> Any:
        self._skip_whitespace()
        value = self._parse_value((), 0)
        self._skip_whitespace()
        if self.index != len(self.text):
            raise _JsonParseError(_("JSON contains trailing content"))
        return value

    def _skip_whitespace(self) -> None:
        while self.index < len(self.text) and self.text[self.index] in " \t\r\n":
            self.index += 1

    def _position(self, index: int) -> tuple[int, int]:
        line_index = bisect_right(self._line_starts, index) - 1
        return line_index + 1, index - self._line_starts[line_index] + 1

    def _source_span(self, start: int, end: int) -> SourceSpan:
        line, column = self._position(start)
        end_line, end_column = self._position(end)
        return SourceSpan(line, column, end_line, end_column, synthetic=self.synthetic_origin)

    def _add(self, path: RosPath, value: Any, start: int, end: int) -> PositionedNode:
        payload = "{}:{}:{}:{}".format(self.source_id, start, end, path_identity(path))
        positioned = PositionedNode(
            node_id=hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20],
            path=path,
            value=value,
            span=self._source_span(start, end),
        )
        self.nodes[path_identity(path)] = positioned
        self.occurrences.append(positioned)
        return positioned

    def _parse_value(self, path: RosPath, depth: int) -> Any:
        self.visits += 1
        if depth > MAX_CONTAINER_DEPTH:
            raise _BudgetExceededError(_("template nesting exceeds {}").format(MAX_CONTAINER_DEPTH))
        if self.visits > MAX_PARSE_EVENTS:
            raise _BudgetExceededError(_("parser event budget exceeds {}").format(MAX_PARSE_EVENTS))
        if self.index >= len(self.text):
            raise _JsonParseError(_("unexpected end of JSON input"))
        character = self.text[self.index]
        if character == "{":
            return self._parse_object(path, depth)
        if character == "[":
            return self._parse_array(path, depth)
        if character == '"':
            start = self.index
            value, end = self._parse_string()
            self._add(path, value, start, end)
            return value
        for literal, value in (
            ("-Infinity", float("-inf")),
            ("Infinity", float("inf")),
            ("NaN", float("nan")),
            ("true", True),
            ("false", False),
            ("null", None),
        ):
            if self.text.startswith(literal, self.index):
                start = self.index
                self.index += len(literal)
                self._add(path, value, start, self.index)
                return value
        match = _JSON_NUMBER.match(self.text, self.index)
        if match is None:
            raise _JsonParseError(_("invalid JSON value"))
        start = self.index
        self.index = match.end()
        token = match.group(0)
        value = float(token) if any(character in token for character in ".eE") else int(token)
        self._add(path, value, start, self.index)
        return value

    def _parse_string(self) -> tuple[str, int]:
        start = self.index
        cursor = start + 1
        while cursor < len(self.text):
            character = self.text[cursor]
            if character == '"':
                end = cursor + 1
                break
            if ord(character) < 0x20:
                raise _JsonParseError(_("unescaped control character in JSON string"))
            if character != "\\":
                cursor += 1
                continue
            cursor += 1
            if cursor >= len(self.text) or self.text[cursor] not in '"\\/bfnrtu':
                raise _JsonParseError(_("invalid JSON string escape"))
            if self.text[cursor] == "u":
                escape = self.text[cursor + 1 : cursor + 5]
                if len(escape) != 4 or any(item not in "0123456789abcdefABCDEF" for item in escape):
                    raise _JsonParseError(_("invalid JSON Unicode escape"))
                cursor += 5
            else:
                cursor += 1
        else:
            raise _JsonParseError(_("unterminated JSON string"))
        try:
            value = json.loads(self.text[start:end])
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as error:
            raise _JsonParseError(_("invalid JSON string")) from error
        if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
            raise _JsonParseError(_("invalid Unicode surrogate"))
        self.index = end
        return value, end

    def _parse_array(self, path: RosPath, depth: int) -> list[Any]:
        start = self.index
        self.index += 1
        self._skip_whitespace()
        result: list[Any] = []
        if self.index < len(self.text) and self.text[self.index] == "]":
            self.index += 1
            self._add(path, result, start, self.index)
            return result
        while True:
            item_path = path + (SequenceIndexSegment(len(result)),)
            result.append(self._parse_value(item_path, depth + 1))
            self._skip_whitespace()
            if self.index >= len(self.text):
                raise _JsonParseError(_("unterminated JSON array"))
            delimiter = self.text[self.index]
            self.index += 1
            if delimiter == "]":
                self._add(path, result, start, self.index)
                return result
            if delimiter != ",":
                raise _JsonParseError(_("expected ',' or ']' in JSON array"))
            self._skip_whitespace()

    def _parse_object(self, path: RosPath, depth: int) -> dict[str, Any]:
        start = self.index
        self.index += 1
        self._skip_whitespace()
        result: dict[str, Any] = {}
        seen: dict[str, tuple[RosPath, SourceSpan]] = {}
        occurrences: dict[str, int] = {}
        if self.index < len(self.text) and self.text[self.index] == "}":
            self.index += 1
            self._add(path, result, start, self.index)
            return result
        while True:
            if self.index >= len(self.text) or self.text[self.index] != '"':
                raise _JsonParseError(_("JSON object keys must be strings"))
            key_start = self.index
            key, key_end = self._parse_string()
            self._skip_whitespace()
            if self.index >= len(self.text) or self.text[self.index] != ":":
                raise _JsonParseError(_("expected ':' after JSON object key"))
            self.index += 1
            self._skip_whitespace()
            ordinal = occurrences.get(key, 0)
            occurrences[key] = ordinal + 1
            child_path = path + (mapping_segment(key, ordinal),)
            key_positioned = self._add(child_path, key, key_start, key_end)
            value = self._parse_value(child_path, depth + 1)
            if key in seen:
                first_path, first_span = seen[key]
                diagnostic_nodes = dict(self.nodes)
                diagnostic_nodes[path_identity(child_path)] = key_positioned
                self.diagnostics.append(
                    make_diagnostic(
                        code="ROS1003",
                        severity=Severity.ERROR,
                        category=Category.COMPATIBILITY,
                        summary=_("The template contains a duplicate Mapping key."),
                        detail=_("ROS JSON uses the later value; remove the duplicate to avoid ambiguity."),
                        path=child_path,
                        source_map=SourceMap(diagnostic_nodes, tuple(self.occurrences)),
                        subject="mapping-key",
                        stable_args=("str",),
                        suggestion=_("Keep only one declaration of this key."),
                        related_locations=(RelatedLocation(_("first declaration"), first_span, first_path),),
                    )
                )
                last_wins_nodes = [
                    positioned for positioned in self.nodes.values() if positioned.path[: len(child_path)] == child_path
                ]
                for positioned in last_wins_nodes:
                    semantic_path = first_path + positioned.path[len(child_path) :]
                    self.nodes[path_identity(semantic_path)] = replace(positioned, path=semantic_path)
            else:
                seen[key] = (child_path, self._source_span(key_start, key_end))
            result[key] = value
            self._skip_whitespace()
            if self.index >= len(self.text):
                raise _JsonParseError(_("unterminated JSON object"))
            delimiter = self.text[self.index]
            self.index += 1
            if delimiter == "}":
                self._add(path, result, start, self.index)
                return result
            if delimiter != ",":
                raise _JsonParseError(_("expected ',' or '}' in JSON object"))
            self._skip_whitespace()


def _syntax_diagnostic(message: str, *, line: int | None = None, column: int | None = None) -> Diagnostic:
    span = SourceSpan(line, column, line, column) if line and column else None
    item = make_diagnostic(
        code="ROS1001",
        severity=Severity.ERROR,
        category=Category.COMPATIBILITY,
        summary=_("The ROS template cannot be parsed."),
        detail=message,
        stable_args=("syntax",),
        suggestion=_("Fix the syntax and validate again."),
    )
    if span is None:
        return item
    return replace(item, source_span=span)


def parse_template_source(
    text: str,
    *,
    source_id: str = "TemplateBody",
    synthetic_origin: bool = False,
) -> ParseResult:
    if not isinstance(text, str):
        diagnostic = make_diagnostic(
            code="ROS1000",
            severity=Severity.ERROR,
            category=Category.COMPATIBILITY,
            summary=_("TemplateBody must be a String."),
            detail=_("The current TemplateBody type is not String, so source locations cannot be built."),
            stable_args=(type(text).__name__,),
            expected="String",
            actual=type(text).__name__,
        )
        return ParseResult(None, (diagnostic,), analysis_incomplete=True)
    byte_length = len(text.encode("utf-8"))
    stage_diagnostics: list[Diagnostic] = []
    if byte_length < MIN_TEMPLATE_BYTES or byte_length > MAX_TEMPLATE_BYTES:
        stage_diagnostics.append(
            make_diagnostic(
                code="ROS1002",
                severity=Severity.ERROR,
                category=Category.COMPATIBILITY,
                summary=_("TemplateBody UTF-8 size is outside the allowed range."),
                detail=_("The allowed range is 1 to 524288 bytes; the current size is {} bytes.").format(byte_length),
                stable_args=(str(byte_length),),
            )
        )
    if "\x00" in text:
        stage_diagnostics.append(
            make_diagnostic(
                code="ROS1002",
                severity=Severity.ERROR,
                category=Category.COMPATIBILITY,
                summary=_("TemplateBody contains a NUL character."),
                detail=_("NUL makes ROS template parsing unsafe, so local validation has stopped."),
                stable_args=("nul",),
            )
        )
    if stage_diagnostics:
        return ParseResult(None, tuple(stage_diagnostics), analysis_incomplete=True)

    json_parser = _JsonPositionParser(text, source_id, synthetic_origin=synthetic_origin)
    try:
        json_data = json_parser.parse()
    except _JsonParseError:
        pass
    except _BudgetExceededError as error:
        diagnostic = make_diagnostic(
            code="ROS9001",
            severity=Severity.ERROR,
            category=Category.LIMITATION,
            summary=_("The ROS template exceeds the local-analysis safety budget."),
            detail=str(error),
            stable_args=("parser-budget",),
        )
        return ParseResult(None, (diagnostic,), analysis_incomplete=True)
    else:
        parsed = ParsedTemplate(
            data=json_data,
            source_map=SourceMap(dict(json_parser.nodes), tuple(json_parser.occurrences)),
            source_kind="JSON",
            text=text,
        )
        return ParseResult(parsed, tuple(json_parser.diagnostics))

    loader = _RosYamlLoader(text)
    try:
        alias_marks = tuple(
            (token.start_mark, token.end_mark)
            for token in yaml.scan(text, Loader=_RosYamlLoader)
            if isinstance(token, yaml.tokens.AliasToken)
        )
        node = loader.get_single_node()
        if node is None:
            return ParseResult(None, (_syntax_diagnostic(_("The template is empty.")),), analysis_incomplete=True)
        builder = _PositionBuilder(
            text,
            source_id,
            synthetic_origin=synthetic_origin,
            alias_marks=alias_marks,
        )
        builder.build(node, loader)
        # Positions are collected occurrence-by-occurrence above.  Construct
        # semantic data with the real loader so nested merge keys and aliases
        # retain exactly the same last-wins behavior as normal ROS YAML input.
        data = yaml.load(text, Loader=_RosYamlLoader)
        parsed = ParsedTemplate(
            data=data,
            source_map=SourceMap(dict(builder.nodes), tuple(builder.occurrences)),
            source_kind="YAML",
            text=text,
        )
        return ParseResult(parsed, tuple(builder.diagnostics))
    except _BudgetExceededError as error:
        diagnostic = make_diagnostic(
            code="ROS9001",
            severity=Severity.ERROR,
            category=Category.LIMITATION,
            summary=_("The ROS template exceeds the local-analysis safety budget."),
            detail=str(error),
            stable_args=("parser-budget",),
        )
        return ParseResult(None, (diagnostic,), analysis_incomplete=True)
    except yaml.YAMLError as error:
        mark = getattr(error, "problem_mark", None)
        problem = getattr(error, "problem", None) or str(error)
        return ParseResult(
            None,
            (
                _syntax_diagnostic(
                    str(problem),
                    line=mark.line + 1 if mark else None,
                    column=mark.column + 1 if mark else None,
                ),
            ),
            analysis_incomplete=True,
        )
    except Exception as error:
        diagnostic = make_diagnostic(
            code="ROS9999",
            severity=Severity.ERROR,
            category=Category.LIMITATION,
            summary=_("An internal error occurred during the ROS local-validator parsing stage."),
            detail=type(error).__name__,
            stable_args=(type(error).__name__,),
        )
        return ParseResult(None, (diagnostic,), analysis_incomplete=True)
    finally:
        loader.dispose()
