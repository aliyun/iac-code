package infraguard.rules.aliyun.actiontrail_trail_name_required

import rego.v1
import data.infraguard.helpers

rule_meta := {
    "id": "actiontrail-trail-name-required",
    "severity": "medium",
    "name": {
        "en": "ActionTrail trail must configure name",
        "zh": "ActionTrail 跟踪必须配置名称",
        "ja": "ActionTrail 跟踪必须配置名称",
        "de": "ActionTrail 跟踪必须配置名称",
        "es": "ActionTrail 跟踪必须配置名称",
        "fr": "ActionTrail 跟踪必须配置名称",
        "pt": "ActionTrail 跟踪必须配置名称"
    },
    "description": {
        "en": "Checks ActionTrail trail must configure name",
        "zh": "检查ActionTrail 跟踪必须配置名称",
        "ja": "检查ActionTrail 跟踪必须配置名称",
        "de": "检查ActionTrail 跟踪必须配置名称",
        "es": "检查ActionTrail 跟踪必须配置名称",
        "fr": "检查ActionTrail 跟踪必须配置名称",
        "pt": "检查ActionTrail 跟踪必须配置名称"
    },
    "reason": {
        "en": "ActionTrail trail must configure name is not satisfied.",
        "zh": "ActionTrail 跟踪必须配置名称未满足。",
        "ja": "ActionTrail 跟踪必须配置名称未满足。",
        "de": "ActionTrail 跟踪必须配置名称未满足。",
        "es": "ActionTrail 跟踪必须配置名称未满足。",
        "fr": "ActionTrail 跟踪必须配置名称未满足。",
        "pt": "ActionTrail 跟踪必须配置名称未满足。"
    },
    "recommendation": {
        "en": "Configure TrailName on ALIYUN::ACTIONTRAIL::Trail to satisfy the policy.",
        "zh": "请在 ALIYUN::ACTIONTRAIL::Trail 上配置 TrailName 以满足策略。",
        "ja": "请在 ALIYUN::ACTIONTRAIL::Trail 上配置 TrailName 以满足策略。",
        "de": "请在 ALIYUN::ACTIONTRAIL::Trail 上配置 TrailName 以满足策略。",
        "es": "请在 ALIYUN::ACTIONTRAIL::Trail 上配置 TrailName 以满足策略。",
        "fr": "请在 ALIYUN::ACTIONTRAIL::Trail 上配置 TrailName 以满足策略。",
        "pt": "请在 ALIYUN::ACTIONTRAIL::Trail 上配置 TrailName 以满足策略。"
    },
    "resource_types": ["ALIYUN::ACTIONTRAIL::Trail"]
}

deny contains result if {
    some name, resource in helpers.resources_by_type("ALIYUN::ACTIONTRAIL::Trail")
    not is_compliant(resource)
    result := {
        "id": rule_meta.id,
        "resource_id": name,
        "violation_path": ["Properties", "TrailName"],
        "meta": {
            "severity": rule_meta.severity,
            "reason": rule_meta.reason,
            "recommendation": rule_meta.recommendation,
        },
    }
}

is_compliant(resource) if {
    helpers.has_property(resource, "TrailName")
}
