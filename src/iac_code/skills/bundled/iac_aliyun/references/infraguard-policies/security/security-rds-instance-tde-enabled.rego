package infraguard.rules.aliyun.security_rds_instance_tde_enabled

import rego.v1
import data.infraguard.helpers

rule_meta := {
    "id": "security-rds-instance-tde-enabled",
    "severity": "high",
    "name": {
        "en": "RDS instance must enable TDE",
        "zh": "RDS 实例必须启用 TDE",
    },
    "description": {
        "en": "Checks RDS instance must enable TDE",
        "zh": "检查RDS 实例必须启用 TDE",
    },
    "reason": {
        "en": "RDS instance must enable TDE is not satisfied.",
        "zh": "RDS 实例必须启用 TDE未满足。",
    },
    "recommendation": {
        "en": "Configure TDEStatus on ALIYUN::RDS::DBInstance to satisfy the policy.",
        "zh": "请在 ALIYUN::RDS::DBInstance 上配置 TDEStatus 以满足策略。",
    },
    "resource_types": ["ALIYUN::RDS::DBInstance"],
}

deny contains result if {
    some name, resource in helpers.resources_by_type("ALIYUN::RDS::DBInstance")
    not is_compliant(resource)
    result := {
        "id": rule_meta.id,
        "resource_id": name,
        "violation_path": ["Properties", "TDEStatus"],
        "meta": {
            "severity": rule_meta.severity,
            "reason": rule_meta.reason,
            "recommendation": rule_meta.recommendation,
        },
    }
}

is_compliant(resource) if {
    helpers.get_property(resource, "TDEStatus", "") == "Enabled"
}
