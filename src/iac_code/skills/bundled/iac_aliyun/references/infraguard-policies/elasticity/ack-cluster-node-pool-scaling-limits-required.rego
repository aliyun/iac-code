package infraguard.rules.aliyun.ack_cluster_node_pool_scaling_limits_required

import rego.v1
import data.infraguard.helpers

rule_meta := {
    "id": "ack-cluster-node-pool-scaling-limits-required",
    "severity": "medium",
    "name": {
        "en": "ESS scaling group must configure MinSize",
        "zh": "ESS 伸缩组必须配置最小容量",
        "ja": "ESS 伸缩组必须配置最小容量",
        "de": "ESS 伸缩组必须配置最小容量",
        "es": "ESS 伸缩组必须配置最小容量",
        "fr": "ESS 伸缩组必须配置最小容量",
        "pt": "ESS 伸缩组必须配置最小容量"
    },
    "description": {
        "en": "Checks ESS scaling group must configure MinSize",
        "zh": "检查ESS 伸缩组必须配置最小容量",
        "ja": "检查ESS 伸缩组必须配置最小容量",
        "de": "检查ESS 伸缩组必须配置最小容量",
        "es": "检查ESS 伸缩组必须配置最小容量",
        "fr": "检查ESS 伸缩组必须配置最小容量",
        "pt": "检查ESS 伸缩组必须配置最小容量"
    },
    "reason": {
        "en": "ESS scaling group must configure MinSize is not satisfied.",
        "zh": "ESS 伸缩组必须配置最小容量未满足。",
        "ja": "ESS 伸缩组必须配置最小容量未满足。",
        "de": "ESS 伸缩组必须配置最小容量未满足。",
        "es": "ESS 伸缩组必须配置最小容量未满足。",
        "fr": "ESS 伸缩组必须配置最小容量未满足。",
        "pt": "ESS 伸缩组必须配置最小容量未满足。"
    },
    "recommendation": {
        "en": "Configure MinSize on ALIYUN::ESS::ScalingGroup to satisfy the policy.",
        "zh": "请在 ALIYUN::ESS::ScalingGroup 上配置 MinSize 以满足策略。",
        "ja": "请在 ALIYUN::ESS::ScalingGroup 上配置 MinSize 以满足策略。",
        "de": "请在 ALIYUN::ESS::ScalingGroup 上配置 MinSize 以满足策略。",
        "es": "请在 ALIYUN::ESS::ScalingGroup 上配置 MinSize 以满足策略。",
        "fr": "请在 ALIYUN::ESS::ScalingGroup 上配置 MinSize 以满足策略。",
        "pt": "请在 ALIYUN::ESS::ScalingGroup 上配置 MinSize 以满足策略。"
    },
    "resource_types": ["ALIYUN::ESS::ScalingGroup"]
}

deny contains result if {
    some name, resource in helpers.resources_by_type("ALIYUN::ESS::ScalingGroup")
    not is_compliant(resource)
    result := {
        "id": rule_meta.id,
        "resource_id": name,
        "violation_path": ["Properties", "MinSize"],
        "meta": {
            "severity": rule_meta.severity,
            "reason": rule_meta.reason,
            "recommendation": rule_meta.recommendation,
        },
    }
}

is_compliant(resource) if {
    helpers.has_property(resource, "MinSize")
}
