package infraguard.rules.aliyun.polardb_cluster_tags_required

import rego.v1
import data.infraguard.helpers

rule_meta := {
    "id": "polardb-cluster-tags-required",
    "severity": "medium",
    "name": {
        "en": "PolarDB cluster must configure tags",
        "zh": "PolarDB 集群必须配置标签",
        "ja": "PolarDB 集群必须配置标签",
        "de": "PolarDB 集群必须配置标签",
        "es": "PolarDB 集群必须配置标签",
        "fr": "PolarDB 集群必须配置标签",
        "pt": "PolarDB 集群必须配置标签"
    },
    "description": {
        "en": "Checks PolarDB cluster must configure tags",
        "zh": "检查PolarDB 集群必须配置标签",
        "ja": "检查PolarDB 集群必须配置标签",
        "de": "检查PolarDB 集群必须配置标签",
        "es": "检查PolarDB 集群必须配置标签",
        "fr": "检查PolarDB 集群必须配置标签",
        "pt": "检查PolarDB 集群必须配置标签"
    },
    "reason": {
        "en": "PolarDB cluster must configure tags is not satisfied.",
        "zh": "PolarDB 集群必须配置标签未满足。",
        "ja": "PolarDB 集群必须配置标签未满足。",
        "de": "PolarDB 集群必须配置标签未满足。",
        "es": "PolarDB 集群必须配置标签未满足。",
        "fr": "PolarDB 集群必须配置标签未满足。",
        "pt": "PolarDB 集群必须配置标签未满足。"
    },
    "recommendation": {
        "en": "Configure Tags on ALIYUN::POLARDB::DBCluster to satisfy the policy.",
        "zh": "请在 ALIYUN::POLARDB::DBCluster 上配置 Tags 以满足策略。",
        "ja": "请在 ALIYUN::POLARDB::DBCluster 上配置 Tags 以满足策略。",
        "de": "请在 ALIYUN::POLARDB::DBCluster 上配置 Tags 以满足策略。",
        "es": "请在 ALIYUN::POLARDB::DBCluster 上配置 Tags 以满足策略。",
        "fr": "请在 ALIYUN::POLARDB::DBCluster 上配置 Tags 以满足策略。",
        "pt": "请在 ALIYUN::POLARDB::DBCluster 上配置 Tags 以满足策略。"
    },
    "resource_types": ["ALIYUN::POLARDB::DBCluster"]
}

deny contains result if {
    some name, resource in helpers.resources_by_type("ALIYUN::POLARDB::DBCluster")
    not is_compliant(resource)
    result := {
        "id": rule_meta.id,
        "resource_id": name,
        "violation_path": ["Properties", "Tags"],
        "meta": {
            "severity": rule_meta.severity,
            "reason": rule_meta.reason,
            "recommendation": rule_meta.recommendation,
        },
    }
}

is_compliant(resource) if {
    helpers.has_property(resource, "Tags")
}
