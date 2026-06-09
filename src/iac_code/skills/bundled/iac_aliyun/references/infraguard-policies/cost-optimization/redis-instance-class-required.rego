package infraguard.rules.aliyun.redis_instance_class_required

import rego.v1
import data.infraguard.helpers

rule_meta := {
    "id": "redis-instance-class-required",
    "severity": "medium",
    "name": {
        "en": "Redis instance must set instance class",
        "zh": "Redis 实例必须设置规格",
        "ja": "ALIYUN::REDIS::Instance には InstanceClass を設定する必要があります",
        "de": "Für ALIYUN::REDIS::Instance muss InstanceClass konfiguriert sein",
        "es": "ALIYUN::REDIS::Instance debe tener InstanceClass configurado",
        "fr": "ALIYUN::REDIS::Instance doit avoir InstanceClass configuré",
        "pt": "ALIYUN::REDIS::Instance deve ter InstanceClass configurado"
    },
    "description": {
        "en": "Checks Redis instance must set instance class",
        "zh": "检查Redis 实例必须设置规格",
        "ja": "ALIYUN::REDIS::Instance に InstanceClass が設定されていることを確認します",
        "de": "Prüft, ob InstanceClass für ALIYUN::REDIS::Instance konfiguriert ist",
        "es": "Comprueba que ALIYUN::REDIS::Instance tenga InstanceClass configurado",
        "fr": "Vérifie que ALIYUN::REDIS::Instance a InstanceClass configuré",
        "pt": "Verifica se ALIYUN::REDIS::Instance tem InstanceClass configurado"
    },
    "reason": {
        "en": "Redis instance must set instance class is not satisfied.",
        "zh": "Redis 实例必须设置规格未满足。",
        "ja": "ALIYUN::REDIS::Instance に InstanceClass が設定されていません。",
        "de": "Für ALIYUN::REDIS::Instance ist InstanceClass nicht konfiguriert.",
        "es": "ALIYUN::REDIS::Instance no tiene InstanceClass configurado.",
        "fr": "ALIYUN::REDIS::Instance n'a pas InstanceClass configuré.",
        "pt": "ALIYUN::REDIS::Instance não tem InstanceClass configurado."
    },
    "recommendation": {
        "en": "Configure InstanceClass on ALIYUN::REDIS::Instance to satisfy the policy.",
        "zh": "请在 ALIYUN::REDIS::Instance 上配置 InstanceClass 以满足策略。",
        "ja": "ポリシーを満たすには、ALIYUN::REDIS::Instance に InstanceClass を設定してください。",
        "de": "Konfigurieren Sie InstanceClass für ALIYUN::REDIS::Instance, um die Richtlinie zu erfüllen.",
        "es": "Configure InstanceClass en ALIYUN::REDIS::Instance para cumplir la política.",
        "fr": "Configurez InstanceClass sur ALIYUN::REDIS::Instance pour satisfaire la politique.",
        "pt": "Configure InstanceClass em ALIYUN::REDIS::Instance para atender à política."
    },
    "resource_types": ["ALIYUN::REDIS::Instance"]
}

deny contains result if {
    some name, resource in helpers.resources_by_type("ALIYUN::REDIS::Instance")
    not is_compliant(resource)
    result := {
        "id": rule_meta.id,
        "resource_id": name,
        "violation_path": ["Properties", "InstanceClass"],
        "meta": {
            "severity": rule_meta.severity,
            "reason": rule_meta.reason,
            "recommendation": rule_meta.recommendation,
        },
    }
}

is_compliant(resource) if {
    helpers.has_property(resource, "InstanceClass")
}
