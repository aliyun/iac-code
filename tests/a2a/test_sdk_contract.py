import sys
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib


def test_a2a_sdk_server_contract_imports() -> None:
    import inspect

    from a2a.server.agent_execution import AgentExecutor, RequestContext
    from a2a.server.context import ServerCallContext
    from a2a.server.events import EventQueue
    from a2a.server.request_handlers import DefaultRequestHandler
    from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
    from a2a.server.tasks import TaskStore
    from a2a.types import (
        AgentCapabilities,
        AgentCard,
        AgentInterface,
        AgentProvider,
        AgentSkill,
        HTTPAuthSecurityScheme,
        Message,
        Part,
        Task,
        TaskState,
        TaskStatus,
        TaskStatusUpdateEvent,
    )

    assert AgentExecutor is not None
    assert RequestContext is not None
    assert ServerCallContext is not None
    assert EventQueue is not None
    assert TaskStore is not None
    assert DefaultRequestHandler is not None
    assert create_agent_card_routes is not None
    assert create_jsonrpc_routes is not None
    assert AgentCard is not None
    assert AgentCapabilities is not None
    assert AgentInterface is not None
    assert AgentProvider is not None
    assert AgentSkill is not None
    assert HTTPAuthSecurityScheme is not None
    assert Message is not None
    assert Part is not None
    assert Task is not None
    assert TaskState is not None
    assert TaskStatus is not None
    assert TaskStatusUpdateEvent is not None

    request_handler_sig = inspect.signature(DefaultRequestHandler)
    assert "agent_executor" in request_handler_sig.parameters
    assert "task_store" in request_handler_sig.parameters
    assert "agent_card" in request_handler_sig.parameters
    assert hasattr(RequestContext, "get_user_input")

    assert "text" in Part.DESCRIPTOR.fields_by_name
    assert "data" in Part.DESCRIPTOR.fields_by_name


def test_a2a_extra_includes_server_runner_dependency() -> None:
    pyproject = tomllib.loads(Path("pyproject.toml").read_text())

    a2a_extra = pyproject["project"]["optional-dependencies"]["a2a"]

    assert any(dependency.startswith("uvicorn") for dependency in a2a_extra)
    assert any("signing" in dependency for dependency in a2a_extra)


def test_runtime_transport_extras_match_dependency_errors() -> None:
    pyproject = tomllib.loads(Path("pyproject.toml").read_text())

    optional_dependencies = pyproject["project"]["optional-dependencies"]

    assert any("signing" in dependency for dependency in optional_dependencies["a2a-signing"])
    assert any(dependency.startswith("grpcio") for dependency in optional_dependencies["a2a-grpc"])
    assert any(dependency.startswith("redis") for dependency in optional_dependencies["a2a-redis"])
