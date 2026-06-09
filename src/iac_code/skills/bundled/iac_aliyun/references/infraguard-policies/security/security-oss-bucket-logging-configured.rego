package infraguard.rules.aliyun.security_oss_bucket_logging_configured

import rego.v1
import data.infraguard.helpers

rule_meta := {
    "id": "security-oss-bucket-logging-configured",
    "severity": "medium",
    "name": {
        "en": "OSS bucket must configure access logging",
        "zh": "OSS Bucket 必须配置访问日志",
    },
    "description": {
        "en": "Checks OSS bucket must configure access logging",
        "zh": "检查OSS Bucket 必须配置访问日志",
    },
    "reason": {
        "en": "OSS bucket must configure access logging is not satisfied.",
        "zh": "OSS Bucket 必须配置访问日志未满足。",
    },
    "recommendation": {
        "en": "Configure LoggingConfiguration on ALIYUN::OSS::Bucket to satisfy the policy.",
        "zh": "请在 ALIYUN::OSS::Bucket 上配置 LoggingConfiguration 以满足策略。",
    },
    "resource_types": ["ALIYUN::OSS::Bucket"],
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
