package infraguard.rules.aliyun.kms_key_description_required

import rego.v1
import data.infraguard.helpers

rule_meta := {
    "id": "kms-key-description-required",
    "severity": "medium",
    "name": {
        "en": "KMS key must configure description",
        "zh": "KMS 密钥必须配置描述",
        "ja": "ALIYUN::KMS::Key には Description を設定する必要があります",
        "de": "Für ALIYUN::KMS::Key muss Description konfiguriert sein",
        "es": "ALIYUN::KMS::Key debe tener Description configurado",
        "fr": "ALIYUN::KMS::Key doit avoir Description configuré",
        "pt": "ALIYUN::KMS::Key deve ter Description configurado"
    },
    "description": {
        "en": "Checks KMS key must configure description",
        "zh": "检查KMS 密钥必须配置描述",
        "ja": "ALIYUN::KMS::Key に Description が設定されていることを確認します",
        "de": "Prüft, ob Description für ALIYUN::KMS::Key konfiguriert ist",
        "es": "Comprueba que ALIYUN::KMS::Key tenga Description configurado",
        "fr": "Vérifie que ALIYUN::KMS::Key a Description configuré",
        "pt": "Verifica se ALIYUN::KMS::Key tem Description configurado"
    },
    "reason": {
        "en": "KMS key must configure description is not satisfied.",
        "zh": "KMS 密钥必须配置描述未满足。",
        "ja": "ALIYUN::KMS::Key に Description が設定されていません。",
        "de": "Für ALIYUN::KMS::Key ist Description nicht konfiguriert.",
        "es": "ALIYUN::KMS::Key no tiene Description configurado.",
        "fr": "ALIYUN::KMS::Key n'a pas Description configuré.",
        "pt": "ALIYUN::KMS::Key não tem Description configurado."
    },
    "recommendation": {
        "en": "Configure Description on ALIYUN::KMS::Key to satisfy the policy.",
        "zh": "请在 ALIYUN::KMS::Key 上配置 Description 以满足策略。",
        "ja": "ポリシーを満たすには、ALIYUN::KMS::Key に Description を設定してください。",
        "de": "Konfigurieren Sie Description für ALIYUN::KMS::Key, um die Richtlinie zu erfüllen.",
        "es": "Configure Description en ALIYUN::KMS::Key para cumplir la política.",
        "fr": "Configurez Description sur ALIYUN::KMS::Key pour satisfaire la politique.",
        "pt": "Configure Description em ALIYUN::KMS::Key para atender à política."
    },
    "resource_types": ["ALIYUN::KMS::Key"]
}

deny contains result if {
    some name, resource in helpers.resources_by_type("ALIYUN::KMS::Key")
    not is_compliant(resource)
    result := {
        "id": rule_meta.id,
        "resource_id": name,
        "violation_path": ["Properties", "Description"],
        "meta": {
            "severity": rule_meta.severity,
            "reason": rule_meta.reason,
            "recommendation": rule_meta.recommendation,
        },
    }
}

is_compliant(resource) if {
    helpers.has_property(resource, "Description")
}
