package infraguard.rules.aliyun.ess_scaling_configuration_instance_type_candidates_required

import rego.v1
import data.infraguard.helpers

rule_meta := {
    "id": "ess-scaling-configuration-instance-type-candidates-required",
    "severity": "medium",
    "name": {
        "en": "ESS scaling configuration must set instance type",
        "zh": "ESS 伸缩配置必须设置实例规格",
        "ja": "ESS 伸缩配置必须设置实例规格",
        "de": "ESS 伸缩配置必须设置实例规格",
        "es": "ESS 伸缩配置必须设置实例规格",
        "fr": "ESS 伸缩配置必须设置实例规格",
        "pt": "ESS 伸缩配置必须设置实例规格"
    },
    "description": {
        "en": "Checks ESS scaling configuration must set instance type",
        "zh": "检查ESS 伸缩配置必须设置实例规格",
        "ja": "检查ESS 伸缩配置必须设置实例规格",
        "de": "检查ESS 伸缩配置必须设置实例规格",
        "es": "检查ESS 伸缩配置必须设置实例规格",
        "fr": "检查ESS 伸缩配置必须设置实例规格",
        "pt": "检查ESS 伸缩配置必须设置实例规格"
    },
    "reason": {
        "en": "ESS scaling configuration must set instance type is not satisfied.",
        "zh": "ESS 伸缩配置必须设置实例规格未满足。",
        "ja": "ESS 伸缩配置必须设置实例规格未满足。",
        "de": "ESS 伸缩配置必须设置实例规格未满足。",
        "es": "ESS 伸缩配置必须设置实例规格未满足。",
        "fr": "ESS 伸缩配置必须设置实例规格未满足。",
        "pt": "ESS 伸缩配置必须设置实例规格未满足。"
    },
    "recommendation": {
        "en": "Configure InstanceType on ALIYUN::ESS::ScalingConfiguration to satisfy the policy.",
        "zh": "请在 ALIYUN::ESS::ScalingConfiguration 上配置 InstanceType 以满足策略。",
        "ja": "请在 ALIYUN::ESS::ScalingConfiguration 上配置 InstanceType 以满足策略。",
        "de": "请在 ALIYUN::ESS::ScalingConfiguration 上配置 InstanceType 以满足策略。",
        "es": "请在 ALIYUN::ESS::ScalingConfiguration 上配置 InstanceType 以满足策略。",
        "fr": "请在 ALIYUN::ESS::ScalingConfiguration 上配置 InstanceType 以满足策略。",
        "pt": "请在 ALIYUN::ESS::ScalingConfiguration 上配置 InstanceType 以满足策略。"
    },
    "resource_types": ["ALIYUN::ESS::ScalingConfiguration"]
}

deny contains result if {
    some name, resource in helpers.resources_by_type("ALIYUN::ESS::ScalingConfiguration")
    not is_compliant(resource)
    result := {
        "id": rule_meta.id,
        "resource_id": name,
        "violation_path": ["Properties", "InstanceType"],
        "meta": {
            "severity": rule_meta.severity,
            "reason": rule_meta.reason,
            "recommendation": rule_meta.recommendation,
        },
    }
}

is_compliant(resource) if {
    helpers.has_property(resource, "InstanceType")
}
