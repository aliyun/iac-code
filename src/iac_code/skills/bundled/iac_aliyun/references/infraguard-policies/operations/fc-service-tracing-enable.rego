package infraguard.rules.aliyun.fc_service_tracing_enable

import rego.v1
import data.infraguard.helpers

rule_meta := {
    "id": "fc-service-tracing-enable",
    "severity": "medium",
    "name": {
        "en": "FC service must configure tracing",
        "zh": "函数计算服务必须配置链路追踪",
        "ja": "ALIYUN::FC::Service には TracingConfig を設定する必要があります",
        "de": "Für ALIYUN::FC::Service muss TracingConfig konfiguriert sein",
        "es": "ALIYUN::FC::Service debe tener TracingConfig configurado",
        "fr": "ALIYUN::FC::Service doit avoir TracingConfig configuré",
        "pt": "ALIYUN::FC::Service deve ter TracingConfig configurado"
    },
    "description": {
        "en": "Checks FC service must configure tracing",
        "zh": "检查函数计算服务必须配置链路追踪",
        "ja": "ALIYUN::FC::Service に TracingConfig が設定されていることを確認します",
        "de": "Prüft, ob TracingConfig für ALIYUN::FC::Service konfiguriert ist",
        "es": "Comprueba que ALIYUN::FC::Service tenga TracingConfig configurado",
        "fr": "Vérifie que ALIYUN::FC::Service a TracingConfig configuré",
        "pt": "Verifica se ALIYUN::FC::Service tem TracingConfig configurado"
    },
    "reason": {
        "en": "FC service must configure tracing is not satisfied.",
        "zh": "函数计算服务必须配置链路追踪未满足。",
        "ja": "ALIYUN::FC::Service に TracingConfig が設定されていません。",
        "de": "Für ALIYUN::FC::Service ist TracingConfig nicht konfiguriert.",
        "es": "ALIYUN::FC::Service no tiene TracingConfig configurado.",
        "fr": "ALIYUN::FC::Service n'a pas TracingConfig configuré.",
        "pt": "ALIYUN::FC::Service não tem TracingConfig configurado."
    },
    "recommendation": {
        "en": "Configure TracingConfig on ALIYUN::FC::Service to satisfy the policy.",
        "zh": "请在 ALIYUN::FC::Service 上配置 TracingConfig 以满足策略。",
        "ja": "ポリシーを満たすには、ALIYUN::FC::Service に TracingConfig を設定してください。",
        "de": "Konfigurieren Sie TracingConfig für ALIYUN::FC::Service, um die Richtlinie zu erfüllen.",
        "es": "Configure TracingConfig en ALIYUN::FC::Service para cumplir la política.",
        "fr": "Configurez TracingConfig sur ALIYUN::FC::Service pour satisfaire la politique.",
        "pt": "Configure TracingConfig em ALIYUN::FC::Service para atender à política."
    },
    "resource_types": ["ALIYUN::FC::Service"]
}

deny contains result if {
    some name, resource in helpers.resources_by_type("ALIYUN::FC::Service")
    not is_compliant(resource)
    result := {
        "id": rule_meta.id,
        "resource_id": name,
        "violation_path": ["Properties", "TracingConfig"],
        "meta": {
            "severity": rule_meta.severity,
            "reason": rule_meta.reason,
            "recommendation": rule_meta.recommendation,
        },
    }
}

is_compliant(resource) if {
    helpers.has_property(resource, "TracingConfig")
}
