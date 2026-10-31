# RAM permissions

## Permission model

The packaged bridge and Runtime-cache commands do not call Alibaba Cloud APIs and require no RAM permission.
Infrastructure jobs run through the authenticated iac-code Runtime. Grant the Runtime credential only the exact
Alibaba Cloud actions needed by the user's approved task. Template generation, conversion, and offline validation
also require no cloud permission.

Do not attach a product-wide `FullAccess` policy or use an action wildcard. Read-only discovery should use exact
`Describe`, `List`, or `Get` actions. Create, update, and delete actions must be added only after the corresponding
plan and cleanup scope have been approved. Where an API supports resource-level authorization, scope `Resource` to
the target account, region, stack, or resource ARN rather than all resources.

## Required permissions by workflow

The following `required_permissions` declaration defines the exact RAM actions for these supported operations in
Normal and Pipeline modes. Select only the operation and action needed by the approved task; do not grant the
union of this declaration. Mode selection itself grants no permission. Write actions require the corresponding
explicit user approval and resource scope; read actions remain subject to Runtime permission decisions.

```yaml
required_permissions:
  offline_template_and_runtime_cache: []
  caller_preflight:
    read: ["sts:GetCallerIdentity"]
    write_after_approval: []
  ros_template_and_stack:
    read:
      - "ros:ValidateTemplate"
      - "ros:GetTemplateParameterConstraints"
      - "ros:PreviewStack"
      - "ros:GetStack"
      - "ros:ListStackResources"
    write_after_approval: ["ros:CreateStack", "ros:UpdateStack", "ros:DeleteStack"]
  vpc_and_vswitch:
    read:
      - "vpc:DescribeVpcs"
      - "vpc:DescribeVSwitches"
      - "vpc:DescribeVpcAttribute"
      - "vpc:DescribeVSwitchAttributes"
    write_after_approval: ["vpc:CreateVpc", "vpc:CreateVSwitch", "vpc:DeleteVSwitch", "vpc:DeleteVpc"]
  security_group:
    read: ["ecs:DescribeZones", "ecs:DescribeSecurityGroups", "ecs:DescribeSecurityGroupAttribute"]
    write_after_approval:
      - "ecs:CreateSecurityGroup"
      - "ecs:AuthorizeSecurityGroup"
      - "ecs:AuthorizeSecurityGroupEgress"
      - "ecs:DeleteSecurityGroup"
```

Other resource types require their own exact product/action pairs. Never infer that the actions above authorize ECS
instances, databases, public IP addresses, gateways, load balancers, disks, or any other unrelated resource.

## Idempotent writes

For APIs supporting `ClientToken`, preserve the same token and identical parameters for retries of one approved
operation. After a timeout or uncertain result, query the original stack/resource/request state before another
write; do not create a duplicate resource to recover a missing response. Keep the original job and cursor, honor
permission boundaries, and stop if the outcome cannot be established. A changed plan or expanded cleanup scope
requires a new explicit confirmation. This guidance does not authorize direct cloud calls outside the Runtime.

## Failure handling

If the Runtime returns `Forbidden`, `Forbidden.RAM`, `NoPermission`, or a similar authorization error, report the
exact denied action and request ID when available. Do not retry with broader credentials, print credentials, or
bypass the bridge with a direct CLI call. Ask for the narrow missing action only when continuing the approved task
requires it.
