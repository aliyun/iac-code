package infraguard.rules.aliyun.ecs_instance_name_required

import rego.v1
import data.infraguard.helpers

rule_meta := {
    "id": "ecs-instance-name-required",
    "severity": "medium",
    "name": {
        "en": "ECS instance must configure name",
        "zh": "ECS 实例必须配置名称",
        "ja": "ECS 实例必须配置名称",
        "de": "ECS 实例必须配置名称",
        "es": "ECS 实例必须配置名称",
        "fr": "ECS 实例必须配置名称",
        "pt": "ECS 实例必须配置名称"
    },
    "description": {
        "en": "Checks ECS instance must configure name",
        "zh": "检查ECS 实例必须配置名称",
        "ja": "检查ECS 实例必须配置名称",
        "de": "检查ECS 实例必须配置名称",
        "es": "检查ECS 实例必须配置名称",
        "fr": "检查ECS 实例必须配置名称",
        "pt": "检查ECS 实例必须配置名称"
    },
    "reason": {
        "en": "ECS instance must configure name is not satisfied.",
        "zh": "ECS 实例必须配置名称未满足。",
        "ja": "ECS 实例必须配置名称未满足。",
        "de": "ECS 实例必须配置名称未满足。",
        "es": "ECS 实例必须配置名称未满足。",
        "fr": "ECS 实例必须配置名称未满足。",
        "pt": "ECS 实例必须配置名称未满足。"
    },
    "recommendation": {
        "en": "Configure InstanceName on ALIYUN::ECS::Instance to satisfy the policy.",
        "zh": "请在 ALIYUN::ECS::Instance 上配置 InstanceName 以满足策略。",
        "ja": "请在 ALIYUN::ECS::Instance 上配置 InstanceName 以满足策略。",
        "de": "请在 ALIYUN::ECS::Instance 上配置 InstanceName 以满足策略。",
        "es": "请在 ALIYUN::ECS::Instance 上配置 InstanceName 以满足策略。",
        "fr": "请在 ALIYUN::ECS::Instance 上配置 InstanceName 以满足策略。",
        "pt": "请在 ALIYUN::ECS::Instance 上配置 InstanceName 以满足策略。"
    },
    "resource_types": ["ALIYUN::ECS::Instance"]
}

deny contains result if {
    some name, resource in helpers.resources_by_type("ALIYUN::ECS::Instance")
    not is_compliant(resource)
    result := {
        "id": rule_meta.id,
        "resource_id": name,
        "violation_path": ["Properties", "InstanceName"],
        "meta": {
            "severity": rule_meta.severity,
            "reason": rule_meta.reason,
            "recommendation": rule_meta.recommendation,
        },
    }
}

is_compliant(resource) if {
    helpers.has_property(resource, "InstanceName")
}
