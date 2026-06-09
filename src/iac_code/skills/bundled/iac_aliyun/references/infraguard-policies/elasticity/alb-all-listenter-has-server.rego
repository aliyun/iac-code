package infraguard.rules.aliyun.alb_all_listenter_has_server

import rego.v1
import data.infraguard.helpers

rule_meta := {
    "id": "alb-all-listenter-has-server",
    "severity": "medium",
    "name": {
        "en": "ALB listener must configure server group",
        "zh": "ALB 监听必须配置服务器组",
        "ja": "ALIYUN::ALB::Listener には DefaultActions を設定する必要があります",
        "de": "Für ALIYUN::ALB::Listener muss DefaultActions konfiguriert sein",
        "es": "ALIYUN::ALB::Listener debe tener DefaultActions configurado",
        "fr": "ALIYUN::ALB::Listener doit avoir DefaultActions configuré",
        "pt": "ALIYUN::ALB::Listener deve ter DefaultActions configurado"
    },
    "description": {
        "en": "Checks ALB listener must configure server group",
        "zh": "检查ALB 监听必须配置服务器组",
        "ja": "ALIYUN::ALB::Listener に DefaultActions が設定されていることを確認します",
        "de": "Prüft, ob DefaultActions für ALIYUN::ALB::Listener konfiguriert ist",
        "es": "Comprueba que ALIYUN::ALB::Listener tenga DefaultActions configurado",
        "fr": "Vérifie que ALIYUN::ALB::Listener a DefaultActions configuré",
        "pt": "Verifica se ALIYUN::ALB::Listener tem DefaultActions configurado"
    },
    "reason": {
        "en": "ALB listener must configure server group is not satisfied.",
        "zh": "ALB 监听必须配置服务器组未满足。",
        "ja": "ALIYUN::ALB::Listener に DefaultActions が設定されていません。",
        "de": "Für ALIYUN::ALB::Listener ist DefaultActions nicht konfiguriert.",
        "es": "ALIYUN::ALB::Listener no tiene DefaultActions configurado.",
        "fr": "ALIYUN::ALB::Listener n'a pas DefaultActions configuré.",
        "pt": "ALIYUN::ALB::Listener não tem DefaultActions configurado."
    },
    "recommendation": {
        "en": "Configure DefaultActions on ALIYUN::ALB::Listener to satisfy the policy.",
        "zh": "请在 ALIYUN::ALB::Listener 上配置 DefaultActions 以满足策略。",
        "ja": "ポリシーを満たすには、ALIYUN::ALB::Listener に DefaultActions を設定してください。",
        "de": "Konfigurieren Sie DefaultActions für ALIYUN::ALB::Listener, um die Richtlinie zu erfüllen.",
        "es": "Configure DefaultActions en ALIYUN::ALB::Listener para cumplir la política.",
        "fr": "Configurez DefaultActions sur ALIYUN::ALB::Listener pour satisfaire la politique.",
        "pt": "Configure DefaultActions em ALIYUN::ALB::Listener para atender à política."
    },
    "resource_types": ["ALIYUN::ALB::Listener"]
}

deny contains result if {
    some name, resource in helpers.resources_by_type("ALIYUN::ALB::Listener")
    not is_compliant(resource)
    result := {
        "id": rule_meta.id,
        "resource_id": name,
        "violation_path": ["Properties", "DefaultActions"],
        "meta": {
            "severity": rule_meta.severity,
            "reason": rule_meta.reason,
            "recommendation": rule_meta.recommendation,
        },
    }
}

is_compliant(resource) if {
    helpers.has_property(resource, "DefaultActions")
}
