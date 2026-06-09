package infraguard.rules.aliyun.alb_address_type_intranet

import rego.v1
import data.infraguard.helpers

rule_meta := {
    "id": "alb-address-type-intranet",
    "severity": "medium",
    "name": {
        "en": "ALB should use intranet address type",
        "zh": "ALB 应使用内网地址类型",
        "ja": "ALIYUN::ALB::LoadBalancer には AddressType を設定する必要があります",
        "de": "Für ALIYUN::ALB::LoadBalancer muss AddressType konfiguriert sein",
        "es": "ALIYUN::ALB::LoadBalancer debe tener AddressType configurado",
        "fr": "ALIYUN::ALB::LoadBalancer doit avoir AddressType configuré",
        "pt": "ALIYUN::ALB::LoadBalancer deve ter AddressType configurado"
    },
    "description": {
        "en": "Checks ALB should use intranet address type",
        "zh": "检查ALB 应使用内网地址类型",
        "ja": "ALIYUN::ALB::LoadBalancer に AddressType が設定されていることを確認します",
        "de": "Prüft, ob AddressType für ALIYUN::ALB::LoadBalancer konfiguriert ist",
        "es": "Comprueba que ALIYUN::ALB::LoadBalancer tenga AddressType configurado",
        "fr": "Vérifie que ALIYUN::ALB::LoadBalancer a AddressType configuré",
        "pt": "Verifica se ALIYUN::ALB::LoadBalancer tem AddressType configurado"
    },
    "reason": {
        "en": "ALB should use intranet address type is not satisfied.",
        "zh": "ALB 应使用内网地址类型未满足。",
        "ja": "ALIYUN::ALB::LoadBalancer に AddressType が設定されていません。",
        "de": "Für ALIYUN::ALB::LoadBalancer ist AddressType nicht konfiguriert.",
        "es": "ALIYUN::ALB::LoadBalancer no tiene AddressType configurado.",
        "fr": "ALIYUN::ALB::LoadBalancer n'a pas AddressType configuré.",
        "pt": "ALIYUN::ALB::LoadBalancer não tem AddressType configurado."
    },
    "recommendation": {
        "en": "Configure AddressType on ALIYUN::ALB::LoadBalancer to satisfy the policy.",
        "zh": "请在 ALIYUN::ALB::LoadBalancer 上配置 AddressType 以满足策略。",
        "ja": "ポリシーを満たすには、ALIYUN::ALB::LoadBalancer に AddressType を設定してください。",
        "de": "Konfigurieren Sie AddressType für ALIYUN::ALB::LoadBalancer, um die Richtlinie zu erfüllen.",
        "es": "Configure AddressType en ALIYUN::ALB::LoadBalancer para cumplir la política.",
        "fr": "Configurez AddressType sur ALIYUN::ALB::LoadBalancer pour satisfaire la politique.",
        "pt": "Configure AddressType em ALIYUN::ALB::LoadBalancer para atender à política."
    },
    "resource_types": ["ALIYUN::ALB::LoadBalancer"]
}

deny contains result if {
    some name, resource in helpers.resources_by_type("ALIYUN::ALB::LoadBalancer")
    not is_compliant(resource)
    result := {
        "id": rule_meta.id,
        "resource_id": name,
        "violation_path": ["Properties", "AddressType"],
        "meta": {
            "severity": rule_meta.severity,
            "reason": rule_meta.reason,
            "recommendation": rule_meta.recommendation,
        },
    }
}

is_compliant(resource) if {
    helpers.get_property(resource, "AddressType", "") == "Intranet"
}
