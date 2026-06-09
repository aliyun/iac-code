package infraguard.rules.aliyun.ecs_instance_type_required

import rego.v1
import data.infraguard.helpers

rule_meta := {
    "id": "ecs-instance-type-required",
    "severity": "medium",
    "name": {
        "en": "ECS instance must set instance type",
        "zh": "ECS 实例必须设置实例规格",
        "ja": "ALIYUN::ECS::Instance には InstanceType を設定する必要があります",
        "de": "Für ALIYUN::ECS::Instance muss InstanceType konfiguriert sein",
        "es": "ALIYUN::ECS::Instance debe tener InstanceType configurado",
        "fr": "ALIYUN::ECS::Instance doit avoir InstanceType configuré",
        "pt": "ALIYUN::ECS::Instance deve ter InstanceType configurado"
    },
    "description": {
        "en": "Checks ECS instance must set instance type",
        "zh": "检查ECS 实例必须设置实例规格",
        "ja": "ALIYUN::ECS::Instance に InstanceType が設定されていることを確認します",
        "de": "Prüft, ob InstanceType für ALIYUN::ECS::Instance konfiguriert ist",
        "es": "Comprueba que ALIYUN::ECS::Instance tenga InstanceType configurado",
        "fr": "Vérifie que ALIYUN::ECS::Instance a InstanceType configuré",
        "pt": "Verifica se ALIYUN::ECS::Instance tem InstanceType configurado"
    },
    "reason": {
        "en": "ECS instance must set instance type is not satisfied.",
        "zh": "ECS 实例必须设置实例规格未满足。",
        "ja": "ALIYUN::ECS::Instance に InstanceType が設定されていません。",
        "de": "Für ALIYUN::ECS::Instance ist InstanceType nicht konfiguriert.",
        "es": "ALIYUN::ECS::Instance no tiene InstanceType configurado.",
        "fr": "ALIYUN::ECS::Instance n'a pas InstanceType configuré.",
        "pt": "ALIYUN::ECS::Instance não tem InstanceType configurado."
    },
    "recommendation": {
        "en": "Configure InstanceType on ALIYUN::ECS::Instance to satisfy the policy.",
        "zh": "请在 ALIYUN::ECS::Instance 上配置 InstanceType 以满足策略。",
        "ja": "ポリシーを満たすには、ALIYUN::ECS::Instance に InstanceType を設定してください。",
        "de": "Konfigurieren Sie InstanceType für ALIYUN::ECS::Instance, um die Richtlinie zu erfüllen.",
        "es": "Configure InstanceType en ALIYUN::ECS::Instance para cumplir la política.",
        "fr": "Configurez InstanceType sur ALIYUN::ECS::Instance pour satisfaire la politique.",
        "pt": "Configure InstanceType em ALIYUN::ECS::Instance para atender à política."
    },
    "resource_types": ["ALIYUN::ECS::Instance"]
}

deny contains result if {
    some name, resource in helpers.resources_by_type("ALIYUN::ECS::Instance")
    not is_compliant(resource)
    result := {
        "id": rule_meta.id,
        "resource_id": name,
        "violation_path": ["Properties", "InstanceType"],
        "meta": {
            "severity": rule_meta.severity,
            "reason": rule_meta.reason,
            "recommendation": rule_meta.recommendation,
        },
    }
}

is_compliant(resource) if {
    helpers.has_property(resource, "InstanceType")
}
