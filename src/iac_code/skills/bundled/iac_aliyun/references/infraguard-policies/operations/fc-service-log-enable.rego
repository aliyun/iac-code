package infraguard.rules.aliyun.fc_service_log_enable

import rego.v1
import data.infraguard.helpers

rule_meta := {
    "id": "fc-service-log-enable",
    "severity": "medium",
    "name": {
        "en": "FC service must configure logging",
        "zh": "函数计算服务必须配置日志",
        "ja": "函数计算服务必须配置日志",
        "de": "函数计算服务必须配置日志",
        "es": "函数计算服务必须配置日志",
        "fr": "函数计算服务必须配置日志",
        "pt": "函数计算服务必须配置日志"
    },
    "description": {
        "en": "Checks FC service must configure logging",
        "zh": "检查函数计算服务必须配置日志",
        "ja": "检查函数计算服务必须配置日志",
        "de": "检查函数计算服务必须配置日志",
        "es": "检查函数计算服务必须配置日志",
        "fr": "检查函数计算服务必须配置日志",
        "pt": "检查函数计算服务必须配置日志"
    },
    "reason": {
        "en": "FC service must configure logging is not satisfied.",
        "zh": "函数计算服务必须配置日志未满足。",
        "ja": "函数计算服务必须配置日志未满足。",
        "de": "函数计算服务必须配置日志未满足。",
        "es": "函数计算服务必须配置日志未满足。",
        "fr": "函数计算服务必须配置日志未满足。",
        "pt": "函数计算服务必须配置日志未满足。"
    },
    "recommendation": {
        "en": "Configure LogConfig on ALIYUN::FC::Service to satisfy the policy.",
        "zh": "请在 ALIYUN::FC::Service 上配置 LogConfig 以满足策略。",
        "ja": "请在 ALIYUN::FC::Service 上配置 LogConfig 以满足策略。",
        "de": "请在 ALIYUN::FC::Service 上配置 LogConfig 以满足策略。",
        "es": "请在 ALIYUN::FC::Service 上配置 LogConfig 以满足策略。",
        "fr": "请在 ALIYUN::FC::Service 上配置 LogConfig 以满足策略。",
        "pt": "请在 ALIYUN::FC::Service 上配置 LogConfig 以满足策略。"
    },
    "resource_types": ["ALIYUN::FC::Service"]
}

deny contains result if {
    some name, resource in helpers.resources_by_type("ALIYUN::FC::Service")
    not is_compliant(resource)
    result := {
        "id": rule_meta.id,
        "resource_id": name,
        "violation_path": ["Properties", "LogConfig"],
        "meta": {
            "severity": rule_meta.severity,
            "reason": rule_meta.reason,
            "recommendation": rule_meta.recommendation,
        },
    }
}

is_compliant(resource) if {
    helpers.has_property(resource, "LogConfig")
}
