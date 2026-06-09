package infraguard.rules.aliyun.sls_logstore_ttl_configured

import rego.v1
import data.infraguard.helpers

rule_meta := {
    "id": "sls-logstore-ttl-configured",
    "severity": "medium",
    "name": {
        "en": "SLS Logstore must configure TTL",
        "zh": "SLS Logstore 必须配置 TTL",
        "ja": "SLS Logstore 必须配置 TTL",
        "de": "SLS Logstore 必须配置 TTL",
        "es": "SLS Logstore 必须配置 TTL",
        "fr": "SLS Logstore 必须配置 TTL",
        "pt": "SLS Logstore 必须配置 TTL"
    },
    "description": {
        "en": "Checks SLS Logstore must configure TTL",
        "zh": "检查SLS Logstore 必须配置 TTL",
        "ja": "检查SLS Logstore 必须配置 TTL",
        "de": "检查SLS Logstore 必须配置 TTL",
        "es": "检查SLS Logstore 必须配置 TTL",
        "fr": "检查SLS Logstore 必须配置 TTL",
        "pt": "检查SLS Logstore 必须配置 TTL"
    },
    "reason": {
        "en": "SLS Logstore must configure TTL is not satisfied.",
        "zh": "SLS Logstore 必须配置 TTL未满足。",
        "ja": "SLS Logstore 必须配置 TTL未满足。",
        "de": "SLS Logstore 必须配置 TTL未满足。",
        "es": "SLS Logstore 必须配置 TTL未满足。",
        "fr": "SLS Logstore 必须配置 TTL未满足。",
        "pt": "SLS Logstore 必须配置 TTL未满足。"
    },
    "recommendation": {
        "en": "Configure TTL on ALIYUN::SLS::Logstore to satisfy the policy.",
        "zh": "请在 ALIYUN::SLS::Logstore 上配置 TTL 以满足策略。",
        "ja": "请在 ALIYUN::SLS::Logstore 上配置 TTL 以满足策略。",
        "de": "请在 ALIYUN::SLS::Logstore 上配置 TTL 以满足策略。",
        "es": "请在 ALIYUN::SLS::Logstore 上配置 TTL 以满足策略。",
        "fr": "请在 ALIYUN::SLS::Logstore 上配置 TTL 以满足策略。",
        "pt": "请在 ALIYUN::SLS::Logstore 上配置 TTL 以满足策略。"
    },
    "resource_types": ["ALIYUN::SLS::Logstore"]
}

deny contains result if {
    some name, resource in helpers.resources_by_type("ALIYUN::SLS::Logstore")
    not is_compliant(resource)
    result := {
        "id": rule_meta.id,
        "resource_id": name,
        "violation_path": ["Properties", "TTL"],
        "meta": {
            "severity": rule_meta.severity,
            "reason": rule_meta.reason,
            "recommendation": rule_meta.recommendation,
        },
    }
}

is_compliant(resource) if {
    helpers.has_property(resource, "TTL")
}
