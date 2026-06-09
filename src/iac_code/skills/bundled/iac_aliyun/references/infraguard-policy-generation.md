# InfraGuard 合规策略生成

本文件定义 IaC Code 的 InfraGuard 策略生成能力。`SKILL.md` 只保留入口说明，详细策略生成规则、策略资产目录和 Rego 写法以本文为准。

## 能力目标

- 当用户要求“生成合规策略”“写 InfraGuard 规则”“用 Rego 检查模板”“策略校验”等时，生成 InfraGuard 可运行的 Rego 策略。
- 支持按多维度生成或组合策略，包括：安全性、高可用、成本优化、合规性、最佳实践、可运维性、网络架构、弹性能力。
- IaC Code 需要生成 100+ 个 InfraGuard 策略，覆盖 8 个场景，作为面向不同用户需求的通用合规策略资产。
- 已生成的策略资产位于 [references/infraguard-policies/](infraguard-policies/)，按场景目录组织。
- 每个场景都提供一个 InfraGuard pack，位于 [references/infraguard-policies/packs/](infraguard-policies/packs/)，用于按场景快速组合对应规则；安全类规则统一放在 `security` 场景目录。

## 策略资产选择

- 需要快速落地：从已生成策略资产中选择匹配策略。
- 需要按场景执行：优先选择对应的 `iac-code-<scenario>` pack。
- 需要组织专属约束：在通用策略基础上生成或改写自定义 Rego 规则。
- 需要覆盖多个治理目标：组合多个场景下的策略，并补充缺失规则。

## 策略维度

- **安全性**：公网暴露、弱访问控制、未启用加密、未启用审计、敏感参数未隐藏、RAM 权限过宽。
- **高可用**：多可用区、多个后端、多节点、主备或集群架构、关键服务避免单点。
- **成本优化**：实例规格边界、闲置资源、带宽上限、预付费到期、快照/日志保留周期。
- **合规性**：MLPS、ISO 27001、PCI-DSS、SOC 2、NIST 800-53 等合规包或组织控制项。
- **最佳实践**：阿里云 Well-Architected、安全组、备份、资源保护、平台安全等最佳实践。
- **可运维性**：日志、监控、审计、追踪、备份、删除保护、自动快照、告警所需配置。
- **网络架构**：VPC 内网化、公网入口收敛、ACL、安全组、负载均衡、多地域/多可用区连接。
- **弹性能力**：ESS 弹性伸缩、多交换机、自动扩缩容、负载均衡绑定、容量与规格约束。

## 生成流程

1. 识别目标 IaC 类型：
   - **ROS**：检查 ROS YAML/JSON 模板，资源类型形如 `ALIYUN::ECS::Instance`。
   - **Terraform**：检查 Terraform alicloud provider 配置，资源类型形如 `alicloud_instance`。
   - InfraGuard 官方当前主要支持 Alibaba Cloud ROS 模板。用户未指定时，默认生成 ROS 规则。
   - 若请求同时覆盖 ROS 和 Terraform，分别生成两个 `.rego` 文件，不要混写在同一个 package 中。
2. 根据自然语言提炼：策略维度、规则 ID、严重级别、适用资源类型、违规条件、修复建议、违规路径。
3. 优先复用官方 InfraGuard 规则风格：
   - `package` 使用 `infraguard.rules.aliyun.<rule_name_snake_case>`。
   - `rule_meta.id` 使用不带场景前缀的 kebab-case，例如 `ecs-running-instance-no-public-ip`。
   - `rule_meta` 使用官方字段：`id`、`severity`、`name`、`description`、`reason`、`recommendation`、`resource_types`。
   - `name`、`description`、`reason`、`recommendation` 尽量提供 `en`、`zh`、`ja`、`de`、`es`、`fr`、`pt` 七种语言，至少不要低于官方同类规则的语言覆盖。
   - 不添加 InfraGuard 官方没有使用的自定义 metadata 字段，例如 `dimension`。
4. 将策略写入 `.rego` 文件，文件名使用 kebab-case，例如 `ecs-running-instance-no-public-ip.rego`。
5. 如本地存在 `infraguard` 命令，生成后运行 `infraguard policy validate <策略文件>` 校验；失败时修复并重试。
6. 若 `infraguard` 命令不存在，告知用户可用该命令验证，不要阻塞生成。

## ROS Rego 结构

```rego
package infraguard.rules.aliyun.<rule_name_snake_case>

import rego.v1
import data.infraguard.helpers

rule_meta := {
    "id": "<rule-id-kebab-case>",
    "severity": "high",
    "name": {
        "en": "<English name>",
        "zh": "<中文名称>",
        "ja": "<Japanese name>",
        "de": "<German name>",
        "es": "<Spanish name>",
        "fr": "<French name>",
        "pt": "<Portuguese name>"
    },
    "description": {
        "en": "<what this checks>",
        "zh": "<检查内容>",
        "ja": "<Japanese description>",
        "de": "<German description>",
        "es": "<Spanish description>",
        "fr": "<French description>",
        "pt": "<Portuguese description>"
    },
    "reason": {
        "en": "<why it failed>",
        "zh": "<违规原因>",
        "ja": "<Japanese reason>",
        "de": "<German reason>",
        "es": "<Spanish reason>",
        "fr": "<French reason>",
        "pt": "<Portuguese reason>"
    },
    "recommendation": {
        "en": "<how to fix>",
        "zh": "<修复建议>",
        "ja": "<Japanese recommendation>",
        "de": "<German recommendation>",
        "es": "<Spanish recommendation>",
        "fr": "<French recommendation>",
        "pt": "<Portuguese recommendation>"
    },
    "resource_types": ["ALIYUN::<Product>::<Type>"],
}

deny contains result if {
    some name, resource in helpers.resources_by_type("ALIYUN::<Product>::<Type>")
    not is_compliant(resource)
    result := {
        "id": rule_meta.id,
        "resource_id": name,
        "violation_path": ["Properties", "<PropertyName>"],
        "meta": {
            "severity": rule_meta.severity,
            "reason": rule_meta.reason,
            "recommendation": rule_meta.recommendation,
        },
    }
}

is_compliant(resource) if {
    helpers.get_property(resource, "<PropertyName>", "<default>") == "<allowed-value>"
}
```

## Terraform Rego 结构

```rego
package infraguard.rules.terraform.<rule_name_snake_case>

import rego.v1
import data.infraguard.helpers.terraform as tf

rule_meta := {
    "id": "<rule-id-kebab-case>",
    "severity": "high",
    "name": {"en": "<English name>", "zh": "<中文名称>"},
    "description": {"en": "<what this checks>", "zh": "<检查内容>"},
    "reason": {"en": "<why it failed>", "zh": "<违规原因>"},
    "recommendation": {"en": "<how to fix>", "zh": "<修复建议>"},
    "resource_types": ["alicloud_<resource_type>"],
    "iac_type": "terraform",
}

deny contains result if {
    some name, resource in tf.resources_by_type("alicloud_<resource_type>")
    value := tf.get_attribute(resource, "<attribute_name>", "<default>")
    not tf.is_unknown(value)
    not is_compliant(value)
    result := {
        "id": rule_meta.id,
        "resource_id": sprintf("alicloud_<resource_type>.%s", [name]),
        "meta": {
            "severity": rule_meta.severity,
            "reason": rule_meta.reason,
            "recommendation": rule_meta.recommendation,
        },
    }
}

is_compliant(value) if {
    value == "<allowed-value>"
}
```

## 规则要求

- `package` 使用下划线，不使用连字符；`rule_meta.id` 和文件名使用 kebab-case。
- `rule_meta` 至少包含 `id`、`severity`、`name`、`description`、`reason`、`recommendation`、`resource_types`；Terraform 规则额外包含 `"iac_type": "terraform"`。
- `severity` 只使用 `high`、`medium`、`low`。
- `deny contains result if` 的 `result` 必须包含 `id`、`resource_id`、`meta`；ROS 规则尽量包含 `violation_path`。
- ROS 规则优先使用 `helpers.resources_by_type`、`helpers.resources_by_types`、`helpers.has_property`、`helpers.get_property`、`helpers.is_true`、`helpers.is_false`、`helpers.is_public_cidr`、`helpers.is_referencing`、`helpers.is_get_att_referencing`。
- Terraform 规则优先使用 `tf.resources_by_type`、`tf.get_attribute`、`tf.is_unknown`；遇到静态无法确定的属性时跳过，不要把 `"<unknown>"` 当作违规。
- 对涉及 `Ref`、`Fn::GetAtt`、列表、标签、安全组规则、白名单、CIDR、端口范围的检查，使用 helper 或显式处理多种写法，不要只覆盖一个字面量路径。
- 不要编造 InfraGuard 不支持的命令；策略验证使用 `infraguard policy validate <file>`，扫描模板使用 `infraguard scan <template> -p <policy>`。

## 不适合写成 Rego 的内容

以下安全准则需要流程证据或运行时数据，不能只靠 ROS/Terraform 模板静态判断。遇到这类需求时，应生成评审清单或 pack 说明，不要伪造 Rego 检查：

- 云厂商、平台团队、安全团队和业务团队的共享责任矩阵。
- owner、风险接受人、例外审批人是否真实有效。
- 事件响应演练、恢复演练、渗透测试是否已经执行。
- 运行时漏洞是否已修复、账号是否实际长期未使用、密钥是否实际泄露。
- 供应商合同、客户审计要求和合规证据是否完整。

如果必须落地为 InfraGuard 策略，只检查模板中可验证的资源配置，例如标签、日志投递、加密、网络暴露、MFA、密码策略、备份策略、镜像扫描、KMS 轮换。

## 安全规则资产

安全相关的可静态验证项已落地为：

- Pack: `references/infraguard-policies/packs/iac-code-security-pack.rego`
- Rules: `references/infraguard-policies/security/*.rego`
- Helpers: `references/infraguard-policies/lib/helpers.rego`

该 pack 覆盖身份、网络公网暴露、数据保护、审计日志、供应链和密钥管理。不可静态验证的流程类要求保留在架构评审和上线证据中处理。
