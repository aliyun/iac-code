package infraguard.rules.aliyun.ecs_security_group_description_required

import rego.v1
import data.infraguard.helpers

rule_meta := {
    "id": "ecs-security-group-description-required",
    "severity": "medium",
    "name": {
        "en": "Security group must configure description",
        "zh": "安全组必须配置描述",
        "ja": "ALIYUN::ECS::SecurityGroup には Description を設定する必要があります",
        "de": "Für ALIYUN::ECS::SecurityGroup muss Description konfiguriert sein",
        "es": "ALIYUN::ECS::SecurityGroup debe tener Description configurado",
        "fr": "ALIYUN::ECS::SecurityGroup doit avoir Description configuré",
        "pt": "ALIYUN::ECS::SecurityGroup deve ter Description configurado"
    },
    "description": {
        "en": "Checks Security group must configure description",
        "zh": "检查安全组必须配置描述",
        "ja": "ALIYUN::ECS::SecurityGroup に Description が設定されていることを確認します",
        "de": "Prüft, ob Description für ALIYUN::ECS::SecurityGroup konfiguriert ist",
        "es": "Comprueba que ALIYUN::ECS::SecurityGroup tenga Description configurado",
        "fr": "Vérifie que ALIYUN::ECS::SecurityGroup a Description configuré",
        "pt": "Verifica se ALIYUN::ECS::SecurityGroup tem Description configurado"
    },
    "reason": {
        "en": "Security group must configure description is not satisfied.",
        "zh": "安全组必须配置描述未满足。",
        "ja": "ALIYUN::ECS::SecurityGroup に Description が設定されていません。",
        "de": "Für ALIYUN::ECS::SecurityGroup ist Description nicht konfiguriert.",
        "es": "ALIYUN::ECS::SecurityGroup no tiene Description configurado.",
        "fr": "ALIYUN::ECS::SecurityGroup n'a pas Description configuré.",
        "pt": "ALIYUN::ECS::SecurityGroup não tem Description configurado."
    },
    "recommendation": {
        "en": "Configure Description on ALIYUN::ECS::SecurityGroup to satisfy the policy.",
        "zh": "请在 ALIYUN::ECS::SecurityGroup 上配置 Description 以满足策略。",
        "ja": "ポリシーを満たすには、ALIYUN::ECS::SecurityGroup に Description を設定してください。",
        "de": "Konfigurieren Sie Description für ALIYUN::ECS::SecurityGroup, um die Richtlinie zu erfüllen.",
        "es": "Configure Description en ALIYUN::ECS::SecurityGroup para cumplir la política.",
        "fr": "Configurez Description sur ALIYUN::ECS::SecurityGroup pour satisfaire la politique.",
        "pt": "Configure Description em ALIYUN::ECS::SecurityGroup para atender à política."
    },
    "resource_types": ["ALIYUN::ECS::SecurityGroup"]
}

deny contains result if {
    some name, resource in helpers.resources_by_type("ALIYUN::ECS::SecurityGroup")
    not is_compliant(resource)
    result := {
        "id": rule_meta.id,
        "resource_id": name,
        "violation_path": ["Properties", "Description"],
        "meta": {
            "severity": rule_meta.severity,
            "reason": rule_meta.reason,
            "recommendation": rule_meta.recommendation,
        },
    }
}

is_compliant(resource) if {
    helpers.has_property(resource, "Description")
}
