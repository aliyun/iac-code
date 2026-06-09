package infraguard.rules.aliyun.vpn_gateway_vpc_required

import rego.v1
import data.infraguard.helpers

rule_meta := {
    "id": "vpn-gateway-vpc-required",
    "severity": "high",
    "name": {
        "en": "VPN Gateway must bind VPC",
        "zh": "VPN 网关必须绑定 VPC",
        "ja": "ALIYUN::VPC::VpnGateway には VpcId を設定する必要があります",
        "de": "Für ALIYUN::VPC::VpnGateway muss VpcId konfiguriert sein",
        "es": "ALIYUN::VPC::VpnGateway debe tener VpcId configurado",
        "fr": "ALIYUN::VPC::VpnGateway doit avoir VpcId configuré",
        "pt": "ALIYUN::VPC::VpnGateway deve ter VpcId configurado"
    },
    "description": {
        "en": "Checks VPN Gateway must bind VPC",
        "zh": "检查VPN 网关必须绑定 VPC",
        "ja": "ALIYUN::VPC::VpnGateway に VpcId が設定されていることを確認します",
        "de": "Prüft, ob VpcId für ALIYUN::VPC::VpnGateway konfiguriert ist",
        "es": "Comprueba que ALIYUN::VPC::VpnGateway tenga VpcId configurado",
        "fr": "Vérifie que ALIYUN::VPC::VpnGateway a VpcId configuré",
        "pt": "Verifica se ALIYUN::VPC::VpnGateway tem VpcId configurado"
    },
    "reason": {
        "en": "VPN Gateway must bind VPC is not satisfied.",
        "zh": "VPN 网关必须绑定 VPC未满足。",
        "ja": "ALIYUN::VPC::VpnGateway に VpcId が設定されていません。",
        "de": "Für ALIYUN::VPC::VpnGateway ist VpcId nicht konfiguriert.",
        "es": "ALIYUN::VPC::VpnGateway no tiene VpcId configurado.",
        "fr": "ALIYUN::VPC::VpnGateway n'a pas VpcId configuré.",
        "pt": "ALIYUN::VPC::VpnGateway não tem VpcId configurado."
    },
    "recommendation": {
        "en": "Configure VpcId on ALIYUN::VPC::VpnGateway to satisfy the policy.",
        "zh": "请在 ALIYUN::VPC::VpnGateway 上配置 VpcId 以满足策略。",
        "ja": "ポリシーを満たすには、ALIYUN::VPC::VpnGateway に VpcId を設定してください。",
        "de": "Konfigurieren Sie VpcId für ALIYUN::VPC::VpnGateway, um die Richtlinie zu erfüllen.",
        "es": "Configure VpcId en ALIYUN::VPC::VpnGateway para cumplir la política.",
        "fr": "Configurez VpcId sur ALIYUN::VPC::VpnGateway pour satisfaire la politique.",
        "pt": "Configure VpcId em ALIYUN::VPC::VpnGateway para atender à política."
    },
    "resource_types": ["ALIYUN::VPC::VpnGateway"]
}

deny contains result if {
    some name, resource in helpers.resources_by_type("ALIYUN::VPC::VpnGateway")
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
