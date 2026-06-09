package infraguard.rules.aliyun.alb_all_listenter_has_server

import rego.v1
import data.infraguard.helpers

rule_meta := {
    "id": "alb-all-listenter-has-server",
    "severity": "medium",
    "name": {
        "en": "ALB listener must configure server group",
        "zh": "ALB 监听必须配置服务器组",
        "ja": "ALB 监听必须配置服务器组",
        "de": "ALB 监听必须配置服务器组",
        "es": "ALB 监听必须配置服务器组",
        "fr": "ALB 监听必须配置服务器组",
        "pt": "ALB 监听必须配置服务器组"
    },
    "description": {
        "en": "Checks ALB listener must configure server group",
        "zh": "检查ALB 监听必须配置服务器组",
        "ja": "检查ALB 监听必须配置服务器组",
        "de": "检查ALB 监听必须配置服务器组",
        "es": "检查ALB 监听必须配置服务器组",
        "fr": "检查ALB 监听必须配置服务器组",
        "pt": "检查ALB 监听必须配置服务器组"
    },
    "reason": {
        "en": "ALB listener must configure server group is not satisfied.",
        "zh": "ALB 监听必须配置服务器组未满足。",
        "ja": "ALB 监听必须配置服务器组未满足。",
        "de": "ALB 监听必须配置服务器组未满足。",
        "es": "ALB 监听必须配置服务器组未满足。",
        "fr": "ALB 监听必须配置服务器组未满足。",
        "pt": "ALB 监听必须配置服务器组未满足。"
    },
    "recommendation": {
        "en": "Configure DefaultActions on ALIYUN::ALB::Listener to satisfy the policy.",
        "zh": "请在 ALIYUN::ALB::Listener 上配置 DefaultActions 以满足策略。",
        "ja": "请在 ALIYUN::ALB::Listener 上配置 DefaultActions 以满足策略。",
        "de": "请在 ALIYUN::ALB::Listener 上配置 DefaultActions 以满足策略。",
        "es": "请在 ALIYUN::ALB::Listener 上配置 DefaultActions 以满足策略。",
        "fr": "请在 ALIYUN::ALB::Listener 上配置 DefaultActions 以满足策略。",
        "pt": "请在 ALIYUN::ALB::Listener 上配置 DefaultActions 以满足策略。"
    },
    "resource_types": ["ALIYUN::ALB::Listener"]
}

deny contains result if {
    some name, resource in helpers.resources_by_type("ALIYUN::ALB::Listener")
    not is_compliant(resource)
    result := {
        "id": rule_meta.id,
        "resource_id": name,
        "violation_path": ["Properties", "DefaultActions"],
        "meta": {
            "severity": rule_meta.severity,
            "reason": rule_meta.reason,
            "recommendation": rule_meta.recommendation,
        },
    }
}

is_compliant(resource) if {
    helpers.has_property(resource, "DefaultActions")
}
