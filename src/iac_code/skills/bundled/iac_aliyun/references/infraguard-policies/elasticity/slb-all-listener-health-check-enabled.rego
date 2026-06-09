package infraguard.rules.aliyun.slb_all_listener_health_check_enabled

import rego.v1
import data.infraguard.helpers

rule_meta := {
    "id": "slb-all-listener-health-check-enabled",
    "severity": "medium",
    "name": {
        "en": "SLB listener must configure backend server port",
        "zh": "SLB 监听必须配置后端端口",
        "ja": "SLB 监听必须配置后端端口",
        "de": "SLB 监听必须配置后端端口",
        "es": "SLB 监听必须配置后端端口",
        "fr": "SLB 监听必须配置后端端口",
        "pt": "SLB 监听必须配置后端端口"
    },
    "description": {
        "en": "Checks SLB listener must configure backend server port",
        "zh": "检查SLB 监听必须配置后端端口",
        "ja": "检查SLB 监听必须配置后端端口",
        "de": "检查SLB 监听必须配置后端端口",
        "es": "检查SLB 监听必须配置后端端口",
        "fr": "检查SLB 监听必须配置后端端口",
        "pt": "检查SLB 监听必须配置后端端口"
    },
    "reason": {
        "en": "SLB listener must configure backend server port is not satisfied.",
        "zh": "SLB 监听必须配置后端端口未满足。",
        "ja": "SLB 监听必须配置后端端口未满足。",
        "de": "SLB 监听必须配置后端端口未满足。",
        "es": "SLB 监听必须配置后端端口未满足。",
        "fr": "SLB 监听必须配置后端端口未满足。",
        "pt": "SLB 监听必须配置后端端口未满足。"
    },
    "recommendation": {
        "en": "Configure BackendServerPort on ALIYUN::SLB::Listener to satisfy the policy.",
        "zh": "请在 ALIYUN::SLB::Listener 上配置 BackendServerPort 以满足策略。",
        "ja": "请在 ALIYUN::SLB::Listener 上配置 BackendServerPort 以满足策略。",
        "de": "请在 ALIYUN::SLB::Listener 上配置 BackendServerPort 以满足策略。",
        "es": "请在 ALIYUN::SLB::Listener 上配置 BackendServerPort 以满足策略。",
        "fr": "请在 ALIYUN::SLB::Listener 上配置 BackendServerPort 以满足策略。",
        "pt": "请在 ALIYUN::SLB::Listener 上配置 BackendServerPort 以满足策略。"
    },
    "resource_types": ["ALIYUN::SLB::Listener"]
}

deny contains result if {
    some name, resource in helpers.resources_by_type("ALIYUN::SLB::Listener")
    not is_compliant(resource)
    result := {
        "id": rule_meta.id,
        "resource_id": name,
        "violation_path": ["Properties", "BackendServerPort"],
        "meta": {
            "severity": rule_meta.severity,
            "reason": rule_meta.reason,
            "recommendation": rule_meta.recommendation,
        },
    }
}

is_compliant(resource) if {
    helpers.has_property(resource, "BackendServerPort")
}
