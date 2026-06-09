package infraguard.rules.aliyun.ess_scaling_configuration_image_check

import rego.v1
import data.infraguard.helpers

rule_meta := {
    "id": "ess-scaling-configuration-image-check",
    "severity": "medium",
    "name": {
        "en": "ESS scaling configuration must set image",
        "zh": "ESS 伸缩配置必须设置镜像",
        "ja": "ESS 伸缩配置必须设置镜像",
        "de": "ESS 伸缩配置必须设置镜像",
        "es": "ESS 伸缩配置必须设置镜像",
        "fr": "ESS 伸缩配置必须设置镜像",
        "pt": "ESS 伸缩配置必须设置镜像"
    },
    "description": {
        "en": "Checks ESS scaling configuration must set image",
        "zh": "检查ESS 伸缩配置必须设置镜像",
        "ja": "检查ESS 伸缩配置必须设置镜像",
        "de": "检查ESS 伸缩配置必须设置镜像",
        "es": "检查ESS 伸缩配置必须设置镜像",
        "fr": "检查ESS 伸缩配置必须设置镜像",
        "pt": "检查ESS 伸缩配置必须设置镜像"
    },
    "reason": {
        "en": "ESS scaling configuration must set image is not satisfied.",
        "zh": "ESS 伸缩配置必须设置镜像未满足。",
        "ja": "ESS 伸缩配置必须设置镜像未满足。",
        "de": "ESS 伸缩配置必须设置镜像未满足。",
        "es": "ESS 伸缩配置必须设置镜像未满足。",
        "fr": "ESS 伸缩配置必须设置镜像未满足。",
        "pt": "ESS 伸缩配置必须设置镜像未满足。"
    },
    "recommendation": {
        "en": "Configure ImageId on ALIYUN::ESS::ScalingConfiguration to satisfy the policy.",
        "zh": "请在 ALIYUN::ESS::ScalingConfiguration 上配置 ImageId 以满足策略。",
        "ja": "请在 ALIYUN::ESS::ScalingConfiguration 上配置 ImageId 以满足策略。",
        "de": "请在 ALIYUN::ESS::ScalingConfiguration 上配置 ImageId 以满足策略。",
        "es": "请在 ALIYUN::ESS::ScalingConfiguration 上配置 ImageId 以满足策略。",
        "fr": "请在 ALIYUN::ESS::ScalingConfiguration 上配置 ImageId 以满足策略。",
        "pt": "请在 ALIYUN::ESS::ScalingConfiguration 上配置 ImageId 以满足策略。"
    },
    "resource_types": ["ALIYUN::ESS::ScalingConfiguration"]
}

deny contains result if {
    some name, resource in helpers.resources_by_type("ALIYUN::ESS::ScalingConfiguration")
    not is_compliant(resource)
    result := {
        "id": rule_meta.id,
        "resource_id": name,
        "violation_path": ["Properties", "ImageId"],
        "meta": {
            "severity": rule_meta.severity,
            "reason": rule_meta.reason,
            "recommendation": rule_meta.recommendation,
        },
    }
}

is_compliant(resource) if {
    helpers.has_property(resource, "ImageId")
}
