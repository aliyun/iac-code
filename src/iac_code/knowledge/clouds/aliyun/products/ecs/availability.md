# 可用性查询

相关选型：[产品指南](knowledge://clouds/aliyun/products/ecs/ecs.md)。[实例规格与存储推荐](knowledge://clouds/aliyun/products/ecs/sizing.md)。

## 可用性查询

### 1. 查询候选规格的实际 vCPU / 内存

对每个拟采用的 `InstanceType` 调用：

```
aliyun_api(product="ecs", action="DescribeInstanceTypes", params={
    "InstanceTypes.1": "<候选实例规格>"
})
```

以返回的 `CpuCoreCount` 和 `MemorySize`（GiB）作为实际规格证据。用户明确指定 vCPU/内存时，先据此过滤候选；只有实际规格精确满足要求的候选才能继续做库存筛选。不得用场景推荐值替代用户明确值。

### 2. 查询可用实例规格

```
aliyun_api(product="ecs", action="DescribeAvailableResource", params={
    "DestinationResource": "InstanceType",
    "IoOptimized": "optimized",
    "InstanceChargeType": "PostPaid"
})
```

> InstanceChargeType 也可传 "PrePaid"（包年包月）。

返回结果按可用区分组，包含各可用区支持的实例规格列表。

### 3. 查询可用磁盘类型

```
aliyun_api(product="ecs", action="DescribeAvailableResource", params={
    "DestinationResource": "SystemDisk",
    "ZoneId": "<选定的可用区>",
    "InstanceType": "<选定的实例规格>",
    "InstanceChargeType": "PostPaid"
})
```

### 4. 筛选逻辑

1. 从 ROS `AllowedValues` 或可用规格列表中取候选，并用 `DescribeInstanceTypes` 的实际 vCPU/内存先过滤用户硬约束
2. 从返回的可用区列表中，筛出同时满足实例规格族和磁盘类型的可用区
3. 用户未明确 vCPU/内存时，按「实例规格推荐策略」的档位顺序匹配规格族（成本/性价比/性能 三档分别选）
4. 同档位规格族不可用时：
   - **个人 `e` 实例缺货** → 按"性价比优先"档位重新选
   - **其他规格族缺货** → 在同档位的备选规格族列表中按顺序回退（如 `u1` → `u2a` → `u2i`）
   - 仅当用户未明确 vCPU/内存时，整档位都不可用才可选同实例族中最接近的可用规格
   - 用户已明确 vCPU/内存时，不得选择最接近值、升级或降级；报告 `InstanceType` 参数冲突
5. 云盘类型按「存储配置推荐」生成，并以可用磁盘类型查询结果做最终校验
