package infraguard.rules.aliyun.rds_instance_deletion_protection_enabled

import rego.v1
import data.infraguard.helpers

rule_meta := {
    "id": "rds-instance-deletion-protection-enabled",
    "severity": "medium",
    "name": {
        "en": "RDS instance must enable deletion protection",
        "zh": "RDS 实例必须启用删除保护",
        "ja": "ALIYUN::RDS::DBInstance には DeletionProtection を設定する必要があります",
        "de": "Für ALIYUN::RDS::DBInstance muss DeletionProtection konfiguriert sein",
        "es": "ALIYUN::RDS::DBInstance debe tener DeletionProtection configurado",
        "fr": "ALIYUN::RDS::DBInstance doit avoir DeletionProtection configuré",
        "pt": "ALIYUN::RDS::DBInstance deve ter DeletionProtection configurado"
    },
    "description": {
        "en": "Checks RDS instance must enable deletion protection",
        "zh": "检查RDS 实例必须启用删除保护",
        "ja": "ALIYUN::RDS::DBInstance に DeletionProtection が設定されていることを確認します",
        "de": "Prüft, ob DeletionProtection für ALIYUN::RDS::DBInstance konfiguriert ist",
        "es": "Comprueba que ALIYUN::RDS::DBInstance tenga DeletionProtection configurado",
        "fr": "Vérifie que ALIYUN::RDS::DBInstance a DeletionProtection configuré",
        "pt": "Verifica se ALIYUN::RDS::DBInstance tem DeletionProtection configurado"
    },
    "reason": {
        "en": "RDS instance must enable deletion protection is not satisfied.",
        "zh": "RDS 实例必须启用删除保护未满足。",
        "ja": "ALIYUN::RDS::DBInstance に DeletionProtection が設定されていません。",
        "de": "Für ALIYUN::RDS::DBInstance ist DeletionProtection nicht konfiguriert.",
        "es": "ALIYUN::RDS::DBInstance no tiene DeletionProtection configurado.",
        "fr": "ALIYUN::RDS::DBInstance n'a pas DeletionProtection configuré.",
        "pt": "ALIYUN::RDS::DBInstance não tem DeletionProtection configurado."
    },
    "recommendation": {
        "en": "Configure DeletionProtection on ALIYUN::RDS::DBInstance to satisfy the policy.",
        "zh": "请在 ALIYUN::RDS::DBInstance 上配置 DeletionProtection 以满足策略。",
        "ja": "ポリシーを満たすには、ALIYUN::RDS::DBInstance に DeletionProtection を設定してください。",
        "de": "Konfigurieren Sie DeletionProtection für ALIYUN::RDS::DBInstance, um die Richtlinie zu erfüllen.",
        "es": "Configure DeletionProtection en ALIYUN::RDS::DBInstance para cumplir la política.",
        "fr": "Configurez DeletionProtection sur ALIYUN::RDS::DBInstance pour satisfaire la politique.",
        "pt": "Configure DeletionProtection em ALIYUN::RDS::DBInstance para atender à política."
    },
    "resource_types": ["ALIYUN::RDS::DBInstance"]
}

deny contains result if {
    some name, resource in helpers.resources_by_type("ALIYUN::RDS::DBInstance")
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
