package infraguard.rules.aliyun.ecs_disk_category_required

import rego.v1
import data.infraguard.helpers

rule_meta := {
    "id": "ecs-disk-category-required",
    "severity": "medium",
    "name": {
        "en": "ECS disk must set disk category",
        "zh": "ECS 云盘必须设置云盘类型",
        "ja": "ECS 云盘必须设置云盘类型",
        "de": "ECS 云盘必须设置云盘类型",
        "es": "ECS 云盘必须设置云盘类型",
        "fr": "ECS 云盘必须设置云盘类型",
        "pt": "ECS 云盘必须设置云盘类型"
    },
    "description": {
        "en": "Checks ECS disk must set disk category",
        "zh": "检查ECS 云盘必须设置云盘类型",
        "ja": "检查ECS 云盘必须设置云盘类型",
        "de": "检查ECS 云盘必须设置云盘类型",
        "es": "检查ECS 云盘必须设置云盘类型",
        "fr": "检查ECS 云盘必须设置云盘类型",
        "pt": "检查ECS 云盘必须设置云盘类型"
    },
    "reason": {
        "en": "ECS disk must set disk category is not satisfied.",
        "zh": "ECS 云盘必须设置云盘类型未满足。",
        "ja": "ECS 云盘必须设置云盘类型未满足。",
        "de": "ECS 云盘必须设置云盘类型未满足。",
        "es": "ECS 云盘必须设置云盘类型未满足。",
        "fr": "ECS 云盘必须设置云盘类型未满足。",
        "pt": "ECS 云盘必须设置云盘类型未满足。"
    },
    "recommendation": {
        "en": "Configure DiskCategory on ALIYUN::ECS::Disk to satisfy the policy.",
        "zh": "请在 ALIYUN::ECS::Disk 上配置 DiskCategory 以满足策略。",
        "ja": "请在 ALIYUN::ECS::Disk 上配置 DiskCategory 以满足策略。",
        "de": "请在 ALIYUN::ECS::Disk 上配置 DiskCategory 以满足策略。",
        "es": "请在 ALIYUN::ECS::Disk 上配置 DiskCategory 以满足策略。",
        "fr": "请在 ALIYUN::ECS::Disk 上配置 DiskCategory 以满足策略。",
        "pt": "请在 ALIYUN::ECS::Disk 上配置 DiskCategory 以满足策略。"
    },
    "resource_types": ["ALIYUN::ECS::Disk"]
}

deny contains result if {
    some name, resource in helpers.resources_by_type("ALIYUN::ECS::Disk")
    not is_compliant(resource)
    result := {
        "id": rule_meta.id,
        "resource_id": name,
        "violation_path": ["Properties", "DiskCategory"],
        "meta": {
            "severity": rule_meta.severity,
            "reason": rule_meta.reason,
            "recommendation": rule_meta.recommendation,
        },
    }
}

is_compliant(resource) if {
    helpers.has_property(resource, "DiskCategory")
}
