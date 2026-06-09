package infraguard.rules.aliyun.redis_instance_name_required

import rego.v1
import data.infraguard.helpers

rule_meta := {
    "id": "redis-instance-name-required",
    "severity": "medium",
    "name": {
        "en": "Redis instance must configure name",
        "zh": "Redis 实例必须配置名称",
        "ja": "Redis 实例必须配置名称",
        "de": "Redis 实例必须配置名称",
        "es": "Redis 实例必须配置名称",
        "fr": "Redis 实例必须配置名称",
        "pt": "Redis 实例必须配置名称"
    },
    "description": {
        "en": "Checks Redis instance must configure name",
        "zh": "检查Redis 实例必须配置名称",
        "ja": "检查Redis 实例必须配置名称",
        "de": "检查Redis 实例必须配置名称",
        "es": "检查Redis 实例必须配置名称",
        "fr": "检查Redis 实例必须配置名称",
        "pt": "检查Redis 实例必须配置名称"
    },
    "reason": {
        "en": "Redis instance must configure name is not satisfied.",
        "zh": "Redis 实例必须配置名称未满足。",
        "ja": "Redis 实例必须配置名称未满足。",
        "de": "Redis 实例必须配置名称未满足。",
        "es": "Redis 实例必须配置名称未满足。",
        "fr": "Redis 实例必须配置名称未满足。",
        "pt": "Redis 实例必须配置名称未满足。"
    },
    "recommendation": {
        "en": "Configure InstanceName on ALIYUN::REDIS::Instance to satisfy the policy.",
        "zh": "请在 ALIYUN::REDIS::Instance 上配置 InstanceName 以满足策略。",
        "ja": "请在 ALIYUN::REDIS::Instance 上配置 InstanceName 以满足策略。",
        "de": "请在 ALIYUN::REDIS::Instance 上配置 InstanceName 以满足策略。",
        "es": "请在 ALIYUN::REDIS::Instance 上配置 InstanceName 以满足策略。",
        "fr": "请在 ALIYUN::REDIS::Instance 上配置 InstanceName 以满足策略。",
        "pt": "请在 ALIYUN::REDIS::Instance 上配置 InstanceName 以满足策略。"
    },
    "resource_types": ["ALIYUN::REDIS::Instance"]
}

deny contains result if {
    some name, resource in helpers.resources_by_type("ALIYUN::REDIS::Instance")
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
