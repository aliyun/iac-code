package infraguard.rules.aliyun.alb_address_type_intranet

import rego.v1
import data.infraguard.helpers

rule_meta := {
    "id": "alb-address-type-intranet",
    "severity": "medium",
    "name": {
        "en": "ALB should use intranet address type",
        "zh": "ALB 应使用内网地址类型",
        "ja": "ALB 应使用内网地址类型",
        "de": "ALB 应使用内网地址类型",
        "es": "ALB 应使用内网地址类型",
        "fr": "ALB 应使用内网地址类型",
        "pt": "ALB 应使用内网地址类型"
    },
    "description": {
        "en": "Checks ALB should use intranet address type",
        "zh": "检查ALB 应使用内网地址类型",
        "ja": "检查ALB 应使用内网地址类型",
        "de": "检查ALB 应使用内网地址类型",
        "es": "检查ALB 应使用内网地址类型",
        "fr": "检查ALB 应使用内网地址类型",
        "pt": "检查ALB 应使用内网地址类型"
    },
    "reason": {
        "en": "ALB should use intranet address type is not satisfied.",
        "zh": "ALB 应使用内网地址类型未满足。",
        "ja": "ALB 应使用内网地址类型未满足。",
        "de": "ALB 应使用内网地址类型未满足。",
        "es": "ALB 应使用内网地址类型未满足。",
        "fr": "ALB 应使用内网地址类型未满足。",
        "pt": "ALB 应使用内网地址类型未满足。"
    },
    "recommendation": {
        "en": "Configure AddressType on ALIYUN::ALB::LoadBalancer to satisfy the policy.",
        "zh": "请在 ALIYUN::ALB::LoadBalancer 上配置 AddressType 以满足策略。",
        "ja": "请在 ALIYUN::ALB::LoadBalancer 上配置 AddressType 以满足策略。",
        "de": "请在 ALIYUN::ALB::LoadBalancer 上配置 AddressType 以满足策略。",
        "es": "请在 ALIYUN::ALB::LoadBalancer 上配置 AddressType 以满足策略。",
        "fr": "请在 ALIYUN::ALB::LoadBalancer 上配置 AddressType 以满足策略。",
        "pt": "请在 ALIYUN::ALB::LoadBalancer 上配置 AddressType 以满足策略。"
    },
    "resource_types": ["ALIYUN::ALB::LoadBalancer"]
}

deny contains result if {
    some name, resource in helpers.resources_by_type("ALIYUN::ALB::LoadBalancer")
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
