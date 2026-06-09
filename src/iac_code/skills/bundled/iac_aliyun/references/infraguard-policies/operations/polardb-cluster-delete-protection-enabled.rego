package infraguard.rules.aliyun.polardb_cluster_delete_protection_enabled

import rego.v1
import data.infraguard.helpers

rule_meta := {
    "id": "polardb-cluster-delete-protection-enabled",
    "severity": "medium",
    "name": {
        "en": "PolarDB cluster must enable deletion protection",
        "zh": "PolarDB 集群必须启用删除保护",
        "ja": "PolarDB 集群必须启用删除保护",
        "de": "PolarDB 集群必须启用删除保护",
        "es": "PolarDB 集群必须启用删除保护",
        "fr": "PolarDB 集群必须启用删除保护",
        "pt": "PolarDB 集群必须启用删除保护"
    },
    "description": {
        "en": "Checks PolarDB cluster must enable deletion protection",
        "zh": "检查PolarDB 集群必须启用删除保护",
        "ja": "检查PolarDB 集群必须启用删除保护",
        "de": "检查PolarDB 集群必须启用删除保护",
        "es": "检查PolarDB 集群必须启用删除保护",
        "fr": "检查PolarDB 集群必须启用删除保护",
        "pt": "检查PolarDB 集群必须启用删除保护"
    },
    "reason": {
        "en": "PolarDB cluster must enable deletion protection is not satisfied.",
        "zh": "PolarDB 集群必须启用删除保护未满足。",
        "ja": "PolarDB 集群必须启用删除保护未满足。",
        "de": "PolarDB 集群必须启用删除保护未满足。",
        "es": "PolarDB 集群必须启用删除保护未满足。",
        "fr": "PolarDB 集群必须启用删除保护未满足。",
        "pt": "PolarDB 集群必须启用删除保护未满足。"
    },
    "recommendation": {
        "en": "Configure DeletionProtection on ALIYUN::POLARDB::DBCluster to satisfy the policy.",
        "zh": "请在 ALIYUN::POLARDB::DBCluster 上配置 DeletionProtection 以满足策略。",
        "ja": "请在 ALIYUN::POLARDB::DBCluster 上配置 DeletionProtection 以满足策略。",
        "de": "请在 ALIYUN::POLARDB::DBCluster 上配置 DeletionProtection 以满足策略。",
        "es": "请在 ALIYUN::POLARDB::DBCluster 上配置 DeletionProtection 以满足策略。",
        "fr": "请在 ALIYUN::POLARDB::DBCluster 上配置 DeletionProtection 以满足策略。",
        "pt": "请在 ALIYUN::POLARDB::DBCluster 上配置 DeletionProtection 以满足策略。"
    },
    "resource_types": ["ALIYUN::POLARDB::DBCluster"]
}

deny contains result if {
    some name, resource in helpers.resources_by_type("ALIYUN::POLARDB::DBCluster")
    not is_compliant(resource)
    result := {
        "id": rule_meta.id,
        "resource_id": name,
        "violation_path": ["Properties", "DeletionProtection"],
        "meta": {
            "severity": rule_meta.severity,
            "reason": rule_meta.reason,
            "recommendation": rule_meta.recommendation,
        },
    }
}

is_compliant(resource) if {
    helpers.get_property(resource, "DeletionProtection", false) == true
}
