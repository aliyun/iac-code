package infraguard.rules.aliyun.security_ecs_disk_encrypted

import rego.v1
import data.infraguard.helpers

rule_meta := {
    "id": "security-ecs-disk-encrypted",
    "severity": "high",
    "name": {
        "en": "ECS disk must enable encryption",
        "zh": "ECS 云盘必须启用加密",
    },
    "description": {
        "en": "Checks ECS disk must enable encryption",
        "zh": "检查ECS 云盘必须启用加密",
    },
    "reason": {
        "en": "ECS disk must enable encryption is not satisfied.",
        "zh": "ECS 云盘必须启用加密未满足。",
    },
    "recommendation": {
        "en": "Configure Encrypted on ALIYUN::ECS::Disk to satisfy the policy.",
        "zh": "请在 ALIYUN::ECS::Disk 上配置 Encrypted 以满足策略。",
    },
    "resource_types": ["ALIYUN::ECS::Disk"],
}

deny contains result if {
    some name, resource in helpers.resources_by_type("ALIYUN::ECS::Disk")
    not is_compliant(resource)
    result := {
        "id": rule_meta.id,
        "resource_id": name,
        "violation_path": ["Properties", "Encrypted"],
        "meta": {
            "severity": rule_meta.severity,
            "reason": rule_meta.reason,
            "recommendation": rule_meta.recommendation,
        },
    }
}

is_compliant(resource) if {
    helpers.get_property(resource, "Encrypted", false) == true
}
