package infraguard.rules.aliyun.nat_gateway_spec_required

import rego.v1
import data.infraguard.helpers

rule_meta := {
    "id": "nat-gateway-spec-required",
    "severity": "medium",
    "name": {
        "en": "NAT Gateway must set specification",
        "zh": "NAT 网关必须设置规格",
        "ja": "ALIYUN::VPC::NatGateway には NatGatewaySpec を設定する必要があります",
        "de": "Für ALIYUN::VPC::NatGateway muss NatGatewaySpec konfiguriert sein",
        "es": "ALIYUN::VPC::NatGateway debe tener NatGatewaySpec configurado",
        "fr": "ALIYUN::VPC::NatGateway doit avoir NatGatewaySpec configuré",
        "pt": "ALIYUN::VPC::NatGateway deve ter NatGatewaySpec configurado"
    },
    "description": {
        "en": "Checks NAT Gateway must set specification",
        "zh": "检查NAT 网关必须设置规格",
        "ja": "ALIYUN::VPC::NatGateway に NatGatewaySpec が設定されていることを確認します",
        "de": "Prüft, ob NatGatewaySpec für ALIYUN::VPC::NatGateway konfiguriert ist",
        "es": "Comprueba que ALIYUN::VPC::NatGateway tenga NatGatewaySpec configurado",
        "fr": "Vérifie que ALIYUN::VPC::NatGateway a NatGatewaySpec configuré",
        "pt": "Verifica se ALIYUN::VPC::NatGateway tem NatGatewaySpec configurado"
    },
    "reason": {
        "en": "NAT Gateway must set specification is not satisfied.",
        "zh": "NAT 网关必须设置规格未满足。",
        "ja": "ALIYUN::VPC::NatGateway に NatGatewaySpec が設定されていません。",
        "de": "Für ALIYUN::VPC::NatGateway ist NatGatewaySpec nicht konfiguriert.",
        "es": "ALIYUN::VPC::NatGateway no tiene NatGatewaySpec configurado.",
        "fr": "ALIYUN::VPC::NatGateway n'a pas NatGatewaySpec configuré.",
        "pt": "ALIYUN::VPC::NatGateway não tem NatGatewaySpec configurado."
    },
    "recommendation": {
        "en": "Configure NatGatewaySpec on ALIYUN::VPC::NatGateway to satisfy the policy.",
        "zh": "请在 ALIYUN::VPC::NatGateway 上配置 NatGatewaySpec 以满足策略。",
        "ja": "ポリシーを満たすには、ALIYUN::VPC::NatGateway に NatGatewaySpec を設定してください。",
        "de": "Konfigurieren Sie NatGatewaySpec für ALIYUN::VPC::NatGateway, um die Richtlinie zu erfüllen.",
        "es": "Configure NatGatewaySpec en ALIYUN::VPC::NatGateway para cumplir la política.",
        "fr": "Configurez NatGatewaySpec sur ALIYUN::VPC::NatGateway pour satisfaire la politique.",
        "pt": "Configure NatGatewaySpec em ALIYUN::VPC::NatGateway para atender à política."
    },
    "resource_types": ["ALIYUN::VPC::NatGateway"]
}

deny contains result if {
    some name, resource in helpers.resources_by_type("ALIYUN::VPC::NatGateway")
    not is_compliant(resource)
    result := {
        "id": rule_meta.id,
        "resource_id": name,
        "violation_path": ["Properties", "NatGatewaySpec"],
        "meta": {
            "severity": rule_meta.severity,
            "reason": rule_meta.reason,
            "recommendation": rule_meta.recommendation,
        },
    }
}

is_compliant(resource) if {
    helpers.has_property(resource, "NatGatewaySpec")
}
