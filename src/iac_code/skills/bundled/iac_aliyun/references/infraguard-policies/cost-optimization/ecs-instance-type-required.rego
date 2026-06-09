package infraguard.rules.aliyun.ecs_instance_type_required

import rego.v1
import data.infraguard.helpers

rule_meta := {
    "id": "ecs-instance-type-required",
    "severity": "medium",
    "name": {
        "en": "ECS instance must set instance type",
        "zh": "ECS 实例必须设置实例规格",
        "ja": "ECS 实例必须设置实例规格",
        "de": "ECS 实例必须设置实例规格",
        "es": "ECS 实例必须设置实例规格",
        "fr": "ECS 实例必须设置实例规格",
        "pt": "ECS 实例必须设置实例规格"
    },
    "description": {
        "en": "Checks ECS instance must set instance type",
        "zh": "检查ECS 实例必须设置实例规格",
        "ja": "检查ECS 实例必须设置实例规格",
        "de": "检查ECS 实例必须设置实例规格",
        "es": "检查ECS 实例必须设置实例规格",
        "fr": "检查ECS 实例必须设置实例规格",
        "pt": "检查ECS 实例必须设置实例规格"
    },
    "reason": {
        "en": "ECS instance must set instance type is not satisfied.",
        "zh": "ECS 实例必须设置实例规格未满足。",
        "ja": "ECS 实例必须设置实例规格未满足。",
        "de": "ECS 实例必须设置实例规格未满足。",
        "es": "ECS 实例必须设置实例规格未满足。",
        "fr": "ECS 实例必须设置实例规格未满足。",
        "pt": "ECS 实例必须设置实例规格未满足。"
    },
    "recommendation": {
        "en": "Configure InstanceType on ALIYUN::ECS::Instance to satisfy the policy.",
        "zh": "请在 ALIYUN::ECS::Instance 上配置 InstanceType 以满足策略。",
        "ja": "请在 ALIYUN::ECS::Instance 上配置 InstanceType 以满足策略。",
        "de": "请在 ALIYUN::ECS::Instance 上配置 InstanceType 以满足策略。",
        "es": "请在 ALIYUN::ECS::Instance 上配置 InstanceType 以满足策略。",
        "fr": "请在 ALIYUN::ECS::Instance 上配置 InstanceType 以满足策略。",
        "pt": "请在 ALIYUN::ECS::Instance 上配置 InstanceType 以满足策略。"
    },
    "resource_types": ["ALIYUN::ECS::Instance"]
}

deny contains result if {
    some name, resource in helpers.resources_by_type("ALIYUN::ECS::Instance")
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
