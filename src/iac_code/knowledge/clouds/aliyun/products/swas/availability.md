# 套餐与镜像查询

相关选型：[产品指南](knowledge://clouds/aliyun/products/swas/swas.md)。

## 套餐与镜像查询

### 查询目标地域的套餐

```python
aliyun_api(product="SWAS-OPEN", action="ListPlans", region_id="<目标地域>")
```

根据套餐字段筛选配置，保留对应的 `PlanId`、价格及币种。套餐列表描述规格和参考价格，不代表已经完成库存或创建可行性检查。

### 查询目标地域的镜像

```python
aliyun_api(product="SWAS-OPEN", action="ListImages", params={
    "ImageType": "system"
}, region_id="<目标地域>")
```

`ImageType` 可选 `system`（系统镜像）、`app`（应用镜像）或 `custom`（自定义镜像）；根据镜像的 `Platform`、名称和描述匹配套餐平台与应用依赖。
