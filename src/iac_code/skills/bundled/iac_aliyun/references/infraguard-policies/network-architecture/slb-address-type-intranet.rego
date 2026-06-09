package infraguard.rules.aliyun.slb_address_type_intranet

import rego.v1
import data.infraguard.helpers

rule_meta := {
    "id": "slb-address-type-intranet",
    "severity": "medium",
    "name": {
        "en": "SLB should use intranet address type",
        "zh": "SLB 应使用内网地址类型",
        "ja": "ALIYUN::SLB::LoadBalancer には AddressType を設定する必要があります",
        "de": "Für ALIYUN::SLB::LoadBalancer muss AddressType konfiguriert sein",
        "es": "ALIYUN::SLB::LoadBalancer debe tener AddressType configurado",
        "fr": "ALIYUN::SLB::LoadBalancer doit avoir AddressType configuré",
        "pt": "ALIYUN::SLB::LoadBalancer deve ter AddressType configurado"
    },
    "description": {
        "en": "Checks SLB should use intranet address type",
        "zh": "检查SLB 应使用内网地址类型",
        "ja": "ALIYUN::SLB::LoadBalancer に AddressType が設定されていることを確認します",
        "de": "Prüft, ob AddressType für ALIYUN::SLB::LoadBalancer konfiguriert ist",
        "es": "Comprueba que ALIYUN::SLB::LoadBalancer tenga AddressType configurado",
        "fr": "Vérifie que ALIYUN::SLB::LoadBalancer a AddressType configuré",
        "pt": "Verifica se ALIYUN::SLB::LoadBalancer tem AddressType configurado"
    },
    "reason": {
        "en": "SLB should use intranet address type is not satisfied.",
        "zh": "SLB 应使用内网地址类型未满足。",
        "ja": "ALIYUN::SLB::LoadBalancer に AddressType が設定されていません。",
        "de": "Für ALIYUN::SLB::LoadBalancer ist AddressType nicht konfiguriert.",
        "es": "ALIYUN::SLB::LoadBalancer no tiene AddressType configurado.",
        "fr": "ALIYUN::SLB::LoadBalancer n'a pas AddressType configuré.",
        "pt": "ALIYUN::SLB::LoadBalancer não tem AddressType configurado."
    },
    "recommendation": {
        "en": "Configure AddressType on ALIYUN::SLB::LoadBalancer to satisfy the policy.",
        "zh": "请在 ALIYUN::SLB::LoadBalancer 上配置 AddressType 以满足策略。",
        "ja": "ポリシーを満たすには、ALIYUN::SLB::LoadBalancer に AddressType を設定してください。",
        "de": "Konfigurieren Sie AddressType für ALIYUN::SLB::LoadBalancer, um die Richtlinie zu erfüllen.",
        "es": "Configure AddressType en ALIYUN::SLB::LoadBalancer para cumplir la política.",
        "fr": "Configurez AddressType sur ALIYUN::SLB::LoadBalancer pour satisfaire la politique.",
        "pt": "Configure AddressType em ALIYUN::SLB::LoadBalancer para atender à política."
    },
    "resource_types": ["ALIYUN::SLB::LoadBalancer"]
}

deny contains result if {
    some name, resource in helpers.resources_by_type("ALIYUN::SLB::LoadBalancer")
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
    helpers.get_property(resource, "AddressType", "") == "intranet"
}
