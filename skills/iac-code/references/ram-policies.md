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
    read:
      - "sts:GetCallerIdentity" # Identify the caller and target account.
    write_after_approval: []
  ros_template_and_stack:
    read:
      - "ros:ValidateTemplate" # Validate a template without creating a stack.
      - "ros:GetTemplateParameterConstraints" # Discover valid template parameter values.
      - "ros:PreviewStack" # Preview the approved plan before any deployment.
      - "ros:GetStack" # Inspect the original stack and its operation state.
      - "ros:ListStackResources" # Verify the exact resources belonging to the stack.
    write_after_approval:
      - "ros:CreateStack" # Create the explicitly confirmed stack.
      - "ros:UpdateStack" # Apply an explicitly confirmed change to the selected stack.
      - "ros:DeleteStack" # Remove only the explicitly approved stack and cleanup scope.
  vpc_and_vswitch:
    read:
      - "vpc:DescribeVpcs" # Discover candidate VPCs in the selected account and region.
      - "vpc:DescribeVSwitches" # Discover candidate vSwitches and verify cleanup.
      - "vpc:DescribeVpcAttribute" # Inspect the selected VPC's attributes.
      - "vpc:DescribeVSwitchAttributes" # Inspect a selected vSwitch and its parent VPC.
    write_after_approval:
      - "vpc:CreateVpc" # Create only the VPC from the confirmed deployment plan.
      - "vpc:CreateVSwitch" # Create only the confirmed subnet in the selected VPC.
      - "vpc:DeleteVSwitch" # Delete the exact approved vSwitch after dependency checks.
      - "vpc:DeleteVpc" # Delete the exact approved VPC after its dependencies are removed.
  security_group:
    read:
      - "ecs:DescribeZones" # Discover available zones for the selected region.
      - "ecs:DescribeSecurityGroups" # Discover security groups and verify cleanup.
      - "ecs:DescribeSecurityGroupAttribute" # Inspect the selected group's effective rules.
    write_after_approval:
      - "ecs:CreateSecurityGroup" # Create only the confirmed group in the selected VPC.
      - "ecs:AuthorizeSecurityGroup" # Add only explicitly approved ingress rules.
      - "ecs:AuthorizeSecurityGroupEgress" # Add only explicitly approved egress rules.
      - "ecs:DeleteSecurityGroup" # Delete only the approved group after dependency checks.
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
