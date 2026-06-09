package infraguard.rules.aliyun.sls_project_description_required

import rego.v1
import data.infraguard.helpers

rule_meta := {
    "id": "sls-project-description-required",
    "severity": "medium",
    "name": {
        "en": "SLS project must configure description",
        "zh": "SLS Project 必须配置描述",
        "ja": "SLS Project 必须配置描述",
        "de": "SLS Project 必须配置描述",
        "es": "SLS Project 必须配置描述",
        "fr": "SLS Project 必须配置描述",
        "pt": "SLS Project 必须配置描述"
    },
    "description": {
        "en": "Checks SLS project must configure description",
        "zh": "检查SLS Project 必须配置描述",
        "ja": "检查SLS Project 必须配置描述",
        "de": "检查SLS Project 必须配置描述",
        "es": "检查SLS Project 必须配置描述",
        "fr": "检查SLS Project 必须配置描述",
        "pt": "检查SLS Project 必须配置描述"
    },
    "reason": {
        "en": "SLS project must configure description is not satisfied.",
        "zh": "SLS Project 必须配置描述未满足。",
        "ja": "SLS Project 必须配置描述未满足。",
        "de": "SLS Project 必须配置描述未满足。",
        "es": "SLS Project 必须配置描述未满足。",
        "fr": "SLS Project 必须配置描述未满足。",
        "pt": "SLS Project 必须配置描述未满足。"
    },
    "recommendation": {
        "en": "Configure Description on ALIYUN::SLS::Project to satisfy the policy.",
        "zh": "请在 ALIYUN::SLS::Project 上配置 Description 以满足策略。",
        "ja": "请在 ALIYUN::SLS::Project 上配置 Description 以满足策略。",
        "de": "请在 ALIYUN::SLS::Project 上配置 Description 以满足策略。",
        "es": "请在 ALIYUN::SLS::Project 上配置 Description 以满足策略。",
        "fr": "请在 ALIYUN::SLS::Project 上配置 Description 以满足策略。",
        "pt": "请在 ALIYUN::SLS::Project 上配置 Description 以满足策略。"
    },
    "resource_types": ["ALIYUN::SLS::Project"]
}

deny contains result if {
    some name, resource in helpers.resources_by_type("ALIYUN::SLS::Project")
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
