# IaC 资源定位

相关选型：[产品指南](knowledge://clouds/aliyun/products/ga/ga.md)。

## IaC 资源定位

| 能力 | ROS 查询锚点 | Terraform 查询锚点 |
|------|--------------|--------------------|
| 标准型实例 | `ALIYUN::GA::Accelerator` | `alicloud_ga_accelerator` |
| 监听 | `ALIYUN::GA::Listener` | `alicloud_ga_listener` |
| EIP 加速地域 | `ALIYUN::GA::IpSets` | `alicloud_ga_ip_set` |
| 智能路由终端节点组 | `ALIYUN::GA::EndpointGroup` / `EndpointGroups` | `alicloud_ga_endpoint_group` |
| 转发规则与 ACL | `ALIYUN::GA::ForwardingRules`、`Acl`、`AclsListenerAssociation` | 查询 `alicloud_ga_*` 当前对应资源 |
| 基础带宽包及绑定 | `ALIYUN::GA::BandwidthPackage`、`BandwidthPackageAcceleratorAddition` | `alicloud_ga_bandwidth_package`、`alicloud_ga_bandwidth_package_attachment` |
| 基础型 | `ALIYUN::GA::Basic*` | `alicloud_ga_basic_*` |
| 自定义路由 | 查询当前 `ALIYUN::GA::*CustomRouting*` 及关联 Schema | 查询当前 `alicloud_ga_custom_routing_*` 完整资源链 |

资源名仅用于定位。选定 IaC 后，只生成该 Schema 当前返回的资源、属性、枚举和必填字段；不存在直接映射时报告差异，不做名称推导。
