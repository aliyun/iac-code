package infraguard.rules.aliyun.rds_storage_type_required

import rego.v1
import data.infraguard.helpers

rule_meta := {
    "id": "rds-storage-type-required",
    "severity": "medium",
    "name": {
        "en": "RDS instance must set storage type",
        "zh": "RDS 实例必须设置存储类型",
        "ja": "RDS 实例必须设置存储类型",
        "de": "RDS 实例必须设置存储类型",
        "es": "RDS 实例必须设置存储类型",
        "fr": "RDS 实例必须设置存储类型",
        "pt": "RDS 实例必须设置存储类型"
    },
    "description": {
        "en": "Checks RDS instance must set storage type",
        "zh": "检查RDS 实例必须设置存储类型",
        "ja": "检查RDS 实例必须设置存储类型",
        "de": "检查RDS 实例必须设置存储类型",
        "es": "检查RDS 实例必须设置存储类型",
        "fr": "检查RDS 实例必须设置存储类型",
        "pt": "检查RDS 实例必须设置存储类型"
    },
    "reason": {
        "en": "RDS instance must set storage type is not satisfied.",
        "zh": "RDS 实例必须设置存储类型未满足。",
        "ja": "RDS 实例必须设置存储类型未满足。",
        "de": "RDS 实例必须设置存储类型未满足。",
        "es": "RDS 实例必须设置存储类型未满足。",
        "fr": "RDS 实例必须设置存储类型未满足。",
        "pt": "RDS 实例必须设置存储类型未满足。"
    },
    "recommendation": {
        "en": "Configure DBInstanceStorageType on ALIYUN::RDS::DBInstance to satisfy the policy.",
        "zh": "请在 ALIYUN::RDS::DBInstance 上配置 DBInstanceStorageType 以满足策略。",
        "ja": "请在 ALIYUN::RDS::DBInstance 上配置 DBInstanceStorageType 以满足策略。",
        "de": "请在 ALIYUN::RDS::DBInstance 上配置 DBInstanceStorageType 以满足策略。",
        "es": "请在 ALIYUN::RDS::DBInstance 上配置 DBInstanceStorageType 以满足策略。",
        "fr": "请在 ALIYUN::RDS::DBInstance 上配置 DBInstanceStorageType 以满足策略。",
        "pt": "请在 ALIYUN::RDS::DBInstance 上配置 DBInstanceStorageType 以满足策略。"
    },
    "resource_types": ["ALIYUN::RDS::DBInstance"]
}

deny contains result if {
    some name, resource in helpers.resources_by_type("ALIYUN::RDS::DBInstance")
    not is_compliant(resource)
    result := {
        "id": rule_meta.id,
        "resource_id": name,
        "violation_path": ["Properties", "DBInstanceStorageType"],
        "meta": {
            "severity": rule_meta.severity,
            "reason": rule_meta.reason,
            "recommendation": rule_meta.recommendation,
        },
    }
}

is_compliant(resource) if {
    helpers.has_property(resource, "DBInstanceStorageType")
}
