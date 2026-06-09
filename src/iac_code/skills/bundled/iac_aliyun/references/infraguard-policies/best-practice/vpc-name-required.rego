package infraguard.rules.aliyun.vpc_name_required

import rego.v1
import data.infraguard.helpers

rule_meta := {
    "id": "vpc-name-required",
    "severity": "medium",
    "name": {
        "en": "VPC must configure name",
        "zh": "VPC 必须配置名称",
        "ja": "VPC 必须配置名称",
        "de": "VPC 必须配置名称",
        "es": "VPC 必须配置名称",
        "fr": "VPC 必须配置名称",
        "pt": "VPC 必须配置名称"
    },
    "description": {
        "en": "Checks VPC must configure name",
        "zh": "检查VPC 必须配置名称",
        "ja": "检查VPC 必须配置名称",
        "de": "检查VPC 必须配置名称",
        "es": "检查VPC 必须配置名称",
        "fr": "检查VPC 必须配置名称",
        "pt": "检查VPC 必须配置名称"
    },
    "reason": {
        "en": "VPC must configure name is not satisfied.",
        "zh": "VPC 必须配置名称未满足。",
        "ja": "VPC 必须配置名称未满足。",
        "de": "VPC 必须配置名称未满足。",
        "es": "VPC 必须配置名称未满足。",
        "fr": "VPC 必须配置名称未满足。",
        "pt": "VPC 必须配置名称未满足。"
    },
    "recommendation": {
        "en": "Configure VpcName on ALIYUN::ECS::VPC to satisfy the policy.",
        "zh": "请在 ALIYUN::ECS::VPC 上配置 VpcName 以满足策略。",
        "ja": "请在 ALIYUN::ECS::VPC 上配置 VpcName 以满足策略。",
        "de": "请在 ALIYUN::ECS::VPC 上配置 VpcName 以满足策略。",
        "es": "请在 ALIYUN::ECS::VPC 上配置 VpcName 以满足策略。",
        "fr": "请在 ALIYUN::ECS::VPC 上配置 VpcName 以满足策略。",
        "pt": "请在 ALIYUN::ECS::VPC 上配置 VpcName 以满足策略。"
    },
    "resource_types": ["ALIYUN::ECS::VPC"]
}

deny contains result if {
    some name, resource in helpers.resources_by_type("ALIYUN::ECS::VPC")
    not is_compliant(resource)
    result := {
        "id": rule_meta.id,
        "resource_id": name,
        "violation_path": ["Properties", "VpcName"],
        "meta": {
            "severity": rule_meta.severity,
            "reason": rule_meta.reason,
            "recommendation": rule_meta.recommendation,
        },
    }
}

is_compliant(resource) if {
    helpers.has_property(resource, "VpcName")
}
