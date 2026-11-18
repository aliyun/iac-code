# 可用性查询

相关选型：[产品指南](knowledge://clouds/aliyun/products/redis/redis.md)。

## 可用性查询

### 查询可用 Redis 规格

```
aliyun_api(product="r-kvstore", action="DescribeAvailableResource", params={
    "InstanceChargeType": "PostPaid",
    "Engine": "Redis"
})
```

> InstanceChargeType 也可传 "PrePaid"（包年包月）。

### 筛选逻辑

1. 从返回结果中，找出可用的实例规格和可用区
2. 按「规格推荐」表优先匹配
3. 推荐规格不可用时，选同内存量级中最接近的可用规格
