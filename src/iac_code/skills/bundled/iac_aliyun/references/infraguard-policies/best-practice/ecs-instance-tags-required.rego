package infraguard.rules.aliyun.ecs_instance_tags_required

import rego.v1
import data.infraguard.helpers

rule_meta := {
    "id": "ecs-instance-tags-required",
    "severity": "medium",
    "name": {
        "en": "ECS instance must configure tags",
        "zh": "ECS 实例必须配置标签",
        "ja": "ECS 实例必须配置标签",
        "de": "ECS 实例必须配置标签",
        "es": "ECS 实例必须配置标签",
        "fr": "ECS 实例必须配置标签",
        "pt": "ECS 实例必须配置标签"
    },
    "description": {
        "en": "Checks ECS instance must configure tags",
        "zh": "检查ECS 实例必须配置标签",
        "ja": "检查ECS 实例必须配置标签",
        "de": "检查ECS 实例必须配置标签",
        "es": "检查ECS 实例必须配置标签",
        "fr": "检查ECS 实例必须配置标签",
        "pt": "检查ECS 实例必须配置标签"
    },
    "reason": {
        "en": "ECS instance must configure tags is not satisfied.",
        "zh": "ECS 实例必须配置标签未满足。",
        "ja": "ECS 实例必须配置标签未满足。",
        "de": "ECS 实例必须配置标签未满足。",
        "es": "ECS 实例必须配置标签未满足。",
        "fr": "ECS 实例必须配置标签未满足。",
        "pt": "ECS 实例必须配置标签未满足。"
    },
    "recommendation": {
        "en": "Configure Tags on ALIYUN::ECS::Instance to satisfy the policy.",
        "zh": "请在 ALIYUN::ECS::Instance 上配置 Tags 以满足策略。",
        "ja": "请在 ALIYUN::ECS::Instance 上配置 Tags 以满足策略。",
        "de": "请在 ALIYUN::ECS::Instance 上配置 Tags 以满足策略。",
        "es": "请在 ALIYUN::ECS::Instance 上配置 Tags 以满足策略。",
        "fr": "请在 ALIYUN::ECS::Instance 上配置 Tags 以满足策略。",
        "pt": "请在 ALIYUN::ECS::Instance 上配置 Tags 以满足策略。"
    },
    "resource_types": ["ALIYUN::ECS::Instance"]
}

deny contains result if {
    some name, resource in helpers.resources_by_type("ALIYUN::ECS::Instance")
    not is_compliant(resource)
    result := {
        "id": rule_meta.id,
        "resource_id": name,
        "violation_path": ["Properties", "Tags"],
        "meta": {
            "severity": rule_meta.severity,
            "reason": rule_meta.reason,
            "recommendation": rule_meta.recommendation,
        },
    }
}

is_compliant(resource) if {
    helpers.has_property(resource, "Tags")
}
