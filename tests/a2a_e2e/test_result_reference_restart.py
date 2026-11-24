"""Real HTTP A2A, server replacement, backup restore, and cursor continuation.

Only model choices are scripted. Tool execution, persistence, backup/recovery,
the A2A transports, and the two server processes are production code.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
from pathlib import Path

import pytest

ORIGINAL = "".join(f"row-{index:04d} needle 中文🙂\n" for index in range(4000))
REPO = Path(__file__).resolve().parents[2]


def _runtime(options, *, restored: bool, origin: str):
    from iac_code.agent.agent_loop import AgentLoop
    from iac_code.services.agent_factory import AgentRuntime
    from iac_code.services.session_storage import SessionStorage
    from iac_code.tools.base import Tool, ToolRegistry, ToolResult
    from iac_code.tools.grep import GrepTool
    from iac_code.tools.read_file import ReadFileTool
    from iac_code.types.stream_events import (
        MessageEndEvent,
        MessageStartEvent,
        TextDeltaEvent,
        ToolUseEndEvent,
        ToolUseStartEvent,
        Usage,
    )

    class LargeFixtureTool(Tool):
        name = "large_fixture"
        description = "Return the deterministic large result for the offline test."
        input_schema = {"type": "object"}

        def is_read_only(self, input=None):
            return True

        async def execute(self, *, tool_input, context):
            return ToolResult.success(ORIGINAL)

    class Provider:
        def get_model_name(self):
            return "result-reference-restart-fixture"

        async def stream(self, messages, system, tools=None):
            results = {
                block.tool_use_id: block
                for message in messages
                if isinstance(message.content, list)
                for block in message.content
                if getattr(block, "type", None) == "tool_result"
            }
            continuation = next(
                (
                    json.loads(message.content[len("CONTINUE ") :])
                    for message in reversed(messages)
                    if isinstance(message.content, str) and message.content.startswith("CONTINUE ")
                ),
                None,
            )
            calls = []
            if continuation is not None:
                assert restored, "Continuation must run in the replacement server."
                if "read-next" not in results:
                    calls = [
                        (
                            "read-next",
                            "read_file",
                            {
                                "path": continuation["reference"],
                                "start_char": 0,
                                "max_chars": 3000,
                                "cursor": continuation["read_cursor"],
                            },
                        ),
                        (
                            "search-next",
                            "grep",
                            {
                                "path": continuation["reference"],
                                "pattern": "needle",
                                "output_mode": "positions",
                                "max_results": 2,
                                "cursor": continuation["search_cursor"],
                            },
                        ),
                    ]
                else:
                    assert all(not results[key].is_error for key in ["read-next", "search-next"])
            elif "source" not in results:
                assert not restored, "Restored requests must reuse the previous result."
                calls = [("source", "large_fixture", {})]
            elif "read-first" not in results:
                uri = re.search(r"tool-result://\S+", results["source"].content)[0]
                calls = [
                    ("read-first", "read_file", {"path": uri, "start_char": 0, "max_chars": 3000}),
                    (
                        "search-first",
                        "grep",
                        {
                            "path": uri,
                            "pattern": "needle",
                            "output_mode": "positions",
                            "max_results": 2,
                        },
                    ),
                ]
            yield MessageStartEvent(message_id="fixture-message")
            for call_id, name, arguments in calls:
                yield ToolUseStartEvent(tool_use_id=call_id, name=name)
                yield ToolUseEndEvent(tool_use_id=call_id, name=name, input=arguments)
            if not calls:
                text = "cursor continuation verified" if continuation is not None else "seed verified"
                yield TextDeltaEvent(text=text)
            yield MessageEndEvent(stop_reason="tool_use" if calls else "end_turn", usage=Usage())

    storage = SessionStorage()
    session = storage.ensure_v2_session_dir_for_new_session(options.cwd, options.session_id)
    directory = session / "tool-results"
    if not restored and origin == "pipeline":
        directory = session / "pipeline" / "transcripts" / "transcript_att_0001" / "tool-results"
    registry = ToolRegistry()
    for tool in [LargeFixtureTool(), ReadFileTool(), GrepTool()]:
        registry.register(tool)
    provider = Provider()
    loop = AgentLoop(
        provider_manager=provider,
        system_prompt="Offline cursor restart verification.",
        tool_registry=registry,
        max_turns=5,
        session_storage=storage,
        session_id=options.session_id,
        cwd=options.cwd,
        resume_messages=options.resume_messages,
        result_storage_dir=directory,
    )
    return AgentRuntime(
        agent_loop=loop,
        session_id=loop.session_id,
        tool_registry=registry,
        provider_manager=provider,
        command_registry=None,
        task_manager=None,
        memory_manager=None,
        legacy_memory_manager=None,
    )


def _serve():
    import argparse

    import uvicorn

    from iac_code.a2a import executor
    from iac_code.a2a.app import create_app

    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--restored", action="store_true")
    parser.add_argument("--origin", choices=["normal", "pipeline"], required=True)
    args = parser.parse_args()
    executor.create_agent_runtime = lambda options: _runtime(options, restored=args.restored, origin=args.origin)
    app = create_app(
        host="127.0.0.1",
        port=args.port,
        token=None,
        model="result-reference-restart-fixture",
        persistence_dir=args.root / "persistence",
        artifact_dir=args.root / "artifacts",
        auto_approve_permissions=False,
    )
    uvicorn.run(app, host="127.0.0.1", port=args.port, access_log=False)


def _results(config):
    results = {}
    for path in config.glob("projects/**/session.jsonl"):
        for line in path.read_text(encoding="utf-8").splitlines():
            message = json.loads(line)
            for block in message.get("content", []) if isinstance(message.get("content"), list) else []:
                if block.get("type") == "tool_result":
                    results[block["tool_use_id"]] = block
    return results


@pytest.mark.integration
@pytest.mark.timeout(60)
@pytest.mark.skipif(os.name != "posix", reason="POSIX owned-process restart test")
@pytest.mark.parametrize("origin", ["normal", "pipeline"])
def test_a2a_replacement_restores_result_and_continues_both_cursors(tmp_path, origin):
    from scripts.a2a.e2e.common import ManagedServer, _free_port, _write_server_config, stream_message, wait_for_server

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    root = tmp_path / "run with spaces"
    root.mkdir()
    configs = [root / "original-config", root / "replacement-config"]
    shared = root / "shared-backup"
    servers = []

    def start(index):
        config = configs[index]
        config.mkdir()
        assert not (config / "projects").exists()
        (config / "settings.yml").write_text("permissions:\n  mode: default\n", encoding="utf-8")
        port = _free_port("127.0.0.1")
        env = {
            **os.environ,
            "IAC_CODE_CONFIG_DIR": str(config),
            "IAC_CODE_CONFIG_BACKUP_DIR": str(shared),
            "IAC_CODE_MODE": "normal",
            "IAC_CODE_WIKI_AUTO_SEARCH": "false",
            "IAC_CODE_TELEMETRY_LOCAL_ONLY": "1",
            "PYTHONPATH": str(REPO / "src") + os.pathsep + str(REPO),
        }
        env.pop("IAC_CODE_CONFIG_BACKUP_TMP_DIR", None)
        command = [str(Path(__file__).resolve()), "--root", str(root), "--port", str(port), "--origin", origin]
        if index:
            command.append("--restored")
        server = ManagedServer(
            python_cmd=[sys.executable],
            config_path=_write_server_config(
                root,
                host="127.0.0.1",
                port=port,
                auto_approve_permissions=False,
            ),
            process_cwd=str(REPO),
            allowed_cwd=str(workspace),
            env=env,
            log_prefix=root / f"server-{index}",
            run_mode="normal",
            server_args=command,
        )
        servers.append(server)
        server.start()
        url = f"http://127.0.0.1:{port}"
        wait_for_server(url, owned_server=server, timeout=15)
        return server, url

    try:
        first_server, url = start(0)
        first = stream_message(
            server_url=url,
            cwd=str(workspace),
            prompt="SEED",
            name="seed",
            run_dir=root,
            timeout=20,
        )
        assert "seed verified" in first.text
        before = _results(configs[0])
        uri = re.search(r"tool-result://\S+", before["source"]["content"])[0]
        read_cursor = re.search(r"Next cursor: (\S+)", before["read-first"]["content"])[1]
        search_cursor = re.search(r"Next cursor: (\S+)", before["search-first"]["content"])[1]
        expected_start = int(re.search(r"characters \d+-(\d+)", before["read-first"]["content"])[1])
        assert list(shared.rglob("source.txt")), "The first process must publish a real shared backup."
        first_server.kill9()
        shutil.rmtree(configs[0] / "projects")
        second_server, url = start(1)
        assert first_server.process.pid != second_server.process.pid
        follow = stream_message(
            server_url=url,
            cwd=str(workspace),
            context_id=first.context_id,
            prompt="CONTINUE "
            + json.dumps(
                {
                    "reference": uri,
                    "read_cursor": read_cursor,
                    "search_cursor": search_cursor,
                }
            ),
            name="continue",
            run_dir=root,
            timeout=20,
        )
        assert "cursor continuation verified" in follow.text
        after = _results(configs[1])
        assert not after["read-next"]["is_error"] and not after["search-next"]["is_error"]
        start_char, end_char = map(int, re.search(r"characters (\d+)-(\d+)", after["read-next"]["content"]).groups())
        assert start_char == expected_start
        assert after["read-next"]["content"].split("\n\n", 1)[1] == ORIGINAL[start_char:end_char]
        assert ":3: character " in after["search-next"]["content"]
        files = list(configs[1].glob("projects/**/source.txt"))
        assert len(files) == 1 and files[0].read_text(encoding="utf-8") == ORIGINAL
        assert before["source"]["content"] == after["source"]["content"]
    finally:
        for server in servers:
            server.terminate()


if __name__ == "__main__":
    _serve()
