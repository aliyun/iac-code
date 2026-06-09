package infraguard.rules.aliyun.ecs_disk_size_required

import rego.v1
import data.infraguard.helpers

rule_meta := {
    "id": "ecs-disk-size-required",
    "severity": "medium",
    "name": {
        "en": "ECS disk must set disk size",
        "zh": "ECS 云盘必须设置容量",
        "ja": "ECS 云盘必须设置容量",
        "de": "ECS 云盘必须设置容量",
        "es": "ECS 云盘必须设置容量",
        "fr": "ECS 云盘必须设置容量",
        "pt": "ECS 云盘必须设置容量"
    },
    "description": {
        "en": "Checks ECS disk must set disk size",
        "zh": "检查ECS 云盘必须设置容量",
        "ja": "检查ECS 云盘必须设置容量",
        "de": "检查ECS 云盘必须设置容量",
        "es": "检查ECS 云盘必须设置容量",
        "fr": "检查ECS 云盘必须设置容量",
        "pt": "检查ECS 云盘必须设置容量"
    },
    "reason": {
        "en": "ECS disk must set disk size is not satisfied.",
        "zh": "ECS 云盘必须设置容量未满足。",
        "ja": "ECS 云盘必须设置容量未满足。",
        "de": "ECS 云盘必须设置容量未满足。",
        "es": "ECS 云盘必须设置容量未满足。",
        "fr": "ECS 云盘必须设置容量未满足。",
        "pt": "ECS 云盘必须设置容量未满足。"
    },
    "recommendation": {
        "en": "Configure Size on ALIYUN::ECS::Disk to satisfy the policy.",
        "zh": "请在 ALIYUN::ECS::Disk 上配置 Size 以满足策略。",
        "ja": "请在 ALIYUN::ECS::Disk 上配置 Size 以满足策略。",
        "de": "请在 ALIYUN::ECS::Disk 上配置 Size 以满足策略。",
        "es": "请在 ALIYUN::ECS::Disk 上配置 Size 以满足策略。",
        "fr": "请在 ALIYUN::ECS::Disk 上配置 Size 以满足策略。",
        "pt": "请在 ALIYUN::ECS::Disk 上配置 Size 以满足策略。"
    },
    "resource_types": ["ALIYUN::ECS::Disk"]
}

deny contains result if {
    some name, resource in helpers.resources_by_type("ALIYUN::ECS::Disk")
    not is_compliant(resource)
    result := {
        "id": rule_meta.id,
        "resource_id": name,
        "violation_path": ["Properties", "Size"],
        "meta": {
            "severity": rule_meta.severity,
            "reason": rule_meta.reason,
            "recommendation": rule_meta.recommendation,
        },
    }
}

is_compliant(resource) if {
    helpers.has_property(resource, "Size")
}
