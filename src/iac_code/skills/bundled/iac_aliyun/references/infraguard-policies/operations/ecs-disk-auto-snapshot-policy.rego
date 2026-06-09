package infraguard.rules.aliyun.ecs_disk_auto_snapshot_policy

import rego.v1
import data.infraguard.helpers

rule_meta := {
    "id": "ecs-disk-auto-snapshot-policy",
    "severity": "medium",
    "name": {
        "en": "ECS disk must attach auto snapshot policy",
        "zh": "ECS 云盘必须绑定自动快照策略",
        "ja": "ECS 云盘必须绑定自动快照策略",
        "de": "ECS 云盘必须绑定自动快照策略",
        "es": "ECS 云盘必须绑定自动快照策略",
        "fr": "ECS 云盘必须绑定自动快照策略",
        "pt": "ECS 云盘必须绑定自动快照策略"
    },
    "description": {
        "en": "Checks ECS disk must attach auto snapshot policy",
        "zh": "检查ECS 云盘必须绑定自动快照策略",
        "ja": "检查ECS 云盘必须绑定自动快照策略",
        "de": "检查ECS 云盘必须绑定自动快照策略",
        "es": "检查ECS 云盘必须绑定自动快照策略",
        "fr": "检查ECS 云盘必须绑定自动快照策略",
        "pt": "检查ECS 云盘必须绑定自动快照策略"
    },
    "reason": {
        "en": "ECS disk must attach auto snapshot policy is not satisfied.",
        "zh": "ECS 云盘必须绑定自动快照策略未满足。",
        "ja": "ECS 云盘必须绑定自动快照策略未满足。",
        "de": "ECS 云盘必须绑定自动快照策略未满足。",
        "es": "ECS 云盘必须绑定自动快照策略未满足。",
        "fr": "ECS 云盘必须绑定自动快照策略未满足。",
        "pt": "ECS 云盘必须绑定自动快照策略未满足。"
    },
    "recommendation": {
        "en": "Configure AutoSnapshotPolicyId on ALIYUN::ECS::Disk to satisfy the policy.",
        "zh": "请在 ALIYUN::ECS::Disk 上配置 AutoSnapshotPolicyId 以满足策略。",
        "ja": "请在 ALIYUN::ECS::Disk 上配置 AutoSnapshotPolicyId 以满足策略。",
        "de": "请在 ALIYUN::ECS::Disk 上配置 AutoSnapshotPolicyId 以满足策略。",
        "es": "请在 ALIYUN::ECS::Disk 上配置 AutoSnapshotPolicyId 以满足策略。",
        "fr": "请在 ALIYUN::ECS::Disk 上配置 AutoSnapshotPolicyId 以满足策略。",
        "pt": "请在 ALIYUN::ECS::Disk 上配置 AutoSnapshotPolicyId 以满足策略。"
    },
    "resource_types": ["ALIYUN::ECS::Disk"]
}

deny contains result if {
    some name, resource in helpers.resources_by_type("ALIYUN::ECS::Disk")
    not is_compliant(resource)
    result := {
        "id": rule_meta.id,
        "resource_id": name,
        "violation_path": ["Properties", "AutoSnapshotPolicyId"],
        "meta": {
            "severity": rule_meta.severity,
            "reason": rule_meta.reason,
            "recommendation": rule_meta.recommendation,
        },
    }
}

is_compliant(resource) if {
    helpers.has_property(resource, "AutoSnapshotPolicyId")
}
