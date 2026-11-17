"""Validate resource choices against current visible sources, never client URLs.

Source digests are content versions, not cloud-account identities. Raw source
content is read from the authorized transcript and never stored in bindings.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import struct
from dataclasses import dataclass
from functools import cached_property
from typing import Any

import yaml

from iac_code.tools.cloud.resource_references import ResourceSpec, locator_text, resource_locator_text, resource_spec

_SOURCE_FIELDS = {"message_id", "tool_use_id", "source_id", "source_path"}
_MESSAGE_PATHS = {"/text", "/thinking"}
_REQUEST_SOURCE = re.compile(r"blocking:(permission|question|elicitation):(.+)")
_REQUEST_PATH = re.compile(r"/(?:prompt|command|fields/(?:0|[1-9][0-9]*)/(?:label|description))")
_ERROR_SOURCE = re.compile(r"session-error:[1-9][0-9]*:.+")
_PREVIEW_SOURCE = re.compile(r"output-preview:[0-9a-f]{64}")
_NON_RESOURCE_FIELDS = {"requestid", "taskid", "operationid", "logicalresourceid", "displayname"}
_TYPE_FIELDS = ("Type", "ResourceType", "resource_type", "resourceType")
_ID_PATTERNS = {
    "ALIYUN::ECS::Instance": r"i-[A-Za-z0-9]{6,}",
    "ALIYUN::ECS::SecurityGroup": r"sg-[A-Za-z0-9]{6,}",
    "ALIYUN::VPC::VPC": r"vpc-[A-Za-z0-9]{6,}",
    "ALIYUN::VPC::VSwitch": r"vsw-[A-Za-z0-9]{6,}",
    "ALIYUN::VPC::EIP": r"eip-[A-Za-z0-9]{6,}",
    "ALIYUN::RDS::DBInstance": r"rm-[A-Za-z0-9]{6,}",
    "ALIYUN::ALB::LoadBalancer": r"alb-[A-Za-z0-9]{6,}",
    "ALIYUN::NLB::LoadBalancer": r"nlb-[A-Za-z0-9]{6,}",
}
_HCL_TOKEN = re.compile(r'//[^\n]*|\#[^\n]*|/\*[\s\S]*?\*/|"(?:\\.|[^"\\])*"|[A-Za-z_][\w-]*|[{}=]')


def _hcl_has_name(text: str, identifier: str, spec: ResourceSpec) -> bool:
    """Literal name/id assignments in an explicit resource block, never labels."""
    tokens = [match.group() for match in _HCL_TOKEN.finditer(text) if not match.group().startswith(("//", "#", "/*"))]
    scopes: list[str] = []
    for index, token in enumerate(tokens):
        if token == "{":
            resource_type = scopes[-1] if scopes else ""
            if index >= 3 and tokens[index - 3] in {"resource", "data"}:
                try:
                    resource_type = json.loads(tokens[index - 2])
                except ValueError:
                    resource_type = ""
            scopes.append(resource_type)
        elif token == "}" and scopes:
            scopes.pop()
        elif token in {"name", "id"} and scopes and index + 2 < len(tokens) and tokens[index + 1] == "=":
            if not _field_matches(token, spec, "name", scopes[-1]):
                continue
            try:
                value = json.loads(tokens[index + 2])
            except ValueError:
                continue
            if value == identifier:
                return True
    return False


def _markdown_has_name(text: str, identifier: str, spec: ResourceSpec, local_type: str) -> bool:
    # Only a real header/separator/data table establishes a column's meaning.
    lines = text.splitlines()
    for index in range(1, len(lines) - 1):
        columns = [part.strip() for part in lines[index].strip().strip("|").split("|")]
        if not columns or not all(re.fullmatch(r":?-{3,}:?", part) for part in columns):
            continue
        headers = [part.strip().strip("`") for part in lines[index - 1].strip().strip("|").split("|")]
        if len(headers) != len(columns):
            continue
        for line in lines[index + 1 :]:
            if "|" not in line:
                break
            cells = [part.strip().strip("`") for part in line.strip().strip("|").split("|")]
            if len(cells) != len(headers):
                break
            row = dict(zip(headers, cells))
            own_type = next((row[key] for key in _TYPE_FIELDS if resource_spec(row.get(key))), local_type)
            if any(value == identifier and _field_matches(key, spec, "name", own_type) for key, value in row.items()):
                return True
    return False


def _canonical(value: Any) -> Any:
    """Cross-language source encoding; JSON float spelling must not affect it."""
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, (int, float)):
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("resource_source_invalid")
        return ["number", struct.pack(">d", number).hex()]
    if isinstance(value, list):
        return ["array", [_canonical(item) for item in value]]
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        keys = sorted(value, key=lambda key: key.encode("utf-16-be", errors="surrogatepass"))
        return ["object", [[key, _canonical(value[key])] for key in keys]]
    raise ValueError("resource_source_invalid")


def canonical_source_json(value: Any) -> str:
    return json.dumps(_canonical(value), ensure_ascii=False, separators=(",", ":"))


def clean_source(value: Any) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) - _SOURCE_FIELDS:
        raise ValueError("resource_source_invalid")
    source = {key: item for key, item in value.items() if isinstance(item, str) and item}
    owners = [key for key in ("message_id", "tool_use_id", "source_id") if source.get(key)]
    if len(owners) != 1 or len(source) != 2 or not locator_text(source[owners[0]]):
        raise ValueError("resource_source_invalid")
    path = source.get("source_path", "")
    if owners[0] == "source_id":
        if _PREVIEW_SOURCE.fullmatch(source["source_id"]):
            if path != "/content":
                raise ValueError("resource_source_invalid")
            return source
        if _ERROR_SOURCE.fullmatch(source["source_id"]):
            if path != "/message":
                raise ValueError("resource_source_invalid")
            return source
        match = _REQUEST_SOURCE.fullmatch(source["source_id"])
        if match is None or not _REQUEST_PATH.fullmatch(path) or len(path) > 128:
            raise ValueError("resource_source_invalid")
        if path == "/command" and match[1] != "permission":
            raise ValueError("resource_source_invalid")
        if path.startswith("/fields/") and match[1] != "elicitation":
            raise ValueError("resource_source_invalid")
        return source
    if path not in (_MESSAGE_PATHS if owners[0] == "message_id" else {"/tool"}):
        raise ValueError("resource_source_invalid")
    return source


def _input(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (ValueError, RecursionError):
            pass
    return value


def tool_source_value(tool: dict[str, Any]) -> dict[str, Any]:
    """Only displayed, durable tool fields; no elapsed/status/internal metadata."""
    results = []
    for result in tool.get("results") or []:
        if isinstance(result, str):
            results.append(result)
        elif isinstance(result, dict):
            value = next((result[key] for key in ("content", "summary", "result") if result.get(key) is not None), "")
            results.append(value)
    progress = tool.get("stackProgress")
    stack_progress = None
    if isinstance(progress, dict):
        resources = []
        rows = progress.get("resources")
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            spec = resource_spec(row.get("resource_type"))
            fields = (spec.identifier_field, *spec.parent_fields) if spec else ()
            attributes = row.get("attributes") if isinstance(row.get("attributes"), dict) else {}
            resources.append(
                {
                    "resourceType": row.get("resource_type"),
                    "PhysicalResourceId": row.get("physical_resource_id"),
                    "attributes": {key: attributes[key] for key in fields if isinstance(attributes.get(key), str)},
                }
            )
        stack_progress = {"StackId": progress.get("stackId"), "resources": resources}
    return {
        "toolName": tool.get("toolName") or "",
        "input": _input(tool.get("input")),
        "results": results,
        "stackProgress": stack_progress,
    }


@dataclass(frozen=True)
class VisibleResourceSource:
    descriptor: dict[str, str]
    value: Any
    tool_type: str = ""
    field_name: str | None = None

    @cached_property
    def digest(self) -> str:
        value = self.value if self.field_name is None else {"fieldName": self.field_name, "text": self.value}
        encoded = canonical_source_json([self.descriptor, value]).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


def request_source_texts(kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Only non-interactive request text rendered by components/blocking.js.

    Do not scan choices, editable values, schema defaults, URLs or hidden tool
    input. Each displayed field owns its own binding and content version.
    """
    fields = {"permission": ("message", "description", "title", "action"), "question": ("question", "title")}
    prompt = next((payload[key] for key in fields.get(kind, ("message",)) if payload.get(key)), "")
    texts: dict[str, Any] = {"/prompt": prompt} if isinstance(prompt, str) and prompt else {}
    if kind == "permission":
        tool_input = payload.get("toolInput")
        command = payload.get("command") or (tool_input.get("command") if isinstance(tool_input, dict) else "")
        if isinstance(command, str) and command:
            texts["/command"] = command
    if kind == "elicitation" and payload.get("mode") == "form":
        rows = payload.get("fields")
        # Use the same filtered field positions as the renderer. Non-string
        # malformed labels are not accepted as authority for a resource choice.
        rows = [row for row in rows if isinstance(row, dict) and row.get("name")] if isinstance(rows, list) else []
        for index, row in enumerate(rows):
            if not isinstance(row["name"], str):
                continue
            label = row.get("label") or row.get("name")
            if isinstance(label, str):
                texts[f"/fields/{index}/label"] = {
                    "fieldName": row["name"],
                    "text": label + (" *" if row.get("required") is True else ""),
                }
            description = row.get("description")
            if isinstance(description, str) and description:
                texts[f"/fields/{index}/description"] = {"fieldName": row["name"], "text": description}
    return texts


def with_request_sources(
    transcript: dict[str, Any], session: Any, pipeline_snapshot: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Project current requests of this authorized session, never historical IDs.

    The projection is internal to resource validation, not a new API payload or
    cloud observation. A request's removal revokes its choices on hydration.
    """
    sources = {}
    for kind, attribute in (
        ("permission", "pending_permissions"),
        ("question", "pending_questions"),
        ("elicitation", "pending_elicitations"),
    ):
        for request_id, pending in getattr(session, attribute, {}).items():
            if pending.session_id != session.session_id or pending.request_id != request_id:
                continue
            if isinstance(pending.payload, dict):
                sources[f"blocking:{kind}:{request_id}"] = request_source_texts(kind, pending.payload)
    # Match pipelinePendingQuestionRequest's existing reload fallback. The
    # caller supplies only the local snapshot loaded for this session; live
    # registered requests take precedence, just as in the frontend.
    if getattr(session, "mode", None) == "pipeline" and isinstance(pipeline_snapshot, dict):
        control = pipeline_snapshot.get("control")
        pending = pipeline_snapshot.get("pendingInput") or (
            control.get("waitingInput") if isinstance(control, dict) else None
        )
        if isinstance(pending, dict) and pending.get("kind") == "ask_user_question":
            request_id = pending.get("inputId") or (
                "ask-" + pending["toolUseId"] if isinstance(pending.get("toolUseId"), str) else None
            )
            if locator_text(request_id):
                source_id = f"blocking:question:{request_id}"
                sources.setdefault(
                    source_id,
                    request_source_texts("question", {"question": pending.get("question") or pending.get("prompt")}),
                )
    return {**transcript, "resourceRequestSources": sources}


def with_error_source(transcript: dict[str, Any], session: Any) -> dict[str, Any]:
    """Validate the latest buffered error not cleared by a later user turn.

    An identical message from another event is a new appearance source. Local
    browser failures and evicted events cannot be verified from this buffer;
    bindings must not resurrect them or scan debug payloads for hidden values.
    """
    latest = None
    for event in session.events.replay_after(0):
        if event.get("sessionId") != session.session_id:
            continue
        payload = event.get("payload")
        if not isinstance(payload, dict):
            continue
        if event.get("type") == "error":
            latest = event
        elif event.get("type") == "user.message" and payload.get("turnId"):
            if latest and latest["payload"].get("turnId") != payload["turnId"]:
                latest = None
    sources = {}
    if latest:
        sequence, created_at = latest.get("sequence"), latest.get("createdAt")
        message = latest["payload"].get("message")
        if (
            type(sequence) is int
            and sequence > 0
            and isinstance(created_at, str)
            and created_at
            and isinstance(message, str)
            and message
        ):
            sources[f"session-error:{sequence}:{created_at}"] = {"/message": message}
    return {**transcript, "resourceErrorSources": sources}


def read_visible_source(transcript: dict[str, Any], descriptor: Any) -> VisibleResourceSource:
    source = clean_source(descriptor)
    if "source_id" in source:
        projection = (
            "resourcePreviewSources"
            if _PREVIEW_SOURCE.fullmatch(source["source_id"])
            else "resourceErrorSources"
            if _ERROR_SOURCE.fullmatch(source["source_id"])
            else "resourceRequestSources"
        )
        value = (transcript.get(projection) or {}).get(source["source_id"], {}).get(source["source_path"])
        if source["source_path"].startswith("/fields/"):
            if (
                not isinstance(value, dict)
                or set(value) != {"fieldName", "text"}
                or not isinstance(value["fieldName"], str)
                or not isinstance(value["text"], str)
            ):
                raise ValueError("resource_source_unavailable")
            # Field identity versions the source but is not visible locator
            # evidence. Reordering identical descriptions cannot move a choice.
            return VisibleResourceSource(source, value["text"], field_name=value["fieldName"])
        if not isinstance(value, str) or not value:
            raise ValueError("resource_source_unavailable")
        return VisibleResourceSource(source, value)
    if "message_id" in source:
        matches = [
            row
            for row in transcript.get("messages") or []
            if isinstance(row, dict) and row.get("messageId") == source["message_id"]
        ]
        if len(matches) != 1:
            raise ValueError("resource_source_unavailable")
        row = matches[0]
        value = row.get(source["source_path"][1:])
        if not isinstance(value, str):
            raise ValueError("resource_source_unavailable")
        return VisibleResourceSource(source, value)
    tool = (transcript.get("tools") or {}).get(source["tool_use_id"])
    if not isinstance(tool, dict):
        raise ValueError("resource_source_unavailable")
    value = tool_source_value(tool)
    request = value["input"]
    tool_type = ""
    if tool.get("toolName") == "aliyun_api" and isinstance(request, dict):
        product, action = str(request.get("product") or "").lower(), str(request.get("action") or "")
        if product == "ram":
            tool_type = "ALIYUN::RAM::Role" if "Role" in action else "ALIYUN::RAM::User" if "User" in action else ""
        elif product == "ecs":
            tool_type = "ALIYUN::ECS::Instance" if "Instance" in action else "ALIYUN::ECS::SecurityGroup"
        elif product == "ros" and "Stack" in action:
            tool_type = "ALIYUN::ROS::Stack"
    return VisibleResourceSource(source, value, tool_type)


def clean_occurrence_group(value: Any, identifier: str, kind: str, spec: ResourceSpec) -> list[Any]:
    """An opaque appearance scope is not authority to call a cloud API."""
    if not isinstance(value, list) or len(value) != 10 or len(canonical_source_json(value)) > 8192:
        raise ValueError("resource_occurrence_invalid")
    if value[:2] != [kind, identifier] or value[3] != spec.resource_type:
        raise ValueError("resource_occurrence_invalid")
    fields = {
        spec.identifier_field: spec.identifier_kind,
        **({spec.stable_field: "id"} if spec.stable_field else {}),
        **dict(spec.alias_fields),
    }
    if value[2] not in fields or fields[value[2]] != kind:
        raise ValueError("resource_occurrence_invalid")
    if any(item is not None and not locator_text(item) for item in value[4:7]):
        raise ValueError("resource_occurrence_invalid")
    if not isinstance(value[7], list):
        raise ValueError("resource_occurrence_invalid")
    for pair in value[7]:
        if not isinstance(pair, list) or len(pair) != 2 or not isinstance(pair[0], str) or not locator_text(pair[1]):
            raise ValueError("resource_occurrence_invalid")
    semantic = value[8]
    if semantic is not None and (
        not isinstance(semantic, dict)
        or set(semantic) - {"kind", "field", "typed"}
        or semantic.get("kind") not in {"field", "explicit_type", "command_field"}
        or ("typed" in semantic and not isinstance(semantic["typed"], bool))
    ):
        raise ValueError("resource_occurrence_invalid")
    if kind == "name" and semantic is None:
        raise ValueError("resource_occurrence_invalid")
    if value[9] is not None and (not isinstance(value[9], str) or len(value[9]) > 4096):
        raise ValueError("resource_occurrence_invalid")
    return value


def resource_binding_source_key(digest: str, group: list[Any]) -> str:
    # Group is canonical data, never a DOM position or a URL. Keeping it encoded
    # allows a synchronous lookup during decoration after one async source hash.
    return "source-v1:" + digest + ":" + canonical_source_json(group)


def _field_matches(field: str, spec: ResourceSpec, kind: str, local_type: str) -> bool:
    if field.replace("_", "").lower() in _NON_RESOURCE_FIELDS:
        return False
    locator_fields = [(spec.identifier_field, spec.identifier_kind), *spec.alias_fields]
    if spec.stable_field:
        locator_fields.append((spec.stable_field, "id"))
    expected = [name for name, expected_kind in locator_fields if expected_kind == kind]
    spellings = {
        spelling for name in expected for spelling in (name, re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name).lower())
    }
    if field in spellings:
        return True
    return bool(
        resource_spec(local_type) == spec
        and local_type.startswith("alicloud_")
        and field in ({"name", "id", "PhysicalResourceId"} if kind == "name" else {"id"})
    )


def source_contains_locator(
    source: VisibleResourceSource, identifier: str, kind: str, spec: ResourceSpec, *, require_type: bool = False
) -> bool:
    """Positive name semantics are required independently of client claims."""
    if not resource_locator_text(identifier, spec, spec.identifier_field):
        return False
    literal = re.escape(identifier)
    boundary = r"[\w@./%+*${}<>-]" if kind == "name" else r"[A-Za-z0-9_@./%+*${}<>-]"
    pattern = re.compile(r"(?<!" + boundary + ")" + literal + r"(?!" + boundary + ")")
    # Match the same narrow prose suffix as the frontend. This never relaxes
    # structured field equality, names, quoted literals, or composite IDs.
    sentence_pattern = None
    if kind == "id" and re.fullmatch(r"[A-Za-z0-9-]+", identifier):
        sentence_pattern = re.compile(
            r"(?<!" + boundary + r")(?<![\"'`])" + literal + r"\.(?=$|[\s,;!?，。；！？)\]）])"
        )

    def visit(value: Any, field: str = "", local_type: str = "") -> bool:
        if field.replace("_", "").lower() in _NON_RESOURCE_FIELDS:
            return False
        if isinstance(value, dict):
            own_type = next((value[key] for key in _TYPE_FIELDS if resource_spec(value.get(key))), local_type)
            return any(visit(item, str(key), own_type) for key, item in value.items())
        if isinstance(value, list):
            return any(visit(item, field, local_type) for item in value)
        if not isinstance(value, str):
            return False
        if value == identifier and _field_matches(field, spec, kind, local_type):
            if require_type and field in {"InstanceId", "instance_id", "LoadBalancerId", "load_balancer_id"}:
                explicit = resource_spec(local_type)
                if explicit is not None and explicit != spec:
                    return False
                if explicit is None and not re.fullmatch(_ID_PATTERNS.get(spec.resource_type, r"(?!)"), identifier):
                    return False
            return True
        stripped = value.strip()
        if stripped.startswith("```") and stripped.endswith("```"):
            # Decode fenced JSON before walking its fields; RequestId inside a
            # code block is no more authoritative than an ordinary JSON body.
            lines = stripped.splitlines()
            if len(lines) >= 3:
                return visit("\n".join(lines[1:-1]), "", local_type)
        if stripped.startswith(("{", "[")):
            try:
                parsed = json.loads(stripped)
            except (ValueError, RecursionError):
                pass
            else:
                return visit(parsed, "", local_type)
        if re.search(r"(?m)^\s*(?:-\s+)?[\w]+:\s+", stripped):
            try:
                parsed = yaml.safe_load(stripped)
            except (yaml.YAMLError, ValueError, RecursionError):
                pass
            else:
                if isinstance(parsed, (dict, list)):
                    return visit(parsed, "", local_type)
        # URLs/masks/placeholders cannot provide source evidence for inner IDs.
        safe = re.sub(r"(?:^|[^a-z0-9+.-])[a-z][a-z0-9+.-]*://[^\s<>\"'`]+", " ", value, flags=re.I)
        if kind == "id":
            prose_match = not field and sentence_pattern is not None and sentence_pattern.search(safe)
            if not require_type:
                return bool(pattern.search(safe) or prose_match)
            # A new declaration has no prior API identity to rely on. Accept a
            # reviewed ID shape or an explicit locator field, never any UUID.
            if re.fullmatch(_ID_PATTERNS.get(spec.resource_type, r"(?!)"), identifier):
                return bool(pattern.search(safe) or prose_match)
            fields = [spec.identifier_field, re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", spec.identifier_field).lower()]
            return any(
                re.search(
                    r"(?<!\w)[\"']?" + re.escape(name) + r"[\"']?\s*[:=]\s*[\"'`]?" + literal + r"(?!" + boundary + ")",
                    safe,
                )
                for name in fields
            )
        if _hcl_has_name(safe, identifier, spec) or _markdown_has_name(safe, identifier, spec, local_type):
            return True
        expected_fields = [
            name
            for name, expected_kind in ((spec.identifier_field, spec.identifier_kind), *spec.alias_fields)
            if expected_kind == kind
        ]
        fields = list(
            dict.fromkeys(
                spelling
                for name in expected_fields
                for spelling in (name, re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name).lower())
            )
        )
        for name in fields:
            field_pattern = r"(?<!\w)[\"']?" + re.escape(name) + r"[\"']?\s*[:=]\s*[\"'`]?"
            if re.search(field_pattern + literal + r"(?!" + boundary + ")", safe):
                return True
            if re.search(r"--" + re.escape(name) + r"\s+[\"']?" + literal + r"(?!" + boundary + ")", safe):
                return True
        labels = {
            "ALIYUN::RAM::Role": r"RAM\s*(?:role\b|角色)",
            "ALIYUN::RAM::User": r"RAM\s*(?:user\b|用户)",
            "ALIYUN::OSS::Bucket": r"OSS\s*(?:bucket\b|存储桶)",
            "ALIYUN::FC::Function": r"FC\s*(?:function\b|函数)",
        }
        label = labels.get(spec.resource_type)
        if label:
            prefix = r"\b" + label + r"(?:\s+name\b|名称|名)?\s*(?:is\b\s*|named\b\s*|为|是|[:=：])?\s*[`\"'“「]?"
            if re.search(prefix + literal + r"(?!" + boundary + ")", safe, re.I):
                return True
            prose_prefix = r"\b" + label + r"(?:\s+name\b|名称|名)?\s*(?:is\b\s*|named\b\s*|为|是|[:=：])?\s*"
            if re.search(prose_prefix + literal + r"\.(?=$|[\s,;!?，。；！？)\]）])", safe, re.I):
                return True
        return False

    return visit(source.value, local_type=source.tool_type)
