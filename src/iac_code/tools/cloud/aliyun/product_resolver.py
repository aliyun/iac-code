"""Conservative Alibaba Cloud Product Code resolution over the product catalog."""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Literal

import yaml

from iac_code.tools.cloud.aliyun.openmeta import (
    MetadataSource,
    OpenMetaCacheStatus,
    OpenMetaClient,
    OpenMetaError,
    ProductMetadata,
)

ProductMatchStrategy = Literal[
    "exact_code",
    "trimmed_exact",
    "separator_normalized",
    "short_name",
    "builtin_alias",
    "single_edit",
    "separator_ambiguous",
    "alias_ambiguous",
    "single_edit_ambiguous",
    "excluded",
    "not_found",
    "unavailable",
    "unverified",
]
ProductMatchConfidence = Literal["high", "medium", "none"]
ProductMatchOutcome = Literal["matched", "not_found", "error", "unverified"]

PRODUCT_MATCH_STRATEGIES = frozenset(
    {
        "exact_code",
        "trimmed_exact",
        "separator_normalized",
        "short_name",
        "builtin_alias",
        "single_edit",
        "separator_ambiguous",
        "alias_ambiguous",
        "single_edit_ambiguous",
        "excluded",
        "not_found",
        "unavailable",
        "unverified",
    }
)
PRODUCT_MATCH_CONFIDENCES = frozenset({"high", "medium", "none"})
PRODUCT_MATCH_OUTCOMES = frozenset({"matched", "not_found", "error", "unverified"})

_PRODUCT_DATA_DIR = Path(__file__).parent / "data" / "openmeta"
_ALIASES_PATH = _PRODUCT_DATA_DIR / "product_aliases.yml"
_CATALOG_PATH = _PRODUCT_DATA_DIR / "product_catalog.json"
_ASCII_WHITESPACE = " \t\n\r\f\v"
_SAFE_PRODUCT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_PLACEHOLDER_ALIASES = frozenset({"-", "n/a", "na", "none", "null", "unknown", "暂无"})
_MIN_FUZZY_LENGTH = 5


@dataclass(frozen=True)
class ProductResolution:
    requested_product: str
    normalized_product: str
    metadata: ProductMetadata | None
    strategy: ProductMatchStrategy
    confidence: ProductMatchConfidence
    suggestions: tuple[str, ...] = ()
    source: MetadataSource | None = None
    error: OpenMetaError | None = None
    cache_status: OpenMetaCacheStatus = "miss"

    @property
    def canonical_product(self) -> str | None:
        return self.metadata.product if self.metadata is not None else None

    @property
    def outcome(self) -> ProductMatchOutcome:
        if self.strategy == "unverified":
            return "unverified"
        if self.metadata is not None:
            return "matched"
        return "not_found" if self.error == "not_found" else "error"


@dataclass(frozen=True)
class ProductAlias:
    alias: str
    product: str
    reason: str
    source: str


@dataclass(frozen=True)
class _ProductMatch:
    metadata: ProductMetadata | None
    strategy: ProductMatchStrategy
    confidence: ProductMatchConfidence
    suggestions: tuple[str, ...] = ()


class _ProductIndex:
    def __init__(self, products: Sequence[ProductMetadata], aliases: Sequence[ProductAlias]) -> None:
        code_by_key: dict[str, ProductMetadata] = {}
        separator: defaultdict[str, set[str]] = defaultdict(set)
        short_names: defaultdict[str, set[str]] = defaultdict(set)
        builtin_aliases: defaultdict[str, set[str]] = defaultdict(set)
        deleted_forms: defaultdict[str, set[str]] = defaultdict(set)

        for metadata in products:
            key = metadata.product.casefold()
            existing = code_by_key.get(key)
            if existing is not None and existing.product != metadata.product:
                raise ValueError("duplicate OpenMeta product code")
            code_by_key[key] = metadata
            separator[_separator_key(key)].add(key)
            short_name = _safe_alias(metadata.short_name)
            if short_name is not None:
                short_names[short_name].add(key)
            if len(key) >= _MIN_FUZZY_LENGTH:
                for deleted in _deleted_forms(key):
                    deleted_forms[deleted].add(key)

        for alias in aliases:
            target = alias.product.casefold()
            if target not in code_by_key:
                raise ValueError("product alias target is absent from the OpenMeta catalog")
            builtin_aliases[alias.alias.casefold()].add(target)

        self._code_by_key = MappingProxyType(code_by_key)
        self._separator = _freeze_sets(separator)
        self._short_names = _freeze_sets(short_names)
        self._builtin_aliases = _freeze_sets(builtin_aliases)
        self._deleted_forms = _freeze_sets(deleted_forms)

    def match(self, product: str) -> _ProductMatch:
        key = product.casefold()
        exact = self._code_by_key.get(key)
        if exact is not None:
            return _ProductMatch(exact, "exact_code", "high")

        separator_candidates = self._separator.get(_separator_key(key), ())
        if separator_candidates:
            return self._from_candidates(separator_candidates, "separator_normalized", "separator_ambiguous")

        short_candidates = set(self._short_names.get(key, ()))
        builtin_candidates = set(self._builtin_aliases.get(key, ()))
        alias_candidates = short_candidates | builtin_candidates
        if alias_candidates:
            if len(alias_candidates) == 1:
                target = next(iter(alias_candidates))
                strategy: ProductMatchStrategy = "short_name" if target in short_candidates else "builtin_alias"
                return _ProductMatch(self._code_by_key[target], strategy, "high")
            return _ProductMatch(
                None,
                "alias_ambiguous",
                "none",
                self._suggestions(alias_candidates),
            )

        fuzzy_candidates = self._fuzzy_candidates(key)
        if len(fuzzy_candidates) == 1:
            target = next(iter(fuzzy_candidates))
            return _ProductMatch(self._code_by_key[target], "single_edit", "medium")
        if fuzzy_candidates:
            return _ProductMatch(
                None,
                "single_edit_ambiguous",
                "none",
                self._suggestions(fuzzy_candidates),
            )
        return _ProductMatch(None, "not_found", "none")

    def _from_candidates(
        self,
        candidates: Sequence[str],
        unique_strategy: ProductMatchStrategy,
        ambiguous_strategy: ProductMatchStrategy,
    ) -> _ProductMatch:
        if len(candidates) == 1:
            target = candidates[0]
            return _ProductMatch(self._code_by_key[target], unique_strategy, "high")
        return _ProductMatch(None, ambiguous_strategy, "none", self._suggestions(candidates))

    def _fuzzy_candidates(self, key: str) -> set[str]:
        if len(key) < _MIN_FUZZY_LENGTH:
            return set()
        candidates = set(self._deleted_forms.get(key, ()))
        for deleted in _deleted_forms(key):
            exact = self._code_by_key.get(deleted)
            if exact is not None and len(deleted) >= _MIN_FUZZY_LENGTH:
                candidates.add(deleted)
            candidates.update(self._deleted_forms.get(deleted, ()))
        return {
            candidate
            for candidate in candidates
            if len(candidate) >= _MIN_FUZZY_LENGTH and _single_edit_apart(key, candidate)
        }

    def _suggestions(self, candidates: Iterable[str]) -> tuple[str, ...]:
        return tuple(
            self._code_by_key[key].product
            for key in sorted(set(candidates), key=lambda item: self._code_by_key[item].product.casefold())[:3]
        )


class ProductResolver:
    """Resolve a model-provided product against the bundled offline catalog."""

    def __init__(
        self,
        openmeta: OpenMetaClient,
        *,
        aliases_path: Path | None = _ALIASES_PATH,
        catalog_path: Path = _CATALOG_PATH,
        catalog: Sequence[ProductMetadata] | None = None,
        observer: Callable[[ProductResolution], None] | None = None,
    ) -> None:
        self._openmeta = openmeta
        aliases = load_product_aliases(aliases_path)
        excluded = getattr(openmeta, "is_product_excluded", None)
        self._aliases = tuple(alias for alias in aliases if not callable(excluded) or not excluded(alias.product))
        self._observer = observer
        products = tuple(catalog) if catalog is not None else load_product_catalog(catalog_path)
        filter_product = getattr(openmeta, "filter_product_metadata", None)
        visible_products: list[ProductMetadata] = []
        for metadata in products:
            if callable(filter_product):
                filtered = filter_product(metadata)
                if filtered is not None:
                    visible_products.append(filtered)
            elif not callable(excluded) or not excluded(metadata.product):
                visible_products.append(metadata)
        self._index = _ProductIndex(tuple(visible_products), self._aliases)

    async def resolve(self, requested_product: str) -> ProductResolution:
        normalized = requested_product.strip(_ASCII_WHITESPACE) if isinstance(requested_product, str) else ""
        if not normalized or _SAFE_PRODUCT.fullmatch(normalized) is None:
            return self._emit(
                ProductResolution(
                    requested_product=requested_product if isinstance(requested_product, str) else "",
                    normalized_product=normalized,
                    metadata=None,
                    strategy="unavailable",
                    confidence="none",
                    error="protocol_error",
                )
            )

        excluded = getattr(self._openmeta, "is_product_excluded", None)
        if callable(excluded) and excluded(normalized):
            return self._emit(
                ProductResolution(
                    requested_product=requested_product,
                    normalized_product=normalized,
                    metadata=None,
                    strategy="excluded",
                    confidence="none",
                    error="not_found",
                    cache_status="negative_hit",
                )
            )

        match = self._index.match(normalized)
        strategy = match.strategy
        if strategy == "exact_code" and normalized != requested_product:
            strategy = "trimmed_exact"
        elif strategy == "not_found":
            strategy = "unverified"
        return self._emit(
            ProductResolution(
                requested_product=requested_product,
                normalized_product=normalized,
                metadata=match.metadata,
                strategy=strategy,
                confidence=match.confidence,
                suggestions=match.suggestions,
                error=None if match.metadata is not None else "not_found",
            )
        )

    def _emit(self, resolution: ProductResolution) -> ProductResolution:
        if self._observer is not None:
            self._observer(resolution)
        return resolution


def load_product_aliases(path: Path | None = _ALIASES_PATH) -> tuple[ProductAlias, ...]:
    if path is None:
        return ()
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, Mapping) or raw.get("schema_version") != 1:
        raise ValueError("invalid product alias configuration")
    aliases = raw.get("aliases")
    if not isinstance(aliases, Mapping):
        raise ValueError("invalid product alias configuration")
    result: list[ProductAlias] = []
    seen: set[str] = set()
    for alias, entry in aliases.items():
        if not isinstance(alias, str) or _SAFE_PRODUCT.fullmatch(alias) is None or alias.casefold() in seen:
            raise ValueError("invalid product alias configuration")
        if not isinstance(entry, Mapping):
            raise ValueError("invalid product alias configuration")
        product = entry.get("product")
        reason = entry.get("reason")
        source = entry.get("source")
        if any(not isinstance(value, str) or not value for value in (product, reason, source)):
            raise ValueError("invalid product alias configuration")
        if _SAFE_PRODUCT.fullmatch(product) is None:
            raise ValueError("invalid product alias configuration")
        seen.add(alias.casefold())
        result.append(ProductAlias(alias=alias, product=product, reason=reason, source=source))
    return tuple(result)


def load_product_catalog(path: Path = _CATALOG_PATH) -> tuple[ProductMetadata, ...]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, Mapping):
        raise ValueError("invalid product catalog")
    meta = raw.get("_meta")
    products = raw.get("products")
    if (
        not isinstance(meta, Mapping)
        or meta.get("schema_version") != 1
        or not isinstance(products, list)
        or not products
    ):
        raise ValueError("invalid product catalog")
    expected_digest = meta.get("catalog_products_sha256")
    if not isinstance(expected_digest, str) or expected_digest != product_catalog_digest(products):
        raise ValueError("invalid product catalog digest")
    result: list[ProductMetadata] = []
    seen: set[str] = set()
    for raw_product in products:
        if not isinstance(raw_product, Mapping):
            raise ValueError("invalid product catalog")
        short_name = raw_product.get("shortName")
        if short_name is not None and normalize_catalog_short_name(short_name) != short_name:
            raise ValueError("invalid product catalog shortName")
        try:
            metadata = ProductMetadata.from_openmeta(raw_product)
        except ValueError as exc:
            raise ValueError("invalid product catalog") from exc
        key = metadata.product.casefold()
        if key in seen:
            raise ValueError("duplicate product catalog code")
        seen.add(key)
        result.append(metadata)
    return tuple(result)


def product_catalog_digest(products: Sequence[Mapping[str, object]]) -> str:
    encoded = json.dumps(products, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def normalize_catalog_short_name(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip(_ASCII_WHITESPACE)
    return normalized if _safe_alias(normalized) is not None else None


def _safe_alias(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip(_ASCII_WHITESPACE).casefold()
    if normalized in _PLACEHOLDER_ALIASES or _SAFE_PRODUCT.fullmatch(normalized) is None:
        return None
    return normalized


def _separator_key(value: str) -> str:
    return value.replace("-", "_")


def _deleted_forms(value: str) -> set[str]:
    return {value[:index] + value[index + 1 :] for index in range(len(value))}


def _single_edit_apart(left: str, right: str) -> bool:
    if left == right or abs(len(left) - len(right)) > 1:
        return False
    if len(left) == len(right):
        mismatches = [index for index, pair in enumerate(zip(left, right)) if pair[0] != pair[1]]
        if len(mismatches) == 1:
            return True
        return (
            len(mismatches) == 2
            and mismatches[1] == mismatches[0] + 1
            and left[mismatches[0]] == right[mismatches[1]]
            and left[mismatches[1]] == right[mismatches[0]]
        )
    shorter, longer = (left, right) if len(left) < len(right) else (right, left)
    short_index = long_index = differences = 0
    while short_index < len(shorter) and long_index < len(longer):
        if shorter[short_index] == longer[long_index]:
            short_index += 1
            long_index += 1
            continue
        differences += 1
        long_index += 1
        if differences > 1:
            return False
    return True


def _freeze_sets(values: Mapping[str, set[str]]) -> Mapping[str, tuple[str, ...]]:
    return MappingProxyType({key: tuple(sorted(items)) for key, items in values.items()})
