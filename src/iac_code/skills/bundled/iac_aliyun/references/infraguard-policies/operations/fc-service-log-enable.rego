package infraguard.rules.aliyun.fc_service_log_enable

import rego.v1
import data.infraguard.helpers

rule_meta := {
    "id": "fc-service-log-enable",
    "severity": "medium",
    "name": {
        "en": "FC service must configure logging",
        "zh": "函数计算服务必须配置日志",
        "ja": "ALIYUN::FC::Service には LogConfig を設定する必要があります",
        "de": "Für ALIYUN::FC::Service muss LogConfig konfiguriert sein",
        "es": "ALIYUN::FC::Service debe tener LogConfig configurado",
        "fr": "ALIYUN::FC::Service doit avoir LogConfig configuré",
        "pt": "ALIYUN::FC::Service deve ter LogConfig configurado"
    },
    "description": {
        "en": "Checks FC service must configure logging",
        "zh": "检查函数计算服务必须配置日志",
        "ja": "ALIYUN::FC::Service に LogConfig が設定されていることを確認します",
        "de": "Prüft, ob LogConfig für ALIYUN::FC::Service konfiguriert ist",
        "es": "Comprueba que ALIYUN::FC::Service tenga LogConfig configurado",
        "fr": "Vérifie que ALIYUN::FC::Service a LogConfig configuré",
        "pt": "Verifica se ALIYUN::FC::Service tem LogConfig configurado"
    },
    "reason": {
        "en": "FC service must configure logging is not satisfied.",
        "zh": "函数计算服务必须配置日志未满足。",
        "ja": "ALIYUN::FC::Service に LogConfig が設定されていません。",
        "de": "Für ALIYUN::FC::Service ist LogConfig nicht konfiguriert.",
        "es": "ALIYUN::FC::Service no tiene LogConfig configurado.",
        "fr": "ALIYUN::FC::Service n'a pas LogConfig configuré.",
        "pt": "ALIYUN::FC::Service não tem LogConfig configurado."
    },
    "recommendation": {
        "en": "Configure LogConfig on ALIYUN::FC::Service to satisfy the policy.",
        "zh": "请在 ALIYUN::FC::Service 上配置 LogConfig 以满足策略。",
        "ja": "ポリシーを満たすには、ALIYUN::FC::Service に LogConfig を設定してください。",
        "de": "Konfigurieren Sie LogConfig für ALIYUN::FC::Service, um die Richtlinie zu erfüllen.",
        "es": "Configure LogConfig en ALIYUN::FC::Service para cumplir la política.",
        "fr": "Configurez LogConfig sur ALIYUN::FC::Service pour satisfaire la politique.",
        "pt": "Configure LogConfig em ALIYUN::FC::Service para atender à política."
    },
    "resource_types": ["ALIYUN::FC::Service"]
}

deny contains result if {
    some name, resource in helpers.resources_by_type("ALIYUN::FC::Service")
    not is_compliant(resource)
    result := {
        "id": rule_meta.id,
        "resource_id": name,
        "violation_path": ["Properties", "LogConfig"],
        "meta": {
            "severity": rule_meta.severity,
            "reason": rule_meta.reason,
            "recommendation": rule_meta.recommendation,
        },
    }
}

is_compliant(resource) if {
    helpers.has_property(resource, "LogConfig")
}
