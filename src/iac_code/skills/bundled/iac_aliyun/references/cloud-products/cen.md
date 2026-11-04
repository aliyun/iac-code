# CEN 云企业网参考

## 资源逻辑 ID 约束

用户明确指定的资源逻辑 ID 应逐字保留，`Ref` / `Fn::GetAtt` 必须使用这些 ID，不得自行改名、添加后缀或用示例名称替代。

## 资源类型

- `ALIYUN::CEN::CenInstance`：创建 CEN 实例

## ALIYUN::CEN::CenInstance 属性约束

| 属性 | 类型 | 必填 | 说明 |
|------|------|------|------|
| CenName | String | 否 | CEN 实例名称 |
| Description | String | 否 | CEN 实例描述 |

## 输出属性（Fn::GetAtt）

| 属性 | 说明 |
|------|------|
| CenId | CEN 实例 ID，格式以 `cen-` 开头 |

## 资源引用约束

- CEN 实例为全局资源，不绑定特定地域或可用区，无需 ZoneId 等库存相关参数。
- 创建 CEN 实例本身不依赖 VPC、VSwitch 等附属资源；仅在需要加载网络实例时才声明对应资源。

## 可用性查询

CEN 实例创建不涉及库存或可用区限制，无需执行可用性查询。

## 常见误区

- **属性名混淆**：CEN 实例名称属性为 `CenName`，不是 `Name`。使用前务必通过 `GetResourceType` 确认 Schema。
- **过度创建附属资源**：任务仅要求创建 CEN 实例时，不要额外添加 BandwidthPackage、TransitRouter、CenBandwidthLimit 等资源。
- **硬编码 ID**：禁止在模板中写入固定的 CEN 实例 ID；输出应始终通过 `Fn::GetAtt` 动态获取。
