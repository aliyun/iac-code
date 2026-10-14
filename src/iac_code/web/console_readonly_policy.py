"""Reviewed exceptions to ReadOnlyAccess needed by native console pages.

Add exact actions only, with documented read-only semantics or an explicit
owner confirmation. Never learn IAM grants automatically from a denied console
request or a Check/Search prefix.
This registry is shipped with the product; no cloud calls happen on import.

Maintenance: record the official API/service-policy URL or the explicit owner
confirmation honestly; an owner confirmation is not an official API reference.
Extend the offline contract test for every addition. The document hash changes automatically,
so previously configured installations offer the same explicit repair action.
Keep the registry additive: removing an action needs a reviewed migration, not
silently treating the former document as a customer conflict. Unknown actions
without an explicit read-only confirmation must not be granted just to pass a test.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

POLICY_NAME = "IacCodeConsoleReadOnlySupplement"
POLICY_DESCRIPTION = "Managed by iac-code: reviewed read-only permissions for embedded console previews."
REVIEWED_ACTIONS = {
    "oos:SearchInventory": "https://help.aliyun.com/zh/oos/developer-reference/api-oos-2019-06-01-searchinventory",
    "lindorm:CheckLindormInstanceValidate": "https://help.aliyun.com/zh/ram/developer-reference/aliyunlindormreadonlyaccess",
    "mse:CheckServiceLinkRole": "https://help.aliyun.com/zh/ram/developer-reference/aliyunmsereadonlyaccess/",
    "sae:CheckApplicationSlbConfigWarn": "https://help.aliyun.com/en/ram/developer-reference/aliyunsaereadonlyaccess",
    # Owner confirmed this exact console check is read-only and explicitly
    # requested permanent inclusion; the published system policy omits it.
    "odps:CheckTunnelAdditional": "owner-confirmed-readonly:2026-10-14",
}


def policy_document() -> dict[str, Any]:
    return {
        "Version": "1",
        "Statement": [{"Effect": "Allow", "Action": sorted(REVIEWED_ACTIONS), "Resource": "*"}],
    }


def policy_json() -> str:
    return json.dumps(policy_document(), separators=(",", ":"), sort_keys=True)


def policy_revision() -> str:
    return hashlib.sha256(policy_json().encode("utf-8")).hexdigest()


def managed_actions(document: Any) -> set[str] | None:
    """Recognize current or older subset documents, not customer modifications."""
    if not isinstance(document, dict) or set(document) != {"Version", "Statement"} or document["Version"] != "1":
        return None
    statements = document["Statement"]
    if not isinstance(statements, list) or len(statements) != 1:
        return None
    statement = statements[0]
    if not isinstance(statement, dict) or set(statement) != {"Effect", "Action", "Resource"}:
        return None
    if statement["Effect"] != "Allow" or statement["Resource"] != "*":
        return None
    actions = statement["Action"]
    if not isinstance(actions, list) or not actions or not all(isinstance(action, str) for action in actions):
        return None
    if len(set(actions)) != len(actions) or not set(actions).issubset(REVIEWED_ACTIONS):
        return None
    return {action for action in actions if isinstance(action, str)}
