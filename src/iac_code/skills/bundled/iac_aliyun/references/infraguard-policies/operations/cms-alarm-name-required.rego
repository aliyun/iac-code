package infraguard.rules.aliyun.cms_alarm_name_required

import rego.v1
import data.infraguard.helpers

rule_meta := {
    "id": "cms-alarm-name-required",
    "severity": "medium",
    "name": {
        "en": "CMS alarm must configure name",
        "zh": "云监控告警必须配置名称",
        "ja": "ALIYUN::CMS::Alarm には Name を設定する必要があります",
        "de": "Für ALIYUN::CMS::Alarm muss Name konfiguriert sein",
        "es": "ALIYUN::CMS::Alarm debe tener Name configurado",
        "fr": "ALIYUN::CMS::Alarm doit avoir Name configuré",
        "pt": "ALIYUN::CMS::Alarm deve ter Name configurado"
    },
    "description": {
        "en": "Checks CMS alarm must configure name",
        "zh": "检查云监控告警必须配置名称",
        "ja": "ALIYUN::CMS::Alarm に Name が設定されていることを確認します",
        "de": "Prüft, ob Name für ALIYUN::CMS::Alarm konfiguriert ist",
        "es": "Comprueba que ALIYUN::CMS::Alarm tenga Name configurado",
        "fr": "Vérifie que ALIYUN::CMS::Alarm a Name configuré",
        "pt": "Verifica se ALIYUN::CMS::Alarm tem Name configurado"
    },
    "reason": {
        "en": "CMS alarm must configure name is not satisfied.",
        "zh": "云监控告警必须配置名称未满足。",
        "ja": "ALIYUN::CMS::Alarm に Name が設定されていません。",
        "de": "Für ALIYUN::CMS::Alarm ist Name nicht konfiguriert.",
        "es": "ALIYUN::CMS::Alarm no tiene Name configurado.",
        "fr": "ALIYUN::CMS::Alarm n'a pas Name configuré.",
        "pt": "ALIYUN::CMS::Alarm não tem Name configurado."
    },
    "recommendation": {
        "en": "Configure Name on ALIYUN::CMS::Alarm to satisfy the policy.",
        "zh": "请在 ALIYUN::CMS::Alarm 上配置 Name 以满足策略。",
        "ja": "ポリシーを満たすには、ALIYUN::CMS::Alarm に Name を設定してください。",
        "de": "Konfigurieren Sie Name für ALIYUN::CMS::Alarm, um die Richtlinie zu erfüllen.",
        "es": "Configure Name en ALIYUN::CMS::Alarm para cumplir la política.",
        "fr": "Configurez Name sur ALIYUN::CMS::Alarm pour satisfaire la politique.",
        "pt": "Configure Name em ALIYUN::CMS::Alarm para atender à política."
    },
    "resource_types": ["ALIYUN::CMS::Alarm"]
}

deny contains result if {
    some name, resource in helpers.resources_by_type("ALIYUN::CMS::Alarm")
    not is_compliant(resource)
    result := {
        "id": rule_meta.id,
        "resource_id": name,
        "violation_path": ["Properties", "Name"],
        "meta": {
            "severity": rule_meta.severity,
            "reason": rule_meta.reason,
            "recommendation": rule_meta.recommendation,
        },
    }
}

is_compliant(resource) if {
    helpers.has_property(resource, "Name")
}
