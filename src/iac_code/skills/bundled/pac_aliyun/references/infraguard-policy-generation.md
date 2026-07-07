# InfraGuard Policy Generation

This reference keeps PAC work aligned with InfraGuard without vendoring the InfraGuard policy catalog into iac-code.

## Lazy InfraGuard Sync

Run this sync before any PAC implementation, generation, validation, or catalog lookup. It is intentionally lazy: do it only when the PAC skill is triggered and the user needs InfraGuard-backed work.

1. Check whether InfraGuard is available:
   ```bash
   infraguard version
   ```
2. If the command is missing and the user wants the agent to prepare the local toolchain, install the official CLI through the Alibaba Cloud Go proxy:
   ```bash
   GOPROXY=https://mirrors.aliyun.com/goproxy/,direct go install github.com/aliyun/infraguard/cmd/infraguard@latest
   ```
   Do not suggest a plain `go install` command that relies on direct GitHub fetches.
3. If InfraGuard is installed, check whether the local CLI is already the latest version:
   ```bash
   infraguard update --check
   ```
   If the local version is not latest, tell the user that InfraGuard should be upgraded before PAC work continues:
   - When the local version is lower than `0.10.1`, upgrade by reinstalling with the Alibaba Cloud Go proxy command above.
   - When the local version is `0.10.1` or newer, upgrade with:
     ```bash
     infraguard update
     ```
4. Check for policy updates before relying on policy names or behavior:
   ```bash
   infraguard policy update
   ```
5. Inspect the current policy catalog from the refreshed tool:
   ```bash
   infraguard policy list
   ```
6. When generating or editing custom policies, validate the file:
   ```bash
   infraguard policy validate path/to/policy.rego
   ```

If a command cannot run because InfraGuard or Go is not installed, explain the missing prerequisite and continue only with user-approved installation or with static guidance. If the latest-version check cannot run, state that the local InfraGuard freshness is unverified instead of assuming it is current.

## Policy Lookup

- Prefer official policy IDs and packs from `infraguard policy list`.
- Use `infraguard policy get <policy-id>` when the user needs details for an existing rule.
- Use policy references in scan commands as `rule:aliyun:<name>` or `pack:aliyun:<name>`.
- Do not infer that a previously known policy still exists; refresh first with `infraguard policy update`.

## Supported Scan Dimensions

When the user prompt mentions one of these dimensions, scan with the matching pack directly:

| Prompt dimension | Pack |
| --- | --- |
| 最佳实践, best practice | `pack:aliyun:best-practice` |
| 合规性, 合规, compliance | `pack:aliyun:compliance` |
| 成本优化, 成本, cost optimization | `pack:aliyun:cost-optimization` |
| 弹性能力, 弹性, elasticity | `pack:aliyun:elasticity` |
| 高可用, 高可用性, high availability | `pack:aliyun:high-availability` |
| 网络架构, network architecture | `pack:aliyun:network-architecture` |
| 可运维性, 可运维, operations | `pack:aliyun:operations` |
| 安全性, 安全, security | `pack:aliyun:security` |

If the prompt mentions multiple supported dimensions, pass one `-p` flag for each matching pack. If the prompt asks for a broad assessment without naming a dimension, ask which dimension to scan or use the most relevant pack inferred from the user's risk statement.

## Template Scanning

Use InfraGuard scan for ROS templates:

```bash
infraguard scan template.yaml -p pack:aliyun:best-practice
```

For automation or downstream analysis, request JSON output:

```bash
infraguard scan template.yaml -p pack:aliyun:security --format json
```

For prompts that mention multiple dimensions, include each matching pack:

```bash
infraguard scan template.yaml -p pack:aliyun:security -p pack:aliyun:high-availability --format json
```

Summaries should include the violating resource, property path, severity, reason, and concrete ROS template change.

## Custom Policy Generation

Generate custom Rego only when official policies do not cover the user requirement. Keep each rule focused on one static ROS-template assertion.

Recommended output bundle:

- The custom policy file.
- A minimal violating ROS template.
- A minimal passing ROS template.
- The validation command and scan commands used.

Design constraints:

- Read only from template input, resource definitions, properties, references, mappings, conditions, and parameters.
- Keep cloud account state, billing history, runtime metrics, and manual approval evidence outside the policy unless the user supplies them as explicit input data.
- Prefer actionable violation paths pointing to the ROS property the user should edit.
- Validate syntax with `infraguard policy validate` before presenting the policy as ready.

## Handoff To IaC Workflows

When a policy finding requires editing or regenerating a ROS/Terraform template, use the IaC template workflow after the PAC result is clear. Keep the PAC source of truth in InfraGuard; do not copy official policy bodies into iac-code.
