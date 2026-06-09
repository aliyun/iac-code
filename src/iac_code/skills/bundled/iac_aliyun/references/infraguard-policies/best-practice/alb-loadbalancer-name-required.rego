package infraguard.rules.aliyun.alb_loadbalancer_name_required

import rego.v1
import data.infraguard.helpers

rule_meta := {
    "id": "alb-loadbalancer-name-required",
    "severity": "medium",
    "name": {
        "en": "ALB must configure name",
        "zh": "ALB 必须配置名称",
        "ja": "ALIYUN::ALB::LoadBalancer には LoadBalancerName を設定する必要があります",
        "de": "Für ALIYUN::ALB::LoadBalancer muss LoadBalancerName konfiguriert sein",
        "es": "ALIYUN::ALB::LoadBalancer debe tener LoadBalancerName configurado",
        "fr": "ALIYUN::ALB::LoadBalancer doit avoir LoadBalancerName configuré",
        "pt": "ALIYUN::ALB::LoadBalancer deve ter LoadBalancerName configurado"
    },
    "description": {
        "en": "Checks ALB must configure name",
        "zh": "检查ALB 必须配置名称",
        "ja": "ALIYUN::ALB::LoadBalancer に LoadBalancerName が設定されていることを確認します",
        "de": "Prüft, ob LoadBalancerName für ALIYUN::ALB::LoadBalancer konfiguriert ist",
        "es": "Comprueba que ALIYUN::ALB::LoadBalancer tenga LoadBalancerName configurado",
        "fr": "Vérifie que ALIYUN::ALB::LoadBalancer a LoadBalancerName configuré",
        "pt": "Verifica se ALIYUN::ALB::LoadBalancer tem LoadBalancerName configurado"
    },
    "reason": {
        "en": "ALB must configure name is not satisfied.",
        "zh": "ALB 必须配置名称未满足。",
        "ja": "ALIYUN::ALB::LoadBalancer に LoadBalancerName が設定されていません。",
        "de": "Für ALIYUN::ALB::LoadBalancer ist LoadBalancerName nicht konfiguriert.",
        "es": "ALIYUN::ALB::LoadBalancer no tiene LoadBalancerName configurado.",
        "fr": "ALIYUN::ALB::LoadBalancer n'a pas LoadBalancerName configuré.",
        "pt": "ALIYUN::ALB::LoadBalancer não tem LoadBalancerName configurado."
    },
    "recommendation": {
        "en": "Configure LoadBalancerName on ALIYUN::ALB::LoadBalancer to satisfy the policy.",
        "zh": "请在 ALIYUN::ALB::LoadBalancer 上配置 LoadBalancerName 以满足策略。",
        "ja": "ポリシーを満たすには、ALIYUN::ALB::LoadBalancer に LoadBalancerName を設定してください。",
        "de": "Konfigurieren Sie LoadBalancerName für ALIYUN::ALB::LoadBalancer, um die Richtlinie zu erfüllen.",
        "es": "Configure LoadBalancerName en ALIYUN::ALB::LoadBalancer para cumplir la política.",
        "fr": "Configurez LoadBalancerName sur ALIYUN::ALB::LoadBalancer pour satisfaire la politique.",
        "pt": "Configure LoadBalancerName em ALIYUN::ALB::LoadBalancer para atender à política."
    },
    "resource_types": ["ALIYUN::ALB::LoadBalancer"]
}

deny contains result if {
    some name, resource in helpers.resources_by_type("ALIYUN::ALB::LoadBalancer")
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
