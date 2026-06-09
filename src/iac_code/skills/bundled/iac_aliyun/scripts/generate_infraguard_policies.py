"""Generate bundled InfraGuard policy examples for the iac-aliyun skill."""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent
from typing import Any

# ruff: noqa: E501


ROOT = Path(__file__).resolve().parents[1] / "references" / "infraguard-policies"
POLICY_METADATA_LANGUAGES = ("en", "zh", "ja", "de", "es", "fr", "pt")


def rule_id(scenario: str, policy: dict[str, Any]) -> str:
    return f"{scenario}-{policy['slug']}"


def rule(
    slug: str,
    en: str,
    zh: str,
    resource_type: str,
    property_name: str,
    *,
    check: str = "required",
    expected: Any = None,
    severity: str = "medium",
) -> dict[str, Any]:
    return {
        "slug": slug,
        "en": en,
        "zh": zh,
        "resource_type": resource_type,
        "property": property_name,
        "check": check,
        "expected": expected,
        "severity": severity,
    }


SCENARIOS: dict[str, dict[str, Any]] = {
    "security": {
        "zh": "安全性",
        "rules": [
            rule(
                "ecs-instance-security-group-required",
                "ECS instance must attach a security group",
                "ECS 实例必须绑定安全组",
                "ALIYUN::ECS::Instance",
                "SecurityGroupId",
                severity="high",
            ),
            rule(
                "ecs-instance-vpc-required",
                "ECS instance must run in VPC",
                "ECS 实例必须部署在 VPC 内",
                "ALIYUN::ECS::Instance",
                "VpcId",
                severity="high",
            ),
            rule(
                "ecs-disk-encrypted",
                "ECS disk must enable encryption",
                "ECS 云盘必须启用加密",
                "ALIYUN::ECS::Disk",
                "Encrypted",
                check="true",
                severity="high",
            ),
            rule(
                "rds-instance-vpc-required",
                "RDS instance must run in VPC",
                "RDS 实例必须部署在 VPC 内",
                "ALIYUN::RDS::DBInstance",
                "VpcId",
                severity="high",
            ),
            rule(
                "redis-instance-vpc-required",
                "Redis instance must run in VPC",
                "Redis 实例必须部署在 VPC 内",
                "ALIYUN::REDIS::Instance",
                "VpcId",
                severity="high",
            ),
        ],
    },
    "high-availability": {
        "zh": "高可用",
        "rules": [
            rule(
                "rds-instance-zone-required",
                "RDS instance must specify a zone",
                "RDS 实例必须指定可用区",
                "ALIYUN::RDS::DBInstance",
                "ZoneId",
                severity="medium",
            ),
            rule(
                "rds-instance-secondary-zone-required",
                "RDS instance must specify a secondary zone",
                "RDS 实例必须指定备用可用区",
                "ALIYUN::RDS::DBInstance",
                "ZoneIdSlave1",
                severity="high",
            ),
            rule(
                "polardb-cluster-zone-required",
                "PolarDB cluster must specify a zone",
                "PolarDB 集群必须指定可用区",
                "ALIYUN::POLARDB::DBCluster",
                "ZoneId",
                severity="medium",
            ),
            rule(
                "redis-instance-zone-required",
                "Redis instance must specify a zone",
                "Redis 实例必须指定可用区",
                "ALIYUN::REDIS::Instance",
                "ZoneId",
                severity="medium",
            ),
            rule(
                "mongodb-instance-zone-required",
                "MongoDB instance must specify a zone",
                "MongoDB 实例必须指定可用区",
                "ALIYUN::MONGODB::Instance",
                "ZoneId",
                severity="medium",
            ),
            rule(
                "slb-master-zone-required",
                "SLB must configure a master zone",
                "SLB 必须配置主可用区",
                "ALIYUN::SLB::LoadBalancer",
                "MasterZoneId",
                severity="high",
            ),
            rule(
                "slb-slave-zone-required",
                "SLB must configure a slave zone",
                "SLB 必须配置备可用区",
                "ALIYUN::SLB::LoadBalancer",
                "SlaveZoneId",
                severity="high",
            ),
            rule(
                "alb-zone-mappings-required",
                "ALB must configure zone mappings",
                "ALB 必须配置可用区映射",
                "ALIYUN::ALB::LoadBalancer",
                "ZoneMappings",
                severity="high",
            ),
            rule(
                "nlb-zone-mappings-required",
                "NLB must configure zone mappings",
                "NLB 必须配置可用区映射",
                "ALIYUN::NLB::LoadBalancer",
                "ZoneMappings",
                severity="high",
            ),
            rule(
                "ess-scaling-group-vswitches-required",
                "ESS scaling group must configure VSwitches",
                "ESS 伸缩组必须配置交换机",
                "ALIYUN::ESS::ScalingGroup",
                "VSwitchIds",
                severity="high",
            ),
            rule(
                "ecs-instance-group-min-required",
                "ECS instance group must configure MinAmount",
                "ECS 实例组必须配置最小实例数",
                "ALIYUN::ECS::InstanceGroup",
                "MinAmount",
                severity="medium",
            ),
            rule(
                "ecs-instance-group-max-required",
                "ECS instance group must configure MaxAmount",
                "ECS 实例组必须配置最大实例数",
                "ALIYUN::ECS::InstanceGroup",
                "MaxAmount",
                severity="medium",
            ),
            rule(
                "oss-bucket-zrs-enabled",
                "OSS bucket must use ZRS redundancy",
                "OSS Bucket 必须使用同城冗余",
                "ALIYUN::OSS::Bucket",
                "RedundancyType",
                check="equals",
                expected="ZRS",
                severity="medium",
            ),
        ],
    },
    "cost-optimization": {
        "zh": "成本优化",
        "rules": [
            rule(
                "ecs-instance-charge-type-required",
                "ECS instance must set charge type",
                "ECS 实例必须设置付费类型",
                "ALIYUN::ECS::Instance",
                "InstanceChargeType",
            ),
            rule(
                "ecs-instance-bandwidth-configured",
                "ECS instance must configure outbound bandwidth",
                "ECS 实例必须配置出网带宽",
                "ALIYUN::ECS::Instance",
                "InternetMaxBandwidthOut",
            ),
            rule(
                "ecs-instance-type-required",
                "ECS instance must set instance type",
                "ECS 实例必须设置实例规格",
                "ALIYUN::ECS::Instance",
                "InstanceType",
            ),
            rule(
                "ecs-disk-category-required",
                "ECS disk must set disk category",
                "ECS 云盘必须设置云盘类型",
                "ALIYUN::ECS::Disk",
                "DiskCategory",
            ),
            rule(
                "ecs-disk-size-required",
                "ECS disk must set disk size",
                "ECS 云盘必须设置容量",
                "ALIYUN::ECS::Disk",
                "Size",
            ),
            rule(
                "rds-pay-type-required",
                "RDS instance must set pay type",
                "RDS 实例必须设置付费类型",
                "ALIYUN::RDS::DBInstance",
                "PayType",
            ),
            rule(
                "rds-storage-type-required",
                "RDS instance must set storage type",
                "RDS 实例必须设置存储类型",
                "ALIYUN::RDS::DBInstance",
                "DBInstanceStorageType",
            ),
            rule(
                "redis-instance-class-required",
                "Redis instance must set instance class",
                "Redis 实例必须设置规格",
                "ALIYUN::REDIS::Instance",
                "InstanceClass",
            ),
            rule(
                "slb-internet-charge-type-required",
                "SLB must set internet charge type",
                "SLB 必须设置公网计费类型",
                "ALIYUN::SLB::LoadBalancer",
                "InternetChargeType",
            ),
            rule(
                "eip-bandwidth-required", "EIP must set bandwidth", "EIP 必须设置带宽", "ALIYUN::VPC::EIP", "Bandwidth"
            ),
            rule(
                "nat-gateway-spec-required",
                "NAT Gateway must set specification",
                "NAT 网关必须设置规格",
                "ALIYUN::VPC::NatGateway",
                "NatGatewaySpec",
            ),
            rule(
                "oss-storage-class-required",
                "OSS bucket must set storage class",
                "OSS Bucket 必须设置存储类型",
                "ALIYUN::OSS::Bucket",
                "StorageClass",
            ),
            rule(
                "logstore-ttl-required",
                "SLS Logstore must set TTL",
                "SLS Logstore 必须设置数据保存时间",
                "ALIYUN::SLS::Logstore",
                "TTL",
            ),
        ],
    },
    "compliance": {
        "zh": "合规性",
        "rules": [
            rule(
                "oss-bucket-encryption-compliance",
                "OSS bucket must configure encryption for compliance",
                "OSS Bucket 必须配置合规加密",
                "ALIYUN::OSS::Bucket",
                "ServerSideEncryptionConfiguration",
                severity="high",
            ),
            rule(
                "oss-bucket-logging-compliance",
                "OSS bucket must configure logging for compliance",
                "OSS Bucket 必须配置合规日志",
                "ALIYUN::OSS::Bucket",
                "LoggingConfiguration",
                severity="medium",
            ),
            rule(
                "rds-security-ip-list-required",
                "RDS instance must configure security IP list",
                "RDS 实例必须配置安全白名单",
                "ALIYUN::RDS::DBInstance",
                "SecurityIPList",
                severity="high",
            ),
            rule(
                "rds-backup-retention-required",
                "RDS backup must configure retention period",
                "RDS 备份必须配置保留周期",
                "ALIYUN::RDS::Backup",
                "BackupRetentionPeriod",
                severity="medium",
            ),
            rule(
                "actiontrail-trail-enabled",
                "ActionTrail trail must be enabled",
                "ActionTrail 跟踪必须启用",
                "ALIYUN::ACTIONTRAIL::Trail",
                "Enable",
                check="true",
                severity="high",
            ),
            rule(
                "ram-password-min-length-required",
                "RAM password policy must set minimum length",
                "RAM 密码策略必须设置最小长度",
                "ALIYUN::RAM::PasswordPolicy",
                "MinimumPasswordLength",
                severity="medium",
            ),
            rule(
                "ram-user-mfa-compliance",
                "RAM user must configure MFA for compliance",
                "RAM 用户必须配置 MFA 以满足合规要求",
                "ALIYUN::RAM::User",
                "MFABindRequired",
                check="true",
                severity="high",
            ),
            rule(
                "ecs-deletion-protection-enabled",
                "ECS instance must enable deletion protection",
                "ECS 实例必须启用删除保护",
                "ALIYUN::ECS::Instance",
                "DeletionProtection",
                check="true",
                severity="medium",
            ),
            rule(
                "sls-project-redundancy-required",
                "SLS project must configure data redundancy",
                "SLS Project 必须配置数据冗余",
                "ALIYUN::SLS::Project",
                "DataRedundancyType",
                severity="medium",
            ),
            rule(
                "kms-key-rotation-required",
                "KMS key must configure rotation",
                "KMS 密钥必须配置轮转",
                "ALIYUN::KMS::Key",
                "RotationInterval",
                severity="high",
            ),
            rule(
                "maxcompute-project-encryption-required",
                "MaxCompute project must configure encryption",
                "MaxCompute 项目必须配置加密",
                "ALIYUN::MaxCompute::Project",
                "Encryption",
                severity="high",
            ),
            rule(
                "nas-filesystem-encrypt-type-required",
                "NAS file system must set encryption type",
                "NAS 文件系统必须设置加密类型",
                "ALIYUN::NAS::FileSystem",
                "EncryptType",
                severity="high",
            ),
            rule(
                "polardb-tde-enabled",
                "PolarDB cluster must enable TDE",
                "PolarDB 集群必须启用 TDE",
                "ALIYUN::POLARDB::DBCluster",
                "TDEStatus",
                check="equals",
                expected="Enabled",
                severity="high",
            ),
        ],
    },
    "best-practice": {
        "zh": "最佳实践",
        "rules": [
            rule(
                "ecs-instance-tags-required",
                "ECS instance must configure tags",
                "ECS 实例必须配置标签",
                "ALIYUN::ECS::Instance",
                "Tags",
            ),
            rule(
                "ecs-instance-name-required",
                "ECS instance must configure name",
                "ECS 实例必须配置名称",
                "ALIYUN::ECS::Instance",
                "InstanceName",
            ),
            rule(
                "ecs-security-group-description-required",
                "Security group must configure description",
                "安全组必须配置描述",
                "ALIYUN::ECS::SecurityGroup",
                "Description",
            ),
            rule("vpc-name-required", "VPC must configure name", "VPC 必须配置名称", "ALIYUN::ECS::VPC", "VpcName"),
            rule(
                "vswitch-name-required",
                "VSwitch must configure name",
                "交换机必须配置名称",
                "ALIYUN::ECS::VSwitch",
                "VSwitchName",
            ),
            rule(
                "rds-instance-tags-required",
                "RDS instance must configure tags",
                "RDS 实例必须配置标签",
                "ALIYUN::RDS::DBInstance",
                "Tags",
            ),
            rule(
                "redis-instance-name-required",
                "Redis instance must configure name",
                "Redis 实例必须配置名称",
                "ALIYUN::REDIS::Instance",
                "InstanceName",
            ),
            rule(
                "oss-bucket-tags-required",
                "OSS bucket must configure tags",
                "OSS Bucket 必须配置标签",
                "ALIYUN::OSS::Bucket",
                "Tags",
            ),
            rule(
                "slb-loadbalancer-name-required",
                "SLB must configure name",
                "SLB 必须配置名称",
                "ALIYUN::SLB::LoadBalancer",
                "LoadBalancerName",
            ),
            rule(
                "alb-loadbalancer-name-required",
                "ALB must configure name",
                "ALB 必须配置名称",
                "ALIYUN::ALB::LoadBalancer",
                "LoadBalancerName",
            ),
            rule(
                "polardb-cluster-tags-required",
                "PolarDB cluster must configure tags",
                "PolarDB 集群必须配置标签",
                "ALIYUN::POLARDB::DBCluster",
                "Tags",
            ),
            rule(
                "sls-project-description-required",
                "SLS project must configure description",
                "SLS Project 必须配置描述",
                "ALIYUN::SLS::Project",
                "Description",
            ),
            rule(
                "kms-key-description-required",
                "KMS key must configure description",
                "KMS 密钥必须配置描述",
                "ALIYUN::KMS::Key",
                "Description",
            ),
        ],
    },
    "operations": {
        "zh": "可运维性",
        "rules": [
            rule(
                "oss-bucket-logging-enabled",
                "OSS bucket must enable logging",
                "OSS Bucket 必须启用日志",
                "ALIYUN::OSS::Bucket",
                "LoggingConfiguration",
                severity="medium",
            ),
            rule(
                "sls-logstore-ttl-configured",
                "SLS Logstore must configure TTL",
                "SLS Logstore 必须配置 TTL",
                "ALIYUN::SLS::Logstore",
                "TTL",
                severity="medium",
            ),
            rule(
                "sls-logstore-shard-count-configured",
                "SLS Logstore must configure shard count",
                "SLS Logstore 必须配置分区数",
                "ALIYUN::SLS::Logstore",
                "ShardCount",
                severity="medium",
            ),
            rule(
                "actiontrail-trail-name-required",
                "ActionTrail trail must configure name",
                "ActionTrail 跟踪必须配置名称",
                "ALIYUN::ACTIONTRAIL::Trail",
                "TrailName",
                severity="medium",
            ),
            rule(
                "ecs-auto-snapshot-policy-required",
                "ECS disk must attach auto snapshot policy",
                "ECS 云盘必须绑定自动快照策略",
                "ALIYUN::ECS::Disk",
                "AutoSnapshotPolicyId",
                severity="medium",
            ),
            rule(
                "rds-backup-policy-required",
                "RDS backup policy must be configured",
                "RDS 必须配置备份策略",
                "ALIYUN::RDS::Backup",
                "BackupTime",
                severity="medium",
            ),
            rule(
                "redis-backup-policy-required",
                "Redis backup policy must be configured",
                "Redis 必须配置备份策略",
                "ALIYUN::REDIS::Instance",
                "BackupPolicy",
                severity="medium",
            ),
            rule(
                "ecs-instance-deletion-protection-ops",
                "ECS instance must enable deletion protection for operations",
                "ECS 实例必须启用运维删除保护",
                "ALIYUN::ECS::Instance",
                "DeletionProtection",
                check="true",
                severity="medium",
            ),
            rule(
                "rds-deletion-protection-enabled",
                "RDS instance must enable deletion protection",
                "RDS 实例必须启用删除保护",
                "ALIYUN::RDS::DBInstance",
                "DeletionProtection",
                check="true",
                severity="medium",
            ),
            rule(
                "polardb-deletion-protection-enabled",
                "PolarDB cluster must enable deletion protection",
                "PolarDB 集群必须启用删除保护",
                "ALIYUN::POLARDB::DBCluster",
                "DeletionProtection",
                check="true",
                severity="medium",
            ),
            rule(
                "fc-service-log-config-required",
                "FC service must configure logging",
                "函数计算服务必须配置日志",
                "ALIYUN::FC::Service",
                "LogConfig",
                severity="medium",
            ),
            rule(
                "fc-service-tracing-config-required",
                "FC service must configure tracing",
                "函数计算服务必须配置链路追踪",
                "ALIYUN::FC::Service",
                "TracingConfig",
                severity="medium",
            ),
            rule(
                "cms-alarm-name-required",
                "CMS alarm must configure name",
                "云监控告警必须配置名称",
                "ALIYUN::CMS::Alarm",
                "Name",
                severity="medium",
            ),
        ],
    },
    "network-architecture": {
        "zh": "网络架构",
        "rules": [
            rule(
                "vpc-cidr-required",
                "VPC must configure CIDR block",
                "VPC 必须配置网段",
                "ALIYUN::ECS::VPC",
                "CidrBlock",
                severity="high",
            ),
            rule(
                "vswitch-cidr-required",
                "VSwitch must configure CIDR block",
                "交换机必须配置网段",
                "ALIYUN::ECS::VSwitch",
                "CidrBlock",
                severity="high",
            ),
            rule(
                "vswitch-zone-required",
                "VSwitch must configure zone",
                "交换机必须配置可用区",
                "ALIYUN::ECS::VSwitch",
                "ZoneId",
                severity="medium",
            ),
            rule(
                "security-group-vpc-required",
                "Security group must bind VPC",
                "安全组必须绑定 VPC",
                "ALIYUN::ECS::SecurityGroup",
                "VpcId",
                severity="high",
            ),
            rule(
                "security-group-type-required",
                "Security group must set type",
                "安全组必须设置类型",
                "ALIYUN::ECS::SecurityGroup",
                "SecurityGroupType",
                severity="medium",
            ),
            rule(
                "nat-gateway-vpc-required",
                "NAT Gateway must bind VPC",
                "NAT 网关必须绑定 VPC",
                "ALIYUN::VPC::NatGateway",
                "VpcId",
                severity="high",
            ),
            rule(
                "eip-bandwidth-package-required",
                "EIP must configure bandwidth",
                "EIP 必须配置带宽",
                "ALIYUN::VPC::EIP",
                "Bandwidth",
                severity="medium",
            ),
            rule(
                "slb-address-type-intranet",
                "SLB should use intranet address type",
                "SLB 应使用内网地址类型",
                "ALIYUN::SLB::LoadBalancer",
                "AddressType",
                check="equals",
                expected="intranet",
                severity="medium",
            ),
            rule(
                "alb-address-type-intranet",
                "ALB should use intranet address type",
                "ALB 应使用内网地址类型",
                "ALIYUN::ALB::LoadBalancer",
                "AddressType",
                check="equals",
                expected="Intranet",
                severity="medium",
            ),
            rule(
                "nlb-address-type-intranet",
                "NLB should use intranet address type",
                "NLB 应使用内网地址类型",
                "ALIYUN::NLB::LoadBalancer",
                "AddressType",
                check="equals",
                expected="Intranet",
                severity="medium",
            ),
            rule(
                "vpn-gateway-vpc-required",
                "VPN Gateway must bind VPC",
                "VPN 网关必须绑定 VPC",
                "ALIYUN::VPC::VpnGateway",
                "VpcId",
                severity="high",
            ),
            rule(
                "cen-instance-name-required",
                "CEN instance must configure name",
                "CEN 实例必须配置名称",
                "ALIYUN::CEN::CenInstance",
                "Name",
                severity="medium",
            ),
            rule(
                "transit-router-vpc-attachment-zone-required",
                "Transit router VPC attachment must configure zone mapping",
                "转发路由器 VPC 连接必须配置可用区映射",
                "ALIYUN::CEN::TransitRouterVpcAttachment",
                "ZoneMappings",
                severity="high",
            ),
        ],
    },
    "elasticity": {
        "zh": "弹性能力",
        "rules": [
            rule(
                "ess-scaling-group-min-size-required",
                "ESS scaling group must configure MinSize",
                "ESS 伸缩组必须配置最小容量",
                "ALIYUN::ESS::ScalingGroup",
                "MinSize",
                severity="medium",
            ),
            rule(
                "ess-scaling-group-max-size-required",
                "ESS scaling group must configure MaxSize",
                "ESS 伸缩组必须配置最大容量",
                "ALIYUN::ESS::ScalingGroup",
                "MaxSize",
                severity="medium",
            ),
            rule(
                "ess-scaling-group-default-cooldown-required",
                "ESS scaling group must configure cooldown",
                "ESS 伸缩组必须配置冷却时间",
                "ALIYUN::ESS::ScalingGroup",
                "DefaultCooldown",
                severity="medium",
            ),
            rule(
                "ess-scaling-group-vswitches-elasticity",
                "ESS scaling group must configure VSwitches for elasticity",
                "ESS 伸缩组必须配置交换机以支持弹性",
                "ALIYUN::ESS::ScalingGroup",
                "VSwitchIds",
                severity="high",
            ),
            rule(
                "ess-scaling-configuration-instance-type-required",
                "ESS scaling configuration must set instance type",
                "ESS 伸缩配置必须设置实例规格",
                "ALIYUN::ESS::ScalingConfiguration",
                "InstanceType",
                severity="medium",
            ),
            rule(
                "ess-scaling-configuration-image-required",
                "ESS scaling configuration must set image",
                "ESS 伸缩配置必须设置镜像",
                "ALIYUN::ESS::ScalingConfiguration",
                "ImageId",
                severity="medium",
            ),
            rule(
                "ess-scaling-rule-adjustment-required",
                "ESS scaling rule must configure adjustment",
                "ESS 伸缩规则必须配置调整方式",
                "ALIYUN::ESS::ScalingRule",
                "AdjustmentType",
                severity="medium",
            ),
            rule(
                "alb-server-group-required",
                "ALB listener must configure server group",
                "ALB 监听必须配置服务器组",
                "ALIYUN::ALB::Listener",
                "DefaultActions",
                severity="medium",
            ),
            rule(
                "slb-listener-backend-required",
                "SLB listener must configure backend server port",
                "SLB 监听必须配置后端端口",
                "ALIYUN::SLB::Listener",
                "BackendServerPort",
                severity="medium",
            ),
            rule(
                "fc-function-instance-concurrency-required",
                "FC function must configure instance concurrency",
                "函数计算函数必须配置实例并发",
                "ALIYUN::FC::Function",
                "InstanceConcurrency",
                severity="medium",
            ),
            rule(
                "fc-function-timeout-required",
                "FC function must configure timeout",
                "函数计算函数必须配置超时时间",
                "ALIYUN::FC::Function",
                "Timeout",
                severity="medium",
            ),
            rule(
                "ack-cluster-worker-vswitches-required",
                "ACK cluster must configure worker VSwitches",
                "ACK 集群必须配置工作节点交换机",
                "ALIYUN::CS::ClusterApplication",
                "WorkerVSwitchIds",
                severity="high",
            ),
            rule(
                "mse-cluster-replicas-required",
                "MSE cluster must configure replicas",
                "MSE 集群必须配置副本数",
                "ALIYUN::MSE::Cluster",
                "Replicas",
                severity="medium",
            ),
        ],
    },
}


HAND_AUTHORED_SECURITY_RULE_IDS = [
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
    "api-gateway-api-internet-request-https",
]


def rego_value(value: Any) -> str:
    if value is True:
        return "true"
    if value is False:
        return "false"
    if value is None:
        return "null"
    if isinstance(value, int | float):
        return str(value)
    return '"' + str(value).replace('"', '\\"') + '"'


def compliance_expression(policy: dict[str, Any]) -> str:
    prop = policy["property"]
    check = policy["check"]
    expected = policy["expected"]
    if check == "required":
        return f'helpers.has_property(resource, "{prop}")'
    if check == "true":
        return f'helpers.get_property(resource, "{prop}", false) == true'
    if check == "false":
        return f'helpers.get_property(resource, "{prop}", false) == false'
    if check == "equals":
        return f'helpers.get_property(resource, "{prop}", "") == {rego_value(expected)}'
    raise ValueError(f"Unsupported check: {check}")


def localized_policy_metadata(
    resource_type: str,
    property_name: str,
    *,
    name_en: str,
    name_zh: str,
    description_en: str,
    description_zh: str,
    reason_en: str,
    reason_zh: str,
    recommendation_en: str,
    recommendation_zh: str,
) -> dict[str, dict[str, str]]:
    return {
        "name": {
            "en": name_en,
            "zh": name_zh,
            "ja": f"{resource_type} には {property_name} を設定する必要があります",
            "de": f"Für {resource_type} muss {property_name} konfiguriert sein",
            "es": f"{resource_type} debe tener {property_name} configurado",
            "fr": f"{resource_type} doit avoir {property_name} configuré",
            "pt": f"{resource_type} deve ter {property_name} configurado",
        },
        "description": {
            "en": description_en,
            "zh": description_zh,
            "ja": f"{resource_type} に {property_name} が設定されていることを確認します",
            "de": f"Prüft, ob {property_name} für {resource_type} konfiguriert ist",
            "es": f"Comprueba que {resource_type} tenga {property_name} configurado",
            "fr": f"Vérifie que {resource_type} a {property_name} configuré",
            "pt": f"Verifica se {resource_type} tem {property_name} configurado",
        },
        "reason": {
            "en": reason_en,
            "zh": reason_zh,
            "ja": f"{resource_type} に {property_name} が設定されていません。",
            "de": f"Für {resource_type} ist {property_name} nicht konfiguriert.",
            "es": f"{resource_type} no tiene {property_name} configurado.",
            "fr": f"{resource_type} n'a pas {property_name} configuré.",
            "pt": f"{resource_type} não tem {property_name} configurado.",
        },
        "recommendation": {
            "en": recommendation_en,
            "zh": recommendation_zh,
            "ja": f"ポリシーを満たすには、{resource_type} に {property_name} を設定してください。",
            "de": f"Konfigurieren Sie {property_name} für {resource_type}, um die Richtlinie zu erfüllen.",
            "es": f"Configure {property_name} en {resource_type} para cumplir la política.",
            "fr": f"Configurez {property_name} sur {resource_type} pour satisfaire la politique.",
            "pt": f"Configure {property_name} em {resource_type} para atender à política.",
        },
    }


def render_translation_map(translations: dict[str, str], *, indent: int = 16) -> str:
    lines = []
    padding = " " * indent
    for index, language in enumerate(POLICY_METADATA_LANGUAGES):
        comma = "," if index < len(POLICY_METADATA_LANGUAGES) - 1 else ""
        lines.append(f'{padding}"{language}": "{translations[language]}"{comma}')
    return "\n".join(lines)


def render_policy(scenario: str, scenario_zh: str, policy: dict[str, Any]) -> str:
    if scenario == "security" and policy["slug"] == "ecs-instance-no-public-ip":
        return render_ecs_no_public_ip_policy(scenario, scenario_zh, policy)

    current_rule_id = rule_id(scenario, policy)
    package_name = current_rule_id.replace("-", "_")
    expression = compliance_expression(policy)
    description_en = f"Checks {policy['en'].removesuffix('.')}"
    reason_en = f"{policy['en'].removesuffix('.')} is not satisfied."
    recommendation_en = f"Configure {policy['property']} on {policy['resource_type']} to satisfy the policy."
    description_zh = f"检查{policy['zh']}"
    reason_zh = f"{policy['zh']}未满足。"
    recommendation_zh = f"请在 {policy['resource_type']} 上配置 {policy['property']} 以满足策略。"
    metadata = localized_policy_metadata(
        policy["resource_type"],
        policy["property"],
        name_en=policy["en"],
        name_zh=policy["zh"],
        description_en=description_en,
        description_zh=description_zh,
        reason_en=reason_en,
        reason_zh=reason_zh,
        recommendation_en=recommendation_en,
        recommendation_zh=recommendation_zh,
    )

    return dedent(
        f'''\
        package infraguard.rules.aliyun.{package_name}

        import rego.v1
        import data.infraguard.helpers

        rule_meta := {{
            "id": "{current_rule_id}",
            "severity": "{policy["severity"]}",
            "name": {{
{render_translation_map(metadata["name"])}
            }},
            "description": {{
{render_translation_map(metadata["description"])}
            }},
            "reason": {{
{render_translation_map(metadata["reason"])}
            }},
            "recommendation": {{
{render_translation_map(metadata["recommendation"])}
            }},
            "resource_types": ["{policy["resource_type"]}"],
        }}

        deny contains result if {{
            some name, resource in helpers.resources_by_type("{policy["resource_type"]}")
            not is_compliant(resource)
            result := {{
                "id": rule_meta.id,
                "resource_id": name,
                "violation_path": ["Properties", "{policy["property"]}"],
                "meta": {{
                    "severity": rule_meta.severity,
                    "reason": rule_meta.reason,
                    "recommendation": rule_meta.recommendation,
                }},
            }}
        }}

        is_compliant(resource) if {{
            {expression}
        }}
        '''
    )


def render_ecs_no_public_ip_policy(scenario: str, scenario_zh: str, policy: dict[str, Any]) -> str:
    current_rule_id = rule_id(scenario, policy)
    package_name = current_rule_id.replace("-", "_")
    metadata = localized_policy_metadata(
        "ALIYUN::ECS::Instance",
        "public exposure",
        name_en=policy["en"],
        name_zh=policy["zh"],
        description_en="Checks ECS public exposure through direct public IP, outbound bandwidth, or EIP association.",
        description_zh="检查 ECS 是否通过公网 IP、出网带宽或 EIP 绑定暴露公网。",
        reason_en="ECS instance is exposed to the public network.",
        reason_zh="ECS 实例存在公网暴露路径。",
        recommendation_en="Disable public IP allocation, set internet outbound bandwidth to 0, and avoid direct EIP association.",
        recommendation_zh="关闭公网 IP 分配，将公网出带宽设为 0，并避免直接绑定 EIP。",
    )
    return dedent(
        f'''\
        package infraguard.rules.aliyun.{package_name}

        import rego.v1
        import data.infraguard.helpers

        rule_meta := {{
            "id": "{current_rule_id}",
            "severity": "{policy["severity"]}",
            "name": {{
{render_translation_map(metadata["name"])}
            }},
            "description": {{
{render_translation_map(metadata["description"])}
            }},
            "reason": {{
{render_translation_map(metadata["reason"])}
            }},
            "recommendation": {{
{render_translation_map(metadata["recommendation"])}
            }},
            "resource_types": ["ALIYUN::ECS::Instance", "ALIYUN::ECS::InstanceGroup"],
        }}

        allocates_public_ip(resource) if {{
            helpers.get_property(resource, "AllocatePublicIP", false) == true
        }}

        has_internet_bandwidth(resource) if {{
            helpers.has_property(resource, "InternetMaxBandwidthOut")
            resource.Properties.InternetMaxBandwidthOut > 0
        }}

        deny contains result if {{
            some name, resource in helpers.resources_by_types(["ALIYUN::ECS::Instance", "ALIYUN::ECS::InstanceGroup"])
            allocates_public_ip(resource)
            result := {{
                "id": rule_meta.id,
                "resource_id": name,
                "violation_path": ["Properties", "AllocatePublicIP"],
                "meta": {{
                    "severity": rule_meta.severity,
                    "reason": rule_meta.reason,
                    "recommendation": rule_meta.recommendation,
                }},
            }}
        }}

        deny contains result if {{
            some name, resource in helpers.resources_by_types(["ALIYUN::ECS::Instance", "ALIYUN::ECS::InstanceGroup"])
            has_internet_bandwidth(resource)
            not allocates_public_ip(resource)
            result := {{
                "id": rule_meta.id,
                "resource_id": name,
                "violation_path": ["Properties", "InternetMaxBandwidthOut"],
                "meta": {{
                    "severity": rule_meta.severity,
                    "reason": rule_meta.reason,
                    "recommendation": rule_meta.recommendation,
                }},
            }}
        }}

        deny contains result if {{
            some name, resource in helpers.resources_by_types(["ALIYUN::ECS::Instance", "ALIYUN::ECS::InstanceGroup"])
            some _, eip_resource in helpers.resources_by_type("ALIYUN::VPC::EIPAssociation")
            instance_id := helpers.get_property(eip_resource, "InstanceId", "")
            helpers.is_referencing(instance_id, name)
            result := {{
                "id": rule_meta.id,
                "resource_id": name,
                "violation_path": ["Properties"],
                "meta": {{
                    "severity": rule_meta.severity,
                    "reason": rule_meta.reason,
                    "recommendation": rule_meta.recommendation,
                }},
            }}
        }}

        deny contains result if {{
            some name, resource in helpers.resources_by_types(["ALIYUN::ECS::Instance", "ALIYUN::ECS::InstanceGroup"])
            some _, eip_resource in helpers.resources_by_type("ALIYUN::VPC::EIPAssociation")
            instance_id := helpers.get_property(eip_resource, "InstanceId", "")
            helpers.is_get_att_referencing(instance_id, name)
            result := {{
                "id": rule_meta.id,
                "resource_id": name,
                "violation_path": ["Properties"],
                "meta": {{
                    "severity": rule_meta.severity,
                    "reason": rule_meta.reason,
                    "recommendation": rule_meta.recommendation,
                }},
            }}
        }}
        '''
    )


def render_pack(scenario: str, spec: dict[str, Any]) -> str:
    package_name = f"iac_code_{scenario.replace('-', '_')}"
    pack_id = f"iac-code-{scenario}"
    pack_rule_ids = [rule_id(scenario, policy) for policy in spec["rules"]]
    if scenario == "security":
        pack_rule_ids.extend(HAND_AUTHORED_SECURITY_RULE_IDS)

    rule_lines = [f'        "{current_rule_id}"' for current_rule_id in pack_rule_ids]
    rules = ",\n".join(rule_lines)
    scenario_en = scenario.replace("-", " ").title()
    description_en = f"Scenario-oriented InfraGuard policies for {scenario_en}."
    description_zh = f"面向{spec['zh']}场景的 InfraGuard 策略组合。"
    if scenario == "security":
        description_en = (
            "Scenario-oriented InfraGuard policies for Security, covering identity, network exposure, "
            "data protection, audit logging, supply chain, and key management."
        )
        description_zh = (
            "面向安全性场景的 InfraGuard 策略组合，覆盖身份、网络公网暴露、数据保护、审计日志、供应链和密钥管理。"
        )

    return (
        f"package infraguard.packs.aliyun.{package_name}\n\n"
        "import rego.v1\n\n"
        "pack_meta := {\n"
        f'    "id": "{pack_id}",\n'
        '    "name": {\n'
        f'        "en": "IaC Code {scenario_en} Scenario Pack",\n'
        f'        "zh": "IaC Code {spec["zh"]}场景合规包",\n'
        "    },\n"
        '    "description": {\n'
        f'        "en": "{description_en}",\n'
        f'        "zh": "{description_zh}",\n'
        "    },\n"
        '    "rules": [\n'
        f"{rules}\n"
        "    ]\n"
        "}\n"
    )


def main() -> None:
    pack_dir = ROOT / "packs"
    pack_dir.mkdir(parents=True, exist_ok=True)
    for existing in pack_dir.glob("*.rego"):
        existing.unlink()

    for scenario in SCENARIOS:
        scenario_dir = ROOT / scenario
        scenario_dir.mkdir(parents=True, exist_ok=True)
        pattern = "security-*.rego" if scenario == "security" else "*.rego"
        for existing in scenario_dir.glob(pattern):
            existing.unlink()

    for scenario, spec in SCENARIOS.items():
        scenario_dir = ROOT / scenario
        for policy in spec["rules"]:
            content = render_policy(scenario, spec["zh"], policy)
            (scenario_dir / f"{scenario}-{policy['slug']}.rego").write_text(content, encoding="utf-8")
        (pack_dir / f"iac-code-{scenario}-pack.rego").write_text(render_pack(scenario, spec), encoding="utf-8")


if __name__ == "__main__":
    main()
