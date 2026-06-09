package infraguard.rules.aliyun.nat_gateway_vpc_required

import rego.v1
import data.infraguard.helpers

rule_meta := {
    "id": "nat-gateway-vpc-required",
    "severity": "high",
    "name": {
        "en": "NAT Gateway must bind VPC",
        "zh": "NAT 网关必须绑定 VPC",
        "ja": "NAT 网关必须绑定 VPC",
        "de": "NAT 网关必须绑定 VPC",
        "es": "NAT 网关必须绑定 VPC",
        "fr": "NAT 网关必须绑定 VPC",
        "pt": "NAT 网关必须绑定 VPC"
    },
    "description": {
        "en": "Checks NAT Gateway must bind VPC",
        "zh": "检查NAT 网关必须绑定 VPC",
        "ja": "检查NAT 网关必须绑定 VPC",
        "de": "检查NAT 网关必须绑定 VPC",
        "es": "检查NAT 网关必须绑定 VPC",
        "fr": "检查NAT 网关必须绑定 VPC",
        "pt": "检查NAT 网关必须绑定 VPC"
    },
    "reason": {
        "en": "NAT Gateway must bind VPC is not satisfied.",
        "zh": "NAT 网关必须绑定 VPC未满足。",
        "ja": "NAT 网关必须绑定 VPC未满足。",
        "de": "NAT 网关必须绑定 VPC未满足。",
        "es": "NAT 网关必须绑定 VPC未满足。",
        "fr": "NAT 网关必须绑定 VPC未满足。",
        "pt": "NAT 网关必须绑定 VPC未满足。"
    },
    "recommendation": {
        "en": "Configure VpcId on ALIYUN::VPC::NatGateway to satisfy the policy.",
        "zh": "请在 ALIYUN::VPC::NatGateway 上配置 VpcId 以满足策略。",
        "ja": "请在 ALIYUN::VPC::NatGateway 上配置 VpcId 以满足策略。",
        "de": "请在 ALIYUN::VPC::NatGateway 上配置 VpcId 以满足策略。",
        "es": "请在 ALIYUN::VPC::NatGateway 上配置 VpcId 以满足策略。",
        "fr": "请在 ALIYUN::VPC::NatGateway 上配置 VpcId 以满足策略。",
        "pt": "请在 ALIYUN::VPC::NatGateway 上配置 VpcId 以满足策略。"
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
