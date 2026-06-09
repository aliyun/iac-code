package infraguard.rules.aliyun.ecs_instance_operational_deletion_protection

import rego.v1
import data.infraguard.helpers

rule_meta := {
    "id": "ecs-instance-operational-deletion-protection",
    "severity": "medium",
    "name": {
        "en": "ECS instance must enable deletion protection for operations",
        "zh": "ECS 实例必须启用运维删除保护",
        "ja": "ALIYUN::ECS::Instance には DeletionProtection を設定する必要があります",
        "de": "Für ALIYUN::ECS::Instance muss DeletionProtection konfiguriert sein",
        "es": "ALIYUN::ECS::Instance debe tener DeletionProtection configurado",
        "fr": "ALIYUN::ECS::Instance doit avoir DeletionProtection configuré",
        "pt": "ALIYUN::ECS::Instance deve ter DeletionProtection configurado"
    },
    "description": {
        "en": "Checks ECS instance must enable deletion protection for operations",
        "zh": "检查ECS 实例必须启用运维删除保护",
        "ja": "ALIYUN::ECS::Instance に DeletionProtection が設定されていることを確認します",
        "de": "Prüft, ob DeletionProtection für ALIYUN::ECS::Instance konfiguriert ist",
        "es": "Comprueba que ALIYUN::ECS::Instance tenga DeletionProtection configurado",
        "fr": "Vérifie que ALIYUN::ECS::Instance a DeletionProtection configuré",
        "pt": "Verifica se ALIYUN::ECS::Instance tem DeletionProtection configurado"
    },
    "reason": {
        "en": "ECS instance must enable deletion protection for operations is not satisfied.",
        "zh": "ECS 实例必须启用运维删除保护未满足。",
        "ja": "ALIYUN::ECS::Instance に DeletionProtection が設定されていません。",
        "de": "Für ALIYUN::ECS::Instance ist DeletionProtection nicht konfiguriert.",
        "es": "ALIYUN::ECS::Instance no tiene DeletionProtection configurado.",
        "fr": "ALIYUN::ECS::Instance n'a pas DeletionProtection configuré.",
        "pt": "ALIYUN::ECS::Instance não tem DeletionProtection configurado."
    },
    "recommendation": {
        "en": "Configure DeletionProtection on ALIYUN::ECS::Instance to satisfy the policy.",
        "zh": "请在 ALIYUN::ECS::Instance 上配置 DeletionProtection 以满足策略。",
        "ja": "ポリシーを満たすには、ALIYUN::ECS::Instance に DeletionProtection を設定してください。",
        "de": "Konfigurieren Sie DeletionProtection für ALIYUN::ECS::Instance, um die Richtlinie zu erfüllen.",
        "es": "Configure DeletionProtection en ALIYUN::ECS::Instance para cumplir la política.",
        "fr": "Configurez DeletionProtection sur ALIYUN::ECS::Instance pour satisfaire la politique.",
        "pt": "Configure DeletionProtection em ALIYUN::ECS::Instance para atender à política."
    },
    "resource_types": ["ALIYUN::ECS::Instance"]
}

deny contains result if {
    some name, resource in helpers.resources_by_type("ALIYUN::ECS::Instance")
    not is_compliant(resource)
    result := {
        "id": rule_meta.id,
        "resource_id": name,
        "violation_path": ["Properties", "DeletionProtection"],
        "meta": {
            "severity": rule_meta.severity,
            "reason": rule_meta.reason,
            "recommendation": rule_meta.recommendation,
        },
    }
}

is_compliant(resource) if {
    helpers.get_property(resource, "DeletionProtection", false) == true
}
