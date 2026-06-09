package infraguard.rules.aliyun.security_ram_user_mfa_required

import rego.v1
import data.infraguard.helpers

rule_meta := {
    "id": "security-ram-user-mfa-required",
    "severity": "high",
    "name": {
        "en": "RAM user must require MFA",
        "zh": "RAM 用户必须要求 MFA",
    },
    "description": {
        "en": "Checks RAM user must require MFA",
        "zh": "检查RAM 用户必须要求 MFA",
    },
    "reason": {
        "en": "RAM user must require MFA is not satisfied.",
        "zh": "RAM 用户必须要求 MFA未满足。",
    },
    "recommendation": {
        "en": "Configure MFABindRequired on ALIYUN::RAM::User to satisfy the policy.",
        "zh": "请在 ALIYUN::RAM::User 上配置 MFABindRequired 以满足策略。",
    },
    "resource_types": ["ALIYUN::RAM::User"],
}

deny contains result if {
    some name, resource in helpers.resources_by_type("ALIYUN::RAM::User")
    not is_compliant(resource)
    result := {
        "id": rule_meta.id,
        "resource_id": name,
        "violation_path": ["Properties", "MFABindRequired"],
        "meta": {
            "severity": rule_meta.severity,
            "reason": rule_meta.reason,
            "recommendation": rule_meta.recommendation,
        },
    }
}

is_compliant(resource) if {
    helpers.get_property(resource, "MFABindRequired", false) == true
}
