package infraguard.rules.aliyun.nlb_address_type_intranet

import rego.v1
import data.infraguard.helpers

rule_meta := {
    "id": "nlb-address-type-intranet",
    "severity": "medium",
    "name": {
        "en": "NLB should use intranet address type",
        "zh": "NLB 应使用内网地址类型",
        "ja": "NLB 应使用内网地址类型",
        "de": "NLB 应使用内网地址类型",
        "es": "NLB 应使用内网地址类型",
        "fr": "NLB 应使用内网地址类型",
        "pt": "NLB 应使用内网地址类型"
    },
    "description": {
        "en": "Checks NLB should use intranet address type",
        "zh": "检查NLB 应使用内网地址类型",
        "ja": "检查NLB 应使用内网地址类型",
        "de": "检查NLB 应使用内网地址类型",
        "es": "检查NLB 应使用内网地址类型",
        "fr": "检查NLB 应使用内网地址类型",
        "pt": "检查NLB 应使用内网地址类型"
    },
    "reason": {
        "en": "NLB should use intranet address type is not satisfied.",
        "zh": "NLB 应使用内网地址类型未满足。",
        "ja": "NLB 应使用内网地址类型未满足。",
        "de": "NLB 应使用内网地址类型未满足。",
        "es": "NLB 应使用内网地址类型未满足。",
        "fr": "NLB 应使用内网地址类型未满足。",
        "pt": "NLB 应使用内网地址类型未满足。"
    },
    "recommendation": {
        "en": "Configure AddressType on ALIYUN::NLB::LoadBalancer to satisfy the policy.",
        "zh": "请在 ALIYUN::NLB::LoadBalancer 上配置 AddressType 以满足策略。",
        "ja": "请在 ALIYUN::NLB::LoadBalancer 上配置 AddressType 以满足策略。",
        "de": "请在 ALIYUN::NLB::LoadBalancer 上配置 AddressType 以满足策略。",
        "es": "请在 ALIYUN::NLB::LoadBalancer 上配置 AddressType 以满足策略。",
        "fr": "请在 ALIYUN::NLB::LoadBalancer 上配置 AddressType 以满足策略。",
        "pt": "请在 ALIYUN::NLB::LoadBalancer 上配置 AddressType 以满足策略。"
    },
    "resource_types": ["ALIYUN::NLB::LoadBalancer"]
}

deny contains result if {
    some name, resource in helpers.resources_by_type("ALIYUN::NLB::LoadBalancer")
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
    helpers.get_property(resource, "AddressType", "") == "Intranet"
}
