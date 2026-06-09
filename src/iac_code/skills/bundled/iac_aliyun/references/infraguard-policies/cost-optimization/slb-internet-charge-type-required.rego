package infraguard.rules.aliyun.slb_internet_charge_type_required

import rego.v1
import data.infraguard.helpers

rule_meta := {
    "id": "slb-internet-charge-type-required",
    "severity": "medium",
    "name": {
        "en": "SLB must set internet charge type",
        "zh": "SLB 必须设置公网计费类型",
        "ja": "SLB 必须设置公网计费类型",
        "de": "SLB 必须设置公网计费类型",
        "es": "SLB 必须设置公网计费类型",
        "fr": "SLB 必须设置公网计费类型",
        "pt": "SLB 必须设置公网计费类型"
    },
    "description": {
        "en": "Checks SLB must set internet charge type",
        "zh": "检查SLB 必须设置公网计费类型",
        "ja": "检查SLB 必须设置公网计费类型",
        "de": "检查SLB 必须设置公网计费类型",
        "es": "检查SLB 必须设置公网计费类型",
        "fr": "检查SLB 必须设置公网计费类型",
        "pt": "检查SLB 必须设置公网计费类型"
    },
    "reason": {
        "en": "SLB must set internet charge type is not satisfied.",
        "zh": "SLB 必须设置公网计费类型未满足。",
        "ja": "SLB 必须设置公网计费类型未满足。",
        "de": "SLB 必须设置公网计费类型未满足。",
        "es": "SLB 必须设置公网计费类型未满足。",
        "fr": "SLB 必须设置公网计费类型未满足。",
        "pt": "SLB 必须设置公网计费类型未满足。"
    },
    "recommendation": {
        "en": "Configure InternetChargeType on ALIYUN::SLB::LoadBalancer to satisfy the policy.",
        "zh": "请在 ALIYUN::SLB::LoadBalancer 上配置 InternetChargeType 以满足策略。",
        "ja": "请在 ALIYUN::SLB::LoadBalancer 上配置 InternetChargeType 以满足策略。",
        "de": "请在 ALIYUN::SLB::LoadBalancer 上配置 InternetChargeType 以满足策略。",
        "es": "请在 ALIYUN::SLB::LoadBalancer 上配置 InternetChargeType 以满足策略。",
        "fr": "请在 ALIYUN::SLB::LoadBalancer 上配置 InternetChargeType 以满足策略。",
        "pt": "请在 ALIYUN::SLB::LoadBalancer 上配置 InternetChargeType 以满足策略。"
    },
    "resource_types": ["ALIYUN::SLB::LoadBalancer"]
}

deny contains result if {
    some name, resource in helpers.resources_by_type("ALIYUN::SLB::LoadBalancer")
    not is_compliant(resource)
    result := {
        "id": rule_meta.id,
        "resource_id": name,
        "violation_path": ["Properties", "InternetChargeType"],
        "meta": {
            "severity": rule_meta.severity,
            "reason": rule_meta.reason,
            "recommendation": rule_meta.recommendation,
        },
    }
}

is_compliant(resource) if {
    helpers.has_property(resource, "InternetChargeType")
}
