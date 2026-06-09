package infraguard.rules.aliyun.slb_loadbalancer_name_required

import rego.v1
import data.infraguard.helpers

rule_meta := {
    "id": "slb-loadbalancer-name-required",
    "severity": "medium",
    "name": {
        "en": "SLB must configure name",
        "zh": "SLB 必须配置名称",
        "ja": "SLB 必须配置名称",
        "de": "SLB 必须配置名称",
        "es": "SLB 必须配置名称",
        "fr": "SLB 必须配置名称",
        "pt": "SLB 必须配置名称"
    },
    "description": {
        "en": "Checks SLB must configure name",
        "zh": "检查SLB 必须配置名称",
        "ja": "检查SLB 必须配置名称",
        "de": "检查SLB 必须配置名称",
        "es": "检查SLB 必须配置名称",
        "fr": "检查SLB 必须配置名称",
        "pt": "检查SLB 必须配置名称"
    },
    "reason": {
        "en": "SLB must configure name is not satisfied.",
        "zh": "SLB 必须配置名称未满足。",
        "ja": "SLB 必须配置名称未满足。",
        "de": "SLB 必须配置名称未满足。",
        "es": "SLB 必须配置名称未满足。",
        "fr": "SLB 必须配置名称未满足。",
        "pt": "SLB 必须配置名称未满足。"
    },
    "recommendation": {
        "en": "Configure LoadBalancerName on ALIYUN::SLB::LoadBalancer to satisfy the policy.",
        "zh": "请在 ALIYUN::SLB::LoadBalancer 上配置 LoadBalancerName 以满足策略。",
        "ja": "请在 ALIYUN::SLB::LoadBalancer 上配置 LoadBalancerName 以满足策略。",
        "de": "请在 ALIYUN::SLB::LoadBalancer 上配置 LoadBalancerName 以满足策略。",
        "es": "请在 ALIYUN::SLB::LoadBalancer 上配置 LoadBalancerName 以满足策略。",
        "fr": "请在 ALIYUN::SLB::LoadBalancer 上配置 LoadBalancerName 以满足策略。",
        "pt": "请在 ALIYUN::SLB::LoadBalancer 上配置 LoadBalancerName 以满足策略。"
    },
    "resource_types": ["ALIYUN::SLB::LoadBalancer"]
}

deny contains result if {
    some name, resource in helpers.resources_by_type("ALIYUN::SLB::LoadBalancer")
    not is_compliant(resource)
    result := {
        "id": rule_meta.id,
        "resource_id": name,
        "violation_path": ["Properties", "LoadBalancerName"],
        "meta": {
            "severity": rule_meta.severity,
            "reason": rule_meta.reason,
            "recommendation": rule_meta.recommendation,
        },
    }
}

is_compliant(resource) if {
    helpers.has_property(resource, "LoadBalancerName")
}
