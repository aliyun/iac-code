package infraguard.rules.aliyun.transit_router_vpc_attachment_multi_zone

import rego.v1
import data.infraguard.helpers

rule_meta := {
    "id": "transit-router-vpc-attachment-multi-zone",
    "severity": "high",
    "name": {
        "en": "Transit router VPC attachment must configure zone mapping",
        "zh": "转发路由器 VPC 连接必须配置可用区映射",
        "ja": "ALIYUN::CEN::TransitRouterVpcAttachment には ZoneMappings を設定する必要があります",
        "de": "Für ALIYUN::CEN::TransitRouterVpcAttachment muss ZoneMappings konfiguriert sein",
        "es": "ALIYUN::CEN::TransitRouterVpcAttachment debe tener ZoneMappings configurado",
        "fr": "ALIYUN::CEN::TransitRouterVpcAttachment doit avoir ZoneMappings configuré",
        "pt": "ALIYUN::CEN::TransitRouterVpcAttachment deve ter ZoneMappings configurado"
    },
    "description": {
        "en": "Checks Transit router VPC attachment must configure zone mapping",
        "zh": "检查转发路由器 VPC 连接必须配置可用区映射",
        "ja": "ALIYUN::CEN::TransitRouterVpcAttachment に ZoneMappings が設定されていることを確認します",
        "de": "Prüft, ob ZoneMappings für ALIYUN::CEN::TransitRouterVpcAttachment konfiguriert ist",
        "es": "Comprueba que ALIYUN::CEN::TransitRouterVpcAttachment tenga ZoneMappings configurado",
        "fr": "Vérifie que ALIYUN::CEN::TransitRouterVpcAttachment a ZoneMappings configuré",
        "pt": "Verifica se ALIYUN::CEN::TransitRouterVpcAttachment tem ZoneMappings configurado"
    },
    "reason": {
        "en": "Transit router VPC attachment must configure zone mapping is not satisfied.",
        "zh": "转发路由器 VPC 连接必须配置可用区映射未满足。",
        "ja": "ALIYUN::CEN::TransitRouterVpcAttachment に ZoneMappings が設定されていません。",
        "de": "Für ALIYUN::CEN::TransitRouterVpcAttachment ist ZoneMappings nicht konfiguriert.",
        "es": "ALIYUN::CEN::TransitRouterVpcAttachment no tiene ZoneMappings configurado.",
        "fr": "ALIYUN::CEN::TransitRouterVpcAttachment n'a pas ZoneMappings configuré.",
        "pt": "ALIYUN::CEN::TransitRouterVpcAttachment não tem ZoneMappings configurado."
    },
    "recommendation": {
        "en": "Configure ZoneMappings on ALIYUN::CEN::TransitRouterVpcAttachment to satisfy the policy.",
        "zh": "请在 ALIYUN::CEN::TransitRouterVpcAttachment 上配置 ZoneMappings 以满足策略。",
        "ja": "ポリシーを満たすには、ALIYUN::CEN::TransitRouterVpcAttachment に ZoneMappings を設定してください。",
        "de": "Konfigurieren Sie ZoneMappings für ALIYUN::CEN::TransitRouterVpcAttachment, um die Richtlinie zu erfüllen.",
        "es": "Configure ZoneMappings en ALIYUN::CEN::TransitRouterVpcAttachment para cumplir la política.",
        "fr": "Configurez ZoneMappings sur ALIYUN::CEN::TransitRouterVpcAttachment pour satisfaire la politique.",
        "pt": "Configure ZoneMappings em ALIYUN::CEN::TransitRouterVpcAttachment para atender à política."
    },
    "resource_types": ["ALIYUN::CEN::TransitRouterVpcAttachment"]
}

deny contains result if {
    some name, resource in helpers.resources_by_type("ALIYUN::CEN::TransitRouterVpcAttachment")
    not is_compliant(resource)
    result := {
        "id": rule_meta.id,
        "resource_id": name,
        "violation_path": ["Properties", "ZoneMappings"],
        "meta": {
            "severity": rule_meta.severity,
            "reason": rule_meta.reason,
            "recommendation": rule_meta.recommendation,
        },
    }
}

is_compliant(resource) if {
    helpers.has_property(resource, "ZoneMappings")
}
