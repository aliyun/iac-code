package infraguard.rules.aliyun.nlb_address_type_intranet

import rego.v1
import data.infraguard.helpers

rule_meta := {
    "id": "nlb-address-type-intranet",
    "severity": "medium",
    "name": {
        "en": "NLB should use intranet address type",
        "zh": "NLB 应使用内网地址类型",
        "ja": "ALIYUN::NLB::LoadBalancer には AddressType を設定する必要があります",
        "de": "Für ALIYUN::NLB::LoadBalancer muss AddressType konfiguriert sein",
        "es": "ALIYUN::NLB::LoadBalancer debe tener AddressType configurado",
        "fr": "ALIYUN::NLB::LoadBalancer doit avoir AddressType configuré",
        "pt": "ALIYUN::NLB::LoadBalancer deve ter AddressType configurado"
    },
    "description": {
        "en": "Checks NLB should use intranet address type",
        "zh": "检查NLB 应使用内网地址类型",
        "ja": "ALIYUN::NLB::LoadBalancer に AddressType が設定されていることを確認します",
        "de": "Prüft, ob AddressType für ALIYUN::NLB::LoadBalancer konfiguriert ist",
        "es": "Comprueba que ALIYUN::NLB::LoadBalancer tenga AddressType configurado",
        "fr": "Vérifie que ALIYUN::NLB::LoadBalancer a AddressType configuré",
        "pt": "Verifica se ALIYUN::NLB::LoadBalancer tem AddressType configurado"
    },
    "reason": {
        "en": "NLB should use intranet address type is not satisfied.",
        "zh": "NLB 应使用内网地址类型未满足。",
        "ja": "ALIYUN::NLB::LoadBalancer に AddressType が設定されていません。",
        "de": "Für ALIYUN::NLB::LoadBalancer ist AddressType nicht konfiguriert.",
        "es": "ALIYUN::NLB::LoadBalancer no tiene AddressType configurado.",
        "fr": "ALIYUN::NLB::LoadBalancer n'a pas AddressType configuré.",
        "pt": "ALIYUN::NLB::LoadBalancer não tem AddressType configurado."
    },
    "recommendation": {
        "en": "Configure AddressType on ALIYUN::NLB::LoadBalancer to satisfy the policy.",
        "zh": "请在 ALIYUN::NLB::LoadBalancer 上配置 AddressType 以满足策略。",
        "ja": "ポリシーを満たすには、ALIYUN::NLB::LoadBalancer に AddressType を設定してください。",
        "de": "Konfigurieren Sie AddressType für ALIYUN::NLB::LoadBalancer, um die Richtlinie zu erfüllen.",
        "es": "Configure AddressType en ALIYUN::NLB::LoadBalancer para cumplir la política.",
        "fr": "Configurez AddressType sur ALIYUN::NLB::LoadBalancer pour satisfaire la politique.",
        "pt": "Configure AddressType em ALIYUN::NLB::LoadBalancer para atender à política."
    },
    "resource_types": ["ALIYUN::NLB::LoadBalancer"]
}

deny contains result if {
    some name, resource in helpers.resources_by_type("ALIYUN::NLB::LoadBalancer")
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
