package infraguard.rules.aliyun.vswitch_name_required

import rego.v1
import data.infraguard.helpers

rule_meta := {
    "id": "vswitch-name-required",
    "severity": "medium",
    "name": {
        "en": "VSwitch must configure name",
        "zh": "交换机必须配置名称",
        "ja": "ALIYUN::ECS::VSwitch には VSwitchName を設定する必要があります",
        "de": "Für ALIYUN::ECS::VSwitch muss VSwitchName konfiguriert sein",
        "es": "ALIYUN::ECS::VSwitch debe tener VSwitchName configurado",
        "fr": "ALIYUN::ECS::VSwitch doit avoir VSwitchName configuré",
        "pt": "ALIYUN::ECS::VSwitch deve ter VSwitchName configurado"
    },
    "description": {
        "en": "Checks VSwitch must configure name",
        "zh": "检查交换机必须配置名称",
        "ja": "ALIYUN::ECS::VSwitch に VSwitchName が設定されていることを確認します",
        "de": "Prüft, ob VSwitchName für ALIYUN::ECS::VSwitch konfiguriert ist",
        "es": "Comprueba que ALIYUN::ECS::VSwitch tenga VSwitchName configurado",
        "fr": "Vérifie que ALIYUN::ECS::VSwitch a VSwitchName configuré",
        "pt": "Verifica se ALIYUN::ECS::VSwitch tem VSwitchName configurado"
    },
    "reason": {
        "en": "VSwitch must configure name is not satisfied.",
        "zh": "交换机必须配置名称未满足。",
        "ja": "ALIYUN::ECS::VSwitch に VSwitchName が設定されていません。",
        "de": "Für ALIYUN::ECS::VSwitch ist VSwitchName nicht konfiguriert.",
        "es": "ALIYUN::ECS::VSwitch no tiene VSwitchName configurado.",
        "fr": "ALIYUN::ECS::VSwitch n'a pas VSwitchName configuré.",
        "pt": "ALIYUN::ECS::VSwitch não tem VSwitchName configurado."
    },
    "recommendation": {
        "en": "Configure VSwitchName on ALIYUN::ECS::VSwitch to satisfy the policy.",
        "zh": "请在 ALIYUN::ECS::VSwitch 上配置 VSwitchName 以满足策略。",
        "ja": "ポリシーを満たすには、ALIYUN::ECS::VSwitch に VSwitchName を設定してください。",
        "de": "Konfigurieren Sie VSwitchName für ALIYUN::ECS::VSwitch, um die Richtlinie zu erfüllen.",
        "es": "Configure VSwitchName en ALIYUN::ECS::VSwitch para cumplir la política.",
        "fr": "Configurez VSwitchName sur ALIYUN::ECS::VSwitch pour satisfaire la politique.",
        "pt": "Configure VSwitchName em ALIYUN::ECS::VSwitch para atender à política."
    },
    "resource_types": ["ALIYUN::ECS::VSwitch"]
}

deny contains result if {
    some name, resource in helpers.resources_by_type("ALIYUN::ECS::VSwitch")
    not is_compliant(resource)
    result := {
        "id": rule_meta.id,
        "resource_id": name,
        "violation_path": ["Properties", "VSwitchName"],
        "meta": {
            "severity": rule_meta.severity,
            "reason": rule_meta.reason,
            "recommendation": rule_meta.recommendation,
        },
    }
}

is_compliant(resource) if {
    helpers.has_property(resource, "VSwitchName")
}
