package infraguard.rules.aliyun.ess_scaling_group_capacity_bounds_required

import rego.v1
import data.infraguard.helpers

rule_meta := {
    "id": "ess-scaling-group-capacity-bounds-required",
    "severity": "medium",
    "name": {
        "en": "ESS scaling group must configure MaxSize",
        "zh": "ESS 伸缩组必须配置最大容量",
        "ja": "ESS 伸缩组必须配置最大容量",
        "de": "ESS 伸缩组必须配置最大容量",
        "es": "ESS 伸缩组必须配置最大容量",
        "fr": "ESS 伸缩组必须配置最大容量",
        "pt": "ESS 伸缩组必须配置最大容量"
    },
    "description": {
        "en": "Checks ESS scaling group must configure MaxSize",
        "zh": "检查ESS 伸缩组必须配置最大容量",
        "ja": "检查ESS 伸缩组必须配置最大容量",
        "de": "检查ESS 伸缩组必须配置最大容量",
        "es": "检查ESS 伸缩组必须配置最大容量",
        "fr": "检查ESS 伸缩组必须配置最大容量",
        "pt": "检查ESS 伸缩组必须配置最大容量"
    },
    "reason": {
        "en": "ESS scaling group must configure MaxSize is not satisfied.",
        "zh": "ESS 伸缩组必须配置最大容量未满足。",
        "ja": "ESS 伸缩组必须配置最大容量未满足。",
        "de": "ESS 伸缩组必须配置最大容量未满足。",
        "es": "ESS 伸缩组必须配置最大容量未满足。",
        "fr": "ESS 伸缩组必须配置最大容量未满足。",
        "pt": "ESS 伸缩组必须配置最大容量未满足。"
    },
    "recommendation": {
        "en": "Configure MaxSize on ALIYUN::ESS::ScalingGroup to satisfy the policy.",
        "zh": "请在 ALIYUN::ESS::ScalingGroup 上配置 MaxSize 以满足策略。",
        "ja": "请在 ALIYUN::ESS::ScalingGroup 上配置 MaxSize 以满足策略。",
        "de": "请在 ALIYUN::ESS::ScalingGroup 上配置 MaxSize 以满足策略。",
        "es": "请在 ALIYUN::ESS::ScalingGroup 上配置 MaxSize 以满足策略。",
        "fr": "请在 ALIYUN::ESS::ScalingGroup 上配置 MaxSize 以满足策略。",
        "pt": "请在 ALIYUN::ESS::ScalingGroup 上配置 MaxSize 以满足策略。"
    },
    "resource_types": ["ALIYUN::ESS::ScalingGroup"]
}

deny contains result if {
    some name, resource in helpers.resources_by_type("ALIYUN::ESS::ScalingGroup")
    not is_compliant(resource)
    result := {
        "id": rule_meta.id,
        "resource_id": name,
        "violation_path": ["Properties", "MaxSize"],
        "meta": {
            "severity": rule_meta.severity,
            "reason": rule_meta.reason,
            "recommendation": rule_meta.recommendation,
        },
    }
}

is_compliant(resource) if {
    helpers.has_property(resource, "MaxSize")
}
