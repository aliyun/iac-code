from a2a.server.routes.agent_card_routes import agent_card_to_dict

from iac_code.a2a.agent_card import build_agent_card
from iac_code.a2a.exposure import A2AExposureType
from iac_code.a2a.pipeline_events import PIPELINE_EVENTS_EXTENSION_URI

PIPELINE_EVENTS_EXTENSION_PARAMS = {
    "schemaVersion": "1.0",
    "enabled": False,
    "mode": "normal",
    "supportsSnapshot": True,
    "supportsReplay": True,
    "supportsInterrupts": True,
    "supportsParallelCandidates": True,
    "stateEndpoint": "/iac-code/pipeline/state",
}


def _extensions_by_uri(data: dict, uri: str) -> list[dict]:
    return [extension for extension in data["capabilities"]["extensions"] if extension["uri"] == uri]


def _extension_by_uri(data: dict, uri: str) -> dict:
    matches = _extensions_by_uri(data, uri)
    assert len(matches) == 1
    return matches[0]


def test_agent_card_declares_a2a_1_jsonrpc_interface() -> None:
    card = build_agent_card(host="127.0.0.1", port=41242, token_enabled=False)
    data = agent_card_to_dict(card)

    assert data["name"] == "iac-code"
    assert data["supportedInterfaces"][0]["protocolVersion"] == "1.0"
    assert data["supportedInterfaces"][0]["protocolBinding"] == "JSONRPC"
    assert data["supportedInterfaces"][0]["url"] == "http://127.0.0.1:41242/"
    assert data["capabilities"]["streaming"] is True
    assert data["capabilities"]["pushNotifications"] is False
    assert any(skill["id"] == "iac_generation" for skill in data["skills"])


def test_agent_card_advertises_supported_input_mime_modes() -> None:
    card = build_agent_card(host="127.0.0.1", port=41242, token_enabled=False)
    data = agent_card_to_dict(card)

    assert data["defaultInputModes"] == [
        "text/plain",
        "application/json",
        "text/markdown",
        "text/yaml",
        "application/yaml",
        "application/x-yaml",
        "image/png",
        "image/jpeg",
        "image/webp",
        "image/gif",
        "audio/mpeg",
        "audio/wav",
        "audio/ogg",
        "application/octet-stream",
    ]
    assert data["defaultOutputModes"] == ["text/plain"]
    assert all(skill["inputModes"] == data["defaultInputModes"] for skill in data["skills"])
    assert all(skill["outputModes"] == ["text/plain"] for skill in data["skills"])


def test_agent_card_advertises_optional_iac_code_extension() -> None:
    card = build_agent_card(host="127.0.0.1", port=41242, token_enabled=False)
    data = agent_card_to_dict(card)

    extension = _extension_by_uri(data, "urn:iac-code:a2a:artifact-metadata:v1")
    assert extension["uri"] == "urn:iac-code:a2a:artifact-metadata:v1"
    assert extension.get("required", False) is False


def test_agent_card_advertises_pipeline_events_extension() -> None:
    card = build_agent_card(host="127.0.0.1", port=41242, token_enabled=False)
    data = agent_card_to_dict(card)

    extension = _extension_by_uri(data, PIPELINE_EVENTS_EXTENSION_URI)
    assert extension.get("required", False) is False
    assert extension["params"] == PIPELINE_EVENTS_EXTENSION_PARAMS


def test_agent_card_marks_pipeline_events_extension_enabled_in_pipeline_mode(monkeypatch) -> None:
    monkeypatch.setenv("IAC_CODE_MODE", "pipeline")
    card = build_agent_card(host="127.0.0.1", port=41242, token_enabled=False)
    data = agent_card_to_dict(card)

    extension = _extension_by_uri(data, PIPELINE_EVENTS_EXTENSION_URI)
    assert extension["params"]["enabled"] is True
    assert extension["params"]["mode"] == "pipeline"


def test_agent_card_uses_canonical_pipeline_events_extension_when_caller_provides_duplicate() -> None:
    card = build_agent_card(
        host="127.0.0.1",
        port=41242,
        token_enabled=False,
        agent_extensions=[
            {
                "uri": PIPELINE_EVENTS_EXTENSION_URI,
                "description": "caller-provided stale pipeline extension",
                "required": True,
                "params": {"schemaVersion": "0.1", "supportsSnapshot": False},
            }
        ],
    )
    data = agent_card_to_dict(card)

    extensions = _extensions_by_uri(data, PIPELINE_EVENTS_EXTENSION_URI)
    assert len(extensions) == 1
    extension = extensions[0]
    assert extension.get("required", False) is False
    assert extension["params"] == PIPELINE_EVENTS_EXTENSION_PARAMS


def test_agent_card_advertises_enabled_thinking_exposure_types() -> None:
    card = build_agent_card(
        host="127.0.0.1",
        port=41242,
        token_enabled=False,
        thinking_exposure_types=[A2AExposureType.RAW_THINKING, A2AExposureType.TOOL_TRACE],
    )
    data = agent_card_to_dict(card)

    extension = _extension_by_uri(data, "urn:iac-code:a2a:thinking-exposure:v1")
    assert extension["uri"] == "urn:iac-code:a2a:thinking-exposure:v1"
    assert extension["params"]["enabledTypes"] == ["raw_thinking", "tool_trace"]
    assert extension.get("required", False) is False


def test_agent_card_accepts_required_extensions() -> None:
    card = build_agent_card(
        host="127.0.0.1",
        port=41242,
        token_enabled=False,
        agent_extensions=[
            {"uri": "urn:iac-code:test-required", "description": "test required extension", "required": True}
        ],
    )
    data = agent_card_to_dict(card)

    extension = _extension_by_uri(data, "urn:iac-code:test-required")
    assert extension["required"] is True


def test_agent_card_lists_enabled_runtime_interfaces() -> None:
    card = build_agent_card(
        host="127.0.0.1",
        port=41242,
        token_enabled=False,
        supported_interfaces=[
            {"url": "unix:///tmp/iac-code.sock", "protocolBinding": "unix", "protocolVersion": "1.0"},
            {"url": "ws://127.0.0.1:41243/a2a", "protocolBinding": "websocket", "protocolVersion": "1.0"},
        ],
    )
    data = agent_card_to_dict(card)

    assert data["supportedInterfaces"][0]["protocolBinding"] == "unix"
    assert data["supportedInterfaces"][1]["protocolBinding"] == "websocket"


def test_agent_card_can_advertise_jsonrpc_rest_and_grpc_interfaces() -> None:
    card = build_agent_card(
        host="127.0.0.1",
        port=41242,
        token_enabled=False,
        supported_interfaces=[
            {"url": "http://127.0.0.1:41242/", "protocolBinding": "JSONRPC", "protocolVersion": "1.0"},
            {"url": "http://127.0.0.1:41242", "protocolBinding": "HTTP+JSON", "protocolVersion": "1.0"},
            {"url": "grpc://127.0.0.1:41243", "protocolBinding": "grpc", "protocolVersion": "1.0"},
        ],
    )
    data = agent_card_to_dict(card)

    assert [item["protocolBinding"] for item in data["supportedInterfaces"]] == ["JSONRPC", "HTTP+JSON", "grpc"]


def test_agent_card_advertises_bearer_auth_only_when_enabled() -> None:
    unauthenticated = build_agent_card(host="127.0.0.1", port=41242, token_enabled=False)
    unauth_data = agent_card_to_dict(unauthenticated)
    assert "securityRequirements" not in unauth_data
    assert "securitySchemes" not in unauth_data
    assert "trusted local environments" in unauth_data["description"]

    authenticated = build_agent_card(host="127.0.0.1", port=41242, token_enabled=True)
    auth_data = agent_card_to_dict(authenticated)
    assert auth_data["securityRequirements"][0]["schemes"]["bearerAuth"]["list"] == [""]
    assert auth_data["securitySchemes"]["bearerAuth"]["httpAuthSecurityScheme"]["scheme"] == "bearer"


def test_agent_card_advertises_basic_auth_when_enabled() -> None:
    card = build_agent_card(host="127.0.0.1", port=41242, token_enabled=False, basic_enabled=True)
    data = agent_card_to_dict(card)

    assert data["securityRequirements"][0]["schemes"]["basicAuth"]["list"] == [""]
    assert data["securitySchemes"]["basicAuth"]["httpAuthSecurityScheme"]["scheme"] == "basic"


def test_agent_card_advertises_api_key_auth_when_enabled() -> None:
    card = build_agent_card(
        host="127.0.0.1",
        port=41242,
        token_enabled=False,
        api_key_enabled=True,
        api_key_header="X-IAC-Code-Key",
    )
    data = agent_card_to_dict(card)

    assert data["securityRequirements"][0]["schemes"]["apiKeyAuth"]["list"] == [""]
    scheme = data["securitySchemes"]["apiKeyAuth"]["apiKeySecurityScheme"]
    assert scheme["location"] == "header"
    assert scheme["name"] == "X-IAC-Code-Key"


def test_agent_card_can_include_signature() -> None:
    card = build_agent_card(host="127.0.0.1", port=41242, token_enabled=False, signing_secret="s" * 32)
    data = agent_card_to_dict(card)

    assert data["signatures"][0]["protected"]


def test_agent_card_advertises_standard_push_config_when_enabled() -> None:
    card = build_agent_card(host="127.0.0.1", port=41242, token_enabled=False, push_notifications=True)
    data = agent_card_to_dict(card)

    assert data["capabilities"]["pushNotifications"] is True
