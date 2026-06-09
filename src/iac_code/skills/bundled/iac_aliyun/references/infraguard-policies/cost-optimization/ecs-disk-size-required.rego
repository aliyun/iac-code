package infraguard.rules.aliyun.ecs_disk_size_required

import rego.v1
import data.infraguard.helpers

rule_meta := {
    "id": "ecs-disk-size-required",
    "severity": "medium",
    "name": {
        "en": "ECS disk must set disk size",
        "zh": "ECS 云盘必须设置容量",
        "ja": "ALIYUN::ECS::Disk には Size を設定する必要があります",
        "de": "Für ALIYUN::ECS::Disk muss Size konfiguriert sein",
        "es": "ALIYUN::ECS::Disk debe tener Size configurado",
        "fr": "ALIYUN::ECS::Disk doit avoir Size configuré",
        "pt": "ALIYUN::ECS::Disk deve ter Size configurado"
    },
    "description": {
        "en": "Checks ECS disk must set disk size",
        "zh": "检查ECS 云盘必须设置容量",
        "ja": "ALIYUN::ECS::Disk に Size が設定されていることを確認します",
        "de": "Prüft, ob Size für ALIYUN::ECS::Disk konfiguriert ist",
        "es": "Comprueba que ALIYUN::ECS::Disk tenga Size configurado",
        "fr": "Vérifie que ALIYUN::ECS::Disk a Size configuré",
        "pt": "Verifica se ALIYUN::ECS::Disk tem Size configurado"
    },
    "reason": {
        "en": "ECS disk must set disk size is not satisfied.",
        "zh": "ECS 云盘必须设置容量未满足。",
        "ja": "ALIYUN::ECS::Disk に Size が設定されていません。",
        "de": "Für ALIYUN::ECS::Disk ist Size nicht konfiguriert.",
        "es": "ALIYUN::ECS::Disk no tiene Size configurado.",
        "fr": "ALIYUN::ECS::Disk n'a pas Size configuré.",
        "pt": "ALIYUN::ECS::Disk não tem Size configurado."
    },
    "recommendation": {
        "en": "Configure Size on ALIYUN::ECS::Disk to satisfy the policy.",
        "zh": "请在 ALIYUN::ECS::Disk 上配置 Size 以满足策略。",
        "ja": "ポリシーを満たすには、ALIYUN::ECS::Disk に Size を設定してください。",
        "de": "Konfigurieren Sie Size für ALIYUN::ECS::Disk, um die Richtlinie zu erfüllen.",
        "es": "Configure Size en ALIYUN::ECS::Disk para cumplir la política.",
        "fr": "Configurez Size sur ALIYUN::ECS::Disk pour satisfaire la politique.",
        "pt": "Configure Size em ALIYUN::ECS::Disk para atender à política."
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
