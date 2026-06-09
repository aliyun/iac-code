package infraguard.rules.aliyun.vpc_cidr_required

import rego.v1
import data.infraguard.helpers

rule_meta := {
    "id": "vpc-cidr-required",
    "severity": "high",
    "name": {
        "en": "VPC must configure CIDR block",
        "zh": "VPC 必须配置网段",
        "ja": "VPC 必须配置网段",
        "de": "VPC 必须配置网段",
        "es": "VPC 必须配置网段",
        "fr": "VPC 必须配置网段",
        "pt": "VPC 必须配置网段"
    },
    "description": {
        "en": "Checks VPC must configure CIDR block",
        "zh": "检查VPC 必须配置网段",
        "ja": "检查VPC 必须配置网段",
        "de": "检查VPC 必须配置网段",
        "es": "检查VPC 必须配置网段",
        "fr": "检查VPC 必须配置网段",
        "pt": "检查VPC 必须配置网段"
    },
    "reason": {
        "en": "VPC must configure CIDR block is not satisfied.",
        "zh": "VPC 必须配置网段未满足。",
        "ja": "VPC 必须配置网段未满足。",
        "de": "VPC 必须配置网段未满足。",
        "es": "VPC 必须配置网段未满足。",
        "fr": "VPC 必须配置网段未满足。",
        "pt": "VPC 必须配置网段未满足。"
    },
    "recommendation": {
        "en": "Configure CidrBlock on ALIYUN::ECS::VPC to satisfy the policy.",
        "zh": "请在 ALIYUN::ECS::VPC 上配置 CidrBlock 以满足策略。",
        "ja": "请在 ALIYUN::ECS::VPC 上配置 CidrBlock 以满足策略。",
        "de": "请在 ALIYUN::ECS::VPC 上配置 CidrBlock 以满足策略。",
        "es": "请在 ALIYUN::ECS::VPC 上配置 CidrBlock 以满足策略。",
        "fr": "请在 ALIYUN::ECS::VPC 上配置 CidrBlock 以满足策略。",
        "pt": "请在 ALIYUN::ECS::VPC 上配置 CidrBlock 以满足策略。"
    },
    "resource_types": ["ALIYUN::ECS::VPC"]
}

deny contains result if {
    some name, resource in helpers.resources_by_type("ALIYUN::ECS::VPC")
    not is_compliant(resource)
    result := {
        "id": rule_meta.id,
        "resource_id": name,
        "violation_path": ["Properties", "CidrBlock"],
        "meta": {
            "severity": rule_meta.severity,
            "reason": rule_meta.reason,
            "recommendation": rule_meta.recommendation,
        },
    }
}

is_compliant(resource) if {
    helpers.has_property(resource, "CidrBlock")
}
