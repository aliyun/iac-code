"""Terraform (alicloud provider) resource type and property naming catalog.

The catalog is built from the vendored ROS/Terraform resource metadata in
``iac_code.pipeline.engine.architecture_metas``, which maps every documented ROS
resource type to its Terraform counterpart and every ROS property to its
Terraform property name.  Only the JSON payload is read here so the ROS tool
layer keeps no import dependency on the pipeline package.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from importlib.resources import files
from types import MappingProxyType
from typing import Any, Mapping

# Block-level arguments that Terraform itself defines for every resource, so they
# never appear in the provider schema derived from the vendored metadata.
META_ARGUMENTS = frozenset({"count", "depends_on", "for_each", "lifecycle", "provider", "provisioner"})

_TOKEN_SEPARATOR = re.compile(r"[_-]+")


def _normalize(name: str) -> str:
    """Drop word separators so ``next_hop_type`` and ``nexthop_type`` collide."""

    return _TOKEN_SEPARATOR.sub("", name).lower()


def _tokens(name: str) -> tuple[str, ...]:
    return tuple(part for part in _TOKEN_SEPARATOR.split(name.lower()) if part)


def _edit_distance_within_one(left: str, right: str) -> bool:
    if abs(len(left) - len(right)) > 1:
        return False
    if left == right:
        return True
    if len(left) > len(right):
        left, right = right, left
    # ``left`` is now the shorter (or equal length) string.
    for index, (short, long) in enumerate(zip(left, right)):
        if short == long:
            continue
        if len(left) == len(right):
            return left[index + 1 :] == right[index + 1 :]
        return left[index:] == right[index + 1 :]
    return True


def _token_multiset_off_by_one(left: tuple[str, ...], right: tuple[str, ...]) -> bool:
    """Detect a single extra or missing token, as in ``alicloud_vpc_route_entry``."""

    if abs(len(left) - len(right)) != 1:
        return False
    if len(left) > len(right):
        left, right = right, left
    remaining = list(right)
    for token in left:
        if token not in remaining:
            return False
        remaining.remove(token)
    return len(remaining) == 1


def _closest(name: str, candidates: set[str]) -> str | None:
    """Return the single closest correction for ``name``.

    A candidate whose tokens are all present in ``name`` wins, because a spurious
    extra token (``alicloud_vpc_route_entry`` for ``alicloud_route_entry``) is a
    more likely mistake than substituting a token for a different real product.
    """

    if not candidates:
        return None
    normalized = _normalize(name)
    tokens = set(_tokens(name))
    return min(
        candidates,
        key=lambda candidate: (
            _normalize(candidate) != normalized,
            not set(_tokens(candidate)) <= tokens,
            abs(len(candidate) - len(name)),
            candidate,
        ),
    )


@dataclass(frozen=True)
class TerraformResourceSpec:
    resource_type: str
    ros_resource_type: str | None
    properties: frozenset[str]
    attributes: frozenset[str]


class TerraformNamingCatalog:
    """Known alicloud resource types with the property names they accept."""

    def __init__(self, specs: Mapping[str, TerraformResourceSpec]) -> None:
        self._specs = MappingProxyType(dict(specs))
        normalized: dict[str, list[str]] = {}
        for resource_type in self._specs:
            normalized.setdefault(_normalize(resource_type), []).append(resource_type)
        self._normalized_types = MappingProxyType({key: tuple(sorted(value)) for key, value in normalized.items()})

    def __len__(self) -> int:
        return len(self._specs)

    @property
    def resource_types(self) -> frozenset[str]:
        return frozenset(self._specs)

    def get(self, resource_type: str) -> TerraformResourceSpec | None:
        return self._specs.get(resource_type)

    def knows_resource_type(self, resource_type: str) -> bool:
        return resource_type in self._specs

    def resource_type_correction(self, resource_type: str) -> str | None:
        """Return the documented type name ``resource_type`` was likely meant to be.

        The catalog is a vendored snapshot, so a missing type is not proof of an
        error.  Only a near-identical documented name is reported, which keeps
        genuinely new provider resources from being rejected locally.
        """

        if self.knows_resource_type(resource_type):
            return None
        exact = self._normalized_types.get(_normalize(resource_type), ())
        if exact:
            return _closest(resource_type, set(exact))
        tokens = _tokens(resource_type)
        normalized = _normalize(resource_type)
        return _closest(
            resource_type,
            {
                candidate
                for candidate in self._specs
                if _token_multiset_off_by_one(tokens, _tokens(candidate))
                or _edit_distance_within_one(normalized, _normalize(candidate))
            },
        )

    def argument_correction(self, resource_type: str, argument: str) -> str | None:
        """Return the documented argument name ``argument`` was likely meant to be."""

        spec = self._specs.get(resource_type)
        if spec is None or argument in spec.properties or argument in META_ARGUMENTS:
            return None
        normalized = _normalize(argument)
        tokens = _tokens(argument)
        return _closest(
            argument,
            {
                candidate
                for candidate in spec.properties
                if _normalize(candidate) == normalized
                or _token_multiset_off_by_one(tokens, _tokens(candidate))
                or _edit_distance_within_one(normalized, _normalize(candidate))
            },
        )


def _terraform_names(entries: Any) -> set[str]:
    names: set[str] = set()
    if not isinstance(entries, list):
        return names
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        name = entry.get("Terraform")
        if isinstance(name, str) and name:
            names.add(name)
        names |= _terraform_names(entry.get("Properties"))
    return names


def build_catalog(config: Any) -> TerraformNamingCatalog:
    specs: dict[str, TerraformResourceSpec] = {}
    if not isinstance(config, list):
        return TerraformNamingCatalog({})
    for item in config:
        if not isinstance(item, Mapping):
            continue
        resource_type_names = item.get("ResourceType")
        if not isinstance(resource_type_names, Mapping):
            continue
        resource_type = resource_type_names.get("Terraform")
        if not isinstance(resource_type, str) or not resource_type:
            continue
        ros_resource_type = resource_type_names.get("ROS")
        properties = _terraform_names(item.get("Properties"))
        attributes = _terraform_names(item.get("Attributes"))
        existing = specs.get(resource_type)
        if existing is not None:
            properties |= existing.properties
            attributes |= existing.attributes
            ros_resource_type = existing.ros_resource_type or ros_resource_type
        specs[resource_type] = TerraformResourceSpec(
            resource_type=resource_type,
            ros_resource_type=ros_resource_type if isinstance(ros_resource_type, str) else None,
            properties=frozenset(properties),
            attributes=frozenset(attributes),
        )
    return TerraformNamingCatalog(specs)


@lru_cache(maxsize=1)
def load_terraform_naming_catalog() -> TerraformNamingCatalog:
    # Addressed through the top-level package so importing the catalog does not
    # pull in the pipeline engine module tree.
    data_path = files("iac_code").joinpath("pipeline/engine/architecture_metas/config.json")
    if not data_path.is_file():
        return TerraformNamingCatalog({})
    return build_catalog(json.loads(data_path.read_text(encoding="utf-8")))
