package infraguard.rules.aliyun.slb_loadbalancer_name_required

import rego.v1
import data.infraguard.helpers

rule_meta := {
    "id": "slb-loadbalancer-name-required",
    "severity": "medium",
    "name": {
        "en": "SLB must configure name",
        "zh": "SLB 必须配置名称",
        "ja": "ALIYUN::SLB::LoadBalancer には LoadBalancerName を設定する必要があります",
        "de": "Für ALIYUN::SLB::LoadBalancer muss LoadBalancerName konfiguriert sein",
        "es": "ALIYUN::SLB::LoadBalancer debe tener LoadBalancerName configurado",
        "fr": "ALIYUN::SLB::LoadBalancer doit avoir LoadBalancerName configuré",
        "pt": "ALIYUN::SLB::LoadBalancer deve ter LoadBalancerName configurado"
    },
    "description": {
        "en": "Checks SLB must configure name",
        "zh": "检查SLB 必须配置名称",
        "ja": "ALIYUN::SLB::LoadBalancer に LoadBalancerName が設定されていることを確認します",
        "de": "Prüft, ob LoadBalancerName für ALIYUN::SLB::LoadBalancer konfiguriert ist",
        "es": "Comprueba que ALIYUN::SLB::LoadBalancer tenga LoadBalancerName configurado",
        "fr": "Vérifie que ALIYUN::SLB::LoadBalancer a LoadBalancerName configuré",
        "pt": "Verifica se ALIYUN::SLB::LoadBalancer tem LoadBalancerName configurado"
    },
    "reason": {
        "en": "SLB must configure name is not satisfied.",
        "zh": "SLB 必须配置名称未满足。",
        "ja": "ALIYUN::SLB::LoadBalancer に LoadBalancerName が設定されていません。",
        "de": "Für ALIYUN::SLB::LoadBalancer ist LoadBalancerName nicht konfiguriert.",
        "es": "ALIYUN::SLB::LoadBalancer no tiene LoadBalancerName configurado.",
        "fr": "ALIYUN::SLB::LoadBalancer n'a pas LoadBalancerName configuré.",
        "pt": "ALIYUN::SLB::LoadBalancer não tem LoadBalancerName configurado."
    },
    "recommendation": {
        "en": "Configure LoadBalancerName on ALIYUN::SLB::LoadBalancer to satisfy the policy.",
        "zh": "请在 ALIYUN::SLB::LoadBalancer 上配置 LoadBalancerName 以满足策略。",
        "ja": "ポリシーを満たすには、ALIYUN::SLB::LoadBalancer に LoadBalancerName を設定してください。",
        "de": "Konfigurieren Sie LoadBalancerName für ALIYUN::SLB::LoadBalancer, um die Richtlinie zu erfüllen.",
        "es": "Configure LoadBalancerName en ALIYUN::SLB::LoadBalancer para cumplir la política.",
        "fr": "Configurez LoadBalancerName sur ALIYUN::SLB::LoadBalancer pour satisfaire la politique.",
        "pt": "Configure LoadBalancerName em ALIYUN::SLB::LoadBalancer para atender à política."
    },
    "resource_types": ["ALIYUN::SLB::LoadBalancer"]
}

deny contains result if {
    some name, resource in helpers.resources_by_type("ALIYUN::SLB::LoadBalancer")
    not is_compliant(resource)
    result := {
        "id": rule_meta.id,
        "resource_id": name,
        "violation_path": ["Properties", "LoadBalancerName"],
        "meta": {
            "severity": rule_meta.severity,
            "reason": rule_meta.reason,
            "recommendation": rule_meta.recommendation,
        },
    }
}

is_compliant(resource) if {
    helpers.has_property(resource, "LoadBalancerName")
}
