"""Metadata-backed ROS template to Mermaid architecture graph rendering."""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, replace
from typing import Any, cast

import yaml

from iac_code.i18n import _, get_current_language
from iac_code.pipeline.engine.architecture_meta import ArchitectureMetaRepository, RelatedProperty, ResourceMeta
from iac_code.pipeline.engine.architecture_rules import (
    ArchitectureRules,
    CompactAttachmentEdge,
    CompactBridgeAttachment,
    CompactConceptGroup,
    CompactOrchestrationAction,
    ConceptEdgeRule,
    RuleLabel,
)

COMPACT_MIN_VISIBLE_NODES = 25
COMPACT_MIN_VISIBLE_ELEMENTS = 25
COMPACT_MIN_EDGES = 25
COMPACT_MIN_ROOT_NODES = 8
COMPACT_MIN_AGGREGATE_COUNT = 3
COMPACT_MIN_FOLDABLE_NODES = 3
COMPACT_MIN_VISIBLE_ELEMENTS_FOR_SINGLE_FOLDABLE = 8
SEMANTIC_FANOUT_COMPACT_THRESHOLD = 2
LAYER_ATTACHMENT_SUMMARY_TYPE = "CONCEPT::Layer::AttachmentSummary"
VIEW_SUMMARY_GROUP_TYPE = "CONCEPT::View::SummaryGroup"
SEMANTIC_EDGE_KINDS = ("traffic", "dependency", "management", "inferred")
RUNTIME_SEMANTIC_EDGE_KINDS = {"traffic", "dependency", "inferred"}
SEMANTIC_EDGE_STYLES = {
    "traffic": "solid_arrow",
    "dependency": "solid_arrow",
    "management": "dotted_arrow",
    "inferred": "dotted_open",
}
ACK_CLUSTER_APPLICATION_TYPE = "ALIYUN::CS::ClusterApplication"
ACK_CLUSTER_HELM_APPLICATION_TYPE = "ALIYUN::CS::ClusterHelmApplication"
ACK_HELM_APPLICATION_TYPES = frozenset(
    {
        ACK_CLUSTER_HELM_APPLICATION_TYPE,
        "MODULE::ACS::ComputeNest::FluxOciHelmDeploy",
    }
)
SEMANTIC_COMPUTE_AGGREGATE_TYPES = frozenset(
    {
        "ALIYUN::ECS::Instance",
        "ALIYUN::ECS::InstanceGroup",
    }
)
SEMANTIC_COMPUTE_ROLE_TOKENS = (
    "mongodb",
    "rabbitmq",
    "redis",
    "mysql",
    "postgresql",
    "postgres",
    "zookeeper",
    "kafka",
    "nacos",
    "elasticsearch",
    "logstash",
    "kibana",
    "consul",
    "etcd",
    "jenkins",
    "gitlab",
)
KUBERNETES_KIND_ATTACHMENT_LABELS: dict[str, RuleLabel] = {
    "AlbConfig": {"en": "ALB ingress config", "zh": "ALB入口配置"},
    "ConfigMap": {"en": "app config", "zh": "应用配置"},
    "CronJob": {"en": "application workload", "zh": "应用工作负载"},
    "DaemonSet": {"en": "application workload", "zh": "应用工作负载"},
    "Deployment": {"en": "application workload", "zh": "应用工作负载"},
    "HorizontalPodAutoscaler": {"en": "HPA autoscaling", "zh": "HPA弹性伸缩"},
    "Ingress": {"en": "Ingress entry", "zh": "Ingress入口"},
    "IngressClass": {"en": "Ingress class", "zh": "IngressClass"},
    "Job": {"en": "application workload", "zh": "应用工作负载"},
    "Namespace": {"en": "namespace", "zh": "命名空间"},
    "PersistentVolumeClaim": {"en": "storage claim", "zh": "存储声明"},
    "Role": {"en": "RBAC permission", "zh": "RBAC权限"},
    "RoleBinding": {"en": "RBAC permission", "zh": "RBAC权限"},
    "Secret": {"en": "app secret", "zh": "应用密钥"},
    "Service": {"en": "service exposure", "zh": "服务暴露"},
    "ServiceAccount": {"en": "service account", "zh": "服务账号"},
    "StatefulSet": {"en": "stateful workload", "zh": "有状态工作负载"},
}
ACK_APPLICATION_CONCEPT_LABELS: dict[str, RuleLabel] = {
    "workload": {"en": "ACK application workload", "zh": "ACK应用工作负载"},
    "service": {"en": "ACK service exposure", "zh": "ACK服务暴露"},
    "ingress": {"en": "ACK ingress routing", "zh": "ACK入口路由"},
    "autoscaler": {"en": "ACK HPA autoscaling", "zh": "ACK HPA弹性伸缩"},
}
ACK_APPLICATION_CONCEPT_EDGE_LABELS: dict[str, RuleLabel] = {
    "ingress_to_service": {"en": "ingress route", "zh": "入口路由"},
    "service_to_workload": {"en": "service forwarding", "zh": "服务转发"},
    "autoscaler_to_workload": {"en": "autoscaling", "zh": "弹性伸缩"},
    "metrics_adapter": {"en": "metrics adapter", "zh": "指标适配"},
    "external_metrics": {"en": "external metrics", "zh": "外部指标"},
    "database_access": {"en": "database access", "zh": "数据库访问"},
    "cache_access": {"en": "cache access", "zh": "缓存访问"},
    "vector_search": {"en": "vector search", "zh": "向量检索"},
    "data_dependency": {"en": "data dependency", "zh": "数据依赖"},
}
ACK_APPLICATION_CONCEPT_TYPES = {
    "workload": "CONCEPT::ACK::ApplicationWorkload",
    "service": "CONCEPT::ACK::ServiceExposure",
    "ingress": "CONCEPT::ACK::IngressEntry",
    "autoscaler": "CONCEPT::ACK::HpaAutoscaling",
}
ACK_APPLICATION_CONCEPT_SUFFIXES = {
    "workload": "ApplicationWorkload",
    "service": "ServiceExposure",
    "ingress": "IngressEntry",
    "autoscaler": "HpaAutoscaling",
}
ACK_WORKLOAD_KINDS = frozenset({"CronJob", "DaemonSet", "Deployment", "Job", "StatefulSet"})
ACK_SERVICE_KINDS = frozenset({"Service"})
ACK_INGRESS_KINDS = frozenset({"AlbConfig", "Ingress", "IngressClass"})
ACK_AUTOSCALER_KINDS = frozenset({"HorizontalPodAutoscaler"})
POLARDB_MIGRATION_TARGET_TYPE = "ALIYUN::POLARDB::DBCluster"
POLARDB_MIGRATION_SOURCE_TYPES = frozenset(
    {
        "ALIYUN::RDS::DBInstance",
        "ALIYUN::RDS::PrepayDBInstance",
    }
)
POLARDB_MIGRATION_CREATION_OPTIONS = frozenset({"MigrationFromRDS"})
POLARDB_MIGRATION_EDGE_LABELS: dict[str, RuleLabel] = {
    "migrate_to": {"en": "migrate to", "zh": "迁移到"},
}


def _legacy_rule_label_i18n_markers() -> tuple[str, ...]:
    """Keep dynamic architecture rule labels visible to Babel extraction."""
    return (
        _("ECS instance"),
        _("ECS instance group"),
        _("Elastic IP address"),
        _("SLB load balancer"),
        _("NAT gateway"),
        _("Shared bandwidth package"),
        _("Database instance"),
        _("VPC"),
        _("VSwitch"),
        _("Security group"),
    )


@dataclass
class TemplateResource:
    logical_id: str
    resource_type: str
    properties: dict[str, Any]
    meta: ResourceMeta | None
    label: str

    @property
    def short_type(self) -> str:
        parts = self.resource_type.split("::")
        return parts[-1] if parts else self.resource_type


@dataclass(frozen=True)
class ResourceRelation:
    source_id: str
    target_id: str
    property_name: str


@dataclass(frozen=True)
class GraphEdge:
    from_id: str
    to_id: str
    style: str
    label: str | None = None


@dataclass(frozen=True)
class ArchitectureRenderResult:
    mermaid_source: str
    architecture_context: dict[str, Any]


@dataclass(frozen=True)
class ArchitectureViewResult:
    id: str
    title: str
    purpose: str
    mermaid_source: str
    architecture_context: dict[str, Any]


@dataclass(frozen=True)
class ArchitectureMultiViewRenderResult:
    views: tuple[ArchitectureViewResult, ...]
    architecture_context: dict[str, Any]


@dataclass(frozen=True)
class ConceptNodeInstance:
    node_id: str
    label: str
    resource_type: str
    controller_id: str
    source_id: str
    via_id: str
    runtime_source_id: str
    group_id: str | None = None


@dataclass(frozen=True)
class ConceptGroupInstance:
    group_id: str
    label: str
    resource_type: str
    member_ids: tuple[str, ...]
    rewrite_edge_kinds: tuple[str, ...]
    parent_id: str | None = None


def render_ros_template_mermaid(
    template_yaml: str,
    *,
    semantic_plan: dict[str, Any] | None = None,
    meta_repository: ArchitectureMetaRepository | None = None,
) -> str:
    return render_ros_template_architecture(
        template_yaml,
        semantic_plan=semantic_plan,
        meta_repository=meta_repository,
    ).mermaid_source


def render_ros_template_architecture(
    template_yaml: str,
    *,
    semantic_plan: dict[str, Any] | None = None,
    meta_repository: ArchitectureMetaRepository | None = None,
) -> ArchitectureRenderResult:
    from iac_code.tools.cloud.aliyun.ros_yaml import ros_yaml_load

    try:
        doc = ros_yaml_load(template_yaml)
    except yaml.YAMLError:
        return ArchitectureRenderResult(
            mermaid_source="graph TD\n  Error[{}]".format(_("YAML parse error")),
            architecture_context=_error_architecture_context("yaml_parse_error"),
        )

    if not isinstance(doc, dict):
        return ArchitectureRenderResult(
            mermaid_source="graph TD",
            architecture_context=_error_architecture_context("template_is_not_mapping"),
        )

    template_summary = _template_summary_context(doc)
    raw_resources = doc.get("Resources") or {}
    if not isinstance(raw_resources, dict) or not raw_resources:
        return ArchitectureRenderResult(
            mermaid_source="graph TD",
            architecture_context=_error_architecture_context("template_has_no_resources"),
        )

    params = doc.get("Parameters") or {}
    params = params if isinstance(params, dict) else {}
    repo = meta_repository or ArchitectureMetaRepository.load_default()
    rules = ArchitectureRules.load_default()
    resources = _build_template_resources(raw_resources, repo, rules)
    if not resources:
        return ArchitectureRenderResult(
            mermaid_source="graph TD",
            architecture_context=_error_architecture_context("template_has_no_supported_resources"),
        )

    original_layers, original_nodes, auxiliary = _classify_resources(resources, rules)
    relations = _extract_relations(resources, rules)
    layers = dict(original_layers)
    nodes = dict(original_nodes)
    edges = _edges_from_auxiliary(auxiliary, relations, rules)
    edges.extend(_dependency_edges(nodes, relations, rules))
    edges.extend(_polardb_migration_edges(nodes, relations))
    layer_parent, node_parent = _build_containment(layers, nodes, relations, rules)
    layers, nodes, edges, node_parent, compacted, semantic_endpoint_aliases = _compact_graph_if_needed(
        layers, nodes, edges, node_parent, auxiliary, relations, rules
    )
    if compacted:
        layers, layer_parent, nodes, node_parent = _flatten_compact_layers(
            layers, layer_parent, nodes, node_parent, relations, rules
        )
    concept_nodes: tuple[ConceptNodeInstance, ...] = ()
    concept_groups: tuple[ConceptGroupInstance, ...] = ()
    if compacted:
        layers, layer_parent, nodes, edges, node_parent, concept_nodes, concept_groups = _add_compact_concept_nodes(
            resources, layers, layer_parent, nodes, edges, node_parent, relations, rules
        )
        nodes, edges, node_parent = _add_ack_application_concept_nodes(resources, nodes, edges, node_parent, relations)

    nodes, node_label_context = _semantic_node_labels_from_plan(semantic_plan, nodes)
    semantic_edges, edge_context = _semantic_edges_from_plan(
        semantic_plan, nodes, rules, edges, concept_nodes, concept_groups, semantic_endpoint_aliases
    )
    semantic_context = {**node_label_context, **edge_context}
    edges = _merge_semantic_edges(edges, semantic_edges)

    architecture_context = _build_architecture_context(
        template_summary=template_summary,
        resources=resources,
        original_layers=original_layers,
        original_nodes=original_nodes,
        auxiliary=auxiliary,
        visible_layers=layers,
        visible_nodes=nodes,
        relations=relations,
        edges=_dedupe_edges(edges),
        layer_parent=layer_parent,
        node_parent=node_parent,
        params=params,
        compacted=compacted,
        semantic_context=semantic_context,
        concept_nodes=concept_nodes,
        concept_groups=concept_groups,
        rules=rules,
    )

    return ArchitectureRenderResult(
        mermaid_source=_render_mermaid(layers, nodes, edges, layer_parent, node_parent, params, rules),
        architecture_context=architecture_context,
    )


def render_ros_template_architecture_views(
    template_yaml: str,
    *,
    semantic_plan: dict[str, Any] | None = None,
    meta_repository: ArchitectureMetaRepository | None = None,
) -> ArchitectureMultiViewRenderResult:
    base_result = render_ros_template_architecture(
        template_yaml,
        semantic_plan=semantic_plan,
        meta_repository=meta_repository,
    )
    views = _render_views_from_context(base_result.architecture_context, base_result.mermaid_source, semantic_plan)
    return ArchitectureMultiViewRenderResult(
        views=views,
        architecture_context={
            **base_result.architecture_context,
            "views": [view.architecture_context for view in views],
        },
    )


def _build_template_resources(
    raw_resources: dict[str, Any],
    repo: ArchitectureMetaRepository,
    rules: ArchitectureRules,
) -> dict[str, TemplateResource]:
    resources: dict[str, TemplateResource] = {}
    for logical_id, raw in raw_resources.items():
        if not isinstance(logical_id, str) or not isinstance(raw, dict):
            continue
        resource_type = raw.get("Type")
        if not isinstance(resource_type, str) or not resource_type:
            continue
        props = raw.get("Properties") if isinstance(raw.get("Properties"), dict) else {}
        meta = repo.get_resource(resource_type)
        resources[logical_id] = TemplateResource(
            logical_id=logical_id,
            resource_type=resource_type,
            properties=props,
            meta=meta,
            label=_resource_label(resource_type, meta, rules),
        )
    _disambiguate_labels(resources)
    return resources


def _template_summary_context(doc: dict[str, Any]) -> dict[str, Any]:
    descriptions = _localized_text_map(doc.get("Description"))
    description = _select_localized_text(descriptions)
    if not description:
        return {}
    return {
        "description": description,
        "descriptions": descriptions,
    }


def _localized_text_map(value: Any) -> dict[str, str]:
    if isinstance(value, str):
        text = _compact_template_text(value)
        return {"default": text} if text else {}
    if not isinstance(value, dict):
        return {}
    result: dict[str, str] = {}
    for raw_key, raw_text in value.items():
        if not isinstance(raw_key, str) or not isinstance(raw_text, str):
            continue
        text = _compact_template_text(raw_text)
        if text:
            result[raw_key] = text
    return result


def _select_localized_text(values: dict[str, str]) -> str | None:
    if not values:
        return None
    language = get_current_language()
    preferred_keys = {
        "zh": ("zh-cn", "zh_CN", "zh", "cn", "default", "en"),
        "en": ("en", "en-us", "en_US", "default", "zh-cn", "zh"),
    }.get(language, (language, "default", "en", "zh-cn", "zh"))
    normalized_values = {key.lower().replace("_", "-"): value for key, value in values.items()}
    for key in preferred_keys:
        value = values.get(key) or normalized_values.get(key.lower().replace("_", "-"))
        if value:
            return value
    return next(iter(values.values()))


def _compact_template_text(value: str, max_chars: int = 1200) -> str:
    text = re.sub(r"\s+", " ", value).strip()
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "…"


def _resource_label(resource_type: str, meta: ResourceMeta | None, rules: ArchitectureRules) -> str:
    legacy_label = rules.legacy_resource_labels.get(resource_type)
    if legacy_label is not None:
        return _(legacy_label)
    meta_label = _localized_meta_label(meta)
    if meta_label is not None:
        return meta_label
    parts = resource_type.split("::")
    if len(parts) >= 3:
        return f"{parts[1]}::{parts[-1]}"
    return resource_type


def _localized_meta_label(meta: ResourceMeta | None) -> str | None:
    if meta is None:
        return None
    language = get_current_language()
    if language == "zh" and meta.name_zh:
        return meta.name_zh
    if meta.name_en:
        return meta.name_en
    return meta.name_zh


def _localized_rule_label(value: RuleLabel | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return _(value)
    language = get_current_language()
    return value.get(language) or value.get("en") or next(iter(value.values()), None)


def _disambiguate_labels(resources: dict[str, TemplateResource]) -> None:
    label_counts = Counter(resource.label for resource in resources.values())
    label_seq: Counter[str] = Counter()
    for resource in resources.values():
        if label_counts[resource.label] > 1:
            label_seq[resource.label] += 1
            resource.label = f"{resource.label} {label_seq[resource.label]}"


def _classify_resources(
    resources: dict[str, TemplateResource],
    rules: ArchitectureRules,
) -> tuple[dict[str, TemplateResource], dict[str, TemplateResource], dict[str, TemplateResource]]:
    layers: dict[str, TemplateResource] = {}
    nodes: dict[str, TemplateResource] = {}
    auxiliary: dict[str, TemplateResource] = {}
    layer_types = rules.network_layer_types | _all_containment_layer_types(rules)
    for logical_id, resource in resources.items():
        if resource.resource_type in layer_types:
            layers[logical_id] = resource
        elif _is_auxiliary(resource, rules):
            auxiliary[logical_id] = resource
        else:
            nodes[logical_id] = resource
    return layers, nodes, auxiliary


def _is_auxiliary(resource: TemplateResource, rules: ArchitectureRules) -> bool:
    if resource.meta is not None and resource.meta.main_resource_type is not None:
        return True
    return resource.short_type in rules.legacy_auxiliary_short_types


def _all_containment_layer_types(rules: ArchitectureRules) -> frozenset[str]:
    return frozenset(
        resource_type for resource_types in rules.containment_layer_types.values() for resource_type in resource_types
    )


def _extract_relations(resources: dict[str, TemplateResource], rules: ArchitectureRules) -> list[ResourceRelation]:
    relations: list[ResourceRelation] = []
    for source in resources.values():
        related_properties = list(source.meta.related_properties) if source.meta is not None else []
        related_properties.extend(_fallback_related_properties(source, rules))
        related_properties.extend(_supplemental_related_properties(source, rules))
        related_properties = _dedupe_related_properties(related_properties)
        for prop in related_properties:
            values = _values_at_path(source.properties, prop.path)
            if not values and len(prop.path) > 1 and prop.path[0] in source.properties:
                values = [source.properties[prop.path[0]]]
            for value in values:
                for target_id in _extract_resource_refs(value, resources):
                    target = resources.get(target_id)
                    if target is None or (prop.targets and target.resource_type not in prop.targets):
                        continue
                    relations.append(
                        ResourceRelation(source_id=source.logical_id, target_id=target_id, property_name=prop.name)
                    )
    return relations


def _fallback_related_properties(source: TemplateResource, rules: ArchitectureRules) -> list[RelatedProperty]:
    if source.meta is not None:
        known_names = {prop.name for prop in source.meta.related_properties}
    else:
        known_names = set()
    return [
        RelatedProperty(name=name, path=(name,), targets=targets)
        for name, targets in rules.fallback_related_properties.items()
        if name in source.properties and name not in known_names
    ]


def _supplemental_related_properties(source: TemplateResource, rules: ArchitectureRules) -> list[RelatedProperty]:
    supplemental = rules.supplemental_related_properties.get(source.resource_type, {})
    properties: list[RelatedProperty] = []
    for name, targets in supplemental.items():
        path = tuple(part for part in name.split(".") if part)
        if not path or path[0] not in source.properties:
            continue
        properties.append(RelatedProperty(name=path[-1], path=path, targets=targets))
    return properties


def _dedupe_related_properties(related_properties: list[RelatedProperty]) -> list[RelatedProperty]:
    deduped: list[RelatedProperty] = []
    seen: set[tuple[str, tuple[str, ...], tuple[str, ...]]] = set()
    for prop in related_properties:
        key = (prop.name, prop.path, prop.targets)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(prop)
    return deduped


def _values_at_path(value: Any, path: tuple[str, ...]) -> list[Any]:
    if not path:
        return [value]
    if isinstance(value, list):
        values: list[Any] = []
        for item in value:
            values.extend(_values_at_path(item, path))
        return values
    if not isinstance(value, dict):
        return []
    key = path[0]
    if key not in value:
        return []
    return _values_at_path(value[key], path[1:])


def _extract_resource_refs(value: Any, resources: dict[str, TemplateResource]) -> list[str]:
    refs: list[str] = []
    if isinstance(value, dict):
        ref = value.get("Ref")
        if isinstance(ref, str) and ref in resources:
            refs.append(ref)
        get_att = value.get("Fn::GetAtt")
        if isinstance(get_att, list) and get_att and isinstance(get_att[0], str) and get_att[0] in resources:
            refs.append(get_att[0])
        elif isinstance(get_att, str):
            logical_id = get_att.split(".", 1)[0]
            if logical_id in resources:
                refs.append(logical_id)
        for child in value.values():
            refs.extend(_extract_resource_refs(child, resources))
    elif isinstance(value, list):
        for item in value:
            refs.extend(_extract_resource_refs(item, resources))
    elif isinstance(value, str):
        refs.extend(_extract_template_refs_from_text(value, resources))
    return _dedupe(refs)


def _edges_from_auxiliary(
    auxiliary: dict[str, TemplateResource],
    relations: list[ResourceRelation],
    rules: ArchitectureRules,
) -> list[GraphEdge]:
    edges: list[GraphEdge] = []
    by_source = _relations_by_source(relations)
    for resource in auxiliary.values():
        main_ref = _main_resource_ref(resource, by_source.get(resource.logical_id, []))
        if main_ref is None:
            continue
        for relation in by_source.get(resource.logical_id, []):
            if relation.target_id != main_ref:
                edges.append(GraphEdge(main_ref, relation.target_id, rules.edge_styles.auxiliary_relation))
    return _dedupe_edges(edges)


def _main_resource_ref(resource: TemplateResource, relations: list[ResourceRelation]) -> str | None:
    if resource.meta is None or resource.meta.main_resource_type is None:
        return None
    main = resource.meta.main_resource_type
    for relation in relations:
        if relation.property_name == main.ref_property:
            return relation.target_id
    return None


def _dependency_edges(
    nodes: dict[str, TemplateResource],
    relations: list[ResourceRelation],
    rules: ArchitectureRules,
) -> list[GraphEdge]:
    node_ids = set(nodes)
    edges: list[GraphEdge] = []
    for relation in relations:
        if relation.source_id in node_ids and relation.target_id in node_ids:
            edges.append(GraphEdge(relation.target_id, relation.source_id, rules.edge_styles.direct_relation))
    return _dedupe_edges(edges)


def _polardb_migration_edges(
    nodes: dict[str, TemplateResource],
    relations: list[ResourceRelation],
) -> list[GraphEdge]:
    by_source = _relations_by_source(relations)
    edges: list[GraphEdge] = []
    for target in nodes.values():
        if target.resource_type != POLARDB_MIGRATION_TARGET_TYPE:
            continue
        creation_option = target.properties.get("CreationOption")
        if not isinstance(creation_option, str) or creation_option not in POLARDB_MIGRATION_CREATION_OPTIONS:
            continue
        for relation in by_source.get(target.logical_id, []):
            if relation.property_name != "SourceResourceId":
                continue
            source = nodes.get(relation.target_id)
            if source is None or source.resource_type not in POLARDB_MIGRATION_SOURCE_TYPES:
                continue
            edges.append(
                GraphEdge(
                    from_id=source.logical_id,
                    to_id=target.logical_id,
                    style="solid_arrow",
                    label=_localized_rule_label(POLARDB_MIGRATION_EDGE_LABELS["migrate_to"]),
                )
            )
    return _dedupe_edges(edges)


def _semantic_edges_from_plan(
    semantic_plan: dict[str, Any] | None,
    nodes: dict[str, TemplateResource],
    rules: ArchitectureRules,
    deterministic_edges: list[GraphEdge] | None = None,
    concept_nodes: tuple[ConceptNodeInstance, ...] = (),
    concept_groups: tuple[ConceptGroupInstance, ...] = (),
    endpoint_aliases: dict[str, str] | None = None,
) -> tuple[list[GraphEdge], dict[str, Any]]:
    accepted: list[dict[str, str]] = []
    rejected: list[dict[str, str]] = []
    edges: list[GraphEdge] = []
    raw_edges = semantic_plan.get("edges") if isinstance(semantic_plan, dict) else []
    if not isinstance(raw_edges, list):
        raw_edges = []
    scaled_runtime_pairs = _scaled_runtime_concept_pairs(raw_edges, concept_nodes)
    allowed_endpoint_ids = set(nodes) | {group.group_id for group in concept_groups}

    for raw_edge in raw_edges:
        if not isinstance(raw_edge, dict):
            continue
        from_id = _semantic_edge_endpoint(raw_edge, "from")
        to_id = _semantic_edge_endpoint(raw_edge, "to")
        if from_id is None or to_id is None:
            rejected.append({"from": from_id or "", "to": to_id or "", "reason": "missing endpoint"})
            continue
        kind = raw_edge.get("kind")
        if not isinstance(kind, str) or kind not in SEMANTIC_EDGE_STYLES:
            kind = "inferred"
        from_id = (endpoint_aliases or {}).get(from_id, from_id)
        to_id = (endpoint_aliases or {}).get(to_id, to_id)
        if from_id == to_id:
            rejected.append({"from": from_id, "to": to_id, "reason": "covered by folded resource"})
            continue
        if _is_covered_by_concept_edge(from_id, to_id, concept_nodes):
            rejected.append({"from": from_id, "to": to_id, "reason": "covered by scaled concept"})
            continue
        scaled_cover_pair = _scaled_runtime_source_pair(
            from_id, to_id, kind, concept_nodes
        ) or _scaled_runtime_controller_peer_pair(from_id, to_id, kind, concept_nodes)
        if scaled_cover_pair in scaled_runtime_pairs:
            rejected.append({"from": from_id, "to": to_id, "reason": "covered by scaled runtime edge"})
            continue
        from_id, to_id = _rewrite_semantic_concept_endpoints(from_id, to_id, kind, concept_nodes)
        from_id, to_id = _rewrite_semantic_group_endpoints(from_id, to_id, kind, concept_groups)
        if _is_covered_by_deterministic_edge(from_id, to_id, deterministic_edges or []):
            rejected.append({"from": from_id, "to": to_id, "reason": "covered by deterministic edge"})
            continue
        if from_id not in allowed_endpoint_ids or to_id not in allowed_endpoint_ids:
            rejected.append({"from": from_id, "to": to_id, "reason": "unknown node"})
            continue
        label = _semantic_edge_label(raw_edge)
        confidence = raw_edge.get("confidence")
        if not isinstance(confidence, str) or confidence not in {"high", "medium", "low"}:
            confidence = "medium"
        edges.append(
            GraphEdge(
                from_id=from_id,
                to_id=to_id,
                style=SEMANTIC_EDGE_STYLES.get(kind, rules.edge_styles.direct_relation),
                label=label,
            )
        )
        accepted.append(
            {
                "from": from_id,
                "to": to_id,
                "kind": kind,
                "label": label or "",
                "confidence": confidence,
            }
        )

    return _dedupe_edges(edges), {"accepted_edges": accepted, "rejected_edges": rejected}


def _semantic_node_labels_from_plan(
    semantic_plan: dict[str, Any] | None,
    nodes: dict[str, TemplateResource],
) -> tuple[dict[str, TemplateResource], dict[str, Any]]:
    accepted: list[dict[str, str]] = []
    rejected: list[dict[str, str]] = []
    updated_nodes = dict(nodes)
    raw_labels = semantic_plan.get("node_labels") if isinstance(semantic_plan, dict) else []
    if not isinstance(raw_labels, list):
        raw_labels = []

    for raw_label in raw_labels:
        if not isinstance(raw_label, dict):
            continue
        node_id = _semantic_node_label_id(raw_label)
        if node_id is None:
            rejected.append({"id": "", "reason": "missing node id"})
            continue
        if node_id not in updated_nodes:
            rejected.append({"id": node_id, "reason": "unknown node"})
            continue
        label = _semantic_node_label(raw_label)
        if label is None:
            rejected.append({"id": node_id, "reason": "missing label"})
            continue
        confidence = raw_label.get("confidence")
        if not isinstance(confidence, str) or confidence not in {"high", "medium", "low"}:
            confidence = "medium"
        reason = raw_label.get("reason")
        accepted_label = {
            "id": node_id,
            "label": label,
            "confidence": confidence,
        }
        if isinstance(reason, str) and reason.strip():
            accepted_label["reason"] = " ".join(reason.strip().split())[:80]
        accepted.append(accepted_label)
        updated_nodes[node_id] = replace(
            updated_nodes[node_id],
            label=_replace_base_label(updated_nodes[node_id].label, label),
        )

    return updated_nodes, {"accepted_node_labels": accepted, "rejected_node_labels": rejected}


def _semantic_node_label_id(raw_label: dict[str, Any]) -> str | None:
    value = raw_label.get("id")
    if not isinstance(value, str) or not value.strip():
        value = raw_label.get("node_id")
    return value.strip() if isinstance(value, str) and value.strip() else None


def _semantic_node_label(raw_label: dict[str, Any]) -> str | None:
    value = raw_label.get("label")
    if not isinstance(value, str):
        return None
    label = " ".join(value.replace("\n", " ").strip().split())
    if not label:
        return None
    return label[:32]


def _replace_base_label(current_label: str, base_label: str) -> str:
    parts = current_label.split("\\n")
    return "\\n".join([base_label, *parts[1:]])


def _is_covered_by_concept_edge(
    from_id: str,
    to_id: str,
    concept_nodes: tuple[ConceptNodeInstance, ...],
) -> bool:
    return any({from_id, to_id} == {concept.controller_id, concept.source_id} for concept in concept_nodes)


def _is_covered_by_deterministic_edge(
    from_id: str,
    to_id: str,
    deterministic_edges: list[GraphEdge],
) -> bool:
    for edge in deterministic_edges:
        if edge.label is None:
            continue
        if (edge.from_id, edge.to_id) == (from_id, to_id):
            return True
        if _is_reverse_of_directional_deterministic_edge(edge, from_id, to_id):
            return True
        if edge.style == "dotted_open" and {edge.from_id, edge.to_id} == {from_id, to_id}:
            return True
    return False


def _is_reverse_of_directional_deterministic_edge(edge: GraphEdge, from_id: str, to_id: str) -> bool:
    return (
        (edge.from_id, edge.to_id) == (to_id, from_id)
        and edge.label in {"migrate to", "迁移到"}
        and edge.style == "solid_arrow"
    )


def _scaled_runtime_concept_pairs(
    raw_edges: list[Any],
    concept_nodes: tuple[ConceptNodeInstance, ...],
) -> set[frozenset[str]]:
    pairs: set[frozenset[str]] = set()
    for raw_edge in raw_edges:
        if not isinstance(raw_edge, dict):
            continue
        kind = raw_edge.get("kind")
        if not isinstance(kind, str) or kind not in RUNTIME_SEMANTIC_EDGE_KINDS:
            continue
        from_id = _semantic_edge_endpoint(raw_edge, "from")
        to_id = _semantic_edge_endpoint(raw_edge, "to")
        if from_id is None or to_id is None:
            continue
        from_id, to_id = _rewrite_semantic_concept_endpoints(from_id, to_id, kind, concept_nodes)
        for concept in concept_nodes:
            if from_id == concept.node_id and to_id != concept.runtime_source_id:
                pairs.add(frozenset({from_id, to_id}))
            if to_id == concept.node_id and from_id != concept.runtime_source_id:
                pairs.add(frozenset({from_id, to_id}))
    return pairs


def _scaled_runtime_source_pair(
    from_id: str,
    to_id: str,
    kind: str,
    concept_nodes: tuple[ConceptNodeInstance, ...],
) -> frozenset[str] | None:
    if kind not in RUNTIME_SEMANTIC_EDGE_KINDS:
        return None
    for concept in concept_nodes:
        if from_id == concept.runtime_source_id and to_id != concept.node_id:
            return frozenset({concept.node_id, to_id})
        if to_id == concept.runtime_source_id and from_id != concept.node_id:
            return frozenset({from_id, concept.node_id})
    return None


def _scaled_runtime_controller_peer_pair(
    from_id: str,
    to_id: str,
    kind: str,
    concept_nodes: tuple[ConceptNodeInstance, ...],
) -> frozenset[str] | None:
    if kind in RUNTIME_SEMANTIC_EDGE_KINDS:
        return None
    for concept in concept_nodes:
        if from_id == concept.controller_id and to_id not in {concept.node_id, concept.source_id}:
            return frozenset({concept.node_id, to_id})
        if to_id == concept.controller_id and from_id not in {concept.node_id, concept.source_id}:
            return frozenset({from_id, concept.node_id})
    return None


def _rewrite_semantic_concept_endpoints(
    from_id: str,
    to_id: str,
    kind: str,
    concept_nodes: tuple[ConceptNodeInstance, ...],
) -> tuple[str, str]:
    if kind not in RUNTIME_SEMANTIC_EDGE_KINDS:
        return from_id, to_id
    controller_to_concept = {concept.controller_id: concept.node_id for concept in concept_nodes}
    if from_id in controller_to_concept and to_id != controller_to_concept[from_id]:
        from_id = controller_to_concept[from_id]
    if to_id in controller_to_concept and from_id != controller_to_concept[to_id]:
        to_id = controller_to_concept[to_id]
    return from_id, to_id


def _rewrite_semantic_group_endpoints(
    from_id: str,
    to_id: str,
    kind: str,
    concept_groups: tuple[ConceptGroupInstance, ...],
) -> tuple[str, str]:
    for group in concept_groups:
        if kind not in group.rewrite_edge_kinds:
            continue
        member_ids = set(group.member_ids)
        from_is_member = from_id in member_ids
        to_is_member = to_id in member_ids
        if from_is_member and not to_is_member:
            from_id = group.group_id
        if to_is_member and not from_is_member:
            to_id = group.group_id
    return from_id, to_id


def _semantic_edge_endpoint(raw_edge: dict[str, Any], key: str) -> str | None:
    value = raw_edge.get(key)
    if not isinstance(value, str) or not value.strip():
        value = raw_edge.get(f"{key}_id")
    return value.strip() if isinstance(value, str) and value.strip() else None


def _semantic_edge_label(raw_edge: dict[str, Any]) -> str | None:
    value = raw_edge.get("label")
    if not isinstance(value, str):
        return None
    label = _normalize_semantic_edge_label(value)
    if not label:
        return None
    return label[:18]


def _normalize_semantic_edge_label(value: str) -> str:
    value = re.sub(r"(?i)<br\s*/?>", "\n", value.strip()).replace("\\n", "\n")
    parts = [" ".join(part.strip().split()) for part in value.splitlines()]
    parts = [part for part in parts if part]
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]
    return "{}（{}）".format(parts[0], " / ".join(parts[1:]))


def _merge_semantic_edges(base_edges: list[GraphEdge], semantic_edges: list[GraphEdge]) -> list[GraphEdge]:
    semantic_pairs = {(edge.from_id, edge.to_id) for edge in semantic_edges}
    semantic_unordered_pairs = {frozenset((edge.from_id, edge.to_id)) for edge in semantic_edges}
    merged = [
        edge
        for edge in base_edges
        if (edge.from_id, edge.to_id) not in semantic_pairs
        and not _is_unlabeled_relation_covered_by_semantic_edge(edge, semantic_unordered_pairs)
    ]
    merged.extend(semantic_edges)
    return _dedupe_edges(merged)


def _is_unlabeled_relation_covered_by_semantic_edge(
    edge: GraphEdge,
    semantic_unordered_pairs: set[frozenset[str]],
) -> bool:
    return (
        edge.label is None
        and edge.style == "dotted_open"
        and frozenset((edge.from_id, edge.to_id)) in semantic_unordered_pairs
    )


def _compact_semantic_fanout_edges(
    nodes: dict[str, TemplateResource],
    semantic_edges: list[GraphEdge],
    rules: ArchitectureRules,
) -> tuple[dict[str, TemplateResource], list[GraphEdge], list[dict[str, Any]]]:
    grouped_edges: dict[tuple[str, str, str, str], list[GraphEdge]] = {}
    for edge in semantic_edges:
        target = nodes.get(edge.to_id)
        if edge.from_id not in nodes or target is None:
            continue
        key = (edge.from_id, edge.style, edge.label or "", target.resource_type)
        grouped_edges.setdefault(key, []).append(edge)

    compacted_edges: set[GraphEdge] = set()
    attachment_labels: dict[str, list[str]] = {}
    context: list[dict[str, Any]] = []
    for (source_id, style, label, target_type), group in grouped_edges.items():
        target_ids = tuple(_dedupe([edge.to_id for edge in group]))
        if len(target_ids) < SEMANTIC_FANOUT_COMPACT_THRESHOLD:
            continue
        compacted_edges.update(group)
        attachment_label = _semantic_fanout_attachment_label(label, target_ids, nodes, rules)
        attachment_labels.setdefault(source_id, []).append(attachment_label)
        context.append(
            {
                "from": source_id,
                "targets": list(target_ids),
                "style": style,
                "label": label,
                "target_type": target_type,
                "rendered_as": "source_attachment",
                "attachment_label": f"{rules.attachment_label_prefix}{attachment_label}",
                "reason": "same source fan-out compacted for terminal readability",
            }
        )

    if not compacted_edges:
        return nodes, semantic_edges, []

    remaining_edges = [edge for edge in semantic_edges if edge not in compacted_edges]
    updated_nodes = _with_attachment_labels(
        nodes,
        {node_id: tuple(labels) for node_id, labels in attachment_labels.items()},
        rules,
    )
    return updated_nodes, remaining_edges, context


def _semantic_fanout_attachment_label(
    label: str,
    target_ids: tuple[str, ...],
    nodes: dict[str, TemplateResource],
    rules: ArchitectureRules,
) -> str:
    base_label = label or _semantic_fanout_target_label(target_ids, nodes, rules)
    return f"{base_label} x{len(target_ids)}"


def _semantic_fanout_target_label(
    target_ids: tuple[str, ...],
    nodes: dict[str, TemplateResource],
    rules: ArchitectureRules,
) -> str:
    target_labels = [_semantic_base_label(nodes[target_id].label) for target_id in target_ids if target_id in nodes]
    normalized_labels = {_strip_trailing_sequence(label) for label in target_labels if label}
    if len(normalized_labels) == 1:
        return next(iter(normalized_labels))
    for target_id in target_ids:
        target = nodes.get(target_id)
        if target is not None:
            return _compact_resource_label(target, rules)
    return _("related resource")


def _semantic_base_label(label: str) -> str:
    return label.split("\\n", 1)[0].strip()


def _strip_trailing_sequence(label: str) -> str:
    stripped = re.sub(r"[\s_-]+(?:x\s*)?\d+$", "", label.strip(), flags=re.I).strip()
    return stripped or label.strip()


def _build_architecture_context(
    *,
    template_summary: dict[str, Any],
    resources: dict[str, TemplateResource],
    original_layers: dict[str, TemplateResource],
    original_nodes: dict[str, TemplateResource],
    auxiliary: dict[str, TemplateResource],
    visible_layers: dict[str, TemplateResource],
    visible_nodes: dict[str, TemplateResource],
    relations: list[ResourceRelation],
    edges: list[GraphEdge],
    layer_parent: dict[str, str],
    node_parent: dict[str, str],
    params: dict[str, Any],
    compacted: bool,
    semantic_context: dict[str, Any],
    concept_nodes: tuple[ConceptNodeInstance, ...],
    concept_groups: tuple[ConceptGroupInstance, ...],
    rules: ArchitectureRules,
) -> dict[str, Any]:
    resource_roles = _resource_roles(original_layers, original_nodes, auxiliary)
    context = {
        "version": "1.0",
        "compacted": compacted,
        "target_language": _target_language_context(),
        "resources": [
            {
                "id": resource.logical_id,
                "label": resource.label,
                "type": resource.resource_type,
                "role": resource_roles.get(resource_id, "unknown"),
                "visible": resource_id in visible_layers or resource_id in visible_nodes,
            }
            for resource_id, resource in resources.items()
        ],
        "containers": [
            {
                "id": layer_id,
                "label": _context_layer_label(layer, params, rules),
                "type": layer.resource_type,
                "parent": layer_parent.get(layer_id),
            }
            for layer_id, layer in visible_layers.items()
        ],
        "visible_nodes": [
            {"id": node.logical_id, "label": node.label, "type": node.resource_type} for node in visible_nodes.values()
        ],
        "containment": [
            {"resource": node_id, "container": parent_id}
            for node_id, parent_id in node_parent.items()
            if node_id in visible_nodes and parent_id in visible_layers
        ],
        "explicit_relations": [
            {
                "source": relation.source_id,
                "target": relation.target_id,
                "property": relation.property_name,
                "source_type": resources[relation.source_id].resource_type,
                "target_type": resources[relation.target_id].resource_type,
            }
            for relation in relations
            if relation.source_id in resources and relation.target_id in resources
        ],
        "property_references": _property_reference_context(resources, visible_nodes),
        "all_property_references": _all_property_reference_context(resources, visible_nodes),
        "route_intents": _route_intent_context(resources),
        "orchestration_actions": _orchestration_action_context(resources, visible_nodes, rules),
        "node_label_hints": _node_label_hint_context(visible_nodes),
        "attachments": _attachment_context(auxiliary, relations, resources),
        "network_attachments": _network_attachment_context(resources),
        "kubernetes_applications": _ack_application_context(resources, relations),
        "concept_nodes": [
            {
                "id": concept.node_id,
                "label": concept.label,
                "type": concept.resource_type,
                "controller": concept.controller_id,
                "source": concept.source_id,
                "via": concept.via_id,
                "runtime_source": concept.runtime_source_id,
                "group": concept.group_id,
            }
            for concept in concept_nodes
            if concept.node_id in visible_nodes
        ],
        "concept_groups": [
            {
                "id": group.group_id,
                "label": group.label,
                "type": group.resource_type,
                "members": list(group.member_ids),
                "parent": group.parent_id,
            }
            for group in concept_groups
            if group.group_id in visible_layers
        ],
        "visible_edges": [
            {
                "from": edge.from_id,
                "to": edge.to_id,
                "style": edge.style,
                "label": edge.label,
            }
            for edge in edges
            if (edge.from_id in visible_nodes or edge.from_id in visible_layers)
            and (edge.to_id in visible_nodes or edge.to_id in visible_layers)
        ],
        "semantic_plan": semantic_context,
        "llm_semantic_plan_schema": _llm_semantic_plan_schema(),
        "llm_rendering_contract": [
            "Use only ids listed in visible_nodes for semantic_plan.edges endpoints.",
            "Use only ids listed in visible_nodes for semantic_plan.node_labels ids.",
            "Node labels replace only the main node title; attachment lines are preserved by the renderer.",
            "Do not encode VPC/VSwitch/SecurityGroup containment as semantic edges.",
            "Use traffic/dependency only for high-confidence runtime relationships.",
            "Use management or inferred for configuration, orchestration, or uncertain relationships.",
            (
                "For scaled compute concept groups, put inherited application traffic on the group members; "
                "the renderer may lift it to the group."
            ),
            "Use orchestration_actions as intent evidence, but do not render orchestration actions as nodes.",
            "Never present inferred edges as explicit template references.",
        ],
    }
    if template_summary:
        context["template_summary"] = template_summary
    return context


def _render_views_from_context(
    architecture_context: dict[str, Any],
    overview_mermaid_source: str,
    semantic_plan: dict[str, Any] | None,
) -> tuple[ArchitectureViewResult, ...]:
    raw_views = semantic_plan.get("views") if isinstance(semantic_plan, dict) else None
    if not isinstance(raw_views, list) or not raw_views:
        return (
            ArchitectureViewResult(
                id="overview",
                title=_("Architecture overview"),
                purpose=_("Show the complete architecture diagram."),
                mermaid_source=overview_mermaid_source,
                architecture_context={
                    "id": "overview",
                    "title": _("Architecture overview"),
                    "purpose": _("Show the complete architecture diagram."),
                    "rendered_as": "full_architecture",
                },
            ),
        )

    rules = ArchitectureRules.load_default()
    detail_anchor_labels = _semantic_detail_anchor_labels(raw_views)
    rendered_views: list[ArchitectureViewResult] = []
    for index, raw_view_value in enumerate(raw_views, start=1):
        if not isinstance(raw_view_value, dict):
            continue
        raw_view = cast("dict[str, Any]", raw_view_value)
        rendered = _render_single_view_from_context(
            architecture_context,
            raw_view,
            index,
            rules,
            detail_anchor_labels,
        )
        if rendered is not None:
            rendered_views.append(rendered)

    if not rendered_views:
        return _render_views_from_context(architecture_context, overview_mermaid_source, None)
    return tuple(rendered_views)


def _render_single_view_from_context(
    architecture_context: dict[str, Any],
    raw_view: dict[str, Any],
    index: int,
    rules: ArchitectureRules,
    detail_anchor_labels: dict[str, list[str]],
) -> ArchitectureViewResult | None:
    view_id = _semantic_view_id(raw_view, index)
    title = _semantic_view_text(raw_view.get("title"), default=view_id)
    purpose = _semantic_view_text(raw_view.get("purpose"), default="")
    layout = _semantic_view_layout(raw_view, view_id)
    anchors = sorted(_semantic_view_anchor_ids(raw_view))

    nodes_by_id = _context_nodes_by_id(architecture_context)
    containers_by_id = _context_containers_by_id(architecture_context)
    node_parent = _context_node_parent(architecture_context)
    layer_parent = _context_layer_parent(containers_by_id)
    group_nodes, group_node_parent = _semantic_view_group_nodes(
        raw_view,
        nodes_by_id,
        containers_by_id,
        node_parent,
        layer_parent,
    )
    requested_ids = _semantic_view_requested_ids(raw_view)
    requested_ids.update(group_nodes)
    explicitly_requested_container_ids = {
        container_id for container_id in requested_ids if container_id in containers_by_id
    }
    flat_container_nodes: dict[str, dict[str, Any]] = {}
    selection_requested_ids = set(requested_ids)
    if layout == "flat":
        flat_container_ids = {container_id for container_id in requested_ids if container_id in containers_by_id}
        selection_requested_ids.difference_update(flat_container_ids)
        flat_container_nodes = _semantic_view_flat_container_nodes(flat_container_ids, containers_by_id)
    selected_nodes, selected_containers = _select_view_elements(
        selection_requested_ids,
        nodes_by_id,
        containers_by_id,
        node_parent,
        layer_parent,
        include_container_ancestors=layout == "contained",
    )
    selected_group_ids = set(group_nodes)
    if selected_group_ids:
        summarized_member_ids = {
            member_id
            for group_id in selected_group_ids
            for member_id in group_nodes.get(group_id, {}).get("members", [])
            if isinstance(member_id, str)
        }
        selected_nodes.difference_update(summarized_member_ids)
    if layout == "contained":
        for group_id in selected_group_ids:
            selected_containers.update(_container_ancestor_chain(group_node_parent.get(group_id), layer_parent))
    selected_flat_container_ids = set(flat_container_nodes)
    selected_ids = selected_nodes | selected_containers | selected_group_ids | selected_flat_container_ids
    semantic_view_edges = _semantic_view_edges(raw_view, selected_ids)
    context_view_edges = _context_view_edges(architecture_context, selected_ids)
    if semantic_view_edges:
        labeled_context_edges = _deterministic_labeled_context_edges(architecture_context, context_view_edges)
        view_edges = _dedupe_edges(
            [
                *labeled_context_edges,
                *(
                    edge
                    for edge in semantic_view_edges
                    if not _is_covered_by_deterministic_edge(edge.from_id, edge.to_id, labeled_context_edges)
                ),
            ]
        )
    else:
        view_edges = context_view_edges
    edge_container_ids = {
        endpoint_id
        for edge in view_edges
        for endpoint_id in (edge.from_id, edge.to_id)
        if endpoint_id in containers_by_id
    }
    selected_containers = _prune_empty_view_containers(
        selected_containers,
        selected_nodes | selected_group_ids,
        {**node_parent, **group_node_parent},
        layer_parent,
        keep_container_ids=explicitly_requested_container_ids | edge_container_ids,
    )
    render_node_parent = {**node_parent, **group_node_parent}
    if view_id == "overview" and layout == "contained":
        selected_containers, render_node_parent = _collapse_noisy_overview_network_containers(
            selected_containers,
            selected_nodes | selected_group_ids,
            containers_by_id,
            render_node_parent,
            layer_parent,
            keep_container_ids=explicitly_requested_container_ids | edge_container_ids,
        )
    selected_ids = selected_nodes | selected_containers | selected_group_ids | selected_flat_container_ids
    if not selected_nodes and not selected_containers and not selected_group_ids and not selected_flat_container_ids:
        return None

    view_containers = {
        container_id: containers_by_id[container_id]
        for container_id in containers_by_id
        if container_id in selected_containers
    }
    view_nodes = {node_id: nodes_by_id[node_id] for node_id in nodes_by_id if node_id in selected_nodes}
    view_nodes.update(group_nodes)
    view_nodes.update(flat_container_nodes)
    if view_id == "overview":
        view_containers = _with_detail_anchor_labels(view_containers, detail_anchor_labels, multiline=False)
        view_nodes = _with_detail_anchor_labels(view_nodes, detail_anchor_labels)
    view_layer_parent = {
        container_id: parent_id
        for container_id, parent_id in layer_parent.items()
        if container_id in view_containers and parent_id in view_containers
    }
    view_node_parent = {
        node_id: parent_id
        for node_id, parent_id in render_node_parent.items()
        if node_id in view_nodes and parent_id in view_containers
    }
    mermaid_source = _render_context_mermaid(
        view_containers,
        view_nodes,
        view_edges,
        view_layer_parent,
        view_node_parent,
        rules,
    )
    view_context = {
        "id": view_id,
        "title": title,
        "purpose": purpose,
        "anchors": anchors,
        "nodes": sorted(view_nodes),
        "containers": sorted(view_containers),
        "edges": [
            {"from": edge.from_id, "to": edge.to_id, "style": edge.style, "label": edge.label} for edge in view_edges
        ],
        "layout": layout,
        "rendered_as": "filtered_architecture_view",
    }
    return ArchitectureViewResult(
        id=view_id,
        title=title,
        purpose=purpose,
        mermaid_source=mermaid_source,
        architecture_context=view_context,
    )


def _semantic_detail_anchor_labels(raw_views: list[Any]) -> dict[str, list[str]]:
    labels_by_anchor: dict[str, list[str]] = {}
    for index, raw_view_value in enumerate(raw_views, start=1):
        if not isinstance(raw_view_value, dict):
            continue
        raw_view = cast("dict[str, Any]", raw_view_value)
        view_id = _semantic_view_id(raw_view, index)
        if view_id == "overview" or not view_id.startswith("detail_"):
            continue
        title = _semantic_view_text(raw_view.get("title"), default=view_id)
        for anchor_id in sorted(_semantic_view_anchor_ids(raw_view)):
            labels_by_anchor.setdefault(anchor_id, []).append(title)
    return labels_by_anchor


def _semantic_view_anchor_ids(raw_view: dict[str, Any]) -> set[str]:
    anchors: set[str] = set()
    for key in ("anchors", "anchor_ids"):
        value = raw_view.get(key)
        if isinstance(value, list):
            anchors.update(item.strip() for item in value if isinstance(item, str) and item.strip())
    return anchors


def _semantic_view_group_nodes(
    raw_view: dict[str, Any],
    nodes_by_id: dict[str, dict[str, Any]],
    containers_by_id: dict[str, dict[str, Any]],
    node_parent: dict[str, str],
    layer_parent: dict[str, str],
) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    group_nodes: dict[str, dict[str, Any]] = {}
    group_parent: dict[str, str] = {}
    raw_groups = raw_view.get("groups")
    if not isinstance(raw_groups, list):
        return group_nodes, group_parent
    for raw_group_value in raw_groups:
        if not isinstance(raw_group_value, dict):
            continue
        raw_group = cast("dict[str, Any]", raw_group_value)
        group_id = _semantic_view_group_id(raw_group)
        if group_id is None or group_id in group_nodes or group_id in nodes_by_id or group_id in containers_by_id:
            continue
        member_ids = [
            member_id
            for member_id in _semantic_view_group_member_ids(raw_group)
            if member_id in nodes_by_id and member_id not in group_nodes
        ]
        if not member_ids:
            continue
        common_parent = _nearest_common_member_container(member_ids, node_parent, layer_parent)
        label = _semantic_view_group_label(
            raw_group,
            group_id,
            member_ids,
            nodes_by_id,
            containers_by_id,
            node_parent,
            layer_parent,
            common_parent,
        )
        group_nodes[group_id] = {
            "id": group_id,
            "label": label,
            "type": VIEW_SUMMARY_GROUP_TYPE,
            "members": member_ids,
        }
        explicit_parent = raw_group.get("parent") or raw_group.get("container")
        if isinstance(explicit_parent, str) and explicit_parent.strip() in containers_by_id:
            group_parent[group_id] = explicit_parent.strip()
            continue
        if common_parent is not None:
            group_parent[group_id] = common_parent
    return group_nodes, group_parent


def _semantic_view_flat_container_nodes(
    container_ids: set[str],
    containers_by_id: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    return {
        container_id: {
            "id": container_id,
            "label": str(containers_by_id[container_id].get("label") or container_id),
            "type": str(containers_by_id[container_id].get("type") or ""),
        }
        for container_id in sorted(container_ids)
        if container_id in containers_by_id
    }


def _semantic_view_group_id(raw_group: dict[str, Any]) -> str | None:
    value = raw_group.get("id")
    if not isinstance(value, str) or not value.strip():
        value = raw_group.get("group_id")
    if not isinstance(value, str) or not value.strip():
        return None
    group_id = value.strip()
    if not re.match(r"^[A-Za-z][A-Za-z0-9_]*$", group_id):
        return None
    return group_id


def _semantic_view_group_member_ids(raw_group: dict[str, Any]) -> list[str]:
    value = raw_group.get("members")
    if not isinstance(value, list):
        value = raw_group.get("member_ids")
    if not isinstance(value, list):
        return []
    return list(dict.fromkeys(item.strip() for item in value if isinstance(item, str) and item.strip()))


def _semantic_view_group_label(
    raw_group: dict[str, Any],
    group_id: str,
    member_ids: list[str],
    nodes_by_id: dict[str, dict[str, Any]],
    containers_by_id: dict[str, dict[str, Any]],
    node_parent: dict[str, str],
    layer_parent: dict[str, str],
    common_parent: str | None,
) -> str:
    base_label = _semantic_view_text(raw_group.get("label"), default=group_id)
    member_labels = [
        _semantic_base_label(str(nodes_by_id[member_id].get("label") or member_id)) for member_id in member_ids[:4]
    ]
    lines = [base_label, *(f"+ {label}" for label in member_labels if label)]
    placement_summary = _semantic_view_group_placement_summary(
        member_ids,
        containers_by_id,
        node_parent,
        layer_parent,
        common_parent,
    )
    if placement_summary:
        lines.append(f"+ {placement_summary}")
    remaining_count = len(member_ids) - len(member_labels)
    if remaining_count > 0:
        remaining_label = _("more")
        lines.append(f"+ {remaining_label} x{remaining_count}")
    return "\\n".join(lines)


def _nearest_common_member_container(
    member_ids: list[str],
    node_parent: dict[str, str],
    layer_parent: dict[str, str],
) -> str | None:
    chains = [
        _container_ancestor_list(node_parent.get(member_id), layer_parent)
        for member_id in member_ids
        if node_parent.get(member_id) is not None
    ]
    if not chains:
        return None
    common = set(chains[0])
    for chain in chains[1:]:
        common.intersection_update(chain)
    for container_id in chains[0]:
        if container_id in common:
            return container_id
    return None


def _semantic_view_group_placement_summary(
    member_ids: list[str],
    containers_by_id: dict[str, dict[str, Any]],
    node_parent: dict[str, str],
    layer_parent: dict[str, str],
    common_parent: str | None,
) -> str | None:
    if common_parent is not None:
        return None
    root_container_ids = [
        _root_container_id(node_parent.get(member_id), layer_parent)
        for member_id in member_ids
        if node_parent.get(member_id) is not None
    ]
    unique_root_ids = list(dict.fromkeys(container_id for container_id in root_container_ids if container_id))
    if len(unique_root_ids) < 2:
        return None
    root_labels = [str(containers_by_id[container_id].get("label") or container_id) for container_id in unique_root_ids]
    kind = _semantic_group_container_kind(root_labels)
    return _("across {kind} x{count}").format(kind=kind, count=len(unique_root_ids))


def _root_container_id(container_id: str | None, layer_parent: dict[str, str]) -> str | None:
    chain = _container_ancestor_list(container_id, layer_parent)
    return chain[-1] if chain else None


def _semantic_group_container_kind(labels: list[str]) -> str:
    if labels and all("VPC" in label for label in labels):
        return "VPC"
    if labels and all("VSwitch" in label or "交换机" in label for label in labels):
        return "VSwitch"
    kinds = {re.split(r"\s|\(", label.strip(), maxsplit=1)[0] for label in labels if label.strip()}
    if len(kinds) == 1:
        return next(iter(kinds))
    return _("boundaries")


def _with_detail_anchor_labels(
    items: dict[str, dict[str, Any]],
    detail_anchor_labels: dict[str, list[str]],
    *,
    multiline: bool = True,
) -> dict[str, dict[str, Any]]:
    annotated: dict[str, dict[str, Any]] = {}
    for item_id, item in items.items():
        labels = detail_anchor_labels.get(item_id)
        if not labels:
            annotated[item_id] = item
            continue
        annotated[item_id] = {
            **item,
            "label": _append_detail_anchor_label(str(item.get("label") or item_id), labels, multiline=multiline),
        }
    return annotated


def _append_detail_anchor_label(label: str, detail_titles: list[str], *, multiline: bool = True) -> str:
    unique_titles = list(dict.fromkeys(title for title in detail_titles if title))
    if not unique_titles:
        return label
    detail_label = " / ".join(unique_titles)
    if not multiline:
        return _("{label} (expand: {detail_label})").format(label=label, detail_label=detail_label)
    return _("{label}\\nexpand: {detail_label}").format(label=label, detail_label=detail_label)


def _semantic_view_id(raw_view: dict[str, Any], index: int) -> str:
    value = raw_view.get("id")
    if not isinstance(value, str) or not value.strip():
        value = raw_view.get("view_id")
    if not isinstance(value, str) or not value.strip():
        return f"view_{index}"
    normalized = re.sub(r"[^A-Za-z0-9_]+", "_", value.strip())
    return normalized.strip("_") or f"view_{index}"


def _semantic_view_text(value: Any, *, default: str) -> str:
    if not isinstance(value, str):
        return default
    text = " ".join(value.strip().split())
    return text[:48] if text else default


def _semantic_view_layout(raw_view: dict[str, Any], view_id: str) -> str:
    value = raw_view.get("layout")
    if not isinstance(value, str) or not value.strip():
        value = raw_view.get("render_mode")
    if isinstance(value, str):
        normalized = value.strip().lower().replace("-", "_")
        if normalized in {"contained", "flat"}:
            return normalized
        if normalized in {"with_containment", "placement", "network"}:
            return "contained"
        if normalized in {"relationship", "relationships", "logical"}:
            return "flat"
    return "flat"


def _semantic_view_requested_ids(raw_view: dict[str, Any]) -> set[str]:
    requested: set[str] = set()
    for key in ("nodes", "node_ids", "containers", "container_ids"):
        value = raw_view.get(key)
        if isinstance(value, list):
            requested.update(item.strip() for item in value if isinstance(item, str) and item.strip())
    for raw_edge in raw_view.get("edges", []) if isinstance(raw_view.get("edges"), list) else []:
        if not isinstance(raw_edge, dict):
            continue
        from_id = _semantic_edge_endpoint(raw_edge, "from")
        to_id = _semantic_edge_endpoint(raw_edge, "to")
        if from_id is not None:
            requested.add(from_id)
        if to_id is not None:
            requested.add(to_id)
    return requested


def _context_nodes_by_id(architecture_context: dict[str, Any]) -> dict[str, dict[str, Any]]:
    nodes: dict[str, dict[str, Any]] = {}
    for item in architecture_context.get("visible_nodes", []):
        if isinstance(item, dict) and isinstance(item.get("id"), str):
            nodes[item["id"]] = item
    return nodes


def _context_containers_by_id(architecture_context: dict[str, Any]) -> dict[str, dict[str, Any]]:
    containers: dict[str, dict[str, Any]] = {}
    for item in architecture_context.get("containers", []):
        if isinstance(item, dict) and isinstance(item.get("id"), str):
            containers[item["id"]] = item
    return containers


def _context_node_parent(architecture_context: dict[str, Any]) -> dict[str, str]:
    parents: dict[str, str] = {}
    for item in architecture_context.get("containment", []):
        if isinstance(item, dict) and isinstance(item.get("resource"), str) and isinstance(item.get("container"), str):
            parents[item["resource"]] = item["container"]
    return parents


def _context_layer_parent(containers_by_id: dict[str, dict[str, Any]]) -> dict[str, str]:
    parents: dict[str, str] = {}
    for container_id, container in containers_by_id.items():
        parent = container.get("parent")
        if isinstance(parent, str) and parent in containers_by_id:
            parents[container_id] = parent
    return parents


def _select_view_elements(
    requested_ids: set[str],
    nodes_by_id: dict[str, dict[str, Any]],
    containers_by_id: dict[str, dict[str, Any]],
    node_parent: dict[str, str],
    layer_parent: dict[str, str],
    *,
    include_container_ancestors: bool,
) -> tuple[set[str], set[str]]:
    selected_nodes = {node_id for node_id in requested_ids if node_id in nodes_by_id}
    selected_containers = {container_id for container_id in requested_ids if container_id in containers_by_id}

    for container_id in tuple(selected_containers):
        selected_nodes.update(
            node_id
            for node_id in nodes_by_id
            if container_id in _container_ancestor_chain(node_parent.get(node_id), layer_parent)
        )

    if include_container_ancestors:
        for node_id in selected_nodes:
            selected_containers.update(_container_ancestor_chain(node_parent.get(node_id), layer_parent))
        for container_id in tuple(selected_containers):
            selected_containers.update(_container_ancestor_chain(layer_parent.get(container_id), layer_parent))

    return selected_nodes, selected_containers


def _prune_empty_view_containers(
    selected_containers: set[str],
    selected_node_ids: set[str],
    node_parent: dict[str, str],
    layer_parent: dict[str, str],
    *,
    keep_container_ids: set[str],
) -> set[str]:
    kept = set(selected_containers)
    changed = True
    while changed:
        changed = False
        for container_id in sorted(kept):
            if container_id in keep_container_ids:
                continue
            has_selected_node = any(
                node_id in selected_node_ids and parent_id == container_id for node_id, parent_id in node_parent.items()
            )
            has_selected_child_container = any(
                child_id in kept and parent_id == container_id for child_id, parent_id in layer_parent.items()
            )
            if has_selected_node or has_selected_child_container:
                continue
            kept.remove(container_id)
            changed = True
    return kept


def _collapse_noisy_overview_network_containers(
    selected_containers: set[str],
    selected_node_ids: set[str],
    containers_by_id: dict[str, dict[str, Any]],
    node_parent: dict[str, str],
    layer_parent: dict[str, str],
    *,
    keep_container_ids: set[str],
) -> tuple[set[str], dict[str, str]]:
    noisy_children_by_parent: dict[str, list[str]] = {}
    for container_id in selected_containers:
        if container_id in keep_container_ids:
            continue
        container = containers_by_id.get(container_id)
        if not container or container.get("type") != "ALIYUN::ECS::VSwitch":
            continue
        parent_id = layer_parent.get(container_id)
        if parent_id in selected_containers:
            noisy_children_by_parent.setdefault(parent_id, []).append(container_id)

    collapsed_container_ids = {
        container_id
        for child_ids in noisy_children_by_parent.values()
        if len(child_ids) >= 3
        for container_id in child_ids
    }
    if not collapsed_container_ids:
        return selected_containers, node_parent

    collapsed_selected_containers = selected_containers - collapsed_container_ids
    collapsed_node_parent = dict(node_parent)
    for node_id in selected_node_ids:
        parent_id = collapsed_node_parent.get(node_id)
        if parent_id not in collapsed_container_ids:
            continue
        replacement_parent_id = _nearest_selected_container_ancestor(
            parent_id,
            collapsed_selected_containers,
            layer_parent,
        )
        if replacement_parent_id is None:
            collapsed_node_parent.pop(node_id, None)
        else:
            collapsed_node_parent[node_id] = replacement_parent_id

    return collapsed_selected_containers, collapsed_node_parent


def _nearest_selected_container_ancestor(
    container_id: str,
    selected_containers: set[str],
    layer_parent: dict[str, str],
) -> str | None:
    current = layer_parent.get(container_id)
    while current is not None:
        if current in selected_containers:
            return current
        current = layer_parent.get(current)
    return None


def _container_ancestor_chain(container_id: str | None, layer_parent: dict[str, str]) -> set[str]:
    return set(_container_ancestor_list(container_id, layer_parent))


def _container_ancestor_list(container_id: str | None, layer_parent: dict[str, str]) -> list[str]:
    ancestors: set[str] = set()
    ordered: list[str] = []
    current = container_id
    while current is not None and current not in ancestors:
        ancestors.add(current)
        ordered.append(current)
        current = layer_parent.get(current)
    return ordered


def _semantic_view_edges(raw_view: dict[str, Any], selected_ids: set[str]) -> list[GraphEdge]:
    raw_edges = raw_view.get("edges")
    if not isinstance(raw_edges, list):
        return []
    edges: list[GraphEdge] = []
    for raw_edge in raw_edges:
        if not isinstance(raw_edge, dict):
            continue
        from_id = _semantic_edge_endpoint(raw_edge, "from")
        to_id = _semantic_edge_endpoint(raw_edge, "to")
        label = _semantic_edge_label(raw_edge)
        kind = raw_edge.get("kind")
        if not isinstance(kind, str) or kind not in SEMANTIC_EDGE_STYLES:
            kind = "inferred"
        if from_id is None or to_id is None or label is None:
            continue
        if from_id not in selected_ids or to_id not in selected_ids:
            continue
        edges.append(GraphEdge(from_id, to_id, SEMANTIC_EDGE_STYLES[kind], label))
    return _dedupe_edges(edges)


def _context_view_edges(architecture_context: dict[str, Any], selected_ids: set[str]) -> list[GraphEdge]:
    edges: list[GraphEdge] = []
    for raw_edge in architecture_context.get("visible_edges", []):
        if not isinstance(raw_edge, dict):
            continue
        from_id = raw_edge.get("from")
        to_id = raw_edge.get("to")
        style = raw_edge.get("style")
        label = raw_edge.get("label")
        if not isinstance(from_id, str) or not isinstance(to_id, str) or not isinstance(style, str):
            continue
        if from_id not in selected_ids or to_id not in selected_ids:
            continue
        edges.append(GraphEdge(from_id, to_id, style, label if isinstance(label, str) else None))
    return _dedupe_edges(edges)


def _deterministic_labeled_context_edges(
    architecture_context: dict[str, Any],
    context_edges: list[GraphEdge],
) -> list[GraphEdge]:
    accepted_edges = architecture_context.get("semantic_plan", {}).get("accepted_edges")
    if not isinstance(accepted_edges, list):
        accepted_edges = []
    semantic_pairs = set()
    for edge in accepted_edges:
        if not isinstance(edge, dict):
            continue
        semantic_pairs.add((str(edge.get("from") or ""), str(edge.get("to") or "")))
    return [
        edge for edge in context_edges if edge.label is not None and (edge.from_id, edge.to_id) not in semantic_pairs
    ]


def _render_context_mermaid(
    containers: dict[str, dict[str, Any]],
    nodes: dict[str, dict[str, Any]],
    edges: list[GraphEdge],
    layer_parent: dict[str, str],
    node_parent: dict[str, str],
    rules: ArchitectureRules,
) -> str:
    lines: list[str] = ["graph TD"]
    sg_style_ids: list[str] = []
    security_group_layer_types = set(rules.containment_types("security_group"))

    def render_layer(layer_id: str, indent: str) -> None:
        layer = containers[layer_id]
        sub_id = f"layer_{layer_id}"
        label = str(layer.get("label") or layer_id)
        lines.append(f"{indent}subgraph {sub_id} [{_subgraph_label(label)}]")
        if layer.get("type") in security_group_layer_types:
            sg_style_ids.append(sub_id)
        for child_layer_id in containers:
            if layer_parent.get(child_layer_id) == layer_id:
                render_layer(child_layer_id, indent + "  ")
        for child_node_id in nodes:
            if node_parent.get(child_node_id) == layer_id:
                lines.append(f'{indent}  {child_node_id}["{nodes[child_node_id].get("label") or child_node_id}"]')
        lines.append(f"{indent}end")

    for layer_id in containers:
        if layer_id not in layer_parent:
            render_layer(layer_id, "  ")

    for node_id, node in nodes.items():
        if node_id not in node_parent:
            lines.append(f'  {node_id}["{node.get("label") or node_id}"]')

    for edge in _dedupe_edges(edges):
        from_endpoint = _context_mermaid_endpoint(edge.from_id, nodes, containers)
        to_endpoint = _context_mermaid_endpoint(edge.to_id, nodes, containers)
        if from_endpoint is None or to_endpoint is None:
            continue
        operator = rules.edge_operator(edge.style)
        if edge.label:
            lines.append(f"  {from_endpoint} {operator}|{_edge_label(edge.label)}| {to_endpoint}")
        else:
            lines.append(f"  {from_endpoint} {operator} {to_endpoint}")

    for style_id in sg_style_ids:
        lines.append(f"  style {style_id} stroke-dasharray: 5 5")

    return "\n".join(lines)


def _context_mermaid_endpoint(
    endpoint_id: str,
    nodes: dict[str, dict[str, Any]],
    containers: dict[str, dict[str, Any]],
) -> str | None:
    if endpoint_id in nodes:
        return endpoint_id
    if endpoint_id in containers:
        return f"layer_{endpoint_id}"
    return None


def _error_architecture_context(reason: str) -> dict[str, Any]:
    return {
        "version": "1.0",
        "error": reason,
        "resources": [],
        "containers": [],
        "visible_nodes": [],
        "containment": [],
        "explicit_relations": [],
        "property_references": [],
        "all_property_references": [],
        "route_intents": [],
        "orchestration_actions": [],
        "node_label_hints": [],
        "attachments": [],
        "concept_nodes": [],
        "concept_groups": [],
        "kubernetes_applications": [],
        "visible_edges": [],
        "semantic_plan": {"accepted_edges": [], "rejected_edges": []},
        "llm_semantic_plan_schema": _llm_semantic_plan_schema(),
        "target_language": _target_language_context(),
    }


def _target_language_context() -> dict[str, str]:
    language = get_current_language()
    names = {
        "zh": "Chinese",
        "en": "English",
        "es": "Spanish",
        "fr": "French",
        "de": "German",
        "ja": "Japanese",
        "pt": "Portuguese",
    }
    return {"code": language, "name": names.get(language, language)}


def _resource_roles(
    layers: dict[str, TemplateResource],
    nodes: dict[str, TemplateResource],
    auxiliary: dict[str, TemplateResource],
) -> dict[str, str]:
    roles: dict[str, str] = {resource_id: "container" for resource_id in layers}
    roles.update({resource_id: "node" for resource_id in nodes})
    roles.update({resource_id: "auxiliary" for resource_id in auxiliary})
    return roles


def _context_layer_label(layer: TemplateResource, params: dict[str, Any], rules: ArchitectureRules) -> str:
    label = _localized_layer_label(layer, params, rules)
    return label


def _localized_layer_label(layer: TemplateResource, params: dict[str, Any], rules: ArchitectureRules) -> str:
    base_label = layer.label.split("\\n", 1)[0]
    label = _(rules.legacy_layer_labels.get(layer.resource_type, base_label))
    cidr_layer_types = set(rules.containment_types("vpc")) | set(rules.containment_types("vswitch"))
    if layer.resource_type in cidr_layer_types:
        cidr = _resolve_cidr(layer.properties.get("CidrBlock"), params)
        if cidr:
            label = f"{label} ({cidr})"
    return label


def _attachment_context(
    auxiliary: dict[str, TemplateResource],
    relations: list[ResourceRelation],
    resources: dict[str, TemplateResource],
) -> list[dict[str, str]]:
    attachments: list[dict[str, str]] = []
    by_source = _relations_by_source(relations)
    for resource in auxiliary.values():
        source_relations = by_source.get(resource.logical_id, [])
        main_ref = _main_resource_ref(resource, source_relations)
        if main_ref is None or main_ref not in resources:
            continue
        for relation in source_relations:
            if relation.target_id == main_ref or relation.target_id not in resources:
                continue
            attachments.append(
                {
                    "via": resource.logical_id,
                    "marker": main_ref,
                    "target": relation.target_id,
                    "property": relation.property_name,
                    "marker_type": resources[main_ref].resource_type,
                    "target_type": resources[relation.target_id].resource_type,
                }
            )
    return attachments


def _network_attachment_context(resources: dict[str, TemplateResource]) -> list[dict[str, str]]:
    attachments: list[dict[str, str]] = []
    for resource in resources.values():
        if resource.resource_type == "ALIYUN::CEN::CenInstanceAttachment":
            item = _cen_child_instance_attachment_context(resource, resources)
        elif resource.resource_type == "ALIYUN::CEN::TransitRouterVpcAttachment":
            item = _transit_router_child_attachment_context(
                resource,
                resources,
                hub_property="TransitRouterId",
                child_type="VPC",
                child_property="VpcId",
            )
        elif resource.resource_type == "ALIYUN::CEN::TransitRouterVbrAttachment":
            item = _transit_router_child_attachment_context(
                resource,
                resources,
                hub_property="TransitRouterId",
                child_type="VBR",
                child_property="VbrId",
            )
        elif resource.resource_type == "ALIYUN::CEN::TransitRouterPeerAttachment":
            item = _transit_router_child_attachment_context(
                resource,
                resources,
                hub_property="TransitRouterId",
                child_type="TransitRouter",
                child_property="PeerTransitRouterId",
                child_region_property="PeerTransitRouterRegionId",
            )
        else:
            item = None
        if item is not None:
            attachments.append(item)
    return attachments


def _cen_child_instance_attachment_context(
    resource: TemplateResource,
    resources: dict[str, TemplateResource],
) -> dict[str, str]:
    properties = resource.properties
    item = {
        "id": resource.logical_id,
        "type": resource.resource_type,
        "network": "CEN",
        "cen": _resource_ref_or_value_summary(properties.get("CenId"), resources),
        "child_instance_type": _resource_ref_or_value_summary(properties.get("ChildInstanceType"), resources),
        "child_instance_id": _resource_ref_or_value_summary(properties.get("ChildInstanceId"), resources),
        "child_instance_region": _resource_ref_or_value_summary(properties.get("ChildInstanceRegionId"), resources),
    }
    _add_child_resource_context(item, properties.get("ChildInstanceId"), resources)
    return {key: value for key, value in item.items() if value}


def _transit_router_child_attachment_context(
    resource: TemplateResource,
    resources: dict[str, TemplateResource],
    *,
    hub_property: str,
    child_type: str,
    child_property: str,
    child_region_property: str | None = None,
) -> dict[str, str]:
    properties = resource.properties
    item = {
        "id": resource.logical_id,
        "type": resource.resource_type,
        "network": "CEN",
        "transit_router": _resource_ref_or_value_summary(properties.get(hub_property), resources),
        "child_instance_type": child_type,
        "child_instance_id": _resource_ref_or_value_summary(properties.get(child_property), resources),
    }
    if child_region_property is not None:
        item["child_instance_region"] = _resource_ref_or_value_summary(properties.get(child_region_property), resources)
    _add_child_resource_context(item, properties.get(child_property), resources)
    return {key: value for key, value in item.items() if value}


def _add_child_resource_context(
    item: dict[str, str],
    value: Any,
    resources: dict[str, TemplateResource],
) -> None:
    child_refs = _extract_resource_refs(value, resources)
    if not child_refs:
        return
    child_id = child_refs[0]
    child_resource = resources.get(child_id)
    if child_resource is None:
        return
    item["child_resource"] = child_id
    item["child_resource_type"] = child_resource.resource_type


def _resource_ref_or_value_summary(value: Any, resources: dict[str, TemplateResource]) -> str:
    refs = _extract_resource_refs(value, resources)
    if refs:
        return refs[0] if len(refs) == 1 else ", ".join(refs)
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        ref = value.get("Ref")
        if isinstance(ref, str) and ref:
            return f"Ref:{ref}"
        get_att = value.get("Fn::GetAtt")
        if isinstance(get_att, list) and get_att and isinstance(get_att[0], str):
            return f"GetAtt:{'.'.join(str(part) for part in get_att)}"
        if isinstance(get_att, str) and get_att:
            return f"GetAtt:{get_att}"
    return ""


def _property_reference_context(
    resources: dict[str, TemplateResource],
    visible_nodes: dict[str, TemplateResource],
) -> list[dict[str, str]]:
    visible_node_ids = set(visible_nodes)
    references: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for source in resources.values():
        if source.logical_id not in visible_node_ids:
            continue
        for property_name, property_value in source.properties.items():
            for target_id in _extract_resource_refs(property_value, resources):
                if target_id not in visible_node_ids:
                    continue
                key = (source.logical_id, target_id, property_name)
                if source.logical_id == target_id or key in seen:
                    continue
                target = resources[target_id]
                seen.add(key)
                references.append(
                    {
                        "source": source.logical_id,
                        "target": target_id,
                        "property": property_name,
                        "source_type": source.resource_type,
                        "target_type": target.resource_type,
                    }
                )
    return references


def _all_property_reference_context(
    resources: dict[str, TemplateResource],
    visible_nodes: dict[str, TemplateResource],
) -> list[dict[str, str | bool]]:
    visible_node_ids = set(visible_nodes)
    references: list[dict[str, str | bool]] = []
    seen: set[tuple[str, str, str]] = set()
    for source in resources.values():
        if source.logical_id not in visible_node_ids:
            continue
        for property_name, property_value in source.properties.items():
            for target_id in _extract_resource_refs(property_value, resources):
                if target_id not in resources:
                    continue
                key = (source.logical_id, target_id, property_name)
                if source.logical_id == target_id or key in seen:
                    continue
                target = resources[target_id]
                seen.add(key)
                references.append(
                    {
                        "source": source.logical_id,
                        "target": target_id,
                        "property": property_name,
                        "source_type": source.resource_type,
                        "target_type": target.resource_type,
                        "target_visible": target_id in visible_node_ids,
                    }
                )
    return references


def _route_intent_context(resources: dict[str, TemplateResource]) -> list[dict[str, str]]:
    intents: list[dict[str, str]] = []
    for resource in resources.values():
        if resource.resource_type == "ALIYUN::ECS::Route":
            item = _ecs_route_intent_context(resource, resources)
        elif resource.resource_type == "ALIYUN::CEN::TransitRouterRouteEntry":
            item = _cen_route_intent_context(resource, resources)
        else:
            item = {}
        if item:
            intents.append(item)
    return intents


def _ecs_route_intent_context(
    resource: TemplateResource,
    resources: dict[str, TemplateResource],
) -> dict[str, str]:
    properties = resource.properties
    item = {
        "id": resource.logical_id,
        "type": resource.resource_type,
        "destination": _route_destination_summary(properties),
        "route_table": _resource_ref_or_value_summary(properties.get("RouteTableId"), resources),
        "next_hop_type": _scalar_property_summary(properties.get("NextHopType")),
        "next_hop": _resource_ref_or_value_summary(properties.get("NextHopId"), resources),
    }
    _add_route_resource_context(item, properties.get("RouteTableId"), resources, prefix="route_table")
    _add_route_resource_context(item, properties.get("NextHopId"), resources, prefix="next_hop")
    return {key: value for key, value in item.items() if value}


def _cen_route_intent_context(
    resource: TemplateResource,
    resources: dict[str, TemplateResource],
) -> dict[str, str]:
    properties = resource.properties
    item = {
        "id": resource.logical_id,
        "type": resource.resource_type,
        "destination": _route_destination_summary(properties),
        "route_table": _resource_ref_or_value_summary(properties.get("TransitRouterRouteTableId"), resources),
        "next_hop_type": _scalar_property_summary(properties.get("TransitRouterRouteEntryNextHopType")),
        "next_hop": _resource_ref_or_value_summary(properties.get("TransitRouterRouteEntryNextHopId"), resources),
    }
    _add_route_resource_context(item, properties.get("TransitRouterRouteTableId"), resources, prefix="route_table")
    _add_route_resource_context(item, properties.get("TransitRouterRouteEntryNextHopId"), resources, prefix="next_hop")
    return {key: value for key, value in item.items() if value}


def _route_destination_summary(properties: dict[str, Any]) -> str:
    for property_name in (
        "DestinationCidrBlock",
        "DestinationIpv6CidrBlock",
        "DestinationChildInstanceCidrBlock",
        "TransitRouterRouteEntryDestinationCidrBlock",
    ):
        summary = _scalar_property_summary(properties.get(property_name))
        if summary:
            return summary
    return ""


def _scalar_property_summary(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    return ""


def _add_route_resource_context(
    item: dict[str, str],
    value: Any,
    resources: dict[str, TemplateResource],
    *,
    prefix: str,
) -> None:
    refs = _extract_resource_refs(value, resources)
    if not refs:
        return
    resource_id = refs[0]
    resource = resources.get(resource_id)
    if resource is None:
        return
    item[f"{prefix}_resource"] = resource_id
    item[f"{prefix}_resource_type"] = resource.resource_type


def _orchestration_action_context(
    resources: dict[str, TemplateResource],
    visible_nodes: dict[str, TemplateResource],
    rules: ArchitectureRules,
) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    visible_node_ids = set(visible_nodes)
    for resource in resources.values():
        action_rule = _orchestration_action_rule(resource, rules)
        if action_rule is None or not action_rule.target_properties:
            continue
        command_id = _first_property_ref(resource, resources, action_rule.command_properties)
        evidence_resource = resources.get(command_id) if command_id is not None else resource
        evidence_rule = _orchestration_action_rule(evidence_resource, rules) if evidence_resource is not None else None
        evidence_properties = evidence_rule.evidence_properties if evidence_rule is not None else ()
        targets = _property_ref_context_items(
            resource,
            resources,
            visible_node_ids,
            action_rule.target_properties,
        )
        referenced_resources = (
            _property_ref_context_items(evidence_resource, resources, visible_node_ids, evidence_properties)
            if evidence_resource is not None
            else []
        )
        if not targets and not referenced_resources:
            continue
        actions.append(
            {
                "id": resource.logical_id,
                "type": resource.resource_type,
                "command": command_id,
                "targets": targets,
                "referenced_resources": referenced_resources,
            }
        )
    return actions


def _orchestration_action_rule(
    resource: TemplateResource | None,
    rules: ArchitectureRules,
) -> CompactOrchestrationAction | None:
    if resource is None:
        return None
    for action_rule in rules.compact_orchestration_actions:
        if resource.resource_type in action_rule.resource_types:
            return action_rule
    return None


def _first_property_ref(
    resource: TemplateResource,
    resources: dict[str, TemplateResource],
    property_names: tuple[str, ...],
) -> str | None:
    for item in _property_ref_context_items(resource, resources, set(), property_names):
        resource_id = item.get("id")
        if isinstance(resource_id, str):
            return resource_id
    return None


def _property_ref_context_items(
    resource: TemplateResource,
    resources: dict[str, TemplateResource],
    visible_node_ids: set[str],
    property_names: tuple[str, ...],
) -> list[dict[str, str | bool]]:
    if not property_names:
        return []
    property_name_set = set(property_names)
    items: list[dict[str, str | bool]] = []
    seen: set[tuple[str, str]] = set()
    for property_name, property_value in resource.properties.items():
        if property_name not in property_name_set:
            continue
        for target_id in _extract_resource_refs(property_value, resources):
            target = resources.get(target_id)
            if target is None:
                continue
            key = (target_id, property_name)
            if key in seen:
                continue
            seen.add(key)
            items.append(
                {
                    "id": target_id,
                    "type": target.resource_type,
                    "property": property_name,
                    "visible": target_id in visible_node_ids,
                }
            )
    return items


def _node_label_hint_context(visible_nodes: dict[str, TemplateResource]) -> list[dict[str, Any]]:
    hints: list[dict[str, Any]] = []
    for node in visible_nodes.values():
        node_hints = _node_label_hints(node.properties)
        if not node_hints:
            continue
        hints.append(
            {
                "id": node.logical_id,
                "label": node.label,
                "type": node.resource_type,
                "hints": node_hints,
            }
        )
    return hints


def _node_label_hints(properties: dict[str, Any]) -> dict[str, str]:
    hint_keys = (
        "InstanceName",
        "Name",
        "LoadBalancerName",
        "DBClusterDescription",
        "DBInstanceDescription",
        "NatGatewayName",
        "VSwitchName",
    )
    hints: dict[str, str] = {}
    for key in hint_keys:
        value = properties.get(key)
        if isinstance(value, str) and value.strip():
            hints[key] = value.strip()
    return hints


def _llm_semantic_plan_schema() -> dict[str, Any]:
    return {
        "node_labels": {
            "required_fields": ["id", "label", "confidence"],
            "confidence_values": ["high", "medium", "low"],
            "max_label_chars": 32,
        },
        "edges": {
            "allowed_kinds": list(SEMANTIC_EDGE_KINDS),
            "required_fields": ["from", "to", "kind", "label", "confidence"],
            "confidence_values": ["high", "medium", "low"],
            "max_label_chars": 18,
        },
        "views": {
            "required_fields": ["id", "title", "purpose", "layout", "nodes", "edges"],
            "detail_required_fields": ["anchors"],
            "optional_fields": {
                "groups": {
                    "item_fields": ["id", "label", "members", "parent"],
                    "description": (
                        "View-only summary nodes. members must be ids from visible_nodes; parent may be a container id."
                    ),
                }
            },
            "recommended_ids": [
                "overview",
                "detail_app",
                "detail_data",
                "detail_network",
                "detail_operations",
                "detail_permissions",
            ],
            "allowed_layouts": ["flat", "contained"],
            "layout_guidance": {
                "flat": "logical drill-down view; use when placement boundaries are not important",
                "contained": (
                    "overview or detail placement view; use when VPC/VSwitch/region/security boundaries matter"
                ),
            },
            "max_overview_nodes": 8,
            "max_detail_nodes": 12,
            "max_edges_per_view": 8,
            "anchor_guidance": "Every detail_<area> view must anchor to one or more ids present in the overview view.",
        },
    }


def _compact_graph_if_needed(
    layers: dict[str, TemplateResource],
    nodes: dict[str, TemplateResource],
    edges: list[GraphEdge],
    node_parent: dict[str, str],
    auxiliary: dict[str, TemplateResource],
    relations: list[ResourceRelation],
    rules: ArchitectureRules,
) -> tuple[
    dict[str, TemplateResource],
    dict[str, TemplateResource],
    list[GraphEdge],
    dict[str, str],
    bool,
    dict[str, str],
]:
    compactable_ids = {node_id for node_id, node in nodes.items() if _is_compact_supported(node)}
    compactable_edges = [edge for edge in edges if edge.from_id in compactable_ids and edge.to_id in compactable_ids]
    root_compactable_ids = {node_id for node_id in compactable_ids if node_id not in node_parent}
    visible_element_count = len(layers) + len(nodes)
    should_compact_by_size = (
        len(compactable_ids) > COMPACT_MIN_VISIBLE_NODES
        or visible_element_count > COMPACT_MIN_VISIBLE_ELEMENTS
        or len(compactable_edges) > COMPACT_MIN_EDGES
        or len(root_compactable_ids) > COMPACT_MIN_ROOT_NODES
    )
    foldable_overview_node_count = _foldable_overview_node_count(nodes, compactable_ids, rules)
    should_compact_by_foldables = foldable_overview_node_count >= COMPACT_MIN_FOLDABLE_NODES or (
        foldable_overview_node_count >= 1 and visible_element_count >= COMPACT_MIN_VISIBLE_ELEMENTS_FOR_SINGLE_FOLDABLE
    )
    if not should_compact_by_size and not should_compact_by_foldables:
        return layers, nodes, edges, node_parent, False, {}

    target_resources = {**layers, **nodes}
    folded_edges, folded_node_ids, folded_source_attachment_labels = _compact_relation_fold_edges(
        {**auxiliary, **nodes},
        target_resources,
        relations,
        rules,
    )
    attachment_labels, attached_marker_ids, attachment_targets = _compact_attachment_labels(
        nodes, auxiliary, relations, rules
    )
    attachment_edges = _compact_attachment_edges(nodes, relations, attachment_targets, rules)
    attachment_labels = _propagate_attachment_labels(attachment_labels, attachment_targets)
    bridge_via_resources = {**auxiliary, **layers, **nodes}
    child_attachment_labels, child_attachment_ids, child_attachment_targets = _compact_child_attachment_labels(
        {**auxiliary, **nodes}, {**target_resources, **auxiliary}, relations, rules
    )
    child_attachment_labels, child_attachment_ids, child_attachment_targets = _filter_unresolved_child_attachments(
        child_attachment_labels,
        child_attachment_ids,
        child_attachment_targets,
        set(target_resources),
    )
    bridge_attachment_labels, bridge_attachment_ids, bridge_attachment_targets = _compact_bridge_attachment_labels(
        nodes, target_resources, bridge_via_resources, relations, rules
    )
    attachment_labels = _merge_attachment_labels(
        attachment_labels,
        child_attachment_labels,
        bridge_attachment_labels,
        folded_source_attachment_labels,
    )
    hidden_target_aliases = {**child_attachment_targets, **bridge_attachment_targets}
    attachment_labels = _propagate_hidden_attachment_labels(attachment_labels, hidden_target_aliases)
    concept_via_ids = {
        node_id
        for node_id, node in nodes.items()
        if any(node.resource_type in rule.via_resource_types for rule in rules.compact_concept_nodes)
    }
    hidden_ids = (
        attached_marker_ids
        | child_attachment_ids
        | bridge_attachment_ids
        | folded_node_ids
        | {
            node_id
            for node_id, node in nodes.items()
            if node_id in compactable_ids and node.short_type in rules.compact_hidden_short_types
        }
    )
    hidden_ids |= {
        node_id
        for node_id, node in nodes.items()
        if node_id in compactable_ids and node.resource_type in rules.compact_orchestration_action_types
    }
    hidden_ids |= {
        node_id
        for node_id, node in nodes.items()
        if node_id in compactable_ids
        and node.resource_type in rules.compact_attachment_marker_types
        and node_id not in attached_marker_ids
    }
    hidden_ids |= concept_via_ids
    compact_nodes = {node_id: node for node_id, node in nodes.items() if node_id not in hidden_ids}
    compact_edges = [edge for edge in edges if edge.from_id not in hidden_ids and edge.to_id not in hidden_ids]
    compact_parent = {node_id: parent for node_id, parent in node_parent.items() if node_id in compact_nodes}
    compact_endpoint_ids = set(compact_nodes) | set(layers)
    compact_edges.extend(
        edge for edge in folded_edges if edge.from_id in compact_endpoint_ids and edge.to_id in compact_endpoint_ids
    )
    compact_edges.extend(
        edge for edge in attachment_edges if edge.from_id in compact_nodes and edge.to_id in compact_nodes
    )
    compact_attachment_labels = {
        node_id: labels for node_id, labels in attachment_labels.items() if node_id in compact_nodes
    }
    compact_layer_labels = {layer_id: labels for layer_id, labels in attachment_labels.items() if layer_id in layers}
    compact_layers = layers
    compact_nodes, compact_parent, layer_summary_ids = _add_layer_attachment_summary_nodes(
        compact_nodes,
        compact_parent,
        compact_layer_labels,
        compact_layers,
        rules,
    )
    if layer_summary_ids:
        compact_edges = _retarget_layer_edge_endpoints(compact_edges, layer_summary_ids)
        hidden_target_aliases = _retarget_endpoint_aliases(hidden_target_aliases, layer_summary_ids)

    aggregate_groups = _aggregate_groups(compact_nodes, compact_edges, compact_parent)
    if not aggregate_groups:
        return (
            compact_layers,
            _with_compact_labels(compact_nodes, rules, compact_attachment_labels),
            _dedupe_edges(compact_edges),
            compact_parent,
            True,
            _visible_endpoint_aliases(hidden_target_aliases, compact_nodes, compact_layers),
        )

    remapped_ids: dict[str, str] = {}
    aggregated_nodes: dict[str, TemplateResource] = {}
    aggregated_parent: dict[str, str] = {}

    for members in aggregate_groups:
        first_node = compact_nodes[members[0]]
        aggregate_id = _aggregate_node_id(
            first_node.resource_type,
            compact_parent.get(first_node.logical_id),
            _semantic_compute_aggregate_role_key(first_node),
        )
        for member_id in members:
            remapped_ids[member_id] = aggregate_id
        aggregated_nodes[aggregate_id] = replace(
            first_node,
            logical_id=aggregate_id,
            properties={},
            label="{} x{}".format(_compact_resource_label(first_node, rules), len(members)),
        )
        parent = compact_parent.get(first_node.logical_id)
        if parent is not None:
            aggregated_parent[aggregate_id] = parent

    for node_id, node in compact_nodes.items():
        if node_id in remapped_ids:
            continue
        aggregated_nodes[node_id] = replace(node, label=_compact_resource_label(node, rules))
        parent = compact_parent.get(node_id)
        if parent is not None:
            aggregated_parent[node_id] = parent

    remapped_attachment_labels = _remap_attachment_labels(
        compact_attachment_labels,
        remapped_ids,
        set(aggregated_nodes),
    )
    aggregated_nodes = _with_attachment_labels(aggregated_nodes, remapped_attachment_labels, rules)
    remapped_edges = [
        replace(
            edge,
            from_id=remapped_ids.get(edge.from_id, edge.from_id),
            to_id=remapped_ids.get(edge.to_id, edge.to_id),
        )
        for edge in compact_edges
    ]
    remapped_aliases = {
        source_id: remapped_ids.get(target_id, target_id)
        for source_id, target_id in _normalize_endpoint_aliases(hidden_target_aliases).items()
    }
    return (
        compact_layers,
        aggregated_nodes,
        _dedupe_edges(remapped_edges),
        aggregated_parent,
        True,
        _visible_endpoint_aliases(remapped_aliases, aggregated_nodes, compact_layers),
    )


def _is_compact_supported(resource: TemplateResource) -> bool:
    return resource.resource_type.startswith("ALIYUN::")


def _foldable_overview_node_count(
    nodes: dict[str, TemplateResource],
    compactable_ids: set[str],
    rules: ArchitectureRules,
) -> int:
    child_attachment_types = {
        resource_type for rule in rules.compact_child_attachments for resource_type in rule.resource_types
    }
    bridge_attachment_types = {
        resource_type for rule in rules.compact_bridge_attachments for resource_type in rule.resource_types
    }
    concept_via_types = {
        resource_type for rule in rules.compact_concept_nodes for resource_type in rule.via_resource_types
    }
    return sum(
        1
        for node_id, node in nodes.items()
        if node_id in compactable_ids
        and (
            node.short_type in rules.compact_hidden_short_types
            or node.resource_type in rules.compact_orchestration_action_types
            or node.resource_type in child_attachment_types
            or node.resource_type in bridge_attachment_types
            or node.resource_type in concept_via_types
        )
    )


def _aggregate_groups(
    nodes: dict[str, TemplateResource],
    edges: list[GraphEdge],
    node_parent: dict[str, str],
) -> list[list[str]]:
    signatures = _node_edge_signatures(nodes, edges)
    buckets: dict[tuple[str, str | None, str, tuple[tuple[str, str], ...]], list[str]] = {}
    for node_id, node in nodes.items():
        if not _is_compact_supported(node):
            continue
        key = (
            node.resource_type,
            node_parent.get(node_id),
            _semantic_compute_aggregate_role_key(node),
            signatures.get(node_id, ()),
        )
        buckets.setdefault(key, []).append(node_id)
    return [members for members in buckets.values() if len(members) >= COMPACT_MIN_AGGREGATE_COUNT]


def _semantic_compute_aggregate_role_key(node: TemplateResource) -> str:
    if node.resource_type not in SEMANTIC_COMPUTE_AGGREGATE_TYPES:
        return ""

    values = [node.logical_id, node.label]
    for property_name in ("InstanceName", "HostName", "Name"):
        property_value = node.properties.get(property_name)
        if isinstance(property_value, str):
            values.append(property_value)
    raw_value = " ".join(values)
    normalized = _normalize_semantic_role_text(raw_value)
    compacted = _compact_semantic_role_text(raw_value)
    for token in SEMANTIC_COMPUTE_ROLE_TOKENS:
        if token in normalized or token in compacted:
            return token
    return ""


def _normalize_semantic_role_text(value: str) -> str:
    words = re.findall(r"[a-z0-9]+", re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", value).lower())
    return " ".join(words)


def _compact_semantic_role_text(value: str) -> str:
    return "".join(re.findall(r"[a-z0-9]+", value.lower()))


def _node_edge_signatures(
    nodes: dict[str, TemplateResource],
    edges: list[GraphEdge],
) -> dict[str, tuple[tuple[str, str], ...]]:
    signatures: dict[str, list[tuple[str, str]]] = {node_id: [] for node_id in nodes}
    for edge in edges:
        if edge.from_id in signatures:
            signatures[edge.from_id].append((f"out:{edge.style}:{edge.label or ''}", edge.to_id))
        if edge.to_id in signatures:
            signatures[edge.to_id].append((f"in:{edge.style}:{edge.label or ''}", edge.from_id))
    return {node_id: tuple(sorted(signature)) for node_id, signature in signatures.items()}


def _compact_relation_fold_edges(
    via_resources: dict[str, TemplateResource],
    endpoint_resources: dict[str, TemplateResource],
    relations: list[ResourceRelation],
    rules: ArchitectureRules,
) -> tuple[list[GraphEdge], set[str], dict[str, tuple[str, ...]]]:
    folded_edges: list[GraphEdge] = []
    folded_resource_ids: set[str] = set()
    source_attachment_labels: dict[str, list[str]] = {}
    by_source = _relations_by_source(relations)
    for resource in via_resources.values():
        source_relations = by_source.get(resource.logical_id, [])
        for fold in rules.compact_relation_folds:
            if resource.resource_type not in fold.via_resource_types:
                continue
            source_id = _relation_target(source_relations, fold.source_property, endpoint_resources)
            target_id = _relation_target(source_relations, fold.target_property, endpoint_resources)
            if source_id is None or target_id is None:
                continue
            label = _localized_rule_label(fold.edge_label) or _compact_resource_label(resource, rules)
            if fold.render_as == "source_attachment":
                source_attachment_labels.setdefault(source_id, []).append(label)
            else:
                folded_edges.append(
                    GraphEdge(
                        from_id=source_id,
                        to_id=target_id,
                        style=fold.edge_style,
                        label=label,
                    )
                )
            folded_resource_ids.add(resource.logical_id)
    return (
        _dedupe_edges(folded_edges),
        folded_resource_ids,
        {node_id: tuple(labels) for node_id, labels in source_attachment_labels.items()},
    )


def _add_compact_concept_nodes(
    resources: dict[str, TemplateResource],
    layers: dict[str, TemplateResource],
    layer_parent: dict[str, str],
    nodes: dict[str, TemplateResource],
    edges: list[GraphEdge],
    node_parent: dict[str, str],
    relations: list[ResourceRelation],
    rules: ArchitectureRules,
) -> tuple[
    dict[str, TemplateResource],
    dict[str, str],
    dict[str, TemplateResource],
    list[GraphEdge],
    dict[str, str],
    tuple[ConceptNodeInstance, ...],
    tuple[ConceptGroupInstance, ...],
]:
    if not rules.compact_concept_nodes:
        return layers, layer_parent, nodes, edges, node_parent, (), ()

    concept_nodes: list[ConceptNodeInstance] = []
    concept_groups: list[ConceptGroupInstance] = []
    expanded_layers = dict(layers)
    expanded_layer_parent = dict(layer_parent)
    expanded_nodes = dict(nodes)
    expanded_edges = list(edges)
    expanded_parent = dict(node_parent)
    by_source = _relations_by_source(relations)

    for via_resource in resources.values():
        source_relations = by_source.get(via_resource.logical_id, [])
        for rule in rules.compact_concept_nodes:
            if via_resource.resource_type not in rule.via_resource_types:
                continue
            controller_id = _relation_target(source_relations, rule.controller_property, expanded_nodes)
            source_id = _relation_target(source_relations, rule.source_property, expanded_nodes)
            if controller_id is None or source_id is None:
                continue
            concept_id = _unique_node_id(_safe_mermaid_id(f"{controller_id}{rule.id_suffix}"), expanded_nodes)
            concept_label = _localized_rule_label(rule.label) or rule.resource_type
            expanded_nodes[concept_id] = TemplateResource(
                logical_id=concept_id,
                resource_type=rule.resource_type,
                properties={},
                meta=None,
                label=concept_label,
            )
            parent_id = expanded_parent.get(controller_id)
            if parent_id is not None:
                expanded_parent[concept_id] = parent_id
            role_ids = {"controller": controller_id, "source": source_id, "concept": concept_id}
            if rule.controller_edge is not None:
                expanded_edges.append(
                    GraphEdge(
                        from_id=controller_id,
                        to_id=_concept_edge_target_id(rule.controller_edge, role_ids),
                        style=rule.controller_edge.style,
                        label=_localized_rule_label(rule.controller_edge.label),
                    )
                )
            if rule.source_edge is not None:
                expanded_edges.append(
                    GraphEdge(
                        from_id=source_id,
                        to_id=_concept_edge_target_id(rule.source_edge, role_ids),
                        style=rule.source_edge.style,
                        label=_localized_rule_label(rule.source_edge.label),
                    )
                )
            group_id: str | None = None
            if rule.group is not None:
                group_id = _add_concept_group(
                    rule.group,
                    role_ids,
                    expanded_layers,
                    expanded_layer_parent,
                    expanded_nodes,
                    expanded_parent,
                    concept_groups,
                )
            concept_nodes.append(
                ConceptNodeInstance(
                    node_id=concept_id,
                    label=concept_label,
                    resource_type=rule.resource_type,
                    controller_id=controller_id,
                    source_id=source_id,
                    via_id=via_resource.logical_id,
                    runtime_source_id=source_id,
                    group_id=group_id,
                )
            )

    return (
        expanded_layers,
        expanded_layer_parent,
        expanded_nodes,
        _dedupe_edges(expanded_edges),
        expanded_parent,
        tuple(concept_nodes),
        tuple(concept_groups),
    )


def _add_ack_application_concept_nodes(
    resources: dict[str, TemplateResource],
    nodes: dict[str, TemplateResource],
    edges: list[GraphEdge],
    node_parent: dict[str, str],
    relations: list[ResourceRelation],
) -> tuple[dict[str, TemplateResource], list[GraphEdge], dict[str, str]]:
    cluster_categories = _ack_application_categories_by_cluster(resources, nodes, relations)
    if not cluster_categories:
        return nodes, edges, node_parent

    expanded_nodes = dict(nodes)
    expanded_edges = list(edges)
    expanded_parent = dict(node_parent)
    by_source = _relations_by_source(relations)
    concept_ids_by_cluster: dict[str, dict[str, str]] = {}

    for cluster_id, categories in cluster_categories.items():
        if not _should_expose_ack_application_concepts(categories):
            continue
        concept_ids: dict[str, str] = {}
        for category in ("workload", "service", "ingress", "autoscaler"):
            entries = categories.get(category)
            if not entries:
                continue
            concept_id = _unique_node_id(
                _safe_mermaid_id(f"{cluster_id}{ACK_APPLICATION_CONCEPT_SUFFIXES[category]}"),
                expanded_nodes,
            )
            expanded_nodes[concept_id] = TemplateResource(
                logical_id=concept_id,
                resource_type=ACK_APPLICATION_CONCEPT_TYPES[category],
                properties={
                    "cluster": cluster_id,
                    "category": category,
                    "sources": tuple(_dedupe([str(entry["source"]) for entry in entries])),
                    "kinds": tuple(_dedupe([str(entry["kind"]) for entry in entries])),
                    "names": tuple(_dedupe([str(entry["name"]) for entry in entries if entry.get("name")])),
                },
                meta=None,
                label=_localized_rule_label(ACK_APPLICATION_CONCEPT_LABELS[category])
                or ACK_APPLICATION_CONCEPT_TYPES[category],
            )
            parent_id = expanded_parent.get(cluster_id)
            if parent_id is not None:
                expanded_parent[concept_id] = parent_id
            concept_ids[category] = concept_id
        concept_ids_by_cluster[cluster_id] = concept_ids

    if not concept_ids_by_cluster:
        return nodes, edges, node_parent

    for concept_ids in concept_ids_by_cluster.values():
        ingress_id = concept_ids.get("ingress")
        service_id = concept_ids.get("service")
        workload_id = concept_ids.get("workload")
        autoscaler_id = concept_ids.get("autoscaler")
        if ingress_id is not None and service_id is not None:
            expanded_edges.append(
                GraphEdge(
                    from_id=ingress_id,
                    to_id=service_id,
                    style="solid_arrow",
                    label=_localized_rule_label(ACK_APPLICATION_CONCEPT_EDGE_LABELS["ingress_to_service"]),
                )
            )
        if service_id is not None and workload_id is not None:
            expanded_edges.append(
                GraphEdge(
                    from_id=service_id,
                    to_id=workload_id,
                    style="solid_arrow",
                    label=_localized_rule_label(ACK_APPLICATION_CONCEPT_EDGE_LABELS["service_to_workload"]),
                )
            )
        if autoscaler_id is not None and workload_id is not None:
            expanded_edges.append(
                GraphEdge(
                    from_id=autoscaler_id,
                    to_id=workload_id,
                    style="dotted_arrow",
                    label=_localized_rule_label(ACK_APPLICATION_CONCEPT_EDGE_LABELS["autoscaler_to_workload"]),
                )
            )

    for resource in resources.values():
        if resource.resource_type not in ACK_HELM_APPLICATION_TYPES:
            continue
        cluster_id = _relation_target(by_source.get(resource.logical_id, []), "ClusterId", nodes)
        autoscaler_id = concept_ids_by_cluster.get(cluster_id or "", {}).get("autoscaler")
        if autoscaler_id is None or resource.logical_id not in expanded_nodes:
            continue
        expanded_edges.append(
            GraphEdge(
                from_id=resource.logical_id,
                to_id=autoscaler_id,
                style="dotted_open",
                label=_localized_rule_label(ACK_APPLICATION_CONCEPT_EDGE_LABELS["metrics_adapter"]),
            )
        )

    for cluster_id, categories in cluster_categories.items():
        autoscaler_id = concept_ids_by_cluster.get(cluster_id, {}).get("autoscaler")
        if autoscaler_id is None:
            continue
        for entry in categories.get("autoscaler", ()):
            for target_id in entry.get("template_refs", ()):
                if target_id in expanded_nodes:
                    expanded_edges.append(
                        GraphEdge(
                            from_id=target_id,
                            to_id=autoscaler_id,
                            style="dotted_open",
                            label=_localized_rule_label(ACK_APPLICATION_CONCEPT_EDGE_LABELS["external_metrics"]),
                        )
                    )

    for resource in resources.values():
        if resource.resource_type not in ACK_HELM_APPLICATION_TYPES:
            continue
        cluster_id = _relation_target(by_source.get(resource.logical_id, []), "ClusterId", nodes)
        workload_id = concept_ids_by_cluster.get(cluster_id or "", {}).get("workload")
        if workload_id is None:
            continue
        for target_id in _extract_resource_refs(resource.properties.get("ChartValues"), expanded_nodes):
            target = expanded_nodes.get(target_id)
            label = _ack_application_data_dependency_label(target.resource_type if target else "")
            if label is None:
                continue
            expanded_edges.append(
                GraphEdge(
                    from_id=workload_id,
                    to_id=target_id,
                    style="solid_arrow",
                    label=label,
                )
            )

    return expanded_nodes, _dedupe_edges(expanded_edges), expanded_parent


def _ack_application_categories_by_cluster(
    resources: dict[str, TemplateResource],
    visible_nodes: dict[str, TemplateResource],
    relations: list[ResourceRelation],
) -> dict[str, dict[str, tuple[dict[str, Any], ...]]]:
    by_source = _relations_by_source(relations)
    categories_by_cluster: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for resource in resources.values():
        if resource.resource_type != ACK_CLUSTER_APPLICATION_TYPE:
            continue
        cluster_id = _relation_target(by_source.get(resource.logical_id, []), "ClusterId", visible_nodes)
        if cluster_id is None:
            continue
        for entry in _ack_cluster_application_manifest_entries(resource, resources, include_documents=True):
            category = _ack_application_concept_category(str(entry["kind"]))
            if category is None:
                continue
            categories_by_cluster.setdefault(cluster_id, {}).setdefault(category, []).append(
                {
                    "cluster": cluster_id,
                    **entry,
                }
            )
            if category == "ingress":
                for service_entry in _ack_ingress_backend_service_entries(entry):
                    categories_by_cluster.setdefault(cluster_id, {}).setdefault("service", []).append(
                        {
                            "cluster": cluster_id,
                            **service_entry,
                        }
                    )
    for resource in resources.values():
        if resource.resource_type not in ACK_HELM_APPLICATION_TYPES:
            continue
        cluster_id = _relation_target(by_source.get(resource.logical_id, []), "ClusterId", visible_nodes)
        if cluster_id is None:
            continue
        for entry in _ack_helm_application_workload_entries(resource, resources):
            categories_by_cluster.setdefault(cluster_id, {}).setdefault("workload", []).append(
                {
                    "cluster": cluster_id,
                    **entry,
                }
            )
    return {
        cluster_id: {category: tuple(entries) for category, entries in categories.items()}
        for cluster_id, categories in categories_by_cluster.items()
    }


def _ack_ingress_backend_service_entries(entry: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    document = entry.get("document")
    if not isinstance(document, dict):
        return ()
    service_names: list[str] = []
    for service in _find_kubernetes_ingress_backend_services(document.get("spec")):
        name = service.get("name")
        if isinstance(name, str) and name:
            service_names.append(name)
    return tuple(
        {
            "source": str(entry["source"]),
            "kind": "Service",
            "name": name,
            "label": _localized_rule_label(KUBERNETES_KIND_ATTACHMENT_LABELS.get("Service")) or "Service",
            "template_refs": tuple(entry.get("template_refs", ())),
        }
        for name in _dedupe(service_names)
    )


def _find_kubernetes_ingress_backend_services(value: Any) -> list[dict[str, Any]]:
    services: list[dict[str, Any]] = []
    if isinstance(value, dict):
        service = value.get("service")
        if isinstance(service, dict):
            services.append(service)
        for child in value.values():
            services.extend(_find_kubernetes_ingress_backend_services(child))
    elif isinstance(value, list):
        for item in value:
            services.extend(_find_kubernetes_ingress_backend_services(item))
    return services


def _ack_helm_application_workload_entries(
    resource: TemplateResource,
    resources: dict[str, TemplateResource],
) -> tuple[dict[str, Any], ...]:
    wait_until = resource.properties.get("WaitUntil")
    if not isinstance(wait_until, list):
        wait_until = []
    entries: list[dict[str, Any]] = []
    for item in wait_until:
        if not isinstance(item, dict):
            continue
        kind = item.get("Kind")
        if not isinstance(kind, str) or _ack_application_concept_category(kind) != "workload":
            continue
        name = item.get("Name")
        entries.append(
            {
                "source": resource.logical_id,
                "kind": kind,
                "name": name if isinstance(name, str) else None,
                "label": _localized_rule_label(KUBERNETES_KIND_ATTACHMENT_LABELS.get(kind))
                or _localized_rule_label(ACK_APPLICATION_CONCEPT_LABELS["workload"])
                or kind,
                "template_refs": _extract_resource_refs(resource.properties.get("ChartValues"), resources),
            }
        )
    if entries:
        return tuple(entries)
    release_name = resource.properties.get("ReleaseName")
    if isinstance(release_name, str) and release_name:
        return (
            {
                "source": resource.logical_id,
                "kind": "Deployment",
                "name": release_name,
                "label": _localized_rule_label(KUBERNETES_KIND_ATTACHMENT_LABELS.get("Deployment"))
                or _localized_rule_label(ACK_APPLICATION_CONCEPT_LABELS["workload"])
                or "Deployment",
                "template_refs": _extract_resource_refs(resource.properties.get("ChartValues"), resources),
            },
        )
    return ()


def _should_expose_ack_application_concepts(categories: dict[str, tuple[dict[str, Any], ...]]) -> bool:
    semantic_categories = {"workload", "service", "ingress", "autoscaler"}
    present = semantic_categories.intersection(category for category, entries in categories.items() if entries)
    return len(present) >= 2 or "autoscaler" in present


def _ack_application_concept_category(kind: str) -> str | None:
    if kind in ACK_WORKLOAD_KINDS:
        return "workload"
    if kind in ACK_SERVICE_KINDS:
        return "service"
    if kind in ACK_INGRESS_KINDS:
        return "ingress"
    if kind in ACK_AUTOSCALER_KINDS:
        return "autoscaler"
    return None


def _ack_application_data_dependency_label(resource_type: str) -> str | None:
    if "REDIS::" in resource_type:
        return _localized_rule_label(ACK_APPLICATION_CONCEPT_EDGE_LABELS["cache_access"])
    if "GPDB::" in resource_type or "ADB" in resource_type or "AnalyticDB" in resource_type:
        return _localized_rule_label(ACK_APPLICATION_CONCEPT_EDGE_LABELS["vector_search"])
    if (
        "RDS::" in resource_type
        or "POLARDB::" in resource_type
        or "MongoDB::" in resource_type
        or "DRDS::" in resource_type
        or "DBInstance" in resource_type
    ):
        return _localized_rule_label(ACK_APPLICATION_CONCEPT_EDGE_LABELS["database_access"])
    if "OSS::" in resource_type or "NAS::" in resource_type:
        return _localized_rule_label(ACK_APPLICATION_CONCEPT_EDGE_LABELS["data_dependency"])
    return None


def _concept_edge_target_id(edge: ConceptEdgeRule, role_ids: dict[str, str]) -> str:
    return role_ids.get(edge.target, role_ids["concept"])


def _add_concept_group(
    group_rule: CompactConceptGroup,
    role_ids: dict[str, str],
    layers: dict[str, TemplateResource],
    layer_parent: dict[str, str],
    nodes: dict[str, TemplateResource],
    node_parent: dict[str, str],
    concept_groups: list[ConceptGroupInstance],
) -> str | None:
    member_ids = tuple(_dedupe([role_ids[role] for role in group_rule.members if role in role_ids]))
    if len(member_ids) < 2:
        return None
    parent_ids = {node_parent.get(member_id) for member_id in member_ids}
    if len(parent_ids) != 1:
        return None
    group_id = _unique_layer_id(_safe_mermaid_id(f"{role_ids['controller']}{group_rule.id_suffix}"), layers, nodes)
    group_label = _localized_rule_label(group_rule.label) or group_rule.resource_type
    layers[group_id] = TemplateResource(
        logical_id=group_id,
        resource_type=group_rule.resource_type,
        properties={},
        meta=None,
        label=group_label,
    )
    parent_id = next(iter(parent_ids))
    if parent_id is not None:
        layer_parent[group_id] = parent_id
    for member_id in member_ids:
        node_parent[member_id] = group_id
    concept_groups.append(
        ConceptGroupInstance(
            group_id=group_id,
            label=group_label,
            resource_type=group_rule.resource_type,
            member_ids=member_ids,
            rewrite_edge_kinds=group_rule.rewrite_edge_kinds,
            parent_id=parent_id,
        )
    )
    return group_id


def _unique_layer_id(
    base_id: str,
    layers: dict[str, TemplateResource],
    nodes: dict[str, TemplateResource],
) -> str:
    if base_id not in layers and base_id not in nodes:
        return base_id
    index = 2
    while f"{base_id}{index}" in layers or f"{base_id}{index}" in nodes:
        index += 1
    return f"{base_id}{index}"


def _unique_node_id(base_id: str, nodes: dict[str, TemplateResource]) -> str:
    if base_id not in nodes:
        return base_id
    index = 2
    while f"{base_id}{index}" in nodes:
        index += 1
    return f"{base_id}{index}"


def _relation_target(
    relations: list[ResourceRelation],
    property_name: str,
    resources: dict[str, TemplateResource],
) -> str | None:
    for relation in relations:
        if relation.property_name == property_name and relation.target_id in resources:
            return relation.target_id
    return None


def _relation_targets(
    relations: list[ResourceRelation],
    property_names: tuple[str, ...],
    nodes: dict[str, TemplateResource],
    target_types: tuple[str, ...] = (),
) -> tuple[str, ...]:
    property_name_set = set(property_names)
    target_type_set = set(target_types)
    targets: list[str] = []
    for relation in relations:
        if relation.property_name not in property_name_set or relation.target_id not in nodes:
            continue
        if target_type_set and nodes[relation.target_id].resource_type not in target_type_set:
            continue
        targets.append(relation.target_id)
    return tuple(_dedupe(targets))


def _compact_attachment_labels(
    nodes: dict[str, TemplateResource],
    auxiliary: dict[str, TemplateResource],
    relations: list[ResourceRelation],
    rules: ArchitectureRules,
) -> tuple[dict[str, tuple[str, ...]], set[str], dict[str, tuple[str, ...]]]:
    labels: dict[str, list[str]] = {}
    attached_marker_ids: set[str] = set()
    attachment_targets: dict[str, list[str]] = {}
    by_source = _relations_by_source(relations)
    for resource in auxiliary.values():
        source_relations = by_source.get(resource.logical_id, [])
        main_ref = _main_resource_ref(resource, source_relations)
        if main_ref is None or main_ref not in nodes:
            continue
        main_resource = nodes[main_ref]
        if not _is_compact_attachment_marker(main_resource, rules):
            continue
        target_ids = _dedupe(
            [
                relation.target_id
                for relation in source_relations
                if relation.target_id != main_ref and relation.target_id in nodes
            ]
        )
        if not target_ids:
            continue
        marker_label = _compact_resource_label(main_resource, rules)
        for target_id in target_ids:
            labels.setdefault(target_id, []).append(marker_label)
            attachment_targets.setdefault(main_ref, []).append(target_id)
        attached_marker_ids.add(main_ref)
    return (
        {node_id: tuple(node_labels) for node_id, node_labels in labels.items()},
        attached_marker_ids,
        {node_id: tuple(_dedupe(target_ids)) for node_id, target_ids in attachment_targets.items()},
    )


def _propagate_attachment_labels(
    attachment_labels: dict[str, tuple[str, ...]],
    attachment_targets: dict[str, tuple[str, ...]],
) -> dict[str, tuple[str, ...]]:
    propagated: dict[str, list[str]] = {}
    for node_id, labels in attachment_labels.items():
        for target_id in _ultimate_attachment_targets(node_id, attachment_targets):
            propagated.setdefault(target_id, []).extend(labels)
    return {node_id: tuple(labels) for node_id, labels in propagated.items()}


def _ultimate_attachment_targets(
    node_id: str,
    attachment_targets: dict[str, tuple[str, ...]],
    seen: frozenset[str] = frozenset(),
) -> tuple[str, ...]:
    if node_id in seen or node_id not in attachment_targets:
        return (node_id,)
    targets: list[str] = []
    for target_id in attachment_targets[node_id]:
        targets.extend(_ultimate_attachment_targets(target_id, attachment_targets, seen | {node_id}))
    return tuple(_dedupe(targets))


def _compact_attachment_edges(
    nodes: dict[str, TemplateResource],
    relations: list[ResourceRelation],
    attachment_targets: dict[str, tuple[str, ...]],
    rules: ArchitectureRules,
) -> list[GraphEdge]:
    if not rules.compact_attachment_edges:
        return []
    edges: list[GraphEdge] = []
    by_source = _relations_by_source(relations)
    for node in nodes.values():
        source_relations = by_source.get(node.logical_id, [])
        for rule in rules.compact_attachment_edges:
            if node.resource_type not in rule.resource_types:
                continue
            edges.extend(_compact_attachment_edges_for_rule(source_relations, nodes, attachment_targets, rule))
    return _dedupe_edges(edges)


def _compact_attachment_edges_for_rule(
    relations: list[ResourceRelation],
    nodes: dict[str, TemplateResource],
    attachment_targets: dict[str, tuple[str, ...]],
    rule: CompactAttachmentEdge,
) -> list[GraphEdge]:
    source_ids = _relation_targets(relations, rule.source_properties, nodes, rule.source_types)
    marker_ids = _relation_targets(relations, rule.marker_properties, nodes, rule.marker_types)
    if not source_ids or not marker_ids:
        return []
    label = _localized_rule_label(rule.edge_label)
    edges: list[GraphEdge] = []
    for source_id in source_ids:
        for marker_id in marker_ids:
            for target_id in _ultimate_attachment_targets(marker_id, attachment_targets):
                if target_id not in nodes:
                    continue
                edges.append(GraphEdge(from_id=source_id, to_id=target_id, style=rule.edge_style, label=label))
    return edges


def _compact_child_attachment_labels(
    resources: dict[str, TemplateResource],
    target_resources: dict[str, TemplateResource],
    relations: list[ResourceRelation],
    rules: ArchitectureRules,
) -> tuple[dict[str, tuple[str, ...]], set[str], dict[str, str]]:
    labels: dict[str, list[str]] = {}
    child_ids: set[str] = set()
    attachment_targets: dict[str, str] = {}
    by_source = _relations_by_source(relations)
    for resource in resources.values():
        source_relations = by_source.get(resource.logical_id, [])
        if _has_compact_marker_main_resource(resource, source_relations, target_resources, rules):
            continue
        for rule in rules.compact_child_attachments:
            if resource.resource_type not in rule.resource_types:
                continue
            if (
                _is_auxiliary(resource, rules)
                and _is_compact_bridge_via_resource(resource, rules)
                and not rule.keep_when_bridge_via
            ):
                continue
            target_id = _child_attachment_target(
                source_relations, target_resources, rule.target_properties, rule.target_types
            )
            if target_id is None:
                continue
            labels.setdefault(target_id, []).extend(_compact_child_attachment_marker_labels(resource, rule, rules))
            child_ids.add(resource.logical_id)
            attachment_targets[resource.logical_id] = target_id
            break
    return {node_id: tuple(node_labels) for node_id, node_labels in labels.items()}, child_ids, attachment_targets


def _compact_child_attachment_marker_labels(
    resource: TemplateResource,
    rule: Any,
    rules: ArchitectureRules,
) -> tuple[str, ...]:
    if resource.resource_type == ACK_CLUSTER_APPLICATION_TYPE:
        manifest_labels = _ack_cluster_application_manifest_labels(resource)
        if manifest_labels:
            return manifest_labels
    return (_localized_rule_label(rule.label) or _compact_resource_label(resource, rules),)


def _ack_cluster_application_manifest_labels(resource: TemplateResource) -> tuple[str, ...]:
    labels: list[str] = []
    for entry in _ack_cluster_application_manifest_entries(resource, {}):
        kind = str(entry["kind"])
        rule_label = KUBERNETES_KIND_ATTACHMENT_LABELS.get(kind)
        label = _localized_rule_label(rule_label) if rule_label is not None else kind
        labels.append(label or kind)
    return tuple(label for label in labels if label)


def _ack_application_context(
    resources: dict[str, TemplateResource],
    relations: list[ResourceRelation],
) -> list[dict[str, Any]]:
    by_source = _relations_by_source(relations)
    context: list[dict[str, Any]] = []
    for resource in resources.values():
        if resource.resource_type != ACK_CLUSTER_APPLICATION_TYPE:
            continue
        cluster_id = _relation_target(by_source.get(resource.logical_id, []), "ClusterId", resources)
        for entry in _ack_cluster_application_manifest_entries(resource, resources):
            context.append({"cluster": cluster_id, **entry})
    for resource in resources.values():
        if resource.resource_type not in ACK_HELM_APPLICATION_TYPES:
            continue
        cluster_id = _relation_target(by_source.get(resource.logical_id, []), "ClusterId", resources)
        for entry in _ack_helm_application_workload_entries(resource, resources):
            context.append({"cluster": cluster_id, **entry})
    return context


def _ack_cluster_application_manifest_entries(
    resource: TemplateResource,
    resources: dict[str, TemplateResource],
    *,
    include_documents: bool = False,
) -> tuple[dict[str, Any], ...]:
    yaml_content = _ros_string_value(resource.properties.get("YamlContent"))
    if yaml_content is None:
        return ()
    entries: list[dict[str, Any]] = []
    template_refs = _extract_template_refs_from_text(yaml_content, resources)
    for document in _kubernetes_manifest_documents(yaml_content):
        kind = document.get("kind")
        if not isinstance(kind, str) or not kind:
            continue
        rule_label = KUBERNETES_KIND_ATTACHMENT_LABELS.get(kind)
        metadata = document.get("metadata") if isinstance(document.get("metadata"), dict) else {}
        name = metadata.get("name") if isinstance(metadata, dict) else None
        entry: dict[str, Any] = {
            "source": resource.logical_id,
            "kind": kind,
            "name": name if isinstance(name, str) else None,
            "label": _localized_rule_label(rule_label) if rule_label is not None else kind,
            "template_refs": template_refs,
        }
        if include_documents:
            entry["document"] = document
        entries.append(entry)
    return tuple(entries)


def _kubernetes_manifest_documents(yaml_content: str) -> tuple[dict[str, Any], ...]:
    documents: list[dict[str, Any]] = []
    try:
        for document in yaml.safe_load_all(yaml_content):
            if isinstance(document, dict):
                documents.append(document)
    except yaml.YAMLError:
        return ()
    return tuple(documents)


def _extract_template_refs_from_text(text: str, resources: dict[str, TemplateResource]) -> list[str]:
    if not resources:
        return []
    refs: list[str] = []
    for match in re.finditer(r"\$\{([A-Za-z][A-Za-z0-9_]*)(?:[.][^}]*)?\}", text):
        resource_id = match.group(1)
        if resource_id in resources:
            refs.append(resource_id)
    return _dedupe(refs)


def _ros_string_value(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        fn_sub = value.get("Fn::Sub")
        if isinstance(fn_sub, str):
            return fn_sub
        if isinstance(fn_sub, list) and fn_sub and isinstance(fn_sub[0], str):
            return fn_sub[0]
        fn_join = value.get("Fn::Join")
        if isinstance(fn_join, list) and len(fn_join) >= 2:
            delimiter = fn_join[0] if isinstance(fn_join[0], str) else ""
            if not isinstance(fn_join[1], list):
                return None
            parts = [_ros_string_value(item) for item in fn_join[1]]
            joined = delimiter.join(part for part in parts if part is not None)
            return joined or None
    return None


def _kubernetes_manifest_kinds(yaml_content: str) -> tuple[str, ...]:
    return tuple(
        str(document["kind"]) for document in _kubernetes_manifest_documents(yaml_content) if document.get("kind")
    )


def _filter_unresolved_child_attachments(
    attachment_labels: dict[str, tuple[str, ...]],
    child_ids: set[str],
    attachment_targets: dict[str, str],
    visible_target_ids: set[str],
) -> tuple[dict[str, tuple[str, ...]], set[str], dict[str, str]]:
    if not attachment_targets:
        return attachment_labels, child_ids, attachment_targets

    normalized_targets = _normalize_endpoint_aliases(attachment_targets)
    resolved_source_ids = {
        source_id for source_id, target_id in normalized_targets.items() if target_id in visible_target_ids
    }
    filtered_labels = {
        target_id: labels
        for target_id, labels in attachment_labels.items()
        if target_id in visible_target_ids or target_id in resolved_source_ids
    }
    return (
        filtered_labels,
        child_ids & resolved_source_ids,
        {
            source_id: target_id
            for source_id, target_id in attachment_targets.items()
            if source_id in resolved_source_ids
        },
    )


def _has_compact_marker_main_resource(
    resource: TemplateResource,
    relations: list[ResourceRelation],
    target_resources: dict[str, TemplateResource],
    rules: ArchitectureRules,
) -> bool:
    main_ref = _main_resource_ref(resource, relations)
    if main_ref is None:
        return False
    main_resource = target_resources.get(main_ref)
    return main_resource is not None and _is_compact_attachment_marker(main_resource, rules)


def _is_compact_bridge_via_resource(resource: TemplateResource, rules: ArchitectureRules) -> bool:
    return any(resource.resource_type in rule.via_resource_types for rule in rules.compact_bridge_attachments)


def _compact_bridge_attachment_labels(
    nodes: dict[str, TemplateResource],
    target_resources: dict[str, TemplateResource],
    via_resources: dict[str, TemplateResource],
    relations: list[ResourceRelation],
    rules: ArchitectureRules,
) -> tuple[dict[str, tuple[str, ...]], set[str], dict[str, str]]:
    labels: dict[str, list[str]] = {}
    child_ids: set[str] = set()
    attachment_targets: dict[str, str] = {}
    by_source = _relations_by_source(relations)
    for node in nodes.values():
        source_relations = by_source.get(node.logical_id, [])
        for rule in rules.compact_bridge_attachments:
            if node.resource_type not in rule.resource_types:
                continue
            source_ids = _bridge_source_ids(node.logical_id, source_relations, rule.source_properties, nodes)
            target_id = _bridge_attachment_target(source_ids, target_resources, via_resources, by_source, rule)
            if target_id is None:
                continue
            labels.setdefault(target_id, []).append(
                _localized_rule_label(rule.label) or _compact_resource_label(node, rules)
            )
            child_ids.add(node.logical_id)
            attachment_targets[node.logical_id] = target_id
            break
    return {node_id: tuple(node_labels) for node_id, node_labels in labels.items()}, child_ids, attachment_targets


def _bridge_source_ids(
    node_id: str,
    relations: list[ResourceRelation],
    property_names: tuple[str, ...],
    nodes: dict[str, TemplateResource],
) -> tuple[str, ...]:
    if not property_names:
        return (node_id,)
    property_name_set = set(property_names)
    return tuple(
        _dedupe(
            [
                relation.target_id
                for relation in relations
                if relation.property_name in property_name_set and relation.target_id in nodes
            ]
        )
    )


def _bridge_attachment_target(
    source_ids: tuple[str, ...],
    target_resources: dict[str, TemplateResource],
    via_resources: dict[str, TemplateResource],
    relations_by_source: dict[str, list[ResourceRelation]],
    rule: CompactBridgeAttachment,
) -> str | None:
    if not source_ids:
        return None
    source_id_set = set(source_ids)
    via_type_set = set(rule.via_resource_types)
    via_source_property_set = set(rule.via_source_properties)
    for via_node in via_resources.values():
        if via_node.resource_type not in via_type_set:
            continue
        via_relations = relations_by_source.get(via_node.logical_id, [])
        if not any(
            relation.property_name in via_source_property_set and relation.target_id in source_id_set
            for relation in via_relations
        ):
            continue
        if rule.target_self and via_node.resource_type in set(rule.target_types):
            return via_node.logical_id
        target_id = _child_attachment_target(
            via_relations,
            target_resources,
            rule.via_target_properties,
            rule.target_types,
        )
        if target_id is not None:
            return target_id
    return None


def _child_attachment_target(
    relations: list[ResourceRelation],
    nodes: dict[str, TemplateResource],
    property_names: tuple[str, ...],
    target_types: tuple[str, ...],
) -> str | None:
    target_type_set = set(target_types)
    property_name_set = set(property_names)
    for relation in relations:
        if relation.property_name not in property_name_set or relation.target_id not in nodes:
            continue
        if target_type_set and nodes[relation.target_id].resource_type not in target_type_set:
            continue
        return relation.target_id
    return None


def _is_compact_attachment_marker(resource: TemplateResource, rules: ArchitectureRules) -> bool:
    return resource.resource_type in rules.compact_attachment_marker_types


def _merge_attachment_labels(
    *attachment_label_groups: dict[str, tuple[str, ...]],
) -> dict[str, tuple[str, ...]]:
    merged: dict[str, list[str]] = {}
    for attachment_labels in attachment_label_groups:
        for node_id, labels in attachment_labels.items():
            merged.setdefault(node_id, []).extend(labels)
    return {node_id: tuple(labels) for node_id, labels in merged.items()}


def _propagate_hidden_attachment_labels(
    attachment_labels: dict[str, tuple[str, ...]],
    endpoint_aliases: dict[str, str],
) -> dict[str, tuple[str, ...]]:
    if not endpoint_aliases:
        return attachment_labels
    propagated: dict[str, list[str]] = {}
    normalized_aliases = _normalize_endpoint_aliases(endpoint_aliases)
    for node_id, labels in attachment_labels.items():
        propagated.setdefault(normalized_aliases.get(node_id, node_id), []).extend(labels)
    return {node_id: tuple(labels) for node_id, labels in propagated.items()}


def _normalize_endpoint_aliases(endpoint_aliases: dict[str, str]) -> dict[str, str]:
    return {source_id: _ultimate_endpoint_alias_target(source_id, endpoint_aliases) for source_id in endpoint_aliases}


def _ultimate_endpoint_alias_target(
    source_id: str,
    endpoint_aliases: dict[str, str],
    seen: frozenset[str] = frozenset(),
) -> str:
    target_id = endpoint_aliases.get(source_id)
    if target_id is None or target_id in seen:
        return source_id
    return _ultimate_endpoint_alias_target(target_id, endpoint_aliases, seen | {source_id})


def _visible_endpoint_aliases(
    endpoint_aliases: dict[str, str],
    nodes: dict[str, TemplateResource],
    layers: dict[str, TemplateResource],
) -> dict[str, str]:
    visible_ids = set(nodes) | set(layers)
    return {
        source_id: target_id
        for source_id, target_id in _normalize_endpoint_aliases(endpoint_aliases).items()
        if source_id not in visible_ids and target_id in visible_ids
    }


def _with_compact_labels(
    nodes: dict[str, TemplateResource],
    rules: ArchitectureRules,
    attachment_labels: dict[str, tuple[str, ...]] | None = None,
) -> dict[str, TemplateResource]:
    compact_nodes = {
        node_id: replace(node, label=_compact_resource_label(node, rules)) for node_id, node in nodes.items()
    }
    return _with_attachment_labels(compact_nodes, attachment_labels or {}, rules)


def _with_attachment_labels(
    nodes: dict[str, TemplateResource],
    attachment_labels: dict[str, tuple[str, ...]],
    rules: ArchitectureRules,
) -> dict[str, TemplateResource]:
    return {
        node_id: _with_attachment_label(node, attachment_labels.get(node_id, ()), rules)
        for node_id, node in nodes.items()
    }


def _with_attachment_label(
    resource: TemplateResource,
    labels: tuple[str, ...],
    rules: ArchitectureRules,
) -> TemplateResource:
    formatted_labels = _format_attachment_labels(labels, rules)
    if not formatted_labels:
        return resource
    return replace(resource, label="{}\\n{}".format(resource.label, "\\n".join(formatted_labels)))


def _add_layer_attachment_summary_nodes(
    nodes: dict[str, TemplateResource],
    node_parent: dict[str, str],
    layer_attachment_labels: dict[str, tuple[str, ...]],
    layers: dict[str, TemplateResource],
    rules: ArchitectureRules,
) -> tuple[dict[str, TemplateResource], dict[str, str], dict[str, str]]:
    if not layer_attachment_labels:
        return nodes, node_parent, {}

    updated_nodes = dict(nodes)
    updated_parent = dict(node_parent)
    layer_summary_ids: dict[str, str] = {}
    for layer_id, labels in layer_attachment_labels.items():
        formatted_labels = _format_attachment_labels(labels, rules)
        if not formatted_labels or layer_id not in layers:
            continue
        summary_id = _layer_attachment_summary_node_id(layer_id)
        layer_summary_ids[layer_id] = summary_id
        updated_nodes[summary_id] = TemplateResource(
            logical_id=summary_id,
            resource_type=LAYER_ATTACHMENT_SUMMARY_TYPE,
            properties={},
            meta=None,
            label=_layer_attachment_summary_label(layers[layer_id], formatted_labels, rules),
        )
        updated_parent[summary_id] = layer_id
    return updated_nodes, updated_parent, layer_summary_ids


def _layer_attachment_summary_node_id(layer_id: str) -> str:
    return _safe_mermaid_id(f"layer_{layer_id}_Config")


def _layer_attachment_summary_label(
    layer: TemplateResource,
    formatted_labels: list[str],
    rules: ArchitectureRules,
) -> str:
    layer_label = _localized_layer_type_label(layer, rules)
    return _("{layer_label} configuration\\n{details}").format(
        layer_label=layer_label,
        details="\\n".join(formatted_labels),
    )


def _localized_layer_type_label(layer: TemplateResource, rules: ArchitectureRules) -> str:
    base_label = layer.label.split("\\n", 1)[0]
    return _(rules.legacy_layer_labels.get(layer.resource_type, base_label))


def _retarget_layer_edge_endpoints(
    edges: list[GraphEdge],
    layer_summary_ids: dict[str, str],
) -> list[GraphEdge]:
    return [
        replace(
            edge,
            from_id=layer_summary_ids.get(edge.from_id, edge.from_id),
            to_id=layer_summary_ids.get(edge.to_id, edge.to_id),
        )
        for edge in edges
    ]


def _retarget_endpoint_aliases(
    endpoint_aliases: dict[str, str],
    layer_summary_ids: dict[str, str],
) -> dict[str, str]:
    return {source_id: layer_summary_ids.get(target_id, target_id) for source_id, target_id in endpoint_aliases.items()}


def _format_attachment_labels(labels: tuple[str, ...], rules: ArchitectureRules) -> list[str]:
    counts = Counter(labels)
    formatted: list[str] = []
    seen: set[str] = set()
    for label in labels:
        if label in seen:
            continue
        seen.add(label)
        count = counts[label]
        marker = f"{label} x{count}" if count > 1 else label
        formatted.append(f"{rules.attachment_label_prefix}{marker}")
    return formatted


def _remap_attachment_labels(
    attachment_labels: dict[str, tuple[str, ...]],
    remapped_ids: dict[str, str],
    node_ids: set[str],
) -> dict[str, tuple[str, ...]]:
    remapped: dict[str, list[str]] = {}
    for node_id, labels in attachment_labels.items():
        remapped_id = remapped_ids.get(node_id, node_id)
        if remapped_id in node_ids:
            remapped.setdefault(remapped_id, []).extend(labels)
    return {node_id: tuple(labels) for node_id, labels in remapped.items()}


def _compact_resource_label(resource: TemplateResource, rules: ArchitectureRules) -> str:
    label = rules.compact_resource_labels.get(resource.resource_type)
    if label is None:
        return resource.label
    translated = _(label)
    if get_current_language() == "zh" and translated == label:
        meta_label = _localized_meta_label(resource.meta)
        if meta_label is not None:
            return meta_label
    return translated


def _aggregate_node_id(resource_type: str, parent_id: str | None, role_key: str = "") -> str:
    parts = resource_type.split("::")
    type_key = "_".join(parts[1:]) if len(parts) > 1 else resource_type
    role_part = f"_{role_key}" if role_key else ""
    parent_key = f"layer_{parent_id}" if parent_id is not None else "root"
    return _safe_mermaid_id(f"agg_{type_key}{role_part}_{parent_key}")


def _safe_mermaid_id(value: str) -> str:
    return "".join(char if char.isalnum() else "_" for char in value).strip("_")


def _flatten_compact_layers(
    layers: dict[str, TemplateResource],
    layer_parent: dict[str, str],
    nodes: dict[str, TemplateResource],
    node_parent: dict[str, str],
    relations: list[ResourceRelation],
    rules: ArchitectureRules,
) -> tuple[dict[str, TemplateResource], dict[str, str], dict[str, TemplateResource], dict[str, str]]:
    flatten_layer_types = {
        resource_type for role in rules.compact_flatten_layer_roles for resource_type in rules.containment_types(role)
    }
    flatten_layer_ids = {layer_id for layer_id, layer in layers.items() if layer.resource_type in flatten_layer_types}
    if not flatten_layer_ids:
        return layers, layer_parent, nodes, node_parent

    flattened_layers = {layer_id: layer for layer_id, layer in layers.items() if layer_id not in flatten_layer_ids}
    flattened_layer_parent: dict[str, str] = {}
    for layer_id, parent_id in layer_parent.items():
        if layer_id not in flattened_layers:
            continue
        visible_parent_id = _visible_layer_parent(parent_id, flatten_layer_ids, layer_parent)
        if visible_parent_id is not None and visible_parent_id in flattened_layers:
            flattened_layer_parent[layer_id] = visible_parent_id

    flattened_node_parent: dict[str, str] = {}
    attachment_labels: dict[str, list[str]] = {}
    for node_id, parent_id in node_parent.items():
        hidden_parent_ids = _hidden_layer_targets(node_id, relations, flatten_layer_ids)
        if parent_id in flatten_layer_ids and parent_id not in hidden_parent_ids:
            hidden_parent_ids.append(parent_id)
        for hidden_parent_id in hidden_parent_ids:
            attachment_labels.setdefault(node_id, []).append(_layer_marker_label(layers[hidden_parent_id], rules))
        visible_parent_id = (
            _direct_visible_layer_parent(node_id, layers, flatten_layer_ids, relations, rules)
            if hidden_parent_ids
            else _visible_layer_parent(parent_id, flatten_layer_ids, layer_parent)
        )
        if visible_parent_id is None:
            visible_parent_id = _visible_layer_parent(parent_id, flatten_layer_ids, layer_parent)
        if visible_parent_id is not None and visible_parent_id in flattened_layers:
            flattened_node_parent[node_id] = visible_parent_id

    flattened_nodes = _with_attachment_labels(
        nodes,
        {node_id: tuple(labels) for node_id, labels in attachment_labels.items()},
        rules,
    )
    return flattened_layers, flattened_layer_parent, flattened_nodes, flattened_node_parent


def _hidden_layer_targets(
    node_id: str,
    relations: list[ResourceRelation],
    hidden_layer_ids: set[str],
) -> list[str]:
    return _dedupe(
        [
            relation.target_id
            for relation in relations
            if relation.source_id == node_id and relation.target_id in hidden_layer_ids
        ]
    )


def _direct_visible_layer_parent(
    node_id: str,
    layers: dict[str, TemplateResource],
    hidden_layer_ids: set[str],
    relations: list[ResourceRelation],
    rules: ArchitectureRules,
) -> str | None:
    visible_layers = {layer_id: layer for layer_id, layer in layers.items() if layer_id not in hidden_layer_ids}
    for resource_types in _containment_type_priority(rules):
        target_id = _first_target(node_id, relations, candidates=_ids_by_type(visible_layers, resource_types))
        if target_id is not None:
            return target_id
    return None


def _containment_type_priority(rules: ArchitectureRules) -> list[set[str]]:
    return [set(resource_types) for resource_types in reversed(tuple(rules.containment_layer_types.values()))]


def _visible_layer_parent(
    layer_id: str | None,
    hidden_layer_ids: set[str],
    layer_parent: dict[str, str],
) -> str | None:
    while layer_id in hidden_layer_ids:
        layer_id = layer_parent.get(layer_id)
    return layer_id


def _layer_marker_label(layer: TemplateResource, rules: ArchitectureRules) -> str:
    return _(rules.legacy_layer_labels.get(layer.resource_type, layer.label))


def _build_containment(
    layers: dict[str, TemplateResource],
    nodes: dict[str, TemplateResource],
    relations: list[ResourceRelation],
    rules: ArchitectureRules,
) -> tuple[dict[str, str], dict[str, str]]:
    layer_parent: dict[str, str] = {}
    vpc_layer_types = set(rules.containment_types("vpc"))
    vswitch_layer_types = set(rules.containment_types("vswitch"))
    security_group_layer_types = set(rules.containment_types("security_group"))
    core_layer_types = vpc_layer_types | vswitch_layer_types | security_group_layer_types
    vpc_layer_ids = _ids_by_type(layers, vpc_layer_types)
    vswitch_layer_ids = _ids_by_type(layers, vswitch_layer_types)
    security_group_layer_ids = _ids_by_type(layers, security_group_layer_types)

    for layer_id, layer in layers.items():
        if layer.resource_type not in vswitch_layer_types:
            continue
        vpc_ref = _first_target(layer_id, relations, candidates=vpc_layer_ids)
        if vpc_ref is not None:
            layer_parent[layer_id] = vpc_ref

    for layer_id, layer in layers.items():
        if layer.resource_type not in security_group_layer_types:
            continue
        member_vswitches: set[str] = set()
        for node_id in nodes:
            if not _first_target(node_id, relations, candidates={layer_id}):
                continue
            vswitch_id = _first_target(node_id, relations, candidates=vswitch_layer_ids)
            if vswitch_id is not None:
                member_vswitches.add(vswitch_id)
        if len(member_vswitches) == 1:
            layer_parent[layer_id] = next(iter(member_vswitches))
            continue
        vpc_ref = _first_target(layer_id, relations, candidates=vpc_layer_ids)
        if vpc_ref is not None:
            layer_parent[layer_id] = vpc_ref

    for layer_id, layer in layers.items():
        if layer_id in layer_parent or layer.resource_type in core_layer_types:
            continue
        for resource_types in _containment_type_priority(rules):
            candidates = _ids_by_type(layers, resource_types) - {layer_id}
            target_id = _first_target(layer_id, relations, candidates=candidates)
            if target_id is not None:
                layer_parent[layer_id] = target_id
                break

    node_parent: dict[str, str] = {}
    supplemental_layer_type_sets = [
        resource_types for resource_types in _containment_type_priority(rules) if not resource_types <= core_layer_types
    ]
    for node_id in nodes:
        parent = (
            _first_target(node_id, relations, candidates=security_group_layer_ids)
            or _first_target(node_id, relations, candidates=vswitch_layer_ids)
            or _first_target(node_id, relations, candidates=vpc_layer_ids)
        )
        if parent is None:
            for resource_types in supplemental_layer_type_sets:
                parent = _first_target(node_id, relations, candidates=_ids_by_type(layers, resource_types))
                if parent is not None:
                    break
        if parent is not None:
            node_parent[node_id] = parent

    return layer_parent, node_parent


def _first_target(
    source_id: str,
    relations: list[ResourceRelation],
    *,
    candidates: set[str],
) -> str | None:
    for relation in relations:
        if relation.source_id == source_id and relation.target_id in candidates:
            return relation.target_id
    return None


def _ids_by_type(resources: dict[str, TemplateResource], resource_types: set[str]) -> set[str]:
    return {logical_id for logical_id, resource in resources.items() if resource.resource_type in resource_types}


def _render_mermaid(
    layers: dict[str, TemplateResource],
    nodes: dict[str, TemplateResource],
    edges: list[GraphEdge],
    layer_parent: dict[str, str],
    node_parent: dict[str, str],
    params: dict[str, Any],
    rules: ArchitectureRules,
) -> str:
    lines: list[str] = ["graph TD"]
    sg_style_ids: list[str] = []
    security_group_layer_types = set(rules.containment_types("security_group"))

    def layer_label(layer_id: str) -> str:
        return _localized_layer_label(layers[layer_id], params, rules)

    def render_layer(layer_id: str, indent: str) -> None:
        sub_id = f"layer_{layer_id}"
        lines.append(f"{indent}subgraph {sub_id} [{_subgraph_label(layer_label(layer_id))}]")
        if layers[layer_id].resource_type in security_group_layer_types:
            sg_style_ids.append(sub_id)
        for child_layer_id in layers:
            if layer_parent.get(child_layer_id) == layer_id:
                render_layer(child_layer_id, indent + "  ")
        for child_node_id in nodes:
            if node_parent.get(child_node_id) == layer_id:
                lines.append(f'{indent}  {child_node_id}["{nodes[child_node_id].label}"]')
        lines.append(f"{indent}end")

    for layer_id in layers:
        if layer_id not in layer_parent:
            render_layer(layer_id, "  ")

    for node_id, node in nodes.items():
        if node_id not in node_parent:
            lines.append(f'  {node_id}["{node.label}"]')

    for edge in _dedupe_edges(edges):
        from_endpoint = _mermaid_edge_endpoint(edge.from_id, nodes, layers)
        to_endpoint = _mermaid_edge_endpoint(edge.to_id, nodes, layers)
        if from_endpoint is not None and to_endpoint is not None:
            operator = rules.edge_operator(edge.style)
            if edge.label:
                lines.append(f"  {from_endpoint} {operator}|{_edge_label(edge.label)}| {to_endpoint}")
            else:
                lines.append(f"  {from_endpoint} {operator} {to_endpoint}")

    for style_id in sg_style_ids:
        lines.append(f"  style {style_id} stroke-dasharray: 5 5")

    return "\n".join(lines)


def _mermaid_edge_endpoint(
    endpoint_id: str,
    nodes: dict[str, TemplateResource],
    layers: dict[str, TemplateResource],
) -> str | None:
    if endpoint_id in nodes:
        return endpoint_id
    if endpoint_id in layers:
        return f"layer_{endpoint_id}"
    return None


def _resolve_cidr(value: Any, params: dict[str, Any]) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and "Ref" in value:
        ref = value["Ref"]
        if isinstance(ref, str) and ref in params and isinstance(params[ref], dict):
            default = params[ref].get("Default")
            if isinstance(default, str):
                return default
    return None


def _subgraph_label(value: str) -> str:
    return value.replace("[", "(").replace("]", ")")


def _edge_label(value: str) -> str:
    return value.replace("|", "/").replace("[", "(").replace("]", ")")


def _relations_by_source(relations: list[ResourceRelation]) -> dict[str, list[ResourceRelation]]:
    by_source: dict[str, list[ResourceRelation]] = {}
    for relation in relations:
        by_source.setdefault(relation.source_id, []).append(relation)
    return by_source


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            deduped.append(value)
    return deduped


def _dedupe_edges(values: list[GraphEdge]) -> list[GraphEdge]:
    seen: set[tuple[str, str, str, str | None]] = set()
    deduped: list[GraphEdge] = []
    for value in values:
        key = (value.from_id, value.to_id, value.style, value.label)
        if value.from_id == value.to_id or key in seen:
            continue
        seen.add(key)
        deduped.append(value)
    return deduped
