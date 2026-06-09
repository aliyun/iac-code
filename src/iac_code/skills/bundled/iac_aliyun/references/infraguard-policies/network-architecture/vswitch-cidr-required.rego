package infraguard.rules.aliyun.vswitch_cidr_required

import rego.v1
import data.infraguard.helpers

rule_meta := {
    "id": "vswitch-cidr-required",
    "severity": "high",
    "name": {
        "en": "VSwitch must configure CIDR block",
        "zh": "交换机必须配置网段",
        "ja": "交换机必须配置网段",
        "de": "交换机必须配置网段",
        "es": "交换机必须配置网段",
        "fr": "交换机必须配置网段",
        "pt": "交换机必须配置网段"
    },
    "description": {
        "en": "Checks VSwitch must configure CIDR block",
        "zh": "检查交换机必须配置网段",
        "ja": "检查交换机必须配置网段",
        "de": "检查交换机必须配置网段",
        "es": "检查交换机必须配置网段",
        "fr": "检查交换机必须配置网段",
        "pt": "检查交换机必须配置网段"
    },
    "reason": {
        "en": "VSwitch must configure CIDR block is not satisfied.",
        "zh": "交换机必须配置网段未满足。",
        "ja": "交换机必须配置网段未满足。",
        "de": "交换机必须配置网段未满足。",
        "es": "交换机必须配置网段未满足。",
        "fr": "交换机必须配置网段未满足。",
        "pt": "交换机必须配置网段未满足。"
    },
    "recommendation": {
        "en": "Configure CidrBlock on ALIYUN::ECS::VSwitch to satisfy the policy.",
        "zh": "请在 ALIYUN::ECS::VSwitch 上配置 CidrBlock 以满足策略。",
        "ja": "请在 ALIYUN::ECS::VSwitch 上配置 CidrBlock 以满足策略。",
        "de": "请在 ALIYUN::ECS::VSwitch 上配置 CidrBlock 以满足策略。",
        "es": "请在 ALIYUN::ECS::VSwitch 上配置 CidrBlock 以满足策略。",
        "fr": "请在 ALIYUN::ECS::VSwitch 上配置 CidrBlock 以满足策略。",
        "pt": "请在 ALIYUN::ECS::VSwitch 上配置 CidrBlock 以满足策略。"
    },
    "resource_types": ["ALIYUN::ECS::VSwitch"]
}

deny contains result if {
    some name, resource in helpers.resources_by_type("ALIYUN::ECS::VSwitch")
    not is_compliant(resource)
    result := {
        "id": rule_meta.id,
        "resource_id": name,
        "violation_path": ["Properties", "CidrBlock"],
        "meta": {
            "severity": rule_meta.severity,
            "reason": rule_meta.reason,
            "recommendation": rule_meta.recommendation,
        },
    }
}

is_compliant(resource) if {
    helpers.has_property(resource, "CidrBlock")
}
