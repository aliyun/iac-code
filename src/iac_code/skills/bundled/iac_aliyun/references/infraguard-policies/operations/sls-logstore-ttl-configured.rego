package infraguard.rules.aliyun.sls_logstore_ttl_configured

import rego.v1
import data.infraguard.helpers

rule_meta := {
    "id": "sls-logstore-ttl-configured",
    "severity": "medium",
    "name": {
        "en": "SLS Logstore must configure TTL",
        "zh": "SLS Logstore 必须配置 TTL",
        "ja": "ALIYUN::SLS::Logstore には TTL を設定する必要があります",
        "de": "Für ALIYUN::SLS::Logstore muss TTL konfiguriert sein",
        "es": "ALIYUN::SLS::Logstore debe tener TTL configurado",
        "fr": "ALIYUN::SLS::Logstore doit avoir TTL configuré",
        "pt": "ALIYUN::SLS::Logstore deve ter TTL configurado"
    },
    "description": {
        "en": "Checks SLS Logstore must configure TTL",
        "zh": "检查SLS Logstore 必须配置 TTL",
        "ja": "ALIYUN::SLS::Logstore に TTL が設定されていることを確認します",
        "de": "Prüft, ob TTL für ALIYUN::SLS::Logstore konfiguriert ist",
        "es": "Comprueba que ALIYUN::SLS::Logstore tenga TTL configurado",
        "fr": "Vérifie que ALIYUN::SLS::Logstore a TTL configuré",
        "pt": "Verifica se ALIYUN::SLS::Logstore tem TTL configurado"
    },
    "reason": {
        "en": "SLS Logstore must configure TTL is not satisfied.",
        "zh": "SLS Logstore 必须配置 TTL未满足。",
        "ja": "ALIYUN::SLS::Logstore に TTL が設定されていません。",
        "de": "Für ALIYUN::SLS::Logstore ist TTL nicht konfiguriert.",
        "es": "ALIYUN::SLS::Logstore no tiene TTL configurado.",
        "fr": "ALIYUN::SLS::Logstore n'a pas TTL configuré.",
        "pt": "ALIYUN::SLS::Logstore não tem TTL configurado."
    },
    "recommendation": {
        "en": "Configure TTL on ALIYUN::SLS::Logstore to satisfy the policy.",
        "zh": "请在 ALIYUN::SLS::Logstore 上配置 TTL 以满足策略。",
        "ja": "ポリシーを満たすには、ALIYUN::SLS::Logstore に TTL を設定してください。",
        "de": "Konfigurieren Sie TTL für ALIYUN::SLS::Logstore, um die Richtlinie zu erfüllen.",
        "es": "Configure TTL en ALIYUN::SLS::Logstore para cumplir la política.",
        "fr": "Configurez TTL sur ALIYUN::SLS::Logstore pour satisfaire la politique.",
        "pt": "Configure TTL em ALIYUN::SLS::Logstore para atender à política."
    },
    "resource_types": ["ALIYUN::SLS::Logstore"]
}

deny contains result if {
    some name, resource in helpers.resources_by_type("ALIYUN::SLS::Logstore")
    not is_compliant(resource)
    result := {
        "id": rule_meta.id,
        "resource_id": name,
        "violation_path": ["Properties", "TTL"],
        "meta": {
            "severity": rule_meta.severity,
            "reason": rule_meta.reason,
            "recommendation": rule_meta.recommendation,
        },
    }
}

is_compliant(resource) if {
    helpers.has_property(resource, "TTL")
}
