package infraguard.rules.aliyun.ecs_disk_category_required

import rego.v1
import data.infraguard.helpers

rule_meta := {
    "id": "ecs-disk-category-required",
    "severity": "medium",
    "name": {
        "en": "ECS disk must set disk category",
        "zh": "ECS 云盘必须设置云盘类型",
        "ja": "ALIYUN::ECS::Disk には DiskCategory を設定する必要があります",
        "de": "Für ALIYUN::ECS::Disk muss DiskCategory konfiguriert sein",
        "es": "ALIYUN::ECS::Disk debe tener DiskCategory configurado",
        "fr": "ALIYUN::ECS::Disk doit avoir DiskCategory configuré",
        "pt": "ALIYUN::ECS::Disk deve ter DiskCategory configurado"
    },
    "description": {
        "en": "Checks ECS disk must set disk category",
        "zh": "检查ECS 云盘必须设置云盘类型",
        "ja": "ALIYUN::ECS::Disk に DiskCategory が設定されていることを確認します",
        "de": "Prüft, ob DiskCategory für ALIYUN::ECS::Disk konfiguriert ist",
        "es": "Comprueba que ALIYUN::ECS::Disk tenga DiskCategory configurado",
        "fr": "Vérifie que ALIYUN::ECS::Disk a DiskCategory configuré",
        "pt": "Verifica se ALIYUN::ECS::Disk tem DiskCategory configurado"
    },
    "reason": {
        "en": "ECS disk must set disk category is not satisfied.",
        "zh": "ECS 云盘必须设置云盘类型未满足。",
        "ja": "ALIYUN::ECS::Disk に DiskCategory が設定されていません。",
        "de": "Für ALIYUN::ECS::Disk ist DiskCategory nicht konfiguriert.",
        "es": "ALIYUN::ECS::Disk no tiene DiskCategory configurado.",
        "fr": "ALIYUN::ECS::Disk n'a pas DiskCategory configuré.",
        "pt": "ALIYUN::ECS::Disk não tem DiskCategory configurado."
    },
    "recommendation": {
        "en": "Configure DiskCategory on ALIYUN::ECS::Disk to satisfy the policy.",
        "zh": "请在 ALIYUN::ECS::Disk 上配置 DiskCategory 以满足策略。",
        "ja": "ポリシーを満たすには、ALIYUN::ECS::Disk に DiskCategory を設定してください。",
        "de": "Konfigurieren Sie DiskCategory für ALIYUN::ECS::Disk, um die Richtlinie zu erfüllen.",
        "es": "Configure DiskCategory en ALIYUN::ECS::Disk para cumplir la política.",
        "fr": "Configurez DiskCategory sur ALIYUN::ECS::Disk pour satisfaire la politique.",
        "pt": "Configure DiskCategory em ALIYUN::ECS::Disk para atender à política."
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
