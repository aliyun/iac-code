from __future__ import annotations

import importlib.util
import json
import os
import sys
from types import SimpleNamespace

import pytest


def _runner():
    spec = importlib.util.spec_from_file_location(
        "start_chat_permission_wait_runner",
        "scripts/a2a/e2e/permission_wait/run_start_chat_permission_wait.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _ros_agent_bridge():
    spec = importlib.util.spec_from_file_location(
        "permission_wait_ros_agent_bridge",
        "skills/alicloud-ros-agent/scripts/ros_agent.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_real_runner_requires_explicit_cloud_opt_in() -> None:
    runner = _runner()

    with pytest.raises(SystemExit, match="--allow-real-cloud"):
        runner.run(SimpleNamespace(allow_real_cloud=False))


def test_real_runner_allows_a_full_real_pipeline_turn_by_default(monkeypatch, tmp_path) -> None:
    runner = _runner()
    monkeypatch.setattr(
        sys,
        "argv",
        ["runner", "--run-dir", str(tmp_path / "run"), "--mode", "pipeline"],
    )

    args = runner._parse_args()

    assert args.qoder_turn_timeout == 900.0
    assert (args.resident_timeout_seconds, args.sub_pipeline_timeout_seconds, args.timeout_grace_seconds) == (
        300.0,
        300.0,
        30.0,
    )
    assert args.skill_root == [runner.Path("~/.qoder/skills"), runner.Path("~/.qoderwork/skills")]


def test_real_runner_explicit_skill_root_does_not_also_install_defaults(monkeypatch, tmp_path) -> None:
    runner = _runner()
    root = tmp_path / "skills"
    monkeypatch.setattr(
        sys,
        "argv",
        ["runner", "--run-dir", str(tmp_path / "run"), "--mode", "normal", "--skill-root", str(root)],
    )

    args = runner._parse_args()

    assert args.skill_root == [root]


def test_real_runner_places_manager_paths_under_python_temp_root(monkeypatch, tmp_path) -> None:
    runner = _runner()
    bridge = _ros_agent_bridge()
    python_temp = tmp_path / "var" / "folders" / "session" / "T"
    monkeypatch.setattr(runner.tempfile, "gettempdir", lambda: str(python_temp))

    manager_root = runner._manager_runtime_root("pwait-normal-test")
    workspace = manager_root / "qoder-workspace"
    state_root = manager_root / "ros-agent-state"
    workspace.mkdir(parents=True)
    state_root.mkdir()
    monkeypatch.setenv("ALICLOUD_ROS_AGENT_STATE_DIR", str(state_root))

    assert manager_root == python_temp.resolve() / "iac-code-a2a-e2e-manager" / "pwait-normal-test"
    assert manager_root.is_relative_to(python_temp.resolve())
    assert bridge._trusted_manager_workspace(str(workspace)) == workspace.resolve()
    assert bridge._state_root() == state_root.resolve()


def test_real_runner_writes_fixed_start_chat_permission_policy(tmp_path) -> None:
    runner = _runner()
    path = tmp_path / "a2a.yml"

    runner._a2a_config(
        path,
        port=4567,
        persistence=tmp_path / "state",
        artifacts=tmp_path / "artifacts",
    )

    text = path.read_text(encoding="utf-8")
    assert "auto_approve_permissions: false" in text
    assert "resident_timeout_seconds: 300" in text
    assert "sub_pipeline_timeout_seconds: 300" in text
    assert "timeout_grace_seconds: 30" in text


def test_real_runner_can_shorten_permission_policy_for_diagnostic_runs(tmp_path) -> None:
    runner = _runner()
    path = tmp_path / "a2a.yml"

    runner._a2a_config(
        path,
        port=4567,
        persistence=tmp_path / "state",
        artifacts=tmp_path / "artifacts",
        resident_timeout_seconds=2,
        sub_pipeline_timeout_seconds=3,
        timeout_grace_seconds=1,
    )

    text = path.read_text(encoding="utf-8")
    assert "resident_timeout_seconds: 2" in text
    assert "sub_pipeline_timeout_seconds: 3" in text
    assert "timeout_grace_seconds: 1" in text


@pytest.mark.parametrize("mode", ["normal", "pipeline"])
def test_real_runner_installs_skill_with_only_the_requested_mode(tmp_path, mode) -> None:
    runner = _runner()
    repo_root = tmp_path / "repo"
    source = repo_root / "skills" / "alicloud-ros-agent"
    (source / "agents").mkdir(parents=True)
    (source / "scripts").mkdir()
    (source / "SKILL.md").write_text("skill", encoding="utf-8")
    (source / "agents" / "openai.yaml").write_text("name: test\n", encoding="utf-8")
    (source / "scripts" / "ros_agent.py").write_text("# test\n", encoding="utf-8")
    root = tmp_path / "skills"

    backups = runner._sync_skill(repo_root, [root], "127.0.0.1:56124", mode=mode)

    destination = root / "alicloud-ros-agent"
    config = json.loads((destination / "config.json").read_text(encoding="utf-8"))
    assert config["allowedAgentModes"] == [mode]
    assert config["endpoint"] == "127.0.0.1:56124"
    assert backups[0].destination == destination
    assert backups[0].existed is False

    runner._restore_skill_installations(backups)

    assert not destination.exists()


def test_real_runner_restores_the_complete_existing_skill_installation(tmp_path) -> None:
    runner = _runner()
    repo_root = tmp_path / "repo"
    source = repo_root / "skills" / "alicloud-ros-agent"
    (source / "agents").mkdir(parents=True)
    (source / "scripts").mkdir()
    (source / "SKILL.md").write_text("new skill", encoding="utf-8")
    (source / "agents" / "openai.yaml").write_text("name: new\n", encoding="utf-8")
    (source / "scripts" / "ros_agent.py").write_text("# new\n", encoding="utf-8")
    root = tmp_path / "skills"
    destination = root / "alicloud-ros-agent"
    (destination / "agents").mkdir(parents=True)
    (destination / "scripts").mkdir()
    (destination / "SKILL.md").write_text("old skill", encoding="utf-8")
    (destination / "agents" / "legacy.yml").write_text("legacy\n", encoding="utf-8")
    (destination / "scripts" / "legacy.py").write_text("# legacy\n", encoding="utf-8")
    (destination / "config.json").write_text('{"endpoint":"old"}\n', encoding="utf-8")

    backups = runner._sync_skill(repo_root, [root], "127.0.0.1:56124", mode="normal")
    runner._restore_skill_installations(backups)

    assert (destination / "SKILL.md").read_text(encoding="utf-8") == "old skill"
    assert (destination / "agents" / "legacy.yml").read_text(encoding="utf-8") == "legacy\n"
    assert (destination / "scripts" / "legacy.py").read_text(encoding="utf-8") == "# legacy\n"
    assert not (destination / "agents" / "openai.yaml").exists()
    assert not (destination / "scripts" / "ros_agent.py").exists()
    assert json.loads((destination / "config.json").read_text(encoding="utf-8")) == {"endpoint": "old"}


def test_real_runner_rolls_back_the_complete_skill_when_sync_fails(monkeypatch, tmp_path) -> None:
    runner = _runner()
    repo_root = tmp_path / "repo"
    source = repo_root / "skills" / "alicloud-ros-agent"
    (source / "agents").mkdir(parents=True)
    (source / "scripts").mkdir()
    (source / "SKILL.md").write_text("new skill", encoding="utf-8")
    (source / "agents" / "openai.yaml").write_text("name: new\n", encoding="utf-8")
    (source / "scripts" / "ros_agent.py").write_text("# new\n", encoding="utf-8")
    root = tmp_path / "skills"
    destination = root / "alicloud-ros-agent"
    destination.mkdir(parents=True)
    (destination / "SKILL.md").write_text("old skill", encoding="utf-8")
    monkeypatch.setattr(
        runner,
        "_write_json",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("injected config write failure")),
    )

    with pytest.raises(OSError, match="injected"):
        runner._sync_skill(repo_root, [root], "127.0.0.1:56124", mode="normal")

    assert (destination / "SKILL.md").read_text(encoding="utf-8") == "old skill"
    assert not (destination / "agents").exists()
    assert not (destination / "scripts").exists()


def test_real_runner_refreshes_selected_source_before_copying(monkeypatch, tmp_path) -> None:
    runner = _runner()
    source = tmp_path / "source"
    source.mkdir()
    (source / ".cloud-credentials.yml").write_text(
        "aliyun:\n  mode: OAuth\n  oauth_site_type: CN\n  oauth_access_token: access\n  oauth_refresh_token: refresh\n",
        encoding="utf-8",
    )
    observed = []

    from iac_code.services.providers.aliyun import AliyunCredentials

    def refresh(credential):
        observed.append((credential.mode, os.environ.get("IAC_CODE_CONFIG_DIR")))
        return credential

    monkeypatch.setattr(AliyunCredentials, "refresh_oauth_if_needed", staticmethod(refresh))
    monkeypatch.setenv("IAC_CODE_CONFIG_DIR", str(tmp_path / "original"))

    runner._refresh_source_cloud_credentials(source)

    assert observed == [("OAuth", str(source))]
    assert os.environ["IAC_CODE_CONFIG_DIR"] == str(tmp_path / "original")


def test_real_runner_auto_allows_incidental_tools_but_keeps_cloud_mutations_interactive(tmp_path) -> None:
    runner = _runner()
    settings = tmp_path / "settings.yml"
    settings.write_text("model: qwen\npermissions:\n  ask:\n    - bash\n", encoding="utf-8")

    runner._configure_isolated_permissions(tmp_path)

    import yaml

    value = yaml.safe_load(settings.read_text(encoding="utf-8"))
    assert value["model"] == "qwen"
    assert value["permissions"] == {
        "mode": "default",
        "allow": [
            "read_file",
            "write_file",
            "edit_file",
            "list_files",
            "glob",
            "grep",
            "web_fetch",
            "read_memory",
            "write_memory",
            "task_list",
            "task_get",
            "task_stop",
            "agent",
            "skill",
            "aliyun_doc_search",
            "aliyun_api_doc",
            "ros_validate_template",
            "ros_get_template_parameter_constraints",
            "ros_preview_template",
            "ros_estimate_template_cost",
            "infraguard_scan",
            "ask_user_question",
            "show_architecture_diagram",
            "show_candidate_detail",
            "complete_step",
        ],
        "deny": ["bash(*)"],
        "ask": [
            "aliyun_api",
            "ros_deploy",
            "ros_stack_group",
            "ros_template",
            "ros_template_scratch",
            "ros_diagnostic",
            "ros_resource_type_registration",
            "ros_tag",
            "ros_stack",
            "ros_stack_instances",
        ],
        "additional_directories": [],
        "audit": {
            "include_tool_input": False,
            "max_file_bytes": 10 * 1024 * 1024,
            "max_files": 5,
        },
    }


@pytest.mark.asyncio
async def test_real_runner_ros_deploy_write_requires_confirmation_in_default_mode(tmp_path) -> None:
    runner = _runner()
    settings = tmp_path / "settings.yml"
    settings.write_text("model: qwen\n", encoding="utf-8")
    stack_name = "pwait-pipeline-1234-stack"
    runner._configure_isolated_permissions(tmp_path)

    from iac_code.pipeline.selling.tools.ros_deploy_tool import RosDeployTool
    from iac_code.services.permissions.loader import load_permission_context
    from iac_code.services.permissions.pipeline import check_tool_permission

    previous = os.environ.get("IAC_CODE_CONFIG_DIR")
    os.environ["IAC_CODE_CONFIG_DIR"] = str(tmp_path)
    try:
        context = load_permission_context(str(tmp_path))
    finally:
        if previous is None:
            os.environ.pop("IAC_CODE_CONFIG_DIR", None)
        else:
            os.environ["IAC_CODE_CONFIG_DIR"] = previous
    result = await check_tool_permission(
        RosDeployTool(),
        {
            "action": "create",
            "stack_name": stack_name,
            "template_url": "template.yml",
            "region_id": "cn-hangzhou",
        },
        context,
    )

    assert result.behavior == "ask"
    assert result.audit is not None
    assert result.audit.rule == "ros_deploy"


def test_real_runner_uses_repository_prompt_with_run_scoped_names() -> None:
    runner = _runner()

    prompt = runner._prompt_section(
        "Deployment",
        {
            "run_id": "pwait-normal-1234",
            "stack_name": "pwait-normal-1234-stack",
            "vswitch_name": "pwait-normal-1234-vsw",
            "mode": "Normal",
            "mode_arg": "normal",
            "state_dir": "/tmp/pwait-normal-state",
        },
    )

    assert "alicloud-ros-agent Skill" in prompt
    assert "pwait-normal-1234-stack" in prompt
    assert "pwait-normal-1234-vsw" in prompt
    assert "Mermaid" in prompt
    assert "不要创建或删除 VPC" in prompt
    assert "ALICLOUD_ROS_AGENT_STATE_DIR=/tmp/pwait-normal-state" in prompt
    assert "--mode normal" in prompt
    assert "一次 readiness `check`" in prompt
    assert "整个测试只能执行一次 managed `start`" in prompt
    pipeline_prompt = runner._prompt_section(
        "Deployment",
        {
            "run_id": "pwait-pipeline-1234",
            "stack_name": "pwait-pipeline-1234-stack",
            "vswitch_name": "pwait-pipeline-1234-vsw",
            "mode": "Pipeline",
            "mode_arg": "pipeline",
            "state_dir": "/tmp/pwait-pipeline-state",
        },
    )
    assert "恰好两个" in pipeline_prompt
    assert "不同可用区" in pipeline_prompt
    assert "--mode pipeline" in pipeline_prompt


def test_real_runner_refuses_incomplete_read_only_unknown_and_out_of_scope_permissions(tmp_path) -> None:
    runner = _runner()

    for is_read_only in (True, None):
        with pytest.raises(AssertionError, match="non-read-only"):
            runner._validate_permission_scope(
                {"isReadOnly": is_read_only, "effect": "cloud_change", "target": "run-stack"},
                "run-stack",
                tmp_path,
            )
    with pytest.raises(AssertionError, match="non-read-only"):
        runner._validate_permission_scope(
            {"effect": "cloud_change", "target": "run-stack"},
            "run-stack",
            tmp_path,
        )
    for effect in (None, "local_execution", "read"):
        with pytest.raises(AssertionError, match="effect"):
            runner._validate_permission_scope(
                {"isReadOnly": False, "effect": effect, "target": "run-stack"},
                "run-stack",
                tmp_path,
            )
    with pytest.raises(AssertionError, match="non-empty"):
        runner._validate_permission_scope(
            {"isReadOnly": False, "effect": "cloud_change", "target": ""},
            "run-stack",
            tmp_path,
        )
    with pytest.raises(AssertionError, match="outside"):
        runner._validate_permission_scope(
            {"isReadOnly": False, "effect": "cloud_change", "target": "other-stack"},
            "run-stack",
            tmp_path,
        )
    with pytest.raises(AssertionError, match="outside"):
        runner._validate_permission_scope(
            {"isReadOnly": False, "effect": "cloud_change", "target": "other-run-stack-shadow"},
            "run-stack",
            tmp_path,
        )
    runner._validate_permission_scope(
        {"isReadOnly": False, "effect": "cloud_change", "target": "ros CreateStack; stack run-stack"},
        "run-stack",
        tmp_path,
    )
    runner._validate_permission_scope(
        {"isReadOnly": False, "effect": "cloud_change", "target": "ros DeleteStack; stack stack-123"},
        "run-stack",
        tmp_path,
        allowed_stack_ids={"stack-123"},
    )
    with pytest.raises(AssertionError, match="escapes"):
        runner._validate_permission_scope(
            {"isReadOnly": False, "effect": "file_change", "target": "../outside.yml"},
            "run-stack",
            tmp_path,
        )
    inside = tmp_path / "template.yml"
    runner._validate_permission_scope(
        {"isReadOnly": False, "effect": "file_change", "target": inside.name},
        "run-stack",
        tmp_path,
    )


def test_real_runner_cleans_only_exact_inventory_vswitch_ids(monkeypatch) -> None:
    runner = _runner()
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(runner.subprocess, "run", run)

    assert (
        runner._cleanup_exact_vswitches(
            "aliyun",
            "cn-hangzhou",
            {
                "vswitches": [
                    {"vSwitchId": "vsw-run-1", "vSwitchName": "exact-run-name"},
                    {"vSwitchName": "missing-id"},
                ]
            },
        )
        is True
    )

    assert calls == [
        (
            [
                "aliyun",
                "vpc",
                "DeleteVSwitch",
                "--RegionId",
                "cn-hangzhou",
                "--VSwitchId",
                "vsw-run-1",
            ],
            {"check": False, "capture_output": True, "timeout": 60},
        )
    ]


def test_real_runner_treats_failed_exact_vswitch_delete_as_retryable(monkeypatch) -> None:
    runner = _runner()
    monkeypatch.setattr(runner.subprocess, "run", lambda *_args, **_kwargs: SimpleNamespace(returncode=1))

    assert (
        runner._cleanup_exact_vswitches(
            "aliyun",
            "cn-hangzhou",
            {"vswitches": [{"vSwitchId": "vsw-run-1", "vSwitchName": "exact-run-name"}]},
        )
        is False
    )


def test_real_runner_records_local_and_shared_checkpoint_before_answer(tmp_path) -> None:
    runner = _runner()
    config_dir = tmp_path / "config"
    shared_root = tmp_path / "shared"
    relative = "projects/project/session/permission-waits/pwb_12345678.json"
    checkpoint = {
        "boundaryId": "pwb_12345678",
        "inputId": "permission-1",
        "permissionClass": "normal",
        "phase": "WAITING",
        "generation": 3,
    }
    for root in (config_dir, shared_root):
        path = root / relative
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(checkpoint), encoding="utf-8")
    job = {
        "inputRequired": {
            "inputId": "permission-1",
            "kind": "permission",
            "permissionClass": "normal",
            "toolName": "ros_stack",
            "isReadOnly": False,
            "effect": "cloud_change",
        }
    }

    observation = runner._safe_permission_observation(
        job=job,
        config_dir=config_dir,
        shared_root=shared_root,
        observed_at=123.0,
    )

    assert observation == {
        "observedAt": 123.0,
        "inputId": "permission-1",
        "kind": "permission",
        "permissionClass": "normal",
        "toolName": "ros_stack",
        "toolUseId": None,
        "isReadOnly": False,
        "effect": "cloud_change",
        "optionCount": 0,
        "localCheckpoint": True,
        "sharedCheckpoint": True,
        "checkpointPhase": "WAITING",
        "checkpointGeneration": 3,
    }


def test_real_runner_proves_read_only_cloud_execution_from_tool_use_and_result(tmp_path) -> None:
    runner = _runner()
    session_dir = tmp_path / "config" / "projects" / "project" / "session"
    session_dir.mkdir(parents=True)
    tool_use_id = "tool-describe-vpcs"
    (session_dir / "session.jsonl").write_text(
        json.dumps(
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": tool_use_id,
                        "name": "aliyun_api",
                        "input": {"product": "vpc", "action": "DescribeVpcs"},
                    }
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    with (session_dir / "session.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": tool_use_id,
                            "content": "bounded result",
                            "is_error": False,
                        }
                    ],
                }
            )
            + "\n",
        )

    assert runner._read_only_cloud_execution_evidence(tmp_path / "config") == [
        {
            "toolUseId": tool_use_id,
            "product": "vpc",
            "action": "DescribeVpcs",
            "source": "session_transcript",
            "resultPersisted": True,
        }
    ]


def test_real_runner_removes_copied_credentials_and_full_transcripts(tmp_path) -> None:
    runner = _runner()
    config_dir = tmp_path / "config"
    session = config_dir / "projects" / "project" / "session" / "session.jsonl"
    session.parent.mkdir(parents=True)
    session.write_text("full transcript\n", encoding="utf-8")
    (config_dir / ".credentials.yml").write_text("model-secret\n", encoding="utf-8")
    (config_dir / ".cloud-credentials.yml").write_text("cloud-secret\n", encoding="utf-8")
    (config_dir / "settings.yml").write_text("model: test\n", encoding="utf-8")

    runner._remove_sensitive_run_data(config_dir)

    assert not session.exists()
    assert not (config_dir / ".credentials.yml").exists()
    assert not (config_dir / ".cloud-credentials.yml").exists()
    assert (config_dir / "settings.yml").is_file()


def test_real_runner_requires_diagram_to_precede_same_turn_permission_question() -> None:
    runner = _runner()
    permission = [{"qoderTurn": 2, "permissionClass": "pipeline", "effect": "cloud_change"}]

    assert runner._architecture_preceded_deployment_permission(
        [
            {
                "turn": 2,
                "assistantMermaid": True,
                "firstMermaidBlockIndex": 1,
                "firstCloudPermissionBlockIndex": 3,
            }
        ],
        permission,
    )
    assert not runner._architecture_preceded_deployment_permission(
        [
            {
                "turn": 2,
                "assistantMermaid": True,
                "firstMermaidBlockIndex": 3,
                "firstCloudPermissionBlockIndex": 1,
            }
        ],
        permission,
    )
    assert runner._architecture_preceded_deployment_permission(
        [{"turn": 1, "assistantMermaid": True}],
        permission,
    )


def test_real_runner_records_assistant_diagram_and_cloud_permission_block_order(monkeypatch, tmp_path) -> None:
    runner = _runner()
    stdout = "\n".join(
        json.dumps(item)
        for item in (
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": "```mermaid\ngraph TD\n```"},
                    {
                        "type": "tool_use",
                        "id": "bridge-1",
                        "name": "Bash",
                        "input": {
                            "command": (
                                "ALICLOUD_ROS_AGENT_STATE_DIR=/tmp/state python3 "
                                "/repo/skills/alicloud-ros-agent/scripts/ros_agent.py "
                                "start --prompt-file request.txt --mode normal --follow"
                            )
                        },
                    },
                ]
            },
            },
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "content": json.dumps(
                                {
                                    "inputRequired": {
                                        "kind": "permission",
                                        "effect": "cloud_change",
                                        "isReadOnly": False,
                                    }
                                }
                            ),
                        }
                    ]
                },
            },
        )
    )
    captured_command = []

    def run_qoder(command, **_kwargs):
        captured_command.extend(command)
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

    monkeypatch.setattr(runner.subprocess, "run", run_qoder)
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    evidence = runner._run_qoder(
        args=SimpleNamespace(
            qoder_cli=tmp_path / "qodercli",
            qoder_config_dir=tmp_path / "qoder-config",
            qoder_turn_timeout=30,
        ),
        env={"ALICLOUD_ROS_AGENT_STATE_DIR": "/tmp/state"},
        workspace=workspace,
        session_id="session-1",
        prompt="test",
        turn=0,
        resume=False,
        run_dir=tmp_path / "run",
    )

    assert evidence["firstMermaidBlockIndex"] == 1
    assert evidence["firstCloudPermissionBlockIndex"] == 3
    assert evidence["bridgeCommandCount"] == 1
    assert evidence["bridgeManagedStart"] is True
    assert evidence["bridgeManagedStartCount"] == 1
    assert evidence["bridgeCheckCount"] == 0
    assert evidence["bridgeStateDirBound"] is True
    assert evidence["bridgeStateDirBoundCount"] == 1
    assert evidence["bridgeStartShapeOk"] is True
    assert evidence["bridgeScriptPathKinds"] == ["repository"]
    policy = captured_command[captured_command.index("--append-system-prompt") + 1]
    assert "Execute at most one ros_agent.py bridge command" in policy
    assert "managed start exactly once" in policy
    assert "ALICLOUD_ROS_AGENT_STATE_DIR=/tmp/state" in policy
