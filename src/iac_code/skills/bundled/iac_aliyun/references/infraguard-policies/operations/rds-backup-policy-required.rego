package infraguard.rules.aliyun.rds_backup_policy_required

import rego.v1
import data.infraguard.helpers

rule_meta := {
    "id": "rds-backup-policy-required",
    "severity": "medium",
    "name": {
        "en": "RDS backup policy must be configured",
        "zh": "RDS 必须配置备份策略",
        "ja": "ALIYUN::RDS::Backup には BackupTime を設定する必要があります",
        "de": "Für ALIYUN::RDS::Backup muss BackupTime konfiguriert sein",
        "es": "ALIYUN::RDS::Backup debe tener BackupTime configurado",
        "fr": "ALIYUN::RDS::Backup doit avoir BackupTime configuré",
        "pt": "ALIYUN::RDS::Backup deve ter BackupTime configurado"
    },
    "description": {
        "en": "Checks RDS backup policy must be configured",
        "zh": "检查RDS 必须配置备份策略",
        "ja": "ALIYUN::RDS::Backup に BackupTime が設定されていることを確認します",
        "de": "Prüft, ob BackupTime für ALIYUN::RDS::Backup konfiguriert ist",
        "es": "Comprueba que ALIYUN::RDS::Backup tenga BackupTime configurado",
        "fr": "Vérifie que ALIYUN::RDS::Backup a BackupTime configuré",
        "pt": "Verifica se ALIYUN::RDS::Backup tem BackupTime configurado"
    },
    "reason": {
        "en": "RDS backup policy must be configured is not satisfied.",
        "zh": "RDS 必须配置备份策略未满足。",
        "ja": "ALIYUN::RDS::Backup に BackupTime が設定されていません。",
        "de": "Für ALIYUN::RDS::Backup ist BackupTime nicht konfiguriert.",
        "es": "ALIYUN::RDS::Backup no tiene BackupTime configurado.",
        "fr": "ALIYUN::RDS::Backup n'a pas BackupTime configuré.",
        "pt": "ALIYUN::RDS::Backup não tem BackupTime configurado."
    },
    "recommendation": {
        "en": "Configure BackupTime on ALIYUN::RDS::Backup to satisfy the policy.",
        "zh": "请在 ALIYUN::RDS::Backup 上配置 BackupTime 以满足策略。",
        "ja": "ポリシーを満たすには、ALIYUN::RDS::Backup に BackupTime を設定してください。",
        "de": "Konfigurieren Sie BackupTime für ALIYUN::RDS::Backup, um die Richtlinie zu erfüllen.",
        "es": "Configure BackupTime en ALIYUN::RDS::Backup para cumplir la política.",
        "fr": "Configurez BackupTime sur ALIYUN::RDS::Backup pour satisfaire la politique.",
        "pt": "Configure BackupTime em ALIYUN::RDS::Backup para atender à política."
    },
    "resource_types": ["ALIYUN::RDS::Backup"]
}

deny contains result if {
    some name, resource in helpers.resources_by_type("ALIYUN::RDS::Backup")
    not is_compliant(resource)
    result := {
        "id": rule_meta.id,
        "resource_id": name,
        "violation_path": ["Properties", "BackupTime"],
        "meta": {
            "severity": rule_meta.severity,
            "reason": rule_meta.reason,
            "recommendation": rule_meta.recommendation,
        },
    }
}

is_compliant(resource) if {
    helpers.has_property(resource, "BackupTime")
}
