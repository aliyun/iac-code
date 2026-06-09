package infraguard.rules.aliyun.security_group_enterprise_type

import rego.v1
import data.infraguard.helpers

rule_meta := {
    "id": "security-group-enterprise-type",
    "severity": "medium",
    "name": {
        "en": "Security group must set type",
        "zh": "安全组必须设置类型",
        "ja": "安全组必须设置类型",
        "de": "安全组必须设置类型",
        "es": "安全组必须设置类型",
        "fr": "安全组必须设置类型",
        "pt": "安全组必须设置类型"
    },
    "description": {
        "en": "Checks Security group must set type",
        "zh": "检查安全组必须设置类型",
        "ja": "检查安全组必须设置类型",
        "de": "检查安全组必须设置类型",
        "es": "检查安全组必须设置类型",
        "fr": "检查安全组必须设置类型",
        "pt": "检查安全组必须设置类型"
    },
    "reason": {
        "en": "Security group must set type is not satisfied.",
        "zh": "安全组必须设置类型未满足。",
        "ja": "安全组必须设置类型未满足。",
        "de": "安全组必须设置类型未满足。",
        "es": "安全组必须设置类型未满足。",
        "fr": "安全组必须设置类型未满足。",
        "pt": "安全组必须设置类型未满足。"
    },
    "recommendation": {
        "en": "Configure SecurityGroupType on ALIYUN::ECS::SecurityGroup to satisfy the policy.",
        "zh": "请在 ALIYUN::ECS::SecurityGroup 上配置 SecurityGroupType 以满足策略。",
        "ja": "请在 ALIYUN::ECS::SecurityGroup 上配置 SecurityGroupType 以满足策略。",
        "de": "请在 ALIYUN::ECS::SecurityGroup 上配置 SecurityGroupType 以满足策略。",
        "es": "请在 ALIYUN::ECS::SecurityGroup 上配置 SecurityGroupType 以满足策略。",
        "fr": "请在 ALIYUN::ECS::SecurityGroup 上配置 SecurityGroupType 以满足策略。",
        "pt": "请在 ALIYUN::ECS::SecurityGroup 上配置 SecurityGroupType 以满足策略。"
    },
    "resource_types": ["ALIYUN::ECS::SecurityGroup"]
}

deny contains result if {
    some name, resource in helpers.resources_by_type("ALIYUN::ECS::SecurityGroup")
    not is_compliant(resource)
    result := {
        "id": rule_meta.id,
        "resource_id": name,
        "violation_path": ["Properties", "SecurityGroupType"],
        "meta": {
            "severity": rule_meta.severity,
            "reason": rule_meta.reason,
            "recommendation": rule_meta.recommendation,
        },
    }
}

is_compliant(resource) if {
    helpers.has_property(resource, "SecurityGroupType")
}
