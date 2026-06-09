package infraguard.rules.aliyun.security_ecs_instance_no_public_ip

import rego.v1
import data.infraguard.helpers

rule_meta := {
    "id": "security-ecs-instance-no-public-ip",
    "severity": "high",
    "name": {
        "en": "ECS instance must not allocate public IP",
        "zh": "ECS 实例禁止分配公网 IP",
    },
    "description": {
        "en": "Checks ECS public exposure through direct public IP, outbound bandwidth, or EIP association.",
        "zh": "检查 ECS 是否通过公网 IP、出网带宽或 EIP 绑定暴露公网。",
    },
    "reason": {
        "en": "ECS instance is exposed to the public network.",
        "zh": "ECS 实例存在公网暴露路径。",
    },
    "recommendation": {
        "en": "Disable public IP allocation, set internet outbound bandwidth to 0, and avoid direct EIP association.",
        "zh": "关闭公网 IP 分配，将公网出带宽设为 0，并避免直接绑定 EIP。",
    },
    "resource_types": ["ALIYUN::ECS::Instance", "ALIYUN::ECS::InstanceGroup"],
}

allocates_public_ip(resource) if {
    helpers.get_property(resource, "AllocatePublicIP", false) == true
}

has_internet_bandwidth(resource) if {
    helpers.has_property(resource, "InternetMaxBandwidthOut")
    resource.Properties.InternetMaxBandwidthOut > 0
}

deny contains result if {
    some name, resource in helpers.resources_by_types(["ALIYUN::ECS::Instance", "ALIYUN::ECS::InstanceGroup"])
    allocates_public_ip(resource)
    result := {
        "id": rule_meta.id,
        "resource_id": name,
        "violation_path": ["Properties", "AllocatePublicIP"],
        "meta": {
            "severity": rule_meta.severity,
            "reason": rule_meta.reason,
            "recommendation": rule_meta.recommendation,
        },
    }
}

deny contains result if {
    some name, resource in helpers.resources_by_types(["ALIYUN::ECS::Instance", "ALIYUN::ECS::InstanceGroup"])
    has_internet_bandwidth(resource)
    not allocates_public_ip(resource)
    result := {
        "id": rule_meta.id,
        "resource_id": name,
        "violation_path": ["Properties", "InternetMaxBandwidthOut"],
        "meta": {
            "severity": rule_meta.severity,
            "reason": rule_meta.reason,
            "recommendation": rule_meta.recommendation,
        },
    }
}

deny contains result if {
    some name, resource in helpers.resources_by_types(["ALIYUN::ECS::Instance", "ALIYUN::ECS::InstanceGroup"])
    some _, eip_resource in helpers.resources_by_type("ALIYUN::VPC::EIPAssociation")
    instance_id := helpers.get_property(eip_resource, "InstanceId", "")
    helpers.is_referencing(instance_id, name)
    result := {
        "id": rule_meta.id,
        "resource_id": name,
        "violation_path": ["Properties"],
        "meta": {
            "severity": rule_meta.severity,
            "reason": rule_meta.reason,
            "recommendation": rule_meta.recommendation,
        },
    }
}

deny contains result if {
    some name, resource in helpers.resources_by_types(["ALIYUN::ECS::Instance", "ALIYUN::ECS::InstanceGroup"])
    some _, eip_resource in helpers.resources_by_type("ALIYUN::VPC::EIPAssociation")
    instance_id := helpers.get_property(eip_resource, "InstanceId", "")
    helpers.is_get_att_referencing(instance_id, name)
    result := {
        "id": rule_meta.id,
        "resource_id": name,
        "violation_path": ["Properties"],
        "meta": {
            "severity": rule_meta.severity,
            "reason": rule_meta.reason,
            "recommendation": rule_meta.recommendation,
        },
    }
}
