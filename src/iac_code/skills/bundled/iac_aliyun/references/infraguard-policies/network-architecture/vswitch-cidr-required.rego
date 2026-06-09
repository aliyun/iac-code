package infraguard.rules.aliyun.vswitch_cidr_required

import rego.v1
import data.infraguard.helpers

rule_meta := {
    "id": "vswitch-cidr-required",
    "severity": "high",
    "name": {
        "en": "VSwitch must configure CIDR block",
        "zh": "交换机必须配置网段",
        "ja": "ALIYUN::ECS::VSwitch には CidrBlock を設定する必要があります",
        "de": "Für ALIYUN::ECS::VSwitch muss CidrBlock konfiguriert sein",
        "es": "ALIYUN::ECS::VSwitch debe tener CidrBlock configurado",
        "fr": "ALIYUN::ECS::VSwitch doit avoir CidrBlock configuré",
        "pt": "ALIYUN::ECS::VSwitch deve ter CidrBlock configurado"
    },
    "description": {
        "en": "Checks VSwitch must configure CIDR block",
        "zh": "检查交换机必须配置网段",
        "ja": "ALIYUN::ECS::VSwitch に CidrBlock が設定されていることを確認します",
        "de": "Prüft, ob CidrBlock für ALIYUN::ECS::VSwitch konfiguriert ist",
        "es": "Comprueba que ALIYUN::ECS::VSwitch tenga CidrBlock configurado",
        "fr": "Vérifie que ALIYUN::ECS::VSwitch a CidrBlock configuré",
        "pt": "Verifica se ALIYUN::ECS::VSwitch tem CidrBlock configurado"
    },
    "reason": {
        "en": "VSwitch must configure CIDR block is not satisfied.",
        "zh": "交换机必须配置网段未满足。",
        "ja": "ALIYUN::ECS::VSwitch に CidrBlock が設定されていません。",
        "de": "Für ALIYUN::ECS::VSwitch ist CidrBlock nicht konfiguriert.",
        "es": "ALIYUN::ECS::VSwitch no tiene CidrBlock configurado.",
        "fr": "ALIYUN::ECS::VSwitch n'a pas CidrBlock configuré.",
        "pt": "ALIYUN::ECS::VSwitch não tem CidrBlock configurado."
    },
    "recommendation": {
        "en": "Configure CidrBlock on ALIYUN::ECS::VSwitch to satisfy the policy.",
        "zh": "请在 ALIYUN::ECS::VSwitch 上配置 CidrBlock 以满足策略。",
        "ja": "ポリシーを満たすには、ALIYUN::ECS::VSwitch に CidrBlock を設定してください。",
        "de": "Konfigurieren Sie CidrBlock für ALIYUN::ECS::VSwitch, um die Richtlinie zu erfüllen.",
        "es": "Configure CidrBlock en ALIYUN::ECS::VSwitch para cumplir la política.",
        "fr": "Configurez CidrBlock sur ALIYUN::ECS::VSwitch pour satisfaire la politique.",
        "pt": "Configure CidrBlock em ALIYUN::ECS::VSwitch para atender à política."
    },
    "resource_types": ["ALIYUN::ECS::VSwitch"]
}

deny contains result if {
    some name, resource in helpers.resources_by_type("ALIYUN::ECS::VSwitch")
    not is_compliant(resource)
    result := {
        "id": rule_meta.id,
        "resource_id": name,
        "violation_path": ["Properties", "CidrBlock"],
        "meta": {
            "severity": rule_meta.severity,
            "reason": rule_meta.reason,
            "recommendation": rule_meta.recommendation,
        },
    }
}

is_compliant(resource) if {
    helpers.has_property(resource, "CidrBlock")
}
