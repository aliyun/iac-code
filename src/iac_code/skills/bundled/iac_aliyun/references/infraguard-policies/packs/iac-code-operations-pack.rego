package infraguard.packs.aliyun.iac_code_operations

import rego.v1

pack_meta := {
    "id": "iac-code-operations",
    "name": {
        "en": "IaC Code Operations Pack",
        "zh": "IaC Code 可运维性合规包",
        "ja": "IaC Code 運用パック",
        "de": "IaC Code Betriebspaket",
        "es": "Paquete de Operaciones de IaC Code",
        "fr": "Pack Opérations IaC Code",
        "pt": "Pacote de Operações do IaC Code"
    },
    "description": {
        "en": "InfraGuard policies for observability, audit logging, backup, recovery, and deletion protection in Alibaba Cloud ROS templates.",
        "zh": "面向 Alibaba Cloud ROS 模板的 InfraGuard 策略，覆盖可观测、审计日志、备份恢复和删除保护。",
        "ja": "Alibaba Cloud ROS テンプレート向けに、可観測性、監査ログ、バックアップ、復旧、削除保護を確認する InfraGuard ポリシーです。",
        "de": "InfraGuard-Richtlinien fuer Observability, Audit-Logging, Backup, Wiederherstellung und Loeschschutz in Alibaba Cloud ROS-Templates.",
        "es": "Políticas de InfraGuard para observabilidad, registros de auditoría, backup, recuperación y protección contra eliminación en plantillas ROS de Alibaba Cloud.",
        "fr": "Politiques InfraGuard pour observabilite, journaux d'audit, sauvegarde, restauration et protection contre la suppression dans les modeles ROS Alibaba Cloud.",
        "pt": "Políticas InfraGuard para observabilidade, logs de auditoria, backup, recuperação e proteção contra exclusão em modelos ROS do Alibaba Cloud."
    },
    "rules": [
        "oss-bucket-operational-access-logging",
        "sls-logstore-ttl-configured",
        "sls-logstore-shard-count-configured",
        "actiontrail-trail-name-required",
        "ecs-disk-auto-snapshot-policy",
        "rds-backup-policy-required",
        "redis-backup-policy-required",
        "ecs-instance-operational-deletion-protection",
        "rds-instance-deletion-protection-enabled",
        "polardb-cluster-delete-protection-enabled",
        "fc-service-log-enable",
        "fc-service-tracing-enable",
        "cms-alarm-name-required"
    ]
}
