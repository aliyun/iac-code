package infraguard.rules.aliyun.sls_logstore_shard_count_configured

import rego.v1
import data.infraguard.helpers

rule_meta := {
    "id": "sls-logstore-shard-count-configured",
    "severity": "medium",
    "name": {
        "en": "SLS Logstore must configure shard count",
        "zh": "SLS Logstore 必须配置分区数",
        "ja": "SLS Logstore 必须配置分区数",
        "de": "SLS Logstore 必须配置分区数",
        "es": "SLS Logstore 必须配置分区数",
        "fr": "SLS Logstore 必须配置分区数",
        "pt": "SLS Logstore 必须配置分区数"
    },
    "description": {
        "en": "Checks SLS Logstore must configure shard count",
        "zh": "检查SLS Logstore 必须配置分区数",
        "ja": "检查SLS Logstore 必须配置分区数",
        "de": "检查SLS Logstore 必须配置分区数",
        "es": "检查SLS Logstore 必须配置分区数",
        "fr": "检查SLS Logstore 必须配置分区数",
        "pt": "检查SLS Logstore 必须配置分区数"
    },
    "reason": {
        "en": "SLS Logstore must configure shard count is not satisfied.",
        "zh": "SLS Logstore 必须配置分区数未满足。",
        "ja": "SLS Logstore 必须配置分区数未满足。",
        "de": "SLS Logstore 必须配置分区数未满足。",
        "es": "SLS Logstore 必须配置分区数未满足。",
        "fr": "SLS Logstore 必须配置分区数未满足。",
        "pt": "SLS Logstore 必须配置分区数未满足。"
    },
    "recommendation": {
        "en": "Configure ShardCount on ALIYUN::SLS::Logstore to satisfy the policy.",
        "zh": "请在 ALIYUN::SLS::Logstore 上配置 ShardCount 以满足策略。",
        "ja": "请在 ALIYUN::SLS::Logstore 上配置 ShardCount 以满足策略。",
        "de": "请在 ALIYUN::SLS::Logstore 上配置 ShardCount 以满足策略。",
        "es": "请在 ALIYUN::SLS::Logstore 上配置 ShardCount 以满足策略。",
        "fr": "请在 ALIYUN::SLS::Logstore 上配置 ShardCount 以满足策略。",
        "pt": "请在 ALIYUN::SLS::Logstore 上配置 ShardCount 以满足策略。"
    },
    "resource_types": ["ALIYUN::SLS::Logstore"]
}

deny contains result if {
    some name, resource in helpers.resources_by_type("ALIYUN::SLS::Logstore")
    not is_compliant(resource)
    result := {
        "id": rule_meta.id,
        "resource_id": name,
        "violation_path": ["Properties", "ShardCount"],
        "meta": {
            "severity": rule_meta.severity,
            "reason": rule_meta.reason,
            "recommendation": rule_meta.recommendation,
        },
    }
}

is_compliant(resource) if {
    helpers.has_property(resource, "ShardCount")
}
