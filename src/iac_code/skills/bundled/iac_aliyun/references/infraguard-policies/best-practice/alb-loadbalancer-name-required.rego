package infraguard.rules.aliyun.alb_loadbalancer_name_required

import rego.v1
import data.infraguard.helpers

rule_meta := {
    "id": "alb-loadbalancer-name-required",
    "severity": "medium",
    "name": {
        "en": "ALB must configure name",
        "zh": "ALB 必须配置名称",
        "ja": "ALB 必须配置名称",
        "de": "ALB 必须配置名称",
        "es": "ALB 必须配置名称",
        "fr": "ALB 必须配置名称",
        "pt": "ALB 必须配置名称"
    },
    "description": {
        "en": "Checks ALB must configure name",
        "zh": "检查ALB 必须配置名称",
        "ja": "检查ALB 必须配置名称",
        "de": "检查ALB 必须配置名称",
        "es": "检查ALB 必须配置名称",
        "fr": "检查ALB 必须配置名称",
        "pt": "检查ALB 必须配置名称"
    },
    "reason": {
        "en": "ALB must configure name is not satisfied.",
        "zh": "ALB 必须配置名称未满足。",
        "ja": "ALB 必须配置名称未满足。",
        "de": "ALB 必须配置名称未满足。",
        "es": "ALB 必须配置名称未满足。",
        "fr": "ALB 必须配置名称未满足。",
        "pt": "ALB 必须配置名称未满足。"
    },
    "recommendation": {
        "en": "Configure LoadBalancerName on ALIYUN::ALB::LoadBalancer to satisfy the policy.",
        "zh": "请在 ALIYUN::ALB::LoadBalancer 上配置 LoadBalancerName 以满足策略。",
        "ja": "请在 ALIYUN::ALB::LoadBalancer 上配置 LoadBalancerName 以满足策略。",
        "de": "请在 ALIYUN::ALB::LoadBalancer 上配置 LoadBalancerName 以满足策略。",
        "es": "请在 ALIYUN::ALB::LoadBalancer 上配置 LoadBalancerName 以满足策略。",
        "fr": "请在 ALIYUN::ALB::LoadBalancer 上配置 LoadBalancerName 以满足策略。",
        "pt": "请在 ALIYUN::ALB::LoadBalancer 上配置 LoadBalancerName 以满足策略。"
    },
    "resource_types": ["ALIYUN::ALB::LoadBalancer"]
}

deny contains result if {
    some name, resource in helpers.resources_by_type("ALIYUN::ALB::LoadBalancer")
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
