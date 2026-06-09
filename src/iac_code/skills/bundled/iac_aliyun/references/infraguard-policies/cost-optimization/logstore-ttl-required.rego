package infraguard.rules.aliyun.logstore_ttl_required

import rego.v1
import data.infraguard.helpers

rule_meta := {
    "id": "logstore-ttl-required",
    "severity": "medium",
    "name": {
        "en": "SLS Logstore must set TTL",
        "zh": "SLS Logstore 必须设置数据保存时间",
        "ja": "SLS Logstore 必须设置数据保存时间",
        "de": "SLS Logstore 必须设置数据保存时间",
        "es": "SLS Logstore 必须设置数据保存时间",
        "fr": "SLS Logstore 必须设置数据保存时间",
        "pt": "SLS Logstore 必须设置数据保存时间"
    },
    "description": {
        "en": "Checks SLS Logstore must set TTL",
        "zh": "检查SLS Logstore 必须设置数据保存时间",
        "ja": "检查SLS Logstore 必须设置数据保存时间",
        "de": "检查SLS Logstore 必须设置数据保存时间",
        "es": "检查SLS Logstore 必须设置数据保存时间",
        "fr": "检查SLS Logstore 必须设置数据保存时间",
        "pt": "检查SLS Logstore 必须设置数据保存时间"
    },
    "reason": {
        "en": "SLS Logstore must set TTL is not satisfied.",
        "zh": "SLS Logstore 必须设置数据保存时间未满足。",
        "ja": "SLS Logstore 必须设置数据保存时间未满足。",
        "de": "SLS Logstore 必须设置数据保存时间未满足。",
        "es": "SLS Logstore 必须设置数据保存时间未满足。",
        "fr": "SLS Logstore 必须设置数据保存时间未满足。",
        "pt": "SLS Logstore 必须设置数据保存时间未满足。"
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
