# InfraGuard Policy Generation

This reference keeps PAC work aligned with InfraGuard without vendoring the InfraGuard policy catalog into iac-code.

## Lazy InfraGuard Sync

Run this sync before any PAC implementation, generation, validation, or catalog lookup. It is intentionally lazy: do it only when the PAC skill is triggered and the user needs InfraGuard-backed work.

1. Check whether InfraGuard is available:
   ```bash
   infraguard version
   ```
2. If the command is missing and the user wants the agent to prepare the local toolchain, install the official CLI:
   ```bash
   go install github.com/aliyun/infraguard/cmd/infraguard@latest
   ```
3. Check for policy updates before relying on policy names or behavior:
   ```bash
   infraguard policy update
   ```
4. Inspect the current policy catalog from the refreshed tool:
   ```bash
   infraguard policy list
   ```
5. When generating or editing custom policies, validate the file:
   ```bash
   infraguard policy validate path/to/policy.rego
   ```

If a command cannot run because InfraGuard or Go is not installed, explain the missing prerequisite and continue only with user-approved installation or with static guidance.

## Policy Lookup

- Prefer official policy IDs and packs from `infraguard policy list`.
- Use `infraguard policy get <policy-id>` when the user needs details for an existing rule.
- Use policy references in scan commands as `rule:aliyun:<name>` or `pack:aliyun:<name>`.
- Do not infer that a previously known policy still exists; refresh first with `infraguard policy update`.

## Template Scanning

Use InfraGuard scan for ROS templates:

```bash
infraguard scan template.yaml -p pack:aliyun:quick-start-compliance-pack
```

For automation or downstream analysis, request JSON output:

```bash
infraguard scan template.yaml -p rule:aliyun:ecs-instance-no-public-ip --format json
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
