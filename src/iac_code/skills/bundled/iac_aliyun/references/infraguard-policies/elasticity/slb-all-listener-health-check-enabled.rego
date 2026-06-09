package infraguard.rules.aliyun.slb_all_listener_health_check_enabled

import rego.v1
import data.infraguard.helpers

rule_meta := {
    "id": "slb-all-listener-health-check-enabled",
    "severity": "medium",
    "name": {
        "en": "SLB listener must configure backend server port",
        "zh": "SLB 监听必须配置后端端口",
        "ja": "ALIYUN::SLB::Listener には BackendServerPort を設定する必要があります",
        "de": "Für ALIYUN::SLB::Listener muss BackendServerPort konfiguriert sein",
        "es": "ALIYUN::SLB::Listener debe tener BackendServerPort configurado",
        "fr": "ALIYUN::SLB::Listener doit avoir BackendServerPort configuré",
        "pt": "ALIYUN::SLB::Listener deve ter BackendServerPort configurado"
    },
    "description": {
        "en": "Checks SLB listener must configure backend server port",
        "zh": "检查SLB 监听必须配置后端端口",
        "ja": "ALIYUN::SLB::Listener に BackendServerPort が設定されていることを確認します",
        "de": "Prüft, ob BackendServerPort für ALIYUN::SLB::Listener konfiguriert ist",
        "es": "Comprueba que ALIYUN::SLB::Listener tenga BackendServerPort configurado",
        "fr": "Vérifie que ALIYUN::SLB::Listener a BackendServerPort configuré",
        "pt": "Verifica se ALIYUN::SLB::Listener tem BackendServerPort configurado"
    },
    "reason": {
        "en": "SLB listener must configure backend server port is not satisfied.",
        "zh": "SLB 监听必须配置后端端口未满足。",
        "ja": "ALIYUN::SLB::Listener に BackendServerPort が設定されていません。",
        "de": "Für ALIYUN::SLB::Listener ist BackendServerPort nicht konfiguriert.",
        "es": "ALIYUN::SLB::Listener no tiene BackendServerPort configurado.",
        "fr": "ALIYUN::SLB::Listener n'a pas BackendServerPort configuré.",
        "pt": "ALIYUN::SLB::Listener não tem BackendServerPort configurado."
    },
    "recommendation": {
        "en": "Configure BackendServerPort on ALIYUN::SLB::Listener to satisfy the policy.",
        "zh": "请在 ALIYUN::SLB::Listener 上配置 BackendServerPort 以满足策略。",
        "ja": "ポリシーを満たすには、ALIYUN::SLB::Listener に BackendServerPort を設定してください。",
        "de": "Konfigurieren Sie BackendServerPort für ALIYUN::SLB::Listener, um die Richtlinie zu erfüllen.",
        "es": "Configure BackendServerPort en ALIYUN::SLB::Listener para cumplir la política.",
        "fr": "Configurez BackendServerPort sur ALIYUN::SLB::Listener pour satisfaire la politique.",
        "pt": "Configure BackendServerPort em ALIYUN::SLB::Listener para atender à política."
    },
    "resource_types": ["ALIYUN::SLB::Listener"]
}

deny contains result if {
    some name, resource in helpers.resources_by_type("ALIYUN::SLB::Listener")
    not is_compliant(resource)
    result := {
        "id": rule_meta.id,
        "resource_id": name,
        "violation_path": ["Properties", "BackendServerPort"],
        "meta": {
            "severity": rule_meta.severity,
            "reason": rule_meta.reason,
            "recommendation": rule_meta.recommendation,
        },
    }
}

is_compliant(resource) if {
    helpers.has_property(resource, "BackendServerPort")
}
