package infraguard.rules.aliyun.cms_alarm_name_required

import rego.v1
import data.infraguard.helpers

rule_meta := {
    "id": "cms-alarm-name-required",
    "severity": "medium",
    "name": {
        "en": "CMS alarm must configure name",
        "zh": "云监控告警必须配置名称",
        "ja": "云监控告警必须配置名称",
        "de": "云监控告警必须配置名称",
        "es": "云监控告警必须配置名称",
        "fr": "云监控告警必须配置名称",
        "pt": "云监控告警必须配置名称"
    },
    "description": {
        "en": "Checks CMS alarm must configure name",
        "zh": "检查云监控告警必须配置名称",
        "ja": "检查云监控告警必须配置名称",
        "de": "检查云监控告警必须配置名称",
        "es": "检查云监控告警必须配置名称",
        "fr": "检查云监控告警必须配置名称",
        "pt": "检查云监控告警必须配置名称"
    },
    "reason": {
        "en": "CMS alarm must configure name is not satisfied.",
        "zh": "云监控告警必须配置名称未满足。",
        "ja": "云监控告警必须配置名称未满足。",
        "de": "云监控告警必须配置名称未满足。",
        "es": "云监控告警必须配置名称未满足。",
        "fr": "云监控告警必须配置名称未满足。",
        "pt": "云监控告警必须配置名称未满足。"
    },
    "recommendation": {
        "en": "Configure Name on ALIYUN::CMS::Alarm to satisfy the policy.",
        "zh": "请在 ALIYUN::CMS::Alarm 上配置 Name 以满足策略。",
        "ja": "请在 ALIYUN::CMS::Alarm 上配置 Name 以满足策略。",
        "de": "请在 ALIYUN::CMS::Alarm 上配置 Name 以满足策略。",
        "es": "请在 ALIYUN::CMS::Alarm 上配置 Name 以满足策略。",
        "fr": "请在 ALIYUN::CMS::Alarm 上配置 Name 以满足策略。",
        "pt": "请在 ALIYUN::CMS::Alarm 上配置 Name 以满足策略。"
    },
    "resource_types": ["ALIYUN::CMS::Alarm"]
}

deny contains result if {
    some name, resource in helpers.resources_by_type("ALIYUN::CMS::Alarm")
    not is_compliant(resource)
    result := {
        "id": rule_meta.id,
        "resource_id": name,
        "violation_path": ["Properties", "Name"],
        "meta": {
            "severity": rule_meta.severity,
            "reason": rule_meta.reason,
            "recommendation": rule_meta.recommendation,
        },
    }
}

is_compliant(resource) if {
    helpers.has_property(resource, "Name")
}
