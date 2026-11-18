# 阿里云 ECS 选型指南

## 相关知识

- [实例规格推荐策略](knowledge://clouds/aliyun/products/ecs/sizing.md)
- [存储配置推荐](knowledge://clouds/aliyun/products/ecs/sizing.md)
- [费用估算](knowledge://clouds/aliyun/products/ecs/describe-price.md)
- [可用性查询](knowledge://clouds/aliyun/products/ecs/availability.md)
- [CPU 与实例规格事实](knowledge://clouds/aliyun/products/ecs/hardware.md)
- [SGX/TDX 阿里云支持范围](knowledge://clouds/aliyun/products/ecs/confidential-computing.md)
- [GPU 识别与可售范围](knowledge://clouds/aliyun/products/ecs/gpu-availability.md)
- [SGX/TDX 通用原理](knowledge://common/hardware/confidential-computing.md)
- [Intel Arc Pro B70](knowledge://common/hardware/intel-arc-pro-b70.md)

## 镜像选择

- **Ubuntu 24.04**：默认首选，社区支持好，生态丰富，优先使用 x86_64 镜像
- **Alibaba Cloud Linux 4**：仅在用户明确要求或存在兼容性约束时使用
- **CentOS Stream 9**：兼容 RHEL，适合企业场景
- **Windows Server 2025**：Windows 工作负载

调用 DescribeImages 可指定 ImageName 进行模糊搜索。默认优先搜索 Ubuntu 24.04，
例如 `ImageName="ubuntu_24_04_x64_*"`；不要直接复用过期的固定日期镜像 ID，
应使用查询结果中当前地域和实例规格可用的镜像。

## 网络带宽

- 默认按流量计费：适合流量不稳定的业务
- 按带宽计费：适合流量持续稳定、高带宽场景
- 公网 IP 可选按量购买 EIP，支持弹性绑定/解绑
- ECS 公网能力按场景最小化：普通 ECS 无明确公网需求时不创建 EIP，并显式设置 `AllocatePublicIP: false`；网站、公开 API 等场景配置一种必要的公网入口；通过 EIP 或公网负载均衡提供入口时，后端 ECS 同样设置 `AllocatePublicIP: false`，避免重复公网入口。EIP 绑定约束由模板校验硬门禁强制执行。
- `ALIYUN::ECS::Instance` 和 `ALIYUN::ECS::InstanceGroup` 必须在 `Properties` 中显式设置 `VSwitchId`（引用新建或已有交换机），不得省略后回退到经典网络，此约束由模板校验硬门禁强制执行。

## 安全组最佳实践

**默认不开 22/3389**——用 RunCommand/Invocation 部署即可，业务只开必要端口（Web → 80/443，层间通信 → `SourceGroupId`）。

**须开 22/3389 时**（用户明确要求 SSH 或含跳板机）：`SourceCidrIp` 必须限定来源，严禁 `0.0.0.0/0`：
- 执行 `curl -s https://api.ipify.org` 取当前公网 IP，写成 `<ip>/32`
- 获取失败 → 请用户提供白名单 IP，不得回退到 `0.0.0.0/0`

## 登录凭证

**默认不写入 Password 和 KeyPairName**——模板中保持二者均不设置，应用部署通过 RunCommand/Invocation 执行命令，无需登录凭证。

## 在实例中执行 Shell 命令

- ROS 模板：参见 [ros-template.md](knowledge://clouds/aliyun/products/ros/ros-template.md)「在实例中执行命令」章节
- Terraform：参见 [terraform-template.md](knowledge://clouds/aliyun/products/ros/terraform-template.md)「在实例中执行命令」章节

## 高可用架构建议

- 生产环境：至少 2 台 ECS，分布在不同可用区
- 结合 SLB 做负载均衡
- 使用弹性伸缩（ESS）应对流量波动
- 重要数据存 RDS/OSS，避免依赖本地磁盘

## 库存相关属性（模板中须参数化为 Parameters）

| 属性 | 说明 |
|------|------|
| ZoneId | 可用区 |
| InstanceType | 实例规格 |
| ImageId | 镜像 ID（可用镜像受 InstanceType 影响） |
| SystemDiskCategory | 系统盘类型 |
| DataDiskCategory | 数据盘类型（如有数据盘） |
