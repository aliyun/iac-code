package infraguard.rules.aliyun.ecs_instance_tags_required

import rego.v1
import data.infraguard.helpers

rule_meta := {
    "id": "ecs-instance-tags-required",
    "severity": "medium",
    "name": {
        "en": "ECS instance must configure tags",
        "zh": "ECS 实例必须配置标签",
        "ja": "ALIYUN::ECS::Instance には Tags を設定する必要があります",
        "de": "Für ALIYUN::ECS::Instance muss Tags konfiguriert sein",
        "es": "ALIYUN::ECS::Instance debe tener Tags configurado",
        "fr": "ALIYUN::ECS::Instance doit avoir Tags configuré",
        "pt": "ALIYUN::ECS::Instance deve ter Tags configurado"
    },
    "description": {
        "en": "Checks ECS instance must configure tags",
        "zh": "检查ECS 实例必须配置标签",
        "ja": "ALIYUN::ECS::Instance に Tags が設定されていることを確認します",
        "de": "Prüft, ob Tags für ALIYUN::ECS::Instance konfiguriert ist",
        "es": "Comprueba que ALIYUN::ECS::Instance tenga Tags configurado",
        "fr": "Vérifie que ALIYUN::ECS::Instance a Tags configuré",
        "pt": "Verifica se ALIYUN::ECS::Instance tem Tags configurado"
    },
    "reason": {
        "en": "ECS instance must configure tags is not satisfied.",
        "zh": "ECS 实例必须配置标签未满足。",
        "ja": "ALIYUN::ECS::Instance に Tags が設定されていません。",
        "de": "Für ALIYUN::ECS::Instance ist Tags nicht konfiguriert.",
        "es": "ALIYUN::ECS::Instance no tiene Tags configurado.",
        "fr": "ALIYUN::ECS::Instance n'a pas Tags configuré.",
        "pt": "ALIYUN::ECS::Instance não tem Tags configurado."
    },
    "recommendation": {
        "en": "Configure Tags on ALIYUN::ECS::Instance to satisfy the policy.",
        "zh": "请在 ALIYUN::ECS::Instance 上配置 Tags 以满足策略。",
        "ja": "ポリシーを満たすには、ALIYUN::ECS::Instance に Tags を設定してください。",
        "de": "Konfigurieren Sie Tags für ALIYUN::ECS::Instance, um die Richtlinie zu erfüllen.",
        "es": "Configure Tags en ALIYUN::ECS::Instance para cumplir la política.",
        "fr": "Configurez Tags sur ALIYUN::ECS::Instance pour satisfaire la politique.",
        "pt": "Configure Tags em ALIYUN::ECS::Instance para atender à política."
    },
    "resource_types": ["ALIYUN::ECS::Instance"]
}

deny contains result if {
    some name, resource in helpers.resources_by_type("ALIYUN::ECS::Instance")
    not is_compliant(resource)
    result := {
        "id": rule_meta.id,
        "resource_id": name,
        "violation_path": ["Properties", "Tags"],
        "meta": {
            "severity": rule_meta.severity,
            "reason": rule_meta.reason,
            "recommendation": rule_meta.recommendation,
        },
    }
}

is_compliant(resource) if {
    helpers.has_property(resource, "Tags")
}
