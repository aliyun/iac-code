package infraguard.rules.aliyun.ess_scaling_group_capacity_bounds_required

import rego.v1
import data.infraguard.helpers

rule_meta := {
    "id": "ess-scaling-group-capacity-bounds-required",
    "severity": "medium",
    "name": {
        "en": "ESS scaling group must configure MaxSize",
        "zh": "ESS 伸缩组必须配置最大容量",
        "ja": "ALIYUN::ESS::ScalingGroup には MaxSize を設定する必要があります",
        "de": "Für ALIYUN::ESS::ScalingGroup muss MaxSize konfiguriert sein",
        "es": "ALIYUN::ESS::ScalingGroup debe tener MaxSize configurado",
        "fr": "ALIYUN::ESS::ScalingGroup doit avoir MaxSize configuré",
        "pt": "ALIYUN::ESS::ScalingGroup deve ter MaxSize configurado"
    },
    "description": {
        "en": "Checks ESS scaling group must configure MaxSize",
        "zh": "检查ESS 伸缩组必须配置最大容量",
        "ja": "ALIYUN::ESS::ScalingGroup に MaxSize が設定されていることを確認します",
        "de": "Prüft, ob MaxSize für ALIYUN::ESS::ScalingGroup konfiguriert ist",
        "es": "Comprueba que ALIYUN::ESS::ScalingGroup tenga MaxSize configurado",
        "fr": "Vérifie que ALIYUN::ESS::ScalingGroup a MaxSize configuré",
        "pt": "Verifica se ALIYUN::ESS::ScalingGroup tem MaxSize configurado"
    },
    "reason": {
        "en": "ESS scaling group must configure MaxSize is not satisfied.",
        "zh": "ESS 伸缩组必须配置最大容量未满足。",
        "ja": "ALIYUN::ESS::ScalingGroup に MaxSize が設定されていません。",
        "de": "Für ALIYUN::ESS::ScalingGroup ist MaxSize nicht konfiguriert.",
        "es": "ALIYUN::ESS::ScalingGroup no tiene MaxSize configurado.",
        "fr": "ALIYUN::ESS::ScalingGroup n'a pas MaxSize configuré.",
        "pt": "ALIYUN::ESS::ScalingGroup não tem MaxSize configurado."
    },
    "recommendation": {
        "en": "Configure MaxSize on ALIYUN::ESS::ScalingGroup to satisfy the policy.",
        "zh": "请在 ALIYUN::ESS::ScalingGroup 上配置 MaxSize 以满足策略。",
        "ja": "ポリシーを満たすには、ALIYUN::ESS::ScalingGroup に MaxSize を設定してください。",
        "de": "Konfigurieren Sie MaxSize für ALIYUN::ESS::ScalingGroup, um die Richtlinie zu erfüllen.",
        "es": "Configure MaxSize en ALIYUN::ESS::ScalingGroup para cumplir la política.",
        "fr": "Configurez MaxSize sur ALIYUN::ESS::ScalingGroup pour satisfaire la politique.",
        "pt": "Configure MaxSize em ALIYUN::ESS::ScalingGroup para atender à política."
    },
    "resource_types": ["ALIYUN::ESS::ScalingGroup"]
}

deny contains result if {
    some name, resource in helpers.resources_by_type("ALIYUN::ESS::ScalingGroup")
    not is_compliant(resource)
    result := {
        "id": rule_meta.id,
        "resource_id": name,
        "violation_path": ["Properties", "MaxSize"],
        "meta": {
            "severity": rule_meta.severity,
            "reason": rule_meta.reason,
            "recommendation": rule_meta.recommendation,
        },
    }
}

is_compliant(resource) if {
    helpers.has_property(resource, "MaxSize")
}
