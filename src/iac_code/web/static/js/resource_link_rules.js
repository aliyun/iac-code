// Identity/recognition contracts are distinct from release-approved URL routes.
const definitions = [
  ["SLS::Project", "projectName", "name", "account", ["alicloud_log_project"]],
  ["ROS::Stack", "StackId", "id", "account-region", ["alicloud_ros_stack"]],
  ["ACTIONTRAIL::Trail", "Name", "name", "account-region", [], [], null, "TrailArn",
    ["HomeRegion", "Name", "TrailArn"]],
  ["CR::Namespace", "NamespaceId", "id", "account-region", [], ["InstanceId"], null, null,
    ["InstanceId", "NamespaceId", "NamespaceName"]],
  ["CR::Repository", "RepoId", "id", "account-region", [], ["InstanceId", "RepoNamespaceName"], null, null,
    ["InstanceId", "RepoId", "RepoName", "RepoNamespaceName"]],
  ["ElasticSearch::Instance", "instanceId", "id", "account-region"],
  ["KMS::Alias", "AliasName", "name", "account-region", [], [], null, "AliasArn",
    ["AliasArn", "AliasName", "KeyId"]],
  ["ECS::Instance", "InstanceId", "id", "account-region", ["alicloud_instance"], [], /^i-[a-zA-Z0-9]{6,}$/],
  ["ECS::Disk", "DiskId", "id", "account-region", ["alicloud_ecs_disk"], [], /^d-[a-zA-Z0-9]{6,}$/],
  ["ECS::Snapshot", "SnapshotId", "id", "account-region", ["alicloud_ecs_snapshot"], [], /^s-[a-zA-Z0-9]{6,}$/],
  ["ECS::SecurityGroup", "SecurityGroupId", "id", "account-region", ["alicloud_security_group"], [], /^sg-[a-zA-Z0-9]{6,}$/],
  ["ECS::NetworkInterface", "NetworkInterfaceId", "id", "account-region", ["alicloud_ecs_network_interface"], [], /^eni-[a-zA-Z0-9]{6,}$/],
  ["ECS::SSHKeyPair", "KeyPairName", "name", "account-region", ["alicloud_ecs_key_pair"]],
  ["VPC::VPC", "VpcId", "id", "account-region", ["ALIYUN::ECS::VPC", "alicloud_vpc"], [], /^vpc-[a-zA-Z0-9]{6,}$/],
  ["VPC::VSwitch", "VSwitchId", "id", "account-region", ["ALIYUN::ECS::VSwitch", "alicloud_vswitch"], [], /^vsw-[a-zA-Z0-9]{6,}$/],
  ["VPC::EIP", "AllocationId", "id", "account-region", ["alicloud_eip_address"], [], /^eip-[a-zA-Z0-9]{6,}$/],
  ["ECS::SNatEntry", "PhysicalResourceId", "id", "account-region", [], ["NatGatewayId"],
    null, null, ["NatGatewayId", "SnatTableId", "NetworkType"]],
  ["VPC::NatGateway", "PhysicalResourceId", "id", "account-region", ["alicloud_nat_gateway"], [], null, null,
    ["NatGatewayId"]],
  ["RDS::DBInstance", "DBInstanceId", "id", "account-region",
    ["alicloud_db_instance", "alicloud_rds_clone_db_instance", "alicloud_rds_upgrade_db_instance"]],
  ["POLARDB::DBCluster", "PhysicalResourceId", "id", "account-region", ["alicloud_polardb_cluster"],
    [], null, null, ["DBClusterId"]],
  ["POLARDB::DBClusterEndpoint", "DBEndpointId", "id", "account-region", ["alicloud_polardb_endpoint"],
    ["DBClusterId"], null, null, ["DBEndpointId", "DBClusterId"]],
  ["POLARDB::DBClusterEndpointAddress", "ConnectionString", "name", "account-region",
    ["alicloud_polardb_endpoint_address"], ["DBClusterId", "DBEndpointId"], null, null,
    ["ConnectionString", "DBClusterId", "DBEndpointId"]],
  ["PrivateLink::VpcEndpoint", "PhysicalResourceId", "id", "account-region",
    ["alicloud_privatelink_vpc_endpoint"], [], null, null, ["EndpointId"]],
  ["POLARDB::Account", "AccountName", "name", "account-region", ["alicloud_polardb_account"],
    ["DBClusterId"], null, null, ["AccountName", "DBClusterId"]],
  ["RAM::Role", "RoleName", "name", "account", ["alicloud_ram_role"], [], null, "RoleId", ["RoleName", "RoleId"]],
  ["RAM::User", "UserName", "name", "account", ["alicloud_ram_user"], [], null, "UserId", ["UserName", "UserId"]],
  ["RAM::ManagedPolicy", "PhysicalResourceId", "id", "account", ["alicloud_ram_policy"], [], null, null,
    ["PolicyName", "PolicyType"]],
  ["RAM::Group", "PhysicalResourceId", "id", "account", ["alicloud_ram_group"], [], null, null,
    ["GroupId", "GroupName"]],
  ["PVTZ::Zone", "PhysicalResourceId", "id", "account", ["alicloud_pvtz_zone"], [], null, null,
    ["ZoneId", "ZoneName"]],
  ["PVTZ::ZoneRecord", "PhysicalResourceId", "id", "account", ["alicloud_pvtz_zone_record"], ["ZoneId"],
    null, null,
    ["RecordId", "ZoneId", "Rr", "Type", "Value"]],
  ["OTS::Instance", "PhysicalResourceId", "id", "account-region", ["alicloud_ots_instance"], [], null, null,
    ["InstanceName"]],
  ["OTS::Table", "PhysicalResourceId", "id", "account-region", ["alicloud_ots_table"], ["InstanceName"], null,
    null,
    ["InstanceName", "TableName"]],
  ["VPC::RouteTable", "PhysicalResourceId", "id", "account-region", ["alicloud_route_table"], [], null, null,
    ["RouteTableId", "VpcId"]],
  ["ResourceManager::ResourceGroup", "Id", "id", "account",
    ["alicloud_resource_manager_resource_group"], [], null, null, ["DisplayName", "Id", "Name", "Status"]],
  ["VPC::RouterInterface", "PhysicalResourceId", "id", "account-region",
    ["alicloud_router_interface"], [], null, null, ["RouterInterfaceId", "RouterId", "RouterType"]],
  ["ESS::LifecycleHook", "PhysicalResourceId", "id", "account-region", ["alicloud_ess_lifecycle_hook"], [],
    null, null, ["LifecycleHookId", "ScalingGroupId"]],
  ["ESS::ScalingConfiguration", "PhysicalResourceId", "id", "account-region",
    ["alicloud_ess_scaling_configuration"], [], null, null, ["ScalingConfigurationId", "ScalingGroupId"]],
  ["OSS::Bucket", "BucketName", "name", "account-region", ["alicloud_oss_bucket"]],
  ["SLB::LoadBalancer", "LoadBalancerId", "id", "account-region", ["alicloud_slb_load_balancer"]],
  ["ALB::LoadBalancer", "LoadBalancerId", "id", "account-region", ["alicloud_alb_load_balancer"], [], /^alb-[a-zA-Z0-9]{6,}$/],
  ["NLB::LoadBalancer", "LoadBalancerId", "id", "account-region", ["alicloud_nlb_load_balancer"], [], /^nlb-[a-zA-Z0-9]{6,}$/],
  ["NLB::ServerGroup", "PhysicalResourceId", "id", "account-region", ["alicloud_nlb_server_group"], [], null, null,
    ["ServerGroupId"]],
  ["MONGODB::Instance", "PhysicalResourceId", "id", "account-region", ["alicloud_mongodb_instance"]],
  ["ROCKETMQ::Instance", "PhysicalResourceId", "id", "account-region", ["alicloud_ons_instance"], [], null, null,
    ["InstanceId", "regionId", "seriesCode", "serviceCode"]],
  ["REDIS::Instance", "InstanceId", "id", "account-region", ["alicloud_kvstore_instance"]],
  ["FC::Service", "ServiceName", "name", "account-region", ["alicloud_fc_service"]],
  ["FC::Function", "FunctionName", "name", "account-region", ["alicloud_fc_function"], ["ServiceName"]],
  ["FC::Trigger", "TriggerName", "name", "account-region", ["alicloud_fc_trigger"], ["ServiceName", "FunctionName"]],
  ["CEN::TransitRouter", "TransitRouterId", "id", "account-region", ["alicloud_cen_transit_router"], ["CenId"]],
  ["CEN::TransitRouterVpcAttachment", "TransitRouterAttachmentId", "id", "account-region",
    ["alicloud_cen_transit_router_vpc_attachment"], ["CenId", "TransitRouterId"], null, null, ["VpcId"]],
  ["ALB::Listener", "ListenerId", "id", "account-region", ["alicloud_alb_listener"], ["LoadBalancerId"]],
  ["SLB::VServerGroup", "VServerGroupId", "id", "account-region", ["alicloud_slb_server_group"], ["LoadBalancerId"]],
  ["NAS::FileSystem", "FileSystemId", "id", "account-region", ["alicloud_nas_file_system"]],
  ["NAS::AccessGroup", "PhysicalResourceId", "id", "account-region", ["alicloud_nas_access_group"],
    ["FileSystemType"], null, null, ["AccessGroupName", "AccessGroupType", "FileSystemType"]],
  ["NAS::MountTarget", "MountTargetDomain", "id", "account-region",
    ["alicloud_nas_mount_target"], ["FileSystemId"]],
  ["NAS::DataFlow", "DataFlowId", "id", "account-region", [], ["FileSystemId"]],
  ["NAS::Fileset", "FilesetId", "id", "account-region", [], ["FileSystemId"]],
  ["NAS::ProtocolMountTarget", "ProtocolMountTargetDomain", "id", "account-region", [], ["FileSystemId"]],
  ["NAS::ProtocolService", "ProtocolServiceId", "id", "account-region", [], ["FileSystemId"]],
  ["ECS::Image", "ImageId", "id", "account-region", ["alicloud_image"], [], /^m-[a-zA-Z0-9]{6,}$/],
  ["ECS::ImageCopy", "ImageId", "id", "account-region", []],
  ["ECS::ImageDetail", "PhysicalResourceId", "id", "account-region", [], ["DestinationRegionId"]],
];

function expandInjectedCatalog(value) {
  const official = new Map(), sin = new Map();
  if (!value || value.schemaVersion !== 1 || !Array.isArray(value.officialGroups) ||
      !Array.isArray(value.sinHostSubstitution) || value.sinHostSubstitution.length !== 2 ||
      !value.sinOverrides || typeof value.sinOverrides !== "object" || Array.isArray(value.sinOverrides)) {
    return {official, sin, checkedAt: "", hashes: {}};
  }
  for (const group of value.officialGroups) {
    if (!Array.isArray(group) || group.length !== 2 || typeof group[0] !== "string" || !Array.isArray(group[1])) {
      return {official: new Map(), sin: new Map(), checkedAt: "", hashes: {}};
    }
    for (const type of group[1]) {
      if (typeof type !== "string" || typeof group[0] !== "string" || official.has(type)) {
        return {official: new Map(), sin: new Map(), checkedAt: "", hashes: {}};
      }
      official.set(type, group[0]);
    }
  }
  const [source, target] = value.sinHostSubstitution;
  if (typeof source !== "string" || !source || typeof target !== "string" || !target) {
    return {official: new Map(), sin: new Map(), checkedAt: "", hashes: {}};
  }
  for (const [type, template] of official) sin.set(type, template.replaceAll(source, target));
  for (const [type, template] of Object.entries(value.sinOverrides)) {
    if (!official.has(type) || typeof template !== "string" || !template) {
      return {official: new Map(), sin: new Map(), checkedAt: "", hashes: {}};
    }
    sin.set(type, template);
  }
  return {
    official, sin,
    checkedAt: typeof value.checkedAt === "string" ? value.checkedAt : "",
    hashes: value.hashes && typeof value.hashes === "object" ? Object.freeze({...value.hashes}) : {},
  };
}

function expandInjectedIdentities(value, catalogTypes, reservedTypes) {
  if (!Array.isArray(value?.identities)) return [];
  const result = [], claimed = new Set(reservedTypes);
  const token = /^[A-Za-z][A-Za-z0-9_]*$/;
  for (const raw of value.identities) {
    if (!raw || typeof raw !== "object" || Array.isArray(raw) || !catalogTypes.has(raw.resourceType) ||
        !token.test(raw.identifierField) || !["id", "name"].includes(raw.identifierKind) ||
        !["regional", "account"].includes(raw.scopeKind) || !Array.isArray(raw.aliases) ||
        !Array.isArray(raw.parentFields) || !Array.isArray(raw.attributeFields) ||
        raw.aliases.some((item) => !catalogTypes.has(item)) ||
        raw.parentFields.some((item) => !token.test(item)) || raw.attributeFields.some((item) => !token.test(item)) ||
        (raw.stableField != null && !token.test(raw.stableField))) continue;
    const aliases = raw.aliases.filter((item) => item !== raw.resourceType && !claimed.has(item));
    if (claimed.has(raw.resourceType)) continue;
    claimed.add(raw.resourceType);
    for (const alias of aliases) claimed.add(alias);
    result.push(Object.freeze({
      resourceType: raw.resourceType, identifierField: raw.identifierField,
      identifierKind: raw.identifierKind, scopeKind: raw.scopeKind,
      aliases: Object.freeze(aliases), parentFields: Object.freeze([...new Set(raw.parentFields)].sort()),
      attributeFields: Object.freeze([...new Set(raw.attributeFields)].sort()),
      pattern: null, stableField: raw.stableField || undefined,
    }));
  }
  return result;
}

const catalog = expandInjectedCatalog(globalThis.__IAC_RESOURCE_LINKS__);
export const ROS_RESOURCE_LINK_CATALOG = Object.freeze({
  checkedAt: catalog.checkedAt,
  hashes: catalog.hashes,
  official: Object.freeze(Object.fromEntries(catalog.official)),
  sin: Object.freeze(Object.fromEntries(catalog.sin)),
});

const explicitTypes = definitions.map(([
  type, field, kind, scope, aliases = [], parents = [], pattern, stableField, attributeFields = [],
]) => ({
  resourceType: `ALIYUN::${type}`, identifierField: field, identifierKind: kind,
  scopeKind: scope === "account" ? "account" : "regional", aliases: Object.freeze(aliases),
  parentFields: Object.freeze(parents), attributeFields: Object.freeze(attributeFields), pattern, stableField,
}));
const explicitByType = new Map(explicitTypes.flatMap((spec) =>
  [spec.resourceType, ...spec.aliases].map((key) => [key, spec])));
const placeholderFields = (template) => [...template.matchAll(/@?\{([A-Za-z0-9_-]+)\}/g)]
  .map((match) => match[1].split("-", 1)[0]);
const catalogTypes = new Set([...catalog.official.keys(), ...catalog.sin.keys()]);
const injectedTypes = expandInjectedIdentities(
  globalThis.__IAC_RESOURCE_LINKS__, catalogTypes, explicitByType.keys());
const identityByType = new Map(explicitByType);
for (const spec of injectedTypes) {
  identityByType.set(spec.resourceType, spec);
  for (const alias of spec.aliases) identityByType.set(alias, spec);
}
const genericTypes = [...catalogTypes].filter((type) => !identityByType.has(type)).map((type) => Object.freeze({
  resourceType: type,
  identifierField: "PhysicalResourceId",
  identifierKind: "id",
  scopeKind: "regional",
  aliases: Object.freeze([]),
  parentFields: Object.freeze([...new Set([
    ...placeholderFields(catalog.official.get(type) || ""),
    ...placeholderFields(catalog.sin.get(type) || ""),
  ].filter((field) => !["PhysicalId", "RegionId", "ResourceName"].includes(field)))].sort()),
  attributeFields: Object.freeze([]),
  pattern: null,
  stableField: undefined,
}));
export const RESOURCE_TYPES = Object.freeze([...explicitTypes.map(Object.freeze), ...injectedTypes, ...genericTypes]);
const byType = new Map(identityByType);
for (const spec of genericTypes) byType.set(spec.resourceType, spec);
export const resourceSpec = (type) => byType.get(type) || null;
export const canonicalResourceType = (type) => resourceSpec(type)?.resourceType || type;
const snake = (value) => value.replace(/([a-z0-9])([A-Z])/g, "$1_$2").toLowerCase();
const byField = new Map();
for (const spec of RESOURCE_TYPES) {
  for (const field of new Set([spec.identifierField, snake(spec.identifierField), spec.stableField].filter(Boolean))) {
    // A bare PhysicalResourceId never establishes one of hundreds of products.
    if (["id", "name", "PhysicalResourceId", "physical_resource_id"].includes(field)) continue;
    if (!byField.has(field)) byField.set(field, []);
    byField.get(field).push(spec);
  }
}

export function literalIdentifier(value) {
  return typeof value === "string" && value.length > 0 && value.length <= 1024 &&
    !/[\s\x00-\x1f\x7f*<>`]|\$[\w{(]|\.\.\.|…|:\/\//.test(value);
}

// SAE namespace names and CMS contact group names can contain ASCII spaces.
// Do not loosen generic IDs, account scopes, regions or other name contracts.
export function resourceIdentifier(value, spec, field) {
  return literalIdentifier(value) || (((spec?.resourceType === "ALIYUN::SAE::Namespace" && field === "NamespaceName") ||
    (spec?.resourceType === "ALIYUN::CMS::ContactGroup" && ["Name", "PhysicalResourceId"].includes(field))) &&
    typeof value === "string" && value === value.trim() &&
    value.length <= 1024 && literalIdentifier(value.replaceAll(" ", "")));
}

export function specsForField(field, type = null) {
  const explicit = resourceSpec(type);
  const candidates = byField.get(field) || [];
  // Shared InstanceId/LoadBalancerId fields do not prove a product on their own.
  if (explicit && candidates.includes(explicit)) return [explicit];
  // ``Name``/``Id`` are presentation-level labels, not identity contracts.
  // When an API tool has already established the concrete resource type, only
  // its declared identifier field may produce a link. Otherwise a generic
  // Name in (for example) a CMS template response can reuse an unrelated
  // ActionTrail Name rule merely because both products expose that spelling.
  const normalizedField = String(field || "").normalize("NFKC").replace(/[\s_:-]+/g, "").toLowerCase();
  const normalizedIdentifier = String(explicit?.identifierField || "")
    .normalize("NFKC").replace(/[\s_:-]+/g, "").toLowerCase();
  if (explicit && ["id", "name", "physicalresourceid"].includes(normalizedField)) {
    // Terraform resource blocks intentionally use generic ``name``/``id``
    // attributes; their canonical alicloud_* type is the explicit proof.
    if (String(type).startsWith("alicloud_")) return [explicit];
    // Name-based ROS resources may expose an opaque PhysicalResourceId. The
    // scanner will still require ResourceAttributes proof before treating it
    // as a name, but it needs the typed candidate to perform that check.
    if (normalizedField === "physicalresourceid" && explicit.identifierKind === "name") return [explicit];
    return normalizedField === normalizedIdentifier ? [explicit] : [];
  }
  if (candidates.length === 1) return candidates;
  return candidates;
}

const segment = (value) => encodeURIComponent(value).replace(/[!'()*]/g, (char) =>
  `%${char.charCodeAt(0).toString(16).toUpperCase()}`);

function normalizeTemplate(template) {
  return template.startsWith("//") ? `https:${template}` : template;
}

function templateHost(template) {
  return /^https:\/\/([^/?#]+)/i.exec(normalizeTemplate(template))?.[1] || "";
}

function renderCatalogTemplate(template, reference, spec, sourceType) {
  let physicalId = reference.identifier;
  if (["alicloud_slb_master_slave_server_group", "alicloud_slb_rule"].includes(spec.resourceType)) {
    // The reviewed legacy template opens a list under its actual parent,
    // not an instance whose ID is the child group's/rule's ID.
    physicalId = reference.attributes?.LoadBalancerId;
  }
  if (spec.resourceType === "alicloud_slb_rule") {
    const attrs = reference.attributes || {};
    if (!["http", "https"].includes(attrs.ListenerProtocol) ||
        !/^[1-9][0-9]*$/.test(String(attrs.ListenerPort || "")) || Number(attrs.ListenerPort) > 65535) return null;
  }
  if (spec.resourceType === "alicloud_slb_listener") {
    const attrs = reference.attributes || {};
    const parts = String(reference.identifier || "").split(":");
    if (parts.length !== 3 || !/^lb-[a-zA-Z0-9]+$/.test(parts[0]) ||
        !["http", "https", "tcp", "udp"].includes(parts[1]) || !/^[1-9][0-9]*$/.test(parts[2]) ||
        Number(parts[2]) > 65535 || attrs.load_balancer_id !== parts[0] ||
        attrs.LoadBalancerId !== parts[0] || attrs.ListenerProtocol !== parts[1] ||
        String(attrs.ListenerPort) !== parts[2]) return null;
  }
  if (spec.resourceType === "ALIYUN::NAS::MountTarget") {
    // The console lists mount targets inside their actual filesystem. The
    // domain prefix is not proof of that parent (and TF IDs are composite).
    physicalId = reference.attributes?.FileSystemId;
  }
  if (["ALIYUN::POLARDB::Account", "alicloud_polardb_database", "ALIYUN::POLARDB::DBClusterEndpoint",
    "ALIYUN::POLARDB::DBClusterEndpointAddress"].includes(spec.resourceType)) {
    // Account names are only unique within their actual database cluster.
    physicalId = reference.attributes?.DBClusterId;
  }
  const values = {
    ...(reference.attributes || {}),
    [spec.identifierField]: reference.identifier,
    PhysicalId: physicalId,
    RegionId: sourceType === "ALIYUN::ECS::ImageDetail"
      ? reference.attributes?.DestinationRegionId
      : reference.regionId,
    ResourceName: reference.logicalResourceId,
  };
  let missing = false;
  const rendered = normalizeTemplate(template).replace(/@?\{([A-Za-z0-9_-]+)\}/g, (_match, token) => {
    const separator = token.indexOf("-");
    const key = separator < 0 ? token : token.slice(0, separator);
    const fallback = separator < 0 ? "" : token.slice(separator + 1);
    const value = values[key] || fallback;
    if (!literalIdentifier(value)) {
      missing = true;
      return "";
    }
    return segment(value);
  });
  return missing ? null : rendered;
}

function rocketmq4InstanceConsoleUrl(reference) {
  return `https://ons.console.aliyun.com/region/${segment(reference.regionId)}`+
    `/instance/${segment(reference.identifier)}/detail`;
}

function normalizeCatalogRoute(sourceType, partition, value, reference) {
  if (!value) return value;
  if (partition === "aliyun" && sourceType === "ALIYUN::CEN::RouteEntry") {
    return cenRouteEntryParentUrl(reference);
  }
  if (partition === "aliyun" && sourceType === "alicloud_mse_cluster") {
    // The reviewed Terraform alias uses the same InstanceId as the ROS
    // resource. Reuse its released exact detail route, not Terraform's
    // historical Home fallback which loses both the resource and region.
    const canonical = "ALIYUN::MSE::Cluster";
    return renderCatalogTemplate(catalog.official.get(canonical), reference, resourceSpec(canonical), canonical);
  }
  if (partition === "aliyun" && sourceType === "alicloud_ons_instance") {
    // The 4.x Terraform resource uses the same exact InstanceId. Keep its
    // published catalog entry intact, but share the reviewed live detail
    // route instead of its obsolete InstanceManagement hash fallback.
    return rocketmq4InstanceConsoleUrl(reference);
  }
  try {
    const target = new URL(value);
    if (partition === "aliyun" && ["ALIYUN::RAM::ManagedPolicy", "alicloud_ram_policy"].includes(sourceType)) {
      const policy = /^\/policies\/([^/]+)\/Custom\/?$/.exec(target.pathname);
      if (target.hostname !== "ram.console.aliyun.com" || !policy) return value;
      target.pathname = "/policies/detail";
      target.search = "";
      target.searchParams.set("policyType", reference.attributes?.PolicyType === "System" ? "System" : "Custom");
      target.searchParams.set("policyName", reference.attributes?.PolicyName || decodeURIComponent(policy[1]));
      target.hash = "";
      return target.href;
    }
    if (partition === "aliyun" && ["ALIYUN::RAM::Role", "alicloud_ram_role"].includes(sourceType)) {
      const role = /^\/roles\/([^/]+)\/?$/.exec(target.pathname);
      if (target.hostname !== "ram.console.aliyun.com" || !role) return value;
      target.pathname = "/roles/detail";
      target.search = "";
      target.searchParams.set("roleName", decodeURIComponent(role[1]));
      target.hash = "";
      return target.href;
    }
    if (partition === "aliyun" && ["ALIYUN::RAM::User", "alicloud_ram_user"].includes(sourceType)) {
      const userId = reference.attributes?.UserId || reference.identifier;
      if (target.hostname !== "ram.console.aliyun.com" || !literalIdentifier(userId)) return value;
      target.pathname = "/users/detail";
      target.search = "";
      target.searchParams.set("userId", userId);
      target.hash = "";
      return target.href;
    }
    if (partition === "aliyun" && sourceType === "ALIYUN::ROS::StackGroup") {
      const stackGroupName = reference.attributes?.StackGroupName;
      if (target.hostname !== "ros.console.aliyun.com" || !literalIdentifier(stackGroupName)) return null;
      target.pathname = `/${segment(reference.regionId)}/stackGroup/${segment(stackGroupName)}`;
      target.search = "";
      target.hash = "";
      return target.href;
    }
    if (sourceType === "ALIYUN::ECS::Snapshot") {
      return `https://${target.hostname}/snapshotDetail/${segment(reference.regionId)}/${segment(reference.identifier)}/detail`;
    }
    if (["ALIYUN::ECS::NetworkInterface", "alicloud_ecs_network_interface"].includes(sourceType)) {
      if (!resourceSpec(sourceType).pattern.test(reference.identifier)) return null;
      return `https://${target.hostname}/networkInterfaces/region/${segment(reference.regionId)}/detail/${segment(reference.identifier)}`;
    }
    if (partition === "aliyun" &&
        ["ALIYUN::ECI::ImageCache", "alicloud_eci_image_cache", "alicloud_eci_openapi_image_cache"]
          .includes(sourceType)) {
      const containerGroupId = reference.attributes?.ContainerGroupId;
      return literalIdentifier(containerGroupId)
        ? `https://eci.console.aliyun.com/?#/eci/${segment(reference.regionId)}`+
          `/image/${segment(containerGroupId)}/${segment(reference.identifier)}/productevents`
        : null;
    }
    if (partition === "aliyun" && sourceType === "ALIYUN::OTS::VpcBinder") {
      const instanceName = reference.attributes?.InstanceName;
      return literalIdentifier(instanceName)
        ? `https://ots.console.aliyun.com/${segment(reference.regionId)}`+
          `/${segment(instanceName)}/networkManagement`
        : null;
    }
    if (partition === "aliyun" &&
        ["ALIYUN::VPC::VpnConnection", "alicloud_vpn_connection"].includes(sourceType)) {
      return `https://vpc.console.aliyun.com/vpn/${segment(reference.regionId)}`+
        `/vpn-connections/${segment(reference.identifier)}`;
    }
    if (partition === "aliyun" &&
        ["ALIYUN::VPC::RouterInterface", "alicloud_router_interface"].includes(sourceType)) {
      const routerId = reference.attributes?.RouterId;
      if (reference.attributes?.RouterType === "VBR" && literalIdentifier(routerId) && routerId.startsWith("vbr-")) {
        return `https://expressconnect.console.aliyun.com/vbr/${segment(reference.regionId)}`+
          `/detail/${segment(routerId)}`;
      }
      const section = {VBR: "vbr2vpc", VRouter: "vpc2vpc"}[reference.attributes?.RouterType];
      return section
        ? `https://expressconnect.console.aliyun.com/peerconnection/${segment(reference.regionId)}/${section}`+
          `?ConnectionType=${section}&ProductForm=expressconnect&TabKey=${section}`
        : null;
    }
    if (partition === "aliyun" &&
        ["ALIYUN::ECS::SNatEntry", "ALIYUN::VPC::SnatEntry", "alicloud_snat_entry"].includes(sourceType)) {
      return snatEntryUrl(reference, sourceType);
    }
    return value;
  } catch (_error) { return value; }
}

function snatEntryUrl(reference, sourceType = reference.resourceType) {
  const natGatewayId = reference.attributes?.NatGatewayId;
  const networkType = reference.attributes?.NetworkType ||
    (sourceType === "ALIYUN::ECS::SNatEntry" ? "" : "internet");
  const section = {internet: "nat", intranet: "vpc-nat"}[networkType];
  return section && literalIdentifier(natGatewayId) && natGatewayId.startsWith("ngw-")
    ? `https://vpc.console.aliyun.com/${section}/${segment(reference.regionId)}`+
      `/nats/${segment(natGatewayId)}/snats`
    : null;
}

function nasAccessGroupUrl(reference) {
  const fileSystemType = String(reference.attributes?.FileSystemType || "").toLowerCase();
  const accessGroupTypes = {classic: "Classic", vpc: "Vpc"};
  const accessGroupType = accessGroupTypes[String(reference.attributes?.AccessGroupType || "").toLowerCase()];
  return ["standard", "extreme", "cpfs"].includes(fileSystemType) && accessGroupType &&
      !reference.identifier.includes(":") &&
      (!reference.attributes?.AccessGroupName || reference.attributes.AccessGroupName === reference.identifier)
    ? `https://nasnext.console.aliyun.com/${segment(reference.regionId)}/access-group/${fileSystemType}`+
      `/detail/${fileSystemType}/${segment(reference.identifier)}/${accessGroupType}/rule`
    : null;
}

function catalogRule(ruleId, sourceType, partition, template) {
  // CHANNEL_LINKS' instance route cannot address an independent custom
  // parameter template. Only the reviewed domestic template list is valid.
  if (partition === "aliyun" &&
      ["alicloud_rds_parameter_group", "alicloud_nas_access_group"].includes(sourceType)) return null;
  const spec = resourceSpec(sourceType);
  const host = partition === "aliyun" && sourceType === "ALIYUN::CEN::RouteEntry"
    ? "vpc.console.aliyun.com"
    : partition === "aliyun" && ["ALIYUN::VPC::RouterInterface", "alicloud_router_interface"].includes(sourceType)
      ? "expressconnect.console.aliyun.com"
      : templateHost(template);
  if (!spec || !host || !["aliyun", "alibabacloud"].includes(partition)) return null;
  const requiredFields = [...new Set([
    ...(["alicloud_slb_master_slave_server_group", "alicloud_slb_rule"].includes(spec.resourceType)
      ? spec.parentFields : []),
    ...(spec.resourceType === "ALIYUN::NAS::MountTarget" ? ["FileSystemId"] : []),
    ...(["ALIYUN::POLARDB::Account", "alicloud_polardb_database", "ALIYUN::POLARDB::DBClusterEndpoint",
      "ALIYUN::POLARDB::DBClusterEndpointAddress"].includes(spec.resourceType) ? ["DBClusterId"] : []),
    ...(spec.resourceType === "ALIYUN::POLARDB::DBClusterEndpointAddress" ? ["DBEndpointId"] : []),
    ...placeholderFields(template).filter((field) =>
      !["PhysicalId", "RegionId", "ResourceName", spec.identifierField].includes(field)),
    ...(sourceType === "ALIYUN::ECS::ImageDetail" ? ["DestinationRegionId"] : []),
    ...(partition === "aliyun" && sourceType === "ALIYUN::ROS::StackGroup" ? ["StackGroupName"] : []),
    ...(partition === "aliyun" &&
      ["ALIYUN::VPC::RouterInterface", "alicloud_router_interface"].includes(sourceType) ? ["RouterType"] : []),
    ...(partition === "aliyun" &&
      ["ALIYUN::VPC::SnatEntry", "alicloud_snat_entry"].includes(sourceType) ? ["NatGatewayId"] : []),
    ...(partition === "aliyun" && sourceType === "ALIYUN::ECS::SNatEntry"
      ? ["NatGatewayId", "NetworkType"] : []),
    ...(partition === "aliyun" &&
      ["ALIYUN::ECI::ImageCache", "alicloud_eci_image_cache", "alicloud_eci_openapi_image_cache"]
        .includes(sourceType) ? ["ContainerGroupId"] : []),
    ...(partition === "aliyun" && sourceType === "ALIYUN::OTS::VpcBinder" ? ["InstanceName"] : []),
  ])];
  return Object.freeze({
    ruleId, version: 1, resourceType: spec.resourceType, sourceType, partition,
    releaseStatus: "verified", host, identifierField: spec.identifierField,
    requiredFields: Object.freeze(requiredFields),
    provenance: Object.freeze({
      source: `ROS production CHANNEL_LINKS/${partition === "aliyun" ? "OFFICIAL" : "SIN"}:${sourceType}`,
      checkedAt: catalog.checkedAt, kind: "production-channel-config",
    }),
    build: (reference) => normalizeCatalogRoute(
      sourceType, partition, renderCatalogTemplate(template, reference, spec, sourceType), reference),
  });
}

const productionRoutes = [
  ...[...catalog.official].map(([type, template], index) =>
    catalogRule(`ros-channel-official-${index + 1}`, type, "aliyun", template)),
  ...[...catalog.sin].map(([type, template], index) =>
    catalogRule(`ros-channel-sin-${index + 1}`, type, "alibabacloud", template)),
].filter(Boolean);

const otsTableUrl = (reference) => {
  let table = reference.identifier;
  if (reference.resourceType === "alicloud_ots_table") {
    // ROS-managed Terraform exposes the provider's instance:table ID.
    // Keep the full source locator; only navigate after both exact parts
    // agree with the source-bound route attributes.
    const parts = table.split(":");
    if (parts.length !== 2 || parts[0] !== reference.attributes.InstanceName ||
        parts[1] !== reference.attributes.TableName ||
        !/^[A-Za-z][A-Za-z0-9-]{0,15}$/.test(parts[0]) ||
        !/^[A-Za-z_][A-Za-z0-9_]{0,254}$/.test(parts[1])) return null;
    table = parts[1];
  }
  return `https://ots.console.aliyun.com/${segment(reference.regionId)}`+
    `/${segment(reference.attributes.InstanceName)}/${segment(table)}/dataManage`;
};

function liveRule(ruleId, sourceType, host, build, requiredFields = [], checkedAt = "2026-09-12") {
  const spec = resourceSpec(sourceType);
  if (!spec) return null;
  return Object.freeze({
    ruleId, version: 1, resourceType: spec.resourceType, sourceType, partition: "aliyun",
    releaseStatus: "verified", host, identifierField: spec.identifierField,
    requiredFields: Object.freeze(requiredFields),
    provenance: Object.freeze({
      source: "Alibaba Cloud console live navigation",
      checkedAt, kind: "live-console-route",
    }),
    build,
  });
}

function cenRouteEntryParentUrl(reference) {
  const parts = String(reference.identifier || "").split("/");
  if (parts.length !== 3) return null;
  const [cenId, childInstanceId, childRouteTableId] = parts;
  const sourceRegions = [...new Set((reference.sources || [])
    .map((source) => source?.requestRegionId)
    .filter((value) => literalIdentifier(value) && /^[a-z][a-z0-9-]{1,63}$/.test(value)))];
  const regionId = reference.regionId || reference.attributes?.RegionId ||
    (sourceRegions.length === 1 ? sourceRegions[0] : null);
  if (!literalIdentifier(cenId) || !cenId.startsWith("cen-") ||
      !literalIdentifier(childInstanceId) ||
      !(childInstanceId.startsWith("vpc-") || childInstanceId.startsWith("vbr-")) ||
      !literalIdentifier(childRouteTableId) || !childRouteTableId.startsWith("vtb-") ||
      !literalIdentifier(regionId) || !/^[a-z][a-z0-9-]{1,63}$/.test(regionId)) return null;
  // ALIYUN::CEN::RouteEntry publishes a route from an attached VPC/VBR child
  // route table.  Its physical ID is CenId/ChildInstanceId/ChildRouteTableId;
  // it is not a Transit Router route entry.  The console has no standalone
  // page for the publication operation, so the exact child route-table detail
  // is the narrowest truthful landing page.
  return `https://vpc.console.aliyun.com/vpc/${segment(regionId)}`+
    `/route-tables/${segment(childRouteTableId)}`;
}

function ramPolicyType(reference) {
  if (["System", "Custom"].includes(reference.attributes?.PolicyType)) return reference.attributes.PolicyType;
  const name = reference.attributes?.PolicyName || reference.identifier;
  // Old replay facts predate PolicyType capture. RAM reserves the Aliyun
  // prefix for system policies; remaining user-created names are Custom.
  return name === "AdministratorAccess" || name === "ReadOnlyAccess" || /^Aliyun[A-Z]/.test(name)
    ? "System"
    : (name ? "Custom" : "");
}

function clickHouseEnterpriseSource(reference) {
  return (reference.sources || []).some((source) =>
    String(source.product || "").toLowerCase() === "clickhouse" &&
    String(source.action || "").toLowerCase() === "describedbinstances" &&
    source.evidence === "api_response");
}

function routerInterfaceConsoleSection(reference) {
  return {VBR: "vbr2vpc", VRouter: "vpc2vpc"}[reference.attributes?.RouterType] || "";
}

const domesticLiveRoutes = [
  // The custom-template row's View action opens a drawer without changing
  // this regional list URL (live navigation, 2026-10-04). It is not a DB
  // instance or ROS instance-parameter attachment; preserve its own identity.
  liveRule("rds-custom-parameter-template-cn", "alicloud_rds_parameter_group", "rdsnext.console.aliyun.com",
    (reference) => String(reference.attributes?.ParameterGroupType) === "1"
      ? `https://rdsnext.console.aliyun.com/paramGroup/${segment(reference.regionId)}/paramGroups/custom`
      : null, ["ParameterGroupType"], "2026-10-04"),
  liveRule("actiontrail-trail-cn", "ALIYUN::ACTIONTRAIL::Trail", "actiontrail.console.aliyun.com",
    (reference) => `https://actiontrail.console.aliyun.com/`+
      `${segment(reference.attributes.HomeRegion)}/trail-list/${segment(reference.identifier)}`, ["HomeRegion"]),
  // Alias pages are the owning key's detail page. ListAliases returns KeyId
  // on the same reviewed row, so both AliasName and AliasArn can navigate
  // without mistaking either alias locator for the key identifier.
  liveRule("kms-alias-key-detail-cn", "ALIYUN::KMS::Alias", "kms.console.aliyun.com",
    (reference) => `https://kms.console.aliyun.com/${segment(reference.regionId)}`+
      `/key/detail/${segment(reference.attributes.KeyId)}`, ["KeyId"]),
  // The legacy hash route on sae.console.aliyun.com redirects embedded
  // sign-in back to the public login page.  SAE's current console exposes
  // namespaces at this regional list route and its 4service host supports
  // the same embedded sign-in flow as application details.
  liveRule("sae-namespace-list-cn", "ALIYUN::SAE::Namespace", "saenext.console.aliyun.com",
    (reference) => `https://saenext.console.aliyun.com/${segment(reference.regionId)}/namespace`),
  // Terraform NamespaceId is not the ROS NamespaceName locator. Use its
  // independently bound regional list row, never infer an application ID.
  liveRule("sae-terraform-namespace-list-cn", "alicloud_sae_namespace", "saenext.console.aliyun.com",
    (reference) => `https://saenext.console.aliyun.com/${segment(reference.regionId)}/namespace`,
    [], "2026-10-04"),
  // Enterprise ACR namespaces have no standalone detail page.  Keep the
  // stable namespace ID linkable while landing on the exact owning instance's
  // namespace list, where the named row is visible.
  liveRule("cr-enterprise-namespace-cn", "ALIYUN::CR::Namespace", "cr.console.aliyun.com",
    (reference) => `https://cr.console.aliyun.com/${segment(reference.regionId)}`+
      `/instance/${segment(reference.attributes.InstanceId)}/namespaces`, ["InstanceId"]),
  liveRule("cr-enterprise-repository-cn", "ALIYUN::CR::Repository", "cr.console.aliyun.com",
    (reference) => `https://cr.console.aliyun.com/repository/${segment(reference.regionId)}`+
      `/${segment(reference.attributes.InstanceId)}/${segment(reference.attributes.RepoNamespaceName)}`+
      `/${segment(reference.attributes.RepoName)}/details`,
    ["InstanceId", "RepoNamespaceName", "RepoName"]),
  // The current ECS console uses a history route for instance details. The
  // legacy hash route from CHANNEL_LINKS redirects Shift-opened tabs to the
  // instance list and drops the resource ID.
  liveRule("ecs-instance-cn", "ALIYUN::ECS::Instance", "ecs.console.aliyun.com",
    (reference) => `https://ecs.console.aliyun.com/server/${segment(reference.identifier)}`+
      `/detail?regionId=${segment(reference.regionId)}`),
  // The live domestic disk list links directly to this history route. The
  // CHANNEL_LINKS hash route currently redirects here, but keeping the exact
  // console-owned route avoids relying on that compatibility redirect.
  liveRule("ecs-disk-cn", "ALIYUN::ECS::Disk", "ecs.console.aliyun.com",
    (reference) => `https://ecs.console.aliyun.com/diskdetail/${segment(reference.identifier)}`+
      `/detail?regionId=${segment(reference.regionId)}`),
  liveRule("cdn-domain-cn", "ALIYUN::CDN::Domain", "cdn.console.aliyun.com",
    (reference) => `https://cdn.console.aliyun.com/domain/detail/${segment(reference.identifier)}/basic`),
  liveRule("cas-upload-certificate-detail-cn", "ALIYUN::CAS::Certificate", "yundun.console.aliyun.com",
    (reference) => String(reference.attributes?.Upload || "").toLowerCase() === "true"
      ? `https://yundun.console.aliyun.com/?p=cas#/uploadExtendDetail/`+
        `${segment(reference.attributes.CertificateId)}/0/${segment(reference.regionId)}`
      : null,
    ["CertificateId", "Upload"]),
  liveRule("cas-certificate-detail-cn", "ALIYUN::CAS::Certificate", "yundun.console.aliyun.com", (reference) => {
    const certificateId = reference.attributes?.CertificateId;
    const orderId = reference.attributes?.OrderId;
    return literalIdentifier(certificateId) && literalIdentifier(orderId)
      ? `https://yundun.console.aliyun.com/?p=cas#/certExtendDetail/${segment(certificateId)}`+
        `/${segment(orderId)}/${segment(reference.regionId)}`
      : null;
  }),
  liveRule("cas-certificate-filtered-list-cn", "ALIYUN::CAS::Certificate", "yundun.console.aliyun.com",
    (reference) => {
      const adSearchParams = JSON.stringify({
        BrandId: "", Keyword: reference.identifier, ShowSize: 10, CurrentPage: 1, StatusCode: "",
      });
      return `https://yundun.console.aliyun.com/?p=cas#/certExtend/buy/${segment(reference.regionId)}`+
        `?adSearchParams=${encodeURIComponent(adSearchParams)}`;
    }),
  liveRule("nas-access-group-cn", "ALIYUN::NAS::AccessGroup", "nasnext.console.aliyun.com",
    nasAccessGroupUrl, ["AccessGroupType", "FileSystemType"]),
  liveRule("nas-access-group-tf-cn", "alicloud_nas_access_group", "nasnext.console.aliyun.com",
    nasAccessGroupUrl, ["AccessGroupType", "FileSystemType"]),
  // Access rules have no standalone page.  The most precise target is their
  // owning access group's rule tab.  AccessGroupType is joined only from the
  // same-turn DescribeAccessGroups response; never infer Classic/Vpc from the
  // group name.
  liveRule("nas-access-rule-parent-cn", "ALIYUN::NAS::AccessRule", "nasnext.console.aliyun.com", (reference) => {
    const fileSystemType = String(reference.attributes?.FileSystemType || "").toLowerCase();
    const accessGroupTypes = {classic: "Classic", vpc: "Vpc"};
    const accessGroupType = accessGroupTypes[String(reference.attributes?.AccessGroupType || "").toLowerCase()];
    const accessGroupName = reference.attributes?.AccessGroupName;
    return ["standard", "extreme", "cpfs"].includes(fileSystemType) && accessGroupType && accessGroupName
      ? `https://nasnext.console.aliyun.com/${segment(reference.regionId)}/access-group/${fileSystemType}`+
        `/detail/${fileSystemType}/${segment(accessGroupName)}/${accessGroupType}/rule`
      : null;
  }, ["AccessGroupName", "AccessGroupType", "FileSystemType"]),
  liveRule("ens-instance-cn", "ALIYUN::ENS::Instance", "ens.console.aliyun.com",
    (reference) => `https://ens.console.aliyun.com/#/instance/detail/${segment(reference.identifier)}`),
  liveRule("ehpc-cluster-cn", "ALIYUN::EHPC::Cluster", "ehpc.console.aliyun.com",
    (reference) => `https://ehpc.console.aliyun.com/#/clusterdetail/info/${segment(reference.identifier)}`+
      `?referrer=%2Fcluster&regionId=${segment(reference.regionId)}`),
  // The legacy EMR console now redirects this route to the new-console
  // overview and drops both the region and cluster ID.  The EMR on ECS list
  // exposes this resource-specific route in the current console.
  liveRule("emr-cluster-cn", "ALIYUN::EMR::Cluster", "emr-next.console.aliyun.com",
    (reference) => `https://emr-next.console.aliyun.com/#/region/${segment(reference.regionId)}`+
      `/resource/all/ecs/detail/${segment(reference.identifier)}/overview`),
  // The 2023-05-22 DescribeDBInstances API lists Enterprise Edition instances.
  // ROS' older DBCluster type is Community Edition and keeps the production
  // CHANNEL_LINKS route below.  The resource ID prefix is shared, so the
  // observed API source—not an ID heuristic—must select the console section.
  liveRule("clickhouse-enterprise-db-instance-cn", "ALIYUN::ClickHouse::DBCluster",
    "clickhouse.console.aliyun.com", (reference) => clickHouseEnterpriseSource(reference)
      ? `https://clickhouse.console.aliyun.com/clickhouse/${segment(reference.regionId)}`+
        `/list/enterprise/${segment(reference.identifier)}/basic`
      : null),
  liveRule("alb-health-check-template-cn", "ALIYUN::ALB::HealthCheckTemplate", "slb.console.aliyun.com",
    (reference) => `https://slb.console.aliyun.com/alb/${segment(reference.regionId)}`+
      `/health-check-templates/${segment(reference.identifier)}`),
  liveRule("ecs-auto-provisioning-group-cn", "ALIYUN::ECS::AutoProvisioningGroup", "ecs.console.aliyun.com",
    (reference) => `https://ecs.console.aliyun.com/fleet/region/${segment(reference.regionId)}`+
      `/autoProvisioningGroupId/${segment(reference.identifier)}/groupConfig`),
  liveRule("ecs-auto-snapshot-policy-cn", "ALIYUN::ECS::AutoSnapshotPolicy", "ecs.console.aliyun.com",
    (reference) => `https://ecs.console.aliyun.com/autoSnapshotPolicyDetail/${segment(reference.regionId)}`+
      `/${segment(reference.identifier)}/detail`),
  liveRule("ecs-launch-template-cn", "ALIYUN::ECS::LaunchTemplate", "ecs.console.aliyun.com",
    (reference) => `https://ecs.console.aliyun.com/launch-template-detail/region/${segment(reference.regionId)}`+
      `/launchTemplateId/${segment(reference.identifier)}`),
  // The VPN connection list exposes a resource-owned detail anchor. The
  // production CHANNEL_LINKS entry stops at the list and loses the locator.
  liveRule("vpc-vpn-connection-detail-cn", "ALIYUN::VPC::VpnConnection", "vpc.console.aliyun.com",
    (reference) => `https://vpc.console.aliyun.com/vpn/${segment(reference.regionId)}`+
      `/vpn-connections/${segment(reference.identifier)}`),
  // Router interfaces have no standalone detail route. A VBR-side interface
  // is most precisely represented by its real parent VBR detail; the current
  // VBR-upstream list can omit valid legacy/idle interfaces even while
  // DescribeRouterInterfaces still returns them. Other subtypes retain the
  // console-owned peer-connection list selected by RouterType.
  liveRule("vpc-router-interface-list-cn", "ALIYUN::VPC::RouterInterface",
    "expressconnect.console.aliyun.com", (reference) => {
      const routerId = reference.attributes?.RouterId;
      if (reference.attributes?.RouterType === "VBR" && literalIdentifier(routerId) && routerId.startsWith("vbr-")) {
        return `https://expressconnect.console.aliyun.com/vbr/${segment(reference.regionId)}`+
          `/detail/${segment(routerId)}`;
      }
      const section = routerInterfaceConsoleSection(reference);
      return section
        ? `https://expressconnect.console.aliyun.com/peerconnection/${segment(reference.regionId)}/${section}`+
          `?ConnectionType=${section}&ProductForm=expressconnect&TabKey=${section}`
        : null;
    }, ["RouterType"]),
  // A SNAT entry is addressed under its NAT gateway. The API response carries
  // NatGatewayId on the same reviewed row, so never substitute SnatEntryId as
  // the parent as the legacy CHANNEL_LINKS template does.
  liveRule("vpc-snat-entry-list-cn", "ALIYUN::VPC::SnatEntry", "vpc.console.aliyun.com",
    (reference) => snatEntryUrl(reference), ["NatGatewayId"]),
  liveRule("mongodb-instance-cn", "ALIYUN::MONGODB::Instance", "mongodb.console.aliyun.com",
    (reference) => `https://mongodb.console.aliyun.com/replicate/${segment(reference.regionId)}`+
      `/instances/${segment(reference.identifier)}/basicInfo`),
  liveRule("lindorm-hitsdb-instance-cn", "ALIYUN::TSDB::HiTSDBInstance", "lindorm.console.aliyun.com",
    (reference) => `https://lindorm.console.aliyun.com/${segment(reference.regionId)}`+
      `/cluster/lindorm/${segment(reference.identifier)}/info`),
  liveRule("lindorm-instance-cn", "ALIYUN::Lindorm::Instance", "lindorm.console.aliyun.com",
    (reference) => `https://lindorm.console.aliyun.com/${segment(reference.regionId)}`+
      `/cluster/lindorm/${segment(reference.identifier)}/info`),
  liveRule("rocketmq-instance-cn", "ALIYUN::ROCKETMQ::Instance", "ons.console.aliyun.com",
    rocketmq4InstanceConsoleUrl),
  liveRule("memcache-instance-cn", "ALIYUN::MEMCACHE::Instance", "kvstore.console.aliyun.com",
    (reference) => `https://kvstore.console.aliyun.com/Memcache/instance/${segment(reference.regionId)}`+
      `/${segment(reference.identifier)}/Memcache/CLASSIC/default/info`),
  liveRule("eci-container-group-cn", "ALIYUN::ECI::ContainerGroup", "eci.console.aliyun.com",
    (reference) => `https://eci.console.aliyun.com/?regionId=${segment(reference.regionId)}`+
      `#/eci/${segment(reference.regionId)}`+
      `/detail/${segment(reference.identifier)}/containers`),
  // The image-cache list links through the backing container group before the
  // cache ID. DescribeImageCaches returns both IDs on the same reviewed row.
  liveRule("eci-image-cache-cn", "ALIYUN::ECI::ImageCache", "eci.console.aliyun.com",
    (reference) => `https://eci.console.aliyun.com/?#/eci/${segment(reference.regionId)}`+
      `/image/${segment(reference.attributes.ContainerGroupId)}`+
      `/${segment(reference.identifier)}/productevents`, ["ContainerGroupId"]),
  liveRule("mns-queue-cn", "ALIYUN::MNS::Queue", "mns.console.aliyun.com",
    (reference) => `https://mns.console.aliyun.com/region/${segment(reference.regionId)}`+
      `/queue/${segment(reference.identifier)}/detail`),
  liveRule("mns-topic-cn", "ALIYUN::MNS::Topic", "mns.console.aliyun.com",
    (reference) => `https://mns.console.aliyun.com/region/${segment(reference.regionId)}`+
      `/topic/${segment(reference.identifier)}/detail`),
  // ListParameters/ListSecretParameters expose an internal p-* ID, but the
  // console's own row links address both kinds of parameter by Name.  Keep the
  // ID as the primary API fact so either displayed locator resolves to the
  // same object, then build the route from the reviewed same-row Name alias.
  liveRule("oos-parameter-cn", "ALIYUN::OOS::Parameter", "oos.console.aliyun.com",
    (reference) => `https://oos.console.aliyun.com/${segment(reference.regionId)}`+
      `/parameter/detail/${segment(reference.attributes.Name)}?routerType=detail`, ["Name"]),
  liveRule("oos-secret-parameter-cn", "ALIYUN::OOS::SecretParameter", "oos.console.aliyun.com",
    (reference) => `https://oos.console.aliyun.com/${segment(reference.regionId)}`+
      `/parameter/detailSecret/${segment(reference.attributes.Name)}?routerType=detail`, ["Name"]),
  // The patch-baseline table displays Id, but its own detail anchors use Name
  // for both built-in and user baselines.  ListPatchBaselines supplies both
  // fields on the same row, so preserve the API ID as the primary fact while
  // routing every visible alias through the reviewed Name locator.
  liveRule("oos-patch-baseline-cn", "ALIYUN::OOS::PatchBaseline", "oos.console.aliyun.com",
    (reference) => `https://oos.console.aliyun.com/${segment(reference.regionId)}`+
      `/patch/baseline/detail/${segment(reference.attributes.Name)}`, ["Name"]),
  liveRule("ots-instance-cn", "ALIYUN::OTS::Instance", "ots.console.aliyun.com",
    (reference) => `https://ots.console.aliyun.com/${segment(reference.regionId)}`+
      `/${segment(reference.identifier)}/list`),
  // DescribeVpcInfoByInstance returns the binding together with InstanceName.
  // The reviewed console route has no per-binding detail page, so land on the
  // owning instance's network-management page where the exact VPC row appears.
  liveRule("ots-vpc-binder-cn", "ALIYUN::OTS::VpcBinder", "ots.console.aliyun.com",
    (reference) => `https://ots.console.aliyun.com/${segment(reference.regionId)}`+
      `/${segment(reference.attributes.InstanceName)}/networkManagement`, ["InstanceName"]),
  liveRule("ots-table-cn", "ALIYUN::OTS::Table", "ots.console.aliyun.com", otsTableUrl, ["InstanceName"]),
  liveRule("ots-table-tf-cn", "alicloud_ots_table", "ots.console.aliyun.com", otsTableUrl, ["InstanceName"]),
  liveRule("ots-table-list-cn", "ALIYUN::OTS::Table", "ots.console.aliyun.com",
    (reference) => `https://ots.console.aliyun.com/${segment(reference.regionId)}/list?productName=otsnext`),
  liveRule("pvtz-zone-cn", "ALIYUN::PVTZ::Zone", "dnsnext.console.aliyun.com",
    (reference) => `https://dnsnext.console.aliyun.com/privateDNS/zones/`+
      `${segment(reference.identifier)}/records`),
  liveRule("pvtz-zone-record-cn", "ALIYUN::PVTZ::ZoneRecord", "dnsnext.console.aliyun.com",
    (reference) => `https://dnsnext.console.aliyun.com/privateDNS/zones/`+
      `${segment(reference.attributes.ZoneId)}/records`, ["ZoneId"]),
  liveRule("pvtz-zone-record-list-cn", "ALIYUN::PVTZ::ZoneRecord", "dnsnext.console.aliyun.com",
    () => "https://dnsnext.console.aliyun.com/privateDNS"),
  liveRule("ram-managed-policy-cn", "ALIYUN::RAM::ManagedPolicy", "ram.console.aliyun.com", (reference) => {
    const policyType = ramPolicyType(reference);
    const policyName = reference.attributes?.PolicyName || reference.identifier;
    return policyType && literalIdentifier(policyName)
      ? `https://ram.console.aliyun.com/policies/detail?policyType=${segment(policyType)}`+
        `&policyName=${segment(policyName)}`
      : null;
  }),
  liveRule("ram-managed-policy-list-cn", "ALIYUN::RAM::ManagedPolicy", "ram.console.aliyun.com",
    () => "https://ram.console.aliyun.com/policies"),
  liveRule("ram-group-cn", "ALIYUN::RAM::Group", "ram.console.aliyun.com",
    (reference) => `https://ram.console.aliyun.com/groups/${segment(reference.attributes.GroupName)}`,
    ["GroupName"]),
  liveRule("ram-group-list-cn", "ALIYUN::RAM::Group", "ram.console.aliyun.com",
    () => "https://ram.console.aliyun.com/groups"),
  liveRule("cms-contact-cn", "ALIYUN::CMS::Contact", "cloudmonitor.console.aliyun.com",
    () => "https://cloudmonitor.console.aliyun.com/alert/contact"),
  liveRule("cms-contact-group-cn", "ALIYUN::CMS::ContactGroup", "cloudmonitor.console.aliyun.com",
    () => "https://cloudmonitor.console.aliyun.com/alert/contact"),
  liveRule("cms-metric-rule-template-cn", "ALIYUN::CMS::MetricRuleTemplate", "cloudmonitor.console.aliyun.com",
    () => "https://cloudmonitor.console.aliyun.com/alert-template"),
  liveRule("cms-monitor-group-cn", "ALIYUN::CMS::MonitorGroup", "cloudmonitor.console.aliyun.com",
    (reference) => `https://cloudmonitor.console.aliyun.com/app-group/`+
      `${segment(reference.identifier)}/overview`),
  // An ACL rule has its own AcrId, but the console exposes rules inside the
  // parent ACL detail page. DescribeACLAttribute returns that AclId on the
  // same reviewed response row, so never substitute the rule ID here.
  liveRule("sag-acl-rule-cn", "ALIYUN::SAG::ACLRule", "smartag.console.aliyun.com",
    (reference) => `https://smartag.console.aliyun.com/acl/${segment(reference.regionId)}`+
      `/acls/${segment(reference.attributes.AclId)}`, ["AclId"]),
  liveRule("vpc-route-table-cn", "ALIYUN::VPC::RouteTable", "vpc.console.aliyun.com",
    (reference) => `https://vpc.console.aliyun.com/vpc/${segment(reference.regionId)}`+
      `/route-tables/${segment(reference.attributes?.RouteTableId || reference.identifier)}`),
  liveRule("ess-scaling-group-enable-cn", "ALIYUN::ESS::ScalingGroupEnable", "essnew.console.aliyun.com",
    (reference) => `https://essnew.console.aliyun.com/#/v3/group/detail/${segment(reference.regionId)}`+
      `/${segment(reference.identifier)}`),
  // CHANNEL_LINKS still contains legacy /region/ routes. The current alarm
  // route does not redirect and returns InvalidParameter, while the scheduled
  // task route only works through a compatibility redirect. Both consoles
  // expose the exact target on their current list pages.
  liveRule("ess-alarm-task-cn", "ALIYUN::ESS::AlarmTask", "essnew.console.aliyun.com",
    (reference) => `https://essnew.console.aliyun.com/#/task/v3/alarm/`+
      `${segment(reference.regionId)}/system`),
  liveRule("ess-scheduled-task-cn", "ALIYUN::ESS::ScheduledTask", "essnew.console.aliyun.com",
    (reference) => `https://essnew.console.aliyun.com/#/task/v3/timer/${segment(reference.regionId)}`),
  liveRule("ess-lifecycle-hook-cn", "ALIYUN::ESS::LifecycleHook", "essnew.console.aliyun.com",
    (reference) => `https://essnew.console.aliyun.com/#/v3/group/detail/${segment(reference.regionId)}`+
      `/${segment(reference.attributes.ScalingGroupId)}/lifecycleHook`, ["ScalingGroupId"]),
  liveRule("ess-lifecycle-hook-list-cn", "ALIYUN::ESS::LifecycleHook", "essnew.console.aliyun.com",
    (reference) => `https://essnew.console.aliyun.com/#/v3/group/list/${segment(reference.regionId)}`),
  liveRule("ess-scaling-configuration-cn", "ALIYUN::ESS::ScalingConfiguration", "essnew.console.aliyun.com",
    (reference) => `https://essnew.console.aliyun.com/#/v3/group/detail/${segment(reference.regionId)}`+
      `/${segment(reference.attributes.ScalingGroupId)}/scalingConfiguration`, ["ScalingGroupId"]),
  liveRule("ess-scaling-configuration-list-cn", "ALIYUN::ESS::ScalingConfiguration", "essnew.console.aliyun.com",
    (reference) => `https://essnew.console.aliyun.com/#/v3/group/list/${segment(reference.regionId)}`),
  liveRule("cen-bandwidth-package-cn", "ALIYUN::CEN::CenBandwidthPackage", "cen.console.aliyun.com",
    () => "https://cen.console.aliyun.com/cen/bandwidth"),
  liveRule("cen-route-entry-cn", "ALIYUN::CEN::RouteEntry", "vpc.console.aliyun.com",
    cenRouteEntryParentUrl),
  liveRule("cen-transit-router-vpc-attachment-cn", "ALIYUN::CEN::TransitRouterVpcAttachment",
    "cen.console.aliyun.com", (reference) => `https://cen.console.aliyun.com/cen/attachment/`+
      `${segment(reference.regionId)}/${segment(reference.attributes.CenId)}`+
      `/${segment(reference.attributes.TransitRouterId)}/attachInstance`, ["CenId", "TransitRouterId"]),
].filter(Boolean);

function stackRule(partition, host) {
  return Object.freeze({
    ruleId: `ros-stack-${partition === "aliyun" ? "cn" : "intl"}`, version: 1,
    resourceType: "ALIYUN::ROS::Stack", sourceType: "ALIYUN::ROS::Stack", partition,
    releaseStatus: "verified", host, identifierField: "StackId", requiredFields: Object.freeze([]),
    provenance: Object.freeze({
      source: "src/iac_code/web/outputs.py:build_ros_console_url",
      checkedAt: "2026-09-10", kind: "existing-product-route",
    }),
    build: (reference) => `https://${host}/${segment(reference.regionId)}/stacks/${segment(reference.identifier)}`,
  });
}

function ecsImageRule(partition, host) {
  return Object.freeze({
    ruleId: `ecs-image-${partition === "aliyun" ? "cn" : "intl"}`, version: 1,
    resourceType: "ALIYUN::ECS::Image", sourceType: "ALIYUN::ECS::Image", partition,
    releaseStatus: "verified", host, identifierField: "ImageId", requiredFields: Object.freeze([]),
    provenance: Object.freeze({
      source: "ECS console image detail navigation",
      checkedAt: "2026-09-11", kind: "live-console-route",
    }),
    build: (reference) => `https://${host}/imageDetail/region/${segment(reference.regionId)}`+
      `/imageId/${segment(reference.identifier)}`,
  });
}

export const RESOURCE_LINK_RULES = Object.freeze([
  stackRule("aliyun", "ros.console.aliyun.com"),
  stackRule("alibabacloud", "ros-intl.console.alibabacloud.com"),
  ecsImageRule("aliyun", "ecs.console.aliyun.com"),
  ecsImageRule("alibabacloud", "ecs.console.alibabacloud.com"),
  ...domesticLiveRoutes,
  ...productionRoutes,
]);
const approvedHosts = new Set(RESOURCE_LINK_RULES.map((rule) => rule.host));

export function buildResourceConsoleUrl(rule, reference) {
  if (!literalIdentifier(reference.accountScope)) return null;
  return buildVerifiedConsoleUrl(rule, reference);
}

// Compatibility for the pre-existing output-panel stack rows, whose historical
// payload does not carry an account identity. Do not use these rows as facts or
// choices for conversation references. Both callers use the same approved route
// and locator validation; new references still require accountScope above.
export function buildLegacyRosStackConsoleUrl(regionId, stackId, consolePartition = "aliyun") {
  const rule = RESOURCE_LINK_RULES.find((item) => item.resourceType === "ALIYUN::ROS::Stack" &&
    item.partition === consolePartition && item.releaseStatus === "verified");
  return buildVerifiedConsoleUrl(rule, {
    resourceType:"ALIYUN::ROS::Stack", identifier:stackId, identifierKind:"id", identifierField:"StackId",
    regionId, consolePartition,
  });
}

function buildVerifiedConsoleUrl(rule, reference) {
  const spec = resourceSpec(reference.resourceType);
  if (!spec || !rule || rule.releaseStatus !== "verified" || !rule.provenance?.source) return null;
  if (!approvedHosts.has(rule.host)) return null;
  if (rule.resourceType !== spec.resourceType || reference.consolePartition !== rule.partition) return null;
  if (reference.identifierField !== rule.identifierField || !resourceIdentifier(reference.identifier, spec, reference.identifierField)) return null;
  if (reference.identifierKind !== spec.identifierKind) return null;
  if ([".", ".."].includes(reference.identifier)) return null;
  const routeRegion = rule.sourceType === "ALIYUN::ECS::ImageDetail"
    ? reference.attributes?.DestinationRegionId
    : reference.regionId;
  if (spec.scopeKind === "regional" && !literalIdentifier(routeRegion)) return null;
  if (spec.scopeKind === "regional" && !/^[a-z][a-z0-9-]{1,63}$/.test(routeRegion)) return null;
  if ((rule.requiredFields || spec.parentFields).some((key) => !literalIdentifier(reference.attributes?.[key]))) return null;
  try {
    const built = rule.build(reference);
    if (!built) return null;
    const result = new URL(built);
    const expected = renderCatalogTemplate(`https://${rule.host}`, reference, spec, rule.sourceType);
    const expectedHost = expected && new URL(expected).hostname;
    if (result.protocol !== "https:" || result.hostname !== expectedHost || result.port || result.username || result.password) return null;
    if (/%(?:00|0a|0d)/i.test(result.href)) return null;
    return result.href;
  } catch (_error) { return null; }
}

function withContext(reference, occurrence) {
  const result = {...reference, attributes: {...reference.attributes}};
  for (const key of ["accountScope", "consolePartition", "regionId"]) {
    if (!result[key] && occurrence[key]) result[key] = occurrence[key];
  }
  for (const [key, value] of Object.entries(occurrence.attributes || {})) {
    if (!result.attributes[key]) result.attributes[key] = value;
  }
  if (result.scopeKind === "account") delete result.regionId;
  return result;
}

function sameNameReplacements(occurrence, reference, candidates) {
  const spec = resourceSpec(reference.resourceType);
  if (!spec || occurrence.identifierKind !== "name") return [];
  const fields = ["accountScope", "consolePartition", ...(spec.scopeKind === "regional" ? ["regionId"] : [])];
  // A replacement is an explicit choice within the old namespace, not a way
  // to silently switch accounts or use a user declaration as existence proof.
  if (fields.some((field) => !literalIdentifier(reference[field])) ||
      spec.parentFields.some((field) => !literalIdentifier(reference.attributes?.[field]))) return [];
  return candidates.filter((candidate) => candidate.referenceId !== reference.referenceId &&
    candidate.evidence === "api_response" && candidate.status !== "DELETE_COMPLETE" &&
    canonicalResourceType(candidate.resourceType) === spec.resourceType &&
    candidate.identifierKind === "name" && candidate.identifier === reference.identifier &&
    fields.every((field) => candidate[field] === reference[field]) &&
    spec.parentFields.every((field) => candidate.attributes?.[field] === reference.attributes?.[field]));
}

function collapseCompatibleIdCandidates(occurrence, candidates) {
  if (occurrence.identifierKind !== "id" || candidates.length < 2) return candidates;
  const spec = resourceSpec(occurrence.resourceType || candidates[0]?.resourceType);
  if (!spec || candidates.some((item) => canonicalResourceType(item.resourceType) !== spec.resourceType)) return candidates;
  const values = (reference) => [
    reference.accountScope,
    reference.consolePartition,
    ...(spec.scopeKind === "regional" ? [reference.regionId] : []),
    ...spec.parentFields.map((field) => reference.attributes?.[field]),
  ];
  const rows = candidates.map((reference) => ({reference, values:values(reference)}));
  const dominant = rows.filter((candidate) => rows.every((other) => candidate.values.every((value, index) => {
    const otherValue = other.values[index];
    return (!otherValue || value === otherValue) && (!otherValue || Boolean(value));
  })));
  // Repeated observations of an ID may begin without credential scope and be
  // enriched by a later trusted response. Collapse only when one known scope
  // dominates every partial observation. Conflicting accounts/regions remain
  // genuinely ambiguous, and names never use this shortcut.
  return dominant.length ? [dominant[0].reference] : candidates;
}

export function resolveResourceOccurrence(occurrence, index, rules = RESOURCE_LINK_RULES) {
  if (!resourceIdentifier(occurrence.identifier, resourceSpec(occurrence.resourceType), occurrence.identifierField) ||
      (occurrence.identifierKind === "name" && !occurrence.semantic)) {
    return {status: "not_resource"};
  }
  let candidates = index.candidates(occurrence);
  const allCandidates = candidates;
  const binding = index.bindingFor(occurrence);
  if (binding && candidates.some((item) => item.referenceId === binding.referenceId)) candidates = [binding];
  else {
    // Source ownership selects only compatible identities, never a last-used account.
    const owned = candidates.filter((reference) => (reference.sources || []).some((source) =>
      (occurrence.toolUseId && source.toolUseId === occurrence.toolUseId) ||
      (occurrence.messageId && source.messageId === occurrence.messageId) ||
      (occurrence.turnId && source.turnId === occurrence.turnId)));
    if (owned.length) candidates = owned;
  }
  candidates = collapseCompatibleIdCandidates(occurrence, candidates);
  if (candidates.length > 1) return {status: "ambiguous", candidates};
  let reference = candidates[0];
  const observedReference = Boolean(reference);
  if (!reference) {
    const spec = resourceSpec(occurrence.resourceType);
    if (!spec) return {status: "unsupported"};
    reference = {...occurrence, scopeKind: spec.scopeKind, sources: [], attributes: occurrence.attributes || {}};
  }
  reference = withContext(reference, occurrence);
  // Configured credentials may complete the account/site context of a resource
  // already proved by an API fact. A recognized locator with no matching fact
  // may reuse only a cloud context independently observed in this same visible
  // turn; it must not turn arbitrary prose into a console target.
  const inferredContext = observedReference
    ? index.contextFor?.(occurrence, reference)
    : index.observedContextFor?.(occurrence, reference);
  reference = withContext(reference, inferredContext || {});
  const spec = resourceSpec(reference.resourceType);
  if (!spec) return {status: "unsupported"};
  let matching = rules.filter((rule) => rule.resourceType === spec.resourceType && rule.releaseStatus === "verified");
  if (!matching.length) return {status: "unsupported", reference};
  const missing = [];
  if (!reference.accountScope) missing.push("accountScope");
  if (!reference.consolePartition) missing.push("consolePartition");
  if (spec.scopeKind === "regional" && !reference.regionId) missing.push("regionId");
  // A numeric RoleId is not a RoleName. Use only a proved same-object primary/alias.
  const exact = matching.filter((item) => item.sourceType === reference.resourceType);
  if (exact.length) matching = exact;
  matching = matching.filter((item) => !reference.consolePartition || item.partition === reference.consolePartition);
  if (!matching.length) return {status: "unsupported", reference};
  if (reference.identifierKind === "name" && reference.status === "DELETE_COMPLETE") {
    return {status: "needs_context", reason: "deleted_name", reference, missing, candidates,
      replacements: sameNameReplacements(occurrence, reference, allCandidates)};
  }
  if (missing.length) return {status: "needs_context", reference, missing, candidates};
  let closest = null;
  for (const rule of matching) {
    let candidate = reference;
    if (reference.resourceType === "alicloud_nas_access_group" &&
        reference.identifierField === "PhysicalResourceId") {
      const parts = reference.identifier.split(":");
      if (reference.evidence !== "api_response" || !reference.sources?.some(source =>
          source.product === "ros" && ["ListStackResources", "GetStackResource"].includes(source.action) &&
          source.evidence === "api_response") ||
          parts.length !== 2 || !/^[A-Za-z][A-Za-z0-9_-]{2,63}$/.test(parts[0]) ||
          !["standard", "extreme", "cpfs"].includes(parts[1]) ||
          (reference.attributes?.AccessGroupName && reference.attributes.AccessGroupName !== parts[0]) ||
          (reference.attributes?.FileSystemType && reference.attributes.FileSystemType !== parts[1]) ||
          reference.aliases?.some(alias =>
            alias.identifierField === "AccessGroupName" && alias.identifier !== parts[0])) continue;
      candidate = {...reference, identifier: parts[0],
        attributes: {...reference.attributes, AccessGroupName: parts[0], FileSystemType: parts[1]}};
    }
    if (reference.resourceType === "alicloud_nas_mount_target" &&
        reference.identifierField === "PhysicalResourceId") {
      // Terraform's formal provider import ID is FileSystemId:MountTargetDomain,
      // not an API domain. Project only that exact two-part identity; conflicting
      // same-row parent/domain attributes must never be silently overwritten.
      const parts = reference.identifier.split(":");
      if (parts.length !== 2 || !parts.every(literalIdentifier) ||
          !/^[A-Za-z0-9.-]+\.nas\.aliyuncs\.com$/.test(parts[1]) ||
          (reference.attributes?.FileSystemId && reference.attributes.FileSystemId !== parts[0]) ||
          (reference.attributes?.MountTargetDomain && reference.attributes.MountTargetDomain !== parts[1])) continue;
      candidate = {...reference, identifier: parts[1], identifierField: "MountTargetDomain",
        attributes: {...reference.attributes, FileSystemId: parts[0], MountTargetDomain: parts[1]}};
    }
    const routeMissing = [];
    if (candidate.identifierField !== rule.identifierField) {
      const alias = candidate.aliases?.find((item) => item.identifierField === rule.identifierField);
      if (alias) candidate = {...candidate, ...alias};
      else routeMissing.push(rule.identifierField);
    }
    for (const field of rule.requiredFields || spec.parentFields) {
      if (!candidate.attributes?.[field] && field !== candidate.identifierField) routeMissing.push(field);
    }
    if (routeMissing.length) {
      if (!closest || routeMissing.length < closest.missing.length) closest = {reference:candidate, missing:routeMissing};
      continue;
    }
    const url = buildResourceConsoleUrl(rule, candidate);
    if (url) return {status: "resolved", url, reference:candidate, ruleId: rule.ruleId};
  }
  return closest
    ? {status: "needs_context", reference:closest.reference, missing:closest.missing, candidates}
    : {status: "unsupported", reference};
}

/** Recheck an explicit local choice against current facts, not a cached URL. */
export function resolveResourceChoice(occurrence, choice, index, rules = RESOURCE_LINK_RULES) {
  let reference = choice.referenceId ? index.references.get(choice.referenceId) : null;
  if (choice.referenceId && (!reference || !index.hasLocator(choice.referenceId, occurrence))) return null;
  const spec = resourceSpec(reference?.resourceType || occurrence.resourceType);
  if (!spec) return null;
  if (occurrence.resourceType && canonicalResourceType(occurrence.resourceType) !== spec.resourceType) return null;
  const fields = ["accountScope", "consolePartition", ...(spec.scopeKind === "regional" ? ["regionId"] : [])];
  // A selection may fill missing scope, but cannot rewrite an observed object's
  // account, region or namespace while retaining that object's aliases/status.
  for (const field of fields) {
    if (reference?.[field] && choice[field] && reference[field] !== choice[field]) return null;
    if (occurrence[field] && choice[field] && occurrence[field] !== choice[field]) return null;
  }
  for (const field of spec.parentFields) {
    const value = choice.attributes?.[field];
    if (reference?.attributes?.[field] && value && reference.attributes[field] !== value) return null;
    if (occurrence.attributes?.[field] && value && occurrence.attributes[field] !== value) return null;
  }
  if (!reference) {
    return resolveResourceOccurrence(withContext({...occurrence, scopeKind: spec.scopeKind}, choice), index, rules);
  }
  reference = withContext(reference, choice);
  // Do not retain candidates/URLs/status copied from the UI. Known references
  // are always freshly read above, including deletion and alias revocation.
  const result = resolveResourceOccurrence(occurrence, {candidates: () => [reference], bindingFor: () => null}, rules);
  if (result.reason === "deleted_name")
    result.replacements = sameNameReplacements(occurrence, reference, index.candidates(occurrence));
  return result;
}
