package infraguard.rules.aliyun.ess_scaling_rule_action_configured

import rego.v1
import data.infraguard.helpers

rule_meta := {
    "id": "ess-scaling-rule-action-configured",
    "severity": "medium",
    "name": {
        "en": "ESS scaling rule must configure adjustment",
        "zh": "ESS 伸缩规则必须配置调整方式",
        "ja": "ESS 伸缩规则必须配置调整方式",
        "de": "ESS 伸缩规则必须配置调整方式",
        "es": "ESS 伸缩规则必须配置调整方式",
        "fr": "ESS 伸缩规则必须配置调整方式",
        "pt": "ESS 伸缩规则必须配置调整方式"
    },
    "description": {
        "en": "Checks ESS scaling rule must configure adjustment",
        "zh": "检查ESS 伸缩规则必须配置调整方式",
        "ja": "检查ESS 伸缩规则必须配置调整方式",
        "de": "检查ESS 伸缩规则必须配置调整方式",
        "es": "检查ESS 伸缩规则必须配置调整方式",
        "fr": "检查ESS 伸缩规则必须配置调整方式",
        "pt": "检查ESS 伸缩规则必须配置调整方式"
    },
    "reason": {
        "en": "ESS scaling rule must configure adjustment is not satisfied.",
        "zh": "ESS 伸缩规则必须配置调整方式未满足。",
        "ja": "ESS 伸缩规则必须配置调整方式未满足。",
        "de": "ESS 伸缩规则必须配置调整方式未满足。",
        "es": "ESS 伸缩规则必须配置调整方式未满足。",
        "fr": "ESS 伸缩规则必须配置调整方式未满足。",
        "pt": "ESS 伸缩规则必须配置调整方式未满足。"
    },
    "recommendation": {
        "en": "Configure AdjustmentType on ALIYUN::ESS::ScalingRule to satisfy the policy.",
        "zh": "请在 ALIYUN::ESS::ScalingRule 上配置 AdjustmentType 以满足策略。",
        "ja": "请在 ALIYUN::ESS::ScalingRule 上配置 AdjustmentType 以满足策略。",
        "de": "请在 ALIYUN::ESS::ScalingRule 上配置 AdjustmentType 以满足策略。",
        "es": "请在 ALIYUN::ESS::ScalingRule 上配置 AdjustmentType 以满足策略。",
        "fr": "请在 ALIYUN::ESS::ScalingRule 上配置 AdjustmentType 以满足策略。",
        "pt": "请在 ALIYUN::ESS::ScalingRule 上配置 AdjustmentType 以满足策略。"
    },
    "resource_types": ["ALIYUN::ESS::ScalingRule"]
}

deny contains result if {
    some name, resource in helpers.resources_by_type("ALIYUN::ESS::ScalingRule")
    not is_compliant(resource)
    result := {
        "id": rule_meta.id,
        "resource_id": name,
        "violation_path": ["Properties", "AdjustmentType"],
        "meta": {
            "severity": rule_meta.severity,
            "reason": rule_meta.reason,
            "recommendation": rule_meta.recommendation,
        },
    }
}

is_compliant(resource) if {
    helpers.has_property(resource, "AdjustmentType")
}
