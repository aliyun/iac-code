package infraguard.rules.aliyun.ess_scaling_configuration_image_check

import rego.v1
import data.infraguard.helpers

rule_meta := {
    "id": "ess-scaling-configuration-image-check",
    "severity": "medium",
    "name": {
        "en": "ESS scaling configuration must set image",
        "zh": "ESS 伸缩配置必须设置镜像",
        "ja": "ALIYUN::ESS::ScalingConfiguration には ImageId を設定する必要があります",
        "de": "Für ALIYUN::ESS::ScalingConfiguration muss ImageId konfiguriert sein",
        "es": "ALIYUN::ESS::ScalingConfiguration debe tener ImageId configurado",
        "fr": "ALIYUN::ESS::ScalingConfiguration doit avoir ImageId configuré",
        "pt": "ALIYUN::ESS::ScalingConfiguration deve ter ImageId configurado"
    },
    "description": {
        "en": "Checks ESS scaling configuration must set image",
        "zh": "检查ESS 伸缩配置必须设置镜像",
        "ja": "ALIYUN::ESS::ScalingConfiguration に ImageId が設定されていることを確認します",
        "de": "Prüft, ob ImageId für ALIYUN::ESS::ScalingConfiguration konfiguriert ist",
        "es": "Comprueba que ALIYUN::ESS::ScalingConfiguration tenga ImageId configurado",
        "fr": "Vérifie que ALIYUN::ESS::ScalingConfiguration a ImageId configuré",
        "pt": "Verifica se ALIYUN::ESS::ScalingConfiguration tem ImageId configurado"
    },
    "reason": {
        "en": "ESS scaling configuration must set image is not satisfied.",
        "zh": "ESS 伸缩配置必须设置镜像未满足。",
        "ja": "ALIYUN::ESS::ScalingConfiguration に ImageId が設定されていません。",
        "de": "Für ALIYUN::ESS::ScalingConfiguration ist ImageId nicht konfiguriert.",
        "es": "ALIYUN::ESS::ScalingConfiguration no tiene ImageId configurado.",
        "fr": "ALIYUN::ESS::ScalingConfiguration n'a pas ImageId configuré.",
        "pt": "ALIYUN::ESS::ScalingConfiguration não tem ImageId configurado."
    },
    "recommendation": {
        "en": "Configure ImageId on ALIYUN::ESS::ScalingConfiguration to satisfy the policy.",
        "zh": "请在 ALIYUN::ESS::ScalingConfiguration 上配置 ImageId 以满足策略。",
        "ja": "ポリシーを満たすには、ALIYUN::ESS::ScalingConfiguration に ImageId を設定してください。",
        "de": "Konfigurieren Sie ImageId für ALIYUN::ESS::ScalingConfiguration, um die Richtlinie zu erfüllen.",
        "es": "Configure ImageId en ALIYUN::ESS::ScalingConfiguration para cumplir la política.",
        "fr": "Configurez ImageId sur ALIYUN::ESS::ScalingConfiguration pour satisfaire la politique.",
        "pt": "Configure ImageId em ALIYUN::ESS::ScalingConfiguration para atender à política."
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
