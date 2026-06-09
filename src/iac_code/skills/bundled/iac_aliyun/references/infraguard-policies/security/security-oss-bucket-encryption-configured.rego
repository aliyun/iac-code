package infraguard.rules.aliyun.security_oss_bucket_encryption_configured

import rego.v1
import data.infraguard.helpers

rule_meta := {
    "id": "security-oss-bucket-encryption-configured",
    "severity": "high",
    "name": {
        "en": "OSS bucket must configure server-side encryption",
        "zh": "OSS Bucket 必须配置服务端加密",
    },
    "description": {
        "en": "Checks OSS bucket must configure server-side encryption",
        "zh": "检查OSS Bucket 必须配置服务端加密",
    },
    "reason": {
        "en": "OSS bucket must configure server-side encryption is not satisfied.",
        "zh": "OSS Bucket 必须配置服务端加密未满足。",
    },
    "recommendation": {
        "en": "Configure ServerSideEncryptionConfiguration on ALIYUN::OSS::Bucket to satisfy the policy.",
        "zh": "请在 ALIYUN::OSS::Bucket 上配置 ServerSideEncryptionConfiguration 以满足策略。",
    },
    "resource_types": ["ALIYUN::OSS::Bucket"],
}

deny contains result if {
    some name, resource in helpers.resources_by_type("ALIYUN::OSS::Bucket")
    not is_compliant(resource)
    result := {
        "id": rule_meta.id,
        "resource_id": name,
        "violation_path": ["Properties", "ServerSideEncryptionConfiguration"],
        "meta": {
            "severity": rule_meta.severity,
            "reason": rule_meta.reason,
            "recommendation": rule_meta.recommendation,
        },
    }
}

is_compliant(resource) if {
    helpers.has_property(resource, "ServerSideEncryptionConfiguration")
}
