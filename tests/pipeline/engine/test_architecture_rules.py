from iac_code.pipeline.engine.architecture_rules import ArchitectureRules


def test_loads_fixed_architecture_rules():
    rules = ArchitectureRules.load_default()

    assert "ALIYUN::ECS::VPC" in rules.network_layer_types
    assert rules.fallback_related_properties["VpcId"] == ("ALIYUN::ECS::VPC",)
    assert rules.edge_styles.auxiliary_relation == "solid_arrow"
    assert rules.edge_styles.direct_relation == "dotted_open"
    assert rules.edge_operator("solid_arrow") == "-->"
    assert rules.edge_operator("dotted_open") == "-.-"
    assert rules.edge_operator("dotted_arrow") == "-.->"
    assert rules.compact_flatten_layer_roles == frozenset({"security_group"})
    assert rules.containment_layer_types["cloud_network"] == ("ALIYUN::CEN::CenInstance",)


def test_loads_compact_concept_node_rules():
    rules = ArchitectureRules.load_default()
    concept = rules.compact_concept_nodes[0]

    assert concept.via_resource_types == frozenset({"ALIYUN::ESS::ScalingConfiguration"})
    assert concept.controller_property == "ScalingGroupId"
    assert concept.source_property == "InstanceId"
    assert concept.id_suffix == "ScaledEcs"
    assert concept.resource_type == "CONCEPT::ESS::ScaledECS"
    assert concept.label == {"en": "Scaled ECS instances", "zh": "伸缩 ECS 实例"}
    assert concept.controller_edge is not None
    assert concept.controller_edge.style == "solid_arrow"
    assert concept.controller_edge.label == {"en": "scales", "zh": "弹性伸缩"}
    assert concept.controller_edge.target == "concept"
    assert concept.source_edge is not None
    assert concept.source_edge.style == "dotted_arrow"
    assert concept.source_edge.label == {"en": "scaling config", "zh": "伸缩配置"}
    assert concept.source_edge.target == "controller"
    assert concept.group is not None
    assert concept.group.id_suffix == "ApplicationGroup"
    assert concept.group.resource_type == "CONCEPT::Application::ServerGroup"
    assert concept.group.label == {"en": "Application server group", "zh": "应用服务组"}
    assert concept.group.members == ("source", "concept")
    assert concept.group.rewrite_edge_kinds == ("traffic", "dependency", "inferred")


def test_loads_compact_relation_fold_rules():
    rules = ArchitectureRules.load_default()
    folds = {tuple(fold.via_resource_types): fold for fold in rules.compact_relation_folds}
    vpc_attachment_fold = folds[("ALIYUN::CEN::TransitRouterVpcAttachment",)]

    assert vpc_attachment_fold.source_property == "TransitRouterId"
    assert vpc_attachment_fold.target_property == "VpcId"
    assert vpc_attachment_fold.edge_style == "dotted_open"
    assert vpc_attachment_fold.edge_label == {"en": "VPC connection", "zh": "VPC连接"}
    assert vpc_attachment_fold.render_as == "source_attachment"


def test_loads_supplemental_relations_and_child_attachment_rules():
    rules = ArchitectureRules.load_default()

    assert rules.supplemental_related_properties["ALIYUN::VPC::EIPAssociation"]["InstanceId"] == (
        "ALIYUN::ECS::NetworkInterface",
        "ALIYUN::VPC::NatGateway",
    )
    assert rules.supplemental_related_properties["ALIYUN::VPC::CommonBandwidthPackageIp"]["Eips"] == (
        "ALIYUN::VPC::EIP",
    )
    assert rules.supplemental_related_properties["ALIYUN::VPC::AnycastEIPAssociation"]["BindInstanceId"] == (
        "ALIYUN::ECS::Instance",
        "ALIYUN::ECS::NetworkInterface",
        "ALIYUN::SLB::LoadBalancer",
        "ALIYUN::VPC::HaVip",
    )
    assert rules.supplemental_related_properties["ALIYUN::VPC::NetworkAclAssociation"]["Resources"] == (
        "ALIYUN::ECS::VSwitch",
    )
    peering_relations = rules.supplemental_related_properties["ALIYUN::VPC::PeeringRouterInterfaceBinding"]
    assert peering_relations["OppositeInterfaceId"] == ("ALIYUN::VPC::RouterInterface",)
    assert "ALIYUN::ECS::NetworkInterface" in rules.compact_attachment_marker_types
    assert {
        "ALIYUN::CloudPhone::RunCommand",
        "ALIYUN::ECS::Command",
        "ALIYUN::ECS::Invocation",
        "ALIYUN::ECS::RunCommand",
        "ALIYUN::ECS::RunCommandOfLifespan",
        "ALIYUN::SWAS::RunCommand",
    }.issubset(rules.compact_orchestration_action_types)
    assert "ALIYUN::ACTIONTRAIL::TrailLogging" not in rules.compact_orchestration_action_types
    assert "ALIYUN::CMS::MetricRuleTemplateDeployment" not in rules.compact_orchestration_action_types
    assert "ALIYUN::SLS::ServiceLog" not in rules.compact_orchestration_action_types

    nat_child_rules = {
        tuple(rule.resource_types): (rule.target_properties, rule.target_types, rule.label)
        for rule in rules.compact_child_attachments
    }
    assert nat_child_rules[("ALIYUN::VPC::SnatEntry",)] == (
        ("SnatTableId",),
        ("ALIYUN::VPC::NatGateway",),
        {"en": "SNAT entry", "zh": "SNAT条目"},
    )
    assert nat_child_rules[("ALIYUN::ECS::SNatEntry",)] == (
        ("SNatTableId",),
        ("ALIYUN::VPC::NatGateway",),
        {"en": "SNAT entry", "zh": "SNAT条目"},
    )
    assert nat_child_rules[("ALIYUN::VPC::ForwardEntry", "ALIYUN::ECS::ForwardEntry")] == (
        ("ForwardTableId",),
        ("ALIYUN::VPC::NatGateway",),
        {"en": "DNAT entry", "zh": "DNAT条目"},
    )
    assert nat_child_rules[("ALIYUN::VPC::CommonBandwidthPackageIp",)] == (
        ("BandwidthPackageId",),
        ("ALIYUN::VPC::CommonBandwidthPackage",),
        {"en": "Shared bandwidth IP", "zh": "共享带宽IP"},
    )

    attachment_edges = {tuple(edge.resource_types): edge for edge in rules.compact_attachment_edges}
    bandwidth_edge = attachment_edges[("ALIYUN::VPC::CommonBandwidthPackageIp",)]
    assert bandwidth_edge.resource_types == ("ALIYUN::VPC::CommonBandwidthPackageIp",)
    assert bandwidth_edge.source_properties == ("BandwidthPackageId",)
    assert bandwidth_edge.marker_properties == ("Eips",)
    assert bandwidth_edge.source_types == ("ALIYUN::VPC::CommonBandwidthPackage",)
    assert bandwidth_edge.marker_types == ("ALIYUN::VPC::EIP",)
    assert bandwidth_edge.edge_style == "dotted_open"
    assert bandwidth_edge.edge_label == {"en": "public bandwidth", "zh": "公网带宽"}
    assert ("ALIYUN::ECS::NetworkInterfaceAttachment",) not in attachment_edges
    assert sum("ALIYUN::VPC::SnatEntry" in rule.resource_types for rule in rules.compact_child_attachments) == 1
    assert sum("ALIYUN::ECS::SNatEntry" in rule.resource_types for rule in rules.compact_child_attachments) == 1
    assert sum("ALIYUN::VPC::ForwardEntry" in rule.resource_types for rule in rules.compact_child_attachments) == 1
    assert sum("ALIYUN::ECS::ForwardEntry" in rule.resource_types for rule in rules.compact_child_attachments) == 1


def test_loads_bridge_attachment_rules():
    rules = ArchitectureRules.load_default()

    bridge_rules = {
        tuple(rule.resource_types): (
            rule.source_properties,
            rule.via_resource_types,
            rule.via_source_properties,
            rule.via_target_properties,
            rule.target_types,
            rule.label,
        )
        for rule in rules.compact_bridge_attachments
    }
    assert bridge_rules[("ALIYUN::NAS::AccessGroup",)] == (
        (),
        ("ALIYUN::NAS::MountTarget",),
        ("AccessGroupName",),
        ("FileSystemId",),
        ("ALIYUN::NAS::FileSystem",),
        {"en": "NAS permission group", "zh": "NAS权限组"},
    )
    assert bridge_rules[("ALIYUN::NAS::AccessRule",)] == (
        ("AccessGroupName",),
        ("ALIYUN::NAS::MountTarget",),
        ("AccessGroupName",),
        ("FileSystemId",),
        ("ALIYUN::NAS::FileSystem",),
        {"en": "NAS permission rule", "zh": "NAS权限规则"},
    )
    assert bridge_rules[("ALIYUN::ECD::NetworkPackage",)] == (
        (),
        ("ALIYUN::ECD::NetworkPackageAssociation",),
        ("NetworkPackageId",),
        ("OfficeSiteId",),
        ("ALIYUN::ECD::SimpleOfficeSite",),
        {"en": "Bandwidth Plan", "zh": "带宽包"},
    )
    assert bridge_rules[("ALIYUN::SAG::SmartAccessGateway",)][0] == ()
    assert bridge_rules[("ALIYUN::VPC::DhcpOptionsSet",)][0] == ()
    assert bridge_rules[("ALIYUN::VPC::RouterInterface",)][0] == ()
    assert ("ALIYUN::VPC::HaVip",) not in bridge_rules
