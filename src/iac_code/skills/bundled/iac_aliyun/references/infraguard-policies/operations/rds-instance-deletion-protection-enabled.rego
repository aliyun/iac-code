package infraguard.rules.aliyun.rds_instance_deletion_protection_enabled

import rego.v1
import data.infraguard.helpers

rule_meta := {
    "id": "rds-instance-deletion-protection-enabled",
    "severity": "medium",
    "name": {
        "en": "RDS instance must enable deletion protection",
        "zh": "RDS 实例必须启用删除保护",
        "ja": "RDS 实例必须启用删除保护",
        "de": "RDS 实例必须启用删除保护",
        "es": "RDS 实例必须启用删除保护",
        "fr": "RDS 实例必须启用删除保护",
        "pt": "RDS 实例必须启用删除保护"
    },
    "description": {
        "en": "Checks RDS instance must enable deletion protection",
        "zh": "检查RDS 实例必须启用删除保护",
        "ja": "检查RDS 实例必须启用删除保护",
        "de": "检查RDS 实例必须启用删除保护",
        "es": "检查RDS 实例必须启用删除保护",
        "fr": "检查RDS 实例必须启用删除保护",
        "pt": "检查RDS 实例必须启用删除保护"
    },
    "reason": {
        "en": "RDS instance must enable deletion protection is not satisfied.",
        "zh": "RDS 实例必须启用删除保护未满足。",
        "ja": "RDS 实例必须启用删除保护未满足。",
        "de": "RDS 实例必须启用删除保护未满足。",
        "es": "RDS 实例必须启用删除保护未满足。",
        "fr": "RDS 实例必须启用删除保护未满足。",
        "pt": "RDS 实例必须启用删除保护未满足。"
    },
    "recommendation": {
        "en": "Configure DeletionProtection on ALIYUN::RDS::DBInstance to satisfy the policy.",
        "zh": "请在 ALIYUN::RDS::DBInstance 上配置 DeletionProtection 以满足策略。",
        "ja": "请在 ALIYUN::RDS::DBInstance 上配置 DeletionProtection 以满足策略。",
        "de": "请在 ALIYUN::RDS::DBInstance 上配置 DeletionProtection 以满足策略。",
        "es": "请在 ALIYUN::RDS::DBInstance 上配置 DeletionProtection 以满足策略。",
        "fr": "请在 ALIYUN::RDS::DBInstance 上配置 DeletionProtection 以满足策略。",
        "pt": "请在 ALIYUN::RDS::DBInstance 上配置 DeletionProtection 以满足策略。"
    },
    "resource_types": ["ALIYUN::RDS::DBInstance"]
}

deny contains result if {
    some name, resource in helpers.resources_by_type("ALIYUN::RDS::DBInstance")
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
