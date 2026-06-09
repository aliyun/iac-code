package infraguard.rules.aliyun.vpn_gateway_vpc_required

import rego.v1
import data.infraguard.helpers

rule_meta := {
    "id": "vpn-gateway-vpc-required",
    "severity": "high",
    "name": {
        "en": "VPN Gateway must bind VPC",
        "zh": "VPN 网关必须绑定 VPC",
        "ja": "VPN 网关必须绑定 VPC",
        "de": "VPN 网关必须绑定 VPC",
        "es": "VPN 网关必须绑定 VPC",
        "fr": "VPN 网关必须绑定 VPC",
        "pt": "VPN 网关必须绑定 VPC"
    },
    "description": {
        "en": "Checks VPN Gateway must bind VPC",
        "zh": "检查VPN 网关必须绑定 VPC",
        "ja": "检查VPN 网关必须绑定 VPC",
        "de": "检查VPN 网关必须绑定 VPC",
        "es": "检查VPN 网关必须绑定 VPC",
        "fr": "检查VPN 网关必须绑定 VPC",
        "pt": "检查VPN 网关必须绑定 VPC"
    },
    "reason": {
        "en": "VPN Gateway must bind VPC is not satisfied.",
        "zh": "VPN 网关必须绑定 VPC未满足。",
        "ja": "VPN 网关必须绑定 VPC未满足。",
        "de": "VPN 网关必须绑定 VPC未满足。",
        "es": "VPN 网关必须绑定 VPC未满足。",
        "fr": "VPN 网关必须绑定 VPC未满足。",
        "pt": "VPN 网关必须绑定 VPC未满足。"
    },
    "recommendation": {
        "en": "Configure VpcId on ALIYUN::VPC::VpnGateway to satisfy the policy.",
        "zh": "请在 ALIYUN::VPC::VpnGateway 上配置 VpcId 以满足策略。",
        "ja": "请在 ALIYUN::VPC::VpnGateway 上配置 VpcId 以满足策略。",
        "de": "请在 ALIYUN::VPC::VpnGateway 上配置 VpcId 以满足策略。",
        "es": "请在 ALIYUN::VPC::VpnGateway 上配置 VpcId 以满足策略。",
        "fr": "请在 ALIYUN::VPC::VpnGateway 上配置 VpcId 以满足策略。",
        "pt": "请在 ALIYUN::VPC::VpnGateway 上配置 VpcId 以满足策略。"
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
