package infraguard.rules.aliyun.ecs_instance_charge_type_required

import rego.v1
import data.infraguard.helpers

rule_meta := {
    "id": "ecs-instance-charge-type-required",
    "severity": "medium",
    "name": {
        "en": "ECS instance must set charge type",
        "zh": "ECS 实例必须设置付费类型",
        "ja": "ECS 实例必须设置付费类型",
        "de": "ECS 实例必须设置付费类型",
        "es": "ECS 实例必须设置付费类型",
        "fr": "ECS 实例必须设置付费类型",
        "pt": "ECS 实例必须设置付费类型"
    },
    "description": {
        "en": "Checks ECS instance must set charge type",
        "zh": "检查ECS 实例必须设置付费类型",
        "ja": "检查ECS 实例必须设置付费类型",
        "de": "检查ECS 实例必须设置付费类型",
        "es": "检查ECS 实例必须设置付费类型",
        "fr": "检查ECS 实例必须设置付费类型",
        "pt": "检查ECS 实例必须设置付费类型"
    },
    "reason": {
        "en": "ECS instance must set charge type is not satisfied.",
        "zh": "ECS 实例必须设置付费类型未满足。",
        "ja": "ECS 实例必须设置付费类型未满足。",
        "de": "ECS 实例必须设置付费类型未满足。",
        "es": "ECS 实例必须设置付费类型未满足。",
        "fr": "ECS 实例必须设置付费类型未满足。",
        "pt": "ECS 实例必须设置付费类型未满足。"
    },
    "recommendation": {
        "en": "Configure InstanceChargeType on ALIYUN::ECS::Instance to satisfy the policy.",
        "zh": "请在 ALIYUN::ECS::Instance 上配置 InstanceChargeType 以满足策略。",
        "ja": "请在 ALIYUN::ECS::Instance 上配置 InstanceChargeType 以满足策略。",
        "de": "请在 ALIYUN::ECS::Instance 上配置 InstanceChargeType 以满足策略。",
        "es": "请在 ALIYUN::ECS::Instance 上配置 InstanceChargeType 以满足策略。",
        "fr": "请在 ALIYUN::ECS::Instance 上配置 InstanceChargeType 以满足策略。",
        "pt": "请在 ALIYUN::ECS::Instance 上配置 InstanceChargeType 以满足策略。"
    },
    "resource_types": ["ALIYUN::ECS::Instance"]
}

deny contains result if {
    some name, resource in helpers.resources_by_type("ALIYUN::ECS::Instance")
    not is_compliant(resource)
    result := {
        "id": rule_meta.id,
        "resource_id": name,
        "violation_path": ["Properties", "InstanceChargeType"],
        "meta": {
            "severity": rule_meta.severity,
            "reason": rule_meta.reason,
            "recommendation": rule_meta.recommendation,
        },
    }
}

is_compliant(resource) if {
    helpers.has_property(resource, "InstanceChargeType")
}
