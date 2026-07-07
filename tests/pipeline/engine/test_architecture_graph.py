from iac_code.pipeline.engine.architecture_graph import (
    render_ros_template_architecture,
    render_ros_template_architecture_views,
)
from iac_code.pipeline.engine.show_diagram_tool import ros_template_to_mermaid


def _set_test_language(monkeypatch, language: str) -> None:
    from iac_code.i18n import setup_i18n

    monkeypatch.setenv("LANGUAGE", language)
    monkeypatch.setenv("LC_ALL", "{}_CN.UTF-8".format(language) if language == "zh" else "en_US.UTF-8")
    monkeypatch.setenv("LANG", "{}_CN.UTF-8".format(language) if language == "zh" else "en_US.UTF-8")
    setup_i18n()


def _filler_resources(count: int = 24) -> str:
    return "".join(
        f"""\
  Bucket{index}:
    Type: ALIYUN::OSS::Bucket
"""
        for index in range(1, count + 1)
    )


def test_uses_meta_resource_name_for_storage_resource():
    template = """\
ROSTemplateFormatVersion: '2015-09-01'
Resources:
  Bucket:
    Type: ALIYUN::OSS::Bucket
    Properties:
      BucketName: demo-bucket
"""

    mermaid = ros_template_to_mermaid(template)

    assert 'Bucket["OSS Bucket"]' in mermaid


def test_architecture_context_includes_template_description(monkeypatch):
    _set_test_language(monkeypatch, "zh")
    try:
        template = """\
ROSTemplateFormatVersion: '2015-09-01'
Description:
  zh-cn: 在现有VPC下，创建Kafka集群，包含管理节点与弹性伸缩节点。
  en: Deploy a Kafka cluster in an existing VPC.
Resources:
  Master:
    Type: ALIYUN::ECS::InstanceGroup
  Workers:
    Type: ALIYUN::ESS::ScalingGroup
"""

        context = render_ros_template_architecture(template).architecture_context

        assert context["template_summary"]["description"] == "在现有VPC下，创建Kafka集群，包含管理节点与弹性伸缩节点。"
        assert context["template_summary"]["descriptions"]["en"] == "Deploy a Kafka cluster in an existing VPC."
    finally:
        _set_test_language(monkeypatch, "en")


def test_uses_current_language_for_resource_and_layer_labels(monkeypatch):
    _set_test_language(monkeypatch, "zh")
    try:
        template = """\
ROSTemplateFormatVersion: '2015-09-01'
Resources:
  VPC:
    Type: ALIYUN::ECS::VPC
  VSwitch:
    Type: ALIYUN::ECS::VSwitch
    Properties:
      VpcId:
        Ref: VPC
  ECS:
    Type: ALIYUN::ECS::Instance
    Properties:
      VSwitchId:
        Ref: VSwitch
      InstanceName: windows_vCenter
  Redis:
    Type: ALIYUN::REDIS::Instance
    Properties:
      VSwitchId:
        Ref: VSwitch
"""

        mermaid = ros_template_to_mermaid(template)

        assert "subgraph layer_VPC [专有网络 VPC]" in mermaid
        assert "subgraph layer_VSwitch [交换机]" in mermaid
        assert 'ECS["ECS 实例"]' in mermaid
        assert 'Redis["Redis实例"]' in mermaid
    finally:
        _set_test_language(monkeypatch, "en")


def test_uses_meta_relation_to_place_nas_inside_vswitch():
    template = """\
ROSTemplateFormatVersion: '2015-09-01'
Resources:
  VPC:
    Type: ALIYUN::ECS::VPC
  VSwitch:
    Type: ALIYUN::ECS::VSwitch
    Properties:
      VpcId:
        Ref: VPC
  NAS:
    Type: ALIYUN::NAS::FileSystem
    Properties:
      VSwitchId:
        Ref: VSwitch
"""

    mermaid = ros_template_to_mermaid(template)

    assert 'NAS["NAS File System"]' in mermaid
    assert mermaid.index("layer_VSwitch") < mermaid.index('NAS["NAS File System"]')


def test_declared_workspace_container_places_pai_resources_inside_workspace():
    template = """\
ROSTemplateFormatVersion: '2015-09-01'
Resources:
  Workspace:
    Type: ALIYUN::PAI::Workspace
  Dataset:
    Type: ALIYUN::PAI::Dataset
    Properties:
      WorkspaceId:
        Ref: Workspace
"""

    mermaid = ros_template_to_mermaid(template)

    assert "subgraph layer_Workspace [PAI Workspace]" in mermaid
    assert 'Dataset["PAI Dataset"]' in mermaid
    assert mermaid.index("layer_Workspace") < mermaid.index('Dataset["PAI Dataset"]')


def test_cen_instance_contains_transit_router_instead_of_drawing_long_dependency_edge():
    template = """\
ROSTemplateFormatVersion: '2015-09-01'
Resources:
  CEN:
    Type: ALIYUN::CEN::CenInstance
  TransitRouter:
    Type: ALIYUN::CEN::TransitRouter
    Properties:
      CenId:
        Ref: CEN
"""

    mermaid = ros_template_to_mermaid(template)

    assert "subgraph layer_CEN [CEN Instance]" in mermaid
    assert 'TransitRouter["CEN Transit Router"]' in mermaid
    assert mermaid.index("subgraph layer_CEN") < mermaid.index('TransitRouter["CEN Transit Router"]')
    assert "CEN -.- TransitRouter" not in mermaid
    assert "TransitRouter -.- CEN" not in mermaid


def test_subgraph_title_uses_termaid_compatible_label_syntax():
    template = """\
ROSTemplateFormatVersion: '2015-09-01'
Resources:
  VPC:
    Type: ALIYUN::ECS::VPC
    Properties:
      CidrBlock: 172.16.0.0/12
  VSwitch:
    Type: ALIYUN::ECS::VSwitch
    Properties:
      VpcId:
        Ref: VPC
      CidrBlock: 172.16.0.0/24
"""

    mermaid = ros_template_to_mermaid(template)

    assert "subgraph layer_VSwitch [VSwitch (172.16.0.0/24)]" in mermaid
    assert 'subgraph layer_VSwitch["VSwitch (172.16.0.0/24)"]' not in mermaid


def test_folds_meta_main_resource_attachment_into_edge():
    template = """\
ROSTemplateFormatVersion: '2015-09-01'
Resources:
  ECS:
    Type: ALIYUN::ECS::Instance
  DataDisk:
    Type: ALIYUN::ECS::Disk
  DiskAttach:
    Type: ALIYUN::ECS::DiskAttachment
    Properties:
      DiskId:
        Ref: DataDisk
      InstanceId:
        Ref: ECS
"""

    mermaid = ros_template_to_mermaid(template)

    assert "DiskAttach" not in mermaid
    assert "DataDisk --> ECS" in mermaid


def test_direct_meta_relations_use_dotted_open_edge():
    template = """\
ROSTemplateFormatVersion: '2015-09-01'
Resources:
  ECS:
    Type: ALIYUN::ECS::Instance
  ScalingGroup:
    Type: ALIYUN::ESS::ScalingGroup
  ScalingConfig:
    Type: ALIYUN::ESS::ScalingConfiguration
    Properties:
      InstanceId:
        Ref: ECS
      ScalingGroupId:
        Ref: ScalingGroup
"""

    mermaid = ros_template_to_mermaid(template)

    assert "ECS -.- ScalingConfig" in mermaid
    assert "ScalingGroup -.- ScalingConfig" in mermaid
    assert "ECS --> ScalingConfig" not in mermaid
    assert "ScalingGroup --> ScalingConfig" not in mermaid


def test_semantic_plan_adds_valid_llm_inferred_edges_only():
    template = """\
ROSTemplateFormatVersion: '2015-09-01'
Resources:
  VSwitch:
    Type: ALIYUN::ECS::VSwitch
  SLB:
    Type: ALIYUN::SLB::LoadBalancer
    Properties:
      VSwitchId:
        Ref: VSwitch
  ECS:
    Type: ALIYUN::ECS::Instance
    Properties:
      VSwitchId:
        Ref: VSwitch
"""

    result = render_ros_template_architecture(
        template,
        semantic_plan={
            "edges": [
                {
                    "from": "SLB",
                    "to": "ECS",
                    "kind": "traffic",
                    "label": "forwards traffic",
                    "confidence": "medium",
                },
                {
                    "from": "SLB",
                    "to": "MissingNode",
                    "kind": "traffic",
                    "label": "invalid",
                    "confidence": "high",
                },
            ]
        },
    )

    assert "SLB -->|forwards traffic| ECS" in result.mermaid_source
    assert "MissingNode" not in result.mermaid_source
    assert result.architecture_context["semantic_plan"]["accepted_edges"] == [
        {
            "from": "SLB",
            "to": "ECS",
            "kind": "traffic",
            "label": "forwards traffic",
            "confidence": "medium",
        }
    ]
    assert result.architecture_context["semantic_plan"]["rejected_edges"] == [
        {
            "from": "SLB",
            "to": "MissingNode",
            "reason": "unknown node",
        }
    ]


def test_polardb_migration_from_rds_uses_source_to_target_direction(monkeypatch):
    _set_test_language(monkeypatch, "zh")
    try:
        template = """\
ROSTemplateFormatVersion: '2015-09-01'
Resources:
  Database:
    Type: ALIYUN::RDS::DBInstance
  DBCluster:
    Type: ALIYUN::POLARDB::DBCluster
    Properties:
      CreationOption: MigrationFromRDS
      SourceResourceId:
        Ref: Database
"""

        result = render_ros_template_architecture(
            template,
            semantic_plan={
                "edges": [
                    {
                        "from": "DBCluster",
                        "to": "Database",
                        "kind": "dependency",
                        "label": "数据迁移源",
                        "confidence": "high",
                    }
                ]
            },
        )

        assert "Database -->|迁移到| DBCluster" in result.mermaid_source
        assert "DBCluster -->|数据迁移源| Database" not in result.mermaid_source
        assert {
            "from": "Database",
            "to": "DBCluster",
            "style": "solid_arrow",
            "label": "迁移到",
        } in result.architecture_context["visible_edges"]
        assert result.architecture_context["semantic_plan"]["rejected_edges"] == [
            {
                "from": "DBCluster",
                "to": "Database",
                "reason": "covered by deterministic edge",
            }
        ]
    finally:
        _set_test_language(monkeypatch, "en")


def test_architecture_view_keeps_deterministic_polardb_migration_edge(monkeypatch):
    _set_test_language(monkeypatch, "zh")
    try:
        template = """\
ROSTemplateFormatVersion: '2015-09-01'
Resources:
  Database:
    Type: ALIYUN::RDS::DBInstance
  DBCluster:
    Type: ALIYUN::POLARDB::DBCluster
    Properties:
      CreationOption: MigrationFromRDS
      SourceResourceId:
        Ref: Database
  EcsInstance:
    Type: ALIYUN::ECS::Instance
"""

        result = render_ros_template_architecture_views(
            template,
            semantic_plan={
                "edges": [
                    {
                        "from": "EcsInstance",
                        "to": "DBCluster",
                        "kind": "traffic",
                        "label": "数据库访问",
                        "confidence": "medium",
                    }
                ],
                "views": [
                    {
                        "id": "overview",
                        "title": "RDS 至 PolarDB 迁移架构",
                        "purpose": "展示从 RDS 迁移到 PolarDB 的核心组件及业务访问关系",
                        "layout": "flat",
                        "nodes": ["Database", "DBCluster", "EcsInstance"],
                        "edges": [
                            {
                                "from": "EcsInstance",
                                "to": "DBCluster",
                                "kind": "traffic",
                                "label": "数据库访问",
                            }
                        ],
                    }
                ],
            },
        )

        assert "Database -->|迁移到| DBCluster" in result.views[0].mermaid_source
        assert {
            "from": "Database",
            "to": "DBCluster",
            "style": "solid_arrow",
            "label": "迁移到",
        } in result.views[0].architecture_context["edges"]
    finally:
        _set_test_language(monkeypatch, "en")


def test_architecture_view_keeps_view_specific_semantic_edge_label():
    template = """\
ROSTemplateFormatVersion: '2015-09-01'
Resources:
  SLB:
    Type: ALIYUN::SLB::LoadBalancer
  ECS:
    Type: ALIYUN::ECS::Instance
"""

    result = render_ros_template_architecture_views(
        template,
        semantic_plan={
            "edges": [
                {
                    "from": "SLB",
                    "to": "ECS",
                    "kind": "traffic",
                    "label": "business traffic",
                    "confidence": "high",
                }
            ],
            "views": [
                {
                    "id": "detail_app",
                    "title": "Application detail",
                    "purpose": "Show request forwarding.",
                    "layout": "flat",
                    "nodes": ["SLB", "ECS"],
                    "edges": [
                        {
                            "from": "SLB",
                            "to": "ECS",
                            "kind": "traffic",
                            "label": "HTTP forwarding",
                        }
                    ],
                }
            ],
        },
    )

    assert "SLB -->|HTTP forwarding| ECS" in result.views[0].mermaid_source
    assert "SLB -->|business traffic| ECS" not in result.views[0].mermaid_source


def test_render_architecture_views_filters_each_mermaid_view():
    template = """\
ROSTemplateFormatVersion: '2015-09-01'
Resources:
  VPC:
    Type: ALIYUN::ECS::VPC
  VSwitch:
    Type: ALIYUN::ECS::VSwitch
    Properties:
      VpcId:
        Ref: VPC
  ALB:
    Type: ALIYUN::ALB::LoadBalancer
    Properties:
      VpcId:
        Ref: VPC
  ECS:
    Type: ALIYUN::ECS::Instance
    Properties:
      VSwitchId:
        Ref: VSwitch
  RDS:
    Type: ALIYUN::RDS::DBInstance
    Properties:
      VSwitchId:
        Ref: VSwitch
"""

    result = render_ros_template_architecture_views(
        template,
        semantic_plan={
            "edges": [
                {"from": "ALB", "to": "ECS", "kind": "traffic", "label": "流量分发"},
                {"from": "ECS", "to": "RDS", "kind": "traffic", "label": "数据库访问"},
            ],
            "views": [
                {
                    "id": "traffic",
                    "title": "业务流量",
                    "purpose": "入口到应用",
                    "nodes": ["ALB", "ECS"],
                    "edges": [{"from": "ALB", "to": "ECS", "kind": "traffic", "label": "流量分发"}],
                },
                {
                    "id": "data",
                    "title": "数据访问",
                    "purpose": "应用访问数据库",
                    "nodes": ["ECS", "RDS"],
                    "edges": [{"from": "ECS", "to": "RDS", "kind": "traffic", "label": "数据库访问"}],
                },
            ],
        },
    )

    assert [view.id for view in result.views] == ["traffic", "data"]
    traffic = result.views[0].mermaid_source
    data = result.views[1].mermaid_source
    assert 'ALB["ALB Instance"]' in traffic
    assert 'ECS["ECS instance"]' in traffic
    assert "ALB -->|流量分发| ECS" in traffic
    assert "subgraph layer_VPC" not in traffic
    assert "subgraph layer_VSwitch" not in traffic
    assert "RDS" not in traffic
    assert 'RDS["ApsaraDB RDS Instance"]' in data
    assert "ECS -->|数据库访问| RDS" in data
    assert "ALB" not in data
    assert result.views[0].architecture_context["layout"] == "flat"


def test_render_architecture_overview_marks_detail_view_anchors():
    template = """\
ROSTemplateFormatVersion: '2015-09-01'
Resources:
  ALB:
    Type: ALIYUN::ALB::LoadBalancer
  ECS:
    Type: ALIYUN::ECS::Instance
  RDS:
    Type: ALIYUN::RDS::DBInstance
"""

    result = render_ros_template_architecture_views(
        template,
        semantic_plan={
            "edges": [
                {"from": "ALB", "to": "ECS", "kind": "traffic", "label": "流量分发"},
                {"from": "ECS", "to": "RDS", "kind": "traffic", "label": "数据库访问"},
            ],
            "views": [
                {
                    "id": "overview",
                    "title": "架构总览",
                    "purpose": "整体链路",
                    "nodes": ["ALB", "ECS", "RDS"],
                    "edges": [
                        {"from": "ALB", "to": "ECS", "kind": "traffic", "label": "流量分发"},
                        {"from": "ECS", "to": "RDS", "kind": "traffic", "label": "数据库访问"},
                    ],
                },
                {
                    "id": "detail_app",
                    "title": "应用层展开",
                    "purpose": "展开应用层",
                    "anchors": ["ECS"],
                    "nodes": ["ALB", "ECS"],
                    "edges": [{"from": "ALB", "to": "ECS", "kind": "traffic", "label": "流量分发"}],
                },
            ],
        },
    )

    overview = result.views[0].mermaid_source
    detail = result.views[1]
    assert 'ECS["ECS instance' in overview
    assert "展开: 应用层展开" in overview
    assert "\\n展开: 应用层展开" in overview
    assert 'ALB["ALB Instance' in overview
    assert 'ALB["ALB Instance\n展开: 应用层展开"]' not in overview
    assert detail.architecture_context["anchors"] == ["ECS"]


def test_render_architecture_overview_supports_semantic_summary_groups():
    template = """\
ROSTemplateFormatVersion: '2015-09-01'
Resources:
  VPC:
    Type: ALIYUN::ECS::VPC
  VSwitch:
    Type: ALIYUN::ECS::VSwitch
    Properties:
      VpcId:
        Ref: VPC
  ECS1:
    Type: ALIYUN::ECS::Instance
    Properties:
      VSwitchId:
        Ref: VSwitch
  ECS2:
    Type: ALIYUN::ECS::Instance
    Properties:
      VSwitchId:
        Ref: VSwitch
  Redis:
    Type: ALIYUN::REDIS::Instance
    Properties:
      VSwitchId:
        Ref: VSwitch
"""

    result = render_ros_template_architecture_views(
        template,
        semantic_plan={
            "node_labels": [
                {"id": "ECS1", "label": "应用服务器 1", "confidence": "high", "reason": "test"},
                {"id": "ECS2", "label": "应用服务器 2", "confidence": "high", "reason": "test"},
                {"id": "Redis", "label": "Redis 缓存", "confidence": "high", "reason": "test"},
            ],
            "views": [
                {
                    "id": "overview",
                    "title": "总览",
                    "purpose": "概览",
                    "layout": "contained",
                    "groups": [{"id": "AppGroup", "label": "应用服务器组", "members": ["ECS1", "ECS2"]}],
                    "nodes": ["AppGroup", "Redis"],
                    "edges": [{"from": "AppGroup", "to": "Redis", "kind": "traffic", "label": "缓存访问"}],
                },
                {
                    "id": "detail_app",
                    "title": "应用层详情",
                    "purpose": "展开应用层",
                    "anchors": ["AppGroup"],
                    "nodes": ["ECS1", "ECS2"],
                    "edges": [{"from": "ECS1", "to": "ECS2", "kind": "management", "label": "同组部署"}],
                },
            ],
        },
    )

    overview = result.views[0].mermaid_source
    assert "subgraph layer_VPC" in overview
    assert "subgraph layer_VSwitch" in overview
    assert 'AppGroup["应用服务器组\\n+ 应用服务器 1\\n+ 应用服务器 2\\n展开: 应用层详情"]' in overview
    assert 'Redis["Redis 缓存"]' in overview
    assert "AppGroup -->|缓存访问| Redis" in overview
    assert 'ECS1["应用服务器 1"]' not in overview
    assert 'ECS2["应用服务器 2"]' not in overview
    assert result.views[1].architecture_context["anchors"] == ["AppGroup"]


def test_render_architecture_overview_collapses_many_vswitch_boundaries(monkeypatch):
    _set_test_language(monkeypatch, "zh")
    try:
        template = """\
ROSTemplateFormatVersion: '2015-09-01'
Resources:
  Vpc:
    Type: ALIYUN::ECS::VPC
  VSwitch1:
    Type: ALIYUN::ECS::VSwitch
    Properties:
      VpcId:
        Ref: Vpc
  VSwitch2:
    Type: ALIYUN::ECS::VSwitch
    Properties:
      VpcId:
        Ref: Vpc
  VSwitch3:
    Type: ALIYUN::ECS::VSwitch
    Properties:
      VpcId:
        Ref: Vpc
  Alb:
    Type: ALIYUN::ALB::LoadBalancer
    Properties:
      VpcId:
        Ref: Vpc
      ZoneMappings:
        - VSwitchId:
            Ref: VSwitch1
  App1:
    Type: ALIYUN::ECS::InstanceGroup
    Properties:
      VSwitchId:
        Ref: VSwitch2
  App2:
    Type: ALIYUN::ECS::InstanceGroup
    Properties:
      VSwitchId:
        Ref: VSwitch3
  Rds:
    Type: ALIYUN::RDS::DBInstance
    Properties:
      VSwitchId:
        Ref: VSwitch3
  Nat:
    Type: ALIYUN::VPC::NatGateway
    Properties:
      VpcId:
        Ref: Vpc
      VSwitchId:
        Ref: VSwitch3
"""

        result = render_ros_template_architecture_views(
            template,
            semantic_plan={
                "node_labels": [
                    {"id": "Alb", "label": "应用入口 ALB", "confidence": "high"},
                    {"id": "App1", "label": "应用服务器组 1", "confidence": "high"},
                    {"id": "App2", "label": "应用服务器组 2", "confidence": "high"},
                    {"id": "Rds", "label": "RDS 数据库", "confidence": "high"},
                    {"id": "Nat", "label": "NAT 网关", "confidence": "high"},
                ],
                "views": [
                    {
                        "id": "overview",
                        "title": "总览",
                        "layout": "contained",
                        "nodes": ["Alb", "App1", "App2", "Rds", "Nat"],
                        "edges": [
                            {"from": "Alb", "to": "App1", "kind": "traffic", "label": "后端转发"},
                            {"from": "Alb", "to": "App2", "kind": "traffic", "label": "后端转发"},
                            {"from": "App1", "to": "Rds", "kind": "dependency", "label": "数据库访问"},
                            {"from": "App2", "to": "Rds", "kind": "dependency", "label": "数据库访问"},
                            {"from": "App1", "to": "Nat", "kind": "traffic", "label": "SNAT 出网"},
                            {"from": "App2", "to": "Nat", "kind": "traffic", "label": "SNAT 出网"},
                        ],
                    }
                ],
            },
        )

        overview = result.views[0].mermaid_source
        assert "subgraph layer_Vpc" in overview
        assert "subgraph layer_VSwitch" not in overview
        assert 'Alb["应用入口 ALB' in overview
        assert 'App1["应用服务器组 1' in overview
        assert 'Rds["RDS 数据库' in overview
        assert "Alb -->|后端转发| App1" in overview
    finally:
        _set_test_language(monkeypatch, "en")


def test_render_architecture_summary_group_marks_cross_container_members(monkeypatch):
    _set_test_language(monkeypatch, "zh")
    try:
        template = """\
ROSTemplateFormatVersion: '2015-09-01'
Resources:
  VPC1:
    Type: ALIYUN::ECS::VPC
  VPC2:
    Type: ALIYUN::ECS::VPC
  VSwitch1:
    Type: ALIYUN::ECS::VSwitch
    Properties:
      VpcId:
        Ref: VPC1
  VSwitch2:
    Type: ALIYUN::ECS::VSwitch
    Properties:
      VpcId:
        Ref: VPC2
  ALB1:
    Type: ALIYUN::ALB::LoadBalancer
    Properties:
      VpcId:
        Ref: VPC1
      AddressType: Internet
  ALB2:
    Type: ALIYUN::ALB::LoadBalancer
    Properties:
      VpcId:
        Ref: VPC2
      AddressType: Internet
"""

        result = render_ros_template_architecture_views(
            template,
            semantic_plan={
                "node_labels": [
                    {"id": "ALB1", "label": "生产 ALB 1", "confidence": "high", "reason": "test"},
                    {"id": "ALB2", "label": "生产 ALB 2", "confidence": "high", "reason": "test"},
                ],
                "views": [
                    {
                        "id": "overview",
                        "title": "总览",
                        "purpose": "概览",
                        "groups": [{"id": "AlbGroup", "label": "生产 ALB", "members": ["ALB1", "ALB2"]}],
                        "nodes": ["AlbGroup"],
                        "edges": [],
                    }
                ],
            },
        )

        overview = result.views[0].mermaid_source
        assert 'AlbGroup["生产 ALB\\n+ 生产 ALB 1\\n+ 生产 ALB 2\\n+ 跨 VPC x2"]' in overview
    finally:
        _set_test_language(monkeypatch, "en")


def test_render_architecture_network_view_is_flat_by_default():
    template = """\
ROSTemplateFormatVersion: '2015-09-01'
Resources:
  VPC:
    Type: ALIYUN::ECS::VPC
  VSwitch:
    Type: ALIYUN::ECS::VSwitch
    Properties:
      VpcId:
        Ref: VPC
  ECS:
    Type: ALIYUN::ECS::Instance
    Properties:
      VSwitchId:
        Ref: VSwitch
"""

    result = render_ros_template_architecture_views(
        template,
        semantic_plan={
            "views": [
                {
                    "id": "network",
                    "title": "网络位置",
                    "purpose": "资源所在网络",
                    "nodes": ["ECS"],
                    "edges": [],
                }
            ]
        },
    )

    network = result.views[0].mermaid_source
    assert "subgraph layer_VPC" not in network
    assert "subgraph layer_VSwitch" not in network
    assert 'ECS["ECS instance"]' in network
    assert result.views[0].architecture_context["layout"] == "flat"


def test_render_architecture_view_can_explicitly_keep_containment():
    template = """\
ROSTemplateFormatVersion: '2015-09-01'
Resources:
  VPC:
    Type: ALIYUN::ECS::VPC
  VSwitch:
    Type: ALIYUN::ECS::VSwitch
    Properties:
      VpcId:
        Ref: VPC
  ECS:
    Type: ALIYUN::ECS::Instance
    Properties:
      VSwitchId:
        Ref: VSwitch
"""

    result = render_ros_template_architecture_views(
        template,
        semantic_plan={
            "views": [
                {
                    "id": "placement",
                    "title": "部署位置",
                    "purpose": "资源所在网络",
                    "layout": "contained",
                    "nodes": ["ECS"],
                    "edges": [],
                }
            ]
        },
    )

    placement = result.views[0].mermaid_source
    assert "subgraph layer_VPC" in placement
    assert "subgraph layer_VSwitch" in placement
    assert 'ECS["ECS instance"]' in placement
    assert result.views[0].architecture_context["layout"] == "contained"


def test_render_architecture_views_falls_back_to_overview_without_view_plan():
    template = """\
ROSTemplateFormatVersion: '2015-09-01'
Resources:
  ECS:
    Type: ALIYUN::ECS::Instance
"""

    single = render_ros_template_architecture(template)
    views = render_ros_template_architecture_views(template)

    assert len(views.views) == 1
    assert views.views[0].id == "overview"
    assert views.views[0].mermaid_source == single.mermaid_source


def test_render_architecture_view_uses_edge_endpoints_when_nodes_are_omitted():
    template = """\
ROSTemplateFormatVersion: '2015-09-01'
Resources:
  ALB:
    Type: ALIYUN::ALB::LoadBalancer
  ECS:
    Type: ALIYUN::ECS::Instance
"""

    result = render_ros_template_architecture_views(
        template,
        semantic_plan={
            "views": [
                {
                    "id": "traffic",
                    "title": "业务流量",
                    "purpose": "入口到应用",
                    "nodes": [],
                    "edges": [{"from": "ALB", "to": "ECS", "kind": "traffic", "label": "流量分发"}],
                }
            ]
        },
    )

    traffic = result.views[0].mermaid_source
    assert 'ALB["ALB Instance"]' in traffic
    assert 'ECS["ECS instance"]' in traffic
    assert "ALB -->|流量分发| ECS" in traffic


def test_semantic_fanout_edges_are_preserved_as_relationships():
    template = """\
ROSTemplateFormatVersion: '2015-09-01'
Resources:
  NLB:
    Type: ALIYUN::NLB::LoadBalancer
  ALB1:
    Type: ALIYUN::ALB::LoadBalancer
  ALB2:
    Type: ALIYUN::ALB::LoadBalancer
"""

    result = render_ros_template_architecture(
        template,
        semantic_plan={
            "edges": [
                {
                    "from": "NLB",
                    "to": "ALB1",
                    "kind": "traffic",
                    "label": "distribute traffic",
                    "confidence": "high",
                },
                {
                    "from": "NLB",
                    "to": "ALB2",
                    "kind": "traffic",
                    "label": "distribute traffic",
                    "confidence": "high",
                },
            ]
        },
    )

    assert 'NLB["NLB Instance"]' in result.mermaid_source
    assert "NLB -->|distribute traffic| ALB1" in result.mermaid_source
    assert "NLB -->|distribute traffic| ALB2" in result.mermaid_source
    assert "compacted_edges" not in result.architecture_context["semantic_plan"]


def test_semantic_edge_label_collapses_multiline_source_marker_for_terminal_rendering():
    template = """\
ROSTemplateFormatVersion: '2015-09-01'
Resources:
  ECS:
    Type: ALIYUN::ECS::Instance
  NAS:
    Type: ALIYUN::NAS::FileSystem
"""

    result = render_ros_template_architecture(
        template,
        semantic_plan={
            "edges": [
                {
                    "from": "ECS",
                    "to": "NAS",
                    "kind": "management",
                    "label": "挂载 NAS\n云助手",
                    "confidence": "high",
                },
                {
                    "from": "NAS",
                    "to": "ECS",
                    "kind": "management",
                    "label": "同步数据<br/>云助手",
                    "confidence": "high",
                },
            ]
        },
    )

    assert "ECS -.->|挂载 NAS（云助手）| NAS" in result.mermaid_source
    assert "NAS -.->|同步数据（云助手）| ECS" in result.mermaid_source


def test_semantic_plan_renames_visible_node_base_label_only():
    fillers = []
    for index in range(1, 24):
        fillers.append(
            f"""\
  Bucket{index}:
    Type: ALIYUN::OSS::Bucket
"""
        )
    template = """\
ROSTemplateFormatVersion: '2015-09-01'
Resources:
  ECS:
    Type: ALIYUN::ECS::Instance
  EIP:
    Type: ALIYUN::VPC::EIP
  EIPAssoc:
    Type: ALIYUN::VPC::EIPAssociation
    Properties:
      AllocationId:
        Ref: EIP
      InstanceId:
        Ref: ECS
""" + "".join(fillers)

    result = render_ros_template_architecture(
        template,
        semantic_plan={
            "node_labels": [
                {
                    "id": "ECS",
                    "label": "vCenter manager",
                    "confidence": "high",
                    "reason": "InstanceName=windows_vCenter",
                },
                {
                    "id": "MissingNode",
                    "label": "ghost",
                    "confidence": "high",
                },
                {
                    "id": "EIP",
                    "label": "should not apply",
                    "confidence": "high",
                },
            ]
        },
    )

    assert 'ECS["vCenter manager\\n+ EIP"]' in result.mermaid_source
    assert 'ECS["ECS instance\\n+ EIP"]' not in result.mermaid_source
    assert {
        "id": "ECS",
        "label": "vCenter manager\\n+ EIP",
        "type": "ALIYUN::ECS::Instance",
    } in result.architecture_context["visible_nodes"]
    assert result.architecture_context["semantic_plan"]["accepted_node_labels"] == [
        {
            "id": "ECS",
            "label": "vCenter manager",
            "confidence": "high",
            "reason": "InstanceName=windows_vCenter",
        }
    ]
    assert result.architecture_context["semantic_plan"]["rejected_node_labels"] == [
        {"id": "MissingNode", "reason": "unknown node"},
        {"id": "EIP", "reason": "unknown node"},
    ]


def test_architecture_context_exposes_fact_bundle_for_llm():
    template = """\
ROSTemplateFormatVersion: '2015-09-01'
Resources:
  VPC:
    Type: ALIYUN::ECS::VPC
  VSwitch:
    Type: ALIYUN::ECS::VSwitch
    Properties:
      VpcId:
        Ref: VPC
  ECS:
    Type: ALIYUN::ECS::Instance
    Properties:
      VSwitchId:
        Ref: VSwitch
      InstanceName: windows_vCenter
  Redis:
    Type: ALIYUN::REDIS::Instance
    Properties:
      VSwitchId:
        Ref: VSwitch
"""

    result = render_ros_template_architecture(template)
    context = result.architecture_context

    assert context["version"] == "1.0"
    assert {"id": "ECS", "label": "ECS instance", "type": "ALIYUN::ECS::Instance"} in context["visible_nodes"]
    assert any(node["id"] == "Redis" and node["type"] == "ALIYUN::REDIS::Instance" for node in context["visible_nodes"])
    assert {"resource": "ECS", "container": "VSwitch"} in context["containment"]
    assert context["llm_semantic_plan_schema"]["edges"]["allowed_kinds"] == [
        "traffic",
        "dependency",
        "management",
        "inferred",
    ]
    assert context["llm_semantic_plan_schema"]["node_labels"]["required_fields"] == [
        "id",
        "label",
        "confidence",
    ]
    assert {
        "id": "ECS",
        "label": "ECS instance",
        "type": "ALIYUN::ECS::Instance",
        "hints": {"InstanceName": "windows_vCenter"},
    } in context["node_label_hints"]


def test_architecture_context_exposes_cen_instance_attachment_facts():
    template = """\
ROSTemplateFormatVersion: '2015-09-01'
Parameters:
  OtherVpcId:
    Type: String
  OtherVbrId:
    Type: String
  OtherRegion:
    Type: String
Resources:
  Vpc:
    Type: ALIYUN::ECS::VPC
  CenInstance:
    Type: ALIYUN::CEN::CenInstance
  CurrentVpcAttachment:
    Type: ALIYUN::CEN::CenInstanceAttachment
    Properties:
      CenId:
        Ref: CenInstance
      ChildInstanceType: VPC
      ChildInstanceId:
        Ref: Vpc
      ChildInstanceRegionId:
        Ref: ALIYUN::Region
  ExternalVpcAttachment:
    Type: ALIYUN::CEN::CenInstanceAttachment
    Properties:
      CenId:
        Ref: CenInstance
      ChildInstanceType: VPC
      ChildInstanceId:
        Ref: OtherVpcId
      ChildInstanceRegionId:
        Ref: OtherRegion
  ExternalVbrAttachment:
    Type: ALIYUN::CEN::CenInstanceAttachment
    Properties:
      CenId:
        Ref: CenInstance
      ChildInstanceType: VBR
      ChildInstanceId:
        Ref: OtherVbrId
      ChildInstanceRegionId:
        Ref: OtherRegion
"""

    result = render_ros_template_architecture(template)

    assert {
        "id": "CurrentVpcAttachment",
        "type": "ALIYUN::CEN::CenInstanceAttachment",
        "network": "CEN",
        "cen": "CenInstance",
        "child_instance_type": "VPC",
        "child_instance_id": "Vpc",
        "child_instance_region": "Ref:ALIYUN::Region",
        "child_resource": "Vpc",
        "child_resource_type": "ALIYUN::ECS::VPC",
    } in result.architecture_context["network_attachments"]
    assert any(
        item["id"] == "ExternalVpcAttachment"
        and item["child_instance_type"] == "VPC"
        and item["child_instance_id"] == "Ref:OtherVpcId"
        and item["child_instance_region"] == "Ref:OtherRegion"
        for item in result.architecture_context["network_attachments"]
    )
    assert any(
        item["id"] == "ExternalVbrAttachment"
        and item["child_instance_type"] == "VBR"
        and item["child_instance_id"] == "Ref:OtherVbrId"
        for item in result.architecture_context["network_attachments"]
    )


def test_flat_view_renders_container_as_summary_node_without_expanding_children():
    template = """\
ROSTemplateFormatVersion: '2015-09-01'
Resources:
  Vpc:
    Type: ALIYUN::ECS::VPC
    Properties:
      CidrBlock: 172.16.0.0/12
  VSwitch:
    Type: ALIYUN::ECS::VSwitch
    Properties:
      VpcId:
        Ref: Vpc
      CidrBlock: 172.16.100.0/25
  BackendServer:
    Type: ALIYUN::ECS::Instance
    Properties:
      VpcId:
        Ref: Vpc
      VSwitchId:
        Ref: VSwitch
  CenInstance:
    Type: ALIYUN::CEN::CenInstance
  CenConfig:
    Type: ALIYUN::CEN::CenInstanceAttachment
    Properties:
      CenId:
        Ref: CenInstance
      ChildInstanceType: VPC
      ChildInstanceId:
        Ref: Vpc
"""
    semantic_plan = {
        "views": [
            {
                "id": "overview",
                "layout": "flat",
                "nodes": ["BackendServer"],
                "edges": [],
            },
            {
                "id": "detail_network",
                "layout": "flat",
                "anchors": ["BackendServer"],
                "nodes": ["Vpc", "CenInstance"],
                "edges": [{"from": "Vpc", "to": "CenInstance", "kind": "management", "label": "CEN 接入"}],
            },
        ]
    }

    result = render_ros_template_architecture_views(template, semantic_plan=semantic_plan)
    detail = result.views[1].mermaid_source

    assert 'Vpc["VPC (172.16.0.0/12)"]' in detail
    assert "subgraph layer_Vpc" not in detail
    assert 'BackendServer["ECS instance"]' not in detail
    assert "Vpc -.->|CEN 接入| CenInstance" in detail


def test_contained_view_summary_group_hides_its_member_nodes():
    template = """\
ROSTemplateFormatVersion: '2015-09-01'
Resources:
  Vpc:
    Type: ALIYUN::ECS::VPC
  VSwitch1:
    Type: ALIYUN::ECS::VSwitch
    Properties:
      VpcId:
        Ref: Vpc
  VSwitch2:
    Type: ALIYUN::ECS::VSwitch
    Properties:
      VpcId:
        Ref: Vpc
  BackendServer1:
    Type: ALIYUN::ECS::Instance
    Properties:
      VSwitchId:
        Ref: VSwitch1
  BackendServer2:
    Type: ALIYUN::ECS::Instance
    Properties:
      VSwitchId:
        Ref: VSwitch2
  LoadBalancer:
    Type: ALIYUN::SLB::LoadBalancer
"""
    result = render_ros_template_architecture_views(
        template,
        semantic_plan={
            "views": [
                {
                    "id": "overview",
                    "layout": "contained",
                    "groups": [
                        {
                            "id": "BackendGroup",
                            "label": "后端服务器组",
                            "members": ["BackendServer1", "BackendServer2"],
                            "parent": "Vpc",
                        }
                    ],
                    "nodes": ["Vpc", "LoadBalancer", "BackendGroup"],
                    "edges": [
                        {
                            "from": "LoadBalancer",
                            "to": "BackendGroup",
                            "kind": "traffic",
                            "label": "流量分发",
                        }
                    ],
                },
                {
                    "id": "detail_network",
                    "title": "网络详情",
                    "layout": "flat",
                    "anchors": ["Vpc"],
                    "nodes": ["Vpc"],
                    "edges": [],
                },
            ],
        },
    )

    overview = result.views[0].mermaid_source

    assert 'BackendGroup["后端服务器组\\n+ ECS instance 1\\n+ ECS instance 2"]' in overview
    assert 'BackendServer1["ECS instance"]' not in overview
    assert 'BackendServer2["ECS instance"]' not in overview
    assert "subgraph layer_VSwitch1" not in overview
    assert "subgraph layer_VSwitch2" not in overview
    assert "LoadBalancer -->|流量分发| BackendGroup" in overview
    assert "\\n展开: 网络详情" not in overview
    assert "展开: 网络详情" in overview


def test_large_graph_folds_repeated_attachment_markers_into_owner_resource():
    repeated_resources = []
    for index in range(1, 14):
        repeated_resources.append(
            f"""\
  ENI{index}:
    Type: ALIYUN::ECS::NetworkInterface
    Properties:
      VSwitchId:
        Ref: VSwitch
      SecurityGroupId:
        Ref: SG
  AssignPrivateIp{index}:
    Type: ALIYUN::ECS::AssignPrivateIpAddresses
    Properties:
      NetworkInterfaceId:
        Ref: ENI{index}
  ENIAttachment{index}:
    Type: ALIYUN::ECS::NetworkInterfaceAttachment
    Properties:
      NetworkInterfaceId:
        Ref: ENI{index}
      InstanceId:
        Ref: ECS
"""
        )
    template = """\
ROSTemplateFormatVersion: '2015-09-01'
Resources:
  VPC:
    Type: ALIYUN::ECS::VPC
  VSwitch:
    Type: ALIYUN::ECS::VSwitch
    Properties:
      VpcId:
        Ref: VPC
  SG:
    Type: ALIYUN::ECS::SecurityGroup
  ECS:
    Type: ALIYUN::ECS::Instance
    Properties:
      VSwitchId:
        Ref: VSwitch
      SecurityGroupId:
        Ref: SG
  EIP1:
    Type: ALIYUN::VPC::EIP
  EIP2:
    Type: ALIYUN::VPC::EIP
  EIP3:
    Type: ALIYUN::VPC::EIP
""" + "".join(repeated_resources)

    mermaid = ros_template_to_mermaid(template)

    assert 'ECS["ECS instance\\n+ ENI x13\\n+ Security group"]' in mermaid
    assert "agg_ECS_NetworkInterface_layer_SG" not in mermaid
    assert "agg_VPC_EIP_root" not in mermaid
    assert " --> ECS" not in mermaid
    assert "Private IP Addresses" not in mermaid
    assert "AssignPrivateIp1" not in mermaid


def test_small_graph_keeps_individual_repeated_resources():
    template = """\
ROSTemplateFormatVersion: '2015-09-01'
Resources:
  VPC:
    Type: ALIYUN::ECS::VPC
  VSwitch:
    Type: ALIYUN::ECS::VSwitch
    Properties:
      VpcId:
        Ref: VPC
  SG:
    Type: ALIYUN::ECS::SecurityGroup
  ECS:
    Type: ALIYUN::ECS::Instance
    Properties:
      VSwitchId:
        Ref: VSwitch
      SecurityGroupId:
        Ref: SG
  ENI1:
    Type: ALIYUN::ECS::NetworkInterface
    Properties:
      VSwitchId:
        Ref: VSwitch
      SecurityGroupId:
        Ref: SG
  ENI2:
    Type: ALIYUN::ECS::NetworkInterface
    Properties:
      VSwitchId:
        Ref: VSwitch
      SecurityGroupId:
        Ref: SG
"""

    mermaid = ros_template_to_mermaid(template)

    assert 'ENI1["ENI 1"]' in mermaid
    assert 'ENI2["ENI 2"]' in mermaid
    assert "ENI x2" not in mermaid


def test_large_apsara_template_is_not_compacted():
    resources = []
    for index in range(1, 31):
        resources.append(
            f"""\
  VSwitch{index}:
    Type: APSARA::ECS::VSwitch
"""
        )
    template = "ROSTemplateFormatVersion: '2015-09-01'\nResources:\n" + "".join(resources)

    mermaid = ros_template_to_mermaid(template)

    assert 'VSwitch1["ECS::VSwitch 1"]' in mermaid
    assert 'VSwitch30["ECS::VSwitch 30"]' in mermaid
    assert "agg_ECS_VSwitch_root" not in mermaid


def test_scaled_ecs_concept_uses_current_language(monkeypatch):
    _set_test_language(monkeypatch, "zh")
    try:
        fillers = []
        for index in range(1, 25):
            fillers.append(
                f"""\
  Bucket{index}:
    Type: ALIYUN::OSS::Bucket
"""
            )
        template = """\
ROSTemplateFormatVersion: '2015-09-01'
Resources:
  ECS:
    Type: ALIYUN::ECS::Instance
  ScalingGroup:
    Type: ALIYUN::ESS::ScalingGroup
  ScalingConfig:
    Type: ALIYUN::ESS::ScalingConfiguration
    Properties:
      InstanceId:
        Ref: ECS
      ScalingGroupId:
        Ref: ScalingGroup
""" + "".join(fillers)

        mermaid = ros_template_to_mermaid(template)

        assert "subgraph layer_ScalingGroupApplicationGroup [应用服务组]" in mermaid
        assert 'ScalingGroupScaledEcs["伸缩 ECS 实例"]' in mermaid
        assert "ScalingGroup -->|弹性伸缩| ScalingGroupScaledEcs" in mermaid
        assert "ECS -.->|伸缩配置| ScalingGroup" in mermaid
        assert "ECS -.-|配置来源| ScalingGroupScaledEcs" not in mermaid
    finally:
        _set_test_language(monkeypatch, "en")


def test_medium_graph_uses_terminal_overview_mode_with_scaled_ecs_concept():
    template = """\
ROSTemplateFormatVersion: '2015-09-01'
Resources:
  VPC:
    Type: ALIYUN::ECS::VPC
  VSwitch1:
    Type: ALIYUN::ECS::VSwitch
    Properties:
      VpcId:
        Ref: VPC
  VSwitch2:
    Type: ALIYUN::ECS::VSwitch
    Properties:
      VpcId:
        Ref: VPC
  SG:
    Type: ALIYUN::ECS::SecurityGroup
  ECS:
    Type: ALIYUN::ECS::Instance
    Properties:
      VSwitchId:
        Ref: VSwitch2
      SecurityGroupId:
        Ref: SG
      UserData:
        Fn::Join:
          - ''
          - - Fn::GetAtt:
                - Redis
                - ConnectionDomain
            - Fn::GetAtt:
                - PolarDB
                - DBClusterId
  SLB:
    Type: ALIYUN::SLB::LoadBalancer
    Properties:
      VSwitchId:
        Ref: VSwitch1
  EIP1:
    Type: ALIYUN::VPC::EIP
  EIP2:
    Type: ALIYUN::VPC::EIP
  EIPAssoc1:
    Type: ALIYUN::VPC::EIPAssociation
    Properties:
      AllocationId:
        Ref: EIP1
      InstanceId:
        Ref: ECS
  EIPAssoc2:
    Type: ALIYUN::VPC::EIPAssociation
    Properties:
      AllocationId:
        Ref: EIP2
      InstanceId:
        Ref: SLB
  DNS:
    Type: ALIYUN::DNS::DomainRecord
  Image:
    Type: ALIYUN::ECS::CustomImage
    Properties:
      InstanceId:
        Ref: ECS
  ScalingConfig:
    Type: ALIYUN::ESS::ScalingConfiguration
    Properties:
      InstanceId:
        Ref: ECS
      ScalingGroupId:
        Ref: ScalingGroup
      ImageId:
        Ref: Image
      SecurityGroupId:
        Ref: SG
  ScalingGroup:
    Type: ALIYUN::ESS::ScalingGroup
    Properties:
      VSwitchId:
        Ref: VSwitch1
  ScalingRule:
    Type: ALIYUN::ESS::ScalingRule
    Properties:
      ScalingGroupId:
        Ref: ScalingGroup
  ScalingEnable:
    Type: ALIYUN::ESS::ScalingGroupEnable
    Properties:
      ScalingGroupId:
        Ref: ScalingGroup
      ScalingConfigurationId:
        Ref: ScalingConfig
      InstanceIds:
        - Ref: ECS
  PolarDB:
    Type: ALIYUN::POLARDB::DBCluster
    Properties:
      VSwitchId:
        Ref: VSwitch2
  PolarDBNodes:
    Type: ALIYUN::POLARDB::DBNodes
    Properties:
      DBClusterId:
        Ref: PolarDB
  PolarDBAccountPrivilege:
    Type: ALIYUN::POLARDB::AccountPrivilege
    Properties:
      DBClusterId:
        Ref: PolarDB
  PolarDBWhitelist:
    Type: ALIYUN::POLARDB::DBClusterAccessWhiteList
    Properties:
      DBClusterId:
        Ref: PolarDB
  Redis:
    Type: ALIYUN::REDIS::Instance
    Properties:
      VSwitchId:
        Ref: VSwitch1
  RedisWhitelist:
    Type: ALIYUN::REDIS::Whitelist
    Properties:
      InstanceId:
        Ref: Redis
  WaitConditionHandle:
    Type: ALIYUN::ROS::WaitConditionHandle
  WaitCondition:
    Type: ALIYUN::ROS::WaitCondition
    Properties:
      Handle:
        Ref: WaitConditionHandle
"""

    result = render_ros_template_architecture(
        template,
        semantic_plan={
            "edges": [
                {
                    "from": "ScalingGroup",
                    "to": "Redis",
                    "kind": "inferred",
                    "label": "cache",
                    "confidence": "low",
                },
                {
                    "from": "SLB",
                    "to": "ScalingGroup",
                    "kind": "traffic",
                    "label": "backend",
                    "confidence": "medium",
                },
                {
                    "from": "ScalingGroup",
                    "to": "ECS",
                    "kind": "management",
                    "label": "Scaling Template",
                    "confidence": "medium",
                },
                {
                    "from": "ScalingGroup",
                    "to": "SLB",
                    "kind": "management",
                    "label": "associated SLB",
                    "confidence": "medium",
                },
            ]
        },
    )
    mermaid = result.mermaid_source
    visible_node_lines = [
        line for line in mermaid.splitlines() if '["' in line and not line.strip().startswith("subgraph ")
    ]

    assert len(visible_node_lines) <= 10
    assert 'Redis["Redis Instance"]' in mermaid
    assert 'PolarDB["PolarDB\\n+ DB node\\n+ Account privilege\\n+ Access whitelist"]' in mermaid
    assert 'ECS["ECS instance\\n+ EIP\\n+ Security group"]' in mermaid
    assert 'SLB["SLB load balancer\\n+ EIP"]' in mermaid
    vswitch1_start = mermaid.index("subgraph layer_VSwitch1")
    vswitch2_start = mermaid.index("subgraph layer_VSwitch2")
    vswitch1 = mermaid[vswitch1_start : mermaid.index("end", vswitch1_start)]
    vswitch2 = mermaid[vswitch2_start : mermaid.index("end", vswitch2_start)]
    assert "subgraph layer_ScalingGroupApplicationGroup" not in mermaid
    assert 'ECS["ECS instance\\n+ EIP\\n+ Security group"]' in vswitch2
    assert 'ScalingGroupScaledEcs["Scaled ECS instances"]' in vswitch1
    assert 'ScalingGroupScaledEcs["Scaled ECS instances"]' in mermaid
    assert "ScalingGroup -->|scales| ScalingGroupScaledEcs" in mermaid
    assert "ECS -.->|scaling config| ScalingGroup" in mermaid
    assert "ECS -.-|config source| ScalingGroupScaledEcs" not in mermaid
    assert "ScalingGroupScaledEcs -.-|cache| Redis" in mermaid
    assert "SLB -->|backend| ScalingGroupScaledEcs" in mermaid
    assert "ScalingGroup -.->|associated SLB| SLB" not in mermaid
    assert "ScalingGroup -.->|ESS Config| ECS" not in mermaid
    assert "ScalingGroup -.->|Scaling Template| ECS" not in mermaid
    assert "subgraph layer_SG [Security group]" not in mermaid
    assert 'ScalingConfig["ESS Config"]' not in mermaid
    assert "ECS -.- ScalingConfig" not in mermaid
    assert "ScalingGroup -.- ScalingConfig" not in mermaid
    assert 'EIP1["EIP"]' not in mermaid
    assert 'EIP2["EIP"]' not in mermaid
    assert "WaitCondition" not in mermaid
    assert "DNS" not in mermaid
    assert "Image" not in mermaid
    assert "ScalingRule" not in mermaid
    assert "ScalingEnable" not in mermaid
    assert "PolarDBNodes" not in mermaid
    assert "PolarDBAccountPrivilege" not in mermaid
    assert "PolarDBWhitelist" not in mermaid
    assert "RedisWhitelist" not in mermaid
    assert {
        "id": "ScalingGroupScaledEcs",
        "label": "Scaled ECS instances",
        "type": "CONCEPT::ESS::ScaledECS",
    } in result.architecture_context["visible_nodes"]
    assert result.architecture_context["concept_nodes"] == [
        {
            "id": "ScalingGroupScaledEcs",
            "label": "Scaled ECS instances",
            "type": "CONCEPT::ESS::ScaledECS",
            "controller": "ScalingGroup",
            "source": "ECS",
            "via": "ScalingConfig",
            "runtime_source": "ECS",
            "group": None,
        }
    ]
    assert result.architecture_context["concept_groups"] == []
    assert {
        "source": "ECS",
        "target": "Redis",
        "property": "UserData",
        "source_type": "ALIYUN::ECS::Instance",
        "target_type": "ALIYUN::REDIS::Instance",
    } in result.architecture_context["property_references"]
    assert result.architecture_context["semantic_plan"]["accepted_edges"] == [
        {
            "from": "ScalingGroupScaledEcs",
            "to": "Redis",
            "kind": "inferred",
            "label": "cache",
            "confidence": "low",
        },
        {
            "from": "SLB",
            "to": "ScalingGroupScaledEcs",
            "kind": "traffic",
            "label": "backend",
            "confidence": "medium",
        },
    ]
    assert result.architecture_context["semantic_plan"]["rejected_edges"] == [
        {
            "from": "ScalingGroup",
            "to": "ECS",
            "reason": "covered by scaled concept",
        },
        {
            "from": "ScalingGroup",
            "to": "SLB",
            "reason": "covered by scaled runtime edge",
        },
    ]


def test_semantic_plan_rejects_runtime_edges_from_scaled_template_source_when_concept_covers_target():
    fillers = []
    for index in range(1, 24):
        fillers.append(
            f"""\
  Bucket{index}:
    Type: ALIYUN::OSS::Bucket
"""
        )
    template = """\
ROSTemplateFormatVersion: '2015-09-01'
Resources:
  ECS:
    Type: ALIYUN::ECS::Instance
  ScalingGroup:
    Type: ALIYUN::ESS::ScalingGroup
  ScalingConfig:
    Type: ALIYUN::ESS::ScalingConfiguration
    Properties:
      InstanceId:
        Ref: ECS
      ScalingGroupId:
        Ref: ScalingGroup
  Redis:
    Type: ALIYUN::REDIS::Instance
""" + "".join(fillers)

    result = render_ros_template_architecture(
        template,
        semantic_plan={
            "edges": [
                {
                    "from": "ScalingGroupScaledEcs",
                    "to": "Redis",
                    "kind": "traffic",
                    "label": "cache access",
                    "confidence": "high",
                },
                {
                    "from": "ECS",
                    "to": "Redis",
                    "kind": "dependency",
                    "label": "cache config",
                    "confidence": "medium",
                },
            ]
        },
    )

    assert "layer_ScalingGroupApplicationGroup -->|cache access| Redis" in result.mermaid_source
    assert "ECS -->|cache config| Redis" not in result.mermaid_source
    assert "ScalingGroupScaledEcs -->|cache config| Redis" not in result.mermaid_source
    assert result.architecture_context["semantic_plan"]["accepted_edges"] == [
        {
            "from": "ScalingGroupApplicationGroup",
            "to": "Redis",
            "kind": "traffic",
            "label": "cache access",
            "confidence": "high",
        }
    ]
    assert result.architecture_context["semantic_plan"]["rejected_edges"] == [
        {
            "from": "ECS",
            "to": "Redis",
            "reason": "covered by scaled runtime edge",
        }
    ]


def test_config_heavy_medium_graph_uses_overview_compaction():
    template = """\
ROSTemplateFormatVersion: '2015-09-01'
Resources:
  VPC:
    Type: ALIYUN::ECS::VPC
  VSwitch:
    Type: ALIYUN::ECS::VSwitch
    Properties:
      VpcId:
        Ref: VPC
  ECS:
    Type: ALIYUN::ECS::Instance
    Properties:
      VSwitchId:
        Ref: VSwitch
  Image:
    Type: ALIYUN::ECS::CustomImage
    Properties:
      InstanceId:
        Ref: ECS
  ScalingGroup:
    Type: ALIYUN::ESS::ScalingGroup
  ScalingConfig:
    Type: ALIYUN::ESS::ScalingConfiguration
    Properties:
      ImageId:
        Ref: Image
      ScalingGroupId:
        Ref: ScalingGroup
  ScalingRule:
    Type: ALIYUN::ESS::ScalingRule
    Properties:
      ScalingGroupId:
        Ref: ScalingGroup
  ScalingEnable:
    Type: ALIYUN::ESS::ScalingGroupEnable
    Properties:
      ScalingGroupId:
        Ref: ScalingGroup
      ScalingConfigurationId:
        Ref: ScalingConfig
  PolarDB:
    Type: ALIYUN::POLARDB::DBCluster
  PolarDBNode:
    Type: ALIYUN::POLARDB::DBInstance
    Properties:
      DBClusterId:
        Ref: PolarDB
  PolarDBPrivilege:
    Type: ALIYUN::POLARDB::AccountPrivilege
    Properties:
      DBClusterId:
        Ref: PolarDB
  PolarDBWhitelist:
    Type: ALIYUN::POLARDB::DBClusterAccessWhiteList
    Properties:
      DBClusterId:
        Ref: PolarDB
"""

    result = render_ros_template_architecture(
        template,
        semantic_plan={
            "edges": [
                {
                    "from": "AppDeploy",
                    "to": "Ack",
                    "kind": "management",
                    "label": "deploy",
                    "confidence": "high",
                }
            ]
        },
    )
    mermaid = result.mermaid_source

    assert result.architecture_context["compacted"] is True
    assert 'ScalingConfig["ESS Config"]' not in mermaid
    assert "ScalingRule" not in mermaid
    assert "ScalingEnable" not in mermaid
    assert "Image" not in mermaid
    assert "PolarDBNode" not in mermaid
    assert "PolarDBPrivilege" not in mermaid
    assert "PolarDBWhitelist" not in mermaid
    assert 'ScalingGroup["ESS Group\\n+ Scaling Configuration\\n+ Scaling Rule"]' in mermaid
    assert 'PolarDB["PolarDB\\n+ DB node\\n+ Account privilege\\n+ Access whitelist"]' in mermaid


def test_compact_graph_folds_ram_access_keys_and_group_membership_into_user():
    template = """\
ROSTemplateFormatVersion: '2015-09-01'
Resources:
  User:
    Type: ALIYUN::RAM::User
  AccessKey:
    Type: ALIYUN::RAM::AccessKey
    Properties:
      UserName:
        Ref: User
  AccessKey2:
    Type: ALIYUN::RAM::AccessKey
    Properties:
      UserName:
        Ref: User
  Group:
    Type: ALIYUN::RAM::Group
  UserGroup:
    Type: ALIYUN::RAM::UserToGroupAddition
    Properties:
      Users:
        - Ref: User
      GroupName:
        Ref: Group
  Bucket1:
    Type: ALIYUN::OSS::Bucket
  Bucket2:
    Type: ALIYUN::OSS::Bucket
"""

    result = render_ros_template_architecture(
        template,
        semantic_plan={
            "edges": [
                {
                    "from": "AppDeploy",
                    "to": "Ack",
                    "kind": "management",
                    "label": "deploy",
                    "confidence": "high",
                }
            ]
        },
    )
    mermaid = result.mermaid_source

    assert result.architecture_context["compacted"] is True
    assert 'AccessKey["AccessKey"]' not in mermaid
    assert 'AccessKey2["AccessKey 2"]' not in mermaid
    assert 'UserGroup["RAM UserToGroupAddition"]' not in mermaid
    assert 'User["RAM User\\n+ AccessKey x2\\n+ User group"]' in mermaid
    assert {
        "source": "UserGroup",
        "source_type": "ALIYUN::RAM::UserToGroupAddition",
        "target": "Group",
        "target_type": "ALIYUN::RAM::Group",
        "property": "GroupName",
    } in result.architecture_context["explicit_relations"]


def test_compact_graph_keeps_distinct_middleware_instance_groups():
    template = """\
ROSTemplateFormatVersion: '2015-09-01'
Resources:
  Vpc:
    Type: ALIYUN::ECS::VPC
  VSwitch:
    Type: ALIYUN::ECS::VSwitch
    Properties:
      VpcId:
        Ref: Vpc
  SecurityGroup:
    Type: ALIYUN::ECS::SecurityGroup
    Properties:
      VpcId:
        Ref: Vpc
  AppServer:
    Type: ALIYUN::ECS::InstanceGroup
    Properties:
      InstanceName: AppServer
      VpcId:
        Ref: Vpc
      VSwitchId:
        Ref: VSwitch
      SecurityGroupId:
        Ref: SecurityGroup
  MongoDBServer:
    Type: ALIYUN::ECS::InstanceGroup
    Properties:
      InstanceName: MongoDBServer
      VpcId:
        Ref: Vpc
      VSwitchId:
        Ref: VSwitch
      SecurityGroupId:
        Ref: SecurityGroup
  RabbitMQServer:
    Type: ALIYUN::ECS::InstanceGroup
    Properties:
      InstanceName: RabbitMQServer
      VpcId:
        Ref: Vpc
      VSwitchId:
        Ref: VSwitch
      SecurityGroupId:
        Ref: SecurityGroup
  Role:
    Type: ALIYUN::RAM::Role
  Bucket:
    Type: ALIYUN::OSS::Bucket
"""

    result = render_ros_template_architecture(template)
    mermaid = result.mermaid_source

    assert "MongoDBServer" in mermaid
    assert "RabbitMQServer" in mermaid
    assert "ECS instance group 1 x3" not in mermaid


def test_compact_graph_keeps_rds_instance_and_folds_database_children():
    template = """\
ROSTemplateFormatVersion: '2015-09-01'
Resources:
  VPC:
    Type: ALIYUN::ECS::VPC
  VSwitch:
    Type: ALIYUN::ECS::VSwitch
    Properties:
      VpcId:
        Ref: VPC
  RdsInstance:
    Type: ALIYUN::RDS::DBInstance
    Properties:
      VSwitchId:
        Ref: VSwitch
  RdsDatabase:
    Type: ALIYUN::RDS::Database
    Properties:
      DBInstanceId:
        Ref: RdsInstance
  RdsAccount:
    Type: ALIYUN::RDS::Account
    Properties:
      DBInstanceId:
        Ref: RdsInstance
  RdsPrivilege:
    Type: ALIYUN::RDS::AccountPrivilege
    Properties:
      DBInstanceId:
        Ref: RdsInstance
      AccountName:
        Ref: RdsAccount
  RdsWhitelist:
    Type: ALIYUN::RDS::DBInstanceSecurityIps
    Properties:
      DBInstanceId:
        Ref: RdsInstance
  ReadOnly1:
    Type: ALIYUN::RDS::ReadOnlyDBInstance
    Properties:
      DBInstanceId:
        Ref: RdsInstance
  ReadOnly2:
    Type: ALIYUN::RDS::ReadOnlyDBInstance
    Properties:
      DBInstanceId:
        Ref: RdsInstance
"""

    result = render_ros_template_architecture(template)
    mermaid = result.mermaid_source

    assert result.architecture_context["compacted"] is True
    assert "RdsDatabase" not in mermaid
    assert "RdsAccount" not in mermaid
    assert "RdsPrivilege" not in mermaid
    assert "RdsWhitelist" not in mermaid
    assert "ReadOnly1" not in mermaid
    assert "ReadOnly2" not in mermaid
    expected_rds_label = (
        'RdsInstance["ApsaraDB RDS Instance\\n+ DB account\\n+ Database\\n+ Account privilege'
        '\\n+ Access whitelist\\n+ Read-only instance x2"]'
    )
    assert expected_rds_label in mermaid


def test_compact_graph_folds_ack_application_config_and_wait_steps():
    fillers = []
    for index in range(1, 24):
        fillers.append(
            f"""\
  Bucket{index}:
    Type: ALIYUN::OSS::Bucket
"""
        )
    template = """\
ROSTemplateFormatVersion: '2015-09-01'
Resources:
  VPC:
    Type: ALIYUN::ECS::VPC
  VSwitch:
    Type: ALIYUN::ECS::VSwitch
    Properties:
      VpcId:
        Ref: VPC
  Ack:
    Type: ALIYUN::CS::ManagedKubernetesCluster
    Properties:
      VpcId:
        Ref: VPC
      VSwitchIds:
        - Ref: VSwitch
  AlbConfig:
    Type: ALIYUN::CS::ClusterApplication
    Properties:
      ClusterId:
        Ref: Ack
      YamlContent: "kind: AlbConfig"
  WaitAlb:
    Type: ALIYUN::ROS::Sleep
    DependsOn:
      - AlbConfig
    Properties:
      CreateDuration: 60
  AppDeploy:
    Type: MODULE::ACS::ComputeNest::FluxOciHelmDeploy
    Properties:
      ClusterId:
        Fn::GetAtt:
          - Ack
          - ClusterId
      ReleaseName: app
""" + "".join(fillers)

    result = render_ros_template_architecture(
        template,
        semantic_plan={
            "edges": [
                {
                    "from": "AppDeploy",
                    "to": "Ack",
                    "kind": "management",
                    "label": "deploy",
                    "confidence": "high",
                }
            ]
        },
    )
    mermaid = result.mermaid_source

    assert result.architecture_context["compacted"] is True
    assert "AlbConfig" not in mermaid
    assert "WaitAlb" not in mermaid
    assert 'Ack["ACK Managed Cluster\\n+ ALB ingress config"]' in mermaid
    assert 'AppDeploy["ACS::FluxOciHelmDeploy"]' in mermaid
    assert "Ack -.- AppDeploy" not in mermaid
    assert "AppDeploy -.->|deploy| Ack" in mermaid
    assert {
        "source": "AppDeploy",
        "target": "Ack",
        "property": "ClusterId",
        "source_type": "MODULE::ACS::ComputeNest::FluxOciHelmDeploy",
        "target_type": "ALIYUN::CS::ManagedKubernetesCluster",
        "target_visible": True,
    } in result.architecture_context["all_property_references"]


def test_compact_graph_preserves_ack_cluster_application_manifest_semantics(monkeypatch):
    _set_test_language(monkeypatch, "zh")
    try:
        template = """\
ROSTemplateFormatVersion: '2015-09-01'
Resources:
  VPC:
    Type: ALIYUN::ECS::VPC
  VSwitch:
    Type: ALIYUN::ECS::VSwitch
    Properties:
      VpcId:
        Ref: VPC
  Ack:
    Type: ALIYUN::CS::ManagedKubernetesCluster
    Properties:
      VpcId:
        Ref: VPC
      VSwitchIds:
        - Ref: VSwitch
  BackendApp:
    Type: ALIYUN::CS::ClusterApplication
    Properties:
      ClusterId:
        Ref: Ack
      YamlContent: |
        apiVersion: apps/v1
        kind: Deployment
        metadata:
          name: tea
        ---
        apiVersion: v1
        kind: Service
        metadata:
          name: tea-svc
  AppIngress:
    Type: ALIYUN::CS::ClusterApplication
    Properties:
      ClusterId:
        Ref: Ack
      YamlContent:
        Fn::Sub: |-
          apiVersion: networking.k8s.io/v1
          kind: Ingress
          metadata:
            name: tea-ingress
  AppHpa:
    Type: ALIYUN::CS::ClusterApplication
    Properties:
      ClusterId:
        Ref: Ack
      YamlContent: |
        apiVersion: autoscaling/v2
        kind: HorizontalPodAutoscaler
        metadata:
          name: tea-hpa
""" + _filler_resources()

        result = render_ros_template_architecture(template)
        mermaid = result.mermaid_source

        assert result.architecture_context["compacted"] is True
        assert "BackendApp" not in mermaid
        assert "AppIngress" not in mermaid
        assert "AppHpa" not in mermaid
        assert "应用工作负载" in mermaid
        assert "服务暴露" in mermaid
        assert "Ingress入口" in mermaid
        assert "HPA弹性伸缩" in mermaid
        assert "ACK应用配置" not in mermaid
    finally:
        _set_test_language(monkeypatch, "en")


def test_compact_graph_exposes_ack_hpa_concepts_for_semantic_planning(monkeypatch):
    _set_test_language(monkeypatch, "zh")
    try:
        template = """\
ROSTemplateFormatVersion: '2015-09-01'
Resources:
  VPC:
    Type: ALIYUN::ECS::VPC
  VSwitch:
    Type: ALIYUN::ECS::VSwitch
    Properties:
      VpcId:
        Ref: VPC
  Ack:
    Type: ALIYUN::CS::ManagedKubernetesCluster
    Properties:
      VpcId:
        Ref: VPC
      VSwitchIds:
        - Ref: VSwitch
  SlsProject:
    Type: ALIYUN::SLS::Project
  AckMetricsAdapter:
    Type: ALIYUN::CS::ClusterHelmApplication
    Properties:
      ClusterId:
        Ref: Ack
      ChartUrl: ack-alibaba-cloud-metrics-adapter
  BackendApp:
    Type: ALIYUN::CS::ClusterApplication
    Properties:
      ClusterId:
        Ref: Ack
      YamlContent: |
        apiVersion: apps/v1
        kind: Deployment
        metadata:
          name: tea
        ---
        apiVersion: v1
        kind: Service
        metadata:
          name: tea-svc
  AppIngress:
    Type: ALIYUN::CS::ClusterApplication
    Properties:
      ClusterId:
        Ref: Ack
      YamlContent: |
        apiVersion: networking.k8s.io/v1
        kind: Ingress
        metadata:
          name: tea-ingress
  AppHpa:
    Type: ALIYUN::CS::ClusterApplication
    Properties:
      ClusterId:
        Ref: Ack
      YamlContent:
        Fn::Sub: |
          apiVersion: autoscaling/v2
          kind: HorizontalPodAutoscaler
          metadata:
            name: tea-hpa
            annotations:
              sls.project: ${SlsProject}
""" + _filler_resources()

        result = render_ros_template_architecture(template)
        context = result.architecture_context
        visible_ids = {node["id"] for node in context["visible_nodes"]}
        visible_edges = {(edge["from"], edge["to"], edge["label"], edge["style"]) for edge in context["visible_edges"]}

        assert {
            "AckApplicationWorkload",
            "AckServiceExposure",
            "AckIngressEntry",
            "AckHpaAutoscaling",
        }.issubset(visible_ids)
        assert ("AckIngressEntry", "AckServiceExposure", "入口路由", "solid_arrow") in visible_edges
        assert ("AckServiceExposure", "AckApplicationWorkload", "服务转发", "solid_arrow") in visible_edges
        assert ("AckHpaAutoscaling", "AckApplicationWorkload", "弹性伸缩", "dotted_arrow") in visible_edges
        assert ("AckMetricsAdapter", "AckHpaAutoscaling", "指标适配", "dotted_open") in visible_edges
        assert ("SlsProject", "AckHpaAutoscaling", "外部指标", "dotted_open") in visible_edges
        assert {
            "cluster": "Ack",
            "source": "AppHpa",
            "kind": "HorizontalPodAutoscaler",
            "name": "tea-hpa",
            "label": "HPA弹性伸缩",
            "template_refs": ["SlsProject"],
        } in context["kubernetes_applications"]
    finally:
        _set_test_language(monkeypatch, "en")


def test_compact_graph_exposes_helm_application_workload_dependencies(monkeypatch):
    _set_test_language(monkeypatch, "zh")
    try:
        template = """\
ROSTemplateFormatVersion: '2015-09-01'
Resources:
  VPC:
    Type: ALIYUN::ECS::VPC
  VSwitch:
    Type: ALIYUN::ECS::VSwitch
    Properties:
      VpcId:
        Ref: VPC
  Ack:
    Type: ALIYUN::CS::ManagedKubernetesCluster
    Properties:
      VpcId:
        Ref: VPC
      VSwitchIds:
        - Ref: VSwitch
  AppIngress:
    Type: ALIYUN::CS::ClusterApplication
    Properties:
      ClusterId:
        Ref: Ack
      YamlContent: |
        apiVersion: networking.k8s.io/v1
        kind: Ingress
        metadata:
          name: app-ingress
        spec:
          rules:
            - http:
                paths:
                  - backend:
                      service:
                        name: app
                        port:
                          number: 80
                    path: /
                    pathType: Prefix
  HelmApp:
    Type: MODULE::ACS::ComputeNest::FluxOciHelmDeploy
    Properties:
      ClusterId:
        Fn::GetAtt:
          - Ack
          - ClusterId
      ReleaseName: app
      WaitUntil:
        - Kind: Deployment
          Name: app
          Namespace: default
      ChartValues:
        externalDatabase:
          host:
            Fn::GetAtt:
              - Rds
              - ConnectionString
        externalRedis:
          host:
            Fn::GetAtt:
              - Redis
              - ConnectionDomain
        vectorStore:
          endpoint:
            Fn::GetAtt:
              - Gpdb
              - ConnectionString
  Rds:
    Type: ALIYUN::RDS::DBInstance
  Redis:
    Type: ALIYUN::REDIS::Instance
  Gpdb:
    Type: ALIYUN::GPDB::DBInstance
""" + _filler_resources()

        result = render_ros_template_architecture(template)
        context = result.architecture_context
        visible_ids = {node["id"] for node in context["visible_nodes"]}
        visible_edges = {(edge["from"], edge["to"], edge["label"], edge["style"]) for edge in context["visible_edges"]}

        assert {
            "AckApplicationWorkload",
            "AckServiceExposure",
            "AckIngressEntry",
        }.issubset(visible_ids)
        assert ("AckIngressEntry", "AckServiceExposure", "入口路由", "solid_arrow") in visible_edges
        assert ("AckServiceExposure", "AckApplicationWorkload", "服务转发", "solid_arrow") in visible_edges
        assert ("AckApplicationWorkload", "Rds", "数据库访问", "solid_arrow") in visible_edges
        assert ("AckApplicationWorkload", "Redis", "缓存访问", "solid_arrow") in visible_edges
        assert ("AckApplicationWorkload", "Gpdb", "向量检索", "solid_arrow") in visible_edges
        assert {
            "cluster": "Ack",
            "source": "HelmApp",
            "kind": "Deployment",
            "name": "app",
            "label": "应用工作负载",
            "template_refs": ["Rds", "Redis", "Gpdb"],
        } in context["kubernetes_applications"]
    finally:
        _set_test_language(monkeypatch, "en")


def test_compact_graph_folds_sls_logstore_into_project():
    fillers = []
    for index in range(1, 8):
        fillers.append(
            f"""\
  Bucket{index}:
    Type: ALIYUN::OSS::Bucket
"""
        )
    template = """\
ROSTemplateFormatVersion: '2015-09-01'
Resources:
  Project:
    Type: ALIYUN::SLS::Project
  Logstore:
    Type: ALIYUN::SLS::Logstore
    Properties:
      ProjectName:
        Ref: Project
""" + "".join(fillers)

    result = render_ros_template_architecture(template)
    mermaid = result.mermaid_source

    assert result.architecture_context["compacted"] is True
    assert 'Logstore["SLS Logstore"]' not in mermaid
    assert 'Project["SLS Project\\n+ Logstore"]' in mermaid


def test_compact_graph_flattens_shared_security_group_without_losing_vswitch_parent():
    fillers = []
    for index in range(1, 24):
        fillers.append(
            f"""\
  Bucket{index}:
    Type: ALIYUN::OSS::Bucket
"""
        )
    template = """\
ROSTemplateFormatVersion: '2015-09-01'
Resources:
  VPC:
    Type: ALIYUN::ECS::VPC
  VSwitch1:
    Type: ALIYUN::ECS::VSwitch
    Properties:
      VpcId:
        Ref: VPC
  VSwitch2:
    Type: ALIYUN::ECS::VSwitch
    Properties:
      VpcId:
        Ref: VPC
  SG:
    Type: ALIYUN::ECS::SecurityGroup
    Properties:
      VpcId:
        Ref: VPC
  ECS1:
    Type: ALIYUN::ECS::Instance
    Properties:
      VSwitchId:
        Ref: VSwitch1
      SecurityGroupId:
        Ref: SG
  ECS2:
    Type: ALIYUN::ECS::Instance
    Properties:
      VSwitchId:
        Ref: VSwitch2
      SecurityGroupId:
        Ref: SG
""" + "".join(fillers)

    mermaid = ros_template_to_mermaid(template)

    assert "subgraph layer_SG [Security group]" not in mermaid
    assert 'ECS1["ECS instance 1\\n+ Security group"]' in mermaid
    assert 'ECS2["ECS instance 2\\n+ Security group"]' in mermaid
    assert 'subgraph layer_VSwitch1 [VSwitch]\n      ECS1["ECS instance 1\\n+ Security group"]' in mermaid
    assert 'subgraph layer_VSwitch2 [VSwitch]\n      ECS2["ECS instance 2\\n+ Security group"]' in mermaid


def test_compact_graph_attaches_bound_marker_resources_from_metadata():
    fillers = []
    for index in range(1, 24):
        fillers.append(
            f"""\
  Bucket{index}:
    Type: ALIYUN::OSS::Bucket
"""
        )
    template = """\
ROSTemplateFormatVersion: '2015-09-01'
Resources:
  ECS:
    Type: ALIYUN::ECS::Instance
  EIP:
    Type: ALIYUN::VPC::EIP
  EIP2:
    Type: ALIYUN::VPC::EIP
  EIPAssoc:
    Type: ALIYUN::VPC::EIPAssociation
    Properties:
      AllocationId:
        Ref: EIP
      InstanceId:
        Ref: ECS
  EIPAssoc2:
    Type: ALIYUN::VPC::EIPAssociation
    Properties:
      AllocationId:
        Ref: EIP2
      InstanceId:
        Ref: ECS
  HaVip:
    Type: ALIYUN::VPC::HaVip
  HaVipAssoc:
    Type: ALIYUN::VPC::HaVipAssociation
    Properties:
      HaVipId:
        Ref: HaVip
      InstanceId:
        Ref: ECS
""" + "".join(fillers)

    mermaid = ros_template_to_mermaid(template)

    assert 'ECS["ECS instance\\n+ EIP x2\\n+ HA-VIP"]' in mermaid
    assert 'EIP["EIP"]' not in mermaid
    assert 'EIP2["EIP"]' not in mermaid
    assert "HaVip[" not in mermaid
    assert "EIP --> ECS" not in mermaid
    assert "HaVip --> ECS" not in mermaid


def test_compact_graph_folds_network_interface_and_nested_eip_into_instance():
    fillers = []
    for index in range(1, 24):
        fillers.append(
            f"""\
  Bucket{index}:
    Type: ALIYUN::OSS::Bucket
"""
        )
    template = """\
ROSTemplateFormatVersion: '2015-09-01'
Resources:
  ECS:
    Type: ALIYUN::ECS::Instance
  ENI:
    Type: ALIYUN::ECS::NetworkInterface
  ENIAttachment:
    Type: ALIYUN::ECS::NetworkInterfaceAttachment
    Properties:
      NetworkInterfaceId:
        Ref: ENI
      InstanceId:
        Ref: ECS
  EIP:
    Type: ALIYUN::VPC::EIP
  EIPAssoc:
    Type: ALIYUN::VPC::EIPAssociation
    Properties:
      AllocationId:
        Ref: EIP
      InstanceId:
        Ref: ENI
""" + "".join(fillers)

    mermaid = ros_template_to_mermaid(template)

    assert 'ECS["ECS instance\\n+ ENI\\n+ EIP"]' in mermaid
    assert 'ENI["ENI"]' not in mermaid
    assert 'EIP["EIP"]' not in mermaid
    assert "EIP --> ENI" not in mermaid
    assert "ENI --> ECS" not in mermaid


def test_compact_graph_uses_supplemental_anycast_eip_target_relation():
    template = """\
ROSTemplateFormatVersion: '2015-09-01'
Resources:
  ECS:
    Type: ALIYUN::ECS::Instance
  AnycastEIP:
    Type: ALIYUN::VPC::AnycastEIP
  AnycastAssoc:
    Type: ALIYUN::VPC::AnycastEIPAssociation
    Properties:
      AnycastId:
        Ref: AnycastEIP
      BindInstanceId:
        Ref: ECS
      BindInstanceType: ECS
      BindInstanceRegionId: cn-hangzhou
""" + _filler_resources()

    mermaid = ros_template_to_mermaid(template)

    assert 'ECS["ECS instance\\n+ Anycast EIP"]' in mermaid
    assert 'AnycastEIP["Anycast EIP"]' not in mermaid
    assert "AnycastAssoc" not in mermaid


def test_compact_graph_can_fold_child_attachment_into_layer_label():
    template = """\
ROSTemplateFormatVersion: '2015-09-01'
Resources:
  VPC:
    Type: ALIYUN::ECS::VPC
  VSwitch:
    Type: ALIYUN::ECS::VSwitch
    Properties:
      VpcId:
        Ref: VPC
      CidrBlock: 192.168.0.0/24
  NetworkAcl:
    Type: ALIYUN::VPC::NetworkAcl
    Properties:
      VpcId:
        Ref: VPC
  NetworkAclAssoc:
    Type: ALIYUN::VPC::NetworkAclAssociation
    Properties:
      NetworkAclId:
        Ref: NetworkAcl
      Resources:
        - ResourceId:
            Ref: VSwitch
          ResourceType: VSwitch
""" + _filler_resources()

    mermaid = ros_template_to_mermaid(template)

    assert "subgraph layer_VSwitch [VSwitch (192.168.0.0/24)]" in mermaid
    assert 'layer_VSwitch_Config["VSwitch configuration\\n+ Associate vSwitch"]' in mermaid
    assert "NetworkAclAssoc" not in mermaid


def test_compact_graph_bridge_attachment_can_use_auxiliary_via_resource():
    template = """\
ROSTemplateFormatVersion: '2015-09-01'
Resources:
  VPC:
    Type: ALIYUN::ECS::VPC
  Dhcp:
    Type: ALIYUN::VPC::DhcpOptionsSet
  DhcpAttach:
    Type: ALIYUN::VPC::DhcpOptionsSetAttachment
    Properties:
      DhcpOptionsSetId:
        Ref: Dhcp
      VpcId:
        Ref: VPC
""" + _filler_resources()

    mermaid = ros_template_to_mermaid(template)

    assert "subgraph layer_VPC [VPC]" in mermaid
    assert 'layer_VPC_Config["VPC configuration\\n+ Associate DHCP Options Set"]' in mermaid
    assert 'Dhcp["DHCP Option Set"]' not in mermaid
    assert "DhcpAttach" not in mermaid
    assert "Associate VPC" not in mermaid


def test_compact_graph_bridge_attachment_folds_ecd_network_package():
    template = """\
ROSTemplateFormatVersion: '2015-09-01'
Resources:
  OfficeSite:
    Type: ALIYUN::ECD::SimpleOfficeSite
  Package:
    Type: ALIYUN::ECD::NetworkPackage
    Properties:
      OfficeSiteId:
        Ref: OfficeSite
  PackageAssoc:
    Type: ALIYUN::ECD::NetworkPackageAssociation
    Properties:
      NetworkPackageId:
        Ref: Package
      OfficeSiteId:
        Ref: OfficeSite
""" + _filler_resources()

    mermaid = ros_template_to_mermaid(template)

    assert 'OfficeSite["EDS Convenience Account Office Network\\n+ Bandwidth Plan"]' in mermaid
    assert 'Package["EDS Bandwidth Plan"]' not in mermaid
    assert "PackageAssoc" not in mermaid


def test_compact_graph_bridge_attachment_folds_sag_acl_and_qos():
    template = """\
ROSTemplateFormatVersion: '2015-09-01'
Resources:
  Sag:
    Type: ALIYUN::SAG::SmartAccessGateway
  Acl:
    Type: ALIYUN::SAG::ACL
  AclAssoc:
    Type: ALIYUN::SAG::ACLAssociation
    Properties:
      AclId:
        Ref: Acl
      SmartAGId:
        Ref: Sag
  Qos:
    Type: ALIYUN::SAG::Qos
  QosAssoc:
    Type: ALIYUN::SAG::QosAssociation
    Properties:
      QosId:
        Ref: Qos
      SmartAGId:
        Ref: Sag
""" + _filler_resources()

    mermaid = ros_template_to_mermaid(template)

    assert 'Sag["SAG Instance\\n+ Associate ACL\\n+ Attach QoS Policy"]' in mermaid
    assert 'Acl["ACL"]' not in mermaid
    assert 'Qos["Qos"]' not in mermaid
    assert "AclAssoc" not in mermaid
    assert "QosAssoc" not in mermaid


def test_compact_graph_hides_unattached_attachment_markers():
    fillers = []
    for index in range(1, 24):
        fillers.append(
            f"""\
  Bucket{index}:
    Type: ALIYUN::OSS::Bucket
"""
        )
    template = """\
ROSTemplateFormatVersion: '2015-09-01'
Resources:
  ECS:
    Type: ALIYUN::ECS::Instance
  UnusedEIP:
    Type: ALIYUN::VPC::EIP
  UnusedENI:
    Type: ALIYUN::ECS::NetworkInterface
""" + "".join(fillers)

    mermaid = ros_template_to_mermaid(template)

    assert 'ECS["ECS instance"]' in mermaid
    assert "UnusedEIP" not in mermaid
    assert "UnusedENI" not in mermaid


def test_compact_graph_folds_nat_gateway_child_resources():
    fillers = []
    for index in range(1, 24):
        fillers.append(
            f"""\
  Bucket{index}:
    Type: ALIYUN::OSS::Bucket
"""
        )
    template = """\
ROSTemplateFormatVersion: '2015-09-01'
Resources:
  VPC:
    Type: ALIYUN::ECS::VPC
  VSwitch:
    Type: ALIYUN::ECS::VSwitch
    Properties:
      VpcId:
        Ref: VPC
  NatGateway:
    Type: ALIYUN::VPC::NatGateway
    Properties:
      VpcId:
        Ref: VPC
      VSwitchId:
        Ref: VSwitch
  EIP1:
    Type: ALIYUN::VPC::EIP
  EIP2:
    Type: ALIYUN::VPC::EIP
  EIPBind1:
    Type: ALIYUN::VPC::EIPAssociation
    Properties:
      AllocationId:
        Ref: EIP1
      InstanceId:
        Ref: NatGateway
  EIPBind2:
    Type: ALIYUN::VPC::EIPAssociation
    Properties:
      AllocationId:
        Ref: EIP2
      InstanceId:
        Ref: NatGateway
  VpcSnat:
    Type: ALIYUN::VPC::SnatEntry
    Properties:
      SnatTableId:
        Fn::GetAtt:
        - NatGateway
        - SNatTableId
  EcsSnat:
    Type: ALIYUN::ECS::SNatEntry
    Properties:
      SNatTableId:
        Fn::GetAtt:
        - NatGateway
        - SNatTableId
  Dnat:
    Type: ALIYUN::VPC::ForwardEntry
    Properties:
      ForwardTableId:
        Fn::GetAtt:
        - NatGateway
        - ForwardTableId
  NatIp:
    Type: ALIYUN::VPC::NatIp
    Properties:
      NatGatewayId:
        Ref: NatGateway
  NatIpCidr:
    Type: ALIYUN::VPC::NatIpCidr
    Properties:
      NatGatewayId:
        Ref: NatGateway
""" + "".join(fillers)

    mermaid = ros_template_to_mermaid(template)

    assert 'NatGateway["NAT gateway\\n+ EIP x2\\n+ SNAT entry x2\\n+ DNAT entry\\n+ NAT IP\\n+ NAT IP CIDR"]' in mermaid
    assert 'EIP1["EIP"]' not in mermaid
    assert 'EIP2["EIP"]' not in mermaid
    assert 'VpcSnat["SNAT Entry"]' not in mermaid
    assert 'EcsSnat["SNAT Table"]' not in mermaid
    assert 'Dnat["VPC Forward Entry"]' not in mermaid
    assert 'NatIp["NAT IP Address"]' not in mermaid
    assert 'NatIpCidr["VPC NAT IP CIDR"]' not in mermaid
    assert "EIP1 --> NatGateway" not in mermaid
    assert "EIP2 --> NatGateway" not in mermaid


def test_compact_graph_folds_shared_bandwidth_ip_into_bandwidth_package():
    fillers = []
    for index in range(1, 24):
        fillers.append(
            f"""\
  Bucket{index}:
    Type: ALIYUN::OSS::Bucket
"""
        )
    template = """\
ROSTemplateFormatVersion: '2015-09-01'
Resources:
  BandwidthPackage:
    Type: ALIYUN::VPC::CommonBandwidthPackage
  BandwidthPackageIp:
    Type: ALIYUN::VPC::CommonBandwidthPackageIp
    Properties:
      BandwidthPackageId:
        Ref: BandwidthPackage
  ECS:
    Type: ALIYUN::ECS::InstanceGroup
""" + "".join(fillers)

    mermaid = ros_template_to_mermaid(template)

    assert 'BandwidthPackage["Shared bandwidth package\\n+ Shared bandwidth IP"]' in mermaid
    assert 'BandwidthPackageIp["Internet Shared Bandwidth Instance IP"]' not in mermaid


def test_compact_graph_keeps_shared_bandwidth_package_connected_to_eip_owner():
    fillers = []
    for index in range(1, 24):
        fillers.append(
            f"""\
  Bucket{index}:
    Type: ALIYUN::OSS::Bucket
"""
        )
    template = """\
ROSTemplateFormatVersion: '2015-09-01'
Resources:
  BandwidthPackage:
    Type: ALIYUN::VPC::CommonBandwidthPackage
  BandwidthPackageIp:
    Type: ALIYUN::VPC::CommonBandwidthPackageIp
    Properties:
      BandwidthPackageId:
        Ref: BandwidthPackage
      Eips:
      - AllocationId:
          Ref: EIP
  EIP:
    Type: ALIYUN::VPC::EIP
  EIPAssociation:
    Type: ALIYUN::VPC::EIPAssociation
    Properties:
      AllocationId:
        Ref: EIP
      InstanceId:
        Ref: ECS
  ECS:
    Type: ALIYUN::ECS::InstanceGroup
""" + "".join(fillers)

    result = render_ros_template_architecture(template)
    mermaid = result.mermaid_source

    assert 'BandwidthPackage["Shared bandwidth package\\n+ Shared bandwidth IP"]' in mermaid
    assert 'ECS["ECS instance group\\n+ EIP"]' in mermaid
    assert "BandwidthPackage -.-|public bandwidth| ECS" in mermaid
    assert 'BandwidthPackageIp["Internet Shared Bandwidth Instance IP"]' not in mermaid
    assert 'EIP["EIP"]' not in mermaid
    assert {
        "from": "BandwidthPackage",
        "to": "ECS",
        "style": "dotted_open",
        "label": "public bandwidth",
    } in result.architecture_context["visible_edges"]


def test_semantic_plan_does_not_override_labeled_shared_bandwidth_relation(monkeypatch):
    _set_test_language(monkeypatch, "zh")
    try:
        fillers = []
        for index in range(1, 24):
            fillers.append(
                f"""\
  Bucket{index}:
    Type: ALIYUN::OSS::Bucket
"""
            )
        template = """\
ROSTemplateFormatVersion: '2015-09-01'
Resources:
  BandwidthPackage:
    Type: ALIYUN::VPC::CommonBandwidthPackage
  BandwidthPackageIp:
    Type: ALIYUN::VPC::CommonBandwidthPackageIp
    Properties:
      BandwidthPackageId:
        Ref: BandwidthPackage
      Eips:
      - AllocationId:
          Ref: EIP
  EIP:
    Type: ALIYUN::VPC::EIP
  EIPAssociation:
    Type: ALIYUN::VPC::EIPAssociation
    Properties:
      AllocationId:
        Ref: EIP
      InstanceId:
        Ref: ECS
  ECS:
    Type: ALIYUN::ECS::InstanceGroup
""" + "".join(fillers)

        result = render_ros_template_architecture(
            template,
            semantic_plan={
                "edges": [
                    {
                        "from": "BandwidthPackage",
                        "to": "ECS",
                        "kind": "traffic",
                        "label": "提供公网带宽",
                        "confidence": "high",
                    }
                ]
            },
        )
        mermaid = result.mermaid_source

        assert "BandwidthPackage -.-|公网带宽| ECS" in mermaid
        assert "BandwidthPackage -->|提供公网带宽| ECS" not in mermaid
        assert result.architecture_context["semantic_plan"]["accepted_edges"] == []
        assert result.architecture_context["semantic_plan"]["rejected_edges"] == [
            {
                "from": "BandwidthPackage",
                "to": "ECS",
                "reason": "covered by deterministic edge",
            }
        ]
    finally:
        _set_test_language(monkeypatch, "en")


def test_compact_graph_folds_network_control_plane_and_rewrites_hidden_semantic_edges(monkeypatch):
    _set_test_language(monkeypatch, "en")
    try:
        fillers = _filler_resources(22)
        template = (
            """\
ROSTemplateFormatVersion: '2015-09-01'
Resources:
  VPC:
    Type: ALIYUN::ECS::VPC
    Properties:
      CidrBlock: 192.168.0.0/16
  VSwitch:
    Type: ALIYUN::ECS::VSwitch
    Properties:
      VpcId:
        Ref: VPC
      CidrBlock: 192.168.0.0/24
  Route:
    Type: ALIYUN::ECS::Route
    Properties:
      RouteTableId:
        Fn::GetAtt:
        - VPC
        - RouteTableId
  BandwidthPackage:
    Type: ALIYUN::VPC::CommonBandwidthPackage
  NLB:
    Type: ALIYUN::NLB::LoadBalancer
    Properties:
      VpcId:
        Ref: VPC
      BandwidthPackageId:
        Ref: BandwidthPackage
  NLBServerGroup:
    Type: ALIYUN::NLB::ServerGroup
    Properties:
      VpcId:
        Ref: VPC
      Servers:
      - ServerId:
          Fn::GetAtt:
          - ALB
          - LoadBalancerId
  NLBListener:
    Type: ALIYUN::NLB::Listener
    Properties:
      LoadBalancerId:
        Ref: NLB
      ServerGroupId:
        Ref: NLBServerGroup
  ALB:
    Type: ALIYUN::ALB::LoadBalancer
    Properties:
      VpcId:
        Ref: VPC
  ALBServerGroup:
    Type: ALIYUN::ALB::ServerGroup
    Properties:
      VpcId:
        Ref: VPC
  ALBListener:
    Type: ALIYUN::ALB::Listener
    Properties:
      LoadBalancerId:
        Ref: ALB
      DefaultActions:
      - ForwardGroupConfig:
          ServerGroupTuples:
          - ServerGroupId:
              Ref: ALBServerGroup
  ALBBackend:
    Type: ALIYUN::ALB::BackendServerAttachment
    Properties:
      ServerGroupId:
        Ref: ALBServerGroup
      Servers:
      - ServerId:
          Ref: ECS
  ECS:
    Type: ALIYUN::ECS::Instance
    Properties:
      VSwitchId:
        Ref: VSwitch
  CEN:
    Type: ALIYUN::CEN::CenInstance
  TransitRouter:
    Type: ALIYUN::CEN::TransitRouter
    Properties:
      CenId:
        Ref: CEN
  VpcAttachment:
    Type: ALIYUN::CEN::TransitRouterVpcAttachment
    Properties:
      TransitRouterId:
        Ref: TransitRouter
      VpcId:
        Ref: VPC
  RouteTable:
    Type: ALIYUN::CEN::TransitRouterRouteTable
    Properties:
      TransitRouterId:
        Ref: TransitRouter
  RouteAssociation:
    Type: ALIYUN::CEN::TransitRouterRouteTableAssociation
    Properties:
      TransitRouterRouteTableId:
        Ref: RouteTable
      TransitRouterAttachmentId:
        Ref: VpcAttachment
  RoutePropagation:
    Type: ALIYUN::CEN::TransitRouterRouteTablePropagation
    Properties:
      TransitRouterRouteTableId:
        Ref: RouteTable
      TransitRouterAttachmentId:
        Ref: VpcAttachment
  RoutePropagationDefault:
    Type: ALIYUN::CEN::TransitRouterRouteTablePropagation
    Properties:
      TransitRouterRouteTableId:
        Fn::GetAtt:
        - TransitRouter
        - SystemTransitRouterRouteTableId
      TransitRouterAttachmentId:
        Ref: VpcAttachment
  RouteEntry:
    Type: ALIYUN::CEN::TransitRouterRouteEntry
    Properties:
      TransitRouterRouteTableId:
        Ref: RouteTable
      TransitRouterRouteEntryNextHopId:
        Ref: VpcAttachment
"""
            + fillers
        )

        result = render_ros_template_architecture(
            template,
            semantic_plan={
                "edges": [
                    {
                        "from": "NLBServerGroup",
                        "to": "ALB",
                        "kind": "traffic",
                        "label": "backend",
                        "confidence": "high",
                    },
                    {
                        "from": "ALBServerGroup",
                        "to": "ECS",
                        "kind": "traffic",
                        "label": "backend",
                        "confidence": "high",
                    },
                ]
            },
        )
        mermaid = result.mermaid_source

        assert "NLBServerGroup" not in mermaid
        assert "ALBServerGroup" not in mermaid
        assert 'BandwidthPackage["Shared bandwidth package"]' not in mermaid
        assert 'Route["Route"]' not in mermaid
        assert 'RouteTable["CEN Route Table' not in mermaid
        assert 'RoutePropagation["CEN Route Learning Correlation"]' not in mermaid
        assert 'RoutePropagationDefault["' not in mermaid
        assert 'VpcAttachment["Connect VPC to Transit Router via CEN"]' not in mermaid
        assert "subgraph layer_VPC [VPC (192.168.0.0/16)]" in mermaid
        assert 'layer_VPC_Config["VPC configuration\\n+ Route"]' in mermaid
        assert 'NLB["NLB Instance\\n+ Listener\\n+ Shared bandwidth package\\n+ NLB server group"]' in mermaid
        assert 'ALB["ALB Instance\\n+ Listener\\n+ ALB server group\\n+ Attach Backend Server"]' in mermaid
        assert 'TransitRouter["CEN Transit Router' in mermaid
        assert "\\n+ VPC connection" in mermaid
        assert "\\n+ CEN route table" in mermaid
        assert "\\n+ Route Learning" in mermaid
        assert "TransitRouter -.-|VPC connection| layer_VPC_Config" not in mermaid
        assert "TransitRouter -.-|VPC connection| layer_VPC\n" not in mermaid
        assert "NLB -->|backend| ALB" in mermaid
        assert "ALB -->|backend| ECS" in mermaid
        assert {
            "from": "NLB",
            "to": "ALB",
            "kind": "traffic",
            "label": "backend",
            "confidence": "high",
        } in result.architecture_context["semantic_plan"]["accepted_edges"]
        assert {
            "from": "ALB",
            "to": "ECS",
            "kind": "traffic",
            "label": "backend",
            "confidence": "high",
        } in result.architecture_context["semantic_plan"]["accepted_edges"]
    finally:
        _set_test_language(monkeypatch, "en")


def test_architecture_context_keeps_route_next_hop_relations_for_hidden_routes():
    template = """\
ROSTemplateFormatVersion: '2015-09-01'
Resources:
  VpcSec:
    Type: ALIYUN::ECS::VPC
  VSwitchSec:
    Type: ALIYUN::ECS::VSwitch
    Properties:
      VpcId:
        Ref: VpcSec
  SecurityGroup:
    Type: ALIYUN::ECS::SecurityGroup
    Properties:
      VpcId:
        Ref: VpcSec
  ForwarderGroup:
    Type: ALIYUN::ECS::InstanceGroup
    Properties:
      VSwitchId:
        Ref: VSwitchSec
      SecurityGroupId:
        Ref: SecurityGroup
  TransitRouter:
    Type: ALIYUN::CEN::TransitRouter
  VpcSecAttachment:
    Type: ALIYUN::CEN::TransitRouterVpcAttachment
    Properties:
      TransitRouterId:
        Ref: TransitRouter
      VpcId:
        Ref: VpcSec
  VpcSecRouteTable:
    Type: ALIYUN::VPC::RouteTable
    Properties:
      VpcId:
        Ref: VpcSec
  RouteForwardToEcs:
    Type: ALIYUN::ECS::Route
    Properties:
      RouteTableId:
        Fn::GetAtt:
        - VpcSec
        - RouteTableId
      DestinationCidrBlock: 0.0.0.0/0
      NextHopType: Instance
      NextHopId:
        Fn::Select:
        - 0
        - Fn::GetAtt:
          - ForwarderGroup
          - InstanceIds
  RouteForwardToCen:
    Type: ALIYUN::ECS::Route
    Properties:
      RouteTableId:
        Ref: VpcSecRouteTable
      DestinationCidrBlock: 0.0.0.0/0
      NextHopType: Attachment
      NextHopId:
        Ref: VpcSecAttachment
  TransitRouterRouteTable:
    Type: ALIYUN::CEN::TransitRouterRouteTable
    Properties:
      TransitRouterId:
        Ref: TransitRouter
  TransitRouterDefaultRoute:
    Type: ALIYUN::CEN::TransitRouterRouteEntry
    Properties:
      TransitRouterRouteTableId:
        Ref: TransitRouterRouteTable
      DestinationCidrBlock: 0.0.0.0/0
      TransitRouterRouteEntryNextHopId:
        Ref: VpcSecAttachment
"""

    result = render_ros_template_architecture(template)

    assert {
        "source": "RouteForwardToEcs",
        "target": "ForwarderGroup",
        "property": "NextHopId",
        "source_type": "ALIYUN::ECS::Route",
        "target_type": "ALIYUN::ECS::InstanceGroup",
    } in result.architecture_context["explicit_relations"]
    assert {
        "source": "RouteForwardToCen",
        "target": "VpcSecAttachment",
        "property": "NextHopId",
        "source_type": "ALIYUN::ECS::Route",
        "target_type": "ALIYUN::CEN::TransitRouterVpcAttachment",
    } in result.architecture_context["explicit_relations"]
    assert {
        "source": "TransitRouterDefaultRoute",
        "target": "VpcSecAttachment",
        "property": "TransitRouterRouteEntryNextHopId",
        "source_type": "ALIYUN::CEN::TransitRouterRouteEntry",
        "target_type": "ALIYUN::CEN::TransitRouterVpcAttachment",
    } in result.architecture_context["explicit_relations"]
    assert {
        "id": "RouteForwardToEcs",
        "type": "ALIYUN::ECS::Route",
        "destination": "0.0.0.0/0",
        "route_table": "VpcSec",
        "route_table_resource": "VpcSec",
        "route_table_resource_type": "ALIYUN::ECS::VPC",
        "next_hop_type": "Instance",
        "next_hop": "ForwarderGroup",
        "next_hop_resource": "ForwarderGroup",
        "next_hop_resource_type": "ALIYUN::ECS::InstanceGroup",
    } in result.architecture_context["route_intents"]
    assert {
        "id": "RouteForwardToCen",
        "type": "ALIYUN::ECS::Route",
        "destination": "0.0.0.0/0",
        "route_table": "VpcSecRouteTable",
        "route_table_resource": "VpcSecRouteTable",
        "route_table_resource_type": "ALIYUN::VPC::RouteTable",
        "next_hop_type": "Attachment",
        "next_hop": "VpcSecAttachment",
        "next_hop_resource": "VpcSecAttachment",
        "next_hop_resource_type": "ALIYUN::CEN::TransitRouterVpcAttachment",
    } in result.architecture_context["route_intents"]
    assert {
        "id": "TransitRouterDefaultRoute",
        "type": "ALIYUN::CEN::TransitRouterRouteEntry",
        "destination": "0.0.0.0/0",
        "route_table": "TransitRouterRouteTable",
        "route_table_resource": "TransitRouterRouteTable",
        "route_table_resource_type": "ALIYUN::CEN::TransitRouterRouteTable",
        "next_hop": "VpcSecAttachment",
        "next_hop_resource": "VpcSecAttachment",
        "next_hop_resource_type": "ALIYUN::CEN::TransitRouterVpcAttachment",
    } in result.architecture_context["route_intents"]


def test_compact_graph_counts_network_layers_as_visible_complexity(monkeypatch):
    _set_test_language(monkeypatch, "en")
    try:
        template = """\
ROSTemplateFormatVersion: '2015-09-01'
Resources:
  CEN:
    Type: ALIYUN::CEN::CenInstance
  TransitRouter:
    Type: ALIYUN::CEN::TransitRouter
    Properties:
      CenId:
        Ref: CEN
  VPC1:
    Type: ALIYUN::ECS::VPC
    Properties:
      CidrBlock: 10.1.0.0/16
  VPC2:
    Type: ALIYUN::ECS::VPC
    Properties:
      CidrBlock: 10.2.0.0/16
  VPC3:
    Type: ALIYUN::ECS::VPC
    Properties:
      CidrBlock: 10.3.0.0/16
  CustomRouteTable1:
    Type: ALIYUN::VPC::RouteTable
    Properties:
      VpcId:
        Ref: VPC1
  VSwitch1A:
    Type: ALIYUN::ECS::VSwitch
    Properties:
      VpcId:
        Ref: VPC1
      CidrBlock: 10.1.0.0/24
  VSwitch1B:
    Type: ALIYUN::ECS::VSwitch
    Properties:
      VpcId:
        Ref: VPC1
      CidrBlock: 10.1.1.0/24
  VSwitch1C:
    Type: ALIYUN::ECS::VSwitch
    Properties:
      VpcId:
        Ref: VPC1
      CidrBlock: 10.1.2.0/24
  VSwitch2A:
    Type: ALIYUN::ECS::VSwitch
    Properties:
      VpcId:
        Ref: VPC2
      CidrBlock: 10.2.0.0/24
  VSwitch2B:
    Type: ALIYUN::ECS::VSwitch
    Properties:
      VpcId:
        Ref: VPC2
      CidrBlock: 10.2.1.0/24
  VSwitch2C:
    Type: ALIYUN::ECS::VSwitch
    Properties:
      VpcId:
        Ref: VPC2
      CidrBlock: 10.2.2.0/24
  VSwitch3A:
    Type: ALIYUN::ECS::VSwitch
    Properties:
      VpcId:
        Ref: VPC3
      CidrBlock: 10.3.0.0/24
  VSwitch3B:
    Type: ALIYUN::ECS::VSwitch
    Properties:
      VpcId:
        Ref: VPC3
      CidrBlock: 10.3.1.0/24
  VSwitch3C:
    Type: ALIYUN::ECS::VSwitch
    Properties:
      VpcId:
        Ref: VPC3
      CidrBlock: 10.3.2.0/24
  SecurityGroup1:
    Type: ALIYUN::ECS::SecurityGroup
    Properties:
      VpcId:
        Ref: VPC1
  SecurityGroup2:
    Type: ALIYUN::ECS::SecurityGroup
    Properties:
      VpcId:
        Ref: VPC2
  SecurityGroup3:
    Type: ALIYUN::ECS::SecurityGroup
    Properties:
      VpcId:
        Ref: VPC3
  ECS1:
    Type: ALIYUN::ECS::Instance
    Properties:
      VSwitchId:
        Ref: VSwitch1C
      SecurityGroupId:
        Ref: SecurityGroup1
  ECS2:
    Type: ALIYUN::ECS::Instance
    Properties:
      VSwitchId:
        Ref: VSwitch2C
      SecurityGroupId:
        Ref: SecurityGroup2
  ECS3:
    Type: ALIYUN::ECS::Instance
    Properties:
      VSwitchId:
        Ref: VSwitch3C
      SecurityGroupId:
        Ref: SecurityGroup3
  Route1:
    Type: ALIYUN::ECS::Route
    Properties:
      RouteTableId:
        Fn::GetAtt:
        - VPC1
        - RouteTableId
  Route2:
    Type: ALIYUN::ECS::Route
    Properties:
      RouteTableId:
        Fn::GetAtt:
        - VPC2
        - RouteTableId
  RouteToCustomTable:
    Type: ALIYUN::ECS::Route
    Properties:
      RouteTableId:
        Ref: CustomRouteTable1
  VpcAttachment1:
    Type: ALIYUN::CEN::TransitRouterVpcAttachment
    Properties:
      TransitRouterId:
        Ref: TransitRouter
      VpcId:
        Ref: VPC1
  VpcAttachment2:
    Type: ALIYUN::CEN::TransitRouterVpcAttachment
    Properties:
      TransitRouterId:
        Ref: TransitRouter
      VpcId:
        Ref: VPC2
  VpcAttachment3:
    Type: ALIYUN::CEN::TransitRouterVpcAttachment
    Properties:
      TransitRouterId:
        Ref: TransitRouter
      VpcId:
        Ref: VPC3
  RouteTable:
    Type: ALIYUN::CEN::TransitRouterRouteTable
    Properties:
      TransitRouterId:
        Ref: TransitRouter
  RoutePropagation:
    Type: ALIYUN::CEN::TransitRouterRouteTablePropagation
    Properties:
      TransitRouterRouteTableId:
        Ref: RouteTable
      TransitRouterAttachmentId:
        Ref: VpcAttachment1
"""

        result = render_ros_template_architecture(template)
        mermaid = result.mermaid_source

        assert result.architecture_context["compacted"] is True
        assert 'Route1["Route"]' not in mermaid
        assert 'CustomRouteTable1["VPC Route Table"]' not in mermaid
        assert 'RouteToCustomTable["Route"]' not in mermaid
        assert 'RouteTable["CEN Route Table' not in mermaid
        assert 'RoutePropagation["CEN Route Learning Correlation"]' not in mermaid
        assert 'TransitRouter["CEN Transit Router' in mermaid
        assert "\\n+ VPC connection x3" in mermaid
        assert "\\n+ CEN route table" in mermaid
        assert "\\n+ Route Learning" in mermaid
        assert 'layer_VPC1_Config["VPC configuration\\n+ VPC route table\\n+ Route x2"]' in mermaid
    finally:
        _set_test_language(monkeypatch, "en")


def test_compact_graph_folds_nas_mount_and_access_control_into_file_system():
    fillers = []
    for index in range(1, 19):
        fillers.append(
            f"""\
  Bucket{index}:
    Type: ALIYUN::OSS::Bucket
"""
        )
    template = """\
ROSTemplateFormatVersion: '2015-09-01'
Resources:
  VPC:
    Type: ALIYUN::ECS::VPC
  VSwitch:
    Type: ALIYUN::ECS::VSwitch
    Properties:
      VpcId:
        Ref: VPC
  SecurityGroup:
    Type: ALIYUN::ECS::SecurityGroup
    Properties:
      VpcId:
        Ref: VPC
  EcsGroup:
    Type: ALIYUN::ECS::InstanceGroup
    Properties:
      VSwitchId:
        Ref: VSwitch
      SecurityGroupId:
        Ref: SecurityGroup
      UserData:
        Fn::Join:
        - ''
        - - 'mount -t nfs '
          - Fn::GetAtt:
            - NasMountTarget
            - MountTargetDomain
          - ':/ /mnt'
  NasAccessGroup:
    Type: ALIYUN::NAS::AccessGroup
  NasAccessRule:
    Type: ALIYUN::NAS::AccessRule
    Properties:
      AccessGroupName:
        Ref: NasAccessGroup
  NasFileSystem:
    Type: ALIYUN::NAS::FileSystem
  NasMountTarget:
    Type: ALIYUN::NAS::MountTarget
    Properties:
      VpcId:
        Ref: VPC
      VSwitchId:
        Ref: VSwitch
      AccessGroupName:
        Ref: NasAccessGroup
      FileSystemId:
        Ref: NasFileSystem
    DependsOn:
    - NasAccessRule
""" + "".join(fillers)

    result = render_ros_template_architecture(template)
    mermaid = result.mermaid_source

    assert (
        'NasFileSystem["NAS File System\\n+ NAS mount target\\n+ NAS permission group\\n+ NAS permission rule"]'
        in mermaid
    )
    assert 'NasMountTarget["NAS Mount Target"]' not in mermaid
    assert 'NasAccessGroup["NAS Permission Group"]' not in mermaid
    assert 'NasAccessRule["NAS Permission Group Rule"]' not in mermaid
    assert {
        "source": "EcsGroup",
        "target": "NasMountTarget",
        "property": "UserData",
        "source_type": "ALIYUN::ECS::InstanceGroup",
        "target_type": "ALIYUN::NAS::MountTarget",
        "target_visible": False,
    } in result.architecture_context["all_property_references"]


def test_compact_graph_hides_cloud_assistant_nodes_but_keeps_orchestration_context():
    fillers = []
    for index in range(1, 18):
        fillers.append(
            f"""\
  Bucket{index}:
    Type: ALIYUN::OSS::Bucket
"""
        )
    template = """\
ROSTemplateFormatVersion: '2015-09-01'
Resources:
  VPC:
    Type: ALIYUN::ECS::VPC
  VSwitch:
    Type: ALIYUN::ECS::VSwitch
    Properties:
      VpcId:
        Ref: VPC
  SecurityGroup:
    Type: ALIYUN::ECS::SecurityGroup
    Properties:
      VpcId:
        Ref: VPC
  EcsGroup:
    Type: ALIYUN::ECS::InstanceGroup
    Properties:
      VSwitchId:
        Ref: VSwitch
      SecurityGroupId:
        Ref: SecurityGroup
  NasFileSystem:
    Type: ALIYUN::NAS::FileSystem
  NasMountTarget:
    Type: ALIYUN::NAS::MountTarget
    Properties:
      VpcId:
        Ref: VPC
      VSwitchId:
        Ref: VSwitch
      FileSystemId:
        Ref: NasFileSystem
  EcsCommand:
    Type: ALIYUN::ECS::Command
    Properties:
      CommandContent:
        Fn::Base64Encode:
          Fn::Join:
          - ''
          - - 'mount -t nfs '
            - Fn::GetAtt:
              - NasMountTarget
              - MountTargetDomain
            - ':/ /mnt'
      Type: RunShellScript
  EcsInvocation:
    Type: ALIYUN::ECS::Invocation
    Properties:
      InstanceIds:
        Fn::GetAtt:
        - EcsGroup
        - InstanceIds
      CommandId:
        Ref: EcsCommand
""" + "".join(fillers)

    result = render_ros_template_architecture(template)
    mermaid = result.mermaid_source

    assert 'EcsCommand["Cloud Assistant Command"]' not in mermaid
    assert 'EcsInvocation["Cloud Assistant Invocation"]' not in mermaid
    assert 'EcsGroup["ECS instance group\\n+ Security group"]' in mermaid
    assert 'NasFileSystem["NAS File System\\n+ NAS mount target"]' in mermaid
    assert result.architecture_context["orchestration_actions"] == [
        {
            "id": "EcsInvocation",
            "type": "ALIYUN::ECS::Invocation",
            "command": "EcsCommand",
            "targets": [
                {
                    "id": "EcsGroup",
                    "type": "ALIYUN::ECS::InstanceGroup",
                    "property": "InstanceIds",
                    "visible": True,
                }
            ],
            "referenced_resources": [
                {
                    "id": "NasMountTarget",
                    "type": "ALIYUN::NAS::MountTarget",
                    "property": "CommandContent",
                    "visible": False,
                }
            ],
        }
    ]


def test_run_command_extracts_fn_sub_references_from_inline_command_content():
    template = """\
ROSTemplateFormatVersion: '2015-09-01'
Resources:
  VPC:
    Type: ALIYUN::ECS::VPC
  VSwitch:
    Type: ALIYUN::ECS::VSwitch
    Properties:
      VpcId:
        Ref: VPC
  SecurityGroup:
    Type: ALIYUN::ECS::SecurityGroup
    Properties:
      VpcId:
        Ref: VPC
  AppGroup1:
    Type: ALIYUN::ECS::InstanceGroup
    Properties:
      VSwitchId:
        Ref: VSwitch
      SecurityGroupId:
        Ref: SecurityGroup
  AppGroup2:
    Type: ALIYUN::ECS::InstanceGroup
    Properties:
      VSwitchId:
        Ref: VSwitch
      SecurityGroupId:
        Ref: SecurityGroup
  RedisInstance:
    Type: ALIYUN::REDIS::Instance
    Properties:
      VpcId:
        Ref: VPC
      VSwitchId:
        Ref: VSwitch
  PolarDBCluster:
    Type: ALIYUN::POLARDB::DBCluster
    Properties:
      VpcId:
        Ref: VPC
      VSwitchId:
        Ref: VSwitch
  InstanceRunCommand:
    Type: ALIYUN::ECS::RunCommand
    Properties:
      InstanceIds:
      - Fn::GetAtt:
        - AppGroup1
        - InstanceIds
      - Fn::GetAtt:
        - AppGroup2
        - InstanceIds
      Type: RunShellScript
      CommandContent:
        Fn::Sub: |-
          export REDIS_HOST="${RedisInstance.ConnectionDomain}"
          export DB_URL="${PolarDBCluster.PrimaryConnectionString}:3306/app"
"""

    result = render_ros_template_architecture(template)

    assert result.architecture_context["orchestration_actions"] == [
        {
            "id": "InstanceRunCommand",
            "type": "ALIYUN::ECS::RunCommand",
            "command": None,
            "targets": [
                {
                    "id": "AppGroup1",
                    "type": "ALIYUN::ECS::InstanceGroup",
                    "property": "InstanceIds",
                    "visible": True,
                },
                {
                    "id": "AppGroup2",
                    "type": "ALIYUN::ECS::InstanceGroup",
                    "property": "InstanceIds",
                    "visible": True,
                },
            ],
            "referenced_resources": [
                {
                    "id": "RedisInstance",
                    "type": "ALIYUN::REDIS::Instance",
                    "property": "CommandContent",
                    "visible": True,
                },
                {
                    "id": "PolarDBCluster",
                    "type": "ALIYUN::POLARDB::DBCluster",
                    "property": "CommandContent",
                    "visible": True,
                },
            ],
        }
    ]
