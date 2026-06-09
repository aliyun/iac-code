package infraguard.rules.aliyun.vswitch_zone_required

import rego.v1
import data.infraguard.helpers

rule_meta := {
    "id": "vswitch-zone-required",
    "severity": "medium",
    "name": {
        "en": "VSwitch must configure zone",
        "zh": "交换机必须配置可用区",
        "ja": "ALIYUN::ECS::VSwitch には ZoneId を設定する必要があります",
        "de": "Für ALIYUN::ECS::VSwitch muss ZoneId konfiguriert sein",
        "es": "ALIYUN::ECS::VSwitch debe tener ZoneId configurado",
        "fr": "ALIYUN::ECS::VSwitch doit avoir ZoneId configuré",
        "pt": "ALIYUN::ECS::VSwitch deve ter ZoneId configurado"
    },
    "description": {
        "en": "Checks VSwitch must configure zone",
        "zh": "检查交换机必须配置可用区",
        "ja": "ALIYUN::ECS::VSwitch に ZoneId が設定されていることを確認します",
        "de": "Prüft, ob ZoneId für ALIYUN::ECS::VSwitch konfiguriert ist",
        "es": "Comprueba que ALIYUN::ECS::VSwitch tenga ZoneId configurado",
        "fr": "Vérifie que ALIYUN::ECS::VSwitch a ZoneId configuré",
        "pt": "Verifica se ALIYUN::ECS::VSwitch tem ZoneId configurado"
    },
    "reason": {
        "en": "VSwitch must configure zone is not satisfied.",
        "zh": "交换机必须配置可用区未满足。",
        "ja": "ALIYUN::ECS::VSwitch に ZoneId が設定されていません。",
        "de": "Für ALIYUN::ECS::VSwitch ist ZoneId nicht konfiguriert.",
        "es": "ALIYUN::ECS::VSwitch no tiene ZoneId configurado.",
        "fr": "ALIYUN::ECS::VSwitch n'a pas ZoneId configuré.",
        "pt": "ALIYUN::ECS::VSwitch não tem ZoneId configurado."
    },
    "recommendation": {
        "en": "Configure ZoneId on ALIYUN::ECS::VSwitch to satisfy the policy.",
        "zh": "请在 ALIYUN::ECS::VSwitch 上配置 ZoneId 以满足策略。",
        "ja": "ポリシーを満たすには、ALIYUN::ECS::VSwitch に ZoneId を設定してください。",
        "de": "Konfigurieren Sie ZoneId für ALIYUN::ECS::VSwitch, um die Richtlinie zu erfüllen.",
        "es": "Configure ZoneId en ALIYUN::ECS::VSwitch para cumplir la política.",
        "fr": "Configurez ZoneId sur ALIYUN::ECS::VSwitch pour satisfaire la politique.",
        "pt": "Configure ZoneId em ALIYUN::ECS::VSwitch para atender à política."
    },
    "resource_types": ["ALIYUN::ECS::VSwitch"]
}

deny contains result if {
    some name, resource in helpers.resources_by_type("ALIYUN::ECS::VSwitch")
    not is_compliant(resource)
    result := {
        "id": rule_meta.id,
        "resource_id": name,
        "violation_path": ["Properties", "ZoneId"],
        "meta": {
            "severity": rule_meta.severity,
            "reason": rule_meta.reason,
            "recommendation": rule_meta.recommendation,
        },
    }
}

is_compliant(resource) if {
    helpers.has_property(resource, "ZoneId")
}
