package infraguard.rules.aliyun.ecs_security_group_description_required

import rego.v1
import data.infraguard.helpers

rule_meta := {
    "id": "ecs-security-group-description-required",
    "severity": "medium",
    "name": {
        "en": "Security group must configure description",
        "zh": "安全组必须配置描述",
        "ja": "安全组必须配置描述",
        "de": "安全组必须配置描述",
        "es": "安全组必须配置描述",
        "fr": "安全组必须配置描述",
        "pt": "安全组必须配置描述"
    },
    "description": {
        "en": "Checks Security group must configure description",
        "zh": "检查安全组必须配置描述",
        "ja": "检查安全组必须配置描述",
        "de": "检查安全组必须配置描述",
        "es": "检查安全组必须配置描述",
        "fr": "检查安全组必须配置描述",
        "pt": "检查安全组必须配置描述"
    },
    "reason": {
        "en": "Security group must configure description is not satisfied.",
        "zh": "安全组必须配置描述未满足。",
        "ja": "安全组必须配置描述未满足。",
        "de": "安全组必须配置描述未满足。",
        "es": "安全组必须配置描述未满足。",
        "fr": "安全组必须配置描述未满足。",
        "pt": "安全组必须配置描述未满足。"
    },
    "recommendation": {
        "en": "Configure Description on ALIYUN::ECS::SecurityGroup to satisfy the policy.",
        "zh": "请在 ALIYUN::ECS::SecurityGroup 上配置 Description 以满足策略。",
        "ja": "请在 ALIYUN::ECS::SecurityGroup 上配置 Description 以满足策略。",
        "de": "请在 ALIYUN::ECS::SecurityGroup 上配置 Description 以满足策略。",
        "es": "请在 ALIYUN::ECS::SecurityGroup 上配置 Description 以满足策略。",
        "fr": "请在 ALIYUN::ECS::SecurityGroup 上配置 Description 以满足策略。",
        "pt": "请在 ALIYUN::ECS::SecurityGroup 上配置 Description 以满足策略。"
    },
    "resource_types": ["ALIYUN::ECS::SecurityGroup"]
}

deny contains result if {
    some name, resource in helpers.resources_by_type("ALIYUN::ECS::SecurityGroup")
    not is_compliant(resource)
    result := {
        "id": rule_meta.id,
        "resource_id": name,
        "violation_path": ["Properties", "Description"],
        "meta": {
            "severity": rule_meta.severity,
            "reason": rule_meta.reason,
            "recommendation": rule_meta.recommendation,
        },
    }
}

is_compliant(resource) if {
    helpers.has_property(resource, "Description")
}
