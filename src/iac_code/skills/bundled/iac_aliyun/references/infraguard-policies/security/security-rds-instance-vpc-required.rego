package infraguard.rules.aliyun.security_rds_instance_vpc_required

import rego.v1
import data.infraguard.helpers

rule_meta := {
    "id": "security-rds-instance-vpc-required",
    "severity": "high",
    "name": {
        "en": "RDS instance must run in VPC",
        "zh": "RDS 实例必须部署在 VPC 内",
    },
    "description": {
        "en": "Checks RDS instance must run in VPC",
        "zh": "检查RDS 实例必须部署在 VPC 内",
    },
    "reason": {
        "en": "RDS instance must run in VPC is not satisfied.",
        "zh": "RDS 实例必须部署在 VPC 内未满足。",
    },
    "recommendation": {
        "en": "Configure VpcId on ALIYUN::RDS::DBInstance to satisfy the policy.",
        "zh": "请在 ALIYUN::RDS::DBInstance 上配置 VpcId 以满足策略。",
    },
    "resource_types": ["ALIYUN::RDS::DBInstance"],
}

deny contains result if {
    some name, resource in helpers.resources_by_type("ALIYUN::RDS::DBInstance")
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
