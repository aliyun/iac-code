package infraguard.rules.aliyun.slb_internet_charge_type_required

import rego.v1
import data.infraguard.helpers

rule_meta := {
    "id": "slb-internet-charge-type-required",
    "severity": "medium",
    "name": {
        "en": "SLB must set internet charge type",
        "zh": "SLB 必须设置公网计费类型",
        "ja": "ALIYUN::SLB::LoadBalancer には InternetChargeType を設定する必要があります",
        "de": "Für ALIYUN::SLB::LoadBalancer muss InternetChargeType konfiguriert sein",
        "es": "ALIYUN::SLB::LoadBalancer debe tener InternetChargeType configurado",
        "fr": "ALIYUN::SLB::LoadBalancer doit avoir InternetChargeType configuré",
        "pt": "ALIYUN::SLB::LoadBalancer deve ter InternetChargeType configurado"
    },
    "description": {
        "en": "Checks SLB must set internet charge type",
        "zh": "检查SLB 必须设置公网计费类型",
        "ja": "ALIYUN::SLB::LoadBalancer に InternetChargeType が設定されていることを確認します",
        "de": "Prüft, ob InternetChargeType für ALIYUN::SLB::LoadBalancer konfiguriert ist",
        "es": "Comprueba que ALIYUN::SLB::LoadBalancer tenga InternetChargeType configurado",
        "fr": "Vérifie que ALIYUN::SLB::LoadBalancer a InternetChargeType configuré",
        "pt": "Verifica se ALIYUN::SLB::LoadBalancer tem InternetChargeType configurado"
    },
    "reason": {
        "en": "SLB must set internet charge type is not satisfied.",
        "zh": "SLB 必须设置公网计费类型未满足。",
        "ja": "ALIYUN::SLB::LoadBalancer に InternetChargeType が設定されていません。",
        "de": "Für ALIYUN::SLB::LoadBalancer ist InternetChargeType nicht konfiguriert.",
        "es": "ALIYUN::SLB::LoadBalancer no tiene InternetChargeType configurado.",
        "fr": "ALIYUN::SLB::LoadBalancer n'a pas InternetChargeType configuré.",
        "pt": "ALIYUN::SLB::LoadBalancer não tem InternetChargeType configurado."
    },
    "recommendation": {
        "en": "Configure InternetChargeType on ALIYUN::SLB::LoadBalancer to satisfy the policy.",
        "zh": "请在 ALIYUN::SLB::LoadBalancer 上配置 InternetChargeType 以满足策略。",
        "ja": "ポリシーを満たすには、ALIYUN::SLB::LoadBalancer に InternetChargeType を設定してください。",
        "de": "Konfigurieren Sie InternetChargeType für ALIYUN::SLB::LoadBalancer, um die Richtlinie zu erfüllen.",
        "es": "Configure InternetChargeType en ALIYUN::SLB::LoadBalancer para cumplir la política.",
        "fr": "Configurez InternetChargeType sur ALIYUN::SLB::LoadBalancer pour satisfaire la politique.",
        "pt": "Configure InternetChargeType em ALIYUN::SLB::LoadBalancer para atender à política."
    },
    "resource_types": ["ALIYUN::SLB::LoadBalancer"]
}

deny contains result if {
    some name, resource in helpers.resources_by_type("ALIYUN::SLB::LoadBalancer")
    not is_compliant(resource)
    result := {
        "id": rule_meta.id,
        "resource_id": name,
        "violation_path": ["Properties", "InternetChargeType"],
        "meta": {
            "severity": rule_meta.severity,
            "reason": rule_meta.reason,
            "recommendation": rule_meta.recommendation,
        },
    }
}

is_compliant(resource) if {
    helpers.has_property(resource, "InternetChargeType")
}
