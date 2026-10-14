"""Resource-link E2E capability gate, installed only by the workflow launcher.

An administrator credential is not a capability. The workflow owns this private
SQLite journal; model tools cannot edit it. Exact API calls, session/credential
binding, quotas and response-derived deletion IDs are checked at the wire boundary.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import json
import os
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, TypeGuard, cast

from iac_code.services.execution_policy import ExecutionPolicyDeniedError

ENV = "IAC_CODE_TEST_EXECUTION_GATE"
SLOT_ENV = "IAC_CODE_TEST_EXECUTION_SLOT"
ERROR = "test_execution_gate_denied"
ATTEST_MODULES = (
    "scripts.web.e2e.resource_link_workflow.execution_gate",
    "scripts.web.e2e.resource_link_workflow.web_entry",
    "iac_code.services.execution_policy",
    "iac_code.agent.agent_loop",
    "iac_code.services.providers.aliyun",
    "iac_code.providers.openai_provider",
    "iac_code.providers.dashscope_provider",
    "iac_code.providers.thinking",
    "iac_code.tools.result_storage",
    "iac_code.tools.cloud.aliyun.aliyun_api",
    "iac_code.tools.cloud.aliyun.api_contract",
    "iac_code.tools.cloud.aliyun.result_contract",
    "iac_code.tools.cloud.aliyun.oss_v4_adapter",
    "iac_code.tools.cloud.resource_references",
    "iac_code.tools.cloud.aliyun.resource_reference_extractors",
    "iac_code.web.resource_references",
    "iac_code.web.resource_reference_controller",
    "iac_code.web.resource_link_catalog",
    "iac_code.web.token_transport",
    "iac_code.web.resource_signin",
    "iac_code.web.resource_signin_role",
    "iac_code.web.console_readonly_policy",
    "iac_code.web.app",
)
ATTEST_ASSETS = (
    "tools/cloud/aliyun/data/direct_resource_api_catalog.json",
    "tools/cloud/aliyun/data/oss/operation_catalog.json",
    "web/static/ros_resource_links.json",
    "web/static/index.html",
    "web/static/styles.css",
    "web/static/js/resource_link_rules.js",
    "web/static/js/resource_linkify.js",
    "web/static/js/app.js",
    "web/static/js/resource_sources.js",
    "web/static/js/resource_occurrences.js",
    "web/static/js/components/tool_cards.js",
    "web/static/js/components/blocking.js",
    "web/static/js/components/output_panel.js",
    "web/static/js/components/resource_link_popover.js",
    "web/static/js/components/resource_link_preview.js",
)
DDL = """
CREATE TABLE IF NOT EXISTS active(slot TEXT PRIMARY KEY,scope TEXT,session TEXT,account TEXT,expires REAL,phase TEXT);
CREATE TABLE IF NOT EXISTS grants(scope TEXT,id TEXT,kind TEXT,call TEXT,quota INTEGER,used INTEGER DEFAULT 0,
 parent TEXT,response_path TEXT,target_param TEXT,PRIMARY KEY(scope,id));
CREATE TABLE IF NOT EXISTS attempts(id TEXT PRIMARY KEY,scope TEXT,grant_id TEXT,session TEXT,
 state TEXT,response TEXT,credential TEXT,created REAL);
CREATE TABLE IF NOT EXISTS identities(scope TEXT,session TEXT,credential TEXT,account TEXT,
 PRIMARY KEY(scope,session,credential));
CREATE TABLE IF NOT EXISTS authority_limits(authority TEXT,id TEXT,used INTEGER,PRIMARY KEY(authority,id));
CREATE TABLE IF NOT EXISTS cleanup_identity_reviews(scope TEXT PRIMARY KEY,grant_id TEXT,session TEXT,
 account TEXT,authority TEXT,evidence TEXT,evidence_sha256 TEXT);
CREATE TABLE IF NOT EXISTS native_identity_recovery_permits(
 scope TEXT PRIMARY KEY,slot TEXT NOT NULL,session TEXT NOT NULL,account TEXT NOT NULL,
 grant_id TEXT NOT NULL,authority TEXT NOT NULL,call TEXT NOT NULL,proposal_sha256 TEXT NOT NULL,
 review_sha256 TEXT NOT NULL,original_review_sha256 TEXT NOT NULL,marker_path TEXT NOT NULL,
 expires REAL NOT NULL,state TEXT NOT NULL,
 created REAL NOT NULL,attempt_id TEXT);
"""


class GateDeniedError(ExecutionPolicyDeniedError):
    def __init__(self) -> None:
        super().__init__(ERROR)  # Never include credentials, approved params or raw response in an error.


class ReadPendingError(Exception):
    """A reviewed read is not due yet; no quota or wire intent was consumed."""

    def __init__(self, seconds: float) -> None:
        super().__init__("reviewed_read_pending")
        self.seconds = seconds


def read_policy(grant: Any) -> dict[str, Any]:
    """Missing policy means legacy behavior, never retrospective quota changes."""
    value = dict(grant).get("read_policy")
    return (json.loads(value) or {}) if isinstance(value, str) else (value or {})


def phase_allowed(grant: Any, phase: str) -> bool:
    policy = read_policy(grant)
    return not policy or policy["phase"] == phase


def canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def bound_target(grant: Any, identifier: str) -> str:
    """Encode one actual response identifier; never append a second delete target."""
    encoding = dict(grant).get("target_encoding")
    if encoding is None:
        return identifier
    if encoding == "json-singleton":
        return canonical([identifier])
    raise GateDeniedError()


def is_oss_bucket_create(call: dict[str, Any]) -> bool:
    params = call.get("params", {})
    return (
        call.get("product") == "oss"
        and call.get("action") == "PutBucket"
        and call.get("version", "2019-05-17") == "2019-05-17"
        and isinstance(params, dict)
        and set(params) == {"bucket", "x-oss-acl", "body"}
        and isinstance(params.get("bucket"), str)
        and re.fullmatch(r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]", params["bucket"]) is not None
        and params.get("x-oss-acl") == "private"
        and params.get("body")
        == {"CreateBucketConfiguration": {"StorageClass": "Standard", "DataRedundancyType": "LRS"}}
    )


def response_identifier(call: dict[str, Any], response: Any, response_path: Any) -> str | None:
    if isinstance(response_path, dict) and response_path.get("kind") == "nas_native_dependency":
        actual = response.get("call") if isinstance(response, dict) else None
        action = call.get("action")
        native_id = NAS_NATIVE_IDS.get(action, ("", ""))[0] if isinstance(action, str) else ""
        expected_path = ["headers", "location"] if action == "PutBucket" else [native_id]
        if (
            not isinstance(actual, dict)
            or response_path.get("path") != expected_path
            or any(actual.get(key) != call.get(key) for key in ("product", "action", "version", "region_id"))
            or not nas_native_success(actual, response, "create")
        ):
            return None
        return nas_native_identifier(actual, response)
    response_path = creation_response_path(response_path)
    if not isinstance(response_path, list):
        return None
    if call.get("product") == "oss" and call.get("action") == "PutBucket":
        if (
            not is_oss_bucket_create({**call, "product": "oss"})
            or response_path != ["headers", "location"]
            or not isinstance(response, dict)
            or set(response) != {"body", "status", "headers"}
            or response.get("status") != 200
            or type(response.get("status")) is not int
            or response.get("body") is not None
            or not isinstance(response.get("headers"), dict)
        ):
            return None
        headers = response["headers"]
        locations = [
            value for name, value in headers.items() if isinstance(name, str) and name.casefold() == "location"
        ]
        request_ids = [
            value for name, value in headers.items() if isinstance(name, str) and name.casefold() == "x-oss-request-id"
        ]
        if (
            len(locations) != 1
            or len(request_ids) != 1
            or not isinstance(locations[0], str)
            or re.fullmatch(r"/[a-z0-9][a-z0-9-]{1,61}[a-z0-9]", locations[0]) is None
            or not isinstance(request_ids[0], str)
            or not request_ids[0]
            or len(request_ids[0]) > 256
            or any(char.isspace() for char in request_ids[0])
        ):
            return None
        identifier = locations[0][1:]
        return identifier if identifier == call["params"]["bucket"] else None
    if response_path == ["headers", "location"]:
        return None
    identifier = response
    try:
        for key in response_path:
            identifier = identifier[key]
    except (KeyError, TypeError):
        return None
    return identifier if isinstance(identifier, str) and identifier else None


@contextmanager
def database(path: Path) -> Iterator[sqlite3.Connection]:
    if not path.is_file() or path.is_symlink():
        raise GateDeniedError()
    if os.name != "nt" and path.stat().st_mode & 0o077:
        raise GateDeniedError()
    connection = sqlite3.connect(f"file:{path.as_posix()}?mode=rw", uri=True, timeout=10)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("BEGIN IMMEDIATE")
        yield connection
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def initialize(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise GateDeniedError()
    with sqlite3.connect(path) as connection:
        connection.executescript(DDL)
        if "authority" not in {row[1] for row in connection.execute("PRAGMA table_info(grants)")}:
            connection.execute("ALTER TABLE grants ADD COLUMN authority TEXT")
        if "read_policy" not in {row[1] for row in connection.execute("PRAGMA table_info(grants)")}:
            connection.execute("ALTER TABLE grants ADD COLUMN read_policy TEXT")
        if "target_encoding" not in {row[1] for row in connection.execute("PRAGMA table_info(grants)")}:
            connection.execute("ALTER TABLE grants ADD COLUMN target_encoding TEXT")
        columns = {row[1] for row in connection.execute("PRAGMA table_info(attempts)")}
        for column in ("reference_scope", "tool_use_id"):
            if column not in columns:
                connection.execute(f"ALTER TABLE attempts ADD COLUMN {column} TEXT")
    os.chmod(path, 0o600)


def _native_identity_call(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == {"product", "action", "version", "region_id", "params"}
        and value.get("product") == "sts"
        and value.get("action") == "GetCallerIdentity"
        and value.get("version") == "2015-04-01"
        and isinstance(value.get("region_id"), str)
        and bool(value["region_id"])
        and value.get("params") == {}
    )


def arm_native_identity_recovery(
    path: Path,
    *,
    slot: str,
    scope: str,
    session: str,
    account: str,
    grant_id: str,
    authority: str,
    call: dict[str, Any],
    proposal_sha256: str,
    review_sha256: str,
    original_review_sha256: str,
    marker_path: str,
    lifetime_seconds: int = 120,
) -> None:
    """Persist one exact STS-only capability; never re-arm a latched scope."""
    if (
        not _native_identity_call(call)
        or lifetime_seconds != 120
        or not all(
            isinstance(value, str) and value
            for value in (
                slot,
                scope,
                session,
                account,
                grant_id,
                authority,
                proposal_sha256,
                review_sha256,
                original_review_sha256,
                marker_path,
            )
        )
    ):
        raise GateDeniedError()
    marker = Path(marker_path)
    try:
        marker_stat = marker.stat()
        marker_value = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as error:
        raise GateDeniedError() from error
    if (
        not isinstance(marker_value, dict)
        or not marker.is_absolute()
        or marker.is_symlink()
        or os.name != "nt"
        and marker_stat.st_mode & 0o077
        or marker_value.get("state") != "may_have_submitted"
        or marker_value.get("proposal_sha256") != proposal_sha256
        or marker_value.get("review_sha256") != review_sha256
        or marker_value.get("scope") != scope
        or marker_value.get("session_id") != session
        or marker_value.get("stage") != "identity"
    ):
        raise GateDeniedError()
    now = time.time()
    with database(path) as connection:
        active = connection.execute("SELECT * FROM active WHERE slot=?", (slot,)).fetchone()
        grant = connection.execute(
            "SELECT * FROM grants WHERE scope=? AND id=?",
            (scope, grant_id),
        ).fetchone()
        extension_review = connection.execute(
            "SELECT * FROM cleanup_identity_reviews WHERE scope=? AND grant_id=?",
            (scope, grant_id),
        ).fetchone()
        limit = connection.execute(
            "SELECT used FROM authority_limits WHERE authority=? AND id=?",
            (authority, grant_id),
        ).fetchone()
        attempts = connection.execute("SELECT grant_id,state,session FROM attempts WHERE scope=?", (scope,)).fetchall()
        if (
            connection.execute(
                "SELECT 1 FROM native_identity_recovery_permits WHERE scope=?",
                (scope,),
            ).fetchone()
            or not active
            or active["scope"] != scope
            or active["session"] != session
            or active["account"] != account
            or active["phase"] != "cleanup"
            or active["expires"] is not None
            and active["expires"] <= now
            or not grant
            or grant["kind"] != "identity"
            or grant["quota"] != 1
            or grant["used"] != 0
            or grant["authority"] != authority
            or canonical(json.loads(grant["call"])) != canonical(call)
            or not extension_review
            or extension_review["session"] != session
            or extension_review["account"] != account
            or extension_review["authority"] != authority
            or extension_review["evidence_sha256"] != original_review_sha256
            or not limit
            or limit[0] != 0
            or any(item["state"] != "succeeded" or item["session"] != session for item in attempts)
            or connection.execute(
                "SELECT 1 FROM attempts a LEFT JOIN grants g ON a.scope=g.scope AND a.grant_id=g.id "
                "WHERE a.scope=? AND (g.kind='delete' OR a.grant_id LIKE 'delete-%')",
                (scope,),
            ).fetchone()
        ):
            raise GateDeniedError()
        connection.execute(
            "INSERT INTO native_identity_recovery_permits(scope,slot,session,account,grant_id,authority,call,"
            "proposal_sha256,review_sha256,original_review_sha256,marker_path,expires,state,created,attempt_id) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL)",
            (
                scope,
                slot,
                session,
                account,
                grant_id,
                authority,
                canonical(call),
                proposal_sha256,
                review_sha256,
                original_review_sha256,
                marker_path,
                now + lifetime_seconds,
                "armed",
                now,
            ),
        )


def enabled() -> bool:
    return ENV in os.environ


def filter_tools(tools: list[Any]) -> list[Any]:
    return [tool for tool in tools if tool.name == "aliyun_api"] if enabled() else tools


def input_schema(tool: str, schema: dict[str, Any]) -> dict[str, Any]:
    if not enabled() or tool != "aliyun_api":
        return schema
    controlled_fields = {"product", "action", "version", "region_id", "params"}
    return {
        **schema,
        "properties": {key: value for key, value in schema["properties"].items() if key in controlled_fields},
        "required": ["product", "action", "region_id"],
    }


def deferred_call(tool: str, index: int) -> str | None:
    if not enabled() or tool != "aliyun_api" or index == 0:
        return None
    return (
        "controlled_test_call_deferred: this call was not executed and consumed no quota. "
        "Await the preceding actual tool result. If it succeeded, form this call separately "
        "from its source using only the original approved scope and remaining quota; "
        "if it failed, stop. This is not a cloud or execution-gate failure."
    )


def require_runtime(available: bool) -> str | None:
    return ERROR if enabled() and not available else None


def on_web_startup() -> None:
    attest_web_runtime()


def attest_web_runtime() -> None:
    """Private opt-in startup evidence from the actual Web interpreter; never credentials."""
    if not enabled():
        return
    journal = Path(os.environ[ENV])
    with database(journal):
        pass
    slot = os.environ.get(SLOT_ENV, "")
    if not slot.isdigit():
        raise GateDeniedError()
    modules = []
    for name in ATTEST_MODULES:
        module = importlib.import_module(name)
        if not module.__file__:
            raise GateDeniedError()
        source = Path(module.__file__).resolve()
        modules.append({"module": name, "path": str(source), "sha256": hashlib.sha256(source.read_bytes()).hexdigest()})
    import iac_code

    package = Path(iac_code.__file__).resolve().parent
    assets = [
        {
            "relative_path": name,
            "path": str(package / name),
            "sha256": hashlib.sha256((package / name).read_bytes()).hexdigest(),
        }
        for name in ATTEST_ASSETS
    ]
    folder = journal.parent / "runtime-attestations"
    folder.mkdir(exist_ok=True)
    path = folder / f"slot-{slot}.json"
    if path.is_symlink():
        raise GateDeniedError()
    value = {
        "slot": slot,
        "pid": os.getpid(),
        "parent_pid": os.getppid(),
        "captured_at": time.time(),
        "modules": modules,
        "assets": assets,
    }
    temporary = folder / f"{uuid.uuid4().hex}.tmp"
    with temporary.open("x", encoding="utf-8") as stream:
        os.chmod(temporary, 0o600)
        stream.write(canonical(value))
    os.replace(temporary, path)


def api_call(value: dict[str, Any]) -> dict[str, Any]:
    # No alternate host, arbitrary body, local file template, or signing override.
    if set(value) - {"product", "action", "version", "region_id", "params"}:
        raise GateDeniedError()
    if not all(isinstance(value.get(key), str) and value[key] for key in ("product", "action", "region_id")):
        raise GateDeniedError()
    params = value.get("params", {})
    if not isinstance(params, dict) or "TemplateURL" in params:
        raise GateDeniedError()
    return {**value, "product": value["product"].lower(), "params": params}


def creation_response_path(value: Any) -> Any:
    if isinstance(value, dict) and value.get("kind") in {"resource_group_empty", "nas_native_dependency"}:
        return value.get("path")
    return value


def resource_group_id(value: Any) -> TypeGuard[str]:
    return isinstance(value, str) and re.fullmatch(r"rg-[A-Za-z0-9-]{1,15}", value) is not None


def resource_group_template(call: dict[str, Any], binding: dict[str, Any]) -> dict[str, str]:
    if (
        call.get("product") != "ros"
        or call.get("action") != "CreateStack"
        or call.get("version") != "2019-09-10"
        or call["params"].get("RegionId") != call["region_id"]
        or set(call["params"]) - {"StackName", "RegionId", "TemplateBody", "TimeoutInMinutes", "DisableRollback"}
    ):
        raise GateDeniedError()
    try:
        template = json.loads(call["params"]["TemplateBody"])
    except (KeyError, TypeError, ValueError) as error:
        raise GateDeniedError() from error
    logical, output = binding.get("logical_resource_id"), binding.get("output_key")
    if (
        not isinstance(template, dict)
        or template.get("ROSTemplateFormatVersion") != "2015-09-01"
        or not isinstance(logical, str)
        or re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", logical) is None
        or not isinstance(output, str)
        or re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", output) is None
    ):
        raise GateDeniedError()
    if "Transform" not in template:
        if set(template) != {"ROSTemplateFormatVersion", "Resources", "Outputs"}:
            raise GateDeniedError()
        resources = template["Resources"]
        if not isinstance(resources, dict) or set(resources) != {logical}:
            raise GateDeniedError()
        resource = resources[logical]
        if (
            not isinstance(resource, dict)
            or set(resource) != {"Type", "Properties"}
            or resource["Type"] != "ALIYUN::ResourceManager::ResourceGroup"
            or not isinstance(resource["Properties"], dict)
            or set(resource["Properties"]) != {"Name", "DisplayName"}
            or template["Outputs"] != {output: {"Value": {"Fn::GetAtt": [logical, "Id"]}}}
        ):
            raise GateDeniedError()
        name, display = resource["Properties"]["Name"], resource["Properties"]["DisplayName"]
        mode = "ROS"
    else:
        if (
            set(template) != {"ROSTemplateFormatVersion", "Transform", "Workspace"}
            or template["Transform"] != "Aliyun::Terraform-v1.5"
            or not isinstance(template["Workspace"], dict)
            or set(template["Workspace"]) != {"main.tf"}
        ):
            raise GateDeniedError()
        content = template["Workspace"]["main.tf"]
        pattern = (
            r"terraform\s*\{\s*required_providers\s*\{\s*alicloud\s*=\s*\{\s*"
            r'source\s*=\s*"aliyun/alicloud"\s*version\s*=\s*"= ?1\.140\.0"\s*\}\s*\}\s*\}\s*'
            r'provider\s*"alicloud"\s*\{\s*region\s*=\s*"(?P<region>[^"]+)"\s*\}\s*'
            r'resource\s*"alicloud_resource_manager_resource_group"\s*"'
            + re.escape(logical)
            + r'"\s*\{\s*resource_group_name\s*=\s*"(?P<name>[^"]+)"\s*'
            r'display_name\s*=\s*"(?P<display>[^"]+)"\s*\}\s*output\s*"'
            + re.escape(output)
            + r'"\s*\{\s*value\s*=\s*alicloud_resource_manager_resource_group\.'
            + re.escape(logical)
            + r"\.id\s*\}\s*"
        )
        match = re.fullmatch(pattern, content.strip()) if isinstance(content, str) else None
        if match is None or match["region"] != call["region_id"]:
            raise GateDeniedError()
        name, display, mode = match["name"], match["display"], "Terraform"
    if (
        not isinstance(name, str)
        or re.fullmatch(r"[A-Za-z][A-Za-z0-9-]{2,11}", name) is None
        or not isinstance(display, str)
        or re.fullmatch(r"[A-Za-z0-9\u4e00-\u9fff-]{1,30}", display) is None
    ):
        raise GateDeniedError()
    return {"logical": logical, "output": output, "name": name, "display": display, "mode": mode}


def validate_resource_group_binding(grant: dict[str, Any], by_id: dict[str, Any]) -> None:
    binding = grant.get("response_path")
    if not isinstance(binding, dict):
        return
    parent_id = grant.get("parent")
    parent = by_id.get(parent_id, {}) if isinstance(parent_id, str) else {}
    call, parent_call = api_call(grant["call"]), api_call(parent.get("call", {}))
    if parent.get("kind") != "create" or call["region_id"] != parent_call["region_id"]:
        raise GateDeniedError()
    if binding.get("kind") == "resource_group_child":
        if (
            set(binding) != {"kind", "logical_resource_id", "output_key", "stack_status_grant", "stack_resources_grant"}
            or grant["kind"] != "read"
            or grant.get("target_param") != "ResourceGroupId"
            or call["product"] != "resourcemanager"
            or call.get("version") != "2020-03-31"
            or call["action"] not in {"GetResourceGroup", "ListResources", "ListResourceGroups"}
            or call["params"].get("ResourceGroupId") != "$created_id"
            or read_policy(grant).get("phase") not in {"execute", "cleanup"}
        ):
            raise GateDeniedError()
        params = call["params"]
        if call["action"] == "GetResourceGroup":
            if set(params) != {"ResourceGroupId"}:
                raise GateDeniedError()
        elif (
            set(params) != {"ResourceGroupId", "PageNumber", "PageSize"}
            or type(params["PageNumber"]) is not int
            or params["PageNumber"] != 1
            or type(params["PageSize"]) is not int
            or not 1 <= params["PageSize"] <= 100
        ):
            raise GateDeniedError()
        resource_group_template(parent_call, binding)
        for key, action in (("stack_status_grant", "GetStack"), ("stack_resources_grant", "ListStackResources")):
            source = by_id.get(binding[key], {})
            source_call = source.get("call", {})
            if (
                source.get("kind") != "read"
                or source.get("parent") != parent["id"]
                or source.get("response_path") != ["StackId"]
                or source.get("target_param") != "StackId"
                or read_policy(source).get("phase") != "execute"
                or source_call.get("product") != "ros"
                or source_call.get("action") != action
                or source_call.get("version") != "2019-09-10"
                or source_call.get("region_id") != parent_call["region_id"]
                or source_call.get("params", {}).get("RegionId") != parent_call["region_id"]
                or source_call.get("params", {}).get("StackId") != "$created_id"
                or (action == "GetStack" and read_policy(source).get("min_interval_seconds", 0) < 30)
            ):
                raise GateDeniedError()
        if (
            len(
                [
                    entry
                    for entry in by_id.values()
                    if entry.get("kind") == "read"
                    and entry.get("parent") == parent["id"]
                    and entry.get("response_path") == binding
                    and entry.get("target_param") == "ResourceGroupId"
                    and entry.get("call", {}).get("product") == "resourcemanager"
                    and entry.get("call", {}).get("action") == "GetResourceGroup"
                    and entry.get("call", {}).get("version") == "2020-03-31"
                    and read_policy(entry).get("phase") == "execute"
                ]
            )
            != 1
        ):
            raise GateDeniedError()
        if not any(
            entry.get("kind") == "delete"
            and entry.get("parent") == parent["id"]
            and isinstance(entry.get("response_path"), dict)
            and entry["response_path"].get("kind") == "resource_group_empty"
            for entry in by_id.values()
        ):
            raise GateDeniedError()
    elif binding.get("kind") == "resource_group_empty":
        member = by_id.get(binding.get("read_grant"), {})
        member_call = member.get("call", {})
        stack = parent_call["product"] == "ros" and parent_call["action"] == "CreateStack"
        path = ["StackId"] if stack else ["ResourceGroup", "Id"]
        if (
            set(binding) != {"kind", "path", "read_grant"}
            or grant["kind"] != "delete"
            or binding["path"] != path
            or call["product"] != parent_call["product"]
            or call.get("version") != ("2019-09-10" if stack else "2020-03-31")
            or call["action"] != ("DeleteStack" if stack else "DeleteResourceGroup")
            or grant.get("target_param") != ("StackId" if stack else "ResourceGroupId")
            or call["params"].get(grant["target_param"]) != "$created_id"
            or (
                stack
                and (
                    call["params"].get("RegionId") != call["region_id"]
                    or call["params"].get("RetainAllResources") is not False
                    or set(call["params"]) != {"StackId", "RegionId", "RetainAllResources"}
                )
            )
            or (
                not stack
                and (
                    parent_call["product"] != "resourcemanager"
                    or parent_call["action"] != "CreateResourceGroup"
                    or parent_call.get("version") != "2020-03-31"
                    or set(call["params"]) != {"ResourceGroupId"}
                )
            )
            or member.get("kind") != "read"
            or member.get("parent") != parent["id"]
            or member.get("target_param") != "ResourceGroupId"
            or member_call.get("product") != "resourcemanager"
            or member_call.get("action") != "ListResources"
            or member_call.get("version") != "2020-03-31"
            or member_call.get("region_id") != parent_call["region_id"]
            or read_policy(member).get("phase") != "cleanup"
            or read_policy(member).get("after_delete")
            or set(member_call.get("params", {})) != {"ResourceGroupId", "PageNumber", "PageSize"}
            or member_call["params"]["ResourceGroupId"] != "$created_id"
            or type(member_call["params"]["PageNumber"]) is not int
            or member_call["params"]["PageNumber"] != 1
            or type(member_call["params"]["PageSize"]) is not int
            or not 1 <= member_call["params"]["PageSize"] <= 100
            or (stack and not isinstance(member.get("response_path"), dict))
            or (not stack and member.get("response_path") != path)
        ):
            raise GateDeniedError()
        if stack:
            validate_resource_group_binding(member, by_id)
        elif (
            set(parent_call["params"]) != {"Name", "DisplayName"}
            or not isinstance(parent_call["params"]["Name"], str)
            or not isinstance(parent_call["params"]["DisplayName"], str)
            or len(
                [
                    entry
                    for entry in by_id.values()
                    if entry.get("kind") == "read"
                    and entry.get("parent") == parent["id"]
                    and entry.get("response_path") == path
                    and entry.get("target_param") == "ResourceGroupId"
                    and entry.get("call", {}).get("product") == "resourcemanager"
                    and entry.get("call", {}).get("action") == "GetResourceGroup"
                    and entry.get("call", {}).get("version") == "2020-03-31"
                    and entry.get("call", {}).get("region_id") == parent_call["region_id"]
                    and entry.get("call", {}).get("params") == {"ResourceGroupId": "$created_id"}
                    and read_policy(entry).get("phase") == "execute"
                ]
            )
            != 1
        ):
            raise GateDeniedError()
    else:
        raise GateDeniedError()


def resource_group_scope(connection: sqlite3.Connection, scope: str) -> bool:
    return any(
        isinstance(binding := json.loads(row[0] or "null"), dict)
        and binding.get("kind") in {"resource_group_child", "resource_group_empty"}
        for row in connection.execute("SELECT response_path FROM grants WHERE scope=?", (scope,))
    )


def successful_resource_group_body(attempt: Any, expected_call: dict[str, Any]) -> dict[str, Any] | None:
    if not attempt or attempt["state"] != "succeeded" or not attempt["response"]:
        return None
    response = json.loads(attempt["response"])
    if (
        not isinstance(response, dict)
        or set(response) != {"body", "status", "headers", "call"}
        or type(response["status"]) is not int
        or response["status"] != 200
        or not isinstance(response["body"], dict)
        or not isinstance(response["headers"], dict)
        or response["call"] != expected_call
    ):
        return None
    body = response["body"]
    request_id = body.get("RequestId")
    if (
        not isinstance(request_id, str)
        or not request_id
        or len(request_id) > 256
        or any(char.isspace() for char in request_id)
        or "Code" in body
        or "ErrorCode" in body
    ):
        return None
    header_ids = [value for key, value in response["headers"].items() if key.casefold() == "x-acs-request-id"]
    if header_ids and header_ids != [request_id]:
        return None
    return body


def successful_mse_read_body(attempt: Any, expected_call: dict[str, Any]) -> dict[str, Any] | None:
    """Validate the original MSE read envelope without removing its success code."""
    if (
        expected_call.get("product") != "mse"
        or expected_call.get("version") != "2019-05-31"
        or expected_call.get("action") not in {"QueryClusterInfo", "ListClusters"}
    ):
        return None
    response = json.loads(attempt["response"] or "null")
    if not isinstance(response, dict) or not isinstance(response.get("body"), dict):
        return None
    body = response["body"]
    code = body.get("ErrorCode")
    if body.get("Success") is not True or not (
        code is None or isinstance(code, str) and code in {"", "mse-100-000", "Success"}
    ):
        return None
    validation = {**response, "body": {k: v for k, v in body.items() if k != "ErrorCode"}}
    if not successful_resource_group_body(
        {"state": attempt["state"], "response": canonical(validation)},
        expected_call,
    ):
        return None
    return body


def resource_group_parent(connection: sqlite3.Connection, active: Any, parent_id: str) -> Any:
    return connection.execute(
        "SELECT a.* FROM attempts a JOIN identities i "
        "ON i.scope=a.scope AND i.session=a.session AND i.credential=a.credential "
        "WHERE a.scope=? AND a.grant_id=? AND a.session=? AND i.account=? AND a.state='succeeded'",
        (active["scope"], parent_id, active["session"], active["account"]),
    ).fetchone()


def latest_resource_group_read(connection: sqlite3.Connection, parent: Any, grant_id: str) -> Any:
    return connection.execute(
        "SELECT * FROM attempts WHERE scope=? AND session=? AND credential=? AND grant_id=? AND created>=? "
        "ORDER BY created DESC,rowid DESC LIMIT 1",
        (parent["scope"], parent["session"], parent["credential"], grant_id, parent["created"]),
    ).fetchone()


def resource_group_read_body(
    connection: sqlite3.Connection,
    parent: Any,
    grant_id: str,
    identifier: str,
) -> dict[str, Any] | None:
    grant = connection.execute(
        "SELECT * FROM grants WHERE scope=? AND id=?",
        (parent["scope"], grant_id),
    ).fetchone()
    if not grant or not grant["target_param"]:
        return None
    expected = json.loads(grant["call"])
    expected["params"][grant["target_param"]] = identifier
    return successful_resource_group_body(latest_resource_group_read(connection, parent, grant_id), expected)


def resource_group_child_id(
    connection: sqlite3.Connection,
    active: Any,
    parent: Any,
    binding: dict[str, Any],
) -> str | None:
    row = connection.execute(
        "SELECT call FROM grants WHERE scope=? AND id=?", (active["scope"], parent["grant_id"])
    ).fetchone()
    create_call = json.loads(row[0])
    template = resource_group_template(create_call, binding)
    created = json.loads(parent["response"])
    stack_id = response_identifier(create_call, created, ["StackId"])
    if not stack_id or not created.get("RequestId"):
        return None
    status = resource_group_read_body(connection, parent, binding["stack_status_grant"], stack_id)
    if (
        not status
        or any(
            status.get(key) != value
            for key, value in (
                ("StackId", stack_id),
                ("StackName", create_call["params"]["StackName"]),
                ("RegionId", create_call["region_id"]),
                ("Status", "CREATE_COMPLETE"),
                ("StackType", template["mode"]),
            )
        )
        or status.get("AccountId", active["account"]) != active["account"]
    ):
        return None
    outputs = status.get("Outputs")
    if not isinstance(outputs, list):
        return None
    matches = [
        output for output in outputs if isinstance(output, dict) and output.get("OutputKey") == template["output"]
    ]
    if len(matches) != 1 or not resource_group_id(identifier := matches[0].get("OutputValue")):
        return None
    if any(not isinstance(output, dict) or output.get("OutputKey") != template["output"] for output in outputs):
        return None
    resource_attempt = latest_resource_group_read(connection, parent, binding["stack_resources_grant"])
    resources = resource_group_read_body(connection, parent, binding["stack_resources_grant"], stack_id)
    if template["mode"] == "ROS" or resource_attempt:
        if not resources or not isinstance(resources.get("Resources"), list) or len(resources["Resources"]) != 1:
            return None
        resource = resources["Resources"][0]
        if (
            not isinstance(resource, dict)
            or any(
                resource.get(key) != value
                for key, value in (
                    ("StackId", stack_id),
                    ("LogicalResourceId", template["logical"]),
                    ("PhysicalResourceId", identifier),
                    ("Status", "CREATE_COMPLETE"),
                )
            )
            or resource.get("ResourceType")
            not in (
                ("ALIYUN::ResourceManager::ResourceGroup",)
                if template["mode"] == "ROS"
                else ("alicloud_resource_manager_resource_group", "ALIYUN::ResourceManager::ResourceGroup")
            )
            or resource.get("StackName", create_call["params"]["StackName"]) != create_call["params"]["StackName"]
        ):
            return None
    return identifier


def resource_group_details(
    connection: sqlite3.Connection,
    active: Any,
    parent: Any,
    identifier: str,
    binding: Any,
) -> bool:
    create_call = json.loads(
        connection.execute(
            "SELECT call FROM grants WHERE scope=? AND id=?",
            (active["scope"], parent["grant_id"]),
        ).fetchone()[0]
    )
    if isinstance(binding, dict):
        expected = resource_group_template(create_call, binding)
        name, display = expected["name"], expected["display"]
    else:
        name, display = create_call["params"].get("Name"), create_call["params"].get("DisplayName")
        created = json.loads(parent["response"])
        group = created.get("ResourceGroup")
        if not isinstance(group, dict) or any(
            group.get(key) != value
            for key, value in (
                ("Id", identifier),
                ("Name", name),
                ("DisplayName", display),
                ("AccountId", active["account"]),
            )
        ):
            return False
    for row in connection.execute(
        "SELECT * FROM grants WHERE scope=? AND parent=?",
        (active["scope"], parent["grant_id"]),
    ):
        call = json.loads(row["call"])
        if (
            row["kind"] != "read"
            or call["product"] != "resourcemanager"
            or call["action"] != "GetResourceGroup"
            or call.get("version") != "2020-03-31"
            or json.loads(row["response_path"]) != binding
            or call["params"] != {"ResourceGroupId": "$created_id"}
            or read_policy(row).get("phase") != "execute"
        ):
            continue
        body = resource_group_read_body(connection, parent, row["id"], identifier)
        group = body.get("ResourceGroup") if body else None
        if (
            isinstance(group, dict)
            and all(
                group.get(key) == value
                for key, value in (
                    ("Id", identifier),
                    ("Name", name),
                    ("DisplayName", display),
                    ("AccountId", active["account"]),
                    ("Status", "OK"),
                )
            )
            and group.get("ResourceGroupId", identifier) == identifier
        ):
            return True
    return False


def resource_group_empty_before_delete(
    connection: sqlite3.Connection,
    active: Any,
    parent: Any,
    binding: dict[str, Any],
) -> bool:
    member = connection.execute(
        "SELECT * FROM grants WHERE scope=? AND id=?",
        (active["scope"], binding["read_grant"]),
    ).fetchone()
    if not member:
        return False
    child = json.loads(member["response_path"])
    create_call = json.loads(
        connection.execute(
            "SELECT call FROM grants WHERE scope=? AND id=?",
            (active["scope"], parent["grant_id"]),
        ).fetchone()[0]
    )
    identifier = (
        resource_group_child_id(connection, active, parent, child)
        if isinstance(child, dict)
        else (response_identifier(create_call, json.loads(parent["response"]), child))
    )
    if not resource_group_id(identifier) or not resource_group_details(connection, active, parent, identifier, child):
        return False
    body = resource_group_read_body(connection, parent, member["id"], identifier)
    params = json.loads(member["call"])["params"]
    return bool(
        body
        and type(body.get("TotalCount")) is int
        and body["TotalCount"] == 0
        and type(body.get("PageNumber")) is int
        and body["PageNumber"] == params["PageNumber"]
        and type(body.get("PageSize")) is int
        and body["PageSize"] == params["PageSize"]
        and body.get("Resources") == {"Resource": []}
        and body.get("ResourceGroupId", identifier) == identifier
        and not any(body.get(key) for key in ("NextToken", "HasMore", "IsTruncated"))
    )


def nas_managed_scope(connection: sqlite3.Connection, scope: str) -> bool:
    return any(
        isinstance(binding := json.loads(row[0] or "null"), dict) and binding.get("kind") == "nas_managed_child"
        for row in connection.execute("SELECT response_path FROM grants WHERE scope=?", (scope,))
    )


def nas_managed_template(call: dict[str, Any]) -> dict[str, str]:
    """Closed empty General-purpose grammar; this is capability validation, not fee authorization."""
    if (
        call.get("product") != "ros"
        or call.get("action") != "CreateStack"
        or call.get("version") != "2019-09-10"
        or call.get("region_id") != "cn-hangzhou"
        or set(call["params"]) != {"RegionId", "StackName", "TemplateBody", "TimeoutInMinutes", "DisableRollback"}
        or call["params"]["RegionId"] != call["region_id"]
        or call["params"]["DisableRollback"] is not False
        or type(call["params"]["TimeoutInMinutes"]) is not int
        or call["params"]["TimeoutInMinutes"] != 10
    ):
        raise GateDeniedError()
    try:
        template = json.loads(call["params"]["TemplateBody"])
    except (TypeError, ValueError) as error:
        raise GateDeniedError() from error
    if (
        not isinstance(template, dict)
        or set(template) != {"ROSTemplateFormatVersion", "Transform", "Workspace"}
        or template["ROSTemplateFormatVersion"] != "2015-09-01"
        or template["Transform"] != "Aliyun::Terraform-v1.5"
        or not isinstance(template["Workspace"], dict)
        or set(template["Workspace"]) != {"main.tf"}
    ):
        raise GateDeniedError()
    pattern = (
        r"terraform\s*\{\s*required_providers\s*\{\s*alicloud\s*=\s*\{\s*"
        r'source\s*=\s*"aliyun/alicloud"\s*version\s*=\s*"= ?1\.260\.0"\s*\}\s*\}\s*\}\s*'
        r'provider\s*"alicloud"\s*\{\s*region\s*=\s*"cn-hangzhou"\s*\}\s*'
        r'resource\s*"alicloud_nas_file_system"\s*"filesystem"\s*\{\s*'
        r'file_system_type\s*=\s*"standard"\s*protocol_type\s*=\s*"NFS"\s*storage_type\s*=\s*"Performance"\s*'
        r'zone_id\s*=\s*"(?P<zone>cn-hangzhou-[a-z])"\s*description\s*=\s*"(?P<description>[A-Za-z0-9-]+)"\s*\}\s*'
        r'resource\s*"alicloud_nas_mount_target"\s*"mount_target"\s*\{\s*'
        r"file_system_id\s*=\s*alicloud_nas_file_system\.filesystem\.id\s*"
        r'access_group_name\s*=\s*"(?P<group>[A-Za-z0-9-]+)"\s*network_type\s*=\s*"Vpc"\s*'
        r'vpc_id\s*=\s*"(?P<vpc>vpc-[a-z0-9]+)"\s*vswitch_id\s*=\s*"(?P<vswitch>vsw-[a-z0-9]+)"\s*\}\s*'
        r'output\s*"file_system_id"\s*\{\s*value\s*=\s*alicloud_nas_file_system\.filesystem\.id\s*\}\s*'
        r'output\s*"mount_target_domain"\s*\{\s*'
        r"value\s*=\s*alicloud_nas_mount_target\.mount_target\.mount_target_domain\s*\}\s*"
    )
    content = template["Workspace"]["main.tf"]
    match = re.fullmatch(pattern, content.strip()) if isinstance(content, str) else None
    if match is None:
        raise GateDeniedError()
    return match.groupdict()


def validate_nas_managed_binding(grant: dict[str, Any], by_id: dict[str, Any]) -> None:
    binding = grant["response_path"]
    parent_id = grant.get("parent")
    parent = by_id.get(parent_id, {}) if isinstance(parent_id, str) else {}
    call = api_call(grant["call"])
    parent_call = api_call(parent.get("call", {}))
    nas_managed_template(parent_call)
    child = binding.get("child")
    if (
        set(binding)
        != {
            "kind",
            "child",
            "filesystem_logical_id",
            "mount_target_logical_id",
            "stack_status_grant",
            "stack_resources_grant",
        }
        or parent.get("kind") != "create"
        or grant["kind"] != "read"
        or child not in {"filesystem", "mount_target"}
        or binding["filesystem_logical_id"] != "filesystem"
        or binding["mount_target_logical_id"] != "mount_target"
        or call["product"] != "nas"
        or call.get("version") != "2017-06-26"
        or call["region_id"] != parent_call["region_id"]
        or grant.get("target_param") != "FileSystemId"
        or "target_encoding" in grant
        or grant["quota"] != 1
        or call["action"] != ("DescribeFileSystems" if child == "filesystem" else "DescribeMountTargets")
        or call["params"]
        != {
            "FileSystemId": "$created_id",
            "PageNumber": 1,
            "PageSize": 10,
            **({"MountTargetDomain": "$created_mount_target_domain"} if child == "mount_target" else {}),
        }
        or read_policy(grant).get("phase") not in {"execute", "cleanup"}
    ):
        raise GateDeniedError()
    for key, action in (("stack_status_grant", "GetStack"), ("stack_resources_grant", "ListStackResources")):
        source = by_id.get(binding[key], {})
        expected = {
            "product": "ros",
            "action": action,
            "version": "2019-09-10",
            "region_id": call["region_id"],
            "params": {
                "RegionId": call["region_id"],
                "StackId": "$created_id",
                **({"OutputOption": "Enabled"} if action == "GetStack" else {}),
            },
        }
        if (
            source.get("kind") != "read"
            or source.get("parent") != parent["id"]
            or source.get("response_path") != ["StackId"]
            or source.get("target_param") != "StackId"
            or source.get("call") != expected
            or read_policy(source).get("phase") != "execute"
            or (action == "GetStack" and read_policy(source).get("min_interval_seconds", 0) < 30)
        ):
            raise GateDeniedError()
    siblings = [entry for entry in by_id.values() if entry.get("parent") == parent["id"]]
    deletes = [entry for entry in siblings if entry["kind"] == "delete"]
    if len(deletes) != 1 or any(
        deletes[0].get(key) != value
        for key, value in (
            ("response_path", ["StackId"]),
            ("target_param", "StackId"),
            (
                "call",
                {
                    "product": "ros",
                    "action": "DeleteStack",
                    "version": "2019-09-10",
                    "region_id": call["region_id"],
                    "params": {"RegionId": call["region_id"], "StackId": "$created_id", "RetainAllResources": False},
                },
            ),
        )
    ):
        raise GateDeniedError()
    for wanted_child in ("filesystem", "mount_target"):
        for phase in ("execute", "cleanup"):
            wanted = {**binding, "child": wanted_child}
            matches = [
                entry
                for entry in siblings
                if entry.get("response_path") == wanted and read_policy(entry).get("phase") == phase
            ]
            if len(matches) != 1 or (
                phase == "cleanup"
                and (
                    read_policy(matches[0]).get("required_cleanup") is not True
                    or read_policy(matches[0]).get("after_delete") != parent["id"]
                )
            ):
                raise GateDeniedError()
    # Keep stack disappearance mandatory alongside both child reads.
    if not any(
        entry["kind"] == "read"
        and entry.get("response_path") == ["StackId"]
        and entry.get("target_param") == "StackId"
        and entry["call"] == by_id[binding["stack_status_grant"]]["call"]
        and read_policy(entry).get("required_cleanup") is True
        and read_policy(entry).get("after_delete") == parent["id"]
        for entry in siblings
    ):
        raise GateDeniedError()
    name_call = {
        "product": "ros",
        "action": "ListStacks",
        "version": "2019-09-10",
        "region_id": call["region_id"],
        "params": {
            "RegionId": call["region_id"],
            "StackName": [parent_call["params"]["StackName"]],
            "PageNumber": 1,
            "PageSize": 10,
        },
    }
    for phase in ("execute", "cleanup"):
        matches = [
            entry
            for entry in by_id.values()
            if entry["kind"] == "read"
            and not entry.get("parent")
            and entry["call"] == name_call
            and read_policy(entry).get("phase") == phase
        ]
        if len(matches) != 1 or (
            phase == "cleanup"
            and (
                read_policy(matches[0]).get("required_cleanup") is not True
                or read_policy(matches[0]).get("after_delete") != parent["id"]
            )
        ):
            raise GateDeniedError()


def nas_managed_parent(connection: sqlite3.Connection, active: Any, parent_id: str) -> Any:
    rows = connection.execute(
        "SELECT a.* FROM attempts a WHERE a.scope=? AND a.grant_id=?",
        (active["scope"], parent_id),
    ).fetchall()
    if len(rows) != 1:
        return None
    parent = rows[0]
    if (
        parent["state"] != "succeeded"
        or parent["session"] != active["session"]
        or not connection.execute(
            "SELECT 1 FROM identities WHERE scope=? AND session=? AND credential=? AND account=?",
            (parent["scope"], parent["session"], parent["credential"], active["account"]),
        ).fetchone()
    ):
        return None
    signed = connection.execute(
        "SELECT a.*,g.call FROM attempts a JOIN grants g ON a.scope=g.scope AND a.grant_id=g.id "
        "WHERE a.scope=? AND a.session=? AND a.credential=? AND g.kind='identity' AND a.created<?",
        (parent["scope"], parent["session"], parent["credential"], parent["created"]),
    ).fetchall()
    if not any(
        a["state"] == "succeeded"
        and json.loads(a["call"])
        == {
            "product": "sts",
            "action": "GetCallerIdentity",
            "version": "2015-04-01",
            "region_id": "cn-hangzhou",
            "params": {},
        }
        and isinstance(body := json.loads(a["response"] or "null"), dict)
        and body.get("AccountId") == active["account"]
        and isinstance(body.get("RequestId"), str)
        and body["RequestId"]
        and not any(key in body for key in ("Code", "ErrorCode"))
        for a in signed
    ):
        return None
    return parent


def nas_managed_delete(connection: sqlite3.Connection, parent: Any) -> Any:
    return connection.execute(
        "SELECT a.* FROM attempts a JOIN grants g ON a.scope=g.scope AND a.grant_id=g.id "
        "WHERE a.scope=? AND g.parent=? AND g.kind='delete' ORDER BY a.created,a.rowid LIMIT 1",
        (parent["scope"], parent["grant_id"]),
    ).fetchone()


def nas_managed_members(
    connection: sqlite3.Connection,
    active: Any,
    parent: Any,
    binding: dict[str, Any],
) -> dict[str, Any] | None:
    """Rejoin original wire facts. After delete starts, freeze only pre-delete proofs."""
    call = json.loads(
        connection.execute(
            "SELECT call FROM grants WHERE scope=? AND id=?",
            (parent["scope"], parent["grant_id"]),
        ).fetchone()[0]
    )
    nas_managed_template(call)
    created = json.loads(parent["response"] or "null")
    if not isinstance(created, dict) or not created.get("RequestId") or not isinstance(created.get("StackId"), str):
        return None
    stack_id = created["StackId"]
    if not stack_id:
        return None
    deletion = nas_managed_delete(connection, parent)
    proofs = {}
    for key in ("stack_status_grant", "stack_resources_grant"):
        source = connection.execute(
            "SELECT * FROM grants WHERE scope=? AND id=?",
            (parent["scope"], binding[key]),
        ).fetchone()
        attempts = connection.execute(
            "SELECT * FROM attempts WHERE scope=? AND grant_id=? AND created>=? ORDER BY created DESC,rowid DESC",
            (parent["scope"], binding[key], parent["created"]),
        ).fetchall()
        candidates = [a for a in attempts if deletion is None or a["created"] < deletion["created"]]
        if not source or not candidates:
            return None
        attempt = candidates[0]
        if attempt["session"] != parent["session"] or attempt["credential"] != parent["credential"]:
            return None
        expected = json.loads(source["call"])
        expected["params"]["StackId"] = stack_id
        body = successful_resource_group_body(attempt, expected)
        if (
            not body
            or any(
                body.get(key, value) != value
                for key, value in (
                    ("AccountId", active["account"]),
                    ("RegionId", call["region_id"]),
                    ("StackId", stack_id),
                )
            )
            or any(body.get(key) for key in ("NextToken", "HasMore", "IsTruncated"))
        ):
            return None
        proofs[key] = (attempt, body)
    status = proofs["stack_status_grant"][1]
    if any(
        status.get(key) != value
        for key, value in (
            ("StackId", stack_id),
            ("StackName", call["params"]["StackName"]),
            ("RegionId", call["region_id"]),
            ("Status", "CREATE_COMPLETE"),
            ("StackType", "Terraform"),
        )
    ):
        return None
    member_attempt, body = proofs["stack_resources_grant"]
    if not member_attempt["tool_use_id"] or member_attempt["reference_scope"] != parent["reference_scope"]:
        return None
    rows = body.get("Resources")
    if not isinstance(rows, list) or len(rows) != 2:
        return None
    members = {}
    for child, resource_type, logical in (
        ("filesystem", "alicloud_nas_file_system", binding["filesystem_logical_id"]),
        ("mount_target", "alicloud_nas_mount_target", binding["mount_target_logical_id"]),
    ):
        matches = [
            row
            for row in rows
            if isinstance(row, dict)
            and row.get("ResourceType") == resource_type
            and row.get("LogicalResourceId") == logical
            and any(key in row for key in ("Status", "ResourceStatus"))
            and all(row[key] == "CREATE_COMPLETE" for key in ("Status", "ResourceStatus") if key in row)
        ]
        if len(matches) != 1 or any(
            matches[0].get(key, value) != value
            for key, value in (
                ("StackId", stack_id),
                ("StackName", call["params"]["StackName"]),
                ("RegionId", call["region_id"]),
                ("AccountId", active["account"]),
                ("Status", "CREATE_COMPLETE"),
            )
        ):
            return None
        members[child] = matches[0]
    fs = members["filesystem"].get("PhysicalResourceId")
    composite = members["mount_target"].get("PhysicalResourceId")
    if not isinstance(fs, str) or not re.fullmatch(r"[a-z0-9]{1,64}", fs) or not isinstance(composite, str):
        return None
    parts = composite.split(":")
    if (
        len(parts) != 2
        or parts[0] != fs
        or not re.fullmatch(
            re.escape(fs) + r"-[a-z0-9-]+\.cn-hangzhou\.nas\.aliyuncs\.com",
            parts[1],
        )
    ):
        return None
    return {
        "filesystem_id": fs,
        "mount_target_domain": parts[1],
        "members": members,
        "member_attempt": member_attempt,
        "stack_id": stack_id,
    }


def nas_managed_params(
    connection: sqlite3.Connection,
    active: Any,
    parent: Any,
    grant: Any,
) -> dict[str, Any] | None:
    binding = json.loads(grant["response_path"])
    proof = nas_managed_members(connection, active, parent, binding)
    if not proof:
        return None
    if read_policy(grant).get("phase") == "cleanup":
        deletion = nas_managed_delete(connection, parent)
        if not deletion or deletion["session"] != parent["session"] or deletion["credential"] != parent["credential"]:
            return None
        expected = json.loads(
            connection.execute(
                "SELECT call FROM grants WHERE scope=? AND id=?",
                (parent["scope"], deletion["grant_id"]),
            ).fetchone()[0]
        )
        expected["params"]["StackId"] = proof["stack_id"]
        if deletion["created"] <= proof["member_attempt"]["created"] or not successful_resource_group_body(
            deletion,
            expected,
        ):
            return None
    elif nas_managed_delete(connection, parent) is not None:
        return None
    return {
        "FileSystemId": proof["filesystem_id"],
        **({"MountTargetDomain": proof["mount_target_domain"]} if binding["child"] == "mount_target" else {}),
    }


def require_nas_stack_precheck(connection: sqlite3.Connection, active: Any, grant: Any, fingerprint: str) -> None:
    call = json.loads(grant["call"])
    expected = {
        "product": "ros",
        "action": "ListStacks",
        "version": "2019-09-10",
        "region_id": call["region_id"],
        "params": {
            "RegionId": call["region_id"],
            "StackName": [call["params"]["StackName"]],
            "PageNumber": 1,
            "PageSize": 10,
        },
    }
    sources = [
        source
        for source in connection.execute(
            "SELECT * FROM grants WHERE scope=? AND kind='read' AND call=?",
            (active["scope"], canonical(expected)),
        )
        if read_policy(source).get("phase") == "execute"
    ]
    if len(sources) != 1:
        raise GateDeniedError()
    source = sources[0]
    latest = connection.execute(
        "SELECT * FROM attempts WHERE scope=? AND grant_id=? ORDER BY created DESC,rowid DESC LIMIT 1",
        (active["scope"], source["id"]),
    ).fetchone()
    body = successful_resource_group_body(latest, expected)
    if (
        not latest
        or latest["session"] != active["session"]
        or latest["credential"] != fingerprint
        or not body
        or type(body.get("TotalCount")) is not int
        or body["TotalCount"] != 0
        or body.get("Stacks") != []
        or any(body.get(key, value) != value for key, value in (("PageNumber", 1), ("PageSize", 10)))
        or any(body.get(key) for key in ("NextToken", "HasMore", "IsTruncated"))
    ):
        raise GateDeniedError()


def nas_managed_owners(connection: sqlite3.Connection, row: Any, root: dict[str, Any]) -> list[dict[str, Any]]:
    if row["state"] != "succeeded" or not root["resource"]["account"]:
        return []
    bindings = [
        json.loads(g[0])
        for g in connection.execute(
            "SELECT response_path FROM grants WHERE scope=? AND parent=?",
            (row["scope"], row["grant_id"]),
        )
        if g[0]
    ]
    binding = next((b for b in bindings if isinstance(b, dict) and b.get("kind") == "nas_managed_child"), None)
    if not binding:
        return []
    active = {"scope": row["scope"], "session": row["session"], "account": root["resource"]["account"]}
    parent = nas_managed_parent(connection, active, row["grant_id"])
    proof = nas_managed_members(connection, active, parent, binding) if parent else None
    if not proof:
        # A successful stack without proven children is still unresolved; never close by omitting them.
        return [
            {
                **root,
                "attempt_id": row["id"] + "-nas-unresolved",
                "managed_child": True,
                "parent_attempt_id": row["id"],
                "state": "unknown",
                "resource": {
                    **root["resource"],
                    "id": None,
                    "product": "nas",
                    "resource_type": "unresolved_managed_children",
                },
            }
        ]
    source = proof["member_attempt"]
    return [
        {
            **root,
            "attempt_id": row["id"] + "-nas-" + child,
            "managed_child": True,
            "parent_attempt_id": row["id"],
            "member_attempt_id": source["id"],
            "tool_use_id": source["tool_use_id"],
            "reference_scope": source["reference_scope"],
            "response": json.loads(source["response"]),
            "parent_stack_id": proof["stack_id"],
            "logical_resource_id": proof["members"][child]["LogicalResourceId"],
            "resource": {
                "id": proof["members"][child]["PhysicalResourceId"],
                "region": root["resource"]["region"],
                "account": root["resource"]["account"],
                "product": "nas",
                "resource_type": proof["members"][child]["ResourceType"],
                **(
                    {"filesystem_id": proof["filesystem_id"], "mount_target_domain": proof["mount_target_domain"]}
                    if child == "mount_target"
                    else {}
                ),
            },
        }
        for child in ("filesystem", "mount_target")
    ]


# Closed templates for the private hosted MSE developer1 and professional3 samples.
# This validates capability shape only; hourly cost/admission is reviewed separately.
MSE_MANAGED_HCL = """terraform {
  required_providers {
    alicloud = {
      source = "aliyun/alicloud"
      version = "= 1.260.0"
    }
  }
}
provider "alicloud" {
  region = "cn-hangzhou"
}
resource "alicloud_vpc" "sample_vpc" {
  vpc_name = "{name}"
  cidr_block = "192.168.244.0/24"
  enable_ipv6 = false
  force_delete = false
}
resource "alicloud_vswitch" "sample_vswitch" {
  vswitch_name = "{name}"
  vpc_id = alicloud_vpc.sample_vpc.id
  zone_id = "cn-hangzhou-b"
  cidr_block = "192.168.244.0/26"
  enable_ipv6 = false
}
resource "alicloud_mse_cluster" "sample" {
  cluster_alias_name = "{name}"
  cluster_specification = "MSE_SC_1_2_60_c"
  cluster_type = "Nacos-Ans"
  cluster_version = "NACOS_2_0_0"
  instance_count = 1
  mse_version = "mse_dev"
  payment_type = "PayAsYouGo"
  net_type = "privatenet"
  connection_type = "slb"
  pub_network_flow = "0"
  vpc_id = alicloud_vpc.sample_vpc.id
  vswitch_id = alicloud_vswitch.sample_vswitch.id
}
output "sample_instance_id" { value = alicloud_mse_cluster.sample.id }
output "sample_cluster_id" { value = alicloud_mse_cluster.sample.cluster_id }
output "sample_vpc_id" { value = alicloud_vpc.sample_vpc.id }
output "sample_vswitch_id" { value = alicloud_vswitch.sample_vswitch.id }
"""
# Keep the original literal for already-bound generations and rollback cleanup.
MSE_MANAGED_PRO_HCL = MSE_MANAGED_HCL.replace(
    '  instance_count = 1\n  mse_version = "mse_dev"\n',
    '  instance_count = 3\n  mse_version = "mse_pro"\n',
)
MSE_MANAGED_CHILDREN = {
    "cluster": ("alicloud_mse_cluster", "sample", "sample_instance_id", "mse", "QueryClusterInfo", "InstanceId"),
    "vpc": ("alicloud_vpc", "sample_vpc", "sample_vpc_id", "vpc", "DescribeVpcAttribute", "VpcId"),
    "vswitch": (
        "alicloud_vswitch",
        "sample_vswitch",
        "sample_vswitch_id",
        "vpc",
        "DescribeVSwitchAttributes",
        "VSwitchId",
    ),
}


def mse_managed_template(call: dict[str, Any]) -> str:
    params = call["params"]
    name = params.get("StackName")
    if (
        call.get("product") != "ros"
        or call.get("action") != "CreateStack"
        or call.get("version") != "2019-09-10"
        or call.get("region_id") != "cn-hangzhou"
        or set(params) != {"RegionId", "StackName", "TemplateBody", "TimeoutInMinutes", "DisableRollback"}
        or params["RegionId"] != call["region_id"]
        or params["DisableRollback"] is not False
        or type(params["TimeoutInMinutes"]) is not int
        or params["TimeoutInMinutes"] != 30
        or not isinstance(name, str)
        or re.fullmatch(r"[A-Za-z][A-Za-z0-9-]{0,63}", name) is None
    ):
        raise GateDeniedError()
    try:
        template = json.loads(params["TemplateBody"])
    except (TypeError, ValueError) as error:
        raise GateDeniedError() from error
    if template not in [
        {
            "ROSTemplateFormatVersion": "2015-09-01",
            "Transform": "Aliyun::Terraform-v1.5",
            "Workspace": {"main.tf": hcl.replace("{name}", name)},
        }
        for hcl in (MSE_MANAGED_HCL, MSE_MANAGED_PRO_HCL)
    ]:
        raise GateDeniedError()
    return name


def mse_managed_scope(connection: sqlite3.Connection, scope: str) -> bool:
    return any(
        isinstance(b := json.loads(row[0] or "null"), dict) and b.get("kind") == "mse_managed_child"
        for row in connection.execute("SELECT response_path FROM grants WHERE scope=?", (scope,))
    )


def validate_mse_read(grant: dict[str, Any], call: dict[str, Any]) -> None:
    """An exact official read exception, never a general Query-prefix allowance."""
    identifier = call["params"].get("InstanceId")
    if (
        grant.get("kind") != "read"
        or call.get("version") != "2019-05-31"
        or call.get("region_id") != "cn-hangzhou"
        or set(call["params"]) != {"InstanceId", "RegionId"}
        or call["params"]["RegionId"] != call["region_id"]
        or not isinstance(identifier, str)
        or (identifier != "$created_id" and re.fullmatch(r"mse-[A-Za-z0-9-]+", identifier) is None)
        or read_policy(grant).get("phase") not in {"execute", "cleanup"}
    ):
        raise GateDeniedError()


def validate_mse_managed_binding(grant: dict[str, Any], by_id: dict[str, Any]) -> None:
    binding = grant["response_path"]
    parent_id = grant.get("parent")
    parent = by_id.get(parent_id, {}) if isinstance(parent_id, str) else {}
    parent_call = api_call(parent.get("call", {}))
    name = mse_managed_template(parent_call)
    call = api_call(grant["call"])
    child = binding.get("child")
    if (
        set(binding) != {"kind", "child", "logical_ids", "stack_status_grant", "stack_resources_grant"}
        or parent.get("kind") != "create"
        or grant["kind"] != "read"
        or not isinstance(child, str)
        or child not in MSE_MANAGED_CHILDREN
        or "target_encoding" in grant
        or call["region_id"] != parent_call["region_id"]
        or not isinstance(binding["logical_ids"], dict)
        or set(binding["logical_ids"]) != set(MSE_MANAGED_CHILDREN)
        or sum(g.get("kind") == "create" for g in by_id.values()) != 1
    ):
        raise GateDeniedError()
    for key, (resource_type, label, *_rest) in MSE_MANAGED_CHILDREN.items():
        if not isinstance(binding["logical_ids"][key], str) or binding["logical_ids"][key] not in {
            label,
            resource_type + "." + label,
        }:
            raise GateDeniedError()
    _resource_type, _label, _output, product, action, parameter = MSE_MANAGED_CHILDREN[child]
    resource_read = call["product"] == "ros" and call["action"] == "GetStackResource"
    expected = {
        "product": "ros" if resource_read else product,
        "action": "GetStackResource" if resource_read else action,
        "version": "2019-09-10" if resource_read else ("2019-05-31" if child == "cluster" else "2016-04-28"),
        "region_id": parent_call["region_id"],
        "params": {
            "RegionId": parent_call["region_id"],
            **(
                {"StackId": "$created_stack_id", "LogicalResourceId": "$created_id", "ShowResourceAttributes": True}
                if resource_read
                else {parameter: "$created_id"}
            ),
        },
    }
    phase = read_policy(grant).get("phase")
    if (
        call != expected
        or grant.get("target_param") != ("LogicalResourceId" if resource_read else parameter)
        or phase not in {"execute", "cleanup"}
        or (phase == "execute" and grant["quota"] != 1)
        or (resource_read and phase != "execute")
    ):
        raise GateDeniedError()
    for key, source_action in (("stack_status_grant", "GetStack"), ("stack_resources_grant", "ListStackResources")):
        source = by_id.get(binding[key], {})
        source_call = {
            "product": "ros",
            "action": source_action,
            "version": "2019-09-10",
            "region_id": parent_call["region_id"],
            "params": {
                "RegionId": parent_call["region_id"],
                "StackId": "$created_id",
                **({"OutputOption": "Enabled"} if source_action == "GetStack" else {}),
            },
        }
        if (
            source.get("kind") != "read"
            or source.get("parent") != parent["id"]
            or source.get("response_path") != ["StackId"]
            or source.get("target_param") != "StackId"
            or source.get("call") != source_call
            or read_policy(source).get("phase") != "execute"
            or (source_action == "GetStack" and read_policy(source).get("min_interval_seconds", 0) < 30)
        ):
            raise GateDeniedError()
    siblings = [g for g in by_id.values() if g.get("parent") == parent["id"]]
    deletes = [g for g in siblings if g["kind"] == "delete"]
    if len(deletes) != 1 or any(
        deletes[0].get(k) != v
        for k, v in (
            ("response_path", ["StackId"]),
            ("target_param", "StackId"),
            (
                "call",
                {
                    "product": "ros",
                    "action": "DeleteStack",
                    "version": "2019-09-10",
                    "region_id": parent_call["region_id"],
                    "params": {
                        "RegionId": parent_call["region_id"],
                        "StackId": "$created_id",
                        "RetainAllResources": False,
                    },
                },
            ),
        )
    ):
        raise GateDeniedError()
    for wanted_child in MSE_MANAGED_CHILDREN:
        for wanted_phase in ("execute", "cleanup"):
            wanted = {**binding, "child": wanted_child}
            matches = [
                g
                for g in siblings
                if g.get("response_path") == wanted
                and g["call"]["product"] != "ros"
                and read_policy(g).get("phase") == wanted_phase
            ]
            if len(matches) != 1 or (
                wanted_phase == "cleanup"
                and (
                    read_policy(matches[0]).get("required_cleanup") is not True
                    or read_policy(matches[0]).get("after_delete") != parent["id"]
                )
            ):
                raise GateDeniedError()
        matches = [
            g
            for g in siblings
            if g.get("response_path") == {**binding, "child": wanted_child} and g["call"]["product"] == "ros"
        ]
        if len(matches) != 1:
            raise GateDeniedError()
    if not any(
        g["kind"] == "read"
        and g.get("response_path") == ["StackId"]
        and g.get("target_param") == "StackId"
        and g["call"] == by_id[binding["stack_status_grant"]]["call"]
        and read_policy(g).get("required_cleanup") is True
        and read_policy(g).get("after_delete") == parent["id"]
        for g in siblings
    ):
        raise GateDeniedError()
    cleanup_status = [
        g
        for g in siblings
        if g["kind"] == "read"
        and g.get("response_path") == ["StackId"]
        and g["call"] == by_id[binding["stack_status_grant"]]["call"]
        and read_policy(g).get("phase") == "cleanup"
    ]
    identities = [g for g in by_id.values() if g["kind"] == "identity"]
    if (
        len(cleanup_status) != 1
        or len(identities) != 1
        or identities[0]["quota"] != 1
        or identities[0]["call"]
        != {
            "product": "sts",
            "action": "GetCallerIdentity",
            "version": "2015-04-01",
            "region_id": parent_call["region_id"],
            "params": {},
        }
    ):
        raise GateDeniedError()
    allowed_ids = {
        parent["id"],
        deletes[0]["id"],
        identities[0]["id"],
        cleanup_status[0]["id"],
        binding["stack_status_grant"],
        binding["stack_resources_grant"],
    }
    for entry in by_id.values():
        typed = entry.get("response_path")
        if isinstance(typed, dict) and typed.get("kind") == "mse_managed_child" and entry.get("parent") == parent["id"]:
            allowed_ids.add(entry["id"])
        elif (
            entry["call"]
            == {
                "product": "mse",
                "action": "ListClusters",
                "version": "2019-05-31",
                "region_id": parent_call["region_id"],
                "params": {
                    "RegionId": parent_call["region_id"],
                    "PageNum": 1,
                    "PageSize": 10,
                    "ClusterAliasName": name,
                },
            }
            and entry["quota"] == 1
        ):
            allowed_ids.add(entry["id"])
    if allowed_ids != set(by_id):
        raise GateDeniedError()
    # Exact-name corroboration cannot replace the response-bound InstanceId read.
    if (
        len(
            [
                g
                for g in by_id.values()
                if g["kind"] == "read"
                and not g.get("parent")
                and g["call"]
                == {
                    "product": "mse",
                    "action": "ListClusters",
                    "version": "2019-05-31",
                    "region_id": parent_call["region_id"],
                    "params": {
                        "RegionId": parent_call["region_id"],
                        "PageNum": 1,
                        "PageSize": 10,
                        "ClusterAliasName": name,
                    },
                }
                and read_policy(g).get("required_cleanup") is True
                and read_policy(g).get("after_delete") == parent["id"]
            ]
        )
        != 1
    ):
        raise GateDeniedError()


def mse_managed_parent(
    connection: sqlite3.Connection,
    active: Any,
    parent_id: str,
    *,
    allow_unknown: bool = False,
) -> Any:
    rows = connection.execute(
        "SELECT * FROM attempts WHERE scope=? AND grant_id=?",
        (active["scope"], parent_id),
    ).fetchall()
    if len(rows) != 1:
        return None
    parent = rows[0]
    if (
        parent["state"] not in ({"succeeded", "unknown"} if allow_unknown else {"succeeded"})
        or parent["session"] != active["session"]
        or not parent["tool_use_id"]
        or not parent["reference_scope"]
        or not connection.execute(
            "SELECT 1 FROM identities WHERE scope=? AND session=? AND credential=? AND account=?",
            (parent["scope"], parent["session"], parent["credential"], active["account"]),
        ).fetchone()
    ):
        return None
    # STS must be an actual same-signer wire observation, not an identities-table assertion.
    signed = connection.execute(
        "SELECT a.*,g.call FROM attempts a JOIN grants g ON a.scope=g.scope AND a.grant_id=g.id "
        "WHERE a.scope=? AND a.session=? AND a.credential=? AND g.kind='identity' AND a.created<?",
        (parent["scope"], parent["session"], parent["credential"], parent["created"]),
    ).fetchall()
    if not any(
        a["state"] == "succeeded"
        and a["tool_use_id"]
        and a["reference_scope"] == parent["reference_scope"]
        and json.loads(a["call"])
        == {
            "product": "sts",
            "action": "GetCallerIdentity",
            "version": "2015-04-01",
            "region_id": "cn-hangzhou",
            "params": {},
        }
        and isinstance(body := json.loads(a["response"] or "null"), dict)
        and body.get("AccountId") == active["account"]
        and isinstance(body.get("RequestId"), str)
        and body["RequestId"]
        and not any(k in body for k in ("Code", "ErrorCode"))
        for a in signed
    ):
        return None
    return parent


def mse_managed_rollback_complete(
    connection: sqlite3.Connection,
    active: Any,
    parent: Any,
    binding: dict[str, Any],
) -> bool:
    """Permit only the owned root's rollback cleanup, without claiming child identity or absence."""
    call = json.loads(
        connection.execute(
            "SELECT call FROM grants WHERE scope=? AND id=?",
            (parent["scope"], parent["grant_id"]),
        ).fetchone()[0]
    )
    name = mse_managed_template(call)
    created = json.loads(parent["response"] or "null")
    stack_id = response_identifier(call, created, ["StackId"])
    if not stack_id or not isinstance(created, dict) or not created.get("RequestId"):
        return False
    source = connection.execute(
        "SELECT * FROM grants WHERE scope=? AND id=?",
        (parent["scope"], binding["stack_status_grant"]),
    ).fetchone()
    if (
        not source
        or source["kind"] != "read"
        or source["parent"] != parent["grant_id"]
        or json.loads(source["response_path"] or "null") != ["StackId"]
        or source["target_param"] != "StackId"
        or read_policy(source).get("phase") != "execute"
    ):
        return False
    expected = {
        "product": "ros",
        "action": "GetStack",
        "version": "2019-09-10",
        "region_id": call["region_id"],
        "params": {"RegionId": call["region_id"], "StackId": "$created_id", "OutputOption": "Enabled"},
    }
    if json.loads(source["call"]) != expected:
        return False
    expected["params"]["StackId"] = stack_id
    deletion = nas_managed_delete(connection, parent)
    attempts = connection.execute(
        "SELECT * FROM attempts WHERE scope=? AND grant_id=? AND created>? ORDER BY created DESC,rowid DESC",
        (parent["scope"], source["id"], parent["created"]),
    ).fetchall()
    candidates = [a for a in attempts if deletion is None or a["created"] < deletion["created"]]
    if not candidates:
        return False
    attempt = candidates[0]
    if (
        attempt["session"] != parent["session"]
        or attempt["credential"] != parent["credential"]
        or not attempt["tool_use_id"]
        or attempt["reference_scope"] != parent["reference_scope"]
    ):
        return False
    body = successful_resource_group_body(attempt, expected)
    if (
        not body
        or any(body.get(k) for k in ("NextToken", "HasMore", "IsTruncated"))
        or any(
            body.get(k, v) != v
            for k, v in (
                ("StackId", stack_id),
                ("StackName", name),
                ("RegionId", call["region_id"]),
                ("AccountId", active["account"]),
                ("StackType", "Terraform"),
            )
        )
    ):
        return False
    status = body.get("Stack", body)
    if (
        not isinstance(status, dict)
        or any(
            status.get(k) != v
            for k, v in (
                ("StackId", stack_id),
                ("StackName", name),
                ("RegionId", call["region_id"]),
                ("StackType", "Terraform"),
                ("Status", "CREATE_ROLLBACK_COMPLETE"),
            )
        )
        or status.get("AccountId", active["account"]) != active["account"]
    ):
        return False
    if status.get("StackStatus", status["Status"]) != status["Status"]:
        return False
    return not any(
        k in body and k in status and body[k] != status[k]
        for k in (
            "Status",
            "StackStatus",
            "StackId",
            "StackName",
            "RegionId",
            "AccountId",
            "StackType",
        )
    )


def mse_managed_members(
    connection: sqlite3.Connection,
    active: Any,
    parent: Any,
    binding: dict[str, Any],
    *,
    complete: bool = True,
) -> dict[str, Any] | None:
    """Rejoin immutable wire facts; later cleanup reads cannot replace pre-delete identities."""
    call = json.loads(
        connection.execute(
            "SELECT call FROM grants WHERE scope=? AND id=?",
            (parent["scope"], parent["grant_id"]),
        ).fetchone()[0]
    )
    name = mse_managed_template(call)
    created = json.loads(parent["response"] or "null")
    stack_id = response_identifier(call, created, ["StackId"])
    if not stack_id or not isinstance(created, dict) or not created.get("RequestId"):
        return None
    deletion = nas_managed_delete(connection, parent)
    proofs = {}
    for key in ("stack_status_grant", "stack_resources_grant"):
        source = connection.execute(
            "SELECT * FROM grants WHERE scope=? AND id=?",
            (parent["scope"], binding[key]),
        ).fetchone()
        attempts = connection.execute(
            "SELECT * FROM attempts WHERE scope=? AND grant_id=? AND created>? ORDER BY created DESC,rowid DESC",
            (parent["scope"], binding[key], parent["created"]),
        ).fetchall()
        candidates = [a for a in attempts if deletion is None or a["created"] < deletion["created"]]
        if not source or not candidates:
            return None
        attempt = candidates[0]
        same_signer = (
            attempt["credential"] == parent["credential"] and attempt["reference_scope"] == parent["reference_scope"]
        )
        if (
            attempt["session"] != parent["session"]
            or not attempt["tool_use_id"]
            or (
                not same_signer
                and not mse_rotated_cleanup_identity(
                    connection,
                    active,
                    source,
                    parent,
                    attempt["credential"],
                    attempt["reference_scope"],
                    before=attempt["created"],
                )
            )
        ):
            return None
        expected = json.loads(source["call"])
        expected["params"]["StackId"] = stack_id
        body = successful_resource_group_body(attempt, expected)
        if (
            not body
            or any(
                body.get(k, v) != v
                for k, v in (
                    ("AccountId", active["account"]),
                    ("RegionId", call["region_id"]),
                    ("StackId", stack_id),
                )
            )
            or any(body.get(k) for k in ("NextToken", "HasMore", "IsTruncated"))
        ):
            return None
        proofs[key] = (attempt, body)
    status_body = proofs["stack_status_grant"][1]
    status = status_body.get("Stack", status_body)
    if (
        not isinstance(status, dict)
        or any(
            status.get(k) != v
            for k, v in (
                ("StackId", stack_id),
                ("StackName", name),
                ("RegionId", call["region_id"]),
                ("StackType", "Terraform"),
            )
        )
        or status.get("AccountId", active["account"]) != active["account"]
    ):
        return None
    if complete and (
        status.get("Status") != "CREATE_COMPLETE" or status.get("StackStatus", "CREATE_COMPLETE") != "CREATE_COMPLETE"
    ):
        return None
    if any(
        k in status_body and k in status and status_body[k] != status[k]
        for k in ("Status", "StackStatus", "StackId", "StackName", "RegionId", "AccountId", "StackType")
    ):
        return None
    member_attempt, body = proofs["stack_resources_grant"]
    rows = body.get("Resources")
    if (
        not isinstance(rows, list)
        or len(rows) > 3
        or (complete and len(rows) != 3)
        or (type(body.get("TotalCount", len(rows))) is not int or body.get("TotalCount", len(rows)) != len(rows))
    ):
        return None
    members = {}
    for row in rows:
        if not isinstance(row, dict):
            return None
        matches = [
            child
            for child, spec in MSE_MANAGED_CHILDREN.items()
            if row.get("ResourceType") == spec[0] and row.get("LogicalResourceId") == binding["logical_ids"][child]
        ]
        if (
            len(matches) != 1
            or matches[0] in members
            or any(
                row.get(k, v) != v
                for k, v in (
                    ("StackId", stack_id),
                    ("StackName", name),
                    ("RegionId", call["region_id"]),
                    ("AccountId", active["account"]),
                )
            )
        ):
            return None
        child = matches[0]
        if complete and (
            not any(k in row for k in ("Status", "ResourceStatus"))
            or any(row[k] != "CREATE_COMPLETE" for k in ("Status", "ResourceStatus") if k in row)
        ):
            return None
        prefix = {"cluster": "mse-", "vpc": "vpc-", "vswitch": "vsw-"}[child]
        identifier = row.get("PhysicalResourceId")
        if not isinstance(identifier, str) or re.fullmatch(re.escape(prefix) + r"[A-Za-z0-9-]+", identifier) is None:
            return None
        members[child] = row
    if len({r["PhysicalResourceId"] for r in members.values()}) != len(members):
        return None
    if complete:
        outputs = status.get("Outputs")
        wanted = {spec[2]: members[child]["PhysicalResourceId"] for child, spec in MSE_MANAGED_CHILDREN.items()}
        if (
            not isinstance(outputs, list)
            or len(outputs) != 4
            or any(
                not isinstance(o, dict)
                or not isinstance(o.get("OutputKey"), str)
                or not isinstance(o.get("OutputValue"), str)
                for o in outputs
            )
        ):
            return None
        values = {o.get("OutputKey"): o.get("OutputValue") for o in outputs}
        if (
            len(values) != 4
            or set(values) != {*wanted, "sample_cluster_id"}
            or any(values.get(k) != v for k, v in wanted.items())
            or not isinstance(values["sample_cluster_id"], str)
            or not values["sample_cluster_id"].startswith("mse-")
        ):
            return None
    return {
        "stack_id": stack_id,
        "members": members,
        "member_attempt": member_attempt,
        "status_attempt": proofs["stack_status_grant"][0],
    }


def mse_managed_params(
    connection: sqlite3.Connection,
    active: Any,
    parent: Any,
    grant: Any,
) -> dict[str, Any] | None:
    binding = json.loads(grant["response_path"])
    proof = mse_managed_members(connection, active, parent, binding)
    if not proof:
        return None
    deletion = nas_managed_delete(connection, parent)
    if read_policy(grant).get("phase") == "cleanup":
        deletion_grant = (
            connection.execute(
                "SELECT * FROM grants WHERE scope=? AND id=?", (parent["scope"], deletion["grant_id"])
            ).fetchone()
            if deletion
            else None
        )
        same_signer = bool(
            deletion
            and deletion["credential"] == parent["credential"]
            and deletion["reference_scope"] == parent["reference_scope"]
        )
        if (
            not deletion
            or deletion["session"] != parent["session"]
            or not deletion["tool_use_id"]
            or (
                not same_signer
                and not mse_rotated_cleanup_identity(
                    connection,
                    active,
                    deletion_grant,
                    parent,
                    deletion["credential"],
                    deletion["reference_scope"],
                    before=deletion["created"],
                )
            )
        ):
            return None
        expected = json.loads(
            connection.execute(
                "SELECT call FROM grants WHERE scope=? AND id=?",
                (parent["scope"], deletion["grant_id"]),
            ).fetchone()[0]
        )
        expected["params"]["StackId"] = proof["stack_id"]
        if not successful_resource_group_body(deletion, expected):
            return None
    elif deletion is not None:
        return None
    call = json.loads(grant["call"])
    if call["product"] == "ros":
        return {"StackId": proof["stack_id"], "LogicalResourceId": binding["logical_ids"][binding["child"]]}
    return {grant["target_param"]: proof["members"][binding["child"]]["PhysicalResourceId"]}


def mse_managed_owners(connection: sqlite3.Connection, row: Any, root: dict[str, Any]) -> list[dict[str, Any]]:
    bindings = [
        json.loads(g[0])
        for g in connection.execute(
            "SELECT response_path FROM grants WHERE scope=? AND parent=?",
            (row["scope"], row["grant_id"]),
        )
        if g[0]
    ]
    binding = next((b for b in bindings if isinstance(b, dict) and b.get("kind") == "mse_managed_child"), None)
    if not binding:
        return []
    active = {"scope": row["scope"], "session": row["session"], "account": root["resource"]["account"]}
    parent = mse_managed_parent(connection, active, row["grant_id"])
    proof = mse_managed_members(connection, active, parent, binding) if parent else None
    original = parent or mse_managed_parent(connection, active, row["grant_id"], allow_unknown=True)
    partial = proof or (
        mse_managed_members(connection, active, original, binding, complete=False) if original else None
    )
    records = []
    if partial:
        source = partial["member_attempt"]
        for child, member in partial["members"].items():
            records.append(
                {
                    **root,
                    "attempt_id": row["id"] + "-mse-" + child,
                    "managed_child": True,
                    "parent_attempt_id": row["id"],
                    "member_attempt_id": source["id"],
                    "tool_use_id": source["tool_use_id"],
                    "reference_scope": source["reference_scope"],
                    "response": json.loads(source["response"]),
                    "parent_stack_id": partial["stack_id"],
                    "logical_resource_id": member["LogicalResourceId"],
                    "state": "succeeded" if proof else "unknown",
                    "resource": {
                        "id": member["PhysicalResourceId"],
                        "region": root["resource"]["region"],
                        "account": root["resource"]["account"],
                        "product": MSE_MANAGED_CHILDREN[child][3],
                        "resource_type": member["ResourceType"],
                    },
                }
            )
    if not proof:
        records.append(
            {
                **root,
                "attempt_id": row["id"] + "-mse-unresolved",
                "managed_child": True,
                "parent_attempt_id": row["id"],
                "state": "unknown",
                "resource": {
                    **root["resource"],
                    "id": None,
                    "product": "mse",
                    "resource_type": "unresolved_managed_children",
                },
            }
        )
    return records


# Closed, reviewed-shape native NFS graph. This describes capabilities, never
# grants approval or performs cloud calls. No arbitrary interpolation/path API.
NAS_NATIVE_PLAN_VARIANTS = ("legacy", "bandwidth_omission_bwo1")


def nas_native_plan(name: str, *, variant: str = "legacy") -> list[dict[str, Any]]:
    if variant not in NAS_NATIVE_PLAN_VARIANTS:
        raise GateDeniedError()
    if not isinstance(name, str) or re.fullmatch(r"iac-link-r0036-[a-f0-9]{8}", name) is None:
        raise GateDeniedError()
    result: list[dict[str, Any]] = []

    def add(
        ident: str,
        kind: str,
        quota: int,
        product: str,
        action: str,
        params: dict[str, Any],
        bindings: dict[str, str],
        *,
        policy: dict[str, Any] | None = None,
        parent: str | None = None,
        target: str | None = None,
        path: list[str] | None = None,
        requires: list[str] | None = None,
    ) -> None:
        binding: dict[str, Any] = {
            "kind": "nas_native_dependency",
            "role": ident,
            "bindings": bindings,
            "requires": requires or [],
        }
        if path is not None:
            binding["path"] = path
        versions = {
            "sts": "2015-04-01",
            "nas": "2017-06-26",
            "vpc": "2016-04-28",
            "ecs": "2014-05-26",
            "oss": "2019-05-17",
        }
        grant: dict[str, Any] = {
            "id": ident,
            "kind": kind,
            "quota": quota,
            "response_path": binding,
            "call": {
                "product": product,
                "action": action,
                "version": versions[product],
                "region_id": "cn-hangzhou",
                "params": params,
            },
        }
        if policy is not None:
            grant["read_policy"] = policy
        if parent is not None:
            grant["parent"] = parent
        if target is not None:
            grant["target_param"] = target
        # Only the stable test name is substituted, before exact plan comparison.
        result.append(json.loads(canonical(grant).replace("{name}", name)))

    add("identity", "identity", 1, "sts", "GetCallerIdentity", {}, {})
    add(
        "create-filesystem-vpc",
        "create",
        1,
        "vpc",
        "CreateVpc",
        {
            "RegionId": "cn-hangzhou",
            "VpcName": "{name}-filesystem",
            "CidrBlock": "172.28.0.0/16",
            "ClientToken": "{name}-filesystem-vpc",
        },
        {},
    )
    add(
        "create-filesystem-vswitch",
        "create",
        1,
        "vpc",
        "CreateVSwitch",
        {
            "RegionId": "cn-hangzhou",
            "VpcId": "$created_id",
            "ZoneId": "cn-hangzhou-k",
            "CidrBlock": "172.28.0.0/24",
            "VSwitchName": "{name}-filesystem",
            "ClientToken": "{name}-filesystem-vsw",
        },
        {"VpcId": "create-filesystem-vpc"},
        requires=["read-filesystem-vpc"],
    )
    add(
        "read-filesystem-vpc",
        "read",
        1,
        "vpc",
        "DescribeVpcs",
        {"RegionId": "cn-hangzhou", "VpcId": "$created_id", "PageNumber": 1, "PageSize": 10},
        {"VpcId": "create-filesystem-vpc"},
        policy={"phase": "execute"},
    )
    add(
        "read-filesystem-vswitch",
        "read",
        1,
        "vpc",
        "DescribeVSwitches",
        {"RegionId": "cn-hangzhou", "VSwitchId": "$created_id", "PageNumber": 1, "PageSize": 10},
        {"VSwitchId": "create-filesystem-vswitch"},
        policy={"phase": "execute"},
    )
    add(
        "create-export-vpc",
        "create",
        1,
        "vpc",
        "CreateVpc",
        {
            "RegionId": "cn-hangzhou",
            "VpcName": "{name}-export",
            "CidrBlock": "172.29.0.0/16",
            "ClientToken": "{name}-export-vpc",
        },
        {},
    )
    add(
        "create-export-vswitch",
        "create",
        1,
        "vpc",
        "CreateVSwitch",
        {
            "RegionId": "cn-hangzhou",
            "VpcId": "$created_id",
            "ZoneId": "cn-hangzhou-k",
            "CidrBlock": "172.29.0.0/24",
            "VSwitchName": "{name}-export",
            "ClientToken": "{name}-export-vsw",
        },
        {"VpcId": "create-export-vpc"},
        requires=["read-export-vpc"],
    )
    add(
        "read-export-vpc",
        "read",
        1,
        "vpc",
        "DescribeVpcs",
        {"RegionId": "cn-hangzhou", "VpcId": "$created_id", "PageNumber": 1, "PageSize": 10},
        {"VpcId": "create-export-vpc"},
        policy={"phase": "execute"},
    )
    add(
        "read-export-vswitch",
        "read",
        1,
        "vpc",
        "DescribeVSwitches",
        {"RegionId": "cn-hangzhou", "VSwitchId": "$created_id", "PageNumber": 1, "PageSize": 10},
        {"VSwitchId": "create-export-vswitch"},
        policy={"phase": "execute"},
    )
    add(
        "precheck-bucket-absent",
        "read",
        1,
        "oss",
        "GetBucketInfo",
        {"bucket": "{name}"},
        {},
        policy={"phase": "execute"},
    )
    add(
        "create-bucket",
        "create",
        1,
        "oss",
        "PutBucket",
        {
            "bucket": "{name}",
            "x-oss-acl": "private",
            "x-oss-bucket-tagging": "cpfs-dataflow=true",
            "body": {"CreateBucketConfiguration": {"StorageClass": "Standard", "DataRedundancyType": "LRS"}},
        },
        {},
        requires=["precheck-bucket-absent"],
    )
    add(
        "read-bucket",
        "read",
        1,
        "oss",
        "GetBucketInfo",
        {"bucket": "$created_id"},
        {"bucket": "create-bucket"},
        policy={"phase": "execute"},
    )
    add(
        "read-bucket-empty",
        "read",
        1,
        "oss",
        "ListObjectsV2",
        {"bucket": "$created_id", "max-keys": 1},
        {"bucket": "create-bucket"},
        policy={"phase": "execute"},
    )
    add(
        "precheck-filesystem",
        "precheck",
        1,
        "nas",
        "CreateFileSystem",
        {
            "FileSystemType": "cpfs",
            "ProtocolType": "cpfs",
            "StorageType": "advance_200",
            "ChargeType": "PayAsYouGo",
            "Capacity": 3600,
            "Bandwidth": 720,
            "ZoneId": "cn-hangzhou-k",
            "VpcId": "$created_id",
            "VSwitchId": "$created_id",
            "Description": "{name}",
            "EncryptType": 0,
            "DryRun": True,
            "ClientToken": "{name}-fs-check",
        },
        {"VpcId": "create-filesystem-vpc", "VSwitchId": "create-filesystem-vswitch"},
        requires=["read-filesystem-vpc", "read-filesystem-vswitch", "read-filesystem-implicit-members"],
    )
    add(
        "create-filesystem",
        "create",
        1,
        "nas",
        "CreateFileSystem",
        {
            "FileSystemType": "cpfs",
            "ProtocolType": "cpfs",
            "StorageType": "advance_200",
            "ChargeType": "PayAsYouGo",
            "Capacity": 3600,
            "Bandwidth": 720,
            "ZoneId": "cn-hangzhou-k",
            "VpcId": "$created_id",
            "VSwitchId": "$created_id",
            "Description": "{name}",
            "EncryptType": 0,
            "DryRun": False,
            "ClientToken": "{name}-fs",
        },
        {"VpcId": "create-filesystem-vpc", "VSwitchId": "create-filesystem-vswitch"},
        requires=["precheck-filesystem"],
    )
    add(
        "read-filesystem-ready",
        "read",
        100,
        "nas",
        "DescribeFileSystems",
        {"FileSystemId": "$created_id", "PageNumber": 1, "PageSize": 10},
        {"FileSystemId": "create-filesystem"},
        policy={"phase": "execute", "min_interval_seconds": 30},
    )
    add(
        "create-fileset",
        "create",
        1,
        "nas",
        "CreateFileset",
        {"FileSystemId": "$created_id", "FileSystemPath": "/{name}/", "Description": "{name}"},
        {"FileSystemId": "create-filesystem"},
        requires=["read-filesystem-ready"],
    )
    add(
        "read-fileset-ready",
        "read",
        10,
        "nas",
        "DescribeFilesets",
        {"FileSystemId": "$created_id", "MaxResults": 20},
        {"FileSystemId": "create-filesystem"},
        policy={"phase": "execute", "min_interval_seconds": 30},
    )
    add(
        "precheck-protocol",
        "precheck",
        1,
        "nas",
        "CreateProtocolService",
        {
            "FileSystemId": "$created_id",
            "ProtocolSpec": "General",
            "ProtocolType": "NFS",
            "Description": "{name}",
            "VpcId": "$created_id",
            "VSwitchId": "$created_id",
            "DryRun": True,
            "ClientToken": "{name}-ptc-check",
        },
        {
            "FileSystemId": "create-filesystem",
            "VpcId": "create-filesystem-vpc",
            "VSwitchId": "create-filesystem-vswitch",
        },
        requires=["read-filesystem-ready"],
    )
    add(
        "create-protocol",
        "create",
        1,
        "nas",
        "CreateProtocolService",
        {
            "FileSystemId": "$created_id",
            "ProtocolSpec": "General",
            "ProtocolType": "NFS",
            "Description": "{name}",
            "VpcId": "$created_id",
            "VSwitchId": "$created_id",
            "DryRun": False,
            "ClientToken": "{name}-ptc",
        },
        {
            "FileSystemId": "create-filesystem",
            "VpcId": "create-filesystem-vpc",
            "VSwitchId": "create-filesystem-vswitch",
        },
        requires=["precheck-protocol"],
    )
    add(
        "read-protocol-ready",
        "read",
        30,
        "nas",
        "DescribeProtocolService",
        {"FileSystemId": "$created_id", "ProtocolServiceIds": "$created_id", "MaxResults": 20},
        {"FileSystemId": "create-filesystem", "ProtocolServiceIds": "create-protocol"},
        policy={"phase": "execute", "min_interval_seconds": 30},
    )
    add(
        "precheck-export",
        "precheck",
        1,
        "nas",
        "CreateProtocolMountTarget",
        {
            "FileSystemId": "$created_id",
            "ProtocolServiceId": "$created_id",
            "FsetId": "$created_id",
            "VpcId": "$created_id",
            "VSwitchId": "$created_id",
            "AccessGroupName": "DEFAULT_VPC_GROUP_NAME",
            "Description": "{name}",
            "DryRun": True,
            "ClientToken": "{name}-exp-check",
        },
        {
            "FileSystemId": "create-filesystem",
            "ProtocolServiceId": "create-protocol",
            "FsetId": "create-fileset",
            "VpcId": "create-export-vpc",
            "VSwitchId": "create-export-vswitch",
        },
        requires=["read-protocol-ready", "read-fileset-ready", "read-export-vpc", "read-export-vswitch"],
    )
    add(
        "create-export",
        "create",
        1,
        "nas",
        "CreateProtocolMountTarget",
        {
            "FileSystemId": "$created_id",
            "ProtocolServiceId": "$created_id",
            "FsetId": "$created_id",
            "VpcId": "$created_id",
            "VSwitchId": "$created_id",
            "AccessGroupName": "DEFAULT_VPC_GROUP_NAME",
            "Description": "{name}",
            "DryRun": False,
            "ClientToken": "{name}-exp",
        },
        {
            "FileSystemId": "create-filesystem",
            "ProtocolServiceId": "create-protocol",
            "FsetId": "create-fileset",
            "VpcId": "create-export-vpc",
            "VSwitchId": "create-export-vswitch",
        },
        requires=["precheck-export"],
    )
    add(
        "read-export-ready",
        "read",
        20,
        "nas",
        "GetProtocolMountTarget",
        {"FileSystemId": "$created_id", "ProtocolServiceId": "$created_id", "ExportId": "$created_id"},
        {"FileSystemId": "create-filesystem", "ProtocolServiceId": "create-protocol", "ExportId": "create-export"},
        policy={"phase": "execute", "min_interval_seconds": 30},
    )
    add(
        "precheck-dataflow",
        "precheck",
        1,
        "nas",
        "CreateDataFlow",
        {
            "FileSystemId": "$created_id",
            "FsetId": "$created_id",
            "SourceStorage": "$bound_own_oss_uri",
            "SourceSecurityType": "SSL",
            "Description": "{name}",
            "Throughput": 600,
            "AutoRefreshPolicy": "None",
            "DryRun": True,
            "ClientToken": "{name}-df-check",
        },
        {"FileSystemId": "create-filesystem", "FsetId": "create-fileset", "SourceStorage": "create-bucket"},
        requires=["read-export-ready", "read-bucket", "read-bucket-empty"],
    )
    add(
        "create-dataflow",
        "create",
        1,
        "nas",
        "CreateDataFlow",
        {
            "FileSystemId": "$created_id",
            "FsetId": "$created_id",
            "SourceStorage": "$bound_own_oss_uri",
            "SourceSecurityType": "SSL",
            "Description": "{name}",
            "Throughput": 600,
            "AutoRefreshPolicy": "None",
            "DryRun": False,
            "ClientToken": "{name}-df",
        },
        {"FileSystemId": "create-filesystem", "FsetId": "create-fileset", "SourceStorage": "create-bucket"},
        requires=["precheck-dataflow"],
    )
    add(
        "query-dataflow-source",
        "read",
        20,
        "nas",
        "DescribeDataFlows",
        {"FileSystemId": "$created_id", "MaxResults": 20},
        {"FileSystemId": "create-filesystem"},
        policy={"phase": "execute", "min_interval_seconds": 30},
    )
    add(
        "read-filesystem-implicit-members",
        "read",
        2,
        "ecs",
        "DescribeSecurityGroups",
        {"RegionId": "cn-hangzhou", "VpcId": "$created_id", "PageNumber": 1, "PageSize": 100},
        {"VpcId": "create-filesystem-vpc"},
        policy={"phase": "execute"},
    )
    add(
        "read-export-implicit-members",
        "read",
        2,
        "ecs",
        "DescribeSecurityGroups",
        {"RegionId": "cn-hangzhou", "VpcId": "$created_id", "PageNumber": 1, "PageSize": 100},
        {"VpcId": "create-export-vpc"},
        policy={"phase": "execute"},
    )
    add(
        "delete-dataflow",
        "delete",
        1,
        "nas",
        "DeleteDataFlow",
        {"FileSystemId": "$created_id", "DataFlowId": "$created_id"},
        {"FileSystemId": "create-filesystem", "DataFlowId": "create-dataflow"},
        parent="create-dataflow",
        target="DataFlowId",
        path=["DataFlowId"],
    )
    add(
        "delete-dataflow-absence",
        "read",
        30,
        "nas",
        "DescribeDataFlows",
        {"FileSystemId": "$created_id", "MaxResults": 20},
        {"FileSystemId": "create-filesystem"},
        policy={
            "phase": "cleanup",
            "min_interval_seconds": 30,
            "required_cleanup": True,
            "after_delete": "create-dataflow",
        },
    )
    add(
        "delete-export",
        "delete",
        1,
        "nas",
        "DeleteProtocolMountTarget",
        {"FileSystemId": "$created_id", "ProtocolServiceId": "$created_id", "ExportId": "$created_id"},
        {"FileSystemId": "create-filesystem", "ProtocolServiceId": "create-protocol", "ExportId": "create-export"},
        parent="create-export",
        target="ExportId",
        path=["ExportId"],
    )
    add(
        "delete-export-absence",
        "read",
        30,
        "nas",
        "GetProtocolMountTarget",
        {"FileSystemId": "$created_id", "ProtocolServiceId": "$created_id", "ExportId": "$created_id"},
        {"FileSystemId": "create-filesystem", "ProtocolServiceId": "create-protocol", "ExportId": "create-export"},
        policy={
            "phase": "cleanup",
            "min_interval_seconds": 30,
            "required_cleanup": True,
            "after_delete": "create-export",
        },
    )
    add(
        "delete-protocol",
        "delete",
        1,
        "nas",
        "DeleteProtocolService",
        {"FileSystemId": "$created_id", "ProtocolServiceId": "$created_id"},
        {"FileSystemId": "create-filesystem", "ProtocolServiceId": "create-protocol"},
        parent="create-protocol",
        target="ProtocolServiceId",
        path=["ProtocolServiceId"],
    )
    add(
        "delete-protocol-absence",
        "read",
        30,
        "nas",
        "DescribeProtocolService",
        {"FileSystemId": "$created_id", "ProtocolServiceIds": "$created_id", "MaxResults": 20},
        {"FileSystemId": "create-filesystem", "ProtocolServiceIds": "create-protocol"},
        policy={
            "phase": "cleanup",
            "min_interval_seconds": 30,
            "required_cleanup": True,
            "after_delete": "create-protocol",
        },
    )
    add(
        "delete-fileset",
        "delete",
        1,
        "nas",
        "DeleteFileset",
        {"FileSystemId": "$created_id", "FsetId": "$created_id"},
        {"FileSystemId": "create-filesystem", "FsetId": "create-fileset"},
        parent="create-fileset",
        target="FsetId",
        path=["FsetId"],
    )
    add(
        "delete-fileset-absence",
        "read",
        30,
        "nas",
        "DescribeFilesets",
        {"FileSystemId": "$created_id", "MaxResults": 20},
        {"FileSystemId": "create-filesystem"},
        policy={
            "phase": "cleanup",
            "min_interval_seconds": 30,
            "required_cleanup": True,
            "after_delete": "create-fileset",
        },
    )
    add(
        "delete-filesystem",
        "delete",
        1,
        "nas",
        "DeleteFileSystem",
        {"FileSystemId": "$created_id"},
        {"FileSystemId": "create-filesystem"},
        parent="create-filesystem",
        target="FileSystemId",
        path=["FileSystemId"],
    )
    add(
        "delete-filesystem-absence",
        "read",
        30,
        "nas",
        "DescribeFileSystems",
        {"FileSystemId": "$created_id", "PageNumber": 1, "PageSize": 10},
        {"FileSystemId": "create-filesystem"},
        policy={
            "phase": "cleanup",
            "min_interval_seconds": 30,
            "required_cleanup": True,
            "after_delete": "create-filesystem",
        },
    )
    add(
        "delete-bucket",
        "delete",
        1,
        "oss",
        "DeleteBucket",
        {"bucket": "$created_id"},
        {"bucket": "create-bucket"},
        parent="create-bucket",
        target="bucket",
        path=["headers", "location"],
    )
    add(
        "delete-bucket-absence",
        "read",
        1,
        "oss",
        "GetBucketInfo",
        {"bucket": "$created_id"},
        {"bucket": "create-bucket"},
        policy={"phase": "cleanup", "required_cleanup": True, "after_delete": "create-bucket"},
        parent="create-bucket",
    )
    add(
        "read-filesystem-implicit-sg-absence",
        "read",
        20,
        "ecs",
        "DescribeSecurityGroups",
        {"RegionId": "cn-hangzhou", "VpcId": "$created_id", "PageNumber": 1, "PageSize": 100},
        {"VpcId": "create-filesystem-vpc"},
        policy={
            "phase": "cleanup",
            "min_interval_seconds": 30,
            "required_cleanup": True,
            "after_delete": "create-protocol",
        },
    )
    add(
        "read-filesystem-eni-absence",
        "read",
        20,
        "ecs",
        "DescribeNetworkInterfaces",
        {"RegionId": "cn-hangzhou", "VpcId": "$created_id", "MaxResults": 100},
        {"VpcId": "create-filesystem-vpc"},
        policy={
            "phase": "cleanup",
            "min_interval_seconds": 30,
            "required_cleanup": True,
            "after_delete": "create-filesystem",
        },
    )
    add(
        "delete-filesystem-vswitch",
        "delete",
        1,
        "vpc",
        "DeleteVSwitch",
        {"RegionId": "cn-hangzhou", "VSwitchId": "$created_id"},
        {"VSwitchId": "create-filesystem-vswitch"},
        parent="create-filesystem-vswitch",
        target="VSwitchId",
        path=["VSwitchId"],
    )
    add(
        "delete-filesystem-vswitch-absence",
        "read",
        10,
        "vpc",
        "DescribeVSwitches",
        {"RegionId": "cn-hangzhou", "VSwitchId": "$created_id", "PageNumber": 1, "PageSize": 10},
        {"VSwitchId": "create-filesystem-vswitch"},
        policy={
            "phase": "cleanup",
            "min_interval_seconds": 30,
            "required_cleanup": True,
            "after_delete": "create-filesystem-vswitch",
        },
        parent="create-filesystem-vswitch",
    )
    add(
        "delete-filesystem-vpc",
        "delete",
        1,
        "vpc",
        "DeleteVpc",
        {"RegionId": "cn-hangzhou", "VpcId": "$created_id"},
        {"VpcId": "create-filesystem-vpc"},
        parent="create-filesystem-vpc",
        target="VpcId",
        path=["VpcId"],
    )
    add(
        "delete-filesystem-vpc-absence",
        "read",
        10,
        "vpc",
        "DescribeVpcs",
        {"RegionId": "cn-hangzhou", "VpcId": "$created_id", "PageNumber": 1, "PageSize": 10},
        {"VpcId": "create-filesystem-vpc"},
        policy={
            "phase": "cleanup",
            "min_interval_seconds": 30,
            "required_cleanup": True,
            "after_delete": "create-filesystem-vpc",
        },
        parent="create-filesystem-vpc",
    )
    add(
        "read-export-implicit-sg-absence",
        "read",
        20,
        "ecs",
        "DescribeSecurityGroups",
        {"RegionId": "cn-hangzhou", "VpcId": "$created_id", "PageNumber": 1, "PageSize": 100},
        {"VpcId": "create-export-vpc"},
        policy={
            "phase": "cleanup",
            "min_interval_seconds": 30,
            "required_cleanup": True,
            "after_delete": "create-protocol",
        },
    )
    add(
        "read-export-eni-absence",
        "read",
        20,
        "ecs",
        "DescribeNetworkInterfaces",
        {"RegionId": "cn-hangzhou", "VpcId": "$created_id", "MaxResults": 100},
        {"VpcId": "create-export-vpc"},
        policy={
            "phase": "cleanup",
            "min_interval_seconds": 30,
            "required_cleanup": True,
            "after_delete": "create-filesystem",
        },
    )
    add(
        "delete-export-vswitch",
        "delete",
        1,
        "vpc",
        "DeleteVSwitch",
        {"RegionId": "cn-hangzhou", "VSwitchId": "$created_id"},
        {"VSwitchId": "create-export-vswitch"},
        parent="create-export-vswitch",
        target="VSwitchId",
        path=["VSwitchId"],
    )
    add(
        "delete-export-vswitch-absence",
        "read",
        10,
        "vpc",
        "DescribeVSwitches",
        {"RegionId": "cn-hangzhou", "VSwitchId": "$created_id", "PageNumber": 1, "PageSize": 10},
        {"VSwitchId": "create-export-vswitch"},
        policy={
            "phase": "cleanup",
            "min_interval_seconds": 30,
            "required_cleanup": True,
            "after_delete": "create-export-vswitch",
        },
        parent="create-export-vswitch",
    )
    add(
        "delete-export-vpc",
        "delete",
        1,
        "vpc",
        "DeleteVpc",
        {"RegionId": "cn-hangzhou", "VpcId": "$created_id"},
        {"VpcId": "create-export-vpc"},
        parent="create-export-vpc",
        target="VpcId",
        path=["VpcId"],
    )
    add(
        "delete-export-vpc-absence",
        "read",
        10,
        "vpc",
        "DescribeVpcs",
        {"RegionId": "cn-hangzhou", "VpcId": "$created_id", "PageNumber": 1, "PageSize": 10},
        {"VpcId": "create-export-vpc"},
        policy={
            "phase": "cleanup",
            "min_interval_seconds": 30,
            "required_cleanup": True,
            "after_delete": "create-export-vpc",
        },
        parent="create-export-vpc",
    )
    if variant == "bandwidth_omission_bwo1":
        # One closed candidate, still requiring independent full operation approval.
        # New tokens also distinguish the two already-deleted network parents.
        for grant in result:
            role = grant["id"]
            params = grant["call"]["params"]
            if role in {
                "create-filesystem-vpc",
                "create-filesystem-vswitch",
                "precheck-filesystem",
                "create-filesystem",
            }:
                params["ClientToken"] += "-bwo1"
            if role in {"precheck-filesystem", "create-filesystem"}:
                del params["Bandwidth"]
    return result


NAS_NATIVE_IDS = {
    "CreateVpc": ("VpcId", r"vpc-[A-Za-z0-9]+"),
    "CreateVSwitch": ("VSwitchId", r"vsw-[A-Za-z0-9]+"),
    "CreateFileSystem": ("FileSystemId", r"cpfs-[A-Za-z0-9]+"),
    "CreateFileset": ("FsetId", r"fset-[A-Za-z0-9]+"),
    "CreateProtocolService": ("ProtocolServiceId", r"ptc-[A-Za-z0-9]+"),
    "CreateProtocolMountTarget": ("ExportId", r"exp-[A-Za-z0-9]+"),
    "CreateDataFlow": ("DataFlowId", r"df-[A-Za-z0-9]+"),
}


def nas_native_binding(grant: Any) -> dict[str, Any] | None:
    value = dict(grant).get("response_path")
    value = json.loads(value or "null") if isinstance(value, str) else value
    return value if isinstance(value, dict) and value.get("kind") == "nas_native_dependency" else None


def nas_native_scope(connection: sqlite3.Connection, scope: str) -> bool:
    return any(
        nas_native_binding(row)
        for row in connection.execute(
            "SELECT response_path FROM grants WHERE scope=?",
            (scope,),
        )
    )


def validate_nas_native_plan(grants: list[dict[str, Any]]) -> bool:
    if not any(nas_native_binding(grant) for grant in grants):
        return False
    try:
        bucket = next(grant for grant in grants if grant["id"] == "create-bucket")
        name = bucket["call"]["params"]["bucket"]
        actual = canonical(sorted(grants, key=lambda item: item["id"]))
        # Compare complete closed variants, never independent optional fields.
        # Mixed tokens/parameters, extra keys, paths, encodings, cycles,
        # DryRun string coercions and quota/policy changes remain forbidden.
        if not any(
            actual == canonical(sorted(nas_native_plan(name, variant=variant), key=lambda item: item["id"]))
            for variant in NAS_NATIVE_PLAN_VARIANTS
        ):
            raise GateDeniedError()
    except (StopIteration, KeyError, TypeError, ValueError) as error:
        raise GateDeniedError() from error
    return True


def nas_native_bucket(call: dict[str, Any]) -> bool:
    params = call.get("params", {})
    return params.get("x-oss-bucket-tagging") == "cpfs-dataflow=true" and is_oss_bucket_create(
        {**call, "params": {key: value for key, value in params.items() if key != "x-oss-bucket-tagging"}}
    )


def nas_native_identifier(call: dict[str, Any], envelope: Any) -> str | None:
    if not isinstance(envelope, dict):
        return None
    if call["action"] == "PutBucket":
        return response_identifier(
            {**call, "params": {key: value for key, value in call["params"].items() if key != "x-oss-bucket-tagging"}},
            {key: envelope.get(key) for key in ("body", "status", "headers")},
            ["headers", "location"],
        )
    key, pattern = NAS_NATIVE_IDS.get(call["action"], ("", ""))
    body = envelope.get("body")
    ident = body.get(key) if isinstance(body, dict) else None
    return ident if isinstance(ident, str) and re.fullmatch(pattern, ident) else None


def nas_native_success(call: dict[str, Any], envelope: Any, kind: str) -> bool:
    if not isinstance(envelope, dict) or type(envelope.get("status")) is not int:
        return False
    body = envelope.get("body")
    headers = envelope.get("headers", {})
    if not isinstance(headers, dict):
        return False
    if call["product"] == "oss" and call["action"] == "DeleteBucket":
        # The actual SDK transport preserves the documented 204/empty body.
        # Do not turn a synthetic 200, other 2xx or an error body into deletion.
        request_ids = [
            value for key, value in headers.items() if isinstance(key, str) and key.casefold() == "x-oss-request-id"
        ]
        return (
            kind == "delete"
            and envelope["status"] == 204
            and body is None
            and call.get("version") == "2019-05-17"
            and call.get("region_id") == "cn-hangzhou"
            and isinstance(call.get("params"), dict)
            and set(call["params"]) == {"bucket"}
            and isinstance(call["params"]["bucket"], str)
            and re.fullmatch(r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]", call["params"]["bucket"]) is not None
            and len(request_ids) == 1
            and isinstance(request_ids[0], str)
            and 0 < len(request_ids[0]) <= 256
            and not any(char.isspace() for char in request_ids[0])
        )
    if envelope["status"] != 200:
        return False
    if call["product"] == "oss":
        if kind == "create":
            return nas_native_bucket(call) and nas_native_identifier(call, envelope) is not None
        return any(
            isinstance(key, str) and key.casefold() == "x-oss-request-id" and isinstance(value, str) and value
            for key, value in headers.items()
        )
    if (
        not isinstance(body, dict)
        or not isinstance(body.get("RequestId"), str)
        or not body["RequestId"]
        or len(body["RequestId"]) > 256
        or any(char.isspace() for char in body["RequestId"])
        or "Code" in body
        or "ErrorCode" in body
    ):
        return False
    header_ids = [
        value for key, value in headers.items() if isinstance(key, str) and key.casefold() == "x-acs-request-id"
    ]
    if header_ids and header_ids != [body["RequestId"]]:
        return False
    if kind == "precheck":
        # Official NAS successful DryRun returns HTTP200 with no created ID.
        # DataFlow docs also mention empty FileSystemId: require both empty.
        return call["params"].get("DryRun") is True and all(
            body.get(key) in (None, "") for key in ("FileSystemId", "ProtocolServiceId", "ExportId", "DataFlowId")
        )
    return kind != "create" or nas_native_identifier(call, envelope) is not None


def nas_native_snapshot(row: Any) -> str:
    """Digest the immutable original row, including signer and raw wire envelope."""
    return hashlib.sha256(canonical(dict(row)).encode()).hexdigest()


def _native_private_review(path: str, digest: str) -> dict[str, Any]:
    file = Path(path)
    if not file.is_absolute() or not file.is_file() or file.is_symlink():
        raise GateDeniedError()
    if os.name != "nt" and file.stat().st_mode & 0o077:
        raise GateDeniedError()
    raw = file.read_bytes()
    value = json.loads(raw)
    if hashlib.sha256(raw).hexdigest() != digest or not isinstance(value, dict):
        raise GateDeniedError()
    return value


def _native_review_record(connection: sqlite3.Connection, key: str) -> tuple[Any, dict[str, Any]] | None:
    row = connection.execute("SELECT * FROM cleanup_identity_reviews WHERE scope=?", (key,)).fetchone()
    return (row, _native_private_review(row["evidence"], row["evidence_sha256"])) if row else None


def _native_preserved_rows(connection: sqlite3.Connection, scope: str, proofs: Any) -> dict[str, Any]:
    if not isinstance(proofs, list) or not proofs:
        raise GateDeniedError()
    rows = {}
    for proof in proofs:
        row = connection.execute(
            "SELECT * FROM attempts WHERE scope=? AND id=?",
            (scope, proof["id"]),
        ).fetchone()
        if not row or row["id"] in rows or nas_native_snapshot(row) != proof["sha256"]:
            raise GateDeniedError()
        rows[row["id"]] = row
    return rows


def _native_preserved_grants(
    connection: sqlite3.Connection, scope: str, authority: str, proofs: Any, ids: set[str]
) -> None:
    if not isinstance(proofs, list) or [p["id"] for p in proofs] != sorted(ids):
        raise GateDeniedError()
    for proof in proofs:
        row = connection.execute(
            "SELECT * FROM grants WHERE scope=? AND id=?",
            (scope, proof["id"]),
        ).fetchone()
        limit = connection.execute(
            "SELECT used FROM authority_limits WHERE authority=? AND id=?",
            (authority, proof["id"]),
        ).fetchone()
        count = connection.execute(
            "SELECT COUNT(*) FROM attempts WHERE scope=? AND grant_id=?",
            (scope, proof["id"]),
        ).fetchone()[0]
        if (
            not row
            or row["authority"] != authority
            or nas_native_snapshot({k: v for k, v in dict(row).items() if k != "used"}) != proof["definition_sha256"]
            or type(proof["used"]) is not int
            or not 0 <= proof["used"] <= row["used"] <= row["quota"]
            or row["used"] != count
            or not limit
            or limit[0] != count
        ):
            raise GateDeniedError()


def _native_identity_row(
    connection: sqlite3.Connection, active: Any, grant: Any, row: Any, call: dict[str, Any], *, permit: Any = None
) -> None:
    if (
        not row
        or row["scope"] != active["scope"]
        or row["session"] != active["session"]
        or row["grant_id"] != grant["id"]
        or row["state"] != "succeeded"
        or not row["credential"]
        or not row["reference_scope"]
        or not row["tool_use_id"]
        or grant["kind"] != "identity"
        or grant["quota"] != 1
        or grant["used"] != 1
        or json.loads(grant["call"]) != call
        or not _native_identity_call(call)
    ):
        raise GateDeniedError()
    raw = json.loads(row["response"] or "null")
    if permit is None:
        if not nas_native_success(call, raw, "identity") or raw.get("call") != call:
            raise GateDeniedError()
        body = raw["body"]
    else:
        # The legacy one-shot permit wrote the actual STS body. Preserve it:
        # only this exact successful, reviewed permit attempt may use that shape.
        if (
            permit["state"] != "consumed"
            or permit["attempt_id"] != row["id"]
            or permit["grant_id"] != grant["id"]
            or permit["call"] != canonical(call)
            or not permit["created"] <= row["created"] < permit["expires"]
        ):
            raise GateDeniedError()
        body = raw
    if (
        not isinstance(body, dict)
        or body.get("AccountId") != active["account"]
        or not isinstance(body.get("RequestId"), str)
        or not body["RequestId"]
        or len(body["RequestId"]) > 256
        or any(c.isspace() for c in body["RequestId"])
        or any(k in body for k in ("Code", "ErrorCode"))
        or not connection.execute(
            "SELECT 1 FROM identities WHERE scope=? AND session=? AND credential=? AND account=?",
            (active["scope"], active["session"], row["credential"], active["account"]),
        ).fetchone()
    ):
        raise GateDeniedError()


def native_consumed_handoff(connection: sqlite3.Connection, active: Any) -> tuple[Any, Any, dict[str, str]] | None:
    """Validate an append-only OPS handoff and each one-shot signer successor.

    A consumed permit alone never unlocks anything. No schema/permit/original
    grant or wire row is changed. Historical parents keep their original signer.
    """
    try:
        scope = active["scope"]
        stored = _native_review_record(connection, scope + ":native-handoff")
        if stored is None:
            return None
        record, review = stored
        permit = connection.execute(
            "SELECT * FROM native_identity_recovery_permits WHERE scope=?",
            (scope,),
        ).fetchone()
        base_stored = _native_review_record(connection, scope)
        if base_stored is None:
            return None
        base_record, base = base_stored
        if (
            not permit
            or permit["state"] != "consumed"
            or review.get("kind") != "ops_native_continuation_handoff"
            or review.get("approved") is not True
            or review.get("permit_sha256") != nas_native_snapshot(permit)
            or review.get("original_review_sha256") != base_record["evidence_sha256"]
            or permit["original_review_sha256"] != base_record["evidence_sha256"]
            or base.get("kind") != "ops_native_signer_continuation_review"
            or base.get("approved") is not True
            or base.get("additional_identity_reads") != 1
            or base.get("grant_id") != permit["grant_id"]
            or review.get("slot") != permit["slot"]
            or review.get("new_creation_quota") != 0
            or base.get("identity_call") != json.loads(permit["call"])
        ):
            raise GateDeniedError()
        for key in ("scope", "session", "account", "authority"):
            if (
                review.get(key) != permit[key]
                or base.get(key) != permit[key]
                or base_record[key] != permit[key]
                or (record[key] != (scope + ":native-handoff" if key == "scope" else permit[key]))
            ):
                raise GateDeniedError()
        if (
            active["session"] != permit["session"]
            or active["account"] != permit["account"]
            or record["grant_id"] != permit["grant_id"]
        ):
            raise GateDeniedError()
        if "slot" in dict(active) and active["slot"] != permit["slot"]:
            raise GateDeniedError()
        phases = review.get("phases")
        if phases not in (["cleanup"], ["execute", "cleanup"]):
            raise GateDeniedError()
        if "phase" in dict(active) and active["phase"] not in phases:
            raise GateDeniedError()
        if dict(active).get("phase") == "execute":
            if connection.execute(
                "SELECT 1 FROM attempts a JOIN grants g ON a.scope=g.scope AND a.grant_id=g.id "
                "WHERE a.scope=? AND g.kind='delete'",
                (scope,),
            ).fetchone():
                raise GateDeniedError()
        marker = Path(permit["marker_path"])
        marker_value = _native_private_review(str(marker), review["marker_sha256"])
        if (
            marker_value.get("state") not in {"may_have_submitted", "submitted"}
            or marker_value.get("stage") != "identity"
            or marker_value.get("proposal_sha256") != permit["proposal_sha256"]
            or marker_value.get("review_sha256") != permit["review_sha256"]
            or marker_value.get("scope") != scope
            or marker_value.get("session_id") != active["session"]
        ):
            raise GateDeniedError()
        grants = {g["id"]: g for g in connection.execute("SELECT * FROM grants WHERE scope=?", (scope,))}
        original_ids = {p["id"] for p in base["preserved_grants"]}
        first_id = permit["grant_id"]
        _native_preserved_grants(connection, scope, permit["authority"], base["preserved_grants"], original_ids)
        original_rows = _native_preserved_rows(connection, scope, base["preserved_attempts"])
        root = original_rows[base["original_identity_attempt"]]
        _native_identity_row(connection, active, grants["identity"], root, json.loads(permit["call"]))
        if any(
            r["session"] != active["session"]
            or r["credential"] != root["credential"]
            or r["reference_scope"] != root["reference_scope"]
            or r["created"] < root["created"]
            for r in original_rows.values()
        ):
            raise GateDeniedError()
        extension = grants[first_id]
        if (
            extension["authority"] != permit["authority"]
            or extension["parent"] is not None
            or json.loads(extension["response_path"] or "null") is not None
            or extension["target_param"] is not None
            or extension["target_encoding"] is not None
            or read_policy(extension)
            or not nas_native_binding(grants["identity"])
        ):
            raise GateDeniedError()
        first = connection.execute("SELECT * FROM attempts WHERE id=?", (permit["attempt_id"],)).fetchone()
        _native_identity_row(connection, active, grants[first_id], first, json.loads(permit["call"]), permit=permit)
        if (
            first["created"] <= max(r["created"] for r in original_rows.values())
            or first["credential"] == root["credential"]
            or first["reference_scope"] == root["reference_scope"]
            or review["identity_attempt"] != {"id": first["id"], "sha256": nas_native_snapshot(first)}
        ):
            raise GateDeniedError()
        rows = _native_preserved_rows(connection, scope, review["preserved_attempts"])
        if set(rows) != set(original_rows) | {first["id"]}:
            raise GateDeniedError()
        current_ids = original_ids | {first_id}
        _native_preserved_grants(connection, scope, permit["authority"], review["preserved_grants"], current_ids)
        allowed = review["grant_ids"]
        if (
            not isinstance(allowed, list)
            or not allowed
            or allowed != sorted(set(allowed))
            or any(i not in original_ids or grants[i]["kind"] == "identity" for i in allowed)
        ):
            raise GateDeniedError()
        signer, extension = first, grants[first_id]
        previous_sha = record["evidence_sha256"]
        prefixes = connection.execute(
            "SELECT * FROM cleanup_identity_reviews WHERE substr(scope,1,?)=? ORDER BY scope",
            (len(scope + ":native-renewal:"), scope + ":native-renewal:"),
        ).fetchall()
        identities = [root, first]
        pending = False
        for number, renewal_record in enumerate(prefixes, 1):
            key = scope + ":native-renewal:" + str(number).zfill(6)
            renewal = _native_private_review(renewal_record["evidence"], renewal_record["evidence_sha256"])
            new_id = "ops-native-identity-renewal-" + str(number)
            if (
                renewal_record["scope"] != key
                or renewal_record["grant_id"] != new_id
                or renewal.get("kind") != "ops_native_signer_renewal_review"
                or renewal.get("approved") is not True
                or renewal.get("sequence") != number
                or renewal.get("additional_identity_reads") != 1
                or renewal.get("new_creation_quota") != 0
                or renewal.get("previous_review_sha256") != previous_sha
                or pending
                or renewal.get("grant_id") != new_id
                or renewal.get("slot") != permit["slot"]
                or renewal.get("identity_call") != json.loads(permit["call"])
                or renewal.get("previous_identity") != {"id": signer["id"], "sha256": nas_native_snapshot(signer)}
            ):
                raise GateDeniedError()
            for field in ("scope", "session", "account", "authority"):
                if renewal.get(field) != permit[field] or (field != "scope" and renewal_record[field] != permit[field]):
                    raise GateDeniedError()
            prefix = _native_preserved_rows(connection, scope, renewal["preserved_attempts"])
            if not set(rows) <= set(prefix) or signer["id"] not in prefix:
                raise GateDeniedError()
            _native_preserved_grants(connection, scope, permit["authority"], renewal["preserved_grants"], current_ids)
            extension = grants[new_id]
            if (
                extension["kind"] != "identity"
                or extension["quota"] != 1
                or extension["authority"] != permit["authority"]
                or extension["parent"] is not None
                or json.loads(extension["call"]) != json.loads(permit["call"])
                or json.loads(extension["response_path"] or "null") is not None
                or extension["target_param"] is not None
                or extension["target_encoding"] is not None
                or read_policy(extension)
            ):
                raise GateDeniedError()
            actual = connection.execute(
                "SELECT * FROM attempts WHERE scope=? AND grant_id=?",
                (scope, new_id),
            ).fetchall()
            if not actual:
                if extension["used"] != 0:
                    raise GateDeniedError()
                pending = True
            elif len(actual) == 1:
                newer = actual[0]
                _native_identity_row(connection, active, extension, newer, json.loads(permit["call"]))
                if (
                    newer["created"] <= max(r["created"] for r in prefix.values())
                    # A process restart deliberately issues a new opaque
                    # reference scope even for the same concrete signer.
                    # A fresh, reviewed same-account STS proves that pair;
                    # only an already-seen signer+scope pair is a replay.
                    or any(
                        newer["credential"] == i["credential"] and newer["reference_scope"] == i["reference_scope"]
                        for i in identities
                    )
                ):
                    raise GateDeniedError()
                identities.append(newer)
                signer = newer
            else:
                raise GateDeniedError()
            rows = prefix
            current_ids.add(new_id)
            previous_sha = renewal_record["evidence_sha256"]
        if set(grants) != current_ids:
            raise GateDeniedError()
        all_rows = connection.execute(
            "SELECT * FROM attempts WHERE scope=? ORDER BY created,rowid",
            (scope,),
        ).fetchall()
        for row in all_rows:
            eligible = [i for i in identities if i["created"] <= row["created"]]
            owner = eligible[-1] if eligible else None
            if (
                not owner
                or row["grant_id"] not in grants
                or (row["created"] <= signer["created"] and row["id"] != signer["id"] and row["id"] not in rows)
                or (row["id"] != owner["id"] and row["created"] <= owner["created"])
                or row["session"] != active["session"]
                or not row["tool_use_id"]
                or row["credential"] != owner["credential"]
                or row["reference_scope"] != owner["reference_scope"]
                or (
                    row["grant_id"] not in allowed
                    and row["id"] not in rows
                    and row["id"] not in {i["id"] for i in identities}
                )
            ):
                raise GateDeniedError()
        for row in rows.values():
            if row["id"] not in {r["id"] for r in all_rows}:
                raise GateDeniedError()
        if pending and {r["id"] for r in all_rows} != set(rows):
            raise GateDeniedError()
        # Exact counts, including each successor, make counter rollback/refill impossible.
        for ident, grant in grants.items():
            count = sum(r["grant_id"] == ident for r in all_rows)
            limit = connection.execute(
                "SELECT used FROM authority_limits WHERE authority=? AND id=?",
                (permit["authority"], ident),
            ).fetchone()
            if not limit or limit[0] != count or grant["used"] != count or not count <= grant["quota"]:
                raise GateDeniedError()
        snapshots = {r["id"]: nas_native_snapshot(r) for r in all_rows if r["created"] < signer["created"]}
        return extension, {**dict(signer), "_handoff_allowed": allowed, "_handoff_pending": pending}, snapshots
    except (OSError, KeyError, TypeError, ValueError, IndexError, AttributeError, sqlite3.Error) as error:
        raise GateDeniedError() from error


def register_native_continuation_handoff(path: Path, *, evidence: str, evidence_sha256: str) -> None:
    """Trusted OPS admission only; no phase transition, calls, refill or permit reset."""
    review = _native_private_review(evidence, evidence_sha256)
    with database(path) as connection:
        active = connection.execute(
            "SELECT * FROM active WHERE slot=? AND scope=? AND session=? AND account=?",
            (review["slot"], review["scope"], review["session"], review["account"]),
        ).fetchone()
        if not active or active["phase"] != "cleanup":
            raise GateDeniedError()
        connection.execute(
            "INSERT INTO cleanup_identity_reviews VALUES(?,?,?,?,?,?,?)",
            (
                review["scope"] + ":native-handoff",
                review["grant_id"],
                review["session"],
                review["account"],
                review["authority"],
                evidence,
                evidence_sha256,
            ),
        )
        if native_consumed_handoff(connection, active) is None:
            raise GateDeniedError()


def register_native_signer_renewal(path: Path, *, evidence: str, evidence_sha256: str) -> None:
    """Append one reviewed STS grant; retain all prior reviews, signatures and counts."""
    review = _native_private_review(evidence, evidence_sha256)
    with database(path) as connection:
        active = connection.execute(
            "SELECT * FROM active WHERE slot=? AND scope=? AND session=? AND account=?",
            (review["slot"], review["scope"], review["session"], review["account"]),
        ).fetchone()
        previous = native_consumed_handoff(connection, active) if active else None
        if not previous or previous[1]["_handoff_pending"]:
            raise GateDeniedError()
        grant_id = review["grant_id"]
        connection.execute(
            "INSERT INTO grants(scope,id,kind,call,quota,used,authority) VALUES(?,?,'identity',?,1,0,?)",
            (active["scope"], grant_id, canonical(review["identity_call"]), review["authority"]),
        )
        connection.execute("INSERT INTO authority_limits VALUES(?,?,0)", (review["authority"], grant_id))
        connection.execute(
            "INSERT INTO cleanup_identity_reviews VALUES(?,?,?,?,?,?,?)",
            (
                active["scope"] + ":native-renewal:" + str(review["sequence"]).zfill(6),
                grant_id,
                active["session"],
                active["account"],
                review["authority"],
                evidence,
                evidence_sha256,
            ),
        )
        native_consumed_handoff(connection, active)


def nas_native_continuation(connection: sqlite3.Connection, active: Any) -> tuple[Any, Any, dict[str, str]] | None:
    """Consume an OPS-only one-shot signer review; never install or refill grants.

    The existing private review journal is only a transport for the review.
    OPS must separately bind its SHA, append one exact STS grant and preserve
    every original grant/attempt. This function does no journal writes.
    """
    try:
        handoff = native_consumed_handoff(connection, active)
        if handoff is not None:
            return handoff
        approved = connection.execute(
            "SELECT * FROM cleanup_identity_reviews WHERE scope=?",
            (active["scope"],),
        ).fetchone()
        extras = connection.execute(
            "SELECT * FROM grants WHERE scope=? AND id LIKE 'ops-native-identity-%'",
            (active["scope"],),
        ).fetchall()
        if not extras:
            return None
        if len(extras) != 1 or not approved:
            raise GateDeniedError()
        extension = extras[0]
        file = Path(approved["evidence"])
        if not file.is_file() or file.is_symlink() or (os.name != "nt" and file.stat().st_mode & 0o077):
            raise GateDeniedError()
        raw = file.read_bytes()
        review = json.loads(raw)
        if (
            hashlib.sha256(raw).hexdigest() != approved["evidence_sha256"]
            or review.get("kind") != "ops_native_signer_continuation_review"
            or review.get("approved") is not True
            or review.get("additional_identity_reads") != 1
            or any(review.get(k) != approved[k] for k in ("scope", "session", "account", "authority"))
            or any(approved[k] != active[k] for k in ("scope", "session", "account"))
            or approved["grant_id"] != extension["id"]
            or review.get("grant_id") != extension["id"]
            or extension["kind"] != "identity"
            or extension["quota"] != 1
            or extension["parent"] is not None
            or extension["authority"] != approved["authority"]
            or json.loads(extension["response_path"] or "null") is not None
            or extension["target_param"] is not None
            or extension["target_encoding"] is not None
            or read_policy(extension)
        ):
            raise GateDeniedError()
        original = connection.execute(
            "SELECT * FROM grants WHERE scope=? AND id='identity'",
            (active["scope"],),
        ).fetchone()
        if (
            not original
            or not nas_native_binding(original)
            or original["authority"] != approved["authority"]
            or json.loads(extension["call"]) != json.loads(original["call"])
            or review.get("identity_call") != json.loads(original["call"])
        ):
            raise GateDeniedError()
        preserved = review["preserved_grants"]
        grants = connection.execute(
            "SELECT * FROM grants WHERE scope=? AND id<>? ORDER BY id",
            (active["scope"], extension["id"]),
        ).fetchall()
        if not isinstance(preserved, list) or len(preserved) != len(grants):
            raise GateDeniedError()
        for grant, proof in zip(grants, preserved):
            if not isinstance(proof, dict):
                raise GateDeniedError()
            proof = cast(dict[str, Any], proof)
            definition = {k: v for k, v in dict(grant).items() if k != "used"}
            limit = connection.execute(
                "SELECT used FROM authority_limits WHERE authority=? AND id=?",
                (grant["authority"], grant["id"]),
            ).fetchone()
            if (
                proof.get("id") != grant["id"]
                or proof.get("definition_sha256") != nas_native_snapshot(definition)
                or type(proof.get("used")) is not int
                or not 0 <= proof["used"] <= grant["used"] <= grant["quota"]
                or not limit
                or limit[0] != grant["used"]
                or grant["authority"] != approved["authority"]
            ):
                raise GateDeniedError()
        proofs = review["preserved_attempts"]
        if not isinstance(proofs, list) or not proofs:
            raise GateDeniedError()
        snapshots = {p["id"]: p["sha256"] for p in proofs}
        if len(snapshots) != len(proofs):
            raise GateDeniedError()
        root = None
        preserved_rows = []
        for ident, digest in snapshots.items():
            row = connection.execute(
                "SELECT * FROM attempts WHERE scope=? AND id=?",
                (active["scope"], ident),
            ).fetchone()
            if not row or nas_native_snapshot(row) != digest or row["session"] != active["session"]:
                raise GateDeniedError()
            preserved_rows.append(row)
            if ident == review["original_identity_attempt"]:
                root = row
        if (
            not root
            or root["grant_id"] != "identity"
            or root["state"] != "succeeded"
            or not root["credential"]
            or not root["reference_scope"]
            or not root["tool_use_id"]
            or not nas_native_success(json.loads(original["call"]), json.loads(root["response"]), "identity")
            or json.loads(root["response"]).get("call") != json.loads(original["call"])
            or json.loads(root["response"])["body"].get("AccountId") != active["account"]
            or not connection.execute(
                "SELECT 1 FROM identities WHERE scope=? AND session=? AND credential=? AND account=?",
                (active["scope"], active["session"], root["credential"], active["account"]),
            ).fetchone()
            or any(
                r["credential"] != root["credential"]
                or r["reference_scope"] != root["reference_scope"]
                or r["created"] < root["created"]
                for r in preserved_rows
            )
        ):
            raise GateDeniedError()
        signed = connection.execute(
            "SELECT * FROM attempts WHERE scope=? AND grant_id=?",
            (active["scope"], extension["id"]),
        ).fetchall()
        limit = connection.execute(
            "SELECT used FROM authority_limits WHERE authority=? AND id=?",
            (extension["authority"], extension["id"]),
        ).fetchone()
        if not limit or extension["used"] != limit[0] or extension["used"] != len(signed) or len(signed) > 1:
            raise GateDeniedError()
        all_rows = connection.execute("SELECT * FROM attempts WHERE scope=?", (active["scope"],)).fetchall()
        # Before renewal, the reviewed snapshot must cover the entire old generation.
        if not signed:
            if len(all_rows) != len(snapshots):
                raise GateDeniedError()
            return extension, root, snapshots
        renewed = signed[0]
        if (
            renewed["session"] != active["session"]
            or not renewed["tool_use_id"]
            or not renewed["reference_scope"]
            or renewed["created"] <= max(r["created"] for r in preserved_rows)
            or renewed["credential"] == root["credential"]
            or renewed["reference_scope"] == root["reference_scope"]
        ):
            raise GateDeniedError()
        # No unreviewed old-signature row or pre-renewal operation may join the chain.
        if any(
            r["id"] not in snapshots
            and r["id"] != renewed["id"]
            and (
                r["session"] != renewed["session"]
                or r["credential"] != renewed["credential"]
                or r["reference_scope"] != renewed["reference_scope"]
                or r["created"] <= renewed["created"]
            )
            for r in all_rows
        ):
            raise GateDeniedError()
        envelope = json.loads(renewed["response"] or "null")
        if (
            renewed["state"] != "succeeded"
            or not nas_native_success(json.loads(extension["call"]), envelope, "identity")
            or envelope["call"] != json.loads(extension["call"])
            or envelope["body"].get("AccountId") != active["account"]
            or not connection.execute(
                "SELECT 1 FROM identities WHERE scope=? AND session=? AND credential=? AND account=?",
                (active["scope"], active["session"], renewed["credential"], active["account"]),
            ).fetchone()
        ):
            raise GateDeniedError()
        return extension, renewed, snapshots
    except (OSError, KeyError, TypeError, ValueError, IndexError, AttributeError, sqlite3.Error) as error:
        raise GateDeniedError() from error


def nas_native_row_bound(connection: sqlite3.Connection, active: Any, row: Any, signer: Any) -> bool:
    if not row or row["session"] != active["session"] or not row["tool_use_id"] or not row["reference_scope"]:
        return False
    if (
        row["credential"] == signer["credential"]
        and row["reference_scope"] == signer["reference_scope"]
        and row["created"] >= signer["created"]
    ):
        return True
    # The signer carries snapshots validated in this same locked transaction.
    # Do not reread/revalidate the entire journal at every recursive dependency.
    return dict(signer).get("_preserved_attempts", {}).get(row["id"]) == nas_native_snapshot(row)


def nas_native_attempt(
    connection: sqlite3.Connection,
    active: Any,
    ident: str,
    signer: Any,
    *,
    allow_error: bool = False,
) -> tuple[Any, dict[str, Any], dict[str, Any]]:
    grant = connection.execute(
        "SELECT * FROM grants WHERE scope=? AND id=?",
        (active["scope"], ident),
    ).fetchone()
    row = connection.execute(
        "SELECT * FROM attempts WHERE scope=? AND grant_id=? ORDER BY created DESC,rowid DESC LIMIT 1",
        (active["scope"], ident),
    ).fetchone()
    if (
        not grant
        or not row
        or not nas_native_binding(grant)
        or not nas_native_row_bound(connection, active, row, signer)
        or (row["state"] != "succeeded" and not allow_error)
    ):
        raise GateDeniedError()
    response = json.loads(row["response"] or "null")
    call = nas_native_resolve(connection, active, grant, signer, prerequisites=False)
    if not isinstance(response, dict) or response.get("call") != call:
        raise GateDeniedError()
    if not allow_error and not nas_native_success(call, response, grant["kind"]):
        raise GateDeniedError()
    return row, call, response


def nas_native_ready(ident: str, call: dict[str, Any], response: dict[str, Any]) -> bool:
    try:
        return _nas_native_ready(ident, call, response)
    except (KeyError, TypeError, ValueError, AttributeError):
        return False


def _nas_native_ready(ident: str, call: dict[str, Any], response: dict[str, Any]) -> bool:
    """Only documented exact same-source readiness; never HTTP200 alone."""
    body, params = response.get("body"), call["params"]
    if ident == "precheck-bucket-absent" or ident == "delete-bucket-absence":
        from iac_code.tools.cloud.aliyun.acs3_transport import NormalizedApiResponse
        from iac_code.tools.cloud.aliyun.oss_v4_adapter import oss_bucket_not_found

        status = response.get("status")
        if type(status) is not int:
            return False
        return oss_bucket_not_found(
            NormalizedApiResponse(
                status=status,
                headers=response.get("headers", {}),
                body=body,
                content_type=None,
                content_encoding=None,
                size=0,
            ),
            params["bucket"],
        )
    if ident == "delete-export-absence" and response.get("status") == 404:
        return isinstance(body, dict) and body.get("Code") == "Resource.NotFound" and bool(body.get("RequestId"))
    if ident == "delete-filesystem-absence" and response.get("status") == 404:
        # The exact post-delete DescribeFileSystems can return a signed 404,
        # not an HTTP200 empty collection. Keep that original negative read
        # unknown in the journal; it only satisfies this own cleanup edge.
        request_id = body.get("RequestId") if isinstance(body, dict) else None
        return bool(
            isinstance(body, dict)
            and call.get("product") == "nas"
            and call.get("action") == "DescribeFileSystems"
            and call.get("version") == "2017-06-26"
            and params.get("FileSystemId")
            and set(params) == {"FileSystemId", "PageNumber", "PageSize"}
            and params["PageNumber"] == 1
            and params["PageSize"] == 10
            and response.get("call") == call
            and isinstance(request_id, str)
            and 0 < len(request_id) <= 128
            and not any(c.isspace() for c in request_id)
            and body.get("Code") == "InvalidFileSystem.NotFound"
            and body.get("statusCode", 404) == 404
            and body.get("HostId") == "nas." + call["region_id"] + ".aliyuncs.com"
            and response.get("headers", {}).get("x-acs-request-id") == request_id
        )
    if not nas_native_success(call, response, "read") or not isinstance(body, dict):
        return False
    if ident.startswith("precheck-"):
        return nas_native_success(call, response, "precheck")
    if ident == "read-bucket-empty":
        value = body
        return (
            value.get("name") == params["bucket"]
            and value.get("is_truncated") is False
            and type(value.get("key_count")) is int
            and value["key_count"] == 0
            and not value.get("contents")
            and not value.get("common_prefixes")
            and not value.get("next_continuation_token")
        )
    if ident == "read-bucket":
        value = body.get("bucket_info", {})
        return (
            value.get("name") == params["bucket"]
            and value.get("location") == "oss-cn-hangzhou"
            and value.get("storage_class") == "Standard"
            and value.get("data_redundancy_type") == "LRS"
            and value.get("acl") == "private"
        )
    collections = {
        "DescribeVpcs": (("Vpcs", "Vpc"), "VpcId"),
        "DescribeVSwitches": (("VSwitches", "VSwitch"), "VSwitchId"),
        "DescribeFileSystems": (("FileSystems", "FileSystem"), "FileSystemId"),
        "DescribeFilesets": (("Entries", "Entrie"), "FsetId"),
        "DescribeProtocolService": (("ProtocolServices",), "ProtocolServiceId"),
        "DescribeDataFlows": (("DataFlowInfo", "DataFlow"), "DataFlowId"),
        "DescribeSecurityGroups": (("SecurityGroups", "SecurityGroup"), "SecurityGroupId"),
        "DescribeNetworkInterfaces": (("NetworkInterfaceSets", "NetworkInterfaceSet"), "NetworkInterfaceId"),
    }
    if call["action"] == "GetProtocolMountTarget":
        return _nas_native_export_ready(call, response)
    contract = collections.get(call["action"])
    if not contract:
        return False
    keys, key = contract
    rows: Any = body
    for part in keys:
        rows = rows.get(part) if isinstance(rows, dict) else None
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows) or body.get("NextToken"):
        return False
    if "PageNumber" in params:
        if (
            type(body.get("TotalCount")) is not int
            or body["TotalCount"] != len(rows)
            or body.get("PageNumber") != 1
            or body.get("PageSize") != params["PageSize"]
        ):
            return False
    if ident.endswith("absence") or ident in {"read-filesystem-implicit-members", "read-export-implicit-members"}:
        return rows == []
    if len(rows) != 1:
        return False
    value = rows[0]
    if key in params and value.get(key) != params[key]:
        return False
    if call["action"] == "DescribeVpcs":
        return value.get("Status") == "Available" and value.get("CidrBlock") in {"172.28.0.0/16", "172.29.0.0/16"}
    if call["action"] == "DescribeVSwitches":
        return (
            value.get("Status") == "Available"
            and value.get("ZoneId") == "cn-hangzhou-k"
            and type(value.get("AvailableIpAddressCount")) is int
            and value["AvailableIpAddressCount"] > 0
        )
    if call["action"] == "DescribeFileSystems":
        version = value.get("Version")
        return (
            value.get("Status") == "Running"
            and value.get("FileSystemType") == "cpfs"
            and value.get("StorageType") == "advance_200"
            and value.get("Capacity") == 3600
            and value.get("Bandwidth") == 720
            and value.get("ZoneId") == "cn-hangzhou-k"
            and value.get("ChargeType") == "PayAsYouGo"
            and isinstance(version, str)
            and re.fullmatch(r"2\.[0-9]+\.[0-9]+", version) is not None
            and tuple(map(int, version.split("."))) >= (2, 3, 4)
            and value.get("MountTargets", {}).get("MountTarget") == []
        )
    if call["action"] == "DescribeFilesets":
        return value.get("Status") == "CREATED" and value.get("FileSystemId") == params["FileSystemId"]
    if call["action"] == "DescribeProtocolService":
        return (
            value.get("Status") == "Running"
            and value.get("FileSystemId") == params["FileSystemId"]
            and value.get("ProtocolServiceId") == params["ProtocolServiceIds"]
            and value.get("ProtocolSpec") == "General"
            and value.get("ProtocolType") == "NFS"
        )
    if ident == "query-dataflow-source":
        return (
            value.get("Status") == "Running"
            and not value.get("ErrorMessage")
            and value.get("FileSystemId") == params["FileSystemId"]
            and value.get("SourceSecurityType") == "SSL"
            and value.get("Throughput") == 600
            and value.get("AutoRefreshPolicy") == "None"
        )
    return False


def _nas_native_export_ready(
    call: dict[str, Any],
    response: dict[str, Any],
    *,
    parent_nfs_verified: bool = False,
) -> bool:
    body = response.get("body")
    if not nas_native_success(call, response, "read") or not isinstance(body, dict):
        return False
    value = body.get("ProtocolMountTarget")
    if not isinstance(value, dict):
        return False
    protocol = value.get("ProtocolType", "")
    return (
        not body.get("NextToken")
        and value.get("ExportId") == call["params"]["ExportId"]
        and value.get("Status") == "AVAILABLE"
        and (protocol == "NFS" or (protocol == "" and parent_nfs_verified))
        and value.get("AccessGroupName") == "DEFAULT_VPC_GROUP_NAME"
        and isinstance(value.get("FsetId"), str)
        and isinstance(value.get("Path"), str)
    )


def _nas_native_export_parent_ready(
    connection: sqlite3.Connection,
    active: Any,
    signer: Any,
    call: dict[str, Any],
    response: dict[str, Any],
) -> bool:
    """An omitted export protocol is usable only through the signed own NFS graph.

    Keep the raw response unchanged. This context-free readiness exception is
    never exposed: every parent and the export must retain their signed creation
    lineage, health and observation order in the same locked journal.
    """
    observed, original, raw = nas_native_attempt(connection, active, "read-export-ready", signer)
    created, _, _ = nas_native_attempt(connection, active, "create-export", signer)
    if original != call or raw != response or observed["created"] <= created["created"]:
        return False
    for read_id, create_id in (
        ("read-filesystem-ready", "create-filesystem"),
        ("read-fileset-ready", "create-fileset"),
        ("read-protocol-ready", "create-protocol"),
    ):
        parent, parent_call, parent_raw = nas_native_attempt(connection, active, read_id, signer)
        creation, _, _ = nas_native_attempt(connection, active, create_id, signer)
        if parent["created"] <= creation["created"] or not nas_native_health(
            connection, active, read_id, signer, parent_call, parent_raw
        ):
            return False
        if read_id == "read-protocol-ready" and (
            parent_call["params"]["FileSystemId"] != call["params"]["FileSystemId"]
            or parent_call["params"]["ProtocolServiceIds"] != call["params"]["ProtocolServiceId"]
        ):
            return False
    return _nas_native_export_ready(call, response, parent_nfs_verified=True)


def nas_native_health(
    connection: sqlite3.Connection,
    active: Any,
    ident: str,
    signer: Any,
    call: dict[str, Any],
    response: dict[str, Any],
) -> bool:
    try:
        return _nas_native_health(connection, active, ident, signer, call, response)
    except (KeyError, TypeError, ValueError, AttributeError):
        return False


def _nas_native_health(
    connection: sqlite3.Connection,
    active: Any,
    ident: str,
    signer: Any,
    call: dict[str, Any],
    response: dict[str, Any],
) -> bool:
    value = response.get("body", {}).get("ProtocolMountTarget")
    if (
        ident == "read-export-ready"
        and call["action"] == "GetProtocolMountTarget"
        and isinstance(value, dict)
        and value.get("ProtocolType", "") == ""
    ):
        if not _nas_native_export_parent_ready(connection, active, signer, call, response):
            return False
    elif not nas_native_ready(ident, call, response):
        return False
    body = response["body"]
    name = json.loads(
        connection.execute(
            "SELECT call FROM grants WHERE scope=? AND id='create-bucket'",
            (active["scope"],),
        ).fetchone()[0]
    )["params"]["bucket"]

    def own(parent: str) -> str | None:
        _, original, raw = nas_native_attempt(connection, active, parent, signer)
        return nas_native_identifier(original, raw)

    if ident in {"read-filesystem-vpc", "read-export-vpc"}:
        side = "filesystem" if ident == "read-filesystem-vpc" else "export"
        value = body["Vpcs"]["Vpc"][0]
        return value.get("VpcId") == own("create-" + side + "-vpc") and value.get("CidrBlock") == (
            "172.28.0.0/16" if side == "filesystem" else "172.29.0.0/16"
        )
    if ident in {"read-filesystem-vswitch", "read-export-vswitch"}:
        side = "filesystem" if ident == "read-filesystem-vswitch" else "export"
        value = body["VSwitches"]["VSwitch"][0]
        return value.get("VpcId") == own("create-" + side + "-vpc") and value.get("CidrBlock") == (
            "172.28.0.0/24" if side == "filesystem" else "172.29.0.0/24"
        )
    if ident == "read-filesystem-ready":
        value = body["FileSystems"]["FileSystem"][0]
        return (
            value.get("VpcId") == own("create-filesystem-vpc")
            and value.get("RegionId") == "cn-hangzhou"
            and value.get("VswIds", {}).get("VswId") == [own("create-filesystem-vswitch")]
        )
    if ident == "read-fileset-ready":
        value = body["Entries"]["Entrie"][0]
        return (
            value.get("FsetId") == own("create-fileset")
            and value.get("FileSystemPath") == "/" + name + "/"
            and value.get("Description") == name
            and value.get("DeletionProtection") is False
        )
    if ident == "read-protocol-ready":
        value = body["ProtocolServices"][0]
        return (
            value.get("VpcId") == own("create-filesystem-vpc")
            and value.get("VSwitchId") == own("create-filesystem-vswitch")
            and value.get("Description") == name
        )
    if ident == "read-export-ready":
        value = body["ProtocolMountTarget"]
        return (
            value.get("ExportId") == own("create-export")
            and call["params"]["FileSystemId"] == own("create-filesystem")
            and call["params"]["ProtocolServiceId"] == own("create-protocol")
            and ("FileSystemId" not in value or value["FileSystemId"] == call["params"]["FileSystemId"])
            and ("ProtocolServiceId" not in value or value["ProtocolServiceId"] == call["params"]["ProtocolServiceId"])
            and value.get("FsetId") == own("create-fileset")
            and value.get("Path") == "/" + name + "/"
            and value.get("VpcId") == own("create-export-vpc")
            and value.get("VSwitchId") == own("create-export-vswitch")
            and value.get("Description") == name
        )
    if ident == "read-bucket":
        value = body.get("bucket_info", {})
        return value.get("owner", {}).get("id") == active["account"]
    if ident == "query-dataflow-source":
        value = body["DataFlowInfo"]["DataFlow"][0]
        bucket = own("create-bucket")
        return (
            isinstance(bucket, str)
            and value.get("DataFlowId") == own("create-dataflow")
            and value.get("FileSystemId") == own("create-filesystem")
            and value.get("FsetId") == own("create-fileset")
            and value.get("FileSystemPath") == "/" + name + "/"
            and value.get("Description") == name
            and value.get("SourceStorage") == "oss://" + bucket
        )
    return True


def require_nas_native_cleanup(connection: sqlite3.Connection, active: Any, grant: Any, signer: Any) -> None:
    # Partial creation failure can clean already-created parents. An unknown
    # child remains unresolved; never interpret a missing successful row as absence.
    children = {
        "create-export": ["create-dataflow"],
        "create-protocol": ["create-export"],
        "create-fileset": ["create-dataflow", "create-export"],
        "create-filesystem": ["create-dataflow", "create-export", "create-protocol", "create-fileset"],
        "create-bucket": ["create-dataflow"],
        "create-filesystem-vswitch": ["create-filesystem"],
        "create-export-vswitch": ["create-export", "create-protocol"],
        "create-filesystem-vpc": ["create-filesystem", "create-protocol", "create-filesystem-vswitch"],
        "create-export-vpc": ["create-export", "create-protocol", "create-export-vswitch"],
    }
    for child in children.get(grant["parent"], []):
        exists = connection.execute(
            "SELECT 1 FROM attempts WHERE scope=? AND grant_id=?",
            (active["scope"], child),
        ).fetchone()
        if not exists:
            continue
        nas_native_attempt(connection, active, child, signer)
        deletion = child.replace("create-", "delete-", 1)
        deleted, _, _ = nas_native_attempt(connection, active, deletion, signer)
        observed, call, raw = nas_native_attempt(
            connection,
            active,
            deletion + "-absence",
            signer,
            allow_error=True,
        )
        if observed["created"] <= deleted["created"] or not nas_native_ready(deletion + "-absence", call, raw):
            raise GateDeniedError()
    if grant["parent"] in {
        "create-filesystem-vswitch",
        "create-export-vswitch",
        "create-filesystem-vpc",
        "create-export-vpc",
    }:
        side = "filesystem" if "filesystem" in grant["parent"] else "export"
        for suffix in ("implicit-sg-absence", "eni-absence"):
            # Even the baseline network has no task-created compute. Require
            # complete, signed empty inventory before removing the own network.
            ident = "read-" + side + "-" + suffix
            _, call, raw = nas_native_attempt(connection, active, ident, signer)
            if not nas_native_ready(ident, call, raw):
                raise GateDeniedError()


def nas_native_resolve(
    connection: sqlite3.Connection,
    active: Any,
    grant: Any,
    signer: Any,
    *,
    prerequisites: bool = True,
) -> dict[str, Any]:
    binding = nas_native_binding(grant)
    if not binding or binding["role"] != grant["id"]:
        raise GateDeniedError()
    call = json.loads(grant["call"])
    for parameter, parent_id in binding["bindings"].items():
        _, parent_call, response = nas_native_attempt(connection, active, parent_id, signer)
        identifier = nas_native_identifier(parent_call, response)
        if identifier is None:
            raise GateDeniedError()
        call["params"][parameter] = "oss://" + identifier if parameter == "SourceStorage" else identifier
    if grant["parent"]:
        nas_native_attempt(connection, active, grant["parent"], signer)
    if prerequisites:
        for ident in binding["requires"]:
            _, original, response = nas_native_attempt(
                connection,
                active,
                ident,
                signer,
                allow_error=ident == "precheck-bucket-absent",
            )
            if not nas_native_health(connection, active, ident, signer, original, response):
                raise GateDeniedError()
        if grant["kind"] == "delete":
            require_nas_native_cleanup(connection, active, grant, signer)
        policy = read_policy(grant)
        if policy.get("after_delete"):
            deletion = connection.execute(
                "SELECT g.id FROM grants g WHERE g.scope=? AND g.kind='delete' AND g.parent=?",
                (active["scope"], policy["after_delete"]),
            ).fetchone()
            created = connection.execute(
                "SELECT 1 FROM attempts WHERE scope=? AND grant_id=?",
                (active["scope"], policy["after_delete"]),
            ).fetchone()
            if not deletion:
                raise GateDeniedError()
            if created:
                nas_native_attempt(connection, active, deletion["id"], signer)
    return call


def nas_native_signer(connection: sqlite3.Connection, active: Any) -> Any:
    continuation = nas_native_continuation(connection, active)
    if continuation:
        return {**dict(continuation[1]), "_preserved_attempts": continuation[2]}
    row = connection.execute(
        "SELECT a.* FROM attempts a JOIN identities i ON i.scope=a.scope AND i.session=a.session "
        "AND i.credential=a.credential WHERE a.scope=? AND a.grant_id='identity' AND a.session=? "
        "AND i.account=? AND a.state='succeeded' ORDER BY a.created DESC,a.rowid DESC LIMIT 1",
        (active["scope"], active["session"], active["account"]),
    ).fetchone()
    if not row or not row["tool_use_id"] or not row["reference_scope"]:
        raise GateDeniedError()
    return row


def nas_native_navigation_source(path: Path, scope: str, session: str, account: str) -> dict[str, str]:
    """Read original signed own-source proof; never create a reference or a verdict."""
    with database(path) as connection:
        active = connection.execute(
            "SELECT * FROM active WHERE scope=? AND session=? AND account=? AND phase='execute'",
            (scope, session, account),
        ).fetchall()
        if (
            len(active) != 1
            or not nas_native_scope(connection, scope)
            or connection.execute(
                "SELECT 1 FROM attempts a JOIN grants g ON a.scope=g.scope AND a.grant_id=g.id "
                "WHERE a.scope=? AND g.kind='delete'",
                (scope,),
            ).fetchone()
        ):
            raise GateDeniedError()
        active = active[0]
        signer = nas_native_signer(connection, active)
        created, create_call, original = nas_native_attempt(connection, active, "create-dataflow", signer)
        observed, call, response = nas_native_attempt(connection, active, "query-dataflow-source", signer)
        if (
            observed["created"] <= created["created"]
            or observed["created"] < signer["created"]
            or observed["credential"] != signer["credential"]
            or observed["reference_scope"] != signer["reference_scope"]
            or not nas_native_health(
                connection,
                active,
                "query-dataflow-source",
                signer,
                call,
                response,
            )
        ):
            raise GateDeniedError()
        for ident in (
            "read-filesystem-ready",
            "read-fileset-ready",
            "read-protocol-ready",
            "read-export-ready",
            "read-bucket",
            "read-bucket-empty",
        ):
            _, dependency_call, dependency = nas_native_attempt(connection, active, ident, signer)
            if not nas_native_health(connection, active, ident, signer, dependency_call, dependency):
                raise GateDeniedError()
        identifier = nas_native_identifier(create_call, original)
        if identifier is None:
            raise GateDeniedError()
        return {
            "identifier": identifier,
            "region": call["region_id"],
            "reference_scope": observed["reference_scope"],
            "tool_use_id": observed["tool_use_id"],
        }


def nas_native_network_closed(connection: sqlite3.Connection, active: Any, side: str, signer: Any) -> bool:
    """Resolve missing derived inventory from the original network's real cleanup.

    This exports no resource ID and changes no call outcome. A missing post-create
    snapshot is no longer an outstanding cleanup risk once both own network
    resources and their NAS dependencies are deleted and independently absent.
    """
    try:
        for target in ("vswitch", "vpc"):
            ident = "delete-" + side + "-" + target
            grant = connection.execute(
                "SELECT * FROM grants WHERE scope=? AND id=?",
                (active["scope"], ident),
            ).fetchone()
            if not grant:
                return False
            require_nas_native_cleanup(connection, active, grant, signer)
            creation, _, _ = nas_native_attempt(connection, active, grant["parent"], signer)
            deleted, _, _ = nas_native_attempt(connection, active, ident, signer)
            observed, call, raw = nas_native_attempt(connection, active, ident + "-absence", signer)
            if not creation["created"] < deleted["created"] < observed["created"] or not nas_native_ready(
                ident + "-absence", call, raw
            ):
                return False
            for suffix in ("implicit-sg-absence", "eni-absence"):
                inventory, _, _ = nas_native_attempt(connection, active, "read-" + side + "-" + suffix, signer)
                if not creation["created"] < inventory["created"] < deleted["created"]:
                    return False
        return True
    except (GateDeniedError, KeyError, TypeError, ValueError, AttributeError):
        return False


def nas_native_owners(connection: sqlite3.Connection, row: Any, root: dict[str, Any]) -> list[dict[str, Any]]:
    """Export implicit SGs from signed own-VPC inventory, or retain cleanup risk.

    The same VPC must have a complete empty pre-create baseline. No SG mutation
    is granted. Missing/malformed inventory remains unresolved until real signed
    cleanup proves the own network and dependencies gone; it is not a cloud write.
    """
    if row["grant_id"] != "create-filesystem" or row["state"] != "succeeded":
        return []
    # creations() joins the grant's call for export. Continuation snapshots bind
    # only the original attempt row, not those extra SELECT columns.
    row = connection.execute("SELECT * FROM attempts WHERE id=? AND scope=?", (row["id"], row["scope"])).fetchone()
    active = {"scope": row["scope"], "session": row["session"], "account": root["resource"]["account"]}
    records = []
    try:
        signer = nas_native_signer(connection, active)
    except GateDeniedError:
        signer = None
    for side in ("filesystem", "export"):
        children: list[dict[str, Any]] | None = None
        proof = None
        try:
            if signer is None or not nas_native_row_bound(connection, active, row, signer):
                raise GateDeniedError()
            _, call, vpc = nas_native_attempt(connection, active, "create-" + side + "-vpc", signer)
            vpc_id = nas_native_identifier(call, vpc)
            ident = "read-" + side + "-implicit-members"
            snapshots = connection.execute(
                "SELECT * FROM attempts WHERE scope=? AND grant_id=? ORDER BY created,rowid",
                (row["scope"], ident),
            ).fetchall()
            if len(snapshots) != 2:
                raise GateDeniedError()
            proof, expected, post = nas_native_attempt(connection, active, ident, signer)
            latest_creation = connection.execute(
                "SELECT MAX(a.created) FROM attempts a JOIN grants g ON g.scope=a.scope AND g.id=a.grant_id "
                "WHERE a.scope=? AND g.kind='create' AND g.call LIKE ?",
                (row["scope"], '%"product":"nas"%'),
            ).fetchone()[0]
            baseline = snapshots[0]
            first_use = (
                row
                if side == "filesystem"
                else nas_native_attempt(
                    connection,
                    active,
                    "create-export",
                    signer,
                )[0]
            )
            raw = json.loads(baseline["response"] or "null")
            if (
                baseline["state"] != "succeeded"
                or baseline["created"] >= first_use["created"]
                or baseline["session"] != row["session"]
                or baseline["credential"] != row["credential"]
                or baseline["reference_scope"] != row["reference_scope"]
                or not baseline["tool_use_id"]
                or not isinstance(raw, dict)
                or raw.get("call") != expected
                or expected["params"].get("VpcId") != vpc_id
                or not nas_native_ready(ident, raw["call"], raw)
            ):
                raise GateDeniedError()
            call, raw = expected, post
            if proof["created"] <= (latest_creation or row["created"]) or not nas_native_success(call, raw, "read"):
                raise GateDeniedError()
            body = raw["body"]
            values = body.get("SecurityGroups", {}).get("SecurityGroup")
            if (
                not isinstance(values, list)
                or type(body.get("TotalCount")) is not int
                or body["TotalCount"] != len(values)
                or body.get("NextToken")
                or body.get("PageNumber") != 1
                or body.get("PageSize") != 100
            ):
                raise GateDeniedError()
            seen = set()
            children = []
            for value in values:
                identifier = value.get("SecurityGroupId") if isinstance(value, dict) else None
                if (
                    not isinstance(identifier, str)
                    or re.fullmatch(r"sg-[A-Za-z0-9]+", identifier) is None
                    or value.get("VpcId") != vpc_id
                    or identifier in seen
                ):
                    raise GateDeniedError()
                seen.add(identifier)
                children.append(
                    {
                        "id": identifier,
                        "vpc_id": vpc_id,
                        "product": "ecs",
                        "action": "DescribeSecurityGroups",
                        "resource_type": "ALIYUN::ECS::SecurityGroup",
                    }
                )
        except (GateDeniedError, KeyError, TypeError, ValueError, AttributeError):
            children = None
        if (
            children is None
            and signer is not None
            and nas_native_row_bound(connection, active, row, signer)
            and nas_native_network_closed(connection, active, side, signer)
        ):
            children = []
        for child in (
            children
            if children is not None
            else [
                {
                    "id": None,
                    "product": "ecs",
                    "resource_type": "unresolved_native_network_members",
                }
            ]
        ):
            records.append(
                {
                    **{key: root[key] for key in ("job_id", "dimension", "session_id", "reference_scope")},
                    "attempt_id": (proof["id"] if proof else row["id"]) + ":" + side + ":" + str(child["id"]),
                    "parent_attempt_id": row["id"],
                    "tool_use_id": proof["tool_use_id"] if proof else None,
                    "state": "succeeded" if children is not None else "unknown",
                    "response": json.loads(proof["response"]) if proof else None,
                    "resource": {**root["resource"], **child},
                }
            )
    return records


def is_ons_instance_list_read(call: dict[str, Any]) -> bool:
    """The documented ONS read is not named List/Get; never admit arbitrary Ons* APIs."""
    params = call.get("params", {})
    return (
        (call.get("product"), call.get("action"), call.get("version"))
        == ("ons", "OnsInstanceInServiceList", "2019-02-14")
        and isinstance(params, dict)
        and set(params) <= {"RegionId", "NeedResourceInfo"}
        and params.get("RegionId") == call.get("region_id")
        and isinstance(call.get("region_id"), str)
        and call["region_id"].startswith("cn-")
        and ("NeedResourceInfo" not in params or type(params["NeedResourceInfo"]) is bool)
    )


def validate_grants(grants: list[dict[str, Any]], *, require_read_policy: bool = False) -> None:
    if not isinstance(grants, list) or not grants or len(grants) > 100:
        raise GateDeniedError()
    ids = [grant.get("id") for grant in grants]
    if len(set(ids)) != len(ids) or any(not isinstance(key, str) or not key for key in ids):
        raise GateDeniedError()
    if validate_nas_native_plan(grants):
        return
    by_id = {grant["id"]: grant for grant in grants}
    for grant in grants:
        call = api_call(grant["call"])
        kind, quota = grant.get("kind"), grant.get("quota")
        if kind not in {"identity", "read", "create", "delete"} or type(quota) is not int:
            raise GateDeniedError()
        if not 1 <= quota <= (100 if kind in {"identity", "read"} else 1):
            raise GateDeniedError()
        policy = grant.get("read_policy")
        if require_read_policy and kind == "read" and policy is None:
            raise GateDeniedError()
        if policy is not None:
            if (
                kind != "read"
                or not isinstance(policy, dict)
                or set(policy) - {"phase", "min_interval_seconds", "required_cleanup", "after_delete"}
                or policy.get("phase") not in {"execute", "cleanup"}
                or type(policy.get("min_interval_seconds", 0)) is not int
                or not 0 <= policy.get("min_interval_seconds", 0) <= 3600
                or type(policy.get("required_cleanup", False)) is not bool
            ):
                raise GateDeniedError()
            if policy.get("required_cleanup") or "after_delete" in policy:
                if not isinstance(policy.get("after_delete"), str):
                    raise GateDeniedError()
                parent = by_id.get(policy.get("after_delete"), {})
                if (
                    policy["phase"] != "cleanup"
                    or parent.get("kind") != "create"
                    or (grant.get("parent") and grant["parent"] != policy["after_delete"])
                ):
                    raise GateDeniedError()
            if require_read_policy and call["action"] == "GetStack" and policy.get("min_interval_seconds", 0) < 30:
                raise GateDeniedError()
        action = call["action"]
        if kind == "identity" and (call["product"], action, call["params"]) != ("sts", "GetCallerIdentity", {}):
            raise GateDeniedError()
        if call["product"] == "mse" and action == "QueryClusterInfo":
            validate_mse_read(grant, call)
        elif kind == "read" and not (
            action.startswith(("Get", "List", "Describe", "Validate")) or is_ons_instance_list_read(call)
        ):
            raise GateDeniedError()
        public_key_import = (
            call["product"] == "ecs"
            and action == "ImportKeyPair"
            and call.get("version", "2014-05-26") == "2014-05-26"
            and all(
                isinstance(call["params"].get(key), str) and call["params"][key]
                for key in ("KeyPairName", "PublicKeyBody")
            )
        )
        if kind == "create" and not (action.startswith("Create") or public_key_import or is_oss_bucket_create(call)):
            raise GateDeniedError()
        if "target_encoding" in grant:
            parent = by_id.get(grant.get("parent"), {})
            if not (
                grant["target_encoding"] == "json-singleton"
                and kind == "delete"
                and call["product"] == "ecs"
                and action == "DeleteKeyPairs"
                and call.get("version", "2014-05-26") == "2014-05-26"
                and grant.get("target_param") == "KeyPairNames"
                and grant.get("response_path") == ["KeyPairName"]
                and parent.get("call", {}).get("action") in {"ImportKeyPair", "CreateKeyPair"}
            ):
                raise GateDeniedError()
        typed_binding = isinstance(grant.get("response_path"), dict)
        if typed_binding:
            if grant["response_path"].get("kind") == "mse_managed_child":
                validate_mse_managed_binding(grant, by_id)
            elif grant["response_path"].get("kind") == "nas_managed_child":
                validate_nas_managed_binding(grant, by_id)
            else:
                validate_resource_group_binding(grant, by_id)
        if kind == "delete" or grant.get("parent"):
            parent = by_id.get(grant.get("parent"), {})
            if is_oss_bucket_create(api_call(parent.get("call", {}))) and (
                action not in {"GetBucketInfo", "DeleteBucket"}
                or (kind == "delete") != (action == "DeleteBucket")
                or grant.get("response_path") != ["headers", "location"]
                or grant.get("target_param") != "bucket"
                or "target_encoding" in grant
                or call.get("version", "2019-05-17") != "2019-05-17"
            ):
                raise GateDeniedError()
            parameter = grant.get("target_param")
            if (
                (kind == "delete" and not action.startswith("Delete"))
                or kind not in {"read", "delete"}
                or parent.get("kind") != "create"
                or (
                    not typed_binding
                    and (
                        not isinstance(grant.get("response_path"), list)
                        or not grant["response_path"]
                        or any(not isinstance(key, str) or not key for key in grant["response_path"])
                    )
                )
                or not isinstance(parameter, str)
                or call["params"].get(parameter) != "$created_id"
                or call["region_id"] != parent["call"]["region_id"]
                or (
                    call["product"] != parent["call"]["product"]
                    and not (
                        typed_binding
                        and grant["response_path"].get("kind")
                        in {
                            "resource_group_child",
                            "nas_managed_child",
                            "mse_managed_child",
                        }
                    )
                )
            ):
                raise GateDeniedError()
        elif "$created_id" in canonical(call):
            raise GateDeniedError()
    for grant in grants:
        call = api_call(grant["call"])
        if is_oss_bucket_create(call) and not any(
            entry["kind"] == "read"
            and entry["call"].get("product", "").casefold() == "oss"
            and entry["call"].get("action") == "GetBucketInfo"
            and entry["call"].get("version", "2019-05-17") == "2019-05-17"
            and entry["call"].get("params") == {"bucket": call["params"]["bucket"]}
            and entry["call"].get("region_id") == call["region_id"]
            and read_policy(entry).get("phase") == "execute"
            and not entry.get("parent")
            for entry in grants
        ):
            raise GateDeniedError()
    protected_parents = {
        grant["parent"]
        for grant in grants
        if isinstance(grant.get("response_path"), dict)
        and grant["response_path"].get("kind") not in {"nas_managed_child", "mse_managed_child"}
    }
    for parent_id in protected_parents:
        deletes = [grant for grant in grants if grant.get("parent") == parent_id and grant["kind"] == "delete"]
        if len(deletes) != 1 or not isinstance(deletes[0].get("response_path"), dict):
            raise GateDeniedError()
    # Every write has an exact delete recipe; this is not a successful-cleanup assertion.
    for grant in grants:
        if grant["kind"] == "create" and not any(
            g.get("parent") == grant["id"] and g["kind"] == "delete" for g in grants
        ):
            raise GateDeniedError()
        if (
            require_read_policy
            and grant["kind"] == "create"
            and not any(
                (entry.get("read_policy") or {}).get("required_cleanup") is True
                and entry["read_policy"].get("after_delete") == grant["id"]
                for entry in grants
            )
        ):
            raise GateDeniedError()
    for grant in grants:
        if (
            grant["kind"] == "create"
            and "alicloud_mse_cluster" in canonical(grant["call"])
            and not any(
                isinstance(g.get("response_path"), dict)
                and g["response_path"].get("kind") == "mse_managed_child"
                and g.get("parent") == grant["id"]
                for g in grants
            )
        ):
            raise GateDeniedError()
    if not any(grant["kind"] == "identity" for grant in grants):
        raise GateDeniedError()


def install(
    path: Path,
    slot: str,
    scope: str,
    session: str,
    account: str,
    expires: float | None,
    grants: list[dict[str, Any]],
    *,
    authority: str | None = None,
    existing_review: tuple[str, str] | None = None,
) -> None:
    validate_grants(grants)
    # None is task-scoped authorization, explicitly revoked on slot release.
    # Keep legacy time-scoped capabilities working without silently renewing them.
    if not session or not account or (expires is not None and not time.time() < expires <= time.time() + 3600):
        raise GateDeniedError()
    with database(path) as connection:
        old = connection.execute("SELECT * FROM active WHERE slot=?", (slot,)).fetchone()
        retained_readonly = False
        if existing_review is not None:
            file = Path(existing_review[0])
            root = path.resolve().parent
            if not file.resolve().is_relative_to(root / "inputs"):
                raise GateDeniedError()
            raw = file.read_bytes()
            proof = json.loads(raw)
            packet_file = root / proof["packet"]["path"]
            if not packet_file.resolve().is_relative_to(root / "inputs") or packet_file.is_symlink():
                raise GateDeniedError()
            packet_raw = packet_file.read_bytes()
            packet = json.loads(packet_raw)
            if (
                file.is_symlink()
                or (os.name != "nt" and file.stat().st_mode & 0o077)
                or hashlib.sha256(raw).hexdigest() != existing_review[1]
                or proof.get("kind") != "ops_existing_query_review"
                or proof.get("approved") is not True
                or proof.get("new_creation_quota") != 0
                or proof.get("pass_added") is not False
                or proof.get("account") != account
                or str(proof.get("slot")) != slot
                or session == proof.get("session_id")
                or authority != "existing-query:" + existing_review[1]
                or connection.execute("SELECT 1 FROM authority_limits WHERE authority=?", (authority,)).fetchone()
                or hashlib.sha256(packet_raw).hexdigest() != proof["packet"]["sha256"]
                or packet.get("execution_grants") != {"QUERY": grants}
                or set(packet.get("requests", {})) != {"QUERY"}
                or any(g["kind"] not in {"identity", "read"} or g["quota"] != 1 for g in grants)
                or any(g.get("parent") or g.get("response_path") or g.get("target_param") for g in grants)
                or not old
                or dict(old) != proof["retained_wire"]["active"]
                or old["scope"] != proof["old_scope"]
                or old["account"] != account
            ):
                raise GateDeniedError()
            for name in ("grants", "attempts"):
                actual = [
                    dict(r)
                    for r in connection.execute(
                        f"SELECT * FROM {name} WHERE scope=? ORDER BY rowid",
                        (old["scope"],),
                    )
                ]
                if actual != proof["retained_wire"]["tables"][name]:
                    raise GateDeniedError()
            limits = [
                dict(r)
                for r in connection.execute(
                    "SELECT * FROM authority_limits WHERE authority IN (SELECT authority FROM grants WHERE scope=?) "
                    "ORDER BY authority,id",
                    (old["scope"],),
                )
            ]
            if limits != proof["retained_wire"]["authority_limits"]:
                raise GateDeniedError()
            retained_readonly = True
        if old and old["scope"] != scope and old["phase"] != "closed":
            # Never detach outstanding creation ownership simply to advance another phase/task.
            # The workflow closes a scope only after independent ownership/absence
            # review. OPS recovery may prove actual cleanup outside a failed old
            # delete attempt; retain that error without blocking a released slot.
            if (
                not retained_readonly
                and connection.execute(
                    "SELECT 1 FROM attempts a JOIN grants g ON a.scope=g.scope AND a.grant_id=g.id "
                    "WHERE a.scope=? AND g.kind='create' AND NOT EXISTS ("
                    "SELECT 1 FROM attempts d JOIN grants x ON d.scope=x.scope AND d.grant_id=x.id "
                    "WHERE d.scope=a.scope AND x.parent=a.grant_id AND x.kind='delete' AND d.state='succeeded')",
                    (old["scope"],),
                ).fetchone()
            ):
                raise GateDeniedError()
        if connection.execute("SELECT 1 FROM grants WHERE scope=?", (scope,)).fetchone():
            raise GateDeniedError()  # Reinstalling cannot reset quotas or replace an approved call.
        for grant in grants:
            connection.execute(
                "INSERT INTO grants(scope,id,kind,call,quota,used,parent,response_path,target_param,"
                "authority,read_policy,target_encoding) "
                "VALUES(?,?,?,?,?,0,?,?,?,?,?,?)",
                (
                    scope,
                    grant["id"],
                    grant["kind"],
                    canonical(api_call(grant["call"])),
                    grant["quota"],
                    grant.get("parent"),
                    canonical(grant.get("response_path")),
                    grant.get("target_param"),
                    authority or scope,
                    canonical(grant.get("read_policy")) if "read_policy" in grant else None,
                    grant.get("target_encoding"),
                ),
            )
            connection.execute(
                "INSERT OR IGNORE INTO authority_limits VALUES(?,?,0)", (authority or scope, grant["id"])
            )
        connection.execute(
            "INSERT OR REPLACE INTO active VALUES(?,?,?,?,?,?)", (slot, scope, session, account, expires, "execute")
        )


def cleanup(path: Path, slot: str, scope: str, session: str) -> None:
    with database(path) as connection:
        row = connection.execute("SELECT * FROM active WHERE slot=?", (slot,)).fetchone()
        if (
            not row
            or row["scope"] != scope
            or row["session"] != session
            or (row["expires"] is not None and row["expires"] <= time.time())
            or row["phase"] not in {"execute", "cleanup"}
        ):
            raise GateDeniedError()
        connection.execute("UPDATE active SET phase='cleanup' WHERE slot=?", (slot,))


def cleanup_identity_extensions(connection: sqlite3.Connection, scope: str) -> list[dict[str, Any]]:
    """Validate an OPS-only, immutable, one-shot cleanup identity supplement.

    Original grants/counters remain unchanged. The supplement cannot establish
    ownership, permit a new creation, or skip the actual signed AccountId check.
    """
    if not connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='cleanup_identity_reviews'"
    ).fetchone():
        return []
    rows = connection.execute("SELECT * FROM cleanup_identity_reviews WHERE scope=?", (scope,)).fetchall()
    values = []
    for row in rows:
        try:
            file = Path(row["evidence"])
            if not file.is_file() or file.is_symlink() or (os.name != "nt" and file.stat().st_mode & 0o077):
                raise GateDeniedError()
            raw = file.read_bytes()
            review = json.loads(raw)
            grant = connection.execute(
                "SELECT * FROM grants WHERE scope=? AND id=?", (scope, row["grant_id"])
            ).fetchone()
            if (
                hashlib.sha256(raw).hexdigest() != row["evidence_sha256"]
                or (
                    review.get("kind") != "ops_cleanup_identity_review"
                    and not (
                        review.get("kind") == "ops_managed_read_continuation_review"
                        and review.get("renew_identity") is True
                    )
                )
                or review.get("approved") is not True
                or any(review.get(key) != row[key] for key in ("scope", "session", "account", "authority"))
                or review.get("additional_identity_reads") != 1
                or not grant
                or grant["kind"] != "identity"
                or grant["quota"] != 1
                or grant["parent"] is not None
                or grant["authority"] != row["authority"]
                or json.loads(grant["call"]) != review["identity_call"]
            ):
                raise GateDeniedError()
            values.append(dict(grant))
        except (OSError, KeyError, TypeError, ValueError) as error:
            raise GateDeniedError() from error
    return values


def cleanup_identity_phase_allowed(connection: sqlite3.Connection, active: Any, grant: Any) -> bool:
    approved = cleanup_identity_extensions(connection, active["scope"])
    registration = connection.execute(
        "SELECT * FROM cleanup_identity_reviews WHERE scope=? AND session=? AND account=?",
        (active["scope"], active["session"], active["account"]),
    ).fetchone()
    if not registration or not any(item["id"] == grant["id"] for item in approved):
        return False
    review = json.loads(Path(registration["evidence"]).read_bytes())
    return active["phase"] == "cleanup" or (
        active["phase"] == "execute"
        and mse_managed_scope(connection, active["scope"])
        and review.get("kind") == "ops_managed_read_continuation_review"
        and review.get("renew_identity") is True
    )


def mse_retained_identity(
    connection: sqlite3.Connection,
    active: Any,
    grant: Any,
    parent: Any,
    credential: str,
    reference_scope: str,
    *,
    before: float | None = None,
) -> bool:
    """One actual same-account signer renewal for the reviewed retained generation.

    The old creation signature remains immutable. Only original reads and the
    original response-bound deletion are eligible; no creation capacity is added.
    """
    try:
        if (
            not parent
            or not grant
            or grant["kind"] not in {"read", "delete"}
            or not mse_managed_scope(connection, active["scope"])
        ):
            return False
        registered = connection.execute(
            "SELECT * FROM cleanup_identity_reviews WHERE scope=?", (active["scope"],)
        ).fetchone()
        if not registered:
            return False
        extensions = cleanup_identity_extensions(connection, active["scope"])
        if len(extensions) != 1 or registered["grant_id"] != extensions[0]["id"]:
            return False
        review = json.loads(Path(registered["evidence"]).read_bytes())
        if (
            review.get("kind") != "ops_managed_read_continuation_review"
            or review.get("renew_identity") is not True
            or any(review.get(k) != active[k] for k in ("scope", "session", "account"))
            or (parent["scope"], parent["session"]) != (active["scope"], active["session"])
            or review.get("root_creation")
            != {
                "attempt_id": parent["id"],
                "grant_id": parent["grant_id"],
                "response_sha256": nas_native_snapshot(json.loads(parent["response"])),
            }
        ):
            return False
        root = Path(connection.execute("PRAGMA database_list").fetchone()[2]).resolve().parent
        packet_path = (root / review["packet"]["path"]).resolve()
        if not packet_path.is_relative_to(root):
            return False
        packet_raw = packet_path.read_bytes()
        if hashlib.sha256(packet_raw).hexdigest() != review["packet"]["sha256"]:
            return False
        packet = json.loads(packet_raw)
        grant_id = dict(grant).get("grant_id", grant["id"])
        installed = connection.execute(
            "SELECT * FROM grants WHERE scope=? AND id=?", (active["scope"], grant_id)
        ).fetchone()
        original = next((g for g in packet["execution_grants"]["TF"] if g["id"] == grant_id), None)
        if (
            not installed
            or installed["authority"] != review.get("authority")
            or not original
            or original["kind"] != installed["kind"]
            or original["quota"] != installed["quota"]
            or canonical(api_call(original["call"])) != installed["call"]
            or original.get("parent") != parent["grant_id"]
            or (
                installed["kind"] == "delete"
                and (original.get("response_path") != ["StackId"] or original.get("target_param") != "StackId")
            )
        ):
            return False
        extension = extensions[0]
        signed = connection.execute(
            "SELECT * FROM attempts WHERE scope=? AND session=? AND grant_id=?",
            (active["scope"], active["session"], extension["id"]),
        ).fetchall()
        limit = connection.execute(
            "SELECT used FROM authority_limits WHERE authority=? AND id=?", (review["authority"], extension["id"])
        ).fetchone()
        if len(signed) != 1 or extension["used"] != 1 or not limit or limit[0] != 1:
            return False
        signer = signed[0]
        body = json.loads(signer["response"] or "null")
        return bool(
            signer["state"] == "succeeded"
            and signer["credential"] == credential
            and signer["reference_scope"] == reference_scope
            and reference_scope
            and signer["tool_use_id"]
            and signer["created"] > parent["created"]
            and (before is None or signer["created"] < before)
            and isinstance(body, dict)
            and body.get("AccountId") == active["account"]
            and isinstance(body.get("RequestId"), str)
            and body["RequestId"]
            and not any(k in body for k in ("Code", "ErrorCode"))
            and connection.execute(
                "SELECT 1 FROM identities WHERE scope=? AND session=? AND credential=? AND account=?",
                (active["scope"], active["session"], credential, active["account"]),
            ).fetchone()
        )
    except (OSError, KeyError, TypeError, ValueError, IndexError, sqlite3.Error, GateDeniedError):
        return False


def mse_rotated_cleanup_identity(
    connection: sqlite3.Connection,
    active: Any,
    grant: Any,
    parent: Any,
    credential: str,
    reference_scope: str,
    *,
    before: float | None = None,
) -> bool:
    """An independently reviewed new signer may only finish original post-delete reads.

    Old creation/member/delete signatures are never rebound. The sole appended
    STS call must actually prove the same account/session/authority before any
    read, and its immutable review must bind that original successful deletion.
    """
    if mse_retained_identity(connection, active, grant, parent, credential, reference_scope, before=before):
        return True
    try:
        policy = read_policy(grant)
        if (
            not parent
            or grant["kind"] != "read"
            or policy.get("phase") != "cleanup"
            or policy.get("required_cleanup") is not True
            or policy.get("after_delete") != parent["grant_id"]
            or not reference_scope
            or grant["authority"]
            != connection.execute(
                "SELECT authority FROM grants WHERE scope=? AND id=?",
                (parent["scope"], parent["grant_id"]),
            ).fetchone()[0]
        ):
            return False
        extensions = cleanup_identity_extensions(connection, active["scope"])
        if len(extensions) != 1:
            return False
        extension = extensions[0]
        approved = connection.execute(
            "SELECT * FROM cleanup_identity_reviews WHERE scope=? AND session=? AND account=? AND authority=?",
            (active["scope"], active["session"], active["account"], grant["authority"]),
        ).fetchone()
        if not approved or approved["grant_id"] != extension["id"]:
            return False
        review = json.loads(Path(approved["evidence"]).read_bytes())
        proof = review.get("post_delete_readonly", {})
        deletion = nas_managed_delete(connection, parent)
        if (
            not deletion
            or deletion["state"] != "succeeded"
            or deletion["session"] != parent["session"]
            or deletion["credential"] != parent["credential"]
            or deletion["reference_scope"] != parent["reference_scope"]
            or not deletion["tool_use_id"]
        ):
            return False
        deletion_grant = connection.execute(
            "SELECT * FROM grants WHERE scope=? AND id=?", (parent["scope"], deletion["grant_id"])
        ).fetchone()
        delete_limit = connection.execute(
            "SELECT used FROM authority_limits WHERE authority=? AND id=?", (grant["authority"], deletion["grant_id"])
        ).fetchone()
        if (
            not deletion_grant
            or deletion_grant["authority"] != grant["authority"]
            or deletion_grant["quota"] != 1
            or deletion_grant["used"] != 1
            or not delete_limit
            or delete_limit[0] != 1
            or connection.execute(
                "SELECT COUNT(*) FROM attempts WHERE scope=? AND grant_id=?", (parent["scope"], deletion["grant_id"])
            ).fetchone()[0]
            != 1
        ):
            return False
        expected = json.loads(deletion_grant["call"])
        expected["params"]["StackId"] = json.loads(parent["response"])["StackId"]
        if not successful_resource_group_body(deletion, expected) or proof.get("successful_deletions") != [
            {
                "attempt_id": deletion["id"],
                "grant_id": deletion["grant_id"],
                "parent": parent["grant_id"],
                "created": deletion["created"],
                "tool_use_id": deletion["tool_use_id"],
                "response_sha256": hashlib.sha256(canonical(json.loads(deletion["response"])).encode()).hexdigest(),
            }
        ]:
            return False
        reads = proof.get("cleanup_read_calls", [])
        grant_id = dict(grant).get("grant_id", grant["id"])
        if not any(
            r.get("id") == grant_id and r.get("kind") == "read" and r.get("read_policy") == policy for r in reads
        ):
            return False
        signed = connection.execute(
            "SELECT * FROM attempts WHERE scope=? AND session=? AND grant_id=? AND credential=?",
            (active["scope"], active["session"], extension["id"], credential),
        ).fetchall()
        limit = connection.execute(
            "SELECT used FROM authority_limits WHERE authority=? AND id=?", (grant["authority"], extension["id"])
        ).fetchone()
        if len(signed) != 1 or extension["used"] != 1 or not limit or limit[0] != 1:
            return False
        signed = signed[0]
        body = json.loads(signed["response"] or "null")
        return bool(
            signed["state"] == "succeeded"
            and signed["tool_use_id"]
            and signed["reference_scope"] == reference_scope
            and signed["created"] > deletion["created"]
            and (before is None or signed["created"] < before)
            and isinstance(body, dict)
            and body.get("AccountId") == active["account"]
            and isinstance(body.get("RequestId"), str)
            and body["RequestId"]
            and not any(k in body for k in ("Code", "ErrorCode"))
            and connection.execute(
                "SELECT 1 FROM identities WHERE scope=? AND session=? AND credential=? AND account=?",
                (active["scope"], active["session"], credential, active["account"]),
            ).fetchone()
        )
    except (OSError, KeyError, TypeError, ValueError, IndexError, sqlite3.Error, GateDeniedError):
        return False


def _matching(connection: sqlite3.Connection, slot: str, session: str, call: dict[str, Any]) -> tuple[Any, Any]:
    active = connection.execute("SELECT * FROM active WHERE slot=?", (slot,)).fetchone()
    if (
        not active
        or active["session"] != session
        or (active["expires"] is not None and active["expires"] <= time.time())
        or active["phase"] not in {"execute", "cleanup"}
    ):
        raise GateDeniedError()
    permit = connection.execute(
        "SELECT * FROM native_identity_recovery_permits WHERE scope=?",
        (active["scope"],),
    ).fetchone()
    handoff = native_consumed_handoff(connection, active) if permit else None
    if permit and handoff is None:
        grant = connection.execute(
            "SELECT * FROM grants WHERE scope=? AND id=?",
            (active["scope"], permit["grant_id"]),
        ).fetchone()
        limit = connection.execute(
            "SELECT used FROM authority_limits WHERE authority=? AND id=?",
            (permit["authority"], permit["grant_id"]),
        ).fetchone()
        if (
            permit["state"] != "armed"
            or permit["expires"] <= time.time()
            or permit["slot"] != slot
            or permit["session"] != session
            or permit["account"] != active["account"]
            or active["phase"] != "cleanup"
            or not grant
            or grant["kind"] != "identity"
            or grant["quota"] != 1
            or grant["used"] != 0
            or grant["authority"] != permit["authority"]
            or canonical(json.loads(grant["call"])) != permit["call"]
            or not _native_identity_call(json.loads(permit["call"]))
            or not limit
            or limit[0] != 0
            or canonical(call) != permit["call"]
            or connection.execute(
                "SELECT 1 FROM attempts WHERE scope=? AND state!='succeeded'",
                (active["scope"],),
            ).fetchone()
            or connection.execute(
                "SELECT 1 FROM attempts a LEFT JOIN grants g ON a.scope=g.scope AND a.grant_id=g.id "
                "WHERE a.scope=? AND (g.kind='delete' OR a.grant_id LIKE 'delete-%')",
                (active["scope"],),
            ).fetchone()
        ):
            raise GateDeniedError()
        return active, grant
    if handoff and handoff[1]["_handoff_pending"]:
        if canonical(call) != handoff[0]["call"]:
            raise GateDeniedError()
        return active, handoff[0]
    for grant in connection.execute("SELECT * FROM grants WHERE scope=? AND used<quota", (active["scope"],)):
        if handoff and grant["id"] not in handoff[1]["_handoff_allowed"]:
            continue
        if grant["id"].startswith("ops-native-identity-"):
            continuation = nas_native_continuation(connection, active)
            if not continuation or continuation[0]["id"] != grant["id"]:
                continue
        if grant["id"].startswith("ops-cleanup-identity-"):
            if not cleanup_identity_phase_allowed(connection, active, grant):
                continue
        limit = connection.execute(
            "SELECT used FROM authority_limits WHERE authority=? AND id=?", (grant["authority"], grant["id"])
        ).fetchone()
        if not limit or limit[0] >= grant["quota"]:
            continue
        if (
            (grant["kind"] == "delete" and active["phase"] != "cleanup")
            or (grant["kind"] == "create" and active["phase"] != "execute")
            or not phase_allowed(grant, active["phase"])
        ):
            continue
        policy = read_policy(grant)
        if (
            policy.get("after_delete")
            and not nas_native_binding(grant)
            and not connection.execute(
                "SELECT 1 FROM attempts a JOIN grants g ON a.scope=g.scope AND a.grant_id=g.id "
                "WHERE a.scope=? AND g.kind='delete' AND g.parent=? AND a.state='succeeded'",
                (active["scope"], policy["after_delete"]),
            ).fetchone()
        ):
            continue
        expected = json.loads(grant["call"])
        if nas_native_binding(grant):
            if grant["kind"] == "precheck" and active["phase"] != "execute":
                continue
            try:
                if grant["kind"] != "identity":
                    expected = nas_native_resolve(connection, active, grant, nas_native_signer(connection, active))
                if canonical(expected) == canonical(call):
                    return active, grant
            except GateDeniedError:
                pass
            continue
        if grant["parent"]:
            parent = connection.execute(
                "SELECT * FROM attempts WHERE scope=? AND grant_id=? AND state='succeeded'",
                (active["scope"], grant["parent"]),
            ).fetchone()
            if not parent:
                continue
            parent_grant = connection.execute(
                "SELECT call FROM grants WHERE scope=? AND id=?", (active["scope"], grant["parent"])
            ).fetchone()
            binding = json.loads(grant["response_path"])
            if mse_managed_scope(connection, active["scope"]) and grant["kind"] == "delete":
                parent = mse_managed_parent(connection, active, grant["parent"])
                mse_binding = next(
                    (
                        json.loads(g[0])
                        for g in connection.execute(
                            "SELECT response_path FROM grants WHERE scope=? AND parent=?",
                            (active["scope"], grant["parent"]),
                        )
                        if g[0]
                        and isinstance(json.loads(g[0]), dict)
                        and json.loads(g[0]).get("kind") == "mse_managed_child"
                    ),
                    None,
                )
                if (
                    not parent
                    or not mse_binding
                    or not (
                        mse_managed_members(connection, active, parent, mse_binding)
                        or mse_managed_rollback_complete(connection, active, parent, mse_binding)
                    )
                ):
                    continue
            if isinstance(binding, dict) and binding.get("kind") == "mse_managed_child":
                parent = mse_managed_parent(connection, active, grant["parent"])
                params = mse_managed_params(connection, active, parent, grant) if parent else None
                if params is not None:
                    expected["params"].update(params)
                    if canonical(expected) == canonical(call):
                        return active, grant
                continue
            if isinstance(binding, dict) and binding.get("kind") == "nas_managed_child":
                parent = nas_managed_parent(connection, active, grant["parent"])
                params = nas_managed_params(connection, active, parent, grant) if parent else None
                if params is not None:
                    expected["params"].update(params)
                    if canonical(expected) == canonical(call):
                        return active, grant
                continue
            if isinstance(binding, dict):
                parent = resource_group_parent(connection, active, grant["parent"])
                if not parent:
                    continue
                if binding["kind"] == "resource_group_child":
                    identifier = resource_group_child_id(connection, active, parent, binding)
                    if not resource_group_id(identifier) or (
                        expected["action"] != "GetResourceGroup"
                        and not resource_group_details(connection, active, parent, identifier, binding)
                    ):
                        continue
                else:
                    if not resource_group_empty_before_delete(connection, active, parent, binding):
                        continue
                    identifier = response_identifier(
                        json.loads(parent_grant["call"]),
                        json.loads(parent["response"]),
                        binding,
                    )
            else:
                identifier = response_identifier(
                    json.loads(parent_grant["call"]),
                    json.loads(parent["response"]),
                    binding,
                )
            if identifier is None:
                continue
            expected["params"][grant["target_param"]] = bound_target(grant, identifier)
        if canonical(expected) == canonical(call):
            return active, grant
    raise GateDeniedError()


def complete_session(session: str, cwd: str) -> str:
    if not session or session.startswith("ws~"):
        return session
    # Product tool bindings use the internal UUID; capabilities use the full
    # sidebar key. Preserve workspace identity rather than accepting UUID alone.
    from iac_code.web.session_manager import _encode_web_session_id

    return _encode_web_session_id(cwd, session)


def execution_instructions(session: str, cwd: str) -> str:
    instructions = _execution_instructions(session, cwd)
    if enabled():
        instructions = (
            "\nMake at most one reviewed aliyun_api call per assistant step, including reads. "
            "Await its actual successful response before forming a dependent call. "
            "Additional calls in the same step are deferred without execution or quota consumption; "
            "submit them separately only after checking the original authority and remaining quota.\n"
        ) + instructions
    return instructions


def _execution_instructions(session: str, cwd: str) -> str:
    """Expose the reviewed execution plan, not fabricated resource/link results.

    Exact inline templates must reach the product agent without smuggling known
    IDs or resource-type literals into the worker's natural-language UI request.
    The wire checks remain authoritative; this section grants no extra access.
    """
    if not enabled():
        return ""
    closed = "\n# Controlled test execution\nNo active reviewed API capability. Do not call tools."
    try:
        with database(Path(os.environ[ENV])) as connection:
            active = connection.execute("SELECT * FROM active WHERE slot=?", (os.environ[SLOT_ENV],)).fetchone()
            if (
                not active
                or active["session"] != complete_session(session, cwd)
                or (active["expires"] is not None and active["expires"] <= time.time())
            ):
                return closed
            if active["phase"] not in {"execute", "cleanup"}:
                return closed
            identity_bound = connection.execute(
                "SELECT 1 FROM identities WHERE scope=? AND account=?",
                (active["scope"], active["account"]),
            ).fetchone()
            permit = connection.execute(
                "SELECT * FROM native_identity_recovery_permits WHERE scope=?",
                (active["scope"],),
            ).fetchone()
            handoff = native_consumed_handoff(connection, active) if permit else None
            if permit and handoff is None:
                if (
                    permit["state"] != "armed"
                    or permit["expires"] <= time.time()
                    or permit["slot"] != os.environ[SLOT_ENV]
                    or permit["session"] != complete_session(session, cwd)
                    or permit["account"] != active["account"]
                    or active["phase"] != "cleanup"
                    or _matching(
                        connection, os.environ[SLOT_ENV], complete_session(session, cwd), json.loads(permit["call"])
                    )[1]["id"]
                    != permit["grant_id"]
                ):
                    return closed
                calls = [
                    {
                        "kind": "identity",
                        "remaining": 1,
                        "call": json.loads(permit["call"]),
                        "parent": None,
                        "response_path": None,
                        "target_param": None,
                    }
                ]
                return (
                    "\n# OPS-reviewed one-shot identity recovery\n"
                    "Make only this exact STS identity call once. Do not call any other API or tool.\n"
                    + canonical(calls)
                )
            if handoff and handoff[1]["_handoff_pending"]:
                return (
                    "\n# OPS-reviewed one-shot signer renewal\n"
                    "Make only this exact STS identity call once; await actual same-account success.\n"
                    + canonical([{"kind": "identity", "remaining": 1, "call": json.loads(handoff[0]["call"])}])
                )
            if active["phase"] == "execute" and not identity_bound:
                identity_instruction = (
                    "First make ONLY the STS identity call; "
                    "await its actual successful AccountId before any other call. "
                )
            else:
                identity_instruction = (
                    "In cleanup or execute continuation, reuse the execution identity ONLY while the wire gate "
                    "verifies the same approved "
                    "account, scope, session and current signing credential. Do not repeat STS solely because the "
                    "phase changed. If the signer rotated or that binding is missing, first make ONLY the STS "
                    "identity call and await its actual successful AccountId. If its remaining quota is exhausted "
                    "or the gate denies a call, stop and preserve evidence for OPS; never bypass the identity check "
                    "or reset quotas. "
                )
            calls = []
            for grant in connection.execute("SELECT * FROM grants WHERE scope=?", (active["scope"],)):
                if handoff and grant["id"] not in handoff[1]["_handoff_allowed"]:
                    continue
                if grant["id"].startswith("ops-native-identity-"):
                    continuation = nas_native_continuation(connection, active)
                    if not continuation or continuation[0]["id"] != grant["id"]:
                        continue
                if grant["id"].startswith("ops-cleanup-identity-") and not cleanup_identity_phase_allowed(
                    connection, active, grant
                ):
                    continue
                if (
                    (grant["kind"] == "delete" and active["phase"] != "cleanup")
                    or (grant["kind"] == "create" and active["phase"] != "execute")
                    or (grant["kind"] == "precheck" and active["phase"] != "execute")
                    or not phase_allowed(
                        grant,
                        active["phase"],
                    )
                ):
                    continue
                used = connection.execute(
                    "SELECT used FROM authority_limits WHERE authority=? AND id=?", (grant["authority"], grant["id"])
                ).fetchone()
                calls.append(
                    {
                        "kind": grant["kind"],
                        "remaining": max(0, grant["quota"] - used[0]),
                        "call": json.loads(grant["call"]),
                        "parent": grant["parent"],
                        "response_path": json.loads(grant["response_path"] or "null"),
                        "target_param": grant["target_param"],
                        **({"target_encoding": grant["target_encoding"]} if grant["target_encoding"] else {}),
                        **({"read_policy": read_policy(grant)} if read_policy(grant) else {}),
                    }
                )
        return (
            "\n# Controlled test execution — OPS-reviewed plan\n"
            "Use only structured aliyun_api and these exact calls. "
            + identity_instruction
            + "Never batch identity with reads/writes. "
            "Use the exact approved inline template, not a regenerated template or file. "
            "Parameters marked $created_id MUST come from the actual parent creation response at response_path; "
            "never guess IDs. For nas_native_dependency use only the fixed role bindings from actual own "
            "successful same-session/signer wire responses; SourceStorage adds only oss:// to own Location. "
            "A precheck is a serial NAS DryRun=true write-class request that creates nothing: HTTP200 and "
            "empty returned IDs are required before the separate create; never report it as creation. "
            "For mse_managed_child first obtain the same original stack's signed Terraform "
            "CREATE_COMPLETE status, enabled matching outputs and exactly its three reviewed members. "
            "The gate binds $created_stack_id and the logical/physical child ID from those original facts. "
            "Read each exact GetStackResource and service detail, freeze identities before DeleteStack; "
            "cleanup uses only the original same-session/signer children after successful own deletion. "
            "Missing/partial/unknown proofs remain unresolved ownership; never infer absence from aliases. "
            "For nas_managed_child first obtain own GetStack CREATE_COMPLETE and "
            "ListStackResources with exact provider types/logical IDs. FileSystemId uses only its own filesystem "
            "PhysicalResourceId; $created_mount_target_domain uses only the domain from its own same-parent "
            "composite mount PhysicalResourceId. Cleanup retains those pre-delete proofs and requires own "
            "successful DeleteStack. Never substitute StackId, query samples or arbitrary paths. "
            "A typed resource_group_child response_path is not the CreateStack StackId: "
            "first obtain the exact own completed GetStack/output and native ListStackResources proof, "
            "then the exact GetResourceGroup identity/name/display/account/OK proof. Only its ResourceManager "
            "read target may use that child group ID. A resource_group_empty delete response_path keeps "
            "its original creation path but requires its read_grant actual unfiltered ListResources success "
            "with integer TotalCount=0, matching pages and Resources.Resource=[] before deletion. "
            "Missing, failed, ambiguous or member-present proof means STOP; do not delete or replenish quota. "
            "Only the same original session, account, scope and signing credential may supply these proofs. "
            "For exact OSS PutBucket headers/location, use only its actual successful "
            "Location header after removing one validated leading slash; the gate checks the original status, "
            "request ID and exact bucket name. Never replace a missing header with the requested name. "
            "Before PutBucket, the exact approved GetBucketInfo must return 404 NoSuchBucket with the exact "
            "BucketName and RequestId under the same session and signer; other errors do not prove absence. "
            "For target_encoding=json-singleton, encode ONLY that one response value as a "
            "compact JSON array string. No additional target is permitted. "
            "Create once; wait for actual status before reporting success. "
            "Do not replay exhausted operations or use ros_deploy/Shell. In cleanup phase delete only task-owned "
            "resources and use actual read-only evidence to confirm absence. Resource links must derive from "
            "real API responses, never these instructions.\n" + canonical(calls)
        )
    except (GateDeniedError, KeyError, sqlite3.Error, OSError, ValueError, TypeError):
        return closed


def check(tool: str, value: dict[str, Any], session: str, *, cwd: str = "") -> str | None:
    if not enabled():
        return None
    try:
        if tool != "aliyun_api":
            # Shell, MCP, file writes and dedicated deployment shortcuts cannot bypass the gate.
            raise GateDeniedError()
        with database(Path(os.environ[ENV])) as connection:
            _matching(connection, os.environ[SLOT_ENV], complete_session(session, cwd), api_call(value))
    except (GateDeniedError, OSError, ValueError, KeyError, sqlite3.Error):
        return ERROR
    return None


def ots_table_precheck_target(call: dict[str, Any]) -> tuple[str, str] | None:
    """Recognize only the reviewed single-table managed template, without HCL evaluation.

    This is deliberately a closed literal grammar, not a general Terraform parser.
    Expressions, extra workspace files/resources/providers and ambiguous targets
    cannot turn a same-parent ListTable result into creation authority.
    """
    if call["product"] != "ros" or call["action"] != "CreateStack":
        return None
    raw = call["params"].get("TemplateBody")
    if not isinstance(raw, str):
        return None
    template = json.loads(raw)
    if not isinstance(template, dict) or set(template) != {"ROSTemplateFormatVersion", "Transform", "Workspace"}:
        return None
    workspace = template.get("Workspace")
    if (
        template["ROSTemplateFormatVersion"] != "2015-09-01"
        or template["Transform"] != "Aliyun::Terraform-v1.5"
        or not isinstance(workspace, dict)
        or set(workspace) != {"main.tf"}
        or not isinstance(workspace["main.tf"], str)
    ):
        return None
    pattern = (
        r"terraform\s*\{\s*required_providers\s*\{\s*alicloud\s*=\s*\{\s*"
        r'source\s*=\s*"aliyun/alicloud"\s+version\s*=\s*"= [0-9]+\.[0-9]+\.[0-9]+"\s*\}\s*\}\s*\}\s*'
        r'provider\s+"alicloud"\s*\{\s*region\s*=\s*"(?P<region>[a-z0-9-]+)"\s*\}\s*'
        r'resource\s+"alicloud_ots_table"\s+"(?P<label>[A-Za-z_][A-Za-z0-9_]*)"\s*\{\s*'
        r'instance_name\s*=\s*"(?P<instance>[a-z][a-z0-9-]+)"\s+'
        r'table_name\s*=\s*"(?P<table>[A-Za-z_][A-Za-z0-9_]{0,254})"\s+'
        r'primary_key\s*\{\s*name\s*=\s*"[A-Za-z_][A-Za-z0-9_]*"\s+type\s*=\s*"String"\s*\}\s*'
        r"time_to_live\s*=\s*-1\s+max_version\s*=\s*1\s*\}\s*"
        r'output\s+"table_id"\s*\{\s*value\s*=\s*alicloud_ots_table\.(?P=label)\.id\s*\}\s*'
        r'output\s+"instance_name"\s*\{\s*value\s*=\s*alicloud_ots_table\.(?P=label)\.instance_name\s*\}\s*'
        r'output\s+"table_name"\s*\{\s*value\s*=\s*alicloud_ots_table\.(?P=label)\.table_name\s*\}\s*'
    )
    match = re.fullmatch(pattern, workspace["main.tf"].strip())
    if not match or match["region"] != call["region_id"] or call["params"].get("RegionId") != call["region_id"]:
        return None
    return match["instance"], match["table"]


def require_ots_table_precheck(
    connection: sqlite3.Connection,
    active: Any,
    grant: Any,
    fingerprint: str,
) -> None:
    """Require the approved precheck's completed absence before reserving any create."""
    precheck = connection.execute(
        "SELECT * FROM grants WHERE scope=? AND id='table-name-precheck'",
        (active["scope"],),
    ).fetchone()
    call = json.loads(grant["call"])
    # The explicit reviewed precheck is mandatory even if its template cannot be
    # recognized. Also fail closed if an OTS template is installed without it.
    if precheck is None and "alicloud_ots_table" not in canonical(call):
        return
    raw = call["params"].get("TemplateBody")
    template = json.loads(raw) if isinstance(raw, str) else None
    if (
        isinstance(template, dict)
        and "Resources" in template
        and "Transform" not in template
        and "Workspace" not in template
    ):
        return  # This repair adds a managed-Terraform precondition, not a native ROS policy.
    target = ots_table_precheck_target(call)
    if precheck is None or target is None:
        raise GateDeniedError()
    instance, table = target
    expected = {
        "product": "tablestore",
        "action": "ListTable",
        "version": "2015-12-31",
        "region_id": call["region_id"],
        "params": {"InstanceName": instance},
    }
    if (
        precheck["kind"] != "read"
        or precheck["parent"]
        or read_policy(precheck) != {"phase": "execute"}
        or json.loads(precheck["call"]) != expected
    ):
        raise GateDeniedError()
    row = connection.execute(
        "SELECT * FROM attempts WHERE scope=? AND grant_id=? ORDER BY created DESC,rowid DESC LIMIT 1",
        (active["scope"], precheck["id"]),
    ).fetchone()
    if (
        not row
        or row["state"] != "succeeded"
        or row["session"] != active["session"]
        or row["credential"] != fingerprint
    ):
        raise GateDeniedError()
    response = json.loads(row["response"] or "null")
    if (
        not isinstance(response, dict)
        or set(response) != {"body", "status"}
        or type(response["status"]) is not int
        or response["status"] != 200
        or not isinstance(response["body"], dict)
        or set(response["body"]) != {"InstanceName", "Tables"}
    ):
        raise GateDeniedError()
    body = response["body"]
    if body["InstanceName"] != instance or not isinstance(body["Tables"], list):
        raise GateDeniedError()
    names = []
    for item in body["Tables"]:
        if (
            not isinstance(item, dict)
            or set(item) != {"InstanceName", "TableName"}
            or item["InstanceName"] != instance
            or not isinstance(item["TableName"], str)
            or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,254}", item["TableName"]) is None
        ):
            raise GateDeniedError()
        names.append(item["TableName"])
    if table in names or len(names) != len(set(names)):
        raise GateDeniedError()


def reserve(
    value: dict[str, Any], session: str, credential: Any, *, read_only: bool, cwd: str = "", tool_use_id: str = ""
) -> tuple[Path, str] | None:
    if not enabled():
        return None
    try:
        path = Path(os.environ[ENV])
        session = complete_session(session, cwd)
        mode = getattr(credential, "mode", None)
        if (
            mode not in {"AK", "StsToken", "OAuth"}
            or not getattr(credential, "access_key_id", None)
            or not getattr(credential, "access_key_secret", None)
        ):
            # Dynamic signing identities require a fingerprint adapter, not a null binding.
            raise GateDeniedError()
        if mode in {"StsToken", "OAuth"} and not getattr(credential, "sts_token", None):
            raise GateDeniedError()
        if mode == "OAuth":
            # The normal product provider has already refreshed OAuth into this
            # concrete STS triple. Bind that actual signer, never OAuth refresh
            # tokens or a dynamic provider identity. Rotation requires STS again.
            expiration = getattr(credential, "sts_expiration", None)
            if type(expiration) is not int or expiration <= time.time() + 60:
                raise GateDeniedError()
            mode = "StsToken"
        fingerprint = hashlib.sha256(
            canonical(
                [mode, *(getattr(credential, key, None) for key in ("access_key_id", "access_key_secret", "sts_token"))]
            ).encode()
        ).hexdigest()
        from iac_code.tools.cloud.aliyun.resource_reference_extractors import credential_reference_context

        reference_scope = credential_reference_context(credential).get("account_scope")
        with database(path) as connection:
            active, grant = _matching(connection, os.environ[SLOT_ENV], session, api_call(value))
            if (grant["kind"] in {"identity", "read"}) != read_only:
                raise GateDeniedError()  # An API name prefix is not proof that its contract is read-only.
            if (
                grant["kind"] != "identity"
                and not connection.execute(
                    "SELECT 1 FROM identities WHERE scope=? AND session=? AND credential=? AND account=?",
                    (active["scope"], session, fingerprint, active["account"]),
                ).fetchone()
            ):
                raise GateDeniedError()
            protected_native = nas_native_scope(connection, active["scope"])
            if protected_native:
                if not tool_use_id or not reference_scope:
                    raise GateDeniedError()
                if grant["kind"] != "identity":
                    signer = nas_native_signer(connection, active)
                    if signer["credential"] != fingerprint or signer["reference_scope"] != reference_scope:
                        raise GateDeniedError()
                    if nas_native_resolve(connection, active, grant, signer) != api_call(value):
                        raise GateDeniedError()
            protected_group = resource_group_scope(connection, active["scope"])
            protected_nas = nas_managed_scope(connection, active["scope"])
            protected_mse = mse_managed_scope(connection, active["scope"])
            if protected_mse:
                if not isinstance(tool_use_id, str) or not tool_use_id or not reference_scope:
                    raise GateDeniedError()
                if grant["kind"] != "identity":
                    signed = connection.execute(
                        "SELECT a.* FROM attempts a JOIN grants g ON a.scope=g.scope AND a.grant_id=g.id "
                        "WHERE a.scope=? AND a.session=? AND a.credential=? AND g.kind='identity' "
                        "ORDER BY a.created DESC,a.rowid DESC LIMIT 1",
                        (active["scope"], session, fingerprint),
                    ).fetchone()
                    if (
                        not signed
                        or signed["state"] != "succeeded"
                        or not signed["tool_use_id"]
                        or signed["reference_scope"] != reference_scope
                    ):
                        raise GateDeniedError()
            if (protected_group or protected_nas or protected_mse) and grant["parent"]:
                original_parent = (
                    mse_managed_parent
                    if protected_mse
                    else nas_managed_parent
                    if protected_nas
                    else resource_group_parent
                )(
                    connection,
                    active,
                    grant["parent"],
                )
                same_signer = bool(
                    original_parent
                    and original_parent["credential"] == fingerprint
                    and (not protected_mse or original_parent["reference_scope"] == reference_scope)
                )
                if not same_signer and not (
                    protected_mse
                    and isinstance(reference_scope, str)
                    and mse_rotated_cleanup_identity(
                        connection, active, grant, original_parent, fingerprint, reference_scope
                    )
                ):
                    raise GateDeniedError()
            approved_call = json.loads(grant["call"])
            if grant["kind"] == "create":
                require_ots_table_precheck(connection, active, grant, fingerprint)
                if protected_nas:
                    require_nas_stack_precheck(connection, active, grant, fingerprint)
            if grant["kind"] == "create" and is_oss_bucket_create(approved_call) and not protected_native:
                from iac_code.tools.cloud.aliyun.acs3_transport import NormalizedApiResponse
                from iac_code.tools.cloud.aliyun.oss_v4_adapter import oss_bucket_not_found

                checked = False
                for row in connection.execute(
                    "SELECT a.response,g.call FROM attempts a JOIN grants g ON a.scope=g.scope AND a.grant_id=g.id "
                    "WHERE a.scope=? AND a.session=? AND a.credential=? AND g.kind='read'",
                    (active["scope"], session, fingerprint),
                ):
                    precheck_call = json.loads(row["call"])
                    response = json.loads(row["response"]) if row["response"] else None
                    if (
                        precheck_call.get("product") == "oss"
                        and precheck_call.get("action") == "GetBucketInfo"
                        and precheck_call.get("version", "2019-05-17") == "2019-05-17"
                        and precheck_call.get("region_id") == approved_call["region_id"]
                        and precheck_call.get("params") == {"bucket": approved_call["params"]["bucket"]}
                        and isinstance(response, dict)
                        and set(response) == {"status", "headers", "body"}
                    ):
                        checked = oss_bucket_not_found(
                            NormalizedApiResponse(
                                status=response["status"],
                                headers=response["headers"],
                                body=response["body"],
                                content_type=None,
                                content_encoding=None,
                                size=0,
                            ),
                            approved_call["params"]["bucket"],
                        )
                        if checked:
                            break
                if not checked:
                    raise GateDeniedError()
            interval = read_policy(grant).get("min_interval_seconds", 0)
            if interval:
                last = connection.execute(
                    "SELECT MAX(a.created) FROM attempts a JOIN grants g "
                    "ON a.scope=g.scope AND a.grant_id=g.id WHERE g.authority=? AND g.id=?",
                    (grant["authority"], grant["id"]),
                ).fetchone()[0]
                if last is not None and (remaining := last + interval - time.time()) > 0:
                    raise ReadPendingError(remaining)
            ident = uuid.uuid4().hex
            permit = connection.execute(
                "SELECT * FROM native_identity_recovery_permits WHERE scope=?",
                (active["scope"],),
            ).fetchone()
            if permit and native_consumed_handoff(connection, active) is None:
                if (
                    grant["id"] != permit["grant_id"]
                    or permit["state"] != "armed"
                    or permit["expires"] <= time.time()
                    or canonical(api_call(value)) != permit["call"]
                ):
                    raise GateDeniedError()
                consumed = connection.execute(
                    "UPDATE native_identity_recovery_permits SET state='consumed',attempt_id=? "
                    "WHERE scope=? AND state='armed' AND expires>?",
                    (ident, active["scope"], time.time()),
                )
                if consumed.rowcount != 1:
                    raise GateDeniedError()
            connection.execute("UPDATE grants SET used=used+1 WHERE scope=? AND id=?", (active["scope"], grant["id"]))
            connection.execute(
                "UPDATE authority_limits SET used=used+1 WHERE authority=? AND id=?", (grant["authority"], grant["id"])
            )
            connection.execute(
                "INSERT INTO attempts(id,scope,grant_id,session,state,response,credential,created,reference_scope,"
                "tool_use_id) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    ident,
                    active["scope"],
                    grant["id"],
                    session,
                    "unknown",
                    canonical({"call": api_call(value)})
                    if (
                        protected_native
                        or (protected_group and grant["kind"] == "read")
                        or ((protected_nas or protected_mse) and grant["kind"] in {"read", "delete"})
                    )
                    else None,
                    fingerprint,
                    time.time(),
                    reference_scope,
                    tool_use_id,
                ),
            )
        return path, ident
    except (OSError, ValueError, KeyError, sqlite3.Error) as error:
        raise GateDeniedError() from error


async def reserve_when_ready(
    value: dict[str, Any], session: str, credential: Any, *, read_only: bool, cwd: str = "", tool_use_id: str = ""
) -> tuple[Path, str] | None:
    """Pace before the network timeout starts, outside SQLite and browser locks.

    Every wake rechecks the original session, signer, phase and cumulative quota.
    Cancellation while waiting has no wire intent and consumes no allowance.
    There is no worker or resource deadline.
    """
    while True:
        try:
            return reserve(value, session, credential, read_only=read_only, cwd=cwd, tool_use_id=tool_use_id)
        except ReadPendingError as pending:
            await asyncio.sleep(min(pending.seconds, 30))


def finish(
    ticket: tuple[Path, str] | None,
    response: Any,
    succeeded: bool,
    *,
    status: int | None = None,
    headers: Any = None,
) -> None:
    if ticket is None:
        return
    path, ident = ticket
    with database(path) as connection:
        attempt = connection.execute("SELECT * FROM attempts WHERE id=? AND state='unknown'", (ident,)).fetchone()
        if not attempt:
            raise GateDeniedError()
        grant = connection.execute(
            "SELECT * FROM grants WHERE scope=? AND id=?", (attempt["scope"], attempt["grant_id"])
        ).fetchone()
        call = json.loads(grant["call"])
        permit = connection.execute(
            "SELECT * FROM native_identity_recovery_permits WHERE scope=? AND attempt_id=?",
            (attempt["scope"], ident),
        ).fetchone()
        if permit:
            active = connection.execute(
                "SELECT account FROM active WHERE slot=? AND scope=? AND session=?",
                (permit["slot"], attempt["scope"], attempt["session"]),
            ).fetchone()
            valid = bool(
                succeeded
                and active
                and grant["id"] == permit["grant_id"]
                and permit["state"] == "consumed"
                and permit["expires"] > time.time()
                and canonical(call) == permit["call"]
                and _native_identity_call(call)
                and isinstance(response, dict)
                and response.get("AccountId") == permit["account"]
                and isinstance(response.get("RequestId"), str)
                and bool(response["RequestId"])
                and not any(key in response for key in ("Code", "ErrorCode"))
            )
            connection.execute(
                "UPDATE attempts SET state=?,response=? WHERE id=?",
                ("succeeded" if valid else "unknown", canonical(response), ident),
            )
            if valid:
                connection.execute(
                    "INSERT OR REPLACE INTO identities VALUES(?,?,?,?)",
                    (attempt["scope"], attempt["session"], attempt["credential"], permit["account"]),
                )
            return
        if nas_native_scope(connection, attempt["scope"]):
            request = json.loads(attempt["response"] or "null")
            actual = request.get("call") if isinstance(request, dict) else None
            envelope = {
                "body": response,
                "status": status,
                "headers": {
                    name: value
                    for name, value in (headers.items() if isinstance(headers, dict) else [])
                    if isinstance(name, str) and name.casefold() in {"x-acs-request-id", "x-oss-request-id", "location"}
                },
                "call": actual,
            }
            succeeded = bool(succeeded and actual and nas_native_success(actual, envelope, grant["kind"]))
            if succeeded and grant["kind"] == "identity":
                active = connection.execute(
                    "SELECT account FROM active WHERE scope=? AND session=?",
                    (attempt["scope"], attempt["session"]),
                ).fetchone()
                succeeded = bool(active and response.get("AccountId") == active["account"])
                if succeeded:
                    connection.execute(
                        "INSERT OR REPLACE INTO identities VALUES(?,?,?,?)",
                        (attempt["scope"], attempt["session"], attempt["credential"], active["account"]),
                    )
            connection.execute(
                "UPDATE attempts SET state=?,response=? WHERE id=?",
                ("succeeded" if succeeded else "unknown", canonical(envelope), ident),
            )
            return
        protected_group = resource_group_scope(connection, attempt["scope"])
        protected_nas = nas_managed_scope(connection, attempt["scope"])
        protected_mse = mse_managed_scope(connection, attempt["scope"])
        if protected_mse and grant["kind"] == "identity":
            succeeded = bool(
                succeeded
                and isinstance(response, dict)
                and response.get("AccountId")
                and successful_resource_group_body(
                    {
                        "state": "succeeded",
                        "response": canonical(
                            {
                                "body": response,
                                "status": status,
                                "headers": headers or {},
                                "call": call,
                            }
                        ),
                    },
                    call,
                )
            )
        if (protected_nas or protected_mse) and grant["kind"] != "identity":
            request = json.loads(attempt["response"] or "null")
            envelope = {
                "body": response,
                "status": status,
                "headers": {
                    name: value
                    for name, value in (headers.items() if isinstance(headers, dict) else [])
                    if isinstance(name, str) and name.casefold() == "x-acs-request-id"
                },
                "call": (
                    call if grant["kind"] == "create" else request.get("call") if isinstance(request, dict) else None
                ),
            }
            if protected_mse and call["product"] == "mse":
                # MSE successful responses have a documented ErrorCode field.
                # Keep it intact; only actual success/transport facts mark the wire successful.
                expected_call = envelope["call"]
                succeeded = bool(
                    succeeded
                    and isinstance(expected_call, dict)
                    and successful_mse_read_body(
                        {"state": "succeeded", "response": canonical(envelope)},
                        expected_call,
                    )
                )
            else:
                expected_call = envelope["call"]
                succeeded = bool(
                    succeeded
                    and isinstance(expected_call, dict)
                    and successful_resource_group_body(
                        {"state": "succeeded", "response": canonical(envelope)},
                        expected_call,
                    )
                )
            # Root creation records retain their established raw response schema.
            if grant["kind"] != "create":
                response = envelope
        if protected_group and grant["kind"] == "create":
            succeeded = bool(
                succeeded
                and type(status) is int
                and status == 200
                and isinstance(response, dict)
                and isinstance(response.get("RequestId"), str)
                and response["RequestId"]
                and "Code" not in response
                and "ErrorCode" not in response
            )
        if (
            protected_group
            and grant["kind"] == "read"
            and (
                (
                    call["product"] == "ros"
                    and call.get("version") == "2019-09-10"
                    and call["action"] in {"GetStack", "ListStackResources"}
                )
                or (
                    call["product"] == "resourcemanager"
                    and call.get("version") == "2020-03-31"
                    and call["action"] in {"GetResourceGroup", "ListResources", "ListResourceGroups"}
                )
            )
        ):
            request = json.loads(attempt["response"] or "null")
            response = {
                "body": response,
                "status": status,
                "headers": {
                    name: value
                    for name, value in (headers.items() if isinstance(headers, dict) else [])
                    if isinstance(name, str) and name.casefold() == "x-acs-request-id"
                },
                "call": request.get("call") if isinstance(request, dict) else None,
            }
            succeeded = bool(succeeded and type(status) is int and status == 200)
        elif (
            call["product"] == "oss"
            and call.get("version") == "2019-05-17"
            and call["action"] in {"PutBucket", "GetBucketInfo", "DeleteBucket", "ListBuckets"}
        ):
            response = {
                "body": response,
                "status": status,
                "headers": {
                    name: value
                    for name, value in (headers.items() if isinstance(headers, dict) else [])
                    if isinstance(name, str) and name.casefold() in {"location", "x-oss-request-id"}
                },
            }
            if call["action"] == "PutBucket":
                succeeded = succeeded and response_identifier(call, response, ["headers", "location"]) is not None
        elif (
            grant["kind"] == "read"
            and call["product"] == "ram"
            and call.get("version") == "2015-05-01"
            and call["action"] in {"GetPolicy", "GetRole"}
        ):
            response = {
                "body": response,
                "status": status,
                "headers": {
                    name: value
                    for name, value in (headers.items() if isinstance(headers, dict) else [])
                    if isinstance(name, str) and name.casefold() == "x-acs-request-id"
                },
            }
        if (
            grant["id"] == "table-name-precheck"
            and grant["kind"] == "read"
            and call["product"] == "tablestore"
            and call["action"] == "ListTable"
        ):
            # Preserve transport status for this execute precondition. Legacy
            # bodies and cleanup reads remain unchanged and cannot satisfy it.
            response = {"body": response, "status": status}
            succeeded = bool(succeeded and type(status) is int and status == 200)
        connection.execute(
            "UPDATE attempts SET state=?,response=? WHERE id=?",
            ("succeeded" if succeeded else "unknown", canonical(response), ident),
        )
        grant = connection.execute(
            "SELECT * FROM grants WHERE scope=? AND id=?", (attempt["scope"], attempt["grant_id"])
        ).fetchone()
        if succeeded and grant["kind"] == "identity" and isinstance(response, dict):
            account = str(response.get("AccountId", ""))
            connection.execute(
                "INSERT OR REPLACE INTO identities VALUES(?,?,?,?)",
                (attempt["scope"], attempt["session"], attempt["credential"], account),
            )


def creations(path: Path, job_ids: list[str]) -> list[dict[str, Any]]:
    """Export actual wire creation records, excluding credential fingerprints and secrets."""
    if not path.exists():
        return []
    records = []
    with database(path) as connection:
        for job_id in job_ids:
            for row in connection.execute(
                "SELECT a.*,g.call FROM attempts a JOIN grants g ON a.scope=g.scope AND a.grant_id=g.id "
                "WHERE g.kind='create' AND a.scope IN (?,?,?,?)",
                tuple(f"{job_id}:{dimension}" for dimension in ("ROS", "TF", "CREATE", "QUERY")),
            ):
                call = json.loads(row["call"])
                native = nas_native_scope(connection, row["scope"])
                if row["scope"].endswith(":QUERY") and not native:
                    continue
                identity = connection.execute(
                    "SELECT account FROM identities WHERE scope=? AND session=? AND credential=?",
                    (row["scope"], row["session"], row["credential"]),
                ).fetchone()
                delete = connection.execute(
                    "SELECT response_path FROM grants WHERE scope=? AND parent=? AND kind='delete'",
                    (row["scope"], row["grant_id"]),
                ).fetchone()
                response = json.loads(row["response"]) if row["response"] else None
                if native:
                    actual = response.get("call") if isinstance(response, dict) else None
                    identifier = nas_native_identifier(actual, response) if actual else None
                else:
                    identifier = response_identifier(call, response, json.loads(delete[0])) if delete else None
                if call["product"] == "oss" and call["action"] == "PutBucket" and row["state"] != "succeeded":
                    identifier = None
                records.append(
                    {
                        "attempt_id": row["id"],
                        "job_id": job_id,
                        "dimension": row["scope"].split(":")[-1],
                        "session_id": row["session"],
                        "reference_scope": dict(row).get("reference_scope"),
                        "tool_use_id": dict(row).get("tool_use_id"),
                        "state": row["state"],
                        "response": response,
                        "resource": {
                            "id": identifier,
                            "region": call["region_id"],
                            "account": identity[0] if identity else None,
                            "product": call["product"],
                            "action": call["action"],
                        },
                    }
                )
                if native:
                    records.extend(nas_native_owners(connection, row, records[-1]))
                elif mse_managed_scope(connection, row["scope"]):
                    records.extend(mse_managed_owners(connection, row, records[-1]))
                elif nas_managed_scope(connection, row["scope"]):
                    records.extend(nas_managed_owners(connection, row, records[-1]))
    return records


def reference_observations(path: Path, scope: str, session: str) -> list[dict[str, Any]]:
    """Bind opaque product references to actual successful signed calls and STS identity.

    Old rows have no scope proof: never synthesize one retrospectively.
    """
    with database(path) as connection:
        if "reference_scope" not in {row[1] for row in connection.execute("PRAGMA table_info(attempts)")}:
            return []
        return [
            {
                "account": row["account"],
                "reference_scope": row["reference_scope"],
                "tool_use_id": row["tool_use_id"],
                "region": json.loads(row["call"])["region_id"],
            }
            for row in connection.execute(
                "SELECT a.reference_scope,a.tool_use_id,i.account,g.call FROM attempts a "
                "JOIN identities i ON i.scope=a.scope AND i.session=a.session AND i.credential=a.credential "
                "JOIN grants g ON g.scope=a.scope AND g.id=a.grant_id "
                "WHERE a.scope=? AND a.session=? AND a.state='succeeded' AND g.kind IN ('read','create') "
                "AND a.reference_scope IS NOT NULL AND a.tool_use_id!=''",
                (scope, session),
            )
        ]
