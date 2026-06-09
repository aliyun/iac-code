package infraguard.rules.aliyun.vpc_name_required

import rego.v1
import data.infraguard.helpers

rule_meta := {
    "id": "vpc-name-required",
    "severity": "medium",
    "name": {
        "en": "VPC must configure name",
        "zh": "VPC 必须配置名称",
        "ja": "ALIYUN::ECS::VPC には VpcName を設定する必要があります",
        "de": "Für ALIYUN::ECS::VPC muss VpcName konfiguriert sein",
        "es": "ALIYUN::ECS::VPC debe tener VpcName configurado",
        "fr": "ALIYUN::ECS::VPC doit avoir VpcName configuré",
        "pt": "ALIYUN::ECS::VPC deve ter VpcName configurado"
    },
    "description": {
        "en": "Checks VPC must configure name",
        "zh": "检查VPC 必须配置名称",
        "ja": "ALIYUN::ECS::VPC に VpcName が設定されていることを確認します",
        "de": "Prüft, ob VpcName für ALIYUN::ECS::VPC konfiguriert ist",
        "es": "Comprueba que ALIYUN::ECS::VPC tenga VpcName configurado",
        "fr": "Vérifie que ALIYUN::ECS::VPC a VpcName configuré",
        "pt": "Verifica se ALIYUN::ECS::VPC tem VpcName configurado"
    },
    "reason": {
        "en": "VPC must configure name is not satisfied.",
        "zh": "VPC 必须配置名称未满足。",
        "ja": "ALIYUN::ECS::VPC に VpcName が設定されていません。",
        "de": "Für ALIYUN::ECS::VPC ist VpcName nicht konfiguriert.",
        "es": "ALIYUN::ECS::VPC no tiene VpcName configurado.",
        "fr": "ALIYUN::ECS::VPC n'a pas VpcName configuré.",
        "pt": "ALIYUN::ECS::VPC não tem VpcName configurado."
    },
    "recommendation": {
        "en": "Configure VpcName on ALIYUN::ECS::VPC to satisfy the policy.",
        "zh": "请在 ALIYUN::ECS::VPC 上配置 VpcName 以满足策略。",
        "ja": "ポリシーを満たすには、ALIYUN::ECS::VPC に VpcName を設定してください。",
        "de": "Konfigurieren Sie VpcName für ALIYUN::ECS::VPC, um die Richtlinie zu erfüllen.",
        "es": "Configure VpcName en ALIYUN::ECS::VPC para cumplir la política.",
        "fr": "Configurez VpcName sur ALIYUN::ECS::VPC pour satisfaire la politique.",
        "pt": "Configure VpcName em ALIYUN::ECS::VPC para atender à política."
    },
    "resource_types": ["ALIYUN::ECS::VPC"]
}

deny contains result if {
    some name, resource in helpers.resources_by_type("ALIYUN::ECS::VPC")
    not is_compliant(resource)
    result := {
        "id": rule_meta.id,
        "resource_id": name,
        "violation_path": ["Properties", "VpcName"],
        "meta": {
            "severity": rule_meta.severity,
            "reason": rule_meta.reason,
            "recommendation": rule_meta.recommendation,
        },
    }
}

is_compliant(resource) if {
    helpers.has_property(resource, "VpcName")
}
