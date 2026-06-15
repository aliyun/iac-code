from __future__ import annotations

from typing import Any

from a2a.server.request_handlers.response_helpers import agent_card_to_dict
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentCardSignature,
    AgentExtension,
    AgentInterface,
    AgentProvider,
    AgentSkill,
    APIKeySecurityScheme,
    HTTPAuthSecurityScheme,
    SecurityRequirement,
)
from google.protobuf.json_format import ParseDict

from iac_code import __version__
from iac_code.a2a.exposure import format_a2a_exposure_types
from iac_code.a2a.parts import supported_input_mime_types
from iac_code.a2a.pipeline_events import PIPELINE_EVENTS_EXTENSION_URI
from iac_code.a2a.signing import sign_agent_card_dict
from iac_code.i18n import _
from iac_code.pipeline.config import RunMode, get_run_mode

IAC_CODE_ARTIFACT_METADATA_EXTENSION_URI = "urn:iac-code:a2a:artifact-metadata:v1"
IAC_CODE_PIPELINE_EVENTS_EXTENSION_URI = PIPELINE_EVENTS_EXTENSION_URI
IAC_CODE_THINKING_EXPOSURE_EXTENSION_URI = "urn:iac-code:a2a:thinking-exposure:v1"
CANONICAL_CALLER_EXTENSION_URIS = {
    IAC_CODE_PIPELINE_EVENTS_EXTENSION_URI,
}


def _base_url(host: str, port: int) -> str:
    return f"http://{host}:{port}/"


def agent_card_to_client_dict(card: AgentCard) -> dict[str, Any]:
    data = agent_card_to_dict(card)
    if not card.supported_interfaces:
        return data

    primary_interface = card.supported_interfaces[0]
    data.setdefault("url", primary_interface.url)
    data.setdefault("preferredTransport", primary_interface.protocol_binding)
    data.setdefault("protocolVersion", primary_interface.protocol_version)

    additional_interfaces = [
        {"url": interface.url, "transport": interface.protocol_binding} for interface in card.supported_interfaces[1:]
    ]
    if additional_interfaces:
        data.setdefault("additionalInterfaces", additional_interfaces)

    return data


def _add_security_requirement(card: AgentCard, scheme_name: str) -> None:
    requirement = SecurityRequirement()
    requirement.schemes[scheme_name].list.append("")
    card.security_requirements.append(requirement)


def build_agent_card(
    *,
    host: str,
    port: int,
    token_enabled: bool,
    basic_enabled: bool = False,
    api_key_enabled: bool = False,
    api_key_header: str = "X-API-Key",
    signing_secret: str | None = None,
    signing_key_id: str = "default",
    push_notifications: bool = False,
    supported_interfaces: list[dict[str, str]] | None = None,
    agent_extensions: Any = None,
    thinking_exposure_types: Any = None,
) -> AgentCard:
    url = _base_url(host, port)
    description = "AI-powered Infrastructure as Code assistant for Alibaba Cloud ROS and Terraform workflows."
    if not token_enabled and not basic_enabled and not api_key_enabled:
        description += " Unauthenticated A2A server mode is intended for trusted local environments."
    if push_notifications:
        description += (
            " Experimental terminal-state webhooks can be enabled locally, but the standard A2A push config API is not"
            " advertised."
        )

    interfaces = (
        [
            AgentInterface(
                url=item["url"],
                protocol_binding=item["protocolBinding"],
                protocol_version=item.get("protocolVersion", "1.0"),
            )
            for item in supported_interfaces
        ]
        if supported_interfaces
        else [
            AgentInterface(url=url, protocol_binding="JSONRPC", protocol_version="1.0"),
        ]
    )
    input_modes = supported_input_mime_types()

    card = AgentCard(
        name="iac-code",
        description=description,
        supported_interfaces=interfaces,
        provider=AgentProvider(organization="iac-code"),
        version=__version__,
        capabilities=AgentCapabilities(
            streaming=True,
            push_notifications=push_notifications,
            extended_agent_card=True,
        ),
        default_input_modes=input_modes,
        default_output_modes=["text/plain"],
        skills=[
            AgentSkill(
                id="iac_generation",
                name="IaC Generation",
                description="Generate Alibaba Cloud ROS and Terraform templates from natural language.",
                tags=["iac", "ros", "terraform", "alibaba-cloud"],
                examples=["Create a VPC with two vSwitches in cn-hangzhou."],
                input_modes=input_modes,
                output_modes=["text/plain"],
            ),
            AgentSkill(
                id="iac_review",
                name="IaC Review",
                description="Inspect IaC templates and suggest fixes.",
                tags=["iac", "review", "validation"],
                examples=["Review this ROS template for missing parameters."],
                input_modes=input_modes,
                output_modes=["text/plain"],
            ),
            AgentSkill(
                id="aliyun_ros_operations",
                name="Alibaba Cloud ROS Operations",
                description="Assist with ROS stack workflows using iac-code tools.",
                tags=["aliyun", "ros", "stack"],
                examples=["Check why this ROS stack update failed."],
                input_modes=input_modes,
                output_modes=["text/plain"],
            ),
            AgentSkill(
                id="terraform_ros_conversion",
                name="Terraform To ROS Conversion",
                description="Assist Terraform-to-ROS conversion using bundled iac-code skill resources.",
                tags=["terraform", "ros", "conversion"],
                examples=["Convert this Terraform VPC module to ROS YAML."],
                input_modes=input_modes,
                output_modes=["text/plain"],
            ),
        ],
    )
    card.capabilities.extensions.append(
        AgentExtension(
            uri=IAC_CODE_ARTIFACT_METADATA_EXTENSION_URI,
            description="Optional iac-code metadata namespace for tool status and stored local artifact metadata.",
            required=False,
        )
    )
    if thinking_exposure_types is not None:
        enabled_types = format_a2a_exposure_types(thinking_exposure_types)
        if enabled_types:
            extension = AgentExtension(
                uri=IAC_CODE_THINKING_EXPOSURE_EXTENSION_URI,
                description=(
                    "Optional iac-code metadata namespace for selected thinking exposure signals. "
                    "Raw thinking is emitted only when raw_thinking is enabled."
                ),
                required=False,
            )
            ParseDict({"enabledTypes": enabled_types}, extension.params)
            card.capabilities.extensions.append(extension)
    for item in _iter_agent_extensions(agent_extensions, excluded_uris=CANONICAL_CALLER_EXTENSION_URIS):
        card.capabilities.extensions.append(_agent_extension_from_dict(item))
    pipeline_events_extension = AgentExtension(
        uri=IAC_CODE_PIPELINE_EVENTS_EXTENSION_URI,
        description=_(
            "Optional iac-code pipeline event extension for state snapshots, replay, interrupts, "
            "and parallel candidate streams."
        ),
        required=False,
    )
    run_mode = get_run_mode()
    ParseDict(
        {
            "schemaVersion": "1.0",
            "enabled": run_mode == RunMode.PIPELINE,
            "mode": run_mode.value,
            "supportsSnapshot": True,
            "supportsReplay": True,
            "supportsInterrupts": True,
            "supportsParallelCandidates": True,
            "stateEndpoint": "/iac-code/pipeline/state",
        },
        pipeline_events_extension.params,
    )
    card.capabilities.extensions.append(pipeline_events_extension)

    if token_enabled:
        card.security_schemes["bearerAuth"].http_auth_security_scheme.CopyFrom(HTTPAuthSecurityScheme(scheme="bearer"))
        _add_security_requirement(card, "bearerAuth")

    if basic_enabled:
        card.security_schemes["basicAuth"].http_auth_security_scheme.CopyFrom(HTTPAuthSecurityScheme(scheme="basic"))
        _add_security_requirement(card, "basicAuth")

    if api_key_enabled:
        card.security_schemes["apiKeyAuth"].api_key_security_scheme.CopyFrom(
            APIKeySecurityScheme(location="header", name=api_key_header)
        )
        _add_security_requirement(card, "apiKeyAuth")

    if signing_secret:
        signed_data = sign_agent_card_dict(agent_card_to_dict(card), secret=signing_secret, key_id=signing_key_id)
        signatures = signed_data.get("signatures")
        signature = signatures[0] if isinstance(signatures, list) and signatures else None
        if isinstance(signature, dict):
            header = signature.get("header")
            header_dict = dict(header) if isinstance(header, dict) else {}
            card_signature = AgentCardSignature(
                protected=str(signature.get("protected") or ""),
                signature=str(signature.get("signature") or ""),
                header=header_dict,
            )
            card.signatures.append(card_signature)

    return card


def build_extended_agent_card(card: AgentCard) -> AgentCard:
    extended = AgentCard()
    extended.CopyFrom(card)
    extended.skills.append(
        AgentSkill(
            id="iac_code_runtime_details",
            name="iac-code Runtime Details",
            description="Authenticated details for task management, push configuration, and local runtime behavior.",
            tags=["iac-code", "runtime", "a2a"],
            examples=["List my current A2A tasks."],
            input_modes=supported_input_mime_types(),
            output_modes=["text/plain"],
        )
    )
    return extended


def _agent_extension_from_dict(item: dict[str, Any]) -> AgentExtension:
    extension = AgentExtension(
        uri=str(item["uri"]),
        description=str(item.get("description") or ""),
        required=bool(item.get("required", False)),
    )
    params = item.get("params")
    if isinstance(params, dict):
        ParseDict(params, extension.params)
    return extension


def _iter_agent_extensions(value: Any, *, excluded_uris: set[str] | None = None) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    blocked_uris = excluded_uris or set()
    return [
        item
        for item in value
        if isinstance(item, dict) and isinstance(item.get("uri"), str) and item["uri"] not in blocked_uris
    ]
