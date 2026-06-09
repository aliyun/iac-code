package infraguard.rules.aliyun.security_rds_instance_ssl_required

import rego.v1
import data.infraguard.helpers

rule_meta := {
    "id": "security-rds-instance-ssl-required",
    "severity": "high",
    "name": {
        "en": "RDS instance must configure SSL",
        "zh": "RDS 实例必须配置 SSL",
    },
    "description": {
        "en": "Checks RDS instance must configure SSL",
        "zh": "检查RDS 实例必须配置 SSL",
    },
    "reason": {
        "en": "RDS instance must configure SSL is not satisfied.",
        "zh": "RDS 实例必须配置 SSL未满足。",
    },
    "recommendation": {
        "en": "Configure SSLSetting on ALIYUN::RDS::DBInstance to satisfy the policy.",
        "zh": "请在 ALIYUN::RDS::DBInstance 上配置 SSLSetting 以满足策略。",
    },
    "resource_types": ["ALIYUN::RDS::DBInstance"],
}

deny contains result if {
    some name, resource in helpers.resources_by_type("ALIYUN::RDS::DBInstance")
    not is_compliant(resource)
    result := {
        "id": rule_meta.id,
        "resource_id": name,
        "violation_path": ["Properties", "SSLSetting"],
        "meta": {
            "severity": rule_meta.severity,
            "reason": rule_meta.reason,
            "recommendation": rule_meta.recommendation,
        },
    }
}

is_compliant(resource) if {
    helpers.has_property(resource, "SSLSetting")
}
