# ROS 模板参数规范

本规范适用于所有通过 ROS 部署的模板。Terraform 模板通过 tf2ros 转换后，同样在生成的 ROS 模板中添加 Parameters 和 Metadata。

## 参数化要求

库存相关的 Parameters 不设 Default 和 AllowedValues，部署前通过可用性查询确定。

选填参数设置 `Default: null`，用户可跳过不填。

敏感参数设置 NoEcho: true。

## AssociationProperty

所有 Parameters **必须**添加 AssociationProperty，让 ROS 控制台自动关联候选值。通过 AssociationPropertyMetadata 实现参数间联动过滤。

### 高频参考表

| 参数用途 | AssociationProperty | 关键 Metadata |
|---------|-------------------|--------------|
| 地域 | ALIYUN::ECS::RegionId | - |
| ECS 可用区 | ALIYUN::ECS::ZoneId | RegionId |
| ECS 实例规格 | ALIYUN::ECS::Instance::InstanceType | RegionId, ZoneId, InstanceChargeType |
| ECS 镜像 | ALIYUN::ECS::Image::ImageId | RegionId, InstanceType |
| 系统盘类型 | ALIYUN::ECS::Disk::SystemDiskCategory | RegionId, ZoneId, InstanceType |
| 数据盘类型 | ALIYUN::ECS::Disk::DataDiskCategory | RegionId, ZoneId, InstanceType |
| VPC | ALIYUN::ECS::VPC::VPCId | RegionId |
| 交换机 | ALIYUN::VPC::VSwitch::VSwitchId | RegionId, ZoneId, VpcId |
| 安全组 | ALIYUN::ECS::SecurityGroup::SecurityGroupId | RegionId, VpcId |
| 密钥对 | ALIYUN::ECS::KeyPair::KeyPairName | RegionId |
| 密码 | ALIYUN::ECS::Instance::Password | - |
| RDS 引擎 | ALIYUN::RDS::Engine::EngineId | - |
| RDS 引擎版本 | ALIYUN::RDS::Engine::EngineVersion | Engine |
| RDS 实例规格 | ALIYUN::RDS::Instance::InstanceType | RegionId, ZoneId, Engine, EngineVersion, DBInstanceStorageType, Category |
| Redis 实例规格 | ALIYUN::Redis::Instance::InstanceType | RegionId, ZoneId, InstanceChargeType |
| SLB 实例规格 | ALIYUN::SLB::Instance::InstanceType | RegionId, ZoneId |
| 付费类型 | ChargeType | - |

> 不在此表中的 AssociationProperty，使用 aliyun_doc_search(keywords="AssociationProperty <产品名>", category_id=28850) 搜索 ROS 文档获取。

## Label

为每个 Parameter 添加 Label，提供中英文显示名：

```yaml
Label:
  en: Zone ID
  zh-cn: 可用区
```

## 参数分组

使用 Metadata 的 ParameterGroups 将参数按逻辑分组，提升控制台体验：

```yaml
Metadata:
  ALIYUN::ROS::Interface:
    ParameterGroups:
      - Parameters:
          - VpcId
          - ZoneId
          - VSwitchId
        Label:
          default: 网络配置
      - Parameters:
          - InstanceType
          - SystemDiskCategory
        Label:
          default: ECS 配置
      - Parameters:
          - DBInstanceClass
          - DBInstanceStorageType
        Label:
          default: RDS 配置
```

所有 Parameters 都应归入某个分组，按资源类型或功能模块分类。

## 完整示例

```yaml
ROSTemplateFormatVersion: '2015-09-01'
Metadata:
  ALIYUN::ROS::Interface:
    ParameterGroups:
      - Parameters:
          - ZoneId
          - VpcId
          - VSwitchId
        Label:
          default: 网络配置
      - Parameters:
          - InstanceType
          - SystemDiskCategory
        Label:
          default: ECS 配置
Parameters:
  ZoneId:
    Type: String
    Label:
      en: Zone ID
      zh-cn: 可用区
    AssociationProperty: ALIYUN::ECS::ZoneId
    AssociationPropertyMetadata:
      RegionId: ${ALIYUN::Region}
  VpcId:
    Type: String
    Label:
      en: VPC
      zh-cn: 专有网络
    AssociationProperty: ALIYUN::ECS::VPC::VPCId
    AssociationPropertyMetadata:
      RegionId: ${ALIYUN::Region}
  VSwitchId:
    Type: String
    Label:
      en: VSwitch
      zh-cn: 交换机
    AssociationProperty: ALIYUN::VPC::VSwitch::VSwitchId
    AssociationPropertyMetadata:
      ZoneId: ${ZoneId}
      VpcId: ${VpcId}
  InstanceType:
    Type: String
    Label:
      en: Instance Type
      zh-cn: 实例规格
    AssociationProperty: ALIYUN::ECS::Instance::InstanceType
    AssociationPropertyMetadata:
      ZoneId: ${ZoneId}
      InstanceChargeType: PostPaid
  SystemDiskCategory:
    Type: String
    Label:
      en: System Disk Category
      zh-cn: 系统盘类型
    AssociationProperty: ALIYUN::ECS::Disk::SystemDiskCategory
    AssociationPropertyMetadata:
      ZoneId: ${ZoneId}
      InstanceType: ${InstanceType}
```

Metadata 中用 `${ParamName}` 引用其他参数，`${ALIYUN::Region}` 引用栈所在地域。

## Terraform 模板的参数规范

Terraform 模板通过 ROS 部署时，参数元信息通过以下方式定义（不需要转换后再修改 ROS 模板）：

### 变量级：description 中嵌入 JSON

AssociationProperty、AssociationPropertyMetadata、Label 写在 variable 的 `description` 中：

```hcl
variable "zone_id" {
  type = string
  description = <<EOT
  {
    "AssociationProperty": "ALIYUN::ECS::ZoneId",
    "Label": {
      "en": "Zone ID",
      "zh-cn": "可用区"
    }
  }
  EOT
}

variable "instance_type" {
  type = string
  description = <<EOT
  {
    "AssociationProperty": "ALIYUN::ECS::Instance::InstanceType",
    "AssociationPropertyMetadata": {
      "ZoneId": "$${zone_id}",
      "InstanceChargeType": "$${instance_charge_type}"
    },
    "Label": {
      "en": "Instance Type",
      "zh-cn": "实例规格"
    }
  }
  EOT
}
```

> description 必须是合法 JSON。Metadata 中用 `$${var_name}` 引用其他变量（双 `$$` 避免 Terraform 插值）。

### 模板级：.metadata 文件

在 Terraform 目录下创建 `.metadata` 文件（JSON 格式），结构与 ROS Metadata 完全一致，用于定义 ParameterGroups 等。该文件会被 tf2ros.py 自动收入 Workspace 中（这是 ROS Terraform 模板的标准做法）：

```json
{
  "ALIYUN::ROS::Interface": {
    "ParameterGroups": [
      {
        "Parameters": ["zone_id", "vpc_id", "vswitch_id"],
        "Label": {"default": "网络配置"}
      },
      {
        "Parameters": ["instance_type", "system_disk_category"],
        "Label": {"default": "ECS 配置"}
      }
    ]
  }
}
```
