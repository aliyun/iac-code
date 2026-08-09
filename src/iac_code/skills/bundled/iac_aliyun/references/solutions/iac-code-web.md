# iac-code Web 单 ECS 部署参考

仅当 `candidate.name` 精确等于 `iac-code-web-single-ecs` 时使用本文件。生成 ROS 模板时直接以
同目录的 `iac-code-web.ros.yml` 为基线，只允许调整可用区、实例规格、系统盘类型、EIP 带宽、
访问 CIDR。不得增加 HTTPS、Nginx、域名、SLB、WAF、多实例或已有百炼
API Key 参数。

## 资源与网络

- 保留模板中的单个 VPC、VSwitch、安全组、ECS、EIP/EIPAssociation、
  `ALIYUN::RAM::Role`、`ALIYUN::ECS::RamRoleAttachment`、`ALIYUN::Bailian::ApiKey`
  和同步 `ALIYUN::ECS::RunCommand`。
- ECS 使用 Alibaba Cloud Linux 3 x86_64，固定 `AllocatePublicIP: false`，公网入口只使用绑定的
  EIP。
- 安全组只开放 TCP 8766，来源使用 `AccessCidr`；不要开放 SSH 端口。
- RAM Role 只信任 `ecs.aliyuncs.com`，附加 `AdministratorAccess` 以支持通用云资源查询和部署，
  并通过 `ALIYUN::ECS::RamRoleAttachment` 绑定到 ECS。
- 百炼 API Key 由 ROS 创建，只允许该 EIP 调用，不要把 API Key 放入 Stack Outputs。

## Bootstrap

- 使用 Python 3.11 虚拟环境，并从阿里云 PyPI 镜像安装部署时最新的 `iac-code[http]`。
- 生成 Web 访问 Token 并以 0600 权限保存到 ECS；systemd 使用 `--access-token-file`、
  `--host 0.0.0.0 --port 8766 --no-open` 以 root 用户启动 iac-code Web。
- `IAC_CODE_CONFIG_DIR` 固定为 `/root/.iac-code`。Bootstrap 将百炼 API Key 写入
  `.credentials.yml`，将阿里云云凭证默认配置为当前 ECS 绑定角色对应的 `EcsRamRole`，并在
  `settings.yml` 中配置 DashScope（默认模型 `qwen3.8-max`）、自动 Memory、中文 UI、
  `selling` Pipeline、普通会话模式和 `bypass_permissions`；`userID` 使用 `ALIYUN::TenantId`。
- Bootstrap 在 `/root/AGENTS.md` 注入当前 ECS 的实例 ID、规格、地域、可用区、VPC、
  VSwitch、安全组、EIP 和 ECS 控制台详情链接。iac-code 将未明确指定目标的 ECS 查询、
  系统运维和负载检查理解为针对本机；可能中断当前 Web 的本机停机、重启、释放、网络和
  安全组变更必须先由用户确认。
- Bootstrap 必须同步等待本机 `/health` 成功；详细命令直接沿用 Golden YAML，不要重新生成。

## Stack Outputs

保持 Golden YAML 的四个输出：`PublicUrl`、`WebAccessToken`、`InstanceId` 和 `EipAddress`。
`PublicUrl` 使用 EIP 的 HTTP 8766 地址，`WebAccessToken` 来自同步 Bootstrap 的执行结果。
