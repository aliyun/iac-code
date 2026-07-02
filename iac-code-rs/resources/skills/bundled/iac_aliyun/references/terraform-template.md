# Terraform 模板最佳实践

## 文件组织

| 规模 | 组织方式 |
|------|----------|
| < 10 资源 | 单 `main.tf` |
| 10-20 资源 | `provider.tf` + `variables.tf` + `main.tf` + `outputs.tf` |
| > 20 资源 | 按资源类型拆分：`vpc.tf`、`ecs.tf`、`rds.tf` 等 |

## 变量管理

### 使用 validation 约束输入

```hcl
variable "env" {
  type        = string
  description = "环境标识"
  validation {
    condition     = contains(["dev", "staging", "prod"], var.env)
    error_message = "env 必须为 dev、staging 或 prod"
  }
}
```

### 使用 locals 减少重复

```hcl
locals {
  name_prefix = "${var.env}-${var.project}"
  common_tags = {
    Environment = var.env
    ManagedBy   = "terraform"
  }
}
```

## Data Source

使用 data source 查询动态信息，避免硬编码：

```hcl
data "alicloud_zones" "available" {
  available_resource_creation = "VSwitch"
}

data "alicloud_images" "ubuntu" {
  name_regex  = "^ubuntu_22"
  owners      = "system"
  most_recent = true
}
```

## 命名约定

- 资源名：`{env}-{product}-{role}`，如 `prod-ecs-web`
- 变量名：蛇形命名，如 `instance_type`
- 资源标识符：用途而非类型，如 `alicloud_instance.web` 而非 `alicloud_instance.instance1`

## 执行公共命令

`alicloud_ecs_commands` data source 查出 command_id，再用 `alicloud_ecs_invocation` 执行

```hcl
# 公共命令：先查 command_id
data "alicloud_ecs_commands" "install_openclaw" {
  name              = "ACS-ECS-InstallOpenClaw-for-linux.sh"
  command_provider  = "AlibabaCloud"
}

# 再执行
resource "alicloud_ecs_invocation" "install_openclaw" {
  command_id  = data.alicloud_ecs_commands.install_openclaw.commands.0.id
  instance_id = [alicloud_instance.web.id]
}
```

## 与 ROS 集成

ROS 支持通过 Terraform 类型模板部署。流程：

1. 生成 Terraform 文件时，按 [template-parameters.md](template-parameters.md) 的「Terraform 模板的参数规范」：
   - 变量 description 中写入 AssociationProperty、Label 等（JSON 格式）
   - 在 tf 目录下创建 `.metadata` 文件定义 ParameterGroups
2. 运行 `python ../scripts/tf2ros.py <terraform_dir> <output.yml>` 生成 ROS 模板文件
   - 递归打包目录下所有文件（`.tf`、`.metadata`、`scripts/*.sh`、子目录任意文本文件等）到 Workspace
   - 自动跳过 `.terraform/`、`.git/`、`__pycache__/` 目录及 `*.tfstate*` 文件
3. 读取模板文件内容，调用 ros_stack(CreateStack) 部署

## 文件函数的 path 参数限制（重要）

`file`、`fileexists`、`fileset`、`filebase64` 的 path 参数受 ROS 严格校验，生成模板时必须遵守：

- 必须是字面量字符串，不能引用变量
- 第一个分词必须是 `${path.module}`、`${path.root}`、`${path.cwd}` 或 `${terraform.workspace}` 之一
- 后续分词只允许字母、数字与 `-_.`，不允许为空、`.` 或 `..`（即不能用 `../` 跳出目录）
- 引用的文件必须存在于 Workspace 中（即在传给 tf2ros.py 的目录里）

## 其他模板结构限制

Terraform 类型模板的其他结构性约束（provider 白名单、不支持的语法、变量类型等）以官方文档为准：<https://help.aliyun.com/zh/ros/user-guide/structure-of-terraform-templates>。ValidateTemplate 报错若指向模板结构问题，按报错信息对照该文档定位修复点。
