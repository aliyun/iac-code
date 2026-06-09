package infraguard.rules.aliyun.vpc_cidr_required

import rego.v1
import data.infraguard.helpers

rule_meta := {
    "id": "vpc-cidr-required",
    "severity": "high",
    "name": {
        "en": "VPC must configure CIDR block",
        "zh": "VPC 必须配置网段",
        "ja": "ALIYUN::ECS::VPC には CidrBlock を設定する必要があります",
        "de": "Für ALIYUN::ECS::VPC muss CidrBlock konfiguriert sein",
        "es": "ALIYUN::ECS::VPC debe tener CidrBlock configurado",
        "fr": "ALIYUN::ECS::VPC doit avoir CidrBlock configuré",
        "pt": "ALIYUN::ECS::VPC deve ter CidrBlock configurado"
    },
    "description": {
        "en": "Checks VPC must configure CIDR block",
        "zh": "检查VPC 必须配置网段",
        "ja": "ALIYUN::ECS::VPC に CidrBlock が設定されていることを確認します",
        "de": "Prüft, ob CidrBlock für ALIYUN::ECS::VPC konfiguriert ist",
        "es": "Comprueba que ALIYUN::ECS::VPC tenga CidrBlock configurado",
        "fr": "Vérifie que ALIYUN::ECS::VPC a CidrBlock configuré",
        "pt": "Verifica se ALIYUN::ECS::VPC tem CidrBlock configurado"
    },
    "reason": {
        "en": "VPC must configure CIDR block is not satisfied.",
        "zh": "VPC 必须配置网段未满足。",
        "ja": "ALIYUN::ECS::VPC に CidrBlock が設定されていません。",
        "de": "Für ALIYUN::ECS::VPC ist CidrBlock nicht konfiguriert.",
        "es": "ALIYUN::ECS::VPC no tiene CidrBlock configurado.",
        "fr": "ALIYUN::ECS::VPC n'a pas CidrBlock configuré.",
        "pt": "ALIYUN::ECS::VPC não tem CidrBlock configurado."
    },
    "recommendation": {
        "en": "Configure CidrBlock on ALIYUN::ECS::VPC to satisfy the policy.",
        "zh": "请在 ALIYUN::ECS::VPC 上配置 CidrBlock 以满足策略。",
        "ja": "ポリシーを満たすには、ALIYUN::ECS::VPC に CidrBlock を設定してください。",
        "de": "Konfigurieren Sie CidrBlock für ALIYUN::ECS::VPC, um die Richtlinie zu erfüllen.",
        "es": "Configure CidrBlock en ALIYUN::ECS::VPC para cumplir la política.",
        "fr": "Configurez CidrBlock sur ALIYUN::ECS::VPC pour satisfaire la politique.",
        "pt": "Configure CidrBlock em ALIYUN::ECS::VPC para atender à política."
    },
    "resource_types": ["ALIYUN::ECS::VPC"]
}

deny contains result if {
    some name, resource in helpers.resources_by_type("ALIYUN::ECS::VPC")
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
