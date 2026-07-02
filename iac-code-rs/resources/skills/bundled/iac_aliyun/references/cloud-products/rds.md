# 阿里云 RDS 选型指南

## 引擎选择

| 引擎 | 推荐场景 | 说明 |
|------|----------|------|
| MySQL | Web 应用、电商、SaaS | 生态最广，开源友好 |
| PostgreSQL | 复杂查询、GIS、分析 | 功能最全，支持 JSON、扩展 |
| SQL Server | .NET 应用、企业系统 | Windows 生态，需授权费 |
| MariaDB | MySQL 替代 | 兼容 MySQL，开源 |

**推荐选择**：优先 MySQL 8.0 或 PostgreSQL 15，除非有特殊需求。

## 版本推荐

- MySQL：**8.0**（长期支持，性能优于 5.7）
- PostgreSQL：**15** 或 **16**
- SQL Server：2019 Enterprise/Standard

## 系列（Edition）

| 系列 | 可用区 | 适用 | 说明 |
|------|--------|------|------|
| 基础版（Basic） | 单 AZ | 开发/测试 | 无主备，成本低，不建议生产用 |
| 高可用版（HA） | 双 AZ | 生产 | 主备架构，自动切换，推荐 |
| 三节点企业版（Cluster） | 三 AZ | 金融/关键系统 | 一主两备，RPO=0 |

## 规格推荐

| 场景 | 推荐规格 | 存储 |
|------|----------|------|
| 开发/测试 | rds.mysql.s1.small (1c2g) | 20 GB |
| 小型应用 | mysql.n2.medium.1 (2c4g) | 100 GB |
| 中型应用 | mysql.n4.large.1 (4c8g) | 200 GB |
| 高并发/大数据 | mysql.n8.xlarge.1 (8c32g) | 500+ GB |

## 存储类型

- **ESSD PL1**：推荐，高 IO，适合大多数生产场景
- **SSD**：性能略低，成本低，适合测试环境
- **ESSD PL2/PL3**：极高 IO，适合高并发写入场景

## 网络配置

- 生产环境：只开放内网访问，禁止公网直连
- 开发/测试：可临时开放公网，IP 白名单严格限制
- 使用安全组或 IP 白名单控制访问来源

## 备份策略

- 自动备份：开启，保留 7 天以上
- 备份时间：业务低峰期（如凌晨 2-4 点）
- 跨地域备份：关键数据开启，防止地域级灾难

## 账户类型与权限

- `MasterUserType: Super`：超级账户，自动拥有所有数据库的全部权限，**不需要**创建 `ALIYUN::RDS::AccountPrivilege` 资源
- `MasterUserType: Normal`：普通账户，需通过 `ALIYUN::RDS::AccountPrivilege` 单独授权数据库访问权限

## 最佳实践

- 连接池：应用层使用连接池（如 HikariCP、pgBouncer），避免连接数暴涨
- 慢查询：开启慢查询日志，阈值 1 秒
- 参数调优：生产环境根据业务调整 `innodb_buffer_pool_size` 等核心参数
- 只读实例：读多写少场景添加只读实例分担压力

## 库存相关属性（模板中须参数化为 Parameters）

| 属性 | 说明 |
|------|------|
| ZoneId | 可用区 |
| DBInstanceClass | 数据库实例规格 |
| DBInstanceStorageType | 存储类型 |

## 可用性查询

### 查询可用 RDS 规格

```
aliyun_api(product="rds", action="DescribeAvailableClasses", params={
    "Engine": "MySQL",
    "EngineVersion": "8.0",
    "DBInstanceStorageType": "cloud_essd",
    "Category": "HighAvailability",
    "CommodityCode": "bards"
})
```

> Engine 可选 MySQL / PostgreSQL / SQLServer / MariaDB。Category 可选 Basic / HighAvailability / cluster。CommodityCode 按量付费用 bards，包年包月用 rds。

### 筛选逻辑

1. 从返回结果中，找出可用的实例规格和可用区
2. 按「规格推荐」表优先匹配
3. 推荐规格不可用时，选同系列中最接近 vCPU/内存配置的可用规格
