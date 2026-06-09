package infraguard.packs.aliyun.iac_code_security

import rego.v1

pack_meta := {
    "id": "iac-code-security",
    "name": {
        "en": "IaC Code Security Scenario Pack",
        "zh": "IaC Code 安全性场景合规包",
    },
    "description": {
        "en": "Scenario-oriented InfraGuard policies for Security.",
        "zh": "面向安全性场景的 InfraGuard 策略组合。",
    },
    "rules": [
        "security-ecs-instance-no-public-ip",
        "security-ecs-instance-security-group-required",
        "security-ecs-instance-vpc-required",
        "security-ecs-disk-encrypted",
        "security-oss-bucket-private-acl",
        "security-oss-bucket-encryption-configured",
        "security-oss-bucket-logging-configured",
        "security-rds-instance-vpc-required",
        "security-rds-instance-ssl-required",
        "security-rds-instance-tde-enabled",
        "security-redis-instance-vpc-required",
        "security-ram-user-mfa-required",
        "security-api-gateway-api-auth-required"
    ]
}
