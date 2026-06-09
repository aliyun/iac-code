package infraguard.rules.aliyun.sls_project_description_required

import rego.v1
import data.infraguard.helpers

rule_meta := {
    "id": "sls-project-description-required",
    "severity": "medium",
    "name": {
        "en": "SLS project must configure description",
        "zh": "SLS Project 必须配置描述",
        "ja": "ALIYUN::SLS::Project には Description を設定する必要があります",
        "de": "Für ALIYUN::SLS::Project muss Description konfiguriert sein",
        "es": "ALIYUN::SLS::Project debe tener Description configurado",
        "fr": "ALIYUN::SLS::Project doit avoir Description configuré",
        "pt": "ALIYUN::SLS::Project deve ter Description configurado"
    },
    "description": {
        "en": "Checks SLS project must configure description",
        "zh": "检查SLS Project 必须配置描述",
        "ja": "ALIYUN::SLS::Project に Description が設定されていることを確認します",
        "de": "Prüft, ob Description für ALIYUN::SLS::Project konfiguriert ist",
        "es": "Comprueba que ALIYUN::SLS::Project tenga Description configurado",
        "fr": "Vérifie que ALIYUN::SLS::Project a Description configuré",
        "pt": "Verifica se ALIYUN::SLS::Project tem Description configurado"
    },
    "reason": {
        "en": "SLS project must configure description is not satisfied.",
        "zh": "SLS Project 必须配置描述未满足。",
        "ja": "ALIYUN::SLS::Project に Description が設定されていません。",
        "de": "Für ALIYUN::SLS::Project ist Description nicht konfiguriert.",
        "es": "ALIYUN::SLS::Project no tiene Description configurado.",
        "fr": "ALIYUN::SLS::Project n'a pas Description configuré.",
        "pt": "ALIYUN::SLS::Project não tem Description configurado."
    },
    "recommendation": {
        "en": "Configure Description on ALIYUN::SLS::Project to satisfy the policy.",
        "zh": "请在 ALIYUN::SLS::Project 上配置 Description 以满足策略。",
        "ja": "ポリシーを満たすには、ALIYUN::SLS::Project に Description を設定してください。",
        "de": "Konfigurieren Sie Description für ALIYUN::SLS::Project, um die Richtlinie zu erfüllen.",
        "es": "Configure Description en ALIYUN::SLS::Project para cumplir la política.",
        "fr": "Configurez Description sur ALIYUN::SLS::Project pour satisfaire la politique.",
        "pt": "Configure Description em ALIYUN::SLS::Project para atender à política."
    },
    "resource_types": ["ALIYUN::SLS::Project"]
}

deny contains result if {
    some name, resource in helpers.resources_by_type("ALIYUN::SLS::Project")
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
