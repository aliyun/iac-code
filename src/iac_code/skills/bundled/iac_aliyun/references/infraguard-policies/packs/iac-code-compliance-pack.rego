package infraguard.packs.aliyun.iac_code_compliance

import rego.v1

pack_meta := {
    "id": "iac-code-compliance",
    "name": {
        "en": "IaC Code Cloud Compliance Pack",
        "zh": "IaC Code 云基础设施合规包",
        "ja": "IaC Code クラウドコンプライアンスパック",
        "de": "IaC Code Cloud-Compliance-Paket",
        "es": "Paquete de Cumplimiento Cloud de IaC Code",
        "fr": "Pack de Conformite Cloud IaC Code",
        "pt": "Pacote de Conformidade Cloud do IaC Code",
    },
    "description": {
        "en": "Compliance-oriented InfraGuard policies for Alibaba Cloud ROS templates, covering audit trails, identity controls, encryption, backup, access restriction, and resilient log storage.",
        "zh": "面向 Alibaba Cloud ROS 模板的合规策略组合，覆盖审计跟踪、身份控制、加密、备份、访问限制和日志冗余存储。",
        "ja": "Alibaba Cloud ROS テンプレート向けのコンプライアンス重視 InfraGuard ポリシーで、監査証跡、ID 制御、暗号化、バックアップ、アクセス制限、ログ冗長保存をカバーします。",
        "de": "Compliance-orientierte InfraGuard-Richtlinien fuer Alibaba Cloud ROS-Vorlagen, mit Audit-Trails, Identitaetskontrollen, Verschluesselung, Backup, Zugriffsbeschraenkung und redundantem Logspeicher.",
        "es": "Politicas de InfraGuard orientadas al cumplimiento para plantillas ROS de Alibaba Cloud, con auditoria, controles de identidad, cifrado, copias de seguridad, restriccion de acceso y almacenamiento redundante de logs.",
        "fr": "Politiques InfraGuard orientees conformite pour les modeles ROS Alibaba Cloud, couvrant les traces d'audit, les controles d'identite, le chiffrement, la sauvegarde, la restriction d'acces et le stockage redondant des journaux.",
        "pt": "Politicas InfraGuard voltadas a conformidade para modelos ROS do Alibaba Cloud, cobrindo trilhas de auditoria, controles de identidade, criptografia, backup, restricao de acesso e armazenamento redundante de logs.",
    },
    "rules": [
        "oss-bucket-server-side-encryption-enabled",
        "oss-bucket-logging-enabled",
        "rds-white-list-internet-ip-access-check",
        "rds-instance-enabled-log-backup",
        "actiontrail-trail-intact-enabled",
        "ram-password-policy-check",
        "ram-user-mfa-check",
        "ecs-instance-deletion-protection-enabled",
        "sls-project-multi-zone",
        "kms-key-rotation-enabled",
        "maxcompute-project-encryption-enabled",
        "nas-filesystem-encrypt-type-check",
        "polardb-cluster-enabled-tde",
    ]
}
