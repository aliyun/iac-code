package infraguard.rules.aliyun.fc_function_timeout_configured

import rego.v1
import data.infraguard.helpers

rule_meta := {
    "id": "fc-function-timeout-configured",
    "severity": "medium",
    "name": {
        "en": "FC function must configure timeout",
        "zh": "函数计算函数必须配置超时时间",
        "ja": "ALIYUN::FC::Function には Timeout を設定する必要があります",
        "de": "Für ALIYUN::FC::Function muss Timeout konfiguriert sein",
        "es": "ALIYUN::FC::Function debe tener Timeout configurado",
        "fr": "ALIYUN::FC::Function doit avoir Timeout configuré",
        "pt": "ALIYUN::FC::Function deve ter Timeout configurado"
    },
    "description": {
        "en": "Checks FC function must configure timeout",
        "zh": "检查函数计算函数必须配置超时时间",
        "ja": "ALIYUN::FC::Function に Timeout が設定されていることを確認します",
        "de": "Prüft, ob Timeout für ALIYUN::FC::Function konfiguriert ist",
        "es": "Comprueba que ALIYUN::FC::Function tenga Timeout configurado",
        "fr": "Vérifie que ALIYUN::FC::Function a Timeout configuré",
        "pt": "Verifica se ALIYUN::FC::Function tem Timeout configurado"
    },
    "reason": {
        "en": "FC function must configure timeout is not satisfied.",
        "zh": "函数计算函数必须配置超时时间未满足。",
        "ja": "ALIYUN::FC::Function に Timeout が設定されていません。",
        "de": "Für ALIYUN::FC::Function ist Timeout nicht konfiguriert.",
        "es": "ALIYUN::FC::Function no tiene Timeout configurado.",
        "fr": "ALIYUN::FC::Function n'a pas Timeout configuré.",
        "pt": "ALIYUN::FC::Function não tem Timeout configurado."
    },
    "recommendation": {
        "en": "Configure Timeout on ALIYUN::FC::Function to satisfy the policy.",
        "zh": "请在 ALIYUN::FC::Function 上配置 Timeout 以满足策略。",
        "ja": "ポリシーを満たすには、ALIYUN::FC::Function に Timeout を設定してください。",
        "de": "Konfigurieren Sie Timeout für ALIYUN::FC::Function, um die Richtlinie zu erfüllen.",
        "es": "Configure Timeout en ALIYUN::FC::Function para cumplir la política.",
        "fr": "Configurez Timeout sur ALIYUN::FC::Function pour satisfaire la politique.",
        "pt": "Configure Timeout em ALIYUN::FC::Function para atender à política."
    },
    "resource_types": ["ALIYUN::FC::Function"]
}

deny contains result if {
    some name, resource in helpers.resources_by_type("ALIYUN::FC::Function")
    not is_compliant(resource)
    result := {
        "id": rule_meta.id,
        "resource_id": name,
        "violation_path": ["Properties", "Timeout"],
        "meta": {
            "severity": rule_meta.severity,
            "reason": rule_meta.reason,
            "recommendation": rule_meta.recommendation,
        },
    }
}

is_compliant(resource) if {
    helpers.has_property(resource, "Timeout")
}
