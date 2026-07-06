"""Architecture diagram metadata loaded from vendored ROS resource meta files."""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from typing import Any


def normalize_resource_type(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    if value.startswith("ROS/"):
        value = value.removeprefix("ROS/")
    elif value.startswith("Terraform/"):
        return None
    return value if value.startswith("ALIYUN::") else None


def _normalize_ros_field(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    return value.removeprefix("ROS/")


@dataclass(frozen=True)
class RelatedProperty:
    name: str
    path: tuple[str, ...]
    targets: tuple[str, ...]
    value_type: str | None = None


@dataclass(frozen=True)
class MainResourceType:
    resource_type: str
    ref_property: str


@dataclass(frozen=True)
class ResourceMeta:
    resource_type: str
    name_zh: str | None
    name_en: str | None
    product_code: str | None
    category_code: str | None
    related_properties: tuple[RelatedProperty, ...]
    main_resource_type: MainResourceType | None

    @cached_property
    def related_properties_by_name(self) -> dict[str, RelatedProperty]:
        return {prop.name: prop for prop in self.related_properties}


@dataclass(frozen=True)
class ProductMeta:
    product_code: str
    ros_code: str | None
    name_zh: str | None
    name_en: str | None
    category_code: str | None


class ArchitectureMetaRepository:
    """Indexed view over the vendored raw architecture metadata JSON files."""

    _default: ArchitectureMetaRepository | None = None

    def __init__(
        self,
        *,
        resources: dict[str, ResourceMeta] | None = None,
        products: dict[str, ProductMeta] | None = None,
        product_categories: dict[str, str] | None = None,
    ) -> None:
        self._resources = resources or {}
        self._products = products or {}
        self._product_categories = product_categories or {}

    @classmethod
    def load_default(cls) -> ArchitectureMetaRepository:
        if cls._default is None:
            cls._default = cls.from_directory(Path(__file__).with_name("architecture_metas"))
        return cls._default

    @classmethod
    def from_directory(cls, directory: Path) -> ArchitectureMetaRepository:
        categories = _load_json(directory / "Categories.json")
        products = _load_json(directory / "products.json")
        config = _load_json(directory / "config.json")
        return cls.from_raw(categories=categories, products=products, config=config)

    @classmethod
    def from_raw(cls, *, categories: Any, products: Any, config: Any) -> ArchitectureMetaRepository:
        product_categories = _build_product_categories(categories)
        product_index = _build_products(products, product_categories)
        resources = _build_resources(config, product_categories)
        return cls(resources=resources, products=product_index, product_categories=product_categories)

    def get_resource(self, resource_type: str) -> ResourceMeta | None:
        return self._resources.get(resource_type)

    def get_product(self, product_code: str) -> ProductMeta | None:
        return self._products.get(product_code)

    def category_code_for_product(self, product_code: str) -> str | None:
        return self._product_categories.get(product_code)


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _build_product_categories(categories: Any) -> dict[str, str]:
    product_categories: dict[str, str] = {}
    if not isinstance(categories, list):
        return product_categories
    for category in categories:
        if not isinstance(category, dict):
            continue
        code = category.get("CategoryCode")
        product_codes = category.get("ProductCodes")
        if not isinstance(code, str) or not isinstance(product_codes, list):
            continue
        for product_code in product_codes:
            if isinstance(product_code, str):
                product_categories.setdefault(product_code, code)
    return product_categories


def _build_products(products: Any, product_categories: dict[str, str]) -> dict[str, ProductMeta]:
    product_index: dict[str, ProductMeta] = {}
    if not isinstance(products, list):
        return product_index
    for product in products:
        if not isinstance(product, dict):
            continue
        product_code = product.get("ProductCode")
        if not isinstance(product_code, str) or product_code in product_index:
            continue
        name = product.get("Name") if isinstance(product.get("Name"), dict) else {}
        relevant = product.get("RelevantCodes") if isinstance(product.get("RelevantCodes"), dict) else {}
        product_index[product_code] = ProductMeta(
            product_code=product_code,
            ros_code=relevant.get("ROS") if isinstance(relevant.get("ROS"), str) else None,
            name_zh=name.get("zh") if isinstance(name.get("zh"), str) else None,
            name_en=name.get("en") if isinstance(name.get("en"), str) else None,
            category_code=product_categories.get(product_code),
        )
    return product_index


def _build_resources(config: Any, product_categories: dict[str, str]) -> dict[str, ResourceMeta]:
    resources: dict[str, ResourceMeta] = {}
    if not isinstance(config, list):
        return resources
    for item in config:
        if not isinstance(item, dict):
            continue
        resource_type = normalize_resource_type((item.get("ResourceType") or {}).get("ROS"))
        if resource_type is None:
            continue
        name = item.get("Name") if isinstance(item.get("Name"), dict) else {}
        product_code = item.get("ProductCode") if isinstance(item.get("ProductCode"), str) else None
        resources[resource_type] = ResourceMeta(
            resource_type=resource_type,
            name_zh=name.get("zh") if isinstance(name.get("zh"), str) else None,
            name_en=name.get("en") if isinstance(name.get("en"), str) else None,
            product_code=product_code,
            category_code=product_categories.get(product_code or ""),
            related_properties=tuple(_iter_related_properties(item.get("Properties"))),
            main_resource_type=_build_main_resource_type(item.get("MainResourceType")),
        )
    return resources


def _build_main_resource_type(value: Any) -> MainResourceType | None:
    if not isinstance(value, dict):
        return None
    resource_type = normalize_resource_type(value.get("ResourceType"))
    ref_property = _normalize_ros_field(value.get("RefProperty"))
    if resource_type is None or ref_property is None:
        return None
    return MainResourceType(resource_type=resource_type, ref_property=ref_property)


def _iter_related_properties(properties: Any, path_prefix: tuple[str, ...] = ()) -> list[RelatedProperty]:
    related: list[RelatedProperty] = []
    if not isinstance(properties, list):
        return related
    for prop in properties:
        if not isinstance(prop, dict):
            continue
        name = _normalize_ros_field(prop.get("ROS"))
        path = (*path_prefix, name) if name else path_prefix
        targets = tuple(
            target
            for target in (normalize_resource_type(item.get("ResourceType")) for item in prop.get("RelatedTo", []))
            if target is not None
        )
        if name and targets:
            related.append(
                RelatedProperty(
                    name=name,
                    path=path,
                    targets=targets,
                    value_type=prop.get("Type") if isinstance(prop.get("Type"), str) else None,
                )
            )
        related.extend(_iter_related_properties(prop.get("Properties"), path))
    return related
