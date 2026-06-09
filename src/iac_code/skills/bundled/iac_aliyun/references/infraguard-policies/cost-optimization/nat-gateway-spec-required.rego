package infraguard.rules.aliyun.nat_gateway_spec_required

import rego.v1
import data.infraguard.helpers

rule_meta := {
    "id": "nat-gateway-spec-required",
    "severity": "medium",
    "name": {
        "en": "NAT Gateway must set specification",
        "zh": "NAT 网关必须设置规格",
        "ja": "NAT 网关必须设置规格",
        "de": "NAT 网关必须设置规格",
        "es": "NAT 网关必须设置规格",
        "fr": "NAT 网关必须设置规格",
        "pt": "NAT 网关必须设置规格"
    },
    "description": {
        "en": "Checks NAT Gateway must set specification",
        "zh": "检查NAT 网关必须设置规格",
        "ja": "检查NAT 网关必须设置规格",
        "de": "检查NAT 网关必须设置规格",
        "es": "检查NAT 网关必须设置规格",
        "fr": "检查NAT 网关必须设置规格",
        "pt": "检查NAT 网关必须设置规格"
    },
    "reason": {
        "en": "NAT Gateway must set specification is not satisfied.",
        "zh": "NAT 网关必须设置规格未满足。",
        "ja": "NAT 网关必须设置规格未满足。",
        "de": "NAT 网关必须设置规格未满足。",
        "es": "NAT 网关必须设置规格未满足。",
        "fr": "NAT 网关必须设置规格未满足。",
        "pt": "NAT 网关必须设置规格未满足。"
    },
    "recommendation": {
        "en": "Configure NatGatewaySpec on ALIYUN::VPC::NatGateway to satisfy the policy.",
        "zh": "请在 ALIYUN::VPC::NatGateway 上配置 NatGatewaySpec 以满足策略。",
        "ja": "请在 ALIYUN::VPC::NatGateway 上配置 NatGatewaySpec 以满足策略。",
        "de": "请在 ALIYUN::VPC::NatGateway 上配置 NatGatewaySpec 以满足策略。",
        "es": "请在 ALIYUN::VPC::NatGateway 上配置 NatGatewaySpec 以满足策略。",
        "fr": "请在 ALIYUN::VPC::NatGateway 上配置 NatGatewaySpec 以满足策略。",
        "pt": "请在 ALIYUN::VPC::NatGateway 上配置 NatGatewaySpec 以满足策略。"
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
