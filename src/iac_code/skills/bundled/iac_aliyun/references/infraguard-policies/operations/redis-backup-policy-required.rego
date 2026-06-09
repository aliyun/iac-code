package infraguard.rules.aliyun.redis_backup_policy_required

import rego.v1
import data.infraguard.helpers

rule_meta := {
    "id": "redis-backup-policy-required",
    "severity": "medium",
    "name": {
        "en": "Redis backup policy must be configured",
        "zh": "Redis 必须配置备份策略",
        "ja": "Redis 必须配置备份策略",
        "de": "Redis 必须配置备份策略",
        "es": "Redis 必须配置备份策略",
        "fr": "Redis 必须配置备份策略",
        "pt": "Redis 必须配置备份策略"
    },
    "description": {
        "en": "Checks Redis backup policy must be configured",
        "zh": "检查Redis 必须配置备份策略",
        "ja": "检查Redis 必须配置备份策略",
        "de": "检查Redis 必须配置备份策略",
        "es": "检查Redis 必须配置备份策略",
        "fr": "检查Redis 必须配置备份策略",
        "pt": "检查Redis 必须配置备份策略"
    },
    "reason": {
        "en": "Redis backup policy must be configured is not satisfied.",
        "zh": "Redis 必须配置备份策略未满足。",
        "ja": "Redis 必须配置备份策略未满足。",
        "de": "Redis 必须配置备份策略未满足。",
        "es": "Redis 必须配置备份策略未满足。",
        "fr": "Redis 必须配置备份策略未满足。",
        "pt": "Redis 必须配置备份策略未满足。"
    },
    "recommendation": {
        "en": "Configure BackupPolicy on ALIYUN::REDIS::Instance to satisfy the policy.",
        "zh": "请在 ALIYUN::REDIS::Instance 上配置 BackupPolicy 以满足策略。",
        "ja": "请在 ALIYUN::REDIS::Instance 上配置 BackupPolicy 以满足策略。",
        "de": "请在 ALIYUN::REDIS::Instance 上配置 BackupPolicy 以满足策略。",
        "es": "请在 ALIYUN::REDIS::Instance 上配置 BackupPolicy 以满足策略。",
        "fr": "请在 ALIYUN::REDIS::Instance 上配置 BackupPolicy 以满足策略。",
        "pt": "请在 ALIYUN::REDIS::Instance 上配置 BackupPolicy 以满足策略。"
    },
    "resource_types": ["ALIYUN::REDIS::Instance"]
}

deny contains result if {
    some name, resource in helpers.resources_by_type("ALIYUN::REDIS::Instance")
    not is_compliant(resource)
    result := {
        "id": rule_meta.id,
        "resource_id": name,
        "violation_path": ["Properties", "BackupPolicy"],
        "meta": {
            "severity": rule_meta.severity,
            "reason": rule_meta.reason,
            "recommendation": rule_meta.recommendation,
        },
    }
}

is_compliant(resource) if {
    helpers.has_property(resource, "BackupPolicy")
}
