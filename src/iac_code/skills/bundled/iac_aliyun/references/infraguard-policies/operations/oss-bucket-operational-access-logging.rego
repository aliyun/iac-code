package infraguard.rules.aliyun.oss_bucket_operational_access_logging

import rego.v1
import data.infraguard.helpers

rule_meta := {
    "id": "oss-bucket-operational-access-logging",
    "severity": "medium",
    "name": {
        "en": "OSS bucket must enable logging",
        "zh": "OSS Bucket 必须启用日志",
        "ja": "OSS Bucket 必须启用日志",
        "de": "OSS Bucket 必须启用日志",
        "es": "OSS Bucket 必须启用日志",
        "fr": "OSS Bucket 必须启用日志",
        "pt": "OSS Bucket 必须启用日志"
    },
    "description": {
        "en": "Checks OSS bucket must enable logging",
        "zh": "检查OSS Bucket 必须启用日志",
        "ja": "检查OSS Bucket 必须启用日志",
        "de": "检查OSS Bucket 必须启用日志",
        "es": "检查OSS Bucket 必须启用日志",
        "fr": "检查OSS Bucket 必须启用日志",
        "pt": "检查OSS Bucket 必须启用日志"
    },
    "reason": {
        "en": "OSS bucket must enable logging is not satisfied.",
        "zh": "OSS Bucket 必须启用日志未满足。",
        "ja": "OSS Bucket 必须启用日志未满足。",
        "de": "OSS Bucket 必须启用日志未满足。",
        "es": "OSS Bucket 必须启用日志未满足。",
        "fr": "OSS Bucket 必须启用日志未满足。",
        "pt": "OSS Bucket 必须启用日志未满足。"
    },
    "recommendation": {
        "en": "Configure LoggingConfiguration on ALIYUN::OSS::Bucket to satisfy the policy.",
        "zh": "请在 ALIYUN::OSS::Bucket 上配置 LoggingConfiguration 以满足策略。",
        "ja": "请在 ALIYUN::OSS::Bucket 上配置 LoggingConfiguration 以满足策略。",
        "de": "请在 ALIYUN::OSS::Bucket 上配置 LoggingConfiguration 以满足策略。",
        "es": "请在 ALIYUN::OSS::Bucket 上配置 LoggingConfiguration 以满足策略。",
        "fr": "请在 ALIYUN::OSS::Bucket 上配置 LoggingConfiguration 以满足策略。",
        "pt": "请在 ALIYUN::OSS::Bucket 上配置 LoggingConfiguration 以满足策略。"
    },
    "resource_types": ["ALIYUN::OSS::Bucket"]
}

deny contains result if {
    some name, resource in helpers.resources_by_type("ALIYUN::OSS::Bucket")
    not is_compliant(resource)
    result := {
        "id": rule_meta.id,
        "resource_id": name,
        "violation_path": ["Properties", "LoggingConfiguration"],
        "meta": {
            "severity": rule_meta.severity,
            "reason": rule_meta.reason,
            "recommendation": rule_meta.recommendation,
        },
    }
}

is_compliant(resource) if {
    helpers.has_property(resource, "LoggingConfiguration")
}
