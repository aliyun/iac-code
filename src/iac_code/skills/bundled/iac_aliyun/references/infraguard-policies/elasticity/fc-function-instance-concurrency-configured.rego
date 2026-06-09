package infraguard.rules.aliyun.fc_function_instance_concurrency_configured

import rego.v1
import data.infraguard.helpers

rule_meta := {
    "id": "fc-function-instance-concurrency-configured",
    "severity": "medium",
    "name": {
        "en": "FC function must configure instance concurrency",
        "zh": "函数计算函数必须配置实例并发",
        "ja": "ALIYUN::FC::Function には InstanceConcurrency を設定する必要があります",
        "de": "Für ALIYUN::FC::Function muss InstanceConcurrency konfiguriert sein",
        "es": "ALIYUN::FC::Function debe tener InstanceConcurrency configurado",
        "fr": "ALIYUN::FC::Function doit avoir InstanceConcurrency configuré",
        "pt": "ALIYUN::FC::Function deve ter InstanceConcurrency configurado"
    },
    "description": {
        "en": "Checks FC function must configure instance concurrency",
        "zh": "检查函数计算函数必须配置实例并发",
        "ja": "ALIYUN::FC::Function に InstanceConcurrency が設定されていることを確認します",
        "de": "Prüft, ob InstanceConcurrency für ALIYUN::FC::Function konfiguriert ist",
        "es": "Comprueba que ALIYUN::FC::Function tenga InstanceConcurrency configurado",
        "fr": "Vérifie que ALIYUN::FC::Function a InstanceConcurrency configuré",
        "pt": "Verifica se ALIYUN::FC::Function tem InstanceConcurrency configurado"
    },
    "reason": {
        "en": "FC function must configure instance concurrency is not satisfied.",
        "zh": "函数计算函数必须配置实例并发未满足。",
        "ja": "ALIYUN::FC::Function に InstanceConcurrency が設定されていません。",
        "de": "Für ALIYUN::FC::Function ist InstanceConcurrency nicht konfiguriert.",
        "es": "ALIYUN::FC::Function no tiene InstanceConcurrency configurado.",
        "fr": "ALIYUN::FC::Function n'a pas InstanceConcurrency configuré.",
        "pt": "ALIYUN::FC::Function não tem InstanceConcurrency configurado."
    },
    "recommendation": {
        "en": "Configure InstanceConcurrency on ALIYUN::FC::Function to satisfy the policy.",
        "zh": "请在 ALIYUN::FC::Function 上配置 InstanceConcurrency 以满足策略。",
        "ja": "ポリシーを満たすには、ALIYUN::FC::Function に InstanceConcurrency を設定してください。",
        "de": "Konfigurieren Sie InstanceConcurrency für ALIYUN::FC::Function, um die Richtlinie zu erfüllen.",
        "es": "Configure InstanceConcurrency en ALIYUN::FC::Function para cumplir la política.",
        "fr": "Configurez InstanceConcurrency sur ALIYUN::FC::Function pour satisfaire la politique.",
        "pt": "Configure InstanceConcurrency em ALIYUN::FC::Function para atender à política."
    },
    "resource_types": ["ALIYUN::FC::Function"]
}

deny contains result if {
    some name, resource in helpers.resources_by_type("ALIYUN::FC::Function")
    not is_compliant(resource)
    result := {
        "id": rule_meta.id,
        "resource_id": name,
        "violation_path": ["Properties", "InstanceConcurrency"],
        "meta": {
            "severity": rule_meta.severity,
            "reason": rule_meta.reason,
            "recommendation": rule_meta.recommendation,
        },
    }
}

is_compliant(resource) if {
    helpers.has_property(resource, "InstanceConcurrency")
}
