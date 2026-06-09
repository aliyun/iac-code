package infraguard.rules.aliyun.vswitch_zone_required

import rego.v1
import data.infraguard.helpers

rule_meta := {
    "id": "vswitch-zone-required",
    "severity": "medium",
    "name": {
        "en": "VSwitch must configure zone",
        "zh": "交换机必须配置可用区",
        "ja": "交换机必须配置可用区",
        "de": "交换机必须配置可用区",
        "es": "交换机必须配置可用区",
        "fr": "交换机必须配置可用区",
        "pt": "交换机必须配置可用区"
    },
    "description": {
        "en": "Checks VSwitch must configure zone",
        "zh": "检查交换机必须配置可用区",
        "ja": "检查交换机必须配置可用区",
        "de": "检查交换机必须配置可用区",
        "es": "检查交换机必须配置可用区",
        "fr": "检查交换机必须配置可用区",
        "pt": "检查交换机必须配置可用区"
    },
    "reason": {
        "en": "VSwitch must configure zone is not satisfied.",
        "zh": "交换机必须配置可用区未满足。",
        "ja": "交换机必须配置可用区未满足。",
        "de": "交换机必须配置可用区未满足。",
        "es": "交换机必须配置可用区未满足。",
        "fr": "交换机必须配置可用区未满足。",
        "pt": "交换机必须配置可用区未满足。"
    },
    "recommendation": {
        "en": "Configure ZoneId on ALIYUN::ECS::VSwitch to satisfy the policy.",
        "zh": "请在 ALIYUN::ECS::VSwitch 上配置 ZoneId 以满足策略。",
        "ja": "请在 ALIYUN::ECS::VSwitch 上配置 ZoneId 以满足策略。",
        "de": "请在 ALIYUN::ECS::VSwitch 上配置 ZoneId 以满足策略。",
        "es": "请在 ALIYUN::ECS::VSwitch 上配置 ZoneId 以满足策略。",
        "fr": "请在 ALIYUN::ECS::VSwitch 上配置 ZoneId 以满足策略。",
        "pt": "请在 ALIYUN::ECS::VSwitch 上配置 ZoneId 以满足策略。"
    },
    "resource_types": ["ALIYUN::ECS::VSwitch"]
}

deny contains result if {
    some name, resource in helpers.resources_by_type("ALIYUN::ECS::VSwitch")
    not is_compliant(resource)
    result := {
        "id": rule_meta.id,
        "resource_id": name,
        "violation_path": ["Properties", "ZoneId"],
        "meta": {
            "severity": rule_meta.severity,
            "reason": rule_meta.reason,
            "recommendation": rule_meta.recommendation,
        },
    }
}

is_compliant(resource) if {
    helpers.has_property(resource, "ZoneId")
}
