from __future__ import annotations

import itertools
import json
import re
import string
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import pytest

from iac_code.tools.cloud.aliyun.openmeta import MetadataFetch, ProductMetadata
from iac_code.tools.cloud.aliyun.product_resolver import (
    ProductResolver,
    _ProductIndex,
    _safe_alias,
    load_product_aliases,
    load_product_catalog,
)

FIXTURE = Path(__file__).parent / "fixtures" / "openmeta" / "product_matching_catalog.json"
PRODUCT_DATA = Path(__file__).parents[4] / "src/iac_code/tools/cloud/aliyun/data/openmeta"
ALIASES = PRODUCT_DATA / "product_aliases.yml"
CATALOG = PRODUCT_DATA / "product_catalog.json"
SAFE_PRODUCT = re.compile(r"^[a-z0-9][a-z0-9_-]{0,127}$")
EDIT_ALPHABET = string.ascii_lowercase + string.digits + "-_"


def product(code: str, *, short_name: str | None = None) -> ProductMetadata:
    return ProductMetadata(code, "2025-01-01", ("2025-01-01",), None, short_name=short_name)


def frozen_catalog() -> tuple[tuple[ProductMetadata, ...], set[str], dict[str, Any]]:
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    excluded = {item.casefold() for item in fixture["excluded_products"]}
    products = tuple(item for item in load_product_catalog(CATALOG) if item.product.casefold() not in excluded)
    return products, excluded, fixture["_meta"]


class CatalogOpenMeta:
    def __init__(
        self,
        products: tuple[ProductMetadata, ...],
        *,
        product_error: str = "not_found",
        excluded: set[str] | None = None,
    ) -> None:
        self.products = products
        self.product_error = product_error
        self.excluded = excluded or set()
        self.calls: list[tuple[str, ...]] = []

    async def get_product(self, requested: str) -> MetadataFetch[Any]:
        self.calls.append(("product", requested))
        value = next((item for item in self.products if item.product.casefold() == requested.casefold()), None)
        return MetadataFetch(
            value=value,
            source="fresh" if value is not None else None,
            error=None if value is not None else self.product_error,  # type: ignore[arg-type]
            cache_status="memory_fresh" if value is not None else "negative_hit",
        )

    async def list_products(self) -> MetadataFetch[Any]:
        self.calls.append(("products",))
        visible = tuple(item for item in self.products if item.product.casefold() not in self.excluded)
        return MetadataFetch(value=visible, source="fresh", error=None, cache_status="memory_fresh")

    def is_product_excluded(self, requested: str) -> bool:
        return requested.casefold() in self.excluded


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("requested", "expected_strategy"),
    [("ecs", "exact_code"), ("ECS", "exact_code"), ("\t ecs \n", "trimmed_exact")],
)
async def test_resolver_matches_exact_code_and_ascii_trim(
    requested: str,
    expected_strategy: str,
) -> None:
    openmeta = CatalogOpenMeta((product("Ecs"),))
    resolver = ProductResolver(openmeta, aliases_path=None, catalog=openmeta.products)

    result = await resolver.resolve(requested)

    assert result.canonical_product == "Ecs"
    assert result.strategy == expected_strategy
    assert result.confidence == "high"
    assert openmeta.calls == []


@pytest.mark.asyncio
async def test_resolver_matches_unique_separator_short_name_builtin_alias_and_single_edit(tmp_path: Path) -> None:
    aliases = tmp_path / "aliases.yml"
    aliases.write_text(
        """
schema_version: 1
aliases:
  ComputeService:
    product: Ecs
    reason: test
    source: test
""",
        encoding="utf-8",
    )
    products = (
        product("dyvmsapi-intl"),
        product("R-kvstore", short_name="Redis"),
        product("Ecs"),
        product("RdsAi"),
    )

    expectations = {
        "dyvmsapi_intl": ("dyvmsapi-intl", "separator_normalized"),
        "redis": ("R-kvstore", "short_name"),
        "computeservice": ("Ecs", "builtin_alias"),
        "RdsAia": ("RdsAi", "single_edit"),
    }
    for requested, (canonical, strategy) in expectations.items():
        openmeta = CatalogOpenMeta(products)
        result = await ProductResolver(openmeta, aliases_path=aliases, catalog=products).resolve(requested)
        assert (result.canonical_product, result.strategy) == (canonical, strategy)
        assert openmeta.calls == []


@pytest.mark.asyncio
async def test_resolver_rejects_ambiguous_and_short_fuzzy_inputs_with_sanitized_suggestions() -> None:
    products = (product("Dysmsapi"), product("Dyvmsapi"), product("FC"))
    openmeta = CatalogOpenMeta(products)
    resolver = ProductResolver(openmeta, aliases_path=None, catalog=products)

    ambiguous = await resolver.resolve("dy0msapi")
    short = await resolver.resolve("FZ")

    assert ambiguous.metadata is None
    assert ambiguous.strategy == "single_edit_ambiguous"
    assert ambiguous.suggestions == ("Dysmsapi", "Dyvmsapi")
    assert short.metadata is None
    assert short.strategy == "unverified"
    assert short.outcome == "unverified"
    assert short.suggestions == ()


@pytest.mark.asyncio
async def test_resolver_rejects_a_synthetic_separator_collision() -> None:
    openmeta = CatalogOpenMeta((product("foo-bar_baz"), product("foo_bar-baz")))

    result = await ProductResolver(openmeta, aliases_path=None, catalog=openmeta.products).resolve("foo-bar-baz")

    assert result.metadata is None
    assert result.strategy == "separator_ambiguous"
    assert result.suggestions == ("foo-bar_baz", "foo_bar-baz")


@pytest.mark.asyncio
async def test_resolver_never_recovers_a_product_level_exclusion() -> None:
    openmeta = CatalogOpenMeta((product("Chatbot"), product("Eci")), excluded={"chatbot"})
    resolver = ProductResolver(openmeta, aliases_path=None, catalog=openmeta.products)

    exact = await resolver.resolve("Chatbot")
    typo = await resolver.resolve("Chatbo")

    assert exact.metadata is None
    assert exact.strategy == "excluded"
    assert exact.suggestions == ()
    assert typo.metadata is None
    assert typo.strategy == "unverified"
    assert typo.suggestions == ()
    assert openmeta.calls == []


@pytest.mark.asyncio
async def test_resolver_skips_aliases_whose_targets_are_explicitly_excluded(tmp_path: Path) -> None:
    aliases = tmp_path / "aliases.yml"
    aliases.write_text(
        """
schema_version: 1
aliases:
  ComputeService:
    product: Ecs
    reason: test
    source: test
""",
        encoding="utf-8",
    )
    openmeta = CatalogOpenMeta((product("Ecs"), product("Chatbot")), excluded={"ecs"})

    result = await ProductResolver(openmeta, aliases_path=aliases, catalog=openmeta.products).resolve("ComputeService")

    assert result.metadata is None
    assert result.strategy == "unverified"
    assert result.suggestions == ()


@pytest.mark.asyncio
async def test_resolver_fails_closed_when_alias_target_is_really_absent(tmp_path: Path) -> None:
    aliases = tmp_path / "aliases.yml"
    aliases.write_text(
        """
schema_version: 1
aliases:
  ComputeService:
    product: Ecs
    reason: test
    source: test
""",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="alias target is absent"):
        ProductResolver(
            CatalogOpenMeta((product("Chatbot"),)),
            aliases_path=aliases,
            catalog=(product("Chatbot"),),
        )


@pytest.mark.asyncio
async def test_resolver_never_reads_remote_product_catalog_even_when_openmeta_is_unavailable() -> None:
    openmeta = CatalogOpenMeta((), product_error="temporarily_unavailable")
    resolution = await ProductResolver(openmeta, aliases_path=None, catalog=(product("Ecs"),)).resolve("Ecs")

    assert resolution.canonical_product == "Ecs"
    assert resolution.error is None
    assert resolution.strategy == "exact_code"
    assert openmeta.calls == []


def test_frozen_product_fixture_has_reviewed_provenance() -> None:
    products, excluded, meta = frozen_catalog()
    raw_fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    rejected_short_names = [
        item["shortName"]
        for item in raw_fixture["products"]
        if item["shortName"] and _safe_alias(item["shortName"]) is None
    ]

    assert len(products) == 341
    assert excluded == set()
    assert len(rejected_short_names) == 48
    assert meta == {
        "schema_version": 1,
        "source_url": "https://api.aliyun.com/meta/v1/products.json?language=ZH_CN",
        "source_fetched_at": "2026-07-18T06:50:47.107773+00:00",
        "source_payload_sha256": "67eca398332b32a0705a9143d1f5ac1eb687704f67476d88fd2337d1aa987605",
        "fixture_products_sha256": "9f7d292d472feacd08d4e64d5fbe41b9d90d60267433674cac61a5a743590c49",
    }


def test_bundled_product_catalog_is_clean_and_has_reviewed_provenance() -> None:
    raw = json.loads(CATALOG.read_text(encoding="utf-8"))
    products = load_product_catalog(CATALOG)
    short_names = {item.product: item.short_name for item in products}

    assert len(products) == 341
    assert raw["_meta"] == {
        "schema_version": 1,
        "source_url": "https://api.aliyun.com/meta/v1/products.json?language=ZH_CN",
        "source_fetched_at": "2026-07-18T06:50:47.107773+00:00",
        "source_payload_sha256": "67eca398332b32a0705a9143d1f5ac1eb687704f67476d88fd2337d1aa987605",
        "catalog_products_sha256": "a1bf7b7f47b4773637255ebf163b339eb721a16b15a25e86ecc762ba2c476689",
    }
    assert short_names["cloud-siem"] == "SC"
    assert short_names["Cloudfw"] == "CFW"
    assert short_names["VoiceNavigator"] == "VN"
    assert short_names["CloudAPI"] is None
    assert short_names["SuperappNlp"] is None


def test_all_separator_variants_from_the_frozen_catalog_are_exhaustively_classified() -> None:
    products, _, _ = frozen_catalog()
    index = _ProductIndex(products, load_product_aliases(ALIASES))
    variants: dict[str, set[str]] = defaultdict(set)

    for metadata in products:
        positions = [index for index, character in enumerate(metadata.product) if character in "-_"]
        for replacements in itertools.product("-_", repeat=len(positions)):
            characters = list(metadata.product.casefold())
            for position, replacement in zip(positions, replacements):
                characters[position] = replacement
            variant = "".join(characters)
            if variant != metadata.product.casefold():
                variants[variant].add(metadata.product)

    assert sum("-" in item.product or "_" in item.product for item in products) == 44
    assert len(variants) == 50
    for variant, expected in variants.items():
        match = index.match(variant)
        assert match.strategy == "separator_normalized"
        assert match.metadata is not None
        assert match.metadata.product in expected


def test_all_short_names_and_builtin_aliases_from_the_frozen_catalog_are_exhaustively_classified() -> None:
    products, _, _ = frozen_catalog()
    aliases = load_product_aliases(ALIASES)
    index = _ProductIndex(products, aliases)
    codes = {item.product.casefold(): item.product for item in products}
    alias_targets: dict[str, set[str]] = defaultdict(set)
    invalid_short_names: list[tuple[str, str]] = []

    for metadata in products:
        alias = _safe_alias(metadata.short_name)
        if alias is None:
            if metadata.short_name:
                invalid_short_names.append((metadata.product, metadata.short_name))
            continue
        alias_targets[alias].add(metadata.product)
    for alias in aliases:
        alias_targets[alias.alias.casefold()].add(alias.product)

    assert sum(_safe_alias(metadata.short_name) is not None for metadata in products) == 211
    assert len(aliases) == 6
    counts = Counter()
    ambiguous: dict[str, tuple[str, ...]] = {}
    for alias, targets in alias_targets.items():
        match = index.match(alias)
        if alias in codes:
            counts["exact_code_wins"] += 1
            assert match.strategy == "exact_code"
            assert match.metadata is not None and match.metadata.product == codes[alias]
        elif len(targets) == 1:
            counts["unique"] += 1
            assert match.strategy in {"short_name", "builtin_alias"}
            assert match.metadata is not None and match.metadata.product in targets
        else:
            counts["ambiguous"] += 1
            ambiguous[alias] = tuple(sorted(targets))
            assert match.strategy == "alias_ambiguous"
            assert match.metadata is None

    assert len(alias_targets) == 209
    assert counts == {"exact_code_wins": 128, "unique": 79, "ambiguous": 2}
    assert ambiguous == {
        "iep": ("AIMath", "EduInterpreting"),
        "pai": ("PAICopilot", "PaiStudio"),
    }
    assert invalid_short_names == []
    for _product, alias in invalid_short_names:
        assert index.match(alias.casefold()).strategy not in {"short_name", "builtin_alias"}


@pytest.mark.parametrize(
    ("short_name", "expected"),
    (
        (" VN", "vn"),
        ("SC ", "sc"),
        ("CFW\n", "cfw"),
    ),
)
def test_safe_alias_normalizes_surrounding_ascii_whitespace(short_name: str, expected: str) -> None:
    assert _safe_alias(short_name) == expected


@pytest.mark.parametrize("short_name", ("暂无", '["Su","SuperappNlp"]', " "))
def test_safe_alias_rejects_placeholder_structured_and_empty_values(short_name: str) -> None:
    assert _safe_alias(short_name) is None


def test_all_single_edit_inputs_from_the_frozen_catalog_are_exhaustively_classified() -> None:
    products, _, _ = frozen_catalog()
    index = _ProductIndex(products, load_product_aliases(ALIASES))
    codes = {item.product.casefold(): item.product for item in products}
    mutations: dict[str, set[str]] = defaultdict(set)

    for key, canonical in codes.items():
        if len(key) < 5:
            continue
        local = {key[:position] + key[position + 1 :] for position in range(len(key))}
        local.update(
            key[:position] + character + key[position + 1 :]
            for position in range(len(key))
            for character in EDIT_ALPHABET
            if character != key[position]
        )
        local.update(
            key[:position] + character + key[position:]
            for position in range(len(key) + 1)
            for character in EDIT_ALPHABET
        )
        local.update(
            key[:position] + key[position + 1] + key[position] + key[position + 2 :]
            for position in range(len(key) - 1)
            if key[position] != key[position + 1]
        )
        for variant in local:
            if len(variant) >= 5 and SAFE_PRODUCT.fullmatch(variant):
                mutations[variant].add(canonical)

    counts = Counter()
    ambiguous_groups: Counter[tuple[str, ...]] = Counter()
    for variant, expected_sources in mutations.items():
        match = index.match(variant)
        if variant in codes:
            counts["exact_code_wins"] += 1
            assert match.strategy == "exact_code"
            assert match.metadata is not None and match.metadata.product == codes[variant]
        elif match.strategy == "separator_normalized":
            counts["separator_unique"] += 1
            assert match.metadata is not None
        elif match.strategy in {"short_name", "builtin_alias"}:
            counts["alias_unique"] += 1
            assert match.metadata is not None
        elif len(expected_sources) == 1:
            counts["fuzzy_unique"] += 1
            assert match.strategy == "single_edit"
            assert match.metadata is not None and match.metadata.product in expected_sources
        else:
            counts["fuzzy_ambiguous"] += 1
            expected_group = tuple(sorted(expected_sources))
            ambiguous_groups[expected_group] += 1
            assert match.strategy == "single_edit_ambiguous"
            assert match.metadata is None
            assert set(match.suggestions).issubset(expected_sources)

    assert len(mutations) == 188_008
    assert counts == {
        "fuzzy_unique": 187_689,
        "separator_unique": 47,
        "alias_unique": 4,
        "fuzzy_ambiguous": 257,
        "exact_code_wins": 11,
    }
    assert ambiguous_groups == {
        ("ADBAI", "RdsAi"): 2,
        ("AiSearchEngine", "searchengine"): 2,
        ("DtsAI", "RdsAi"): 5,
        ("Dyplsapi", "Dypnsapi"): 38,
        ("Dyplsapi", "Dypnsapi", "Dysmsapi", "Dyvmsapi"): 1,
        ("Dyplsapi", "Dysmsapi"): 1,
        ("Dyplsapi", "Dytnsapi"): 1,
        ("Dyplsapi", "Dyvmsapi"): 1,
        ("Dypnsapi", "Dysmsapi", "Dytnsapi"): 1,
        ("Dypnsapi", "Dytnsapi"): 37,
        ("Dypnsapi", "Dytnsapi", "Dyvmsapi"): 1,
        ("Dypnsapi-intl", "Dyvmsapi-intl"): 2,
        ("Dysmsapi", "Dytnsapi", "Dyvmsapi"): 1,
        ("Dysmsapi", "Dyvmsapi"): 37,
        ("ImageSearch", "imgsearch"): 2,
        ("IoTCC", "iovcc"): 38,
        ("PAIFlow", "appflow"): 2,
        ("SchedulerX3", "schedulerx2"): 38,
        ("cloudesl", "cloudsso"): 2,
        ("grace", "xtrace"): 4,
        ("pai-dlc", "pai-dsw"): 2,
        ("polardb", "polardbx"): 39,
    }
