package infraguard.rules.aliyun.nat_gateway_vpc_required

import rego.v1
import data.infraguard.helpers

rule_meta := {
    "id": "nat-gateway-vpc-required",
    "severity": "high",
    "name": {
        "en": "NAT Gateway must bind VPC",
        "zh": "NAT 网关必须绑定 VPC",
        "ja": "ALIYUN::VPC::NatGateway には VpcId を設定する必要があります",
        "de": "Für ALIYUN::VPC::NatGateway muss VpcId konfiguriert sein",
        "es": "ALIYUN::VPC::NatGateway debe tener VpcId configurado",
        "fr": "ALIYUN::VPC::NatGateway doit avoir VpcId configuré",
        "pt": "ALIYUN::VPC::NatGateway deve ter VpcId configurado"
    },
    "description": {
        "en": "Checks NAT Gateway must bind VPC",
        "zh": "检查NAT 网关必须绑定 VPC",
        "ja": "ALIYUN::VPC::NatGateway に VpcId が設定されていることを確認します",
        "de": "Prüft, ob VpcId für ALIYUN::VPC::NatGateway konfiguriert ist",
        "es": "Comprueba que ALIYUN::VPC::NatGateway tenga VpcId configurado",
        "fr": "Vérifie que ALIYUN::VPC::NatGateway a VpcId configuré",
        "pt": "Verifica se ALIYUN::VPC::NatGateway tem VpcId configurado"
    },
    "reason": {
        "en": "NAT Gateway must bind VPC is not satisfied.",
        "zh": "NAT 网关必须绑定 VPC未满足。",
        "ja": "ALIYUN::VPC::NatGateway に VpcId が設定されていません。",
        "de": "Für ALIYUN::VPC::NatGateway ist VpcId nicht konfiguriert.",
        "es": "ALIYUN::VPC::NatGateway no tiene VpcId configurado.",
        "fr": "ALIYUN::VPC::NatGateway n'a pas VpcId configuré.",
        "pt": "ALIYUN::VPC::NatGateway não tem VpcId configurado."
    },
    "recommendation": {
        "en": "Configure VpcId on ALIYUN::VPC::NatGateway to satisfy the policy.",
        "zh": "请在 ALIYUN::VPC::NatGateway 上配置 VpcId 以满足策略。",
        "ja": "ポリシーを満たすには、ALIYUN::VPC::NatGateway に VpcId を設定してください。",
        "de": "Konfigurieren Sie VpcId für ALIYUN::VPC::NatGateway, um die Richtlinie zu erfüllen.",
        "es": "Configure VpcId en ALIYUN::VPC::NatGateway para cumplir la política.",
        "fr": "Configurez VpcId sur ALIYUN::VPC::NatGateway pour satisfaire la politique.",
        "pt": "Configure VpcId em ALIYUN::VPC::NatGateway para atender à política."
    },
    "resource_types": ["ALIYUN::VPC::NatGateway"]
}

deny contains result if {
    some name, resource in helpers.resources_by_type("ALIYUN::VPC::NatGateway")
    not is_compliant(resource)
    result := {
        "id": rule_meta.id,
        "resource_id": name,
        "violation_path": ["Properties", "VpcId"],
        "meta": {
            "severity": rule_meta.severity,
            "reason": rule_meta.reason,
            "recommendation": rule_meta.recommendation,
        },
    }
}

is_compliant(resource) if {
    helpers.has_property(resource, "VpcId")
}
