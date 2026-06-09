package infraguard.rules.aliyun.security_ecs_instance_security_group_required

import rego.v1
import data.infraguard.helpers

rule_meta := {
    "id": "security-ecs-instance-security-group-required",
    "severity": "high",
    "name": {
        "en": "ECS instance must attach a security group",
        "zh": "ECS 实例必须绑定安全组",
    },
    "description": {
        "en": "Checks ECS instance must attach a security group",
        "zh": "检查ECS 实例必须绑定安全组",
    },
    "reason": {
        "en": "ECS instance must attach a security group is not satisfied.",
        "zh": "ECS 实例必须绑定安全组未满足。",
    },
    "recommendation": {
        "en": "Configure SecurityGroupId on ALIYUN::ECS::Instance to satisfy the policy.",
        "zh": "请在 ALIYUN::ECS::Instance 上配置 SecurityGroupId 以满足策略。",
    },
    "resource_types": ["ALIYUN::ECS::Instance"],
}

deny contains result if {
    some name, resource in helpers.resources_by_type("ALIYUN::ECS::Instance")
    not is_compliant(resource)
    result := {
        "id": rule_meta.id,
        "resource_id": name,
        "violation_path": ["Properties", "SecurityGroupId"],
        "meta": {
            "severity": rule_meta.severity,
            "reason": rule_meta.reason,
            "recommendation": rule_meta.recommendation,
        },
    }
}

is_compliant(resource) if {
    helpers.has_property(resource, "SecurityGroupId")
}
