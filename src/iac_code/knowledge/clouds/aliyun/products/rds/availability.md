# 可用性查询

相关选型：[产品指南](knowledge://clouds/aliyun/products/rds/rds.md)。

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
