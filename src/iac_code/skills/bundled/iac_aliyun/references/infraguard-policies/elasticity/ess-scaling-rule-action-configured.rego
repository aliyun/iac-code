package infraguard.rules.aliyun.ess_scaling_rule_action_configured

import rego.v1
import data.infraguard.helpers

rule_meta := {
    "id": "ess-scaling-rule-action-configured",
    "severity": "medium",
    "name": {
        "en": "ESS scaling rule must configure adjustment",
        "zh": "ESS 伸缩规则必须配置调整方式",
        "ja": "ALIYUN::ESS::ScalingRule には AdjustmentType を設定する必要があります",
        "de": "Für ALIYUN::ESS::ScalingRule muss AdjustmentType konfiguriert sein",
        "es": "ALIYUN::ESS::ScalingRule debe tener AdjustmentType configurado",
        "fr": "ALIYUN::ESS::ScalingRule doit avoir AdjustmentType configuré",
        "pt": "ALIYUN::ESS::ScalingRule deve ter AdjustmentType configurado"
    },
    "description": {
        "en": "Checks ESS scaling rule must configure adjustment",
        "zh": "检查ESS 伸缩规则必须配置调整方式",
        "ja": "ALIYUN::ESS::ScalingRule に AdjustmentType が設定されていることを確認します",
        "de": "Prüft, ob AdjustmentType für ALIYUN::ESS::ScalingRule konfiguriert ist",
        "es": "Comprueba que ALIYUN::ESS::ScalingRule tenga AdjustmentType configurado",
        "fr": "Vérifie que ALIYUN::ESS::ScalingRule a AdjustmentType configuré",
        "pt": "Verifica se ALIYUN::ESS::ScalingRule tem AdjustmentType configurado"
    },
    "reason": {
        "en": "ESS scaling rule must configure adjustment is not satisfied.",
        "zh": "ESS 伸缩规则必须配置调整方式未满足。",
        "ja": "ALIYUN::ESS::ScalingRule に AdjustmentType が設定されていません。",
        "de": "Für ALIYUN::ESS::ScalingRule ist AdjustmentType nicht konfiguriert.",
        "es": "ALIYUN::ESS::ScalingRule no tiene AdjustmentType configurado.",
        "fr": "ALIYUN::ESS::ScalingRule n'a pas AdjustmentType configuré.",
        "pt": "ALIYUN::ESS::ScalingRule não tem AdjustmentType configurado."
    },
    "recommendation": {
        "en": "Configure AdjustmentType on ALIYUN::ESS::ScalingRule to satisfy the policy.",
        "zh": "请在 ALIYUN::ESS::ScalingRule 上配置 AdjustmentType 以满足策略。",
        "ja": "ポリシーを満たすには、ALIYUN::ESS::ScalingRule に AdjustmentType を設定してください。",
        "de": "Konfigurieren Sie AdjustmentType für ALIYUN::ESS::ScalingRule, um die Richtlinie zu erfüllen.",
        "es": "Configure AdjustmentType en ALIYUN::ESS::ScalingRule para cumplir la política.",
        "fr": "Configurez AdjustmentType sur ALIYUN::ESS::ScalingRule pour satisfaire la politique.",
        "pt": "Configure AdjustmentType em ALIYUN::ESS::ScalingRule para atender à política."
    },
    "resource_types": ["ALIYUN::ESS::ScalingRule"]
}

deny contains result if {
    some name, resource in helpers.resources_by_type("ALIYUN::ESS::ScalingRule")
    not is_compliant(resource)
    result := {
        "id": rule_meta.id,
        "resource_id": name,
        "violation_path": ["Properties", "AdjustmentType"],
        "meta": {
            "severity": rule_meta.severity,
            "reason": rule_meta.reason,
            "recommendation": rule_meta.recommendation,
        },
    }
}

is_compliant(resource) if {
    helpers.has_property(resource, "AdjustmentType")
}
