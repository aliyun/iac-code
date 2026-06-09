package infraguard.rules.aliyun.rds_backup_policy_required

import rego.v1
import data.infraguard.helpers

rule_meta := {
    "id": "rds-backup-policy-required",
    "severity": "medium",
    "name": {
        "en": "RDS backup policy must be configured",
        "zh": "RDS 必须配置备份策略",
        "ja": "RDS 必须配置备份策略",
        "de": "RDS 必须配置备份策略",
        "es": "RDS 必须配置备份策略",
        "fr": "RDS 必须配置备份策略",
        "pt": "RDS 必须配置备份策略"
    },
    "description": {
        "en": "Checks RDS backup policy must be configured",
        "zh": "检查RDS 必须配置备份策略",
        "ja": "检查RDS 必须配置备份策略",
        "de": "检查RDS 必须配置备份策略",
        "es": "检查RDS 必须配置备份策略",
        "fr": "检查RDS 必须配置备份策略",
        "pt": "检查RDS 必须配置备份策略"
    },
    "reason": {
        "en": "RDS backup policy must be configured is not satisfied.",
        "zh": "RDS 必须配置备份策略未满足。",
        "ja": "RDS 必须配置备份策略未满足。",
        "de": "RDS 必须配置备份策略未满足。",
        "es": "RDS 必须配置备份策略未满足。",
        "fr": "RDS 必须配置备份策略未满足。",
        "pt": "RDS 必须配置备份策略未满足。"
    },
    "recommendation": {
        "en": "Configure BackupTime on ALIYUN::RDS::Backup to satisfy the policy.",
        "zh": "请在 ALIYUN::RDS::Backup 上配置 BackupTime 以满足策略。",
        "ja": "请在 ALIYUN::RDS::Backup 上配置 BackupTime 以满足策略。",
        "de": "请在 ALIYUN::RDS::Backup 上配置 BackupTime 以满足策略。",
        "es": "请在 ALIYUN::RDS::Backup 上配置 BackupTime 以满足策略。",
        "fr": "请在 ALIYUN::RDS::Backup 上配置 BackupTime 以满足策略。",
        "pt": "请在 ALIYUN::RDS::Backup 上配置 BackupTime 以满足策略。"
    },
    "resource_types": ["ALIYUN::RDS::Backup"]
}

deny contains result if {
    some name, resource in helpers.resources_by_type("ALIYUN::RDS::Backup")
    not is_compliant(resource)
    result := {
        "id": rule_meta.id,
        "resource_id": name,
        "violation_path": ["Properties", "BackupTime"],
        "meta": {
            "severity": rule_meta.severity,
            "reason": rule_meta.reason,
            "recommendation": rule_meta.recommendation,
        },
    }
}

is_compliant(resource) if {
    helpers.has_property(resource, "BackupTime")
}
