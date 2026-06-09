package infraguard.rules.aliyun.mse_cluster_high_availability_configured

import rego.v1
import data.infraguard.helpers

rule_meta := {
    "id": "mse-cluster-high-availability-configured",
    "severity": "medium",
    "name": {
        "en": "MSE cluster must configure replicas",
        "zh": "MSE 集群必须配置副本数",
        "ja": "MSE 集群必须配置副本数",
        "de": "MSE 集群必须配置副本数",
        "es": "MSE 集群必须配置副本数",
        "fr": "MSE 集群必须配置副本数",
        "pt": "MSE 集群必须配置副本数"
    },
    "description": {
        "en": "Checks MSE cluster must configure replicas",
        "zh": "检查MSE 集群必须配置副本数",
        "ja": "检查MSE 集群必须配置副本数",
        "de": "检查MSE 集群必须配置副本数",
        "es": "检查MSE 集群必须配置副本数",
        "fr": "检查MSE 集群必须配置副本数",
        "pt": "检查MSE 集群必须配置副本数"
    },
    "reason": {
        "en": "MSE cluster must configure replicas is not satisfied.",
        "zh": "MSE 集群必须配置副本数未满足。",
        "ja": "MSE 集群必须配置副本数未满足。",
        "de": "MSE 集群必须配置副本数未满足。",
        "es": "MSE 集群必须配置副本数未满足。",
        "fr": "MSE 集群必须配置副本数未满足。",
        "pt": "MSE 集群必须配置副本数未满足。"
    },
    "recommendation": {
        "en": "Configure Replicas on ALIYUN::MSE::Cluster to satisfy the policy.",
        "zh": "请在 ALIYUN::MSE::Cluster 上配置 Replicas 以满足策略。",
        "ja": "请在 ALIYUN::MSE::Cluster 上配置 Replicas 以满足策略。",
        "de": "请在 ALIYUN::MSE::Cluster 上配置 Replicas 以满足策略。",
        "es": "请在 ALIYUN::MSE::Cluster 上配置 Replicas 以满足策略。",
        "fr": "请在 ALIYUN::MSE::Cluster 上配置 Replicas 以满足策略。",
        "pt": "请在 ALIYUN::MSE::Cluster 上配置 Replicas 以满足策略。"
    },
    "resource_types": ["ALIYUN::MSE::Cluster"]
}

deny contains result if {
    some name, resource in helpers.resources_by_type("ALIYUN::MSE::Cluster")
    not is_compliant(resource)
    result := {
        "id": rule_meta.id,
        "resource_id": name,
        "violation_path": ["Properties", "Replicas"],
        "meta": {
            "severity": rule_meta.severity,
            "reason": rule_meta.reason,
            "recommendation": rule_meta.recommendation,
        },
    }
}

is_compliant(resource) if {
    helpers.has_property(resource, "Replicas")
}
