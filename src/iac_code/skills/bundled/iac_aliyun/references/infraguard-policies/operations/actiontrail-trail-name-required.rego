package infraguard.rules.aliyun.actiontrail_trail_name_required

import rego.v1
import data.infraguard.helpers

rule_meta := {
    "id": "actiontrail-trail-name-required",
    "severity": "medium",
    "name": {
        "en": "ActionTrail trail must configure name",
        "zh": "ActionTrail 跟踪必须配置名称",
        "ja": "ALIYUN::ACTIONTRAIL::Trail には TrailName を設定する必要があります",
        "de": "Für ALIYUN::ACTIONTRAIL::Trail muss TrailName konfiguriert sein",
        "es": "ALIYUN::ACTIONTRAIL::Trail debe tener TrailName configurado",
        "fr": "ALIYUN::ACTIONTRAIL::Trail doit avoir TrailName configuré",
        "pt": "ALIYUN::ACTIONTRAIL::Trail deve ter TrailName configurado"
    },
    "description": {
        "en": "Checks ActionTrail trail must configure name",
        "zh": "检查ActionTrail 跟踪必须配置名称",
        "ja": "ALIYUN::ACTIONTRAIL::Trail に TrailName が設定されていることを確認します",
        "de": "Prüft, ob TrailName für ALIYUN::ACTIONTRAIL::Trail konfiguriert ist",
        "es": "Comprueba que ALIYUN::ACTIONTRAIL::Trail tenga TrailName configurado",
        "fr": "Vérifie que ALIYUN::ACTIONTRAIL::Trail a TrailName configuré",
        "pt": "Verifica se ALIYUN::ACTIONTRAIL::Trail tem TrailName configurado"
    },
    "reason": {
        "en": "ActionTrail trail must configure name is not satisfied.",
        "zh": "ActionTrail 跟踪必须配置名称未满足。",
        "ja": "ALIYUN::ACTIONTRAIL::Trail に TrailName が設定されていません。",
        "de": "Für ALIYUN::ACTIONTRAIL::Trail ist TrailName nicht konfiguriert.",
        "es": "ALIYUN::ACTIONTRAIL::Trail no tiene TrailName configurado.",
        "fr": "ALIYUN::ACTIONTRAIL::Trail n'a pas TrailName configuré.",
        "pt": "ALIYUN::ACTIONTRAIL::Trail não tem TrailName configurado."
    },
    "recommendation": {
        "en": "Configure TrailName on ALIYUN::ACTIONTRAIL::Trail to satisfy the policy.",
        "zh": "请在 ALIYUN::ACTIONTRAIL::Trail 上配置 TrailName 以满足策略。",
        "ja": "ポリシーを満たすには、ALIYUN::ACTIONTRAIL::Trail に TrailName を設定してください。",
        "de": "Konfigurieren Sie TrailName für ALIYUN::ACTIONTRAIL::Trail, um die Richtlinie zu erfüllen.",
        "es": "Configure TrailName en ALIYUN::ACTIONTRAIL::Trail para cumplir la política.",
        "fr": "Configurez TrailName sur ALIYUN::ACTIONTRAIL::Trail pour satisfaire la politique.",
        "pt": "Configure TrailName em ALIYUN::ACTIONTRAIL::Trail para atender à política."
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
