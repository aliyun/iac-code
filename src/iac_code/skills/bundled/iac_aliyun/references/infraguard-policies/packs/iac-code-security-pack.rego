package infraguard.packs.aliyun.iac_code_security

import rego.v1

pack_meta := {
    "id": "iac-code-security",
    "name": {
        "en": "IaC Code Security Scenario Pack",
        "zh": "IaC Code 安全性场景合规包",
    },
    "description": {
        "en": "Scenario-oriented InfraGuard policies for Security, covering identity, network exposure, data protection, audit logging, supply chain, and key management.",
        "zh": "面向安全性场景的 InfraGuard 策略组合，覆盖身份、网络公网暴露、数据保护、审计日志、供应链和密钥管理。",
    },
    "rules": [
        "security-ecs-instance-security-group-required",
        "security-ecs-instance-vpc-required",
        "security-ecs-disk-encrypted",
        "security-rds-instance-vpc-required",
        "security-redis-instance-vpc-required",
        "actiontrail-trail-intact-enabled",
        "vpc-flow-logs-enabled",
        "ram-user-mfa-check",
        "ram-password-policy-check",
        "ram-policy-no-statements-with-admin-access-check",
        "ecs-running-instance-no-public-ip",
        "ecs-security-group-risky-ports-check-with-protocol",
        "ecs-security-group-not-internet-cidr-access",
        "oss-bucket-server-side-encryption-enabled",
        "oss-bucket-only-https-enabled",
        "oss-bucket-public-read-prohibited",
        "oss-bucket-public-write-prohibited",
        "oss-bucket-logging-enabled",
        "rds-public-connection-and-any-ip-access-check",
        "rds-instance-enabled-ssl",
        "rds-instance-enabled-tde-disk-encryption",
        "redis-instance-no-public-ip",
        "redis-instance-enabled-ssl",
        "cr-repository-image-scanning-enabled",
        "cr-repository-type-private",
        "kms-key-rotation-enabled",
        "kms-secret-rotation-enabled",
        "fc-service-internet-access-disable",
        "api-gateway-api-auth-required",
        "api-gateway-api-internet-request-https"
    ]
}
