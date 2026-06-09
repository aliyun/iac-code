package infraguard.rules.aliyun.security_oss_bucket_private_acl

import rego.v1
import data.infraguard.helpers

rule_meta := {
    "id": "security-oss-bucket-private-acl",
    "severity": "high",
    "name": {
        "en": "OSS bucket ACL must be private",
        "zh": "OSS Bucket ACL 必须为私有",
    },
    "description": {
        "en": "Checks OSS bucket ACL must be private",
        "zh": "检查OSS Bucket ACL 必须为私有",
    },
    "reason": {
        "en": "OSS bucket ACL must be private is not satisfied.",
        "zh": "OSS Bucket ACL 必须为私有未满足。",
    },
    "recommendation": {
        "en": "Configure AccessControl on ALIYUN::OSS::Bucket to satisfy the policy.",
        "zh": "请在 ALIYUN::OSS::Bucket 上配置 AccessControl 以满足策略。",
    },
    "resource_types": ["ALIYUN::OSS::Bucket"],
}

deny contains result if {
    some name, resource in helpers.resources_by_type("ALIYUN::OSS::Bucket")
    not is_compliant(resource)
    result := {
        "id": rule_meta.id,
        "resource_id": name,
        "violation_path": ["Properties", "AccessControl"],
        "meta": {
            "severity": rule_meta.severity,
            "reason": rule_meta.reason,
            "recommendation": rule_meta.recommendation,
        },
    }
}

is_compliant(resource) if {
    helpers.get_property(resource, "AccessControl", "") == "private"
}
