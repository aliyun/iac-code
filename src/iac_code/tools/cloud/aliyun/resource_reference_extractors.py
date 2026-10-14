"""Extract resource facts from reviewed Alibaba Cloud API response shapes.

No cloud calls, filesystem scans, arbitrary recursive *Id matching or URL
generation. Paths are relative to business responses, before display truncation.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import secrets
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Iterator
from functools import lru_cache
from pathlib import Path
from typing import Any

from iac_code.services.providers.aliyun import AliyunCredential
from iac_code.tools.cloud.resource_references import (
    locator_text,
    resource_attributes,
    resource_locator_text,
    resource_spec,
    sanitize_resource_reference,
    stack_resource_reference,
)

_SCOPE_SALT = secrets.token_bytes(32)
_SCOPES: OrderedDict[bytes, str] = OrderedDict()
_SCOPE_LOCK = threading.Lock()
logger = logging.getLogger(__name__)
_DIRECT_API_CATALOG_PATH = Path(__file__).parent / "data" / "direct_resource_api_catalog.json"


def collect_api_resource_references(*, credential: Any, **request: Any) -> list[dict[str, Any]]:
    """Optional display metadata must never turn API success into tool failure."""
    try:
        return extract_api_resource_references(**request, **credential_reference_context(credential))
    except Exception:
        logger.warning("Resource reference extraction failed")
        return []


def credential_reference_context(credential: Any) -> dict[str, str]:
    """Return an opaque execution scope, never a public key-derived fingerprint.

    Equality is checked only in a bounded process-local cache using a salted
    private digest. The public scope is independently random. After restart a
    new unknown scope is intentional: configuration paths do not prove account
    identity. This function must never load or refresh credentials.
    """
    if not isinstance(credential, AliyunCredential):
        return {}
    if (
        credential.mode == "OAuth"
        and credential.credential_source == "iac_code"
        and credential.credential_source_path
        and credential.oauth_session_id
    ):
        # OAuth access, refresh and exchanged STS credentials all rotate. They
        # identify one configured credential slot, not different cloud
        # accounts. Keeping them in the digest made concurrent read-only calls
        # publish a different account scope for every refresh, so otherwise
        # unambiguous resource IDs became ambiguous and sign-in rejected the
        # current credential as "changed". The persisted login generation
        # distinguishes explicit account replacement, including in another
        # process, while remaining stable across routine token refreshes.
        # Legacy/external profiles have no managed generation: their path alone
        # cannot prove that a replacement credential belongs to the same account.
        identity = (
            "oauth_source",
            credential.mode,
            credential.oauth_site_type,
            credential.credential_source,
            credential.credential_source_path,
            credential.oauth_session_id,
        )
    else:
        identity_fields = (
            "mode",
            "access_key_id",
            "access_key_secret",
            "sts_token",
            "ram_role_arn",
            "ram_role_name",
            "oauth_refresh_token",
            "credential_source",
            "credential_source_path",
        )
        identity = tuple(getattr(credential, key) for key in identity_fields)
    material = json.dumps(identity).encode()
    cache_key = hmac.new(_SCOPE_SALT, material, hashlib.sha256).digest()
    with _SCOPE_LOCK:
        scope = _SCOPES.get(cache_key)
        if scope is None:
            scope = "credential:" + uuid.uuid4().hex
            _SCOPES[cache_key] = scope
            if len(_SCOPES) > 256:
                _SCOPES.popitem(last=False)
        _SCOPES.move_to_end(cache_key)
    result = {"account_scope": scope}
    if credential.oauth_site_type in {"CN", "INTL"}:
        result["console_partition"] = "aliyun" if credential.oauth_site_type == "CN" else "alibabacloud"
    return result


def credential_reference_scope_matches(credential: Any, account_scope: str) -> bool | None:
    """Compare an opaque scope without treating a prior-process value as a mismatch.

    ``None`` means the scope was not issued by this process. A caller may accept
    that state only after proving the scope belongs to its own durable session.
    """
    current = credential_reference_context(credential).get("account_scope")
    if current == account_scope:
        return True
    with _SCOPE_LOCK:
        return False if account_scope in _SCOPES.values() else None


# (product, action) -> (canonical resource type, path to each object).
# APIs with the same field spelling are still separate product/action contracts.
_OBJECT_PATHS: dict[tuple[str, str], tuple[str, tuple[str, ...]]] = {
    ("oss", "GetBucketInfo"): ("ALIYUN::OSS::Bucket", ("bucket_info",)),
    ("ecs", "CreateInstance"): ("ALIYUN::ECS::Instance", ()),
    ("ecs", "DescribeInstances"): ("ALIYUN::ECS::Instance", ("Instances", "Instance", "*")),
    ("ecs", "DescribeInstanceAttribute"): ("ALIYUN::ECS::Instance", ()),
    ("ecs", "CreateDisk"): ("ALIYUN::ECS::Disk", ()),
    ("ecs", "DescribeDisks"): ("ALIYUN::ECS::Disk", ("Disks", "Disk", "*")),
    ("ecs", "CreateSnapshot"): ("ALIYUN::ECS::Snapshot", ()),
    ("ecs", "DescribeSnapshots"): ("ALIYUN::ECS::Snapshot", ("Snapshots", "Snapshot", "*")),
    ("ecs", "CreateImage"): ("ALIYUN::ECS::Image", ()),
    ("ecs", "DescribeImages"): ("ALIYUN::ECS::Image", ("Images", "Image", "*")),
    ("ecs", "CreateNetworkInterface"): ("ALIYUN::ECS::NetworkInterface", ()),
    ("ecs", "DescribeNetworkInterfaces"): (
        "ALIYUN::ECS::NetworkInterface",
        ("NetworkInterfaceSets", "NetworkInterfaceSet", "*"),
    ),
    ("ecs", "CreateKeyPair"): ("ALIYUN::ECS::SSHKeyPair", ()),
    ("ecs", "ImportKeyPair"): ("ALIYUN::ECS::SSHKeyPair", ()),
    ("ecs", "DescribeKeyPairs"): ("ALIYUN::ECS::SSHKeyPair", ("KeyPairs", "KeyPair", "*")),
    ("ecs", "CreateSecurityGroup"): ("ALIYUN::ECS::SecurityGroup", ()),
    ("ecs", "DescribeSecurityGroups"): ("ALIYUN::ECS::SecurityGroup", ("SecurityGroups", "SecurityGroup", "*")),
    ("vpc", "CreateVpc"): ("ALIYUN::VPC::VPC", ()),
    ("vpc", "DescribeVpcs"): ("ALIYUN::VPC::VPC", ("Vpcs", "Vpc", "*")),
    ("vpc", "CreateVSwitch"): ("ALIYUN::VPC::VSwitch", ()),
    ("vpc", "DescribeVSwitches"): ("ALIYUN::VPC::VSwitch", ("VSwitches", "VSwitch", "*")),
    ("vpc", "AllocateEipAddress"): ("ALIYUN::VPC::EIP", ()),
    ("vpc", "DescribeEipAddresses"): ("ALIYUN::VPC::EIP", ("EipAddresses", "EipAddress", "*")),
    ("rds", "CreateDBInstance"): ("ALIYUN::RDS::DBInstance", ()),
    ("rds", "DescribeDBInstances"): ("ALIYUN::RDS::DBInstance", ("Items", "DBInstance", "*")),
    ("rds", "DescribeDBInstanceAttribute"): ("ALIYUN::RDS::DBInstance", ("Items", "DBInstanceAttribute", "*")),
    ("slb", "CreateLoadBalancer"): ("ALIYUN::SLB::LoadBalancer", ()),
    ("slb", "DescribeLoadBalancers"): ("ALIYUN::SLB::LoadBalancer", ("LoadBalancers", "LoadBalancer", "*")),
    ("alb", "CreateLoadBalancer"): ("ALIYUN::ALB::LoadBalancer", ()),
    ("alb", "ListLoadBalancers"): ("ALIYUN::ALB::LoadBalancer", ("LoadBalancers", "*")),
    ("nlb", "CreateLoadBalancer"): ("ALIYUN::NLB::LoadBalancer", ()),
    ("nlb", "ListLoadBalancers"): ("ALIYUN::NLB::LoadBalancer", ("LoadBalancers", "*")),
    ("r-kvstore", "CreateInstance"): ("ALIYUN::REDIS::Instance", ()),
    ("r-kvstore", "DescribeInstances"): ("ALIYUN::REDIS::Instance", ("Instances", "KVStoreInstance", "*")),
    ("ram", "CreatePolicy"): ("ALIYUN::RAM::ManagedPolicy", ("Policy",)),
    ("ram", "GetPolicy"): ("ALIYUN::RAM::ManagedPolicy", ("Policy",)),
    ("ram", "CreateRole"): ("ALIYUN::RAM::Role", ("Role",)),
    ("ram", "GetRole"): ("ALIYUN::RAM::Role", ("Role",)),
    ("ram", "ListRoles"): ("ALIYUN::RAM::Role", ("Roles", "Role", "*")),
    ("ram", "CreateUser"): ("ALIYUN::RAM::User", ("User",)),
    ("ram", "GetUser"): ("ALIYUN::RAM::User", ("User",)),
    ("ram", "ListUsers"): ("ALIYUN::RAM::User", ("Users", "User", "*")),
    ("resourcemanager", "CreateResourceGroup"): ("ALIYUN::ResourceManager::ResourceGroup", ("ResourceGroup",)),
    ("resourcemanager", "GetResourceGroup"): ("ALIYUN::ResourceManager::ResourceGroup", ("ResourceGroup",)),
    ("ros", "ListStacks"): ("ALIYUN::ROS::Stack", ("Stacks", "*")),
    ("alb", "CreateListener"): ("ALIYUN::ALB::Listener", ()),
    ("alb", "GetListenerAttribute"): ("ALIYUN::ALB::Listener", ()),
    ("alb", "ListListeners"): ("ALIYUN::ALB::Listener", ("Listeners", "*")),
    ("slb", "CreateVServerGroup"): ("ALIYUN::SLB::VServerGroup", ()),
    ("slb", "CreateAccessControlList"): ("ALIYUN::SLB::AccessControl", ()),
    ("slb", "DescribeAccessControlListAttribute"): ("ALIYUN::SLB::AccessControl", ()),
}
_REQUEST_TYPES = {
    ("ram", "DeleteRole"): "ALIYUN::RAM::Role",
    ("ram", "AttachPolicyToRole"): "ALIYUN::RAM::Role",
    ("ram", "DetachPolicyFromRole"): "ALIYUN::RAM::Role",
    ("ram", "ListPoliciesForRole"): "ALIYUN::RAM::Role",
    ("ecs", "DeleteInstance"): "ALIYUN::ECS::Instance",
    ("ecs", "DeleteDisk"): "ALIYUN::ECS::Disk",
    ("ecs", "DeleteSnapshot"): "ALIYUN::ECS::Snapshot",
    ("ecs", "DeleteImage"): "ALIYUN::ECS::Image",
    ("ecs", "DeleteNetworkInterface"): "ALIYUN::ECS::NetworkInterface",
    ("ecs", "DeleteKeyPairs"): "ALIYUN::ECS::SSHKeyPair",
    ("vpc", "DeleteVpc"): "ALIYUN::VPC::VPC",
    ("vpc", "DeleteVSwitch"): "ALIYUN::VPC::VSwitch",
    ("rds", "DeleteDBInstance"): "ALIYUN::RDS::DBInstance",
}


@lru_cache(maxsize=1)
def direct_api_response_contracts() -> tuple[dict[str, Any], ...]:
    """Return generated inventory and reviewed SLB ACL root contracts.

    The catalog is generated from ROS resource identities and ROS' own
    read-only cleanup scanners.  Keeping paths explicit avoids interpreting an
    unrelated nested ``*Id`` as a resource merely because its spelling looks
    familiar.
    """
    data = json.loads(_DIRECT_API_CATALOG_PATH.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schemaVersion") != 1:
        raise RuntimeError("invalid direct resource API catalog")
    raw_contracts = data.get("responseContracts")
    if not isinstance(raw_contracts, list):
        raise RuntimeError("invalid direct resource API response contracts")
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, tuple[str, ...]]] = set()
    for item in raw_contracts:
        if not isinstance(item, dict):
            raise RuntimeError("invalid direct resource API response contract")
        product = item.get("product")
        action = item.get("action")
        resource_type = item.get("resourceType")
        path = item.get("path")
        identifier_field = item.get("identifierField")
        regional = item.get("regional")
        match_field = item.get("matchField")
        match_values = item.get("matchValues")
        if (
            not all(isinstance(value, str) and value for value in (product, action, resource_type))
            or not isinstance(path, list)
            or not path
            or not all(isinstance(value, str) and value for value in path)
            or path[-1] != "*"
            or not isinstance(identifier_field, str)
            or not identifier_field
            or not isinstance(regional, bool)
            or resource_spec(resource_type) is None
            or (match_field is not None and (not isinstance(match_field, str) or not match_field))
            or (
                match_values is not None
                and (
                    not isinstance(match_values, list)
                    or not match_values
                    or not all(isinstance(value, str) and value for value in match_values)
                )
            )
            or ((match_field is None) != (match_values is None))
        ):
            raise RuntimeError("invalid direct resource API response contract fields")
        key = (product.casefold(), action.casefold(), resource_type, tuple(path))
        if key in seen:
            raise RuntimeError("duplicate direct resource API response contract")
        seen.add(key)
        result.append(
            {
                "product": product,
                "action": action,
                "resource_type": resource_type,
                "path": tuple(path),
                "identifier_field": identifier_field,
                "regional": regional,
                "match_field": match_field or "",
                "match_values": tuple(match_values or ()),
            }
        )
    for action in ("CreateAccessControlList", "DescribeAccessControlListAttribute"):
        resource_type, path = _OBJECT_PATHS[("slb", action)]
        result.append(
            {
                "product": "slb",
                "action": action,
                "resource_type": resource_type,
                "path": path,
                "identifier_field": "AclId",
                "regional": True,
                "match_field": "",
                "match_values": (),
            }
        )
    return tuple(result)


@lru_cache(maxsize=1)
def _generated_contract_lookup() -> dict[
    tuple[str, str], tuple[tuple[str, tuple[str, ...], str, str, tuple[str, ...]], ...]
]:
    grouped: dict[tuple[str, str], list[tuple[str, tuple[str, ...], str, str, tuple[str, ...]]]] = {}
    for item in direct_api_response_contracts():
        key = (item["product"].casefold(), item["action"].casefold())
        grouped.setdefault(key, []).append(
            (
                item["resource_type"],
                item["path"],
                item["identifier_field"],
                item["match_field"],
                item["match_values"],
            )
        )
    return {key: tuple(value) for key, value in grouped.items()}


def _objects(value: Any, path: tuple[str, ...], prefix: str = "") -> Iterator[tuple[Any, str]]:
    if not path:
        yield value, prefix
    elif path[0] == "*" and isinstance(value, list):
        for index, child in enumerate(value):
            yield from _objects(child, path[1:], f"{prefix}/{index}")
    elif isinstance(value, dict) and path[0] in value:
        yield from _objects(value[path[0]], path[1:], f"{prefix}/{path[0]}")


def _nas_filesystem_mount_targets(body: Any) -> Iterator[tuple[dict[str, Any], str]]:
    """Join a mount target only to its own reviewed filesystem response row."""
    for filesystem, parent_path in _objects(body, ("FileSystems", "FileSystem", "*"), "/result"):
        if not isinstance(filesystem, dict):
            continue
        parent = locator_text(filesystem.get("FileSystemId"))
        if not parent:
            continue
        for mount, source_path in _objects(filesystem, ("MountTargets", "MountTarget", "*"), parent_path):
            if not isinstance(mount, dict):
                continue
            claimed_parent = locator_text(mount.get("FileSystemId"))
            if claimed_parent and claimed_parent != parent:
                continue
            yield {**mount, "FileSystemId": parent}, source_path


def _polardb_endpoint_addresses(body: Any) -> Iterator[tuple[dict[str, Any], str]]:
    """Bind each address to its own endpoint row, never a sibling endpoint."""
    for endpoint, parent_path in _objects(body, ("Items", "*"), "/result"):
        if not isinstance(endpoint, dict) or not (parent := locator_text(endpoint.get("DBEndpointId"))):
            continue
        for address, source_path in _objects(endpoint, ("AddressItems", "*"), parent_path):
            if not isinstance(address, dict):
                continue
            claimed = locator_text(address.get("DBEndpointId"))
            if claimed and claimed != parent:
                continue
            cluster = locator_text(endpoint.get("DBClusterId"))
            claimed_cluster = locator_text(address.get("DBClusterId"))
            if cluster and claimed_cluster and cluster != claimed_cluster:
                continue
            yield {**address, **({"DBClusterId": cluster} if cluster else {}), "DBEndpointId": parent}, source_path


def _slb_child_row(
    resource_type: str, obj: Any, body: Any, params: dict[str, Any], action: str
) -> dict[str, Any] | None:
    """Bind only reviewed SLB child identities to their own request/root.

    Reject contradictory parents/components rather than inventing an ID from
    arbitrary nested fields or silently moving a child to another instance.
    """
    if not isinstance(obj, dict):
        return None
    root = body if isinstance(body, dict) else {}
    parent_sources = (params, root, obj)
    if resource_type == "alicloud_slb_listener" and action == "DescribeLoadBalancerListeners":
        # The bulk API's filter is an array, not a parent identity. Every
        # listener must carry its own parent; never borrow the first filter.
        parent = locator_text(obj.get("LoadBalancerId"))
        requested = params.get("LoadBalancerId")
        if not parent or (
            requested is not None
            and (
                not isinstance(requested, list)
                or not 1 <= len(requested) <= 10
                or any(not isinstance(value, str) or not value.strip() for value in requested)
                or parent not in requested
            )
        ):
            return None
        parent_sources = (root, obj)
    parents = {value for source in parent_sources if (value := locator_text(source.get("LoadBalancerId")))}
    if len(parents) != 1:
        return None
    parent = next(iter(parents))
    row = {**obj, "LoadBalancerId": parent}
    if resource_type == "alicloud_slb_listener":
        port = obj.get("ListenerPort")
        protocol = obj.get("ListenerProtocol")
        if (
            isinstance(port, bool)
            or not isinstance(port, int)
            or not 1 <= port <= 65535
            or protocol not in {"http", "https", "tcp", "udp"}
        ):
            return None
        identifier = f"{parent}:{protocol}:{port}"
        if obj.get("ListenerId", identifier) != identifier or obj.get("load_balancer_id", parent) != parent:
            return None
        row.update(ListenerId=identifier, load_balancer_id=parent)
    elif resource_type == "alicloud_slb_rule":
        for field in ("ListenerPort", "ListenerProtocol"):
            claimed, requested = obj.get(field), params.get(field)
            if claimed is not None and requested is not None and str(claimed) != str(requested):
                return None
            row[field] = claimed if claimed is not None else requested
        port, protocol = row["ListenerPort"], row["ListenerProtocol"]
        if (
            isinstance(port, bool)
            or not isinstance(port, int)
            or not 1 <= port <= 65535
            or protocol not in {"http", "https"}
        ):
            return None
    return row


def extract_api_resource_references(
    *,
    product: str,
    action: str,
    params: Any,
    body: Any,
    region_id: str,
    tool_use_id: str,
    account_scope: str = "",
    console_partition: str = "",
) -> list[dict[str, Any]]:
    """Extract only reviewed fields. Call after a successful business response."""
    product = product.casefold()
    params = params if isinstance(params, dict) else {}
    request_region_id = locator_text(params.get("RegionId")) or region_id
    references: list[dict[str, Any]] = []
    source_id = "api:" + uuid.uuid4().hex if not tool_use_id else ""
    observed_at = time.time()

    def add(
        resource_type: str,
        obj: Any,
        path: str,
        evidence: str,
        *,
        fallback: Any = None,
        identifier_path: str | None = None,
        response_identifier_field: str = "",
    ) -> None:
        spec = resource_spec(resource_type)
        if spec is None or not isinstance(obj, dict):
            return
        observed = dict(obj)
        source_identifier_field = spec.identifier_field
        if response_identifier_field and spec.identifier_field not in observed:
            observed[spec.identifier_field] = observed.get(response_identifier_field)
            source_identifier_field = response_identifier_field
        for field in (
            spec.identifier_field,
            *spec.parent_fields,
            *spec.attribute_fields,
            *(field for field, _kind in spec.alias_fields),
        ):
            raw = observed.get(field)
            if isinstance(raw, bool):
                observed[field] = str(raw).lower()
            elif isinstance(raw, int):
                observed[field] = str(raw)
        identifier = resource_locator_text(observed.get(spec.identifier_field), spec, spec.identifier_field)
        if identifier is None:
            return
        attributes = resource_attributes(observed, spec)
        # A reviewed list response is scoped by its request parent fields even
        # when individual response rows omit those fields.  Merge only the
        # declared parent locators; do not promote arbitrary request IDs or
        # names into response evidence.
        if evidence == "api_response" and isinstance(fallback, dict):
            selector_fields = tuple(
                dict.fromkeys(
                    (
                        spec.identifier_field,
                        response_identifier_field,
                        *(field for field, _kind in spec.alias_fields),
                    )
                )
            )
            request_selectors = {
                text for field in selector_fields if field and (text := locator_text(fallback.get(field))) is not None
            }
            response_selectors = {
                text for field in selector_fields if field and (text := locator_text(observed.get(field))) is not None
            }
            request_parents = {
                field: text for field in spec.parent_fields if (text := locator_text(fallback.get(field))) is not None
            }
            # A list request can contain both a parent filter and a resource
            # selector.  Only attach the parent when that selector is absent
            # or names this exact response row; otherwise a broad/inconsistent
            # response must not lend one row another row's parent context.
            if not request_selectors or request_selectors & response_selectors:
                attributes = {**request_parents, **attributes}
        # A get-style response may omit the identifier repeated in the request.
        if isinstance(fallback, dict) and fallback.get(spec.identifier_field) == identifier:
            attributes = {**resource_attributes(fallback, spec), **attributes}
        aliases = []
        if (
            evidence == "api_response"
            and spec.stable_field
            and (stable_id := locator_text(observed.get(spec.stable_field)))
        ):
            aliases.append({"identifier": stable_id, "identifier_field": spec.stable_field, "identifier_kind": "id"})
        if evidence == "api_response":
            for alias_field, alias_kind in spec.alias_fields:
                alias = resource_locator_text(observed.get(alias_field), spec, alias_field)
                if alias is not None and (
                    alias_field != spec.identifier_field or (alias_kind, alias) != (spec.identifier_kind, identifier)
                ):
                    aliases.append(
                        {
                            "identifier": alias,
                            "identifier_field": alias_field,
                            "identifier_kind": alias_kind,
                        }
                    )
        source: dict[str, Any] = {
            "tool_use_id": tool_use_id,
            "source_path": identifier_path or f"{path}/{source_identifier_field}",
            "request_region_id": request_region_id,
            "evidence": evidence,
            "product": product,
            "action": action,
            "observed_at": observed_at,
        }
        if source_id:
            source["source_id"] = source_id
        candidate: dict[str, Any] = {
            "resource_type": resource_type,
            "identifier": identifier,
            "identifier_kind": spec.identifier_kind,
            "identifier_field": spec.identifier_field,
            # CEN VPC attachment list rows expose the resource region as
            # VpcRegionId rather than RegionId.  Prefer either response field
            # over the request/default endpoint region so the console route
            # cannot silently point at the wrong region.
            # Explicitly reviewed response contracts may come from RPC,
            # lower-camel JSON, SDK-normalized payloads (OSS uses lower-case
            # ``region``), or lower-snake-case payloads.  ACK
            # ``DescribeClusters`` is the last case: one transport-unwrapped
            # answer spans regions while every row owns its own ``region_id``,
            # so the request endpoint region is never authoritative per row.
            # Prefer a row-owned region in every reviewed spelling, and only a
            # non-blank one, before falling back to the request endpoint.
            "region_id": (
                observed.get("RegionId")
                or observed.get("regionId")
                or locator_text(observed.get("region_id"))
                or observed.get("region")
                or observed.get("VpcRegionId")
                or request_region_id
            ),
            "account_scope": account_scope,
            "console_partition": console_partition,
            "attributes": attributes,
            "aliases": aliases,
            "sources": [source],
            "evidence": evidence,
        }
        # Delete success is operation evidence even though its identifier is in
        # the input. Keep that source distinct from a returned resource object.
        if evidence == "tool_input" and action == "DeleteRole":
            candidate["status"] = "DELETE_COMPLETE"
        clean = sanitize_resource_reference(candidate)
        if clean is not None:
            references.append(clean)

    explicit_contract = _OBJECT_PATHS.get((product, action))
    contracts: list[tuple[str, tuple[str, ...]]] = []
    if explicit_contract:
        contracts.append(explicit_contract)
    for contract in _generated_contract_lookup().get((product, action.casefold()), ()):
        generated = contract[:2]
        if generated not in contracts:
            contracts.append(generated)
    request_type = _REQUEST_TYPES.get((product, action)) or (explicit_contract[0] if explicit_contract else None)
    ram_policy_root = product == "ram" and action in {"CreatePolicy", "GetPolicy"}
    resource_group_root = product == "resourcemanager" and action in {"CreateResourceGroup", "GetResourceGroup"}
    if request_type and not ram_policy_root and not resource_group_root:
        add(request_type, params, "/params", "tool_input")
    generated_fields = {
        (resource_type, path): identifier_field
        for resource_type, path, identifier_field, _match_field, _match_values in _generated_contract_lookup().get(
            (product, action.casefold()), ()
        )
    }
    generated_matches = {
        (resource_type, path): (match_field, {value.casefold() for value in match_values})
        for resource_type, path, _identifier_field, match_field, match_values in _generated_contract_lookup().get(
            (product, action.casefold()), ()
        )
        if match_field
    }
    for resource_type, path in contracts:
        # Some ROA APIs (notably ACK DescribeClusters) are normalized by the
        # tool transport from ``{"clusters": [...]}`` to the collection
        # itself.  Keep the reviewed contract path authoritative while
        # accepting that lossless wrapper removal; the source path then points
        # at the actual visible list root.
        contract_path = ("*",) if isinstance(body, list) and len(path) == 2 and path[1] == "*" else path
        response_objects = _objects(body, contract_path, "/result")
        if (
            product == "nas"
            and action == "DescribeFileSystems"
            and resource_type == "ALIYUN::NAS::MountTarget"
            and path == ("FileSystems", "FileSystem", "*", "MountTargets", "MountTarget", "*")
        ):
            response_objects = _nas_filesystem_mount_targets(body)
        if (
            product == "polardb"
            and action == "DescribeDBClusterEndpoints"
            and resource_type == "ALIYUN::POLARDB::DBClusterEndpointAddress"
            and path == ("Items", "*", "AddressItems", "*")
        ):
            response_objects = _polardb_endpoint_addresses(body)
        for obj, source_path in response_objects:
            if resource_group_root and resource_type == "ALIYUN::ResourceManager::ResourceGroup":
                if (
                    not isinstance(body, dict)
                    or not isinstance(obj, dict)
                    or "Code" in body
                    or "ErrorCode" in body
                    or not isinstance(obj.get("Id"), str)
                    or locator_text(obj["Id"]) is None
                    or not obj["Id"].startswith("rg-")
                    or (action == "GetResourceGroup" and params.get("ResourceGroupId") != obj["Id"])
                ):
                    continue
                identifier = obj["Id"]
                if any(
                    field in source and source[field] != identifier
                    for source in (params, body, obj)
                    for field in ("Id", "ResourceGroupId")
                ) or any(
                    field in obj and field in source and source[field] != obj[field]
                    for source in (params, body)
                    for field in ("Name", "DisplayName", "AccountId")
                ):
                    continue
                add(resource_type, obj, source_path, "api_response")
                continue
            if ram_policy_root and resource_type == "ALIYUN::RAM::ManagedPolicy":
                if (
                    not isinstance(obj, dict)
                    or not isinstance(obj.get("PolicyName"), str)
                    or re.fullmatch(r"[A-Za-z0-9-]{1,128}", obj["PolicyName"]) is None
                    or obj.get("PolicyType") != "Custom"
                ):
                    continue
                policy_name = obj["PolicyName"]
                if any(
                    field in source and source[field] != expected
                    for source in (params, body, obj)
                    for field, expected in (
                        ("PolicyName", policy_name),
                        ("PhysicalResourceId", policy_name),
                        ("PolicyType", "Custom"),
                    )
                ):
                    continue
                add(
                    resource_type,
                    {"PhysicalResourceId": policy_name, "PolicyName": policy_name, "PolicyType": "Custom"},
                    source_path,
                    "api_response",
                    identifier_path=f"{source_path}/PolicyName",
                )
                continue
            if product == "maxcompute" and resource_type == "ALIYUN::MaxCompute::Project":
                # ListProjects owns each locator at data.projects[*].name and
                # regionId. Never borrow a project from saleTag, the request,
                # another row or a similarly named product/API.
                if (
                    action != "ListProjects"
                    or path != ("data", "projects", "*")
                    or not isinstance(body, dict)
                    or body.get("errorCode")
                    or ("httpCode" in body and body["httpCode"] != 200)
                    or not isinstance(obj, dict)
                    or not isinstance(obj.get("name"), str)
                    or not obj["name"]
                    or locator_text(obj["name"]) != obj["name"]
                    or not isinstance(obj.get("regionId"), str)
                    or locator_text(obj["regionId"]) != obj["regionId"]
                    or obj["regionId"] != request_region_id
                    or any(params[field] != obj["regionId"] for field in ("region", "RegionId") if field in params)
                    or any(
                        obj[field] != obj["regionId"] for field in ("RegionId", "region", "region_id") if field in obj
                    )
                ):
                    continue
            if product == "polardb" and action in {"DescribeDatabases", "DescribeDBClusterEndpoints"}:
                # The exact request scopes this list. Conflicting response
                # parents cannot be used as evidence for the requested cluster.
                requested = locator_text(params.get("DBClusterId"))
                claimed = locator_text(obj.get("DBClusterId")) if isinstance(obj, dict) else None
                if requested and claimed and requested != claimed:
                    continue
            match = generated_matches.get((resource_type, path))
            if match and (
                not isinstance(obj, dict)
                or not isinstance(obj.get(match[0]), str)
                or obj[match[0]].casefold() not in match[1]
            ):
                continue
            response_field = generated_fields.get((resource_type, path), "")
            # The OTS VPC-binding inventory places the parent instance name at
            # the response root (and requires it in the request), while each
            # VpcList row carries the binding itself.  Join only that reviewed
            # parent field for this exact response contract; never recurse
            # through unrelated IDs or borrow context from another call.
            response_obj = obj
            if product == "oss" and action == "GetBucketInfo" and resource_type == "ALIYUN::OSS::Bucket":
                if (
                    not isinstance(obj, dict)
                    or not isinstance(obj.get("name"), str)
                    or re.fullmatch(r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]", obj["name"]) is None
                    or obj["name"] != params.get("bucket")
                    or obj.get("BucketName", obj["name"]) != obj["name"]
                    or obj.get("location") != "oss-" + request_region_id
                    or any(
                        obj.get(field, request_region_id) != request_region_id
                        for field in ("region_id", "RegionId", "region")
                    )
                ):
                    continue
                response_field = "name"
            if product == "vpc" and resource_type == "ALIYUN::VPC::CustomerGateway":
                # The reviewed API locator is an ID string, never a coerced
                # boolean/number or an identifier from another VPC object.
                if not isinstance(obj, dict) or not isinstance(obj.get("CustomerGatewayId"), str):
                    continue
            if product == "slb" and resource_type == "alicloud_slb_server_certificate":
                # This API's certificate locator is a string, not a boolean
                # coerced through the generic scalar identifier helper.
                if not isinstance(obj, dict) or not isinstance(obj.get("ServerCertificateId"), str):
                    continue
            if product == "slb" and resource_type in {
                "alicloud_slb_listener",
                "alicloud_slb_master_slave_server_group",
                "alicloud_slb_rule",
            }:
                response_obj = _slb_child_row(resource_type, obj, body, params, action)
                if response_obj is None:
                    continue
            if (
                resource_type == "ALIYUN::OTS::VpcBinder"
                and action == "ListVpcInfoByInstance"
                and isinstance(obj, dict)
            ):
                instance_name = body.get("InstanceName") if isinstance(body, dict) else None
                response_obj = {
                    **obj,
                    "InstanceName": instance_name or params.get("InstanceName"),
                }
            add(
                resource_type,
                response_obj,
                source_path,
                "api_response",
                fallback=params,
                response_identifier_field=response_field,
                identifier_path=(f"{source_path}/ListenerPort" if resource_type == "alicloud_slb_listener" else None),
            )
            if not isinstance(obj, dict):
                continue
            region = obj.get("RegionId") or request_region_id
            if resource_type == "ALIYUN::ECS::Instance":
                # These belong to each instance, not to another row or the last
                # instance's region. Never interpret every nested Id as InstanceId.
                vpc = obj.get("VpcAttributes")
                if isinstance(vpc, dict):
                    related = {**vpc, "RegionId": region}
                    add("ALIYUN::VPC::VPC", related, f"{source_path}/VpcAttributes", "api_response")
                    add("ALIYUN::VPC::VSwitch", related, f"{source_path}/VpcAttributes", "api_response")
                for group_id, group_path in _objects(obj, ("SecurityGroupIds", "SecurityGroupId", "*"), source_path):
                    add(
                        "ALIYUN::ECS::SecurityGroup",
                        {"SecurityGroupId": group_id, "RegionId": region},
                        group_path,
                        "api_response",
                        identifier_path=group_path,
                    )
                add(
                    "ALIYUN::ECS::Image",
                    {"ImageId": obj.get("ImageId"), "RegionId": region},
                    source_path,
                    "api_response",
                    identifier_path=f"{source_path}/ImageId",
                )
                for interface, interface_path in _objects(
                    obj, ("NetworkInterfaces", "NetworkInterface", "*"), source_path
                ):
                    if isinstance(interface, dict):
                        add(
                            "ALIYUN::ECS::NetworkInterface",
                            {**interface, "RegionId": region},
                            interface_path,
                            "api_response",
                        )
            elif resource_type == "ALIYUN::ECS::Disk":
                add(
                    "ALIYUN::ECS::Instance",
                    {"InstanceId": obj.get("InstanceId"), "RegionId": region},
                    source_path,
                    "api_response",
                    identifier_path=f"{source_path}/InstanceId",
                )
                add(
                    "ALIYUN::ECS::Snapshot",
                    {"SnapshotId": obj.get("SourceSnapshotId"), "RegionId": region},
                    source_path,
                    "api_response",
                    identifier_path=f"{source_path}/SourceSnapshotId",
                )
            elif resource_type == "ALIYUN::ECS::Snapshot":
                add(
                    "ALIYUN::ECS::Disk",
                    {"DiskId": obj.get("SourceDiskId"), "RegionId": region},
                    source_path,
                    "api_response",
                    identifier_path=f"{source_path}/SourceDiskId",
                )
            elif resource_type == "ALIYUN::ECS::Image":
                for mapping, mapping_path in _objects(
                    obj, ("DiskDeviceMappings", "DiskDeviceMapping", "*"), source_path
                ):
                    if isinstance(mapping, dict):
                        add(
                            "ALIYUN::ECS::Snapshot",
                            {"SnapshotId": mapping.get("SnapshotId"), "RegionId": region},
                            mapping_path,
                            "api_response",
                        )
            elif resource_type == "ALIYUN::ECS::NetworkInterface":
                attachment = obj.get("Attachment")
                attached_instance = attachment.get("InstanceId") if isinstance(attachment, dict) else None
                add(
                    "ALIYUN::ECS::Instance",
                    {"InstanceId": obj.get("InstanceId") or attached_instance, "RegionId": region},
                    source_path,
                    "api_response",
                    identifier_path=(
                        f"{source_path}/InstanceId" if obj.get("InstanceId") else f"{source_path}/Attachment/InstanceId"
                    ),
                )
                add(
                    "ALIYUN::VPC::VPC",
                    {"VpcId": obj.get("VpcId"), "RegionId": region},
                    source_path,
                    "api_response",
                    identifier_path=f"{source_path}/VpcId",
                )
                add(
                    "ALIYUN::VPC::VSwitch",
                    {"VSwitchId": obj.get("VSwitchId"), "RegionId": region},
                    source_path,
                    "api_response",
                    identifier_path=f"{source_path}/VSwitchId",
                )
                for group_id, group_path in _objects(obj, ("SecurityGroupIds", "SecurityGroupId", "*"), source_path):
                    add(
                        "ALIYUN::ECS::SecurityGroup",
                        {"SecurityGroupId": group_id, "RegionId": region},
                        group_path,
                        "api_response",
                        identifier_path=group_path,
                    )
            elif resource_type == "ALIYUN::VPC::RouteTable":
                # Route-table inventory rows authoritatively identify the VPC
                # and every associated vSwitch as well as the route table.  A
                # normal DescribeRouteTableList answer commonly displays all
                # three IDs, so retain those reviewed relationships instead of
                # forcing the user to re-enter context already in the response.
                add(
                    "ALIYUN::VPC::VPC",
                    {"VpcId": obj.get("VpcId"), "RegionId": region},
                    source_path,
                    "api_response",
                    identifier_path=f"{source_path}/VpcId",
                )
                for switch_id, switch_path in _objects(obj, ("VSwitchIds", "VSwitchId", "*"), source_path):
                    add(
                        "ALIYUN::VPC::VSwitch",
                        {"VSwitchId": switch_id, "RegionId": region},
                        switch_path,
                        "api_response",
                        identifier_path=switch_path,
                    )
            elif resource_type == "ALIYUN::CEN::TransitRouterVpcAttachment":
                # The attachment inventory is also authoritative for its VPC
                # and zone mappings.  Preserve these related identities so a
                # normal CEN query can link every resource ID returned by the
                # cloud API without asking the user for context that the row
                # already contains.
                vpc_region = obj.get("VpcRegionId") or obj.get("RegionId") or request_region_id
                add(
                    "ALIYUN::VPC::VPC",
                    {"VpcId": obj.get("VpcId"), "RegionId": vpc_region},
                    source_path,
                    "api_response",
                    identifier_path=f"{source_path}/VpcId",
                )
                for mapping, mapping_path in _objects(obj, ("ZoneMappings", "*"), source_path):
                    if not isinstance(mapping, dict):
                        continue
                    add(
                        "ALIYUN::VPC::VSwitch",
                        {"VSwitchId": mapping.get("VSwitchId"), "RegionId": vpc_region},
                        mapping_path,
                        "api_response",
                        identifier_path=f"{mapping_path}/VSwitchId",
                    )
                    add(
                        "ALIYUN::ECS::NetworkInterface",
                        {"NetworkInterfaceId": mapping.get("NetworkInterfaceId"), "RegionId": vpc_region},
                        mapping_path,
                        "api_response",
                        identifier_path=f"{mapping_path}/NetworkInterfaceId",
                    )
    if product == "ecs" and action == "RunInstances":
        for identifier, path in _objects(body, ("InstanceIdSets", "InstanceIdSet", "*"), "/result"):
            add("ALIYUN::ECS::Instance", {"InstanceId": identifier}, path, "api_response", identifier_path=path)
    if product == "ros":
        if action in {
            "CreateStack",
            "GetStack",
            "UpdateStack",
            "DeleteStack",
            "ListStackResources",
            "GetStackResource",
        }:
            add("ALIYUN::ROS::Stack", params, "/params", "tool_input")
            add("ALIYUN::ROS::Stack", body, "/result", "api_response")
        rows_path = ("Resources", "*") if action == "ListStackResources" else ()
        if action in {"ListStackResources", "GetStackResource"}:
            for row, path in _objects(body, rows_path, "/result"):
                if not isinstance(row, dict):
                    continue
                if row.get("ResourceType") in {"ALIYUN::OTS::Table", "alicloud_ots_table"} and (
                    row.get("StackId", params.get("StackId")) != params.get("StackId")
                    or row.get("RegionId", request_region_id) != request_region_id
                    or params.get("RegionId", request_region_id) != request_region_id
                ):
                    continue
                clean = stack_resource_reference(
                    row,
                    stack_id=str(row.get("StackId") or params.get("StackId") or ""),
                    region_id=request_region_id,
                    tool_use_id=tool_use_id,
                    source_path=path,
                    account_scope=account_scope,
                    console_partition=console_partition,
                    action=action,
                )
                if clean is not None:
                    references.append(clean)
    return references
