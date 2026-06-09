package infraguard.rules.aliyun.redis_backup_policy_required

import rego.v1
import data.infraguard.helpers

rule_meta := {
    "id": "redis-backup-policy-required",
    "severity": "medium",
    "name": {
        "en": "Redis backup policy must be configured",
        "zh": "Redis 必须配置备份策略",
        "ja": "ALIYUN::REDIS::Instance には BackupPolicy を設定する必要があります",
        "de": "Für ALIYUN::REDIS::Instance muss BackupPolicy konfiguriert sein",
        "es": "ALIYUN::REDIS::Instance debe tener BackupPolicy configurado",
        "fr": "ALIYUN::REDIS::Instance doit avoir BackupPolicy configuré",
        "pt": "ALIYUN::REDIS::Instance deve ter BackupPolicy configurado"
    },
    "description": {
        "en": "Checks Redis backup policy must be configured",
        "zh": "检查Redis 必须配置备份策略",
        "ja": "ALIYUN::REDIS::Instance に BackupPolicy が設定されていることを確認します",
        "de": "Prüft, ob BackupPolicy für ALIYUN::REDIS::Instance konfiguriert ist",
        "es": "Comprueba que ALIYUN::REDIS::Instance tenga BackupPolicy configurado",
        "fr": "Vérifie que ALIYUN::REDIS::Instance a BackupPolicy configuré",
        "pt": "Verifica se ALIYUN::REDIS::Instance tem BackupPolicy configurado"
    },
    "reason": {
        "en": "Redis backup policy must be configured is not satisfied.",
        "zh": "Redis 必须配置备份策略未满足。",
        "ja": "ALIYUN::REDIS::Instance に BackupPolicy が設定されていません。",
        "de": "Für ALIYUN::REDIS::Instance ist BackupPolicy nicht konfiguriert.",
        "es": "ALIYUN::REDIS::Instance no tiene BackupPolicy configurado.",
        "fr": "ALIYUN::REDIS::Instance n'a pas BackupPolicy configuré.",
        "pt": "ALIYUN::REDIS::Instance não tem BackupPolicy configurado."
    },
    "recommendation": {
        "en": "Configure BackupPolicy on ALIYUN::REDIS::Instance to satisfy the policy.",
        "zh": "请在 ALIYUN::REDIS::Instance 上配置 BackupPolicy 以满足策略。",
        "ja": "ポリシーを満たすには、ALIYUN::REDIS::Instance に BackupPolicy を設定してください。",
        "de": "Konfigurieren Sie BackupPolicy für ALIYUN::REDIS::Instance, um die Richtlinie zu erfüllen.",
        "es": "Configure BackupPolicy en ALIYUN::REDIS::Instance para cumplir la política.",
        "fr": "Configurez BackupPolicy sur ALIYUN::REDIS::Instance pour satisfaire la politique.",
        "pt": "Configure BackupPolicy em ALIYUN::REDIS::Instance para atender à política."
    },
    "resource_types": ["ALIYUN::REDIS::Instance"]
}

deny contains result if {
    some name, resource in helpers.resources_by_type("ALIYUN::REDIS::Instance")
    not is_compliant(resource)
    result := {
        "id": rule_meta.id,
        "resource_id": name,
        "violation_path": ["Properties", "BackupPolicy"],
        "meta": {
            "severity": rule_meta.severity,
            "reason": rule_meta.reason,
            "recommendation": rule_meta.recommendation,
        },
    }
}

is_compliant(resource) if {
    helpers.has_property(resource, "BackupPolicy")
}
