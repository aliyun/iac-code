package infraguard.rules.aliyun.redis_instance_class_required

import rego.v1
import data.infraguard.helpers

rule_meta := {
    "id": "redis-instance-class-required",
    "severity": "medium",
    "name": {
        "en": "Redis instance must set instance class",
        "zh": "Redis 实例必须设置规格",
        "ja": "Redis 实例必须设置规格",
        "de": "Redis 实例必须设置规格",
        "es": "Redis 实例必须设置规格",
        "fr": "Redis 实例必须设置规格",
        "pt": "Redis 实例必须设置规格"
    },
    "description": {
        "en": "Checks Redis instance must set instance class",
        "zh": "检查Redis 实例必须设置规格",
        "ja": "检查Redis 实例必须设置规格",
        "de": "检查Redis 实例必须设置规格",
        "es": "检查Redis 实例必须设置规格",
        "fr": "检查Redis 实例必须设置规格",
        "pt": "检查Redis 实例必须设置规格"
    },
    "reason": {
        "en": "Redis instance must set instance class is not satisfied.",
        "zh": "Redis 实例必须设置规格未满足。",
        "ja": "Redis 实例必须设置规格未满足。",
        "de": "Redis 实例必须设置规格未满足。",
        "es": "Redis 实例必须设置规格未满足。",
        "fr": "Redis 实例必须设置规格未满足。",
        "pt": "Redis 实例必须设置规格未满足。"
    },
    "recommendation": {
        "en": "Configure InstanceClass on ALIYUN::REDIS::Instance to satisfy the policy.",
        "zh": "请在 ALIYUN::REDIS::Instance 上配置 InstanceClass 以满足策略。",
        "ja": "请在 ALIYUN::REDIS::Instance 上配置 InstanceClass 以满足策略。",
        "de": "请在 ALIYUN::REDIS::Instance 上配置 InstanceClass 以满足策略。",
        "es": "请在 ALIYUN::REDIS::Instance 上配置 InstanceClass 以满足策略。",
        "fr": "请在 ALIYUN::REDIS::Instance 上配置 InstanceClass 以满足策略。",
        "pt": "请在 ALIYUN::REDIS::Instance 上配置 InstanceClass 以满足策略。"
    },
    "resource_types": ["ALIYUN::REDIS::Instance"]
}

deny contains result if {
    some name, resource in helpers.resources_by_type("ALIYUN::REDIS::Instance")
    not is_compliant(resource)
    result := {
        "id": rule_meta.id,
        "resource_id": name,
        "violation_path": ["Properties", "InstanceClass"],
        "meta": {
            "severity": rule_meta.severity,
            "reason": rule_meta.reason,
            "recommendation": rule_meta.recommendation,
        },
    }
}

is_compliant(resource) if {
    helpers.has_property(resource, "InstanceClass")
}
