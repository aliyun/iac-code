# ALB

ROS resource reference for Application Load Balancer (ALIYUN::ALB::*). Covers LoadBalancer, ServerGroup, Listener, Rule. Use this before generating ALB templates to avoid field-name, casing, and structure errors.

Sources:
- https://help.aliyun.com/zh/ros/developer-reference/aliyun-alb-loadbalancer
- https://help.aliyun.com/zh/ros/developer-reference/aliyun-alb-servergroup
- https://help.aliyun.com/zh/ros/developer-reference/aliyun-alb-listener
- https://help.aliyun.com/zh/ros/developer-reference/aliyun-alb-rule
- Local verification: ros_resource_value_specs.json, ros_official_resource_index.json, ROS source ros/resources/aliyun/alb/{load_balancer,server_group,listener,rule}.py

## ALIYUN::ALB::LoadBalancer

### Required Properties
- **VpcId**: String
- **AddressType**: String — `Intranet` or `Internet` (capitalized; do not use lowercase `intranet`/`internet`)
- **LoadBalancerEdition**: String — `Basic`, `Standard`, `StandardWithWaf`
- **ZoneMappings**: List — each item requires `ZoneId` (String) and `VSwitchId` (String); typically two different AZs, each with its own VSwitch. Some local regions allow single-AZ; do not claim universal two-AZ requirement
- **LoadBalancerBillingConfig**: Map — requires `PayType` (String); pay-as-you-go is `PostPay`

### Optional Properties
- **AddressIpVersion**: String — accepts `IPv4`, `Ipv4`, `DualStack`; canonical form is `IPv4`
- **AddressAllocatedMode**: String — `Fixed` or `Dynamic`; do not infer defaults across editions
- **DeletionProtectionEnabled**: Boolean — default `false`

### Constraints
- Private (Intranet) ALB must not attach AllocationId, EIP, or bandwidth package
- Do not use ALIYUN::SLB fields (LoadBalancerSpec, MasterZoneId, SlaveZoneId)

### Outputs
- **Ref**: returns LoadBalancerId
- **Fn::GetAtt**: `LoadBalancerId`, `DNSName` (DNSName is a LoadBalancer attribute, not Listener)

## ALIYUN::ALB::ServerGroup

### Required Properties
- **ServerGroupName**: String
- **HealthCheckConfig**: Map — requires `HealthCheckEnabled` (Boolean)

### Key Properties
- **ServerGroupType**: String — `Instance`, `Ip`, `Fc` (Fc backend protocol is HTTP only)
- **Protocol**: String — `HTTP`, `HTTPS`, `gRPC`, `GRPC` (both gRPC and GRPC accepted)
- **VpcId**: String — required for Instance/Ip types; must match ALB's VPC
- **Scheduler**: String — `Wrr`, `Wlc`, `Sch`, `Uch`
- **StickySessionConfig**: Map — separate nested config from HealthCheckConfig; disabling sticky session does not use a top-level boolean on ServerGroup

### HealthCheckConfig Fields
- **HealthCheckEnabled**: Boolean (required)
- **HealthCheckProtocol**: String — `HTTP`, `HTTPS`, `TCP`, `gRPC`, `GRPC`
- **HealthCheckPath**: String — must start with `/` (HTTP/HTTPS health checks)
- **HealthCheckConnectPort**: Integer 0..65535 — `0` means backend port; field name is `HealthCheckConnectPort`, not `HealthCheckPort`
- **HealthCheckCodes**: List — HTTP uses category strings like `http_2xx`; do not apply HTTP code rules to other protocols
- **HealthCheckInterval**: Integer 1..50
- **HealthCheckTimeout**: Integer 1..300; use full property names, `Interval`/`Timeout` are not aliases here
- **HealthyThreshold**: Integer 2..10
- **UnhealthyThreshold**: Integer 2..10

### Protocol Compatibility
- HTTP backend → HTTP/HTTPS/QUIC frontend
- HTTPS backend → HTTPS frontend
- gRPC backend → HTTPS/QUIC frontend

### Constraints
- Do not use CLB fields (VServerGroupId, BackendServerPort)
- Member binding uses separate ALIYUN::ALB::BackendServerAttachment resource, not inline

### Outputs
- **Ref**: returns ServerGroupId

## ALIYUN::ALB::Listener

### Required Properties
- **LoadBalancerId**: String
- **ListenerProtocol**: String — `HTTP`, `HTTPS`, `QUIC` (not TCP/UDP)
- **ListenerPort**: Integer 1..65535
- **DefaultActions**: List

### DefaultActions Structure
Each item:
- **Type**: `ForwardGroup`
- **ForwardGroupConfig**: Map containing `ServerGroupTuples` (List), each with `ServerGroupId` pointing to a server group
- Current ROS Listener default action does **not** declare `Weight`; do not copy Rule weighted structure here

### HTTPS-Specific
- **Certificates**: List — each item uses `CertificateId` to reference a certificate
- **Http2Enabled**: Boolean — only relevant for HTTPS

### Health Check
- Health check is configured on **ServerGroup**, not Listener; do not use CLB-style Listener HealthCheck fields

### Outputs
- **Ref**: returns ListenerId

## ALIYUN::ALB::Rule

### Required Properties
- **ListenerId**: String
- **RuleName**: String
- **Priority**: Integer 1..10000 — unique within a listener; lower = higher priority
- **RuleConditions**: List
- **RuleActions**: List

### RuleConditions Structure
Path condition example:
- **Type**: `Path`
- **PathConfig**: Map with `Values` (List of path strings)

### RuleActions Structure
- **Type**: `ForwardGroup`
- **Order**: Integer 1..50000
- **ForwardGroupConfig**: Map with `ServerGroupTuples` (List), each with `ServerGroupId`
- Single group: Weight defaults to 100
- Multiple groups: each must have explicit integer Weight (0..100)

### Direction
- **Direction**: String — `Request` or `Response`; request routing uses `Request`

### Outputs
- **Ref**: returns RuleId (verified in current version); other attributes depend on target version schema

## Cross-resource Relationships

- ServerGroup.VpcId must equal LoadBalancer.VpcId (same VPC)
- Listener.LoadBalancerId references LoadBalancer via Ref
- Listener DefaultActions.ForwardGroupConfig.ServerGroupTuples[].ServerGroupId references ServerGroup via Ref
- Rule.ListenerId references Listener via Ref
- External IDs (VpcId, VSwitchId, ZoneId, CertificateId) must be parameterized; never hardcode real cloud resource IDs
- Resource dependencies established via Ref/Fn::GetAtt; output attributes cannot be constant strings

## Anti-Patterns

- Using SLB/CLB field names (LoadBalancerSpec, VServerGroupId, BackendServerPort, MasterZoneId)
- Lowercase AddressType values (`intranet`/`internet`)
- Field name `HealthCheckPort` instead of `HealthCheckConnectPort`
- Placing health check config on Listener instead of ServerGroup
- Adding Weight to Listener DefaultActions (only Rule actions use Weight)
- Inline backend server members in ServerGroup (use BackendServerAttachment)
- Top-level `StickySessionEnabled` boolean on ServerGroup (use `StickySessionConfig` nested map)

## Applicability Notes

This reference covers verified common fields. It is not a complete schema. Uncovered properties, capability changes, or validation errors should be checked against the target version's official schema/documentation via GetResourceType. Do not globally disable GetResourceType or documentation retrieval. Example choices (private network, Standard edition, HTTP backend) are not universal ALB limitations. Template generation cannot assert that runtime resources exist, that inventory is available, or that certificates/quotas/billing are valid.
