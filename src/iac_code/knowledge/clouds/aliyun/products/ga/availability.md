# 运行时查询与筛选

相关选型：[产品指南](knowledge://clouds/aliyun/products/ga/ga.md)。

## 运行时查询与筛选

GA OpenAPI 使用文档指定的控制面地域（当前为 `cn-hangzhou`，调用前以最新 API 文档为准）。

| 用途 | API | 关键输入 |
|------|-----|----------|
| 标准型加速地域 | `ListAvailableAccelerateAreas` | 可选 `AcceleratorId`；查询 Anycast 时按当前 API 传 `AccessMode` |
| 终端节点可用地域 | `ListAvailableBusiRegions` | 已有实例时传 `AcceleratorId` |
| 公网线路类型 | `ListIspTypes` | `BusinessRegionId`、实例类型和可选实例 ID |
| 源站验证 | ECS、VPC、SLB、OSS 等对应产品 API | 资源 ID、地域、VPC/VSwitch、地址 |

筛选顺序：

1. 用户指定的实例类型、付费模式、地域、协议、端口、IP 版本和源站类型均为硬约束。
2. 查询所选 IaC 的当前资源 Schema，确认完整资源链、必填属性和枚举。
3. 查询加速地域、终端节点地域和线路的当前可用性，并验证源站资源与地域一致。
4. 能力需要额外资格或 Schema 缺失时，报告限制并给出可选实现；不得静默换 IaC、地域、协议或产品类型。
5. 没有满足全部硬约束的组合时，说明冲突并请用户调整约束。
