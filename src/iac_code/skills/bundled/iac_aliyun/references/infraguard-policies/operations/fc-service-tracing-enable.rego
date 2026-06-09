package infraguard.rules.aliyun.fc_service_tracing_enable

import rego.v1
import data.infraguard.helpers

rule_meta := {
    "id": "fc-service-tracing-enable",
    "severity": "medium",
    "name": {
        "en": "FC service must configure tracing",
        "zh": "函数计算服务必须配置链路追踪",
        "ja": "函数计算服务必须配置链路追踪",
        "de": "函数计算服务必须配置链路追踪",
        "es": "函数计算服务必须配置链路追踪",
        "fr": "函数计算服务必须配置链路追踪",
        "pt": "函数计算服务必须配置链路追踪"
    },
    "description": {
        "en": "Checks FC service must configure tracing",
        "zh": "检查函数计算服务必须配置链路追踪",
        "ja": "检查函数计算服务必须配置链路追踪",
        "de": "检查函数计算服务必须配置链路追踪",
        "es": "检查函数计算服务必须配置链路追踪",
        "fr": "检查函数计算服务必须配置链路追踪",
        "pt": "检查函数计算服务必须配置链路追踪"
    },
    "reason": {
        "en": "FC service must configure tracing is not satisfied.",
        "zh": "函数计算服务必须配置链路追踪未满足。",
        "ja": "函数计算服务必须配置链路追踪未满足。",
        "de": "函数计算服务必须配置链路追踪未满足。",
        "es": "函数计算服务必须配置链路追踪未满足。",
        "fr": "函数计算服务必须配置链路追踪未满足。",
        "pt": "函数计算服务必须配置链路追踪未满足。"
    },
    "recommendation": {
        "en": "Configure TracingConfig on ALIYUN::FC::Service to satisfy the policy.",
        "zh": "请在 ALIYUN::FC::Service 上配置 TracingConfig 以满足策略。",
        "ja": "请在 ALIYUN::FC::Service 上配置 TracingConfig 以满足策略。",
        "de": "请在 ALIYUN::FC::Service 上配置 TracingConfig 以满足策略。",
        "es": "请在 ALIYUN::FC::Service 上配置 TracingConfig 以满足策略。",
        "fr": "请在 ALIYUN::FC::Service 上配置 TracingConfig 以满足策略。",
        "pt": "请在 ALIYUN::FC::Service 上配置 TracingConfig 以满足策略。"
    },
    "resource_types": ["ALIYUN::FC::Service"]
}

deny contains result if {
    some name, resource in helpers.resources_by_type("ALIYUN::FC::Service")
    not is_compliant(resource)
    result := {
        "id": rule_meta.id,
        "resource_id": name,
        "violation_path": ["Properties", "TracingConfig"],
        "meta": {
            "severity": rule_meta.severity,
            "reason": rule_meta.reason,
            "recommendation": rule_meta.recommendation,
        },
    }
}

is_compliant(resource) if {
    helpers.has_property(resource, "TracingConfig")
}
