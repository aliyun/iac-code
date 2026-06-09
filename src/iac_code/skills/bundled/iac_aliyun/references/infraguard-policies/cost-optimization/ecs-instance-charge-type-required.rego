package infraguard.rules.aliyun.ecs_instance_charge_type_required

import rego.v1
import data.infraguard.helpers

rule_meta := {
    "id": "ecs-instance-charge-type-required",
    "severity": "medium",
    "name": {
        "en": "ECS instance must set charge type",
        "zh": "ECS 实例必须设置付费类型",
        "ja": "ALIYUN::ECS::Instance には InstanceChargeType を設定する必要があります",
        "de": "Für ALIYUN::ECS::Instance muss InstanceChargeType konfiguriert sein",
        "es": "ALIYUN::ECS::Instance debe tener InstanceChargeType configurado",
        "fr": "ALIYUN::ECS::Instance doit avoir InstanceChargeType configuré",
        "pt": "ALIYUN::ECS::Instance deve ter InstanceChargeType configurado"
    },
    "description": {
        "en": "Checks ECS instance must set charge type",
        "zh": "检查ECS 实例必须设置付费类型",
        "ja": "ALIYUN::ECS::Instance に InstanceChargeType が設定されていることを確認します",
        "de": "Prüft, ob InstanceChargeType für ALIYUN::ECS::Instance konfiguriert ist",
        "es": "Comprueba que ALIYUN::ECS::Instance tenga InstanceChargeType configurado",
        "fr": "Vérifie que ALIYUN::ECS::Instance a InstanceChargeType configuré",
        "pt": "Verifica se ALIYUN::ECS::Instance tem InstanceChargeType configurado"
    },
    "reason": {
        "en": "ECS instance must set charge type is not satisfied.",
        "zh": "ECS 实例必须设置付费类型未满足。",
        "ja": "ALIYUN::ECS::Instance に InstanceChargeType が設定されていません。",
        "de": "Für ALIYUN::ECS::Instance ist InstanceChargeType nicht konfiguriert.",
        "es": "ALIYUN::ECS::Instance no tiene InstanceChargeType configurado.",
        "fr": "ALIYUN::ECS::Instance n'a pas InstanceChargeType configuré.",
        "pt": "ALIYUN::ECS::Instance não tem InstanceChargeType configurado."
    },
    "recommendation": {
        "en": "Configure InstanceChargeType on ALIYUN::ECS::Instance to satisfy the policy.",
        "zh": "请在 ALIYUN::ECS::Instance 上配置 InstanceChargeType 以满足策略。",
        "ja": "ポリシーを満たすには、ALIYUN::ECS::Instance に InstanceChargeType を設定してください。",
        "de": "Konfigurieren Sie InstanceChargeType für ALIYUN::ECS::Instance, um die Richtlinie zu erfüllen.",
        "es": "Configure InstanceChargeType en ALIYUN::ECS::Instance para cumplir la política.",
        "fr": "Configurez InstanceChargeType sur ALIYUN::ECS::Instance pour satisfaire la politique.",
        "pt": "Configure InstanceChargeType em ALIYUN::ECS::Instance para atender à política."
    },
    "resource_types": ["ALIYUN::ECS::Instance"]
}

deny contains result if {
    some name, resource in helpers.resources_by_type("ALIYUN::ECS::Instance")
    not is_compliant(resource)
    result := {
        "id": rule_meta.id,
        "resource_id": name,
        "violation_path": ["Properties", "InstanceChargeType"],
        "meta": {
            "severity": rule_meta.severity,
            "reason": rule_meta.reason,
            "recommendation": rule_meta.recommendation,
        },
    }
}

is_compliant(resource) if {
    helpers.has_property(resource, "InstanceChargeType")
}
