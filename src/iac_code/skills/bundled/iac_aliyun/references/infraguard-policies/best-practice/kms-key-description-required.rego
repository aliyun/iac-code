package infraguard.rules.aliyun.kms_key_description_required

import rego.v1
import data.infraguard.helpers

rule_meta := {
    "id": "kms-key-description-required",
    "severity": "medium",
    "name": {
        "en": "KMS key must configure description",
        "zh": "KMS 密钥必须配置描述",
        "ja": "KMS 密钥必须配置描述",
        "de": "KMS 密钥必须配置描述",
        "es": "KMS 密钥必须配置描述",
        "fr": "KMS 密钥必须配置描述",
        "pt": "KMS 密钥必须配置描述"
    },
    "description": {
        "en": "Checks KMS key must configure description",
        "zh": "检查KMS 密钥必须配置描述",
        "ja": "检查KMS 密钥必须配置描述",
        "de": "检查KMS 密钥必须配置描述",
        "es": "检查KMS 密钥必须配置描述",
        "fr": "检查KMS 密钥必须配置描述",
        "pt": "检查KMS 密钥必须配置描述"
    },
    "reason": {
        "en": "KMS key must configure description is not satisfied.",
        "zh": "KMS 密钥必须配置描述未满足。",
        "ja": "KMS 密钥必须配置描述未满足。",
        "de": "KMS 密钥必须配置描述未满足。",
        "es": "KMS 密钥必须配置描述未满足。",
        "fr": "KMS 密钥必须配置描述未满足。",
        "pt": "KMS 密钥必须配置描述未满足。"
    },
    "recommendation": {
        "en": "Configure Description on ALIYUN::KMS::Key to satisfy the policy.",
        "zh": "请在 ALIYUN::KMS::Key 上配置 Description 以满足策略。",
        "ja": "请在 ALIYUN::KMS::Key 上配置 Description 以满足策略。",
        "de": "请在 ALIYUN::KMS::Key 上配置 Description 以满足策略。",
        "es": "请在 ALIYUN::KMS::Key 上配置 Description 以满足策略。",
        "fr": "请在 ALIYUN::KMS::Key 上配置 Description 以满足策略。",
        "pt": "请在 ALIYUN::KMS::Key 上配置 Description 以满足策略。"
    },
    "resource_types": ["ALIYUN::KMS::Key"]
}

deny contains result if {
    some name, resource in helpers.resources_by_type("ALIYUN::KMS::Key")
    not is_compliant(resource)
    result := {
        "id": rule_meta.id,
        "resource_id": name,
        "violation_path": ["Properties", "Description"],
        "meta": {
            "severity": rule_meta.severity,
            "reason": rule_meta.reason,
            "recommendation": rule_meta.recommendation,
        },
    }
}

is_compliant(resource) if {
    helpers.has_property(resource, "Description")
}
