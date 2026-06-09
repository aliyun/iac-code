package infraguard.rules.aliyun.cen_instance_name_required

import rego.v1
import data.infraguard.helpers

rule_meta := {
    "id": "cen-instance-name-required",
    "severity": "medium",
    "name": {
        "en": "CEN instance must configure name",
        "zh": "CEN 实例必须配置名称",
        "ja": "CEN 实例必须配置名称",
        "de": "CEN 实例必须配置名称",
        "es": "CEN 实例必须配置名称",
        "fr": "CEN 实例必须配置名称",
        "pt": "CEN 实例必须配置名称"
    },
    "description": {
        "en": "Checks CEN instance must configure name",
        "zh": "检查CEN 实例必须配置名称",
        "ja": "检查CEN 实例必须配置名称",
        "de": "检查CEN 实例必须配置名称",
        "es": "检查CEN 实例必须配置名称",
        "fr": "检查CEN 实例必须配置名称",
        "pt": "检查CEN 实例必须配置名称"
    },
    "reason": {
        "en": "CEN instance must configure name is not satisfied.",
        "zh": "CEN 实例必须配置名称未满足。",
        "ja": "CEN 实例必须配置名称未满足。",
        "de": "CEN 实例必须配置名称未满足。",
        "es": "CEN 实例必须配置名称未满足。",
        "fr": "CEN 实例必须配置名称未满足。",
        "pt": "CEN 实例必须配置名称未满足。"
    },
    "recommendation": {
        "en": "Configure Name on ALIYUN::CEN::CenInstance to satisfy the policy.",
        "zh": "请在 ALIYUN::CEN::CenInstance 上配置 Name 以满足策略。",
        "ja": "请在 ALIYUN::CEN::CenInstance 上配置 Name 以满足策略。",
        "de": "请在 ALIYUN::CEN::CenInstance 上配置 Name 以满足策略。",
        "es": "请在 ALIYUN::CEN::CenInstance 上配置 Name 以满足策略。",
        "fr": "请在 ALIYUN::CEN::CenInstance 上配置 Name 以满足策略。",
        "pt": "请在 ALIYUN::CEN::CenInstance 上配置 Name 以满足策略。"
    },
    "resource_types": ["ALIYUN::CEN::CenInstance"]
}

deny contains result if {
    some name, resource in helpers.resources_by_type("ALIYUN::CEN::CenInstance")
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
