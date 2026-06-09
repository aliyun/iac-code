package infraguard.rules.aliyun.fc_function_timeout_configured

import rego.v1
import data.infraguard.helpers

rule_meta := {
    "id": "fc-function-timeout-configured",
    "severity": "medium",
    "name": {
        "en": "FC function must configure timeout",
        "zh": "函数计算函数必须配置超时时间",
        "ja": "函数计算函数必须配置超时时间",
        "de": "函数计算函数必须配置超时时间",
        "es": "函数计算函数必须配置超时时间",
        "fr": "函数计算函数必须配置超时时间",
        "pt": "函数计算函数必须配置超时时间"
    },
    "description": {
        "en": "Checks FC function must configure timeout",
        "zh": "检查函数计算函数必须配置超时时间",
        "ja": "检查函数计算函数必须配置超时时间",
        "de": "检查函数计算函数必须配置超时时间",
        "es": "检查函数计算函数必须配置超时时间",
        "fr": "检查函数计算函数必须配置超时时间",
        "pt": "检查函数计算函数必须配置超时时间"
    },
    "reason": {
        "en": "FC function must configure timeout is not satisfied.",
        "zh": "函数计算函数必须配置超时时间未满足。",
        "ja": "函数计算函数必须配置超时时间未满足。",
        "de": "函数计算函数必须配置超时时间未满足。",
        "es": "函数计算函数必须配置超时时间未满足。",
        "fr": "函数计算函数必须配置超时时间未满足。",
        "pt": "函数计算函数必须配置超时时间未满足。"
    },
    "recommendation": {
        "en": "Configure Timeout on ALIYUN::FC::Function to satisfy the policy.",
        "zh": "请在 ALIYUN::FC::Function 上配置 Timeout 以满足策略。",
        "ja": "请在 ALIYUN::FC::Function 上配置 Timeout 以满足策略。",
        "de": "请在 ALIYUN::FC::Function 上配置 Timeout 以满足策略。",
        "es": "请在 ALIYUN::FC::Function 上配置 Timeout 以满足策略。",
        "fr": "请在 ALIYUN::FC::Function 上配置 Timeout 以满足策略。",
        "pt": "请在 ALIYUN::FC::Function 上配置 Timeout 以满足策略。"
    },
    "resource_types": ["ALIYUN::FC::Function"]
}

deny contains result if {
    some name, resource in helpers.resources_by_type("ALIYUN::FC::Function")
    not is_compliant(resource)
    result := {
        "id": rule_meta.id,
        "resource_id": name,
        "violation_path": ["Properties", "Timeout"],
        "meta": {
            "severity": rule_meta.severity,
            "reason": rule_meta.reason,
            "recommendation": rule_meta.recommendation,
        },
    }
}

is_compliant(resource) if {
    helpers.has_property(resource, "Timeout")
}
