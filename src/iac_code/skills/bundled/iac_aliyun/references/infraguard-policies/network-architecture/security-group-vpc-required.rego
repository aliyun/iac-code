package infraguard.rules.aliyun.security_group_vpc_required

import rego.v1
import data.infraguard.helpers

rule_meta := {
    "id": "security-group-vpc-required",
    "severity": "high",
    "name": {
        "en": "Security group must bind VPC",
        "zh": "安全组必须绑定 VPC",
        "ja": "安全组必须绑定 VPC",
        "de": "安全组必须绑定 VPC",
        "es": "安全组必须绑定 VPC",
        "fr": "安全组必须绑定 VPC",
        "pt": "安全组必须绑定 VPC"
    },
    "description": {
        "en": "Checks Security group must bind VPC",
        "zh": "检查安全组必须绑定 VPC",
        "ja": "检查安全组必须绑定 VPC",
        "de": "检查安全组必须绑定 VPC",
        "es": "检查安全组必须绑定 VPC",
        "fr": "检查安全组必须绑定 VPC",
        "pt": "检查安全组必须绑定 VPC"
    },
    "reason": {
        "en": "Security group must bind VPC is not satisfied.",
        "zh": "安全组必须绑定 VPC未满足。",
        "ja": "安全组必须绑定 VPC未满足。",
        "de": "安全组必须绑定 VPC未满足。",
        "es": "安全组必须绑定 VPC未满足。",
        "fr": "安全组必须绑定 VPC未满足。",
        "pt": "安全组必须绑定 VPC未满足。"
    },
    "recommendation": {
        "en": "Configure VpcId on ALIYUN::ECS::SecurityGroup to satisfy the policy.",
        "zh": "请在 ALIYUN::ECS::SecurityGroup 上配置 VpcId 以满足策略。",
        "ja": "请在 ALIYUN::ECS::SecurityGroup 上配置 VpcId 以满足策略。",
        "de": "请在 ALIYUN::ECS::SecurityGroup 上配置 VpcId 以满足策略。",
        "es": "请在 ALIYUN::ECS::SecurityGroup 上配置 VpcId 以满足策略。",
        "fr": "请在 ALIYUN::ECS::SecurityGroup 上配置 VpcId 以满足策略。",
        "pt": "请在 ALIYUN::ECS::SecurityGroup 上配置 VpcId 以满足策略。"
    },
    "resource_types": ["ALIYUN::ECS::SecurityGroup"]
}

deny contains result if {
    some name, resource in helpers.resources_by_type("ALIYUN::ECS::SecurityGroup")
    not is_compliant(resource)
    result := {
        "id": rule_meta.id,
        "resource_id": name,
        "violation_path": ["Properties", "VpcId"],
        "meta": {
            "severity": rule_meta.severity,
            "reason": rule_meta.reason,
            "recommendation": rule_meta.recommendation,
        },
    }
}

is_compliant(resource) if {
    helpers.has_property(resource, "VpcId")
}
