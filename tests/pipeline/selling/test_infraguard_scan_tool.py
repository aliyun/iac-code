from __future__ import annotations

import hashlib
import inspect
import json
import os
import signal
import subprocess
import sys
import time

import pytest

from iac_code.pipeline.selling.tools import InfraGuardScanTool
from iac_code.pipeline.selling.tools import infraguard_scan_tool as scan_module
from iac_code.tools.base import ToolContext


def _patch_scan_run(monkeypatch, fake_run):
    async def run_command(command, *, cwd, timeout_seconds, env=None):
        kwargs = {
            "capture_output": True,
            "text": True,
            "check": False,
            "cwd": cwd,
            "timeout": timeout_seconds,
        }
        parameters = inspect.signature(fake_run).parameters
        accepts_var_kwargs = any(parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in parameters.values())
        if "env" in parameters or accepts_var_kwargs:
            kwargs["env"] = env
        return fake_run(command, **kwargs)

    monkeypatch.setattr(scan_module, "_run_infraguard_command", run_command)


class TestInfraGuardScanToolMeta:
    def test_name(self):
        tool = InfraGuardScanTool()

        assert tool.name == "infraguard_scan"

    def test_input_schema_has_required_file_path(self):
        schema = InfraGuardScanTool().input_schema

        assert schema["required"] == ["file_path"]
        assert schema["properties"]["mode"]["enum"] == ["static", "preview"]
        assert schema["properties"]["blocking_severities"]["default"] == ["high"]
        assert schema["properties"]["selected_aspects"]["items"]["type"] == "string"
        assert schema["properties"]["aspect_policy_map"]["type"] == "object"

    def test_timeout_leaves_room_for_internal_structured_scan_timeout(self):
        assert InfraGuardScanTool().timeout is not None
        assert InfraGuardScanTool().timeout > scan_module._SCAN_TIMEOUT_SECONDS


@pytest.mark.asyncio
async def test_run_infraguard_command_timeout_terminates_descendant_process(tmp_path):
    ticks_path = tmp_path / "ticks.txt"
    pid_path = tmp_path / "child.pid"
    child_code = (
        "import pathlib, sys, time\n"
        "ticks = pathlib.Path(sys.argv[1])\n"
        "pathlib.Path(sys.argv[2]).write_text(str(__import__('os').getpid()), encoding='utf-8')\n"
        "while True:\n"
        "    with ticks.open('a', encoding='utf-8') as handle:\n"
        "        handle.write('x')\n"
        "    time.sleep(0.05)\n"
    )
    parent_code = (
        "import pathlib, subprocess, sys, time\n"
        "ticks = pathlib.Path(sys.argv[1])\n"
        "subprocess.Popen([sys.executable, '-c', sys.argv[3], sys.argv[1], sys.argv[2]])\n"
        "deadline = time.time() + 3\n"
        "while not ticks.exists() and time.time() < deadline:\n"
        "    time.sleep(0.01)\n"
        "time.sleep(10)\n"
    )

    child_pid = None
    try:
        with pytest.raises(subprocess.TimeoutExpired):
            await scan_module._run_infraguard_command(
                [sys.executable, "-c", parent_code, str(ticks_path), str(pid_path), child_code],
                cwd=str(tmp_path),
                timeout_seconds=0.5,
            )

        size_after_timeout = ticks_path.stat().st_size
        time.sleep(0.3)
        assert ticks_path.stat().st_size == size_after_timeout
    finally:
        if pid_path.exists():
            child_pid = int(pid_path.read_text(encoding="utf-8"))
        if child_pid is not None:
            _terminate_test_process(child_pid)


def _terminate_test_process(pid: int) -> None:
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True, check=False)
        return
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return


class TestInfraGuardScanToolRendering:
    def test_compact_result_summarizes_scan_without_raw_json(self):
        payload = {
            "command": ["infraguard", "scan", "templates/demo.yml", "--format", "json"],
            "exit_code": 1,
            "mode": "static",
            "passed": True,
            "blocking_findings": 0,
            "findings": [
                {
                    "severity": "low",
                    "rule_id": "rule:aliyun:metadata-ros-composer-check",
                    "resource_id": "",
                    "line": 5,
                }
            ],
            "summary": {
                "total_violations": 1,
                "severity_counts": {"high": 0, "low": 1, "medium": 0},
            },
            "file_path": "templates/demo.yml",
            "selected_aspects": ["best_practice", "network_architecture"],
            "expanded_policies": ["pack:aliyun:best-practice"],
        }

        rendered = InfraGuardScanTool().render_tool_result_message(json.dumps(payload), verbose=False)

        assert rendered is not None
        assert "passed" in rendered
        assert "1 finding" in rendered
        assert "blocking 0" in rendered
        assert "low 1" in rendered
        assert "templates/demo.yml" in rendered
        assert '"command"' not in rendered
        assert "snippet_lines" not in rendered

    def test_verbose_result_formats_findings_and_policies(self):
        payload = {
            "command": ["infraguard", "scan", "templates/demo.yml", "--format", "json"],
            "exit_code": 1,
            "mode": "static",
            "ignore_waivers": True,
            "passed": True,
            "blocking_findings": 0,
            "findings": [
                {
                    "severity": "low",
                    "rule_id": "rule:aliyun:metadata-ros-composer-check",
                    "resource_id": "",
                    "line": 5,
                    "reason": "Composer 缺失或格式无效。",
                    "recommendation": "使用 ROS Composer 导入模板并配置架构图。",
                }
            ],
            "summary": {
                "total_violations": 1,
                "files_scanned": 1,
                "severity_counts": {"high": 0, "low": 1, "medium": 0},
            },
            "file_path": "templates/demo.yml",
            "selected_aspects": ["best_practice", "network_architecture"],
            "expanded_policies": ["pack:aliyun:best-practice", "pack:aliyun:network-architecture"],
        }

        rendered = InfraGuardScanTool().render_tool_result_message(json.dumps(payload), verbose=True)

        assert rendered is not None
        assert "Status: passed" in rendered
        assert "File: templates/demo.yml" in rendered
        assert "Aspects: best_practice, network_architecture" in rendered
        assert "Policies:" in rendered
        assert "pack:aliyun:best-practice" in rendered
        assert "Findings:" in rendered
        assert "low · rule:aliyun:metadata-ros-composer-check · line 5" in rendered
        assert "Reason: Composer 缺失或格式无效。" in rendered
        assert '"command"' not in rendered

    def test_error_result_renders_human_readable_error_label(self):
        payload = {
            "error": "unsupported_no_waivers_flag",
            "file_path": "templates/demo.yml",
        }

        rendered = InfraGuardScanTool().render_tool_result_message(json.dumps(payload), verbose=False)

        assert rendered is not None
        assert "InfraGuard CLI does not support --no-waivers" in rendered
        assert "unsupported_no_waivers_flag" not in rendered


class TestInfraGuardScanToolExecute:
    @pytest.mark.asyncio
    async def test_passes_pipeline_scoped_env_to_infraguard_process(self, monkeypatch, tmp_path):
        template = tmp_path / "template.yaml"
        template.write_text("{}\n", encoding="utf-8")
        seen_env = {}

        def fake_run(command, capture_output, text, check, cwd=None, timeout=None, env=None):
            seen_env.update(env or {})
            return subprocess.CompletedProcess(command, 0, stdout=json.dumps({"summary": {}, "results": []}), stderr="")

        _patch_scan_run(monkeypatch, fake_run)

        result = await InfraGuardScanTool().execute(
            tool_input={"file_path": str(template), "ignore_waivers": False},
            context=ToolContext(env_overrides={"PATH": "/tmp/iac-code-infraguard/bin"}),
        )

        assert result.is_error is False
        assert seen_env["PATH"] == "/tmp/iac-code-infraguard/bin"

    @pytest.mark.asyncio
    async def test_treats_exit_two_as_scan_result(self, monkeypatch, tmp_path):
        template = tmp_path / "template.yaml"
        template.write_text("ROSTemplateFormatVersion: '2015-09-01'\n", encoding="utf-8")

        def fake_run(command, capture_output, text, check, cwd=None, timeout=None):
            assert command == [
                "infraguard",
                "scan",
                str(template),
                "--format",
                "json",
                "--mode",
                "static",
                "--policy",
                "pack:aliyun:security",
                "--no-waivers",
            ]
            assert capture_output is True
            assert text is True
            assert check is False
            assert cwd is not None
            assert timeout is not None
            return subprocess.CompletedProcess(
                command,
                2,
                stdout=json.dumps({"summary": {"high": 1}, "results": [{"severity": "high"}]}),
                stderr="",
            )

        _patch_scan_run(monkeypatch, fake_run)

        result = await InfraGuardScanTool().execute(
            tool_input={
                "file_path": str(template),
                "mode": "static",
                "policies": ["pack:aliyun:security"],
                "blocking_severities": ["high"],
            },
            context=ToolContext(),
        )

        payload = json.loads(result.content)
        assert result.is_error is False
        assert payload["exit_code"] == 2
        assert payload["blocking_findings"] == 1
        assert payload["passed"] is False
        assert payload["findings"] == [{"severity": "high"}]
        assert payload["summary"] == {"high": 1}
        assert payload["file_path"] == str(template)

    @pytest.mark.asyncio
    async def test_uses_case_insensitive_blocking_severities(self, monkeypatch, tmp_path):
        template = tmp_path / "template.yaml"
        template.write_text("{}\n", encoding="utf-8")

        def fake_run(command, capture_output, text, check, cwd=None, timeout=None):
            return subprocess.CompletedProcess(
                command,
                1,
                stdout=json.dumps({"summary": {"critical": 1}, "results": [{"severity": "CRITICAL"}]}),
                stderr="",
            )

        _patch_scan_run(monkeypatch, fake_run)

        result = await InfraGuardScanTool().execute(
            tool_input={
                "file_path": str(template),
                "ignore_waivers": False,
                "blocking_severities": ["critical"],
            },
            context=ToolContext(),
        )

        payload = json.loads(result.content)
        assert result.is_error is False
        assert payload["passed"] is False
        assert payload["blocking_findings"] == 1
        assert "--no-waivers" not in payload["command"]

    @pytest.mark.asyncio
    async def test_fails_when_ignore_waivers_requested_and_cli_does_not_support_flag(self, monkeypatch, tmp_path):
        template = tmp_path / "template.yaml"
        template.write_text("{}\n", encoding="utf-8")
        commands = []

        def fake_run(command, capture_output, text, check, cwd=None, timeout=None):
            commands.append(command)
            if "--no-waivers" in command:
                return subprocess.CompletedProcess(command, 1, stdout="", stderr="unknown flag: --no-waivers")
            return subprocess.CompletedProcess(command, 0, stdout=json.dumps({"summary": {}, "results": []}), stderr="")

        _patch_scan_run(monkeypatch, fake_run)

        result = await InfraGuardScanTool().execute(
            tool_input={"file_path": str(template), "ignore_waivers": True},
            context=ToolContext(),
        )

        payload = json.loads(result.content)
        assert result.is_error is True
        assert len(commands) == 1
        assert "--no-waivers" in commands[0]
        assert payload["error"] == "unsupported_no_waivers_flag"
        assert payload["command"] == commands[0]
        assert payload["ignore_waivers"] is True

    @pytest.mark.asyncio
    async def test_fails_when_no_waivers_is_reported_as_unknown_option_with_exit_code_two(self, monkeypatch, tmp_path):
        template = tmp_path / "template.yaml"
        template.write_text("{}\n", encoding="utf-8")

        def fake_run(command, capture_output, text, check, cwd=None, timeout=None):
            return subprocess.CompletedProcess(command, 2, stdout="", stderr="unknown option: --no-waivers")

        _patch_scan_run(monkeypatch, fake_run)

        result = await InfraGuardScanTool().execute(
            tool_input={"file_path": str(template), "ignore_waivers": True},
            context=ToolContext(),
        )

        payload = json.loads(result.content)
        assert result.is_error is True
        assert payload["error"] == "unsupported_no_waivers_flag"
        assert payload["ignore_waivers"] is True

    @pytest.mark.asyncio
    async def test_include_file_content_preserves_crlf_for_sha256_contract(self, monkeypatch, tmp_path):
        template = tmp_path / "template.yaml"
        template_content = "ROSTemplateFormatVersion: '2015-09-01'\r\nResources: {}\r\n"
        template.write_bytes(template_content.encode("utf-8"))

        def fake_run(command, capture_output, text, check, cwd=None, timeout=None):
            return subprocess.CompletedProcess(command, 0, stdout=json.dumps({"summary": {}, "results": []}), stderr="")

        _patch_scan_run(monkeypatch, fake_run)

        result = await InfraGuardScanTool().execute(
            tool_input={
                "file_path": str(template),
                "ignore_waivers": False,
                "include_file_content": True,
            },
            context=ToolContext(),
        )

        payload = json.loads(result.content)
        assert result.is_error is False
        assert payload["file_content"] == template_content
        assert payload["file_sha256"] == hashlib.sha256(template_content.encode("utf-8")).hexdigest()

    @pytest.mark.asyncio
    async def test_parses_official_top_level_violations_json(self, monkeypatch, tmp_path):
        template = tmp_path / "template.yaml"
        template.write_text("ROSTemplateFormatVersion: '2015-09-01'\n", encoding="utf-8")

        def fake_run(command, capture_output, text, check, cwd=None, timeout=None):
            return subprocess.CompletedProcess(
                command,
                2,
                stdout=json.dumps(
                    {
                        "summary": {"total": 1, "high": 1, "medium": 0, "low": 0},
                        "violations": [
                            {
                                "rule_id": "ecs-no-public-ip",
                                "severity": "high",
                                "resource_id": "MyECS",
                                "reason": "Public IP allocated",
                                "recommendation": "Use NAT Gateway instead",
                            }
                        ],
                    }
                ),
                stderr="",
            )

        _patch_scan_run(monkeypatch, fake_run)

        result = await InfraGuardScanTool().execute(
            tool_input={"file_path": str(template), "blocking_severities": ["high"]},
            context=ToolContext(),
        )

        payload = json.loads(result.content)
        assert result.is_error is False
        assert payload["passed"] is False
        assert payload["blocking_findings"] == 1
        assert payload["findings"] == [
            {
                "rule_id": "ecs-no-public-ip",
                "severity": "high",
                "resource_id": "MyECS",
                "reason": "Public IP allocated",
                "recommendation": "Use NAT Gateway instead",
            }
        ]
        assert payload["file_sha256"]

    @pytest.mark.asyncio
    async def test_summary_severity_counts_are_not_double_counted(self, monkeypatch, tmp_path):
        template = tmp_path / "template.yaml"
        template.write_text("{}\n", encoding="utf-8")

        def fake_run(command, capture_output, text, check, cwd=None, timeout=None):
            return subprocess.CompletedProcess(
                command,
                2,
                stdout=json.dumps(
                    {
                        "summary": {"high": 1, "severity_counts": {"high": 1}},
                        "violations": [{"severity": "high"}],
                    }
                ),
                stderr="",
            )

        _patch_scan_run(monkeypatch, fake_run)

        result = await InfraGuardScanTool().execute(
            tool_input={"file_path": str(template), "blocking_severities": ["high"]},
            context=ToolContext(),
        )

        payload = json.loads(result.content)
        assert result.is_error is False
        assert payload["blocking_findings"] == 1

    @pytest.mark.asyncio
    async def test_exit_code_two_with_only_non_blocking_findings_passes(self, monkeypatch, tmp_path):
        template = tmp_path / "template.yaml"
        template.write_text("{}\n", encoding="utf-8")

        def fake_run(command, capture_output, text, check, cwd=None, timeout=None):
            return subprocess.CompletedProcess(
                command,
                2,
                stdout=json.dumps(
                    {
                        "summary": {"total_violations": 1, "severity_counts": {"medium": 1}},
                        "violations": [{"severity": "medium", "rule_id": "medium-only"}],
                    }
                ),
                stderr="",
            )

        _patch_scan_run(monkeypatch, fake_run)

        result = await InfraGuardScanTool().execute(
            tool_input={"file_path": str(template), "blocking_severities": ["critical", "high"]},
            context=ToolContext(),
        )

        payload = json.loads(result.content)
        assert result.is_error is False
        assert payload["exit_code"] == 2
        assert payload["passed"] is True
        assert payload["blocking_findings"] == 0
        assert payload["findings"] == [{"severity": "medium", "rule_id": "medium-only"}]

    @pytest.mark.asyncio
    async def test_expands_selected_aspects_to_policies(self, monkeypatch, tmp_path):
        template = tmp_path / "template.yaml"
        template.write_text("{}\n", encoding="utf-8")

        def fake_run(command, capture_output, text, check, cwd=None, timeout=None):
            assert command == [
                "infraguard",
                "scan",
                str(template),
                "--format",
                "json",
                "--mode",
                "static",
                "--policy",
                "pack:aliyun:security",
                "--policy",
                "rule:aliyun:ecs-instance-no-public-ip",
                "--policy",
                "pack:aliyun:high-availability",
                "--no-waivers",
            ]
            return subprocess.CompletedProcess(command, 0, stdout=json.dumps({"summary": {}, "results": []}), stderr="")

        _patch_scan_run(monkeypatch, fake_run)

        result = await InfraGuardScanTool().execute(
            tool_input={
                "file_path": str(template),
                "selected_aspects": ["security", "high_availability"],
                "aspect_policy_map": {
                    "security": {
                        "policies": [
                            "pack:aliyun:security",
                            "rule:aliyun:ecs-instance-no-public-ip",
                        ]
                    },
                    "high_availability": {"policies": ["pack:aliyun:high-availability"]},
                },
            },
            context=ToolContext(),
        )

        payload = json.loads(result.content)
        assert result.is_error is False
        assert payload["selected_aspects"] == ["security", "high_availability"]
        assert payload["expanded_policies"] == [
            "pack:aliyun:security",
            "rule:aliyun:ecs-instance-no-public-ip",
            "pack:aliyun:high-availability",
        ]

    @pytest.mark.asyncio
    async def test_rejects_raw_policies_when_aspect_policy_map_is_present(self, tmp_path):
        result = await InfraGuardScanTool().execute(
            tool_input={
                "file_path": str(tmp_path / "template.yaml"),
                "policies": ["rule:*"],
                "selected_aspects": ["security"],
                "aspect_policy_map": {"security": {"policies": ["pack:aliyun:security"]}},
            },
            context=ToolContext(),
        )

        payload = json.loads(result.content)
        assert result.is_error is True
        assert payload["error"] == "raw_policies_not_allowed_with_aspects"

    @pytest.mark.asyncio
    async def test_requires_selected_aspects_when_aspect_policy_map_is_present(self, tmp_path):
        result = await InfraGuardScanTool().execute(
            tool_input={
                "file_path": str(tmp_path / "template.yaml"),
                "selected_aspects": [],
                "aspect_policy_map": {"security": {"policies": ["pack:aliyun:security"]}},
            },
            context=ToolContext(),
        )

        payload = json.loads(result.content)
        assert result.is_error is True
        assert payload["error"] == "selected_aspects_required"

    @pytest.mark.asyncio
    async def test_step_config_aspects_are_used_without_caller_supplied_map(self, monkeypatch, tmp_path):
        template = tmp_path / "template.yaml"
        template.write_text("{}\n", encoding="utf-8")

        def fake_run(command, capture_output, text, check, cwd=None, timeout=None):
            assert command == [
                "infraguard",
                "scan",
                str(template),
                "--format",
                "json",
                "--mode",
                "static",
                "--policy",
                "pack:aliyun:security",
                "--no-waivers",
            ]
            return subprocess.CompletedProcess(command, 0, stdout=json.dumps({"summary": {}, "results": []}), stderr="")

        _patch_scan_run(monkeypatch, fake_run)
        tool = InfraGuardScanTool(
            step_config={
                "infraguard": {
                    "mode": "static",
                    "ignore_waivers": True,
                    "aspects": {"security": {"policies": ["pack:aliyun:security"]}},
                }
            }
        )

        result = await tool.execute(
            tool_input={"file_path": str(template), "selected_aspects": ["security"]},
            context=ToolContext(),
        )

        payload = json.loads(result.content)
        assert result.is_error is False
        assert payload["selected_aspects"] == ["security"]
        assert payload["expanded_policies"] == ["pack:aliyun:security"]

    @pytest.mark.asyncio
    async def test_step_config_scan_options_override_caller_input(self, monkeypatch, tmp_path):
        template = tmp_path / "template.yaml"
        template.write_text("{}\n", encoding="utf-8")

        def fake_run(command, capture_output, text, check, cwd=None, timeout=None):
            assert command == [
                "infraguard",
                "scan",
                str(template),
                "--format",
                "json",
                "--mode",
                "static",
                "--policy",
                "pack:aliyun:security",
                "--no-waivers",
            ]
            return subprocess.CompletedProcess(
                command,
                2,
                stdout=json.dumps({"summary": {"severity_counts": {"critical": 1}}, "results": []}),
                stderr="",
            )

        _patch_scan_run(monkeypatch, fake_run)
        tool = InfraGuardScanTool(
            step_config={
                "infraguard": {
                    "mode": "static",
                    "ignore_waivers": True,
                    "blocking_severities": ["critical", "high"],
                    "aspects": {"security": {"policies": ["pack:aliyun:security"]}},
                }
            }
        )

        result = await tool.execute(
            tool_input={
                "file_path": str(template),
                "mode": "preview",
                "selected_aspects": ["security"],
                "ignore_waivers": False,
                "blocking_severities": ["high"],
            },
            context=ToolContext(),
        )

        payload = json.loads(result.content)
        assert result.is_error is False
        assert payload["mode"] == "static"
        assert payload["ignore_waivers"] is True
        assert payload["blocking_severities"] == ["critical", "high"]
        assert payload["passed"] is False
        assert payload["blocking_findings"] == 1

    @pytest.mark.asyncio
    async def test_step_config_aspect_mode_rejects_raw_policies_without_caller_map(self, tmp_path):
        tool = InfraGuardScanTool(
            step_config={"infraguard": {"aspects": {"security": {"policies": ["pack:aliyun:security"]}}}}
        )

        result = await tool.execute(
            tool_input={
                "file_path": str(tmp_path / "template.yaml"),
                "policies": ["rule:*"],
                "selected_aspects": ["security"],
            },
            context=ToolContext(),
        )

        payload = json.loads(result.content)
        assert result.is_error is True
        assert payload["error"] == "raw_policies_not_allowed_with_aspects"

    @pytest.mark.asyncio
    async def test_unknown_selected_aspect_is_tool_error(self, tmp_path):
        result = await InfraGuardScanTool().execute(
            tool_input={
                "file_path": str(tmp_path / "template.yaml"),
                "selected_aspects": ["security", "unknown"],
                "aspect_policy_map": {"security": {"policies": ["pack:aliyun:security"]}},
            },
            context=ToolContext(),
        )

        payload = json.loads(result.content)
        assert result.is_error is True
        assert payload["error"] == "unknown_policy_aspect"
        assert payload["unknown_aspects"] == ["unknown"]

    @pytest.mark.asyncio
    async def test_flattens_infraguard_v010_nested_violations(self, monkeypatch, tmp_path):
        template = tmp_path / "template.yaml"
        template.write_text("{}\n", encoding="utf-8")

        def fake_run(command, capture_output, text, check, cwd=None, timeout=None):
            return subprocess.CompletedProcess(
                command,
                2,
                stdout=json.dumps(
                    {
                        "schema_version": "2.0",
                        "summary": {"severity_counts": {"high": 1}},
                        "results": [
                            {
                                "file": str(template),
                                "violations": [
                                    {
                                        "id": "rule:aliyun:ecs-instance-no-public-ip",
                                        "severity": "high",
                                        "resource_id": "EcsGroup",
                                        "violation_path": ["Properties", "AllocatePublicIP"],
                                    }
                                ],
                            }
                        ],
                    }
                ),
                stderr="",
            )

        _patch_scan_run(monkeypatch, fake_run)

        result = await InfraGuardScanTool().execute(
            tool_input={"file_path": str(template), "blocking_severities": ["high"]},
            context=ToolContext(),
        )

        payload = json.loads(result.content)
        assert result.is_error is False
        assert payload["passed"] is False
        assert payload["blocking_findings"] == 1
        assert payload["findings"] == [
            {
                "id": "rule:aliyun:ecs-instance-no-public-ip",
                "severity": "high",
                "resource_id": "EcsGroup",
                "violation_path": ["Properties", "AllocatePublicIP"],
                "file": str(template),
            }
        ]

    @pytest.mark.asyncio
    async def test_ignores_infraguard_v010_empty_violation_groups(self, monkeypatch, tmp_path):
        template = tmp_path / "template.yaml"
        template.write_text("{}\n", encoding="utf-8")

        def fake_run(command, capture_output, text, check, cwd=None, timeout=None):
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=json.dumps(
                    {
                        "schema_version": "2.0",
                        "summary": {"severity_counts": {"high": 0}},
                        "results": [{"file": str(template), "violations": None}],
                    }
                ),
                stderr="",
            )

        _patch_scan_run(monkeypatch, fake_run)

        result = await InfraGuardScanTool().execute(
            tool_input={"file_path": str(template), "blocking_severities": ["high"]},
            context=ToolContext(),
        )

        payload = json.loads(result.content)
        assert result.is_error is False
        assert payload["passed"] is True
        assert payload["blocking_findings"] == 0
        assert payload["findings"] == []

    @pytest.mark.asyncio
    async def test_malformed_json_is_tool_error(self, monkeypatch, tmp_path):
        template = tmp_path / "template.yaml"
        template.write_text("{}\n", encoding="utf-8")

        def fake_run(command, capture_output, text, check, cwd=None, timeout=None):
            return subprocess.CompletedProcess(command, 0, stdout="{not-json", stderr="bad json")

        _patch_scan_run(monkeypatch, fake_run)

        result = await InfraGuardScanTool().execute(
            tool_input={"file_path": str(template)},
            context=ToolContext(),
        )

        payload = json.loads(result.content)
        assert result.is_error is True
        assert payload["error"] == "malformed_json"
        assert payload["exit_code"] == 0

    @pytest.mark.asyncio
    async def test_top_level_error_payload_is_tool_error(self, monkeypatch, tmp_path):
        template = tmp_path / "template.yaml"
        template.write_text("{}\n", encoding="utf-8")

        def fake_run(command, capture_output, text, check, cwd=None, timeout=None):
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=json.dumps({"error": "policy_not_found", "message": "missing policy"}),
                stderr="policy stderr",
            )

        _patch_scan_run(monkeypatch, fake_run)

        result = await InfraGuardScanTool().execute(
            tool_input={
                "file_path": str(template),
                "selected_aspects": ["security"],
                "aspect_policy_map": {"security": {"policies": ["pack:aliyun:security"]}},
            },
            context=ToolContext(),
        )

        payload = json.loads(result.content)
        assert result.is_error is True
        assert payload["error"] == "policy_not_found"
        assert payload["exit_code"] == 0
        assert payload["stderr"] == "policy stderr"
        assert payload["selected_aspects"] == ["security"]
        assert payload["expanded_policies"] == ["pack:aliyun:security"]

    @pytest.mark.asyncio
    async def test_unexpected_exit_code_is_tool_error(self, monkeypatch, tmp_path):
        template = tmp_path / "template.yaml"
        template.write_text("{}\n", encoding="utf-8")

        def fake_run(command, capture_output, text, check, cwd=None, timeout=None):
            return subprocess.CompletedProcess(command, 127, stdout="", stderr="missing binary")

        _patch_scan_run(monkeypatch, fake_run)

        result = await InfraGuardScanTool().execute(
            tool_input={"file_path": str(template)},
            context=ToolContext(),
        )

        payload = json.loads(result.content)
        assert result.is_error is True
        assert payload["error"] == "unexpected_exit_code"
        assert payload["exit_code"] == 127
        assert payload["stderr"] == "missing binary"

    @pytest.mark.asyncio
    async def test_relative_file_path_uses_context_cwd_and_preserves_payload_path(self, monkeypatch, tmp_path):
        template = tmp_path / "template.yaml"
        template.write_text("{}\n", encoding="utf-8")

        def fake_run(command, capture_output, text, check, cwd=None, timeout=None):
            assert command[:3] == ["infraguard", "scan", "template.yaml"]
            assert cwd == str(tmp_path)
            assert timeout is not None
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=json.dumps({"summary": {}, "results": []}),
                stderr="",
            )

        _patch_scan_run(monkeypatch, fake_run)

        result = await InfraGuardScanTool().execute(
            tool_input={"file_path": "template.yaml"},
            context=ToolContext(cwd=str(tmp_path)),
        )

        payload = json.loads(result.content)
        assert result.is_error is False
        assert payload["file_path"] == "template.yaml"

    @pytest.mark.asyncio
    async def test_command_not_found_is_tool_error(self, monkeypatch, tmp_path):
        def fake_run(command, capture_output, text, check, cwd=None, timeout=None):
            raise FileNotFoundError("infraguard")

        _patch_scan_run(monkeypatch, fake_run)

        result = await InfraGuardScanTool().execute(
            tool_input={"file_path": "template.yaml"},
            context=ToolContext(cwd=str(tmp_path)),
        )

        payload = json.loads(result.content)
        assert result.is_error is True
        assert payload["error"] == "command_not_found"
        assert payload["file_path"] == "template.yaml"
        assert "infraguard" in payload["stderr"]

    @pytest.mark.asyncio
    async def test_timeout_is_tool_error(self, monkeypatch, tmp_path):
        def fake_run(command, capture_output, text, check, cwd=None, timeout=None):
            raise subprocess.TimeoutExpired(command, timeout)

        _patch_scan_run(monkeypatch, fake_run)

        result = await InfraGuardScanTool().execute(
            tool_input={"file_path": "template.yaml"},
            context=ToolContext(cwd=str(tmp_path)),
        )

        payload = json.loads(result.content)
        assert result.is_error is True
        assert payload["error"] == "timeout"
        assert payload["file_path"] == "template.yaml"
        assert payload["timeout"] is not None
