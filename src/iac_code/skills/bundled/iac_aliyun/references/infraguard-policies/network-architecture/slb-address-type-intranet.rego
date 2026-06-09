package infraguard.rules.aliyun.slb_address_type_intranet

import rego.v1
import data.infraguard.helpers

rule_meta := {
    "id": "slb-address-type-intranet",
    "severity": "medium",
    "name": {
        "en": "SLB should use intranet address type",
        "zh": "SLB 应使用内网地址类型",
        "ja": "SLB 应使用内网地址类型",
        "de": "SLB 应使用内网地址类型",
        "es": "SLB 应使用内网地址类型",
        "fr": "SLB 应使用内网地址类型",
        "pt": "SLB 应使用内网地址类型"
    },
    "description": {
        "en": "Checks SLB should use intranet address type",
        "zh": "检查SLB 应使用内网地址类型",
        "ja": "检查SLB 应使用内网地址类型",
        "de": "检查SLB 应使用内网地址类型",
        "es": "检查SLB 应使用内网地址类型",
        "fr": "检查SLB 应使用内网地址类型",
        "pt": "检查SLB 应使用内网地址类型"
    },
    "reason": {
        "en": "SLB should use intranet address type is not satisfied.",
        "zh": "SLB 应使用内网地址类型未满足。",
        "ja": "SLB 应使用内网地址类型未满足。",
        "de": "SLB 应使用内网地址类型未满足。",
        "es": "SLB 应使用内网地址类型未满足。",
        "fr": "SLB 应使用内网地址类型未满足。",
        "pt": "SLB 应使用内网地址类型未满足。"
    },
    "recommendation": {
        "en": "Configure AddressType on ALIYUN::SLB::LoadBalancer to satisfy the policy.",
        "zh": "请在 ALIYUN::SLB::LoadBalancer 上配置 AddressType 以满足策略。",
        "ja": "请在 ALIYUN::SLB::LoadBalancer 上配置 AddressType 以满足策略。",
        "de": "请在 ALIYUN::SLB::LoadBalancer 上配置 AddressType 以满足策略。",
        "es": "请在 ALIYUN::SLB::LoadBalancer 上配置 AddressType 以满足策略。",
        "fr": "请在 ALIYUN::SLB::LoadBalancer 上配置 AddressType 以满足策略。",
        "pt": "请在 ALIYUN::SLB::LoadBalancer 上配置 AddressType 以满足策略。"
    },
    "resource_types": ["ALIYUN::SLB::LoadBalancer"]
}

deny contains result if {
    some name, resource in helpers.resources_by_type("ALIYUN::SLB::LoadBalancer")
    not is_compliant(resource)
    result := {
        "id": rule_meta.id,
        "resource_id": name,
        "violation_path": ["Properties", "AddressType"],
        "meta": {
            "severity": rule_meta.severity,
            "reason": rule_meta.reason,
            "recommendation": rule_meta.recommendation,
        },
    }
}

is_compliant(resource) if {
    helpers.get_property(resource, "AddressType", "") == "intranet"
}
