from __future__ import annotations

import textwrap

from iac_code.tools.cloud.aliyun.ros_validation.model import (
    MaterializedTemplateSource,
    RequestValidationContext,
    Severity,
)
from iac_code.tools.cloud.aliyun.ros_validation.terraform_naming import (
    build_catalog,
    load_terraform_naming_catalog,
)
from iac_code.tools.cloud.aliyun.ros_validation.terraform_workspace import iter_resource_blocks
from iac_code.tools.cloud.aliyun.ros_validation.validator import validate_ros_template


def validate_workspace(files: dict[str, str], *, transform: str = "Aliyun::OpenTofu-v1.8"):
    workspace = "\n".join(
        "  {}: |\n{}".format(name, textwrap.indent(textwrap.dedent(content).strip("\n"), " " * 4))
        for name, content in files.items()
    )
    body = "ROSTemplateFormatVersion: 2015-09-01\nTransform: {}\nWorkspace:\n{}\n".format(transform, workspace)
    return validate_ros_template(
        MaterializedTemplateSource(body),
        RequestValidationContext(action="ValidateTemplate"),
    )


def naming_diagnostics(report):
    return [item for item in report.diagnostics if item.code in {"ROS1130", "ROS1131"}]


def test_catalog_covers_documented_alicloud_types() -> None:
    catalog = load_terraform_naming_catalog()

    assert catalog.knows_resource_type("alicloud_route_entry")
    assert catalog.knows_resource_type("alicloud_alb_server_group")
    assert not catalog.knows_resource_type("alicloud_vpc_route_entry")


def test_catalog_never_flags_a_documented_name() -> None:
    catalog = load_terraform_naming_catalog()

    for resource_type in catalog.resource_types:
        assert catalog.resource_type_correction(resource_type) is None
        spec = catalog.get(resource_type)
        assert spec is not None
        for argument in spec.properties:
            assert catalog.argument_correction(resource_type, argument) is None


def test_catalog_corrects_an_extra_type_token() -> None:
    catalog = load_terraform_naming_catalog()

    assert catalog.resource_type_correction("alicloud_vpc_route_entry") == "alicloud_route_entry"
    assert catalog.resource_type_correction("alicloud_alb_server_group_attachment") == "alicloud_alb_server_group"


def test_catalog_corrects_separator_and_spelling_mistakes() -> None:
    catalog = load_terraform_naming_catalog()

    assert catalog.argument_correction("alicloud_route_entry", "next_hop_type") == "nexthop_type"
    assert catalog.argument_correction("alicloud_route_entry", "next_hop_id") == "nexthop_id"
    assert catalog.argument_correction("alicloud_route_entry", "destination_cidr_block") == "destination_cidrblock"


def test_catalog_stays_silent_for_meta_arguments_and_unknown_names() -> None:
    catalog = load_terraform_naming_catalog()

    for meta in ("count", "depends_on", "for_each", "lifecycle", "provider"):
        assert catalog.argument_correction("alicloud_route_entry", meta) is None
    # A vendored snapshot cannot prove that an unfamiliar name is wrong.
    assert catalog.resource_type_correction("alicloud_some_unreleased_product_thing") is None
    assert catalog.argument_correction("alicloud_route_entry", "some_unreleased_argument") is None


def test_catalog_merges_duplicate_terraform_types() -> None:
    catalog = build_catalog(
        [
            {
                "ResourceType": {"ROS": "ALIYUN::ECS::Route", "Terraform": "alicloud_route_entry"},
                "Properties": [{"ROS": "NextHopId", "Terraform": "nexthop_id"}],
            },
            {
                "ResourceType": {"ROS": None, "Terraform": "alicloud_route_entry"},
                "Properties": [{"ROS": "RouteTableId", "Terraform": "route_table_id"}],
            },
        ]
    )

    spec = catalog.get("alicloud_route_entry")
    assert spec is not None
    assert spec.properties == {"nexthop_id", "route_table_id"}
    assert spec.ros_resource_type == "ALIYUN::ECS::Route"


def test_reports_misnamed_resource_type_with_the_documented_name() -> None:
    report = validate_workspace(
        {
            "main.tf": """
            resource "alicloud_vpc_route_entry" "default" {
              route_table_id        = alicloud_vpc.main.route_table_id
              destination_cidrblock = "0.0.0.0/0"
            }
            """
        }
    )

    diagnostics = naming_diagnostics(report)
    assert [item.code for item in diagnostics] == ["ROS1130"]
    assert diagnostics[0].severity == Severity.ERROR
    assert diagnostics[0].expected == "alicloud_route_entry"
    assert diagnostics[0].actual == "alicloud_vpc_route_entry"
    assert "alicloud_route_entry" in diagnostics[0].suggestion


def test_reports_misnamed_arguments_of_a_documented_resource() -> None:
    report = validate_workspace(
        {
            "main.tf": """
            resource "alicloud_route_entry" "default" {
              route_table_id        = alicloud_vpc.main.route_table_id
              destination_cidrblock = "0.0.0.0/0"
              next_hop_type         = "NatGateway"
              next_hop_id           = alicloud_nat_gateway.main.id
            }
            """
        }
    )

    diagnostics = naming_diagnostics(report)
    assert [item.code for item in diagnostics] == ["ROS1131", "ROS1131"]
    assert [item.actual for item in diagnostics] == ["next_hop_type", "next_hop_id"]
    assert [item.expected for item in diagnostics] == ["nexthop_type", "nexthop_id"]


def test_accepts_a_correctly_named_terraform_workspace() -> None:
    report = validate_workspace(
        {
            "main.tf": """
            data "alicloud_zones" "available" {
              available_resource_creation = "VSwitch"
            }

            data "alicloud_cen_transit_router_service" "open" {
              enable = "On"
            }

            resource "alicloud_vpc" "main" {
              vpc_name   = "ha-vpc"
              cidr_block = "10.0.0.0/16"
              tags = {
                next_hop_type = "a nested block is not an argument"
              }
            }

            resource "alicloud_route_entry" "default" {
              route_table_id        = alicloud_vpc.main.route_table_id
              destination_cidrblock = "0.0.0.0/0"
              nexthop_type          = "NatGateway"
              nexthop_id            = alicloud_nat_gateway.main.id
              depends_on            = [alicloud_vpc.main]
            }

            resource "alicloud_alb_server_group" "web" {
              server_group_name = "web"
              vpc_id            = alicloud_vpc.main.id
              servers {
                server_id   = alicloud_instance.web.id
                server_type = "Ecs"
                port        = 80
              }
            }
            """
        }
    )

    assert naming_diagnostics(report) == []


def test_ignores_comments_heredocs_and_non_alicloud_providers() -> None:
    report = validate_workspace(
        {
            "main.tf": """
            # resource "alicloud_vpc_route_entry" "commented" {}
            /* resource "alicloud_vpc_route_entry" "block_commented" {} */

            resource "random_password" "db" {
              length      = 16
              unknown_arg = true
            }

            resource "alicloud_instance" "web" {
              instance_type = var.instance_type
              user_data     = <<-EOT
                #!/bin/bash
                next_hop_type = "not terraform source"
              EOT
            }
            """
        }
    )

    assert naming_diagnostics(report) == []


def test_ignores_non_terraform_and_non_tf_workspace_entries() -> None:
    ros_native = validate_ros_template(
        MaterializedTemplateSource(
            "ROSTemplateFormatVersion: 2015-09-01\n"
            "Workspace:\n"
            "  main.tf: |\n"
            '    resource "alicloud_vpc_route_entry" "default" {}\n'
        ),
        RequestValidationContext(action="ValidateTemplate"),
    )
    assert naming_diagnostics(ros_native) == []

    non_tf = validate_workspace(
        {"README.md": 'resource "alicloud_vpc_route_entry" "default" {\n  next_hop_type = "NatGateway"\n}'}
    )
    assert naming_diagnostics(non_tf) == []


def test_resource_block_scanner_extracts_top_level_arguments_only() -> None:
    blocks = list(
        iter_resource_blocks(
            textwrap.dedent(
                """
                resource "alicloud_alb_server_group" "web" {
                  server_group_name = "web"
                  health_check_config {
                    health_check_enabled = false
                  }
                  servers {
                    port = 80
                  }
                  tags = {
                    nested = "value"
                  }
                }
                """
            )
        )
    )

    assert len(blocks) == 1
    assert blocks[0].resource_type == "alicloud_alb_server_group"
    assert blocks[0].resource_name == "web"
    assert blocks[0].arguments == ("server_group_name", "tags")


def test_resource_block_scanner_skips_unbalanced_blocks() -> None:
    assert list(iter_resource_blocks('resource "alicloud_vpc" "main" {\n  vpc_name = "x"\n')) == []
