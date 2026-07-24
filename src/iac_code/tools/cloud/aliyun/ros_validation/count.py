"""Count occurrence eligibility and the pre-runtime CountSelectFold transform."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable


class CountRewriteReason(str, Enum):
    ELIGIBLE = "ELIGIBLE"
    POSITION = "POSITION_NOT_ELIGIBLE"
    RAW_SHAPE = "RAW_SHAPE_NOT_ELIGIBLE"


@dataclass(frozen=True)
class CountRewriteEligibility:
    position_eligible: bool
    raw_shape_eligible: bool
    reason: CountRewriteReason

    @property
    def eligible(self) -> bool:
        return self.position_eligible and self.raw_shape_eligible


@dataclass(frozen=True)
class CountSelectFoldFact:
    activated: bool
    raw_shape: str
    resolved_index: int | slice | None = None
    selected_origin: int | None = None
    transformed_node: Any = None
    deleted_node_indexes: tuple[int, ...] = ()
    precompile_failure: str | None = None


def ref_count_eligibility(position_eligible: bool, raw_args: Any) -> CountRewriteEligibility:
    raw_shape = isinstance(raw_args, str)
    reason = (
        CountRewriteReason.ELIGIBLE
        if position_eligible and raw_shape
        else CountRewriteReason.POSITION
        if not position_eligible
        else CountRewriteReason.RAW_SHAPE
    )
    return CountRewriteEligibility(position_eligible, raw_shape, reason)


def getatt_count_eligibility(position_eligible: bool, raw_args: Any) -> CountRewriteEligibility:
    raw_shape = isinstance(raw_args, list) and len(raw_args) == 2 and isinstance(raw_args[0], str)
    reason = (
        CountRewriteReason.ELIGIBLE
        if position_eligible and raw_shape
        else CountRewriteReason.POSITION
        if not position_eligible
        else CountRewriteReason.RAW_SHAPE
    )
    return CountRewriteEligibility(position_eligible, raw_shape, reason)


def _parse_slice(value: str) -> slice | None:
    if ":" not in value:
        return None
    parts = value.split(":")
    if len(parts) not in (2, 3):
        return None
    try:
        numbers = [int(item) if item else None for item in parts]
    except ValueError:
        return None
    return slice(*numbers)


def fold_count_select(
    args: Any,
    resolve_compile_function: Callable[[dict[str, Any]], Any],
    resolve_lookup_function: Callable[[dict[str, Any]], Any] | None = None,
) -> CountSelectFoldFact:
    """Reproduce the Count precompiler's narrow two/three-item Select fold.

    ``resolve_compile_function`` is deliberately restricted to a single raw
    function Mapping.  It must not recursively execute arbitrary templates.
    """

    if not isinstance(args, list) or len(args) not in (2, 3):
        return CountSelectFoldFact(False, type(args).__name__)
    lhs, rhs = args[0], args[1]
    if not (isinstance(lhs, dict) and len(lhs) == 1 and isinstance(rhs, dict) and len(rhs) == 1):
        return CountSelectFoldFact(False, "list-{}".format(len(args)))
    collection = resolve_compile_function(rhs)
    if not isinstance(collection, list):
        return CountSelectFoldFact(False, "rhs-not-direct-list")
    try:
        lookup = (resolve_lookup_function or resolve_compile_function)(lhs)
        if not isinstance(lookup, (str, int)):
            lookup = resolve_compile_function(lhs)
    except Exception:
        try:
            lookup = resolve_compile_function(lhs)
        except Exception:
            return CountSelectFoldFact(False, "lhs-unresolved")
    if isinstance(lookup, str):
        parsed_slice = _parse_slice(lookup)
        if parsed_slice is None:
            return CountSelectFoldFact(False, "lhs-not-integer")
        try:
            selected = collection[parsed_slice]
        except ValueError as ex:
            return CountSelectFoldFact(
                True,
                "list-{}".format(len(args)),
                resolved_index=parsed_slice,
                precompile_failure=str(ex),
            )
        return CountSelectFoldFact(
            True,
            "list-{}".format(len(args)),
            resolved_index=parsed_slice,
            selected_origin=1,
            transformed_node=selected,
            deleted_node_indexes=(0, 1, 2) if len(args) == 3 else (0, 1),
        )
    if not isinstance(lookup, int):
        return CountSelectFoldFact(False, "lhs-not-integer")
    if lookup >= len(collection):
        if len(args) == 3:
            return CountSelectFoldFact(
                True,
                "list-3",
                resolved_index=lookup,
                selected_origin=2,
                transformed_node=args[2],
                deleted_node_indexes=(0, 1),
            )
        return CountSelectFoldFact(
            True,
            "list-2",
            resolved_index=lookup,
            precompile_failure="positive index out of bounds",
        )
    try:
        selected = collection[lookup]
    except IndexError:
        return CountSelectFoldFact(
            True,
            "list-{}".format(len(args)),
            resolved_index=lookup,
            precompile_failure="negative index out of bounds",
        )
    return CountSelectFoldFact(
        True,
        "list-{}".format(len(args)),
        resolved_index=lookup,
        selected_origin=1,
        transformed_node=selected,
        deleted_node_indexes=(0, 1, 2) if len(args) == 3 else (0, 1),
    )
