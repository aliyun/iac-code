package infraguard.rules.aliyun.eip_bandwidth_required

import rego.v1
import data.infraguard.helpers

rule_meta := {
    "id": "eip-bandwidth-required",
    "severity": "medium",
    "name": {
        "en": "EIP must set bandwidth",
        "zh": "EIP 必须设置带宽",
        "ja": "EIP 必须设置带宽",
        "de": "EIP 必须设置带宽",
        "es": "EIP 必须设置带宽",
        "fr": "EIP 必须设置带宽",
        "pt": "EIP 必须设置带宽"
    },
    "description": {
        "en": "Checks EIP must set bandwidth",
        "zh": "检查EIP 必须设置带宽",
        "ja": "检查EIP 必须设置带宽",
        "de": "检查EIP 必须设置带宽",
        "es": "检查EIP 必须设置带宽",
        "fr": "检查EIP 必须设置带宽",
        "pt": "检查EIP 必须设置带宽"
    },
    "reason": {
        "en": "EIP must set bandwidth is not satisfied.",
        "zh": "EIP 必须设置带宽未满足。",
        "ja": "EIP 必须设置带宽未满足。",
        "de": "EIP 必须设置带宽未满足。",
        "es": "EIP 必须设置带宽未满足。",
        "fr": "EIP 必须设置带宽未满足。",
        "pt": "EIP 必须设置带宽未满足。"
    },
    "recommendation": {
        "en": "Configure Bandwidth on ALIYUN::VPC::EIP to satisfy the policy.",
        "zh": "请在 ALIYUN::VPC::EIP 上配置 Bandwidth 以满足策略。",
        "ja": "请在 ALIYUN::VPC::EIP 上配置 Bandwidth 以满足策略。",
        "de": "请在 ALIYUN::VPC::EIP 上配置 Bandwidth 以满足策略。",
        "es": "请在 ALIYUN::VPC::EIP 上配置 Bandwidth 以满足策略。",
        "fr": "请在 ALIYUN::VPC::EIP 上配置 Bandwidth 以满足策略。",
        "pt": "请在 ALIYUN::VPC::EIP 上配置 Bandwidth 以满足策略。"
    },
    "resource_types": ["ALIYUN::VPC::EIP"]
}

deny contains result if {
    some name, resource in helpers.resources_by_type("ALIYUN::VPC::EIP")
    not is_compliant(resource)
    result := {
        "id": rule_meta.id,
        "resource_id": name,
        "violation_path": ["Properties", "Bandwidth"],
        "meta": {
            "severity": rule_meta.severity,
            "reason": rule_meta.reason,
            "recommendation": rule_meta.recommendation,
        },
    }
}

is_compliant(resource) if {
    helpers.has_property(resource, "Bandwidth")
}
