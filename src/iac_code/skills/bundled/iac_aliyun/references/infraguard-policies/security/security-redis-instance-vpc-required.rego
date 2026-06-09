package infraguard.rules.aliyun.security_redis_instance_vpc_required

import rego.v1
import data.infraguard.helpers

rule_meta := {
    "id": "security-redis-instance-vpc-required",
    "severity": "high",
    "name": {
        "en": "Redis instance must run in VPC",
        "zh": "Redis 实例必须部署在 VPC 内",
    },
    "description": {
        "en": "Checks Redis instance must run in VPC",
        "zh": "检查Redis 实例必须部署在 VPC 内",
    },
    "reason": {
        "en": "Redis instance must run in VPC is not satisfied.",
        "zh": "Redis 实例必须部署在 VPC 内未满足。",
    },
    "recommendation": {
        "en": "Configure VpcId on ALIYUN::REDIS::Instance to satisfy the policy.",
        "zh": "请在 ALIYUN::REDIS::Instance 上配置 VpcId 以满足策略。",
    },
    "resource_types": ["ALIYUN::REDIS::Instance"],
}

deny contains result if {
    some name, resource in helpers.resources_by_type("ALIYUN::REDIS::Instance")
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
