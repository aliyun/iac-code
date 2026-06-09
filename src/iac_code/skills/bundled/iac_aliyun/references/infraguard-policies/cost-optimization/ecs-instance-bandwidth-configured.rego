package infraguard.rules.aliyun.ecs_instance_bandwidth_configured

import rego.v1
import data.infraguard.helpers

rule_meta := {
    "id": "ecs-instance-bandwidth-configured",
    "severity": "medium",
    "name": {
        "en": "ECS instance must configure outbound bandwidth",
        "zh": "ECS 实例必须配置出网带宽",
        "ja": "ALIYUN::ECS::Instance には InternetMaxBandwidthOut を設定する必要があります",
        "de": "Für ALIYUN::ECS::Instance muss InternetMaxBandwidthOut konfiguriert sein",
        "es": "ALIYUN::ECS::Instance debe tener InternetMaxBandwidthOut configurado",
        "fr": "ALIYUN::ECS::Instance doit avoir InternetMaxBandwidthOut configuré",
        "pt": "ALIYUN::ECS::Instance deve ter InternetMaxBandwidthOut configurado"
    },
    "description": {
        "en": "Checks ECS instance must configure outbound bandwidth",
        "zh": "检查ECS 实例必须配置出网带宽",
        "ja": "ALIYUN::ECS::Instance に InternetMaxBandwidthOut が設定されていることを確認します",
        "de": "Prüft, ob InternetMaxBandwidthOut für ALIYUN::ECS::Instance konfiguriert ist",
        "es": "Comprueba que ALIYUN::ECS::Instance tenga InternetMaxBandwidthOut configurado",
        "fr": "Vérifie que ALIYUN::ECS::Instance a InternetMaxBandwidthOut configuré",
        "pt": "Verifica se ALIYUN::ECS::Instance tem InternetMaxBandwidthOut configurado"
    },
    "reason": {
        "en": "ECS instance must configure outbound bandwidth is not satisfied.",
        "zh": "ECS 实例必须配置出网带宽未满足。",
        "ja": "ALIYUN::ECS::Instance に InternetMaxBandwidthOut が設定されていません。",
        "de": "Für ALIYUN::ECS::Instance ist InternetMaxBandwidthOut nicht konfiguriert.",
        "es": "ALIYUN::ECS::Instance no tiene InternetMaxBandwidthOut configurado.",
        "fr": "ALIYUN::ECS::Instance n'a pas InternetMaxBandwidthOut configuré.",
        "pt": "ALIYUN::ECS::Instance não tem InternetMaxBandwidthOut configurado."
    },
    "recommendation": {
        "en": "Configure InternetMaxBandwidthOut on ALIYUN::ECS::Instance to satisfy the policy.",
        "zh": "请在 ALIYUN::ECS::Instance 上配置 InternetMaxBandwidthOut 以满足策略。",
        "ja": "ポリシーを満たすには、ALIYUN::ECS::Instance に InternetMaxBandwidthOut を設定してください。",
        "de": "Konfigurieren Sie InternetMaxBandwidthOut für ALIYUN::ECS::Instance, um die Richtlinie zu erfüllen.",
        "es": "Configure InternetMaxBandwidthOut en ALIYUN::ECS::Instance para cumplir la política.",
        "fr": "Configurez InternetMaxBandwidthOut sur ALIYUN::ECS::Instance pour satisfaire la politique.",
        "pt": "Configure InternetMaxBandwidthOut em ALIYUN::ECS::Instance para atender à política."
    },
    "resource_types": ["ALIYUN::ECS::Instance"]
}

deny contains result if {
    some name, resource in helpers.resources_by_type("ALIYUN::ECS::Instance")
    not is_compliant(resource)
    result := {
        "id": rule_meta.id,
        "resource_id": name,
        "violation_path": ["Properties", "InternetMaxBandwidthOut"],
        "meta": {
            "severity": rule_meta.severity,
            "reason": rule_meta.reason,
            "recommendation": rule_meta.recommendation,
        },
    }
}

is_compliant(resource) if {
    helpers.has_property(resource, "InternetMaxBandwidthOut")
}
