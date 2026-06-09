package infraguard.rules.aliyun.ecs_disk_auto_snapshot_policy

import rego.v1
import data.infraguard.helpers

rule_meta := {
    "id": "ecs-disk-auto-snapshot-policy",
    "severity": "medium",
    "name": {
        "en": "ECS disk must attach auto snapshot policy",
        "zh": "ECS 云盘必须绑定自动快照策略",
        "ja": "ALIYUN::ECS::Disk には AutoSnapshotPolicyId を設定する必要があります",
        "de": "Für ALIYUN::ECS::Disk muss AutoSnapshotPolicyId konfiguriert sein",
        "es": "ALIYUN::ECS::Disk debe tener AutoSnapshotPolicyId configurado",
        "fr": "ALIYUN::ECS::Disk doit avoir AutoSnapshotPolicyId configuré",
        "pt": "ALIYUN::ECS::Disk deve ter AutoSnapshotPolicyId configurado"
    },
    "description": {
        "en": "Checks ECS disk must attach auto snapshot policy",
        "zh": "检查ECS 云盘必须绑定自动快照策略",
        "ja": "ALIYUN::ECS::Disk に AutoSnapshotPolicyId が設定されていることを確認します",
        "de": "Prüft, ob AutoSnapshotPolicyId für ALIYUN::ECS::Disk konfiguriert ist",
        "es": "Comprueba que ALIYUN::ECS::Disk tenga AutoSnapshotPolicyId configurado",
        "fr": "Vérifie que ALIYUN::ECS::Disk a AutoSnapshotPolicyId configuré",
        "pt": "Verifica se ALIYUN::ECS::Disk tem AutoSnapshotPolicyId configurado"
    },
    "reason": {
        "en": "ECS disk must attach auto snapshot policy is not satisfied.",
        "zh": "ECS 云盘必须绑定自动快照策略未满足。",
        "ja": "ALIYUN::ECS::Disk に AutoSnapshotPolicyId が設定されていません。",
        "de": "Für ALIYUN::ECS::Disk ist AutoSnapshotPolicyId nicht konfiguriert.",
        "es": "ALIYUN::ECS::Disk no tiene AutoSnapshotPolicyId configurado.",
        "fr": "ALIYUN::ECS::Disk n'a pas AutoSnapshotPolicyId configuré.",
        "pt": "ALIYUN::ECS::Disk não tem AutoSnapshotPolicyId configurado."
    },
    "recommendation": {
        "en": "Configure AutoSnapshotPolicyId on ALIYUN::ECS::Disk to satisfy the policy.",
        "zh": "请在 ALIYUN::ECS::Disk 上配置 AutoSnapshotPolicyId 以满足策略。",
        "ja": "ポリシーを満たすには、ALIYUN::ECS::Disk に AutoSnapshotPolicyId を設定してください。",
        "de": "Konfigurieren Sie AutoSnapshotPolicyId für ALIYUN::ECS::Disk, um die Richtlinie zu erfüllen.",
        "es": "Configure AutoSnapshotPolicyId en ALIYUN::ECS::Disk para cumplir la política.",
        "fr": "Configurez AutoSnapshotPolicyId sur ALIYUN::ECS::Disk pour satisfaire la politique.",
        "pt": "Configure AutoSnapshotPolicyId em ALIYUN::ECS::Disk para atender à política."
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
