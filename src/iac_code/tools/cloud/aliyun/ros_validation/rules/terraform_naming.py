"""Local naming checks for alicloud resources inside ROS Terraform templates.

ROS Terraform/OpenTofu templates carry their ``.tf`` sources in the top-level
``Workspace`` section, so resource type and property names can be checked before
the template reaches the cloud.  Only names that are provably misspellings of a
documented name are reported; unknown names without a close documented match stay
silent because the naming catalog is a vendored snapshot.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from iac_code.i18n import _
from iac_code.tools.cloud.aliyun.ros_validation.facts import FactBuildResult, RulePhase
from iac_code.tools.cloud.aliyun.ros_validation.model import (
    Category,
    Diagnostic,
    RosPath,
    Severity,
    make_diagnostic,
    mapping_segment,
)
from iac_code.tools.cloud.aliyun.ros_validation.template_kind import is_terraform_template
from iac_code.tools.cloud.aliyun.ros_validation.terraform_naming import (
    TerraformNamingCatalog,
    load_terraform_naming_catalog,
)
from iac_code.tools.cloud.aliyun.ros_validation.terraform_workspace import ResourceBlock, iter_resource_blocks

PARSED_TEMPLATE = "parsed-template"
TERRAFORM_NAMING_CATALOG = "terraform-naming-catalog"
_ALICLOUD_PREFIX = "alicloud_"


@dataclass(frozen=True)
class TerraformNamingCatalogProvider:
    provider_id: str = "builtin.terraform-naming-catalog"
    phase: RulePhase = RulePhase.STRUCTURE
    requires: frozenset[str] = frozenset()
    optional_requires: frozenset[str] = frozenset()
    provides: frozenset[str] = frozenset({TERRAFORM_NAMING_CATALOG})

    def build(self, context: Any) -> FactBuildResult:
        del context
        return FactBuildResult(provided={TERRAFORM_NAMING_CATALOG: load_terraform_naming_catalog()})


@dataclass(frozen=True)
class TerraformNamingRule:
    rule_id: str = "builtin.terraform-naming"
    phase: RulePhase = RulePhase.RESOURCES
    requires: frozenset[str] = frozenset({PARSED_TEMPLATE, TERRAFORM_NAMING_CATALOG})
    optional_requires: frozenset[str] = frozenset()

    def check(self, context: Any) -> tuple[Diagnostic, ...]:
        parsed = context.fact_store.get_required(PARSED_TEMPLATE)
        catalog = context.fact_store.get_required(TERRAFORM_NAMING_CATALOG)
        if not isinstance(parsed.data, Mapping) or not is_terraform_template(parsed.data):
            return ()
        workspace = parsed.data.get("Workspace")
        if not isinstance(workspace, Mapping):
            return ()
        diagnostics: list[Diagnostic] = []
        for filename, content in workspace.items():
            if not isinstance(filename, str) or not filename.endswith(".tf") or not isinstance(content, str):
                continue
            path = (mapping_segment("Workspace"), mapping_segment(filename))
            for block in iter_resource_blocks(content):
                diagnostics.extend(_check_block(block, filename, path, parsed.source_map, catalog))
        return tuple(diagnostics)


def _check_block(
    block: ResourceBlock,
    filename: str,
    path: RosPath,
    source_map: Any,
    catalog: TerraformNamingCatalog,
) -> list[Diagnostic]:
    if not block.resource_type.startswith(_ALICLOUD_PREFIX):
        return []
    if correction := catalog.resource_type_correction(block.resource_type):
        return [
            make_diagnostic(
                code="ROS1130",
                severity=Severity.ERROR,
                category=Category.COMPATIBILITY,
                summary=_("Terraform resource type {} does not exist in the alicloud provider.").format(
                    block.resource_type
                ),
                detail=_(
                    "{file} declares resource {name} with this type; a ROS resource type name cannot be "
                    "translated into a Terraform type name literally."
                ).format(file=filename, name=block.resource_name),
                path=path,
                source_map=source_map,
                subject=block.resource_type,
                stable_args=("resource-type", filename, block.resource_type, correction),
                expected=correction,
                actual=block.resource_type,
                suggestion=_("Use {} instead.").format(correction),
            )
        ]
    if not catalog.knows_resource_type(block.resource_type):
        return []
    diagnostics: list[Diagnostic] = []
    for argument in block.arguments:
        correction = catalog.argument_correction(block.resource_type, argument)
        if correction is None:
            continue
        diagnostics.append(
            make_diagnostic(
                code="ROS1131",
                severity=Severity.ERROR,
                category=Category.COMPATIBILITY,
                summary=_("Terraform resource {type} has no argument named {argument}.").format(
                    type=block.resource_type, argument=argument
                ),
                detail=_("{file} sets this argument on resource {name}.").format(
                    file=filename, name=block.resource_name
                ),
                path=path,
                source_map=source_map,
                subject="{}.{}".format(block.resource_type, argument),
                stable_args=("resource-argument", filename, block.resource_type, argument, correction),
                expected=correction,
                actual=argument,
                suggestion=_("Use {} instead.").format(correction),
            )
        )
    return diagnostics
