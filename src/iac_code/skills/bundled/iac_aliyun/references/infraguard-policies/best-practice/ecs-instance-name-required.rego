package infraguard.rules.aliyun.ecs_instance_name_required

import rego.v1
import data.infraguard.helpers

rule_meta := {
    "id": "ecs-instance-name-required",
    "severity": "medium",
    "name": {
        "en": "ECS instance must configure name",
        "zh": "ECS 实例必须配置名称",
        "ja": "ECS インスタンスには名前を設定する必要があります",
        "de": "Für ECS-Instanzen muss ein Name konfiguriert sein",
        "es": "Las instancias ECS deben tener un nombre configurado",
        "fr": "Les instances ECS doivent avoir un nom configuré",
        "pt": "As instâncias ECS devem ter um nome configurado"
    },
    "description": {
        "en": "Checks ECS instance must configure name",
        "zh": "检查ECS 实例必须配置名称",
        "ja": "ECS インスタンスに名前が設定されていることを確認します",
        "de": "Prüft, ob für ECS-Instanzen ein Name konfiguriert ist",
        "es": "Comprueba que las instancias ECS tengan un nombre configurado",
        "fr": "Vérifie que les instances ECS ont un nom configuré",
        "pt": "Verifica se as instâncias ECS têm um nome configurado"
    },
    "reason": {
        "en": "ECS instance must configure name is not satisfied.",
        "zh": "ECS 实例必须配置名称未满足。",
        "ja": "ECS インスタンス名の設定要件を満たしていません。",
        "de": "Für die ECS-Instanz ist kein Name konfiguriert.",
        "es": "La instancia ECS no tiene un nombre configurado.",
        "fr": "L'instance ECS n'a pas de nom configuré.",
        "pt": "A instância ECS não tem um nome configurado."
    },
    "recommendation": {
        "en": "Configure InstanceName on ALIYUN::ECS::Instance to satisfy the policy.",
        "zh": "请在 ALIYUN::ECS::Instance 上配置 InstanceName 以满足策略。",
        "ja": "ポリシーを満たすには、ALIYUN::ECS::Instance に InstanceName を設定してください。",
        "de": "Konfigurieren Sie InstanceName für ALIYUN::ECS::Instance, um die Richtlinie zu erfüllen.",
        "es": "Configure InstanceName en ALIYUN::ECS::Instance para cumplir la política.",
        "fr": "Configurez InstanceName sur ALIYUN::ECS::Instance pour satisfaire la politique.",
        "pt": "Configure InstanceName em ALIYUN::ECS::Instance para atender à política."
    },
    "resource_types": ["ALIYUN::ECS::Instance"]
}

deny contains result if {
    some name, resource in helpers.resources_by_type("ALIYUN::ECS::Instance")
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
