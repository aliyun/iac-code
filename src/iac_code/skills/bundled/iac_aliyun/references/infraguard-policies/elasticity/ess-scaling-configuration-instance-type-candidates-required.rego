package infraguard.rules.aliyun.ess_scaling_configuration_instance_type_candidates_required

import rego.v1
import data.infraguard.helpers

rule_meta := {
    "id": "ess-scaling-configuration-instance-type-candidates-required",
    "severity": "medium",
    "name": {
        "en": "ESS scaling configuration must set instance type",
        "zh": "ESS 伸缩配置必须设置实例规格",
        "ja": "ALIYUN::ESS::ScalingConfiguration には InstanceType を設定する必要があります",
        "de": "Für ALIYUN::ESS::ScalingConfiguration muss InstanceType konfiguriert sein",
        "es": "ALIYUN::ESS::ScalingConfiguration debe tener InstanceType configurado",
        "fr": "ALIYUN::ESS::ScalingConfiguration doit avoir InstanceType configuré",
        "pt": "ALIYUN::ESS::ScalingConfiguration deve ter InstanceType configurado"
    },
    "description": {
        "en": "Checks ESS scaling configuration must set instance type",
        "zh": "检查ESS 伸缩配置必须设置实例规格",
        "ja": "ALIYUN::ESS::ScalingConfiguration に InstanceType が設定されていることを確認します",
        "de": "Prüft, ob InstanceType für ALIYUN::ESS::ScalingConfiguration konfiguriert ist",
        "es": "Comprueba que ALIYUN::ESS::ScalingConfiguration tenga InstanceType configurado",
        "fr": "Vérifie que ALIYUN::ESS::ScalingConfiguration a InstanceType configuré",
        "pt": "Verifica se ALIYUN::ESS::ScalingConfiguration tem InstanceType configurado"
    },
    "reason": {
        "en": "ESS scaling configuration must set instance type is not satisfied.",
        "zh": "ESS 伸缩配置必须设置实例规格未满足。",
        "ja": "ALIYUN::ESS::ScalingConfiguration に InstanceType が設定されていません。",
        "de": "Für ALIYUN::ESS::ScalingConfiguration ist InstanceType nicht konfiguriert.",
        "es": "ALIYUN::ESS::ScalingConfiguration no tiene InstanceType configurado.",
        "fr": "ALIYUN::ESS::ScalingConfiguration n'a pas InstanceType configuré.",
        "pt": "ALIYUN::ESS::ScalingConfiguration não tem InstanceType configurado."
    },
    "recommendation": {
        "en": "Configure InstanceType on ALIYUN::ESS::ScalingConfiguration to satisfy the policy.",
        "zh": "请在 ALIYUN::ESS::ScalingConfiguration 上配置 InstanceType 以满足策略。",
        "ja": "ポリシーを満たすには、ALIYUN::ESS::ScalingConfiguration に InstanceType を設定してください。",
        "de": "Konfigurieren Sie InstanceType für ALIYUN::ESS::ScalingConfiguration, um die Richtlinie zu erfüllen.",
        "es": "Configure InstanceType en ALIYUN::ESS::ScalingConfiguration para cumplir la política.",
        "fr": "Configurez InstanceType sur ALIYUN::ESS::ScalingConfiguration pour satisfaire la politique.",
        "pt": "Configure InstanceType em ALIYUN::ESS::ScalingConfiguration para atender à política."
    },
    "resource_types": ["ALIYUN::ESS::ScalingConfiguration"]
}

deny contains result if {
    some name, resource in helpers.resources_by_type("ALIYUN::ESS::ScalingConfiguration")
    not is_compliant(resource)
    result := {
        "id": rule_meta.id,
        "resource_id": name,
        "violation_path": ["Properties", "InstanceType"],
        "meta": {
            "severity": rule_meta.severity,
            "reason": rule_meta.reason,
            "recommendation": rule_meta.recommendation,
        },
    }
}

is_compliant(resource) if {
    helpers.has_property(resource, "InstanceType")
}
