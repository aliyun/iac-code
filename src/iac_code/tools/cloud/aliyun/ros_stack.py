"""ROS Stack tool for Alibaba Cloud Resource Orchestration Service."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Literal

from alibabacloud_ros20190910 import models as ros_models

from iac_code.i18n import _
from iac_code.services.cloud_credentials import CloudCredentials
from iac_code.services.telemetry import add_metric, log_event
from iac_code.services.telemetry.names import Events, Metrics
from iac_code.services.telemetry.sanitize import (
    bucket_resource_count,
    sanitize_error_message,
    sanitize_resource_type,
    sanitize_terraform_provider,
)
from iac_code.tools.cloud.aliyun.ros_client import RosClientFactory
from iac_code.tools.cloud.base_stack import BaseCloudStack
from iac_code.tools.cloud.types import ResourceStatus, StackStatus

_URL_SCHEMES = ("http://", "https://", "oss://")

# Telemetry helpers
_TERRAFORM_TRANSFORM_PREFIXES = ("Aliyun::Terraform-", "Aliyun::OpenTofu-")
_HCL_RESOURCE_RE = re.compile(r'resource\s+"([^"]+)"\s+"[^"]+"\s*\{', re.MULTILINE)

_ROS_ERROR_CATEGORIES = {
    "InvalidTemplateBody": "syntax",
    "TemplateFormatVersionNotSupported": "syntax",
    "TemplateURLNotReachable": "reference",
    "ResourceNotFound": "reference",
    "QuotaExceeded": "quota",
    "Forbidden": "permission",
    "NoPermission": "permission",
}

SUPPORTED_ACTIONS = [
    "CreateStack",
    "UpdateStack",
    "ContinueCreateStack",
    "DeleteStack",
]

_CREATE_TERMINAL_STATUSES = {
    "CREATE_COMPLETE",
    "CREATE_FAILED",
    "CREATE_ROLLBACK_COMPLETE",
    "CREATE_ROLLBACK_FAILED",
    "IMPORT_CREATE_COMPLETE",
    "IMPORT_CREATE_FAILED",
    "IMPORT_CREATE_ROLLBACK_COMPLETE",
    "IMPORT_CREATE_ROLLBACK_FAILED",
}

_UPDATE_TERMINAL_STATUSES = {
    "UPDATE_COMPLETE",
    "UPDATE_FAILED",
    "ROLLBACK_COMPLETE",
    "ROLLBACK_FAILED",
    "IMPORT_UPDATE_COMPLETE",
    "IMPORT_UPDATE_FAILED",
    "IMPORT_UPDATE_ROLLBACK_COMPLETE",
    "IMPORT_UPDATE_ROLLBACK_FAILED",
}

_DELETE_TERMINAL_STATUSES = {
    "DELETE_COMPLETE",
    "DELETE_FAILED",
}

logger = logging.getLogger(__name__)


def _parse_template(template_body: str) -> dict | None:
    """Try YAML first (with ROS tag support), then JSON. Return None if unparseable."""
    from iac_code.tools.cloud.aliyun.ros_yaml import ros_yaml_load

    if template_body.lstrip().startswith("{"):
        try:
            data = json.loads(template_body)
        except Exception:
            return None
    else:
        try:
            data = ros_yaml_load(template_body)
        except Exception:
            return None
    return data if isinstance(data, dict) else None


def _detect_iac_kind(template_data: dict) -> Literal["ros", "terraform"]:
    """iac_kind driven by Transform field."""
    transform = template_data.get("Transform", "")
    values = transform if isinstance(transform, list) else [transform]
    for v in values:
        if isinstance(v, str) and v.startswith(_TERRAFORM_TRANSFORM_PREFIXES):
            return "terraform"
    return "ros"


def _extract_ros_resource_types(template_data: dict) -> list[str]:
    """ROS native: enumerate Resources.<name>.Type."""
    resources = template_data.get("Resources", {})
    if not isinstance(resources, dict):
        return []
    types: list[str] = []
    for resource in resources.values():
        if not isinstance(resource, dict):
            continue
        rtype = resource.get("Type")
        if isinstance(rtype, str) and rtype:
            types.append(rtype)
    return types


def _extract_terraform_resource_types(template_data: dict) -> list[str]:
    """Terraform transform: grep `resource "<type>" "<name>"` in Workspace *.tf files."""
    workspace = template_data.get("Workspace", {})
    if not isinstance(workspace, dict):
        return []
    types: list[str] = []
    for filename, content in workspace.items():
        if not isinstance(filename, str) or not filename.endswith(".tf"):
            continue
        if not isinstance(content, str):
            continue
        types.extend(_HCL_RESOURCE_RE.findall(content))
    return types


def _extract_resource_types(template_body: str) -> tuple[Literal["ros", "terraform"], list[str]]:
    """Return (iac_kind, list_of_resource_types)."""
    data = _parse_template(template_body)
    if data is None:
        return ("ros", [])
    kind = _detect_iac_kind(data)
    if kind == "terraform":
        return ("terraform", _extract_terraform_resource_types(data))
    return ("ros", _extract_ros_resource_types(data))


def _template_body_for_telemetry(params: dict) -> str:
    template_body = params.get("TemplateBody", "")
    return template_body if isinstance(template_body, str) else ""


def _count_by_type(types: list[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for t in types:
        counts[t] = counts.get(t, 0) + 1
    return counts


def _classify_ros_error(e: Exception) -> str:
    code = getattr(e, "code", "")
    return _ROS_ERROR_CATEGORIES.get(code, "other")


class RosStack(BaseCloudStack):
    """Alibaba Cloud ROS Stack lifecycle tool.

    Manages the full lifecycle of ROS stacks including create, update,
    continue-create, and delete operations with progress polling.
    """

    poll_interval: int = 5

    @property
    def provider_name(self) -> str:
        return "ros"

    @property
    def supported_actions(self) -> list[str]:
        return SUPPORTED_ACTIONS

    def user_facing_name(self, input: dict | None = None) -> str:
        return _("ROS Stack")

    def is_action_terminal(self, action: str, status: StackStatus) -> bool:
        if action in {"CreateStack", "ContinueCreateStack"}:
            return status.status in _CREATE_TERMINAL_STATUSES
        if action == "UpdateStack":
            return status.status in _UPDATE_TERMINAL_STATUSES
        if action == "DeleteStack":
            return status.status in _DELETE_TERMINAL_STATUSES
        return super().is_action_terminal(action, status)

    def is_action_success(self, action: str, status: StackStatus) -> bool:
        if action == "DeleteStack":
            return status.status == "DELETE_COMPLETE"
        return super().is_action_success(action, status)

    def _log_event_best_effort(self, event_name: str, metadata: dict[str, Any]) -> None:
        try:
            log_event(event_name, metadata)
        except Exception as exc:
            logger.debug("Failed to emit ROS telemetry event %s: %s", event_name, exc)

    def _add_metric_best_effort(self, name: str, value: int, attrs: dict[str, Any]) -> None:
        try:
            add_metric(name, value, attrs)
        except Exception as exc:
            logger.debug("Failed to emit ROS telemetry metric %s: %s", name, exc)

    def _deployment_telemetry_contexts(self) -> dict[tuple[str, str], dict[str, Any]]:
        contexts = getattr(self, "_ros_deployment_telemetry_contexts", None)
        if contexts is None:
            contexts = {}
            self._ros_deployment_telemetry_contexts = contexts
        return contexts

    def _store_deployment_telemetry_context(
        self,
        stack_id: str,
        *,
        action: str,
        iac_kind: str,
        region: str,
        started_at: float,
        resource_count_total: int,
        resource_types: list[str],
        resource_type_counts: list[int],
        terraform_providers: list[str],
    ) -> None:
        if not stack_id:
            return
        self._deployment_telemetry_contexts()[(stack_id, action)] = {
            "action": action,
            "iac_kind": iac_kind,
            "region": region,
            "started_at": started_at,
            "resource_count_total": resource_count_total,
            "resource_count_bucket": bucket_resource_count(resource_count_total),
            "resource_types": resource_types,
            "resource_type_counts": resource_type_counts,
            "terraform_providers": terraform_providers,
        }

    def on_terminal_status(
        self,
        action: str,
        params: dict,
        region: str,
        status: StackStatus,
        resources: list[ResourceStatus],
        elapsed_seconds: int,
    ) -> None:
        if action not in {"CreateStack", "UpdateStack"}:
            return
        context = self._deployment_telemetry_contexts().pop((status.stack_id, action), None)
        if context is None:
            return
        context_action = context.get("action")
        if context_action not in {"CreateStack", "UpdateStack"}:
            return

        kind = context["iac_kind"]
        duration_ms = int((time.monotonic() - context["started_at"]) * 1000)
        resource_count_total = context["resource_count_total"]
        resource_count_bucket = context["resource_count_bucket"]

        if status.is_success:
            self._log_event_best_effort(
                Events.DEPLOYMENT_SUCCEEDED,
                {
                    "iac_kind": kind,
                    "region": context["region"],
                    "duration_ms": duration_ms,
                    "resource_count_total": resource_count_total,
                    "stack_status": status.status,
                },
            )
            self._add_metric_best_effort(Metrics.DEPLOYMENT_COUNT, 1, {"kind": kind, "outcome": "success"})
            self._add_metric_best_effort(
                Metrics.DEPLOYMENT_DURATION,
                duration_ms,
                {
                    "kind": kind,
                    "outcome": "success",
                    "resource_count_bucket": resource_count_bucket,
                },
            )
            for rtype, count in zip(context["resource_types"], context["resource_type_counts"]):
                self._add_metric_best_effort(
                    Metrics.RESOURCE_TYPE_OBSERVED_COUNT,
                    count,
                    {
                        "kind": kind,
                        "resource_type": rtype,
                        "phase": "deploy",
                    },
                )
            if kind == "terraform":
                for prov in context["terraform_providers"]:
                    self._add_metric_best_effort(
                        Metrics.TERRAFORM_PROVIDER_OBSERVED_COUNT,
                        1,
                        {
                            "provider": prov,
                            "phase": "deploy",
                        },
                    )
            return

        sanitized_reason = sanitize_error_message(status.status_reason)
        self._log_event_best_effort(
            Events.DEPLOYMENT_FAILED,
            {
                "iac_kind": kind,
                "region": context["region"],
                "duration_ms": duration_ms,
                "resource_count_total": resource_count_total,
                "stack_status": status.status,
                "status_reason": sanitized_reason,
                "error_code": status.status,
                "error_category": "other",
                "http_status": 0,
                "error_message": sanitized_reason,
            },
        )
        self._add_metric_best_effort(
            Metrics.DEPLOYMENT_COUNT,
            1,
            {
                "kind": kind,
                "outcome": "fail",
                "error_category": "other",
            },
        )
        self._add_metric_best_effort(
            Metrics.DEPLOYMENT_DURATION,
            duration_ms,
            {
                "kind": kind,
                "outcome": "fail",
                "resource_count_bucket": resource_count_bucket,
            },
        )

    def on_polling_error(
        self,
        action: str,
        params: dict,
        region: str,
        stack_id: str,
        error_stage: str,
        error: Exception,
    ) -> None:
        try:
            if action in {"CreateStack", "UpdateStack"}:
                self._deployment_telemetry_contexts().pop((stack_id, action), None)
        except Exception as exc:
            logger.debug("Failed to clean ROS deployment telemetry context: %s", exc)

    def on_polling_cancelled(
        self,
        action: str,
        params: dict,
        region: str,
        stack_id: str,
        elapsed_seconds: int,
    ) -> None:
        if action not in {"CreateStack", "UpdateStack"}:
            return

        context = self._deployment_telemetry_contexts().pop((stack_id, action), None)
        if context is None:
            return
        context_action = context.get("action")
        if context_action not in {"CreateStack", "UpdateStack"}:
            return

        kind = context["iac_kind"]
        duration_ms = int((time.monotonic() - context["started_at"]) * 1000)
        if duration_ms < 0:
            duration_ms = elapsed_seconds * 1000

        self._log_event_best_effort(
            Events.DEPLOYMENT_CANCELLED,
            {
                "iac_kind": kind,
                "region": context["region"],
                "duration_ms": duration_ms,
                "reason": "user_cancel",
            },
        )
        self._add_metric_best_effort(Metrics.DEPLOYMENT_COUNT, 1, {"kind": kind, "outcome": "cancel"})

    def _get_default_region(self) -> str:
        credentials = CloudCredentials()
        cred = credentials.get_provider("aliyun")
        return cred.region_id if cred else ""

    @property
    def description(self) -> str:
        return (
            "Manage Alibaba Cloud ROS (Resource Orchestration Service) stack lifecycle. "
            "Supports creating, updating, continuing, and deleting stacks with "
            "real-time progress polling."
        )

    def _get_client(self, region: str) -> Any:
        credentials = CloudCredentials()
        cred = credentials.get_provider("aliyun")
        return RosClientFactory.create(cred, region_id=region)

    async def call_action(self, action: str, params: dict, region: str) -> str:
        client = self._get_client(region)
        # Ensure RegionId is always in params for the API request
        if region:
            params.setdefault("RegionId", region)
        # TemplateURL as local file path → read into TemplateBody
        template_url = params.get("TemplateURL", "")
        if template_url and not template_url.startswith(_URL_SCHEMES):
            params["TemplateBody"] = Path(template_url).read_text(encoding="utf-8")
            del params["TemplateURL"]
        # TemplateBody must be a JSON string; models may pass a dict
        if isinstance(params.get("TemplateBody"), dict):
            params["TemplateBody"] = json.dumps(params["TemplateBody"], ensure_ascii=False)

        from iac_code.tools.cloud.aliyun.api_hooks import run_hooks

        hook_result = run_hooks("ros", action, params)
        if hook_result is not None:
            raise ValueError(hook_result.content)

        if action == "CreateStack":
            return await self._handle_create_stack(client, params, region)
        elif action == "UpdateStack":
            return await self._handle_update_stack(client, params, region)
        elif action == "ContinueCreateStack":
            request = ros_models.ContinueCreateStackRequest().from_map(params)
            response = await asyncio.to_thread(client.continue_create_stack, request)
            return response.body.stack_id
        elif action == "DeleteStack":
            return await self._handle_delete_stack(client, params, region)
        raise ValueError(f"Unsupported: {action}")

    async def _handle_create_stack(self, client: Any, params: dict, region: str) -> str:
        """CreateStack with telemetry for template generation and deployment."""
        template_body = _template_body_for_telemetry(params)

        # Extract IaC kind and resource types
        kind, resource_types_raw = _extract_resource_types(template_body)
        resource_counts = _count_by_type(resource_types_raw)
        safe_types = [sanitize_resource_type(t, kind) for t in resource_counts.keys()]
        counts = list(resource_counts.values())
        total = sum(counts)

        # Terraform-specific: extract providers
        tf_providers: list[str] = []
        if kind == "terraform":
            raw_providers = {t.split("_", 1)[0] for t in resource_counts.keys() if "_" in t}
            tf_providers = sorted({sanitize_terraform_provider(p) for p in raw_providers})

        # --- Task 27: Emit template.generated event ---
        stripped = template_body.lstrip()
        template_format = "json" if stripped.startswith("{") else "yaml"

        template_generated_payload = {
            "iac_kind": kind,
            "template_format": template_format,
            "template_size_bytes": len(template_body.encode("utf-8")),
            "resource_count_total": total,
            "resource_count_distinct": len(set(safe_types)),
            "resource_types": safe_types[:50],
            "resource_type_counts": counts[:50],
            "generation_source": "agent",
        }
        if kind == "terraform":
            template_generated_payload["terraform_providers"] = tf_providers

        self._log_event_best_effort(Events.TEMPLATE_GENERATED, template_generated_payload)
        self._add_metric_best_effort(
            Metrics.TEMPLATE_GENERATED_COUNT,
            1,
            {
                "kind": kind,
                "format": template_format,
                "outcome": "success",
            },
        )
        for rtype, count in zip(safe_types, counts):
            self._add_metric_best_effort(
                Metrics.RESOURCE_TYPE_OBSERVED_COUNT,
                count,
                {
                    "kind": kind,
                    "resource_type": rtype,
                    "phase": "generate",
                },
            )
        if kind == "terraform":
            for prov in tf_providers:
                self._add_metric_best_effort(
                    Metrics.TERRAFORM_PROVIDER_OBSERVED_COUNT,
                    1,
                    {
                        "provider": prov,
                        "phase": "generate",
                    },
                )

        # --- Task 26: Emit deployment events ---
        deployment_started_payload = {
            "iac_kind": kind,
            "region": region,
            "resource_count_total": total,
            "resource_count_distinct": len(set(safe_types)),
            "resource_types": safe_types[:50],
            "resource_type_counts": counts[:50],
        }
        if kind == "terraform":
            deployment_started_payload["terraform_providers"] = tf_providers

        self._log_event_best_effort(Events.DEPLOYMENT_STARTED, deployment_started_payload)

        started = time.monotonic()
        try:
            request = ros_models.CreateStackRequest().from_map(params)
            response = await asyncio.to_thread(client.create_stack, request)
            stack_id = response.body.stack_id
            self._store_deployment_telemetry_context(
                stack_id,
                action="CreateStack",
                iac_kind=kind,
                region=region,
                started_at=started,
                resource_count_total=total,
                resource_types=safe_types[:50],
                resource_type_counts=counts[:50],
                terraform_providers=tf_providers,
            )
            return stack_id
        except (KeyboardInterrupt, asyncio.CancelledError):
            duration_ms = int((time.monotonic() - started) * 1000)
            self._log_event_best_effort(
                Events.DEPLOYMENT_CANCELLED,
                {
                    "iac_kind": kind,
                    "region": region,
                    "duration_ms": duration_ms,
                    "reason": "user_cancel",
                },
            )
            self._add_metric_best_effort(Metrics.DEPLOYMENT_COUNT, 1, {"kind": kind, "outcome": "cancel"})
            raise
        except TimeoutError:
            duration_ms = int((time.monotonic() - started) * 1000)
            self._log_event_best_effort(
                Events.DEPLOYMENT_CANCELLED,
                {
                    "iac_kind": kind,
                    "region": region,
                    "duration_ms": duration_ms,
                    "reason": "timeout",
                },
            )
            self._add_metric_best_effort(Metrics.DEPLOYMENT_COUNT, 1, {"kind": kind, "outcome": "cancel"})
            raise
        except Exception as e:
            duration_ms = int((time.monotonic() - started) * 1000)
            error_category = _classify_ros_error(e)
            self._log_event_best_effort(
                Events.DEPLOYMENT_FAILED,
                {
                    "iac_kind": kind,
                    "region": region,
                    "duration_ms": duration_ms,
                    "resource_count_total": total,
                    "error_code": getattr(e, "code", type(e).__name__),
                    "error_category": error_category,
                    "http_status": getattr(e, "status_code", 0) or 0,
                    "error_message": sanitize_error_message(str(e)),
                },
            )
            self._add_metric_best_effort(
                Metrics.DEPLOYMENT_COUNT,
                1,
                {
                    "kind": kind,
                    "outcome": "fail",
                    "error_category": error_category,
                },
            )
            self._add_metric_best_effort(
                Metrics.DEPLOYMENT_DURATION,
                duration_ms,
                {
                    "kind": kind,
                    "outcome": "fail",
                    "resource_count_bucket": bucket_resource_count(total),
                },
            )
            raise

    async def _handle_update_stack(self, client: Any, params: dict, region: str) -> str:
        """UpdateStack with telemetry for deployment events."""
        template_body = _template_body_for_telemetry(params)

        # Extract IaC kind and resource types
        kind, resource_types_raw = _extract_resource_types(template_body)
        resource_counts = _count_by_type(resource_types_raw)
        safe_types = [sanitize_resource_type(t, kind) for t in resource_counts.keys()]
        counts = list(resource_counts.values())
        total = sum(counts)

        # Terraform-specific: extract providers
        tf_providers: list[str] = []
        if kind == "terraform":
            raw_providers = {t.split("_", 1)[0] for t in resource_counts.keys() if "_" in t}
            tf_providers = sorted({sanitize_terraform_provider(p) for p in raw_providers})

        deployment_started_payload = {
            "iac_kind": kind,
            "region": region,
            "resource_count_total": total,
            "resource_count_distinct": len(set(safe_types)),
            "resource_types": safe_types[:50],
            "resource_type_counts": counts[:50],
        }
        if kind == "terraform":
            deployment_started_payload["terraform_providers"] = tf_providers

        self._log_event_best_effort(Events.DEPLOYMENT_STARTED, deployment_started_payload)

        started = time.monotonic()
        try:
            request = ros_models.UpdateStackRequest().from_map(params)
            response = await asyncio.to_thread(client.update_stack, request)
            stack_id = response.body.stack_id
            self._store_deployment_telemetry_context(
                stack_id,
                action="UpdateStack",
                iac_kind=kind,
                region=region,
                started_at=started,
                resource_count_total=total,
                resource_types=safe_types[:50],
                resource_type_counts=counts[:50],
                terraform_providers=tf_providers,
            )
            return stack_id
        except (KeyboardInterrupt, asyncio.CancelledError):
            duration_ms = int((time.monotonic() - started) * 1000)
            self._log_event_best_effort(
                Events.DEPLOYMENT_CANCELLED,
                {
                    "iac_kind": kind,
                    "region": region,
                    "duration_ms": duration_ms,
                    "reason": "user_cancel",
                },
            )
            self._add_metric_best_effort(Metrics.DEPLOYMENT_COUNT, 1, {"kind": kind, "outcome": "cancel"})
            raise
        except TimeoutError:
            duration_ms = int((time.monotonic() - started) * 1000)
            self._log_event_best_effort(
                Events.DEPLOYMENT_CANCELLED,
                {
                    "iac_kind": kind,
                    "region": region,
                    "duration_ms": duration_ms,
                    "reason": "timeout",
                },
            )
            self._add_metric_best_effort(Metrics.DEPLOYMENT_COUNT, 1, {"kind": kind, "outcome": "cancel"})
            raise
        except Exception as e:
            duration_ms = int((time.monotonic() - started) * 1000)
            error_category = _classify_ros_error(e)
            self._log_event_best_effort(
                Events.DEPLOYMENT_FAILED,
                {
                    "iac_kind": kind,
                    "region": region,
                    "duration_ms": duration_ms,
                    "resource_count_total": total,
                    "error_code": getattr(e, "code", type(e).__name__),
                    "error_category": error_category,
                    "http_status": getattr(e, "status_code", 0) or 0,
                    "error_message": sanitize_error_message(str(e)),
                },
            )
            self._add_metric_best_effort(
                Metrics.DEPLOYMENT_COUNT,
                1,
                {
                    "kind": kind,
                    "outcome": "fail",
                    "error_category": error_category,
                },
            )
            self._add_metric_best_effort(
                Metrics.DEPLOYMENT_DURATION,
                duration_ms,
                {
                    "kind": kind,
                    "outcome": "fail",
                    "resource_count_bucket": bucket_resource_count(total),
                },
            )
            raise

    async def _handle_delete_stack(self, client: Any, params: dict, region: str) -> str:
        """DeleteStack with request failure/cancellation telemetry, no started or terminal success event."""
        stack_id = params.get("StackId", "")

        # DeleteStack: no template available, use "ros" as conservative default for kind
        kind = "ros"

        started = time.monotonic()
        try:
            request = ros_models.DeleteStackRequest().from_map(params)
            await asyncio.to_thread(client.delete_stack, request)
            return stack_id
        except (KeyboardInterrupt, asyncio.CancelledError):
            duration_ms = int((time.monotonic() - started) * 1000)
            self._log_event_best_effort(
                Events.DEPLOYMENT_CANCELLED,
                {
                    "iac_kind": kind,
                    "region": region,
                    "duration_ms": duration_ms,
                    "reason": "user_cancel",
                },
            )
            self._add_metric_best_effort(Metrics.DEPLOYMENT_COUNT, 1, {"kind": kind, "outcome": "cancel"})
            raise
        except TimeoutError:
            duration_ms = int((time.monotonic() - started) * 1000)
            self._log_event_best_effort(
                Events.DEPLOYMENT_CANCELLED,
                {
                    "iac_kind": kind,
                    "region": region,
                    "duration_ms": duration_ms,
                    "reason": "timeout",
                },
            )
            self._add_metric_best_effort(Metrics.DEPLOYMENT_COUNT, 1, {"kind": kind, "outcome": "cancel"})
            raise
        except Exception as e:
            duration_ms = int((time.monotonic() - started) * 1000)
            error_category = _classify_ros_error(e)
            self._log_event_best_effort(
                Events.DEPLOYMENT_FAILED,
                {
                    "iac_kind": kind,
                    "region": region,
                    "duration_ms": duration_ms,
                    "error_code": getattr(e, "code", type(e).__name__),
                    "error_category": error_category,
                    "http_status": getattr(e, "status_code", 0) or 0,
                    "error_message": sanitize_error_message(str(e)),
                },
            )
            self._add_metric_best_effort(
                Metrics.DEPLOYMENT_COUNT,
                1,
                {
                    "kind": kind,
                    "outcome": "fail",
                    "error_category": error_category,
                },
            )
            self._add_metric_best_effort(
                Metrics.DEPLOYMENT_DURATION,
                duration_ms,
                {
                    "kind": kind,
                    "outcome": "fail",
                    "resource_count_bucket": "0",  # Unknown resource count
                },
            )
            raise

    async def get_stack_status(self, stack_id: str, region: str) -> StackStatus:
        client = self._get_client(region)
        request = ros_models.GetStackRequest(
            stack_id=stack_id, region_id=region, show_resource_progress="PercentageOnly"
        )
        response = await asyncio.to_thread(client.get_stack, request)
        data = response.body.to_map()
        return StackStatus(
            stack_id=data.get("StackId", stack_id),
            stack_name=data.get("StackName", ""),
            status=data.get("Status", ""),
            status_reason=data.get("StatusReason", ""),
            progress_percentage=data.get("ResourceProgress", {}).get("StackOperationProgress", 0),
        )

    async def get_stack_resources(self, stack_id: str, region: str) -> list[ResourceStatus]:
        client = self._get_client(region)
        request = ros_models.ListStackResourcesRequest(stack_id=stack_id, region_id=region)
        response = await asyncio.to_thread(client.list_stack_resources, request)
        data = response.body.to_map()
        resources = []
        for r in data.get("Resources", []):
            resources.append(
                ResourceStatus(
                    name=r.get("LogicalResourceId", ""),
                    resource_type=r.get("ResourceType", ""),
                    status=r.get("Status", ""),
                    status_reason=r.get("StatusReason", ""),
                )
            )
        return resources
