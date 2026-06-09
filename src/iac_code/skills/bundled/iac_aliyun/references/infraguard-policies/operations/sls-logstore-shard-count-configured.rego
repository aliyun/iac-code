package infraguard.rules.aliyun.sls_logstore_shard_count_configured

import rego.v1
import data.infraguard.helpers

rule_meta := {
    "id": "sls-logstore-shard-count-configured",
    "severity": "medium",
    "name": {
        "en": "SLS Logstore must configure shard count",
        "zh": "SLS Logstore 必须配置分区数",
        "ja": "ALIYUN::SLS::Logstore には ShardCount を設定する必要があります",
        "de": "Für ALIYUN::SLS::Logstore muss ShardCount konfiguriert sein",
        "es": "ALIYUN::SLS::Logstore debe tener ShardCount configurado",
        "fr": "ALIYUN::SLS::Logstore doit avoir ShardCount configuré",
        "pt": "ALIYUN::SLS::Logstore deve ter ShardCount configurado"
    },
    "description": {
        "en": "Checks SLS Logstore must configure shard count",
        "zh": "检查SLS Logstore 必须配置分区数",
        "ja": "ALIYUN::SLS::Logstore に ShardCount が設定されていることを確認します",
        "de": "Prüft, ob ShardCount für ALIYUN::SLS::Logstore konfiguriert ist",
        "es": "Comprueba que ALIYUN::SLS::Logstore tenga ShardCount configurado",
        "fr": "Vérifie que ALIYUN::SLS::Logstore a ShardCount configuré",
        "pt": "Verifica se ALIYUN::SLS::Logstore tem ShardCount configurado"
    },
    "reason": {
        "en": "SLS Logstore must configure shard count is not satisfied.",
        "zh": "SLS Logstore 必须配置分区数未满足。",
        "ja": "ALIYUN::SLS::Logstore に ShardCount が設定されていません。",
        "de": "Für ALIYUN::SLS::Logstore ist ShardCount nicht konfiguriert.",
        "es": "ALIYUN::SLS::Logstore no tiene ShardCount configurado.",
        "fr": "ALIYUN::SLS::Logstore n'a pas ShardCount configuré.",
        "pt": "ALIYUN::SLS::Logstore não tem ShardCount configurado."
    },
    "recommendation": {
        "en": "Configure ShardCount on ALIYUN::SLS::Logstore to satisfy the policy.",
        "zh": "请在 ALIYUN::SLS::Logstore 上配置 ShardCount 以满足策略。",
        "ja": "ポリシーを満たすには、ALIYUN::SLS::Logstore に ShardCount を設定してください。",
        "de": "Konfigurieren Sie ShardCount für ALIYUN::SLS::Logstore, um die Richtlinie zu erfüllen.",
        "es": "Configure ShardCount en ALIYUN::SLS::Logstore para cumplir la política.",
        "fr": "Configurez ShardCount sur ALIYUN::SLS::Logstore pour satisfaire la politique.",
        "pt": "Configure ShardCount em ALIYUN::SLS::Logstore para atender à política."
    },
    "resource_types": ["ALIYUN::SLS::Logstore"]
}

deny contains result if {
    some name, resource in helpers.resources_by_type("ALIYUN::SLS::Logstore")
    not is_compliant(resource)
    result := {
        "id": rule_meta.id,
        "resource_id": name,
        "violation_path": ["Properties", "ShardCount"],
        "meta": {
            "severity": rule_meta.severity,
            "reason": rule_meta.reason,
            "recommendation": rule_meta.recommendation,
        },
    }
}

is_compliant(resource) if {
    helpers.has_property(resource, "ShardCount")
}
