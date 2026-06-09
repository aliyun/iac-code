package infraguard.rules.aliyun.ecs_instance_operational_deletion_protection

import rego.v1
import data.infraguard.helpers

rule_meta := {
    "id": "ecs-instance-operational-deletion-protection",
    "severity": "medium",
    "name": {
        "en": "ECS instance must enable deletion protection for operations",
        "zh": "ECS 实例必须启用运维删除保护",
        "ja": "ECS 实例必须启用运维删除保护",
        "de": "ECS 实例必须启用运维删除保护",
        "es": "ECS 实例必须启用运维删除保护",
        "fr": "ECS 实例必须启用运维删除保护",
        "pt": "ECS 实例必须启用运维删除保护"
    },
    "description": {
        "en": "Checks ECS instance must enable deletion protection for operations",
        "zh": "检查ECS 实例必须启用运维删除保护",
        "ja": "检查ECS 实例必须启用运维删除保护",
        "de": "检查ECS 实例必须启用运维删除保护",
        "es": "检查ECS 实例必须启用运维删除保护",
        "fr": "检查ECS 实例必须启用运维删除保护",
        "pt": "检查ECS 实例必须启用运维删除保护"
    },
    "reason": {
        "en": "ECS instance must enable deletion protection for operations is not satisfied.",
        "zh": "ECS 实例必须启用运维删除保护未满足。",
        "ja": "ECS 实例必须启用运维删除保护未满足。",
        "de": "ECS 实例必须启用运维删除保护未满足。",
        "es": "ECS 实例必须启用运维删除保护未满足。",
        "fr": "ECS 实例必须启用运维删除保护未满足。",
        "pt": "ECS 实例必须启用运维删除保护未满足。"
    },
    "recommendation": {
        "en": "Configure DeletionProtection on ALIYUN::ECS::Instance to satisfy the policy.",
        "zh": "请在 ALIYUN::ECS::Instance 上配置 DeletionProtection 以满足策略。",
        "ja": "请在 ALIYUN::ECS::Instance 上配置 DeletionProtection 以满足策略。",
        "de": "请在 ALIYUN::ECS::Instance 上配置 DeletionProtection 以满足策略。",
        "es": "请在 ALIYUN::ECS::Instance 上配置 DeletionProtection 以满足策略。",
        "fr": "请在 ALIYUN::ECS::Instance 上配置 DeletionProtection 以满足策略。",
        "pt": "请在 ALIYUN::ECS::Instance 上配置 DeletionProtection 以满足策略。"
    },
    "resource_types": ["ALIYUN::ECS::Instance"]
}

deny contains result if {
    some name, resource in helpers.resources_by_type("ALIYUN::ECS::Instance")
    not is_compliant(resource)
    result := {
        "id": rule_meta.id,
        "resource_id": name,
        "violation_path": ["Properties", "DeletionProtection"],
        "meta": {
            "severity": rule_meta.severity,
            "reason": rule_meta.reason,
            "recommendation": rule_meta.recommendation,
        },
    }
}

is_compliant(resource) if {
    helpers.get_property(resource, "DeletionProtection", false) == true
}
