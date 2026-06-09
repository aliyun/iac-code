package infraguard.rules.aliyun.redis_instance_name_required

import rego.v1
import data.infraguard.helpers

rule_meta := {
    "id": "redis-instance-name-required",
    "severity": "medium",
    "name": {
        "en": "Redis instance must configure name",
        "zh": "Redis 实例必须配置名称",
        "ja": "ALIYUN::REDIS::Instance には InstanceName を設定する必要があります",
        "de": "Für ALIYUN::REDIS::Instance muss InstanceName konfiguriert sein",
        "es": "ALIYUN::REDIS::Instance debe tener InstanceName configurado",
        "fr": "ALIYUN::REDIS::Instance doit avoir InstanceName configuré",
        "pt": "ALIYUN::REDIS::Instance deve ter InstanceName configurado"
    },
    "description": {
        "en": "Checks Redis instance must configure name",
        "zh": "检查Redis 实例必须配置名称",
        "ja": "ALIYUN::REDIS::Instance に InstanceName が設定されていることを確認します",
        "de": "Prüft, ob InstanceName für ALIYUN::REDIS::Instance konfiguriert ist",
        "es": "Comprueba que ALIYUN::REDIS::Instance tenga InstanceName configurado",
        "fr": "Vérifie que ALIYUN::REDIS::Instance a InstanceName configuré",
        "pt": "Verifica se ALIYUN::REDIS::Instance tem InstanceName configurado"
    },
    "reason": {
        "en": "Redis instance must configure name is not satisfied.",
        "zh": "Redis 实例必须配置名称未满足。",
        "ja": "ALIYUN::REDIS::Instance に InstanceName が設定されていません。",
        "de": "Für ALIYUN::REDIS::Instance ist InstanceName nicht konfiguriert.",
        "es": "ALIYUN::REDIS::Instance no tiene InstanceName configurado.",
        "fr": "ALIYUN::REDIS::Instance n'a pas InstanceName configuré.",
        "pt": "ALIYUN::REDIS::Instance não tem InstanceName configurado."
    },
    "recommendation": {
        "en": "Configure InstanceName on ALIYUN::REDIS::Instance to satisfy the policy.",
        "zh": "请在 ALIYUN::REDIS::Instance 上配置 InstanceName 以满足策略。",
        "ja": "ポリシーを満たすには、ALIYUN::REDIS::Instance に InstanceName を設定してください。",
        "de": "Konfigurieren Sie InstanceName für ALIYUN::REDIS::Instance, um die Richtlinie zu erfüllen.",
        "es": "Configure InstanceName en ALIYUN::REDIS::Instance para cumplir la política.",
        "fr": "Configurez InstanceName sur ALIYUN::REDIS::Instance pour satisfaire la politique.",
        "pt": "Configure InstanceName em ALIYUN::REDIS::Instance para atender à política."
    },
    "resource_types": ["ALIYUN::REDIS::Instance"]
}

deny contains result if {
    some name, resource in helpers.resources_by_type("ALIYUN::REDIS::Instance")
    not is_compliant(resource)
    result := {
        "id": rule_meta.id,
        "resource_id": name,
        "violation_path": ["Properties", "InstanceName"],
        "meta": {
            "severity": rule_meta.severity,
            "reason": rule_meta.reason,
            "recommendation": rule_meta.recommendation,
        },
    }
}

is_compliant(resource) if {
    helpers.has_property(resource, "InstanceName")
}
