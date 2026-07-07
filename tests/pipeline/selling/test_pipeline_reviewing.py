from __future__ import annotations

import os
from pathlib import Path

import yaml

from iac_code.pipeline.engine.loader import load_pipeline_dir
from iac_code.pipeline.engine.step_spec import A2AArtifactSpec, render_prompt


def _selling_dir() -> Path:
    return Path(__file__).resolve().parents[3] / "src" / "iac_code" / "pipeline" / "selling"


def _load_selling(*, enable_reviewing: bool | None = None):
    overrides = None if enable_reviewing is None else {"enable_reviewing": enable_reviewing}
    return load_pipeline_dir(_selling_dir(), feature_flag_overrides=overrides)


def test_review_enabled_loads_infraguard_repair_step_before_cost() -> None:
    loaded = _load_selling(enable_reviewing=True)

    steps = loaded.sub_pipelines["evaluate_candidate"].steps
    assert [step.step_id for step in steps] == ["template_generating", "reviewing", "cost_estimating"]

    template_step, review_step, cost_step = steps
    assert review_step.enabled_when == "enable_reviewing"
    assert review_step.conclusion_field == "template"
    assert review_step.forward == "cost_estimating"
    assert review_step.skill == "iac-aliyun-review"
    assert review_step.prompt_file == "prompts/reviewing.md"
    assert review_step.context_fields == ["intent", "candidate", "template"]
    assert cost_step.context_fields == ["template"]

    assert review_step.tools is not None
    assert review_step.tools.include == [
        "read_file",
        "write_file",
        "edit_file",
        "infraguard_scan",
        "ros_validate_template",
        "aliyun_doc_search",
    ]
    assert review_step.tools.exclude == ["write_memory"]
    assert review_step.inject_tools == ["ros_validate_template"]

    infraguard_config = review_step.config["infraguard"]
    assert infraguard_config["mode"] == "static"
    assert infraguard_config["ignore_waivers"] is True
    assert infraguard_config["max_fix_rounds"] == 5
    assert infraguard_config["blocking_severities"] == ["critical", "high"]
    assert list(infraguard_config["aspects"]) == [
        "security",
        "high_availability",
        "cost_optimization",
        "compliance",
        "best_practice",
        "operations",
        "network_architecture",
        "elasticity",
    ]
    assert {key: value["policies"] for key, value in infraguard_config["aspects"].items()} == {
        "security": ["pack:aliyun:security"],
        "high_availability": ["pack:aliyun:high-availability"],
        "cost_optimization": ["pack:aliyun:cost-optimization"],
        "compliance": ["pack:aliyun:compliance"],
        "best_practice": ["pack:aliyun:best-practice"],
        "operations": ["pack:aliyun:operations"],
        "network_architecture": ["pack:aliyun:network-architecture"],
        "elasticity": ["pack:aliyun:elasticity"],
    }
    assert template_step.a2a_artifacts == [
        A2AArtifactSpec(
            path="conclusion.file_path",
            content="conclusion.template",
            media_type="auto",
            role="intermediate",
            supersedes_path="conclusion.file_path",
        )
    ]
    assert review_step.a2a_artifacts == [
        A2AArtifactSpec(
            path="conclusion.file_path",
            content="conclusion.template",
            media_type="auto",
            role="final",
            supersedes_path="conclusion.file_path",
        )
    ]


def test_review_step_schema_and_completion_guards_allow_clean_scan_shortcut() -> None:
    loaded = _load_selling(enable_reviewing=True)
    review_step = loaded.sub_pipelines["evaluate_candidate"].steps[1]

    assert review_step.conclusion_schema == {
        "type": "object",
        "required": [
            "template",
            "template_sha256",
            "file_path",
            "region",
            "description",
            "validated",
            "review_passed",
            "review_issues",
            "selected_review_aspects",
            "skipped_review_aspects",
            "resolved_infraguard_policies",
            "infraguard_summary",
            "fix_summary",
        ],
        "additionalProperties": False,
        "properties": {
            "template": {"type": "string"},
            "template_sha256": {"type": "string"},
            "file_path": {"type": "string"},
            "region": {"type": "string"},
            "description": {"type": "string"},
            "validated": {"const": True},
            "review_passed": {"const": True},
            "review_issues": {"type": "array"},
            "selected_review_aspects": {"type": "array"},
            "skipped_review_aspects": {"type": "array"},
            "resolved_infraguard_policies": {"type": "array"},
            "infraguard_summary": {"type": "object"},
            "fix_summary": {"type": "string"},
        },
    }
    assert review_step.completion_guards == [
        {
            "when_conclusion_field_equals": {"validated": True},
            "when_tool_result_exists": {
                "tools": ["write_file", "edit_file"],
                "match_conclusion_field": "file_path",
                "match_result_field": "result.file_path",
            },
            "require_tool_result": {
                "tool": "ros_validate_template",
                "match_conclusion_field": "file_path",
                "match_result_field": "input.template_url",
                "disallow_tool_results_after_match": [
                    {
                        "tools": ["write_file", "edit_file"],
                        "match_conclusion_field": "file_path",
                        "match_result_field": "result.file_path",
                        "message_key": "reviewing_rerun_after_validate_template_write",
                    }
                ],
            },
            "message_key": "reviewing_validate_template_required",
        },
        {
            "when_conclusion_field_equals": {"review_passed": True},
            "require_tool_result": {
                "tool": "infraguard_scan",
                "latest_match": True,
                "match_conclusion_field": "file_path",
                "match_result_field": "file_path",
                "match_fields": [{"conclusion_field": "template_sha256", "result_field": "file_sha256"}],
                "result_field_equals": {"passed": True, "blocking_findings": 0},
                "required_result_fields": ["file_content", "file_sha256", "selected_aspects", "expanded_policies"],
                "disallow_tool_results_after_match": [
                    {
                        "tools": ["write_file", "edit_file"],
                        "match_conclusion_field": "file_path",
                        "match_result_field": "result.file_path",
                        "message_key": "reviewing_rerun_after_final_infraguard_write",
                    }
                ],
            },
            "require_conclusion_sha256": {
                "content_field": "template",
                "sha256_field": "template_sha256",
            },
            "message_key": "reviewing_final_infraguard_required",
        },
        {
            "when_conclusion_field_equals": {"review_passed": True},
            "when_tool_result_exists": {
                "tools": ["write_file", "edit_file"],
                "match_conclusion_field": "file_path",
                "match_result_field": "result.file_path",
            },
            "require_tool_result": {
                "tool": "infraguard_scan",
                "latest_match": True,
                "after_tool_result": {
                    "tool": "ros_validate_template",
                    "match_conclusion_field": "file_path",
                    "match_result_field": "input.template_url",
                },
                "match_conclusion_field": "file_path",
                "match_result_field": "file_path",
                "match_fields": [{"conclusion_field": "template_sha256", "result_field": "file_sha256"}],
                "result_field_equals": {"passed": True, "blocking_findings": 0},
                "required_result_fields": ["file_content", "file_sha256", "selected_aspects", "expanded_policies"],
                "disallow_tool_results_after_match": [
                    {
                        "tools": ["write_file", "edit_file"],
                        "match_conclusion_field": "file_path",
                        "match_result_field": "result.file_path",
                        "message_key": "reviewing_rerun_after_final_infraguard_write",
                    }
                ],
            },
            "message_key": "reviewing_final_infraguard_required",
        },
    ]


def test_review_disabled_removes_review_step_and_generated_template_remains_final() -> None:
    loaded = _load_selling(enable_reviewing=False)

    steps = loaded.sub_pipelines["evaluate_candidate"].steps
    assert [step.step_id for step in steps] == ["template_generating", "cost_estimating"]

    template_step, cost_step = steps
    assert template_step.forward == "cost_estimating"
    assert template_step.a2a_artifacts == [
        A2AArtifactSpec(
            path="conclusion.file_path",
            content="conclusion.template",
            media_type="auto",
            role="final",
            supersedes_path="conclusion.file_path",
        )
    ]
    assert cost_step.context_fields == ["template"]


def test_selling_completion_guards_use_message_keys_not_raw_messages() -> None:
    raw = yaml.safe_load((_selling_dir() / "pipeline.yaml").read_text(encoding="utf-8"))

    def iter_guards() -> list[dict]:
        guards: list[dict] = []
        for step in raw.get("steps") or []:
            if isinstance(step, dict):
                guards.extend(guard for guard in step.get("completion_guards") or [] if isinstance(guard, dict))
        for sub_pipeline in (raw.get("sub_pipelines") or {}).values():
            if not isinstance(sub_pipeline, dict):
                continue
            for step in sub_pipeline.get("steps") or []:
                if isinstance(step, dict):
                    guards.extend(guard for guard in step.get("completion_guards") or [] if isinstance(guard, dict))
        return guards

    def assert_no_raw_message(config: dict) -> None:
        assert "message" not in config
        for value in config.values():
            if isinstance(value, dict):
                assert_no_raw_message(value)
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, dict):
                        assert_no_raw_message(item)

    guards = iter_guards()
    assert guards
    for guard in guards:
        assert_no_raw_message(guard)


def test_review_prompt_renders_infraguard_config_from_pipeline_yaml() -> None:
    from iac_code.pipeline.engine.context import PipelineContext

    loaded = _load_selling(enable_reviewing=True)
    review_step = loaded.sub_pipelines["evaluate_candidate"].steps[1]
    prompt = (_selling_dir() / review_step.prompt_file).read_text(encoding="utf-8")
    ctx = PipelineContext({"intent": [], "candidate": [], "template": []})
    ctx.set_conclusion("intent", {"business_type": "demo", "non_functional": {"availability": "low_cost"}})
    ctx.set_conclusion("candidate", {"name": "低成本单机方案", "topology": "single-zone ECS"})
    ctx.set_conclusion(
        "template",
        {
            "file_path": "/tmp/template.yaml",
            "region": "cn-hangzhou",
            "description": "demo",
        },
    )

    rendered = render_prompt(
        prompt,
        ctx,
        review_step.context_fields,
        extra_context={"step_config": review_step.config},
    )

    assert "{step_config" not in rendered
    assert '"security"' in rendered
    assert '"high_availability"' in rendered
    assert '"cost_optimization"' in rendered
    assert '"pack:aliyun:security"' in rendered
    assert '"pack:aliyun:network-architecture"' in rendered
    assert "低成本单机方案" in rendered
    assert "single-zone ECS" in rendered
    assert "selected_aspects" in rendered


def test_step_executor_injects_review_step_config_into_prompt(tmp_path) -> None:
    from unittest.mock import MagicMock

    from iac_code.pipeline.engine.context import PipelineContext
    from iac_code.pipeline.engine.step_executor import StepExecutor
    from iac_code.tools.base import ToolRegistry

    loaded = _load_selling(enable_reviewing=True)
    review_step = loaded.sub_pipelines["evaluate_candidate"].steps[1]
    ctx = PipelineContext({"intent": [], "candidate": [], "template": []})
    ctx.set_conclusion("intent", {"business_type": "demo"})
    ctx.set_conclusion("candidate", {"name": "demo plan"})
    ctx.set_conclusion(
        "template",
        {
            "file_path": "/tmp/template.yaml",
            "region": "cn-hangzhou",
            "description": "demo",
        },
    )

    prompt = StepExecutor(
        provider_manager=MagicMock(),
        base_tool_registry=ToolRegistry(),
        pipeline=loaded,
        pipeline_dir=_selling_dir(),
        cwd=str(tmp_path),
    )._build_full_system_prompt(review_step, ctx)

    assert "{step_config" not in prompt
    assert '"security"' in prompt
    assert '"high_availability"' in prompt
    assert '"pack:aliyun:security"' in prompt
    assert '"pack:aliyun:network-architecture"' in prompt
    assert "demo plan" in prompt


def test_selling_declares_infraguard_prerequisite_installers_and_policy_update() -> None:
    loaded = _load_selling()

    assert loaded.prerequisites["infraguard"] == {
        "command": "infraguard",
        "required_by_flags": ["enable_reviewing"],
        "on_missing": {"repl": "prompt_install", "non_interactive": "disable_feature"},
        "version_check": {
            "command": ["infraguard", "version"],
            "minimum": "0.10.1",
            "pattern": r"InfraGuard:\s*(?P<version>\d+\.\d+\.\d+)",
            "timeout_seconds": 30,
        },
        "installers": [
            {
                "id": "direct-binary",
                "display_key": "direct_binary_download",
                "platforms": ["darwin", "linux", "windows"],
                "download": {
                    "install_dir": "~/bin",
                    "installed_name": "infraguard",
                    "timeout_seconds": 1800,
                    "assets": [
                        {
                            "platforms": ["darwin"],
                            "architectures": ["arm64"],
                            "filename": "infraguard-v0.10.1-darwin-arm64",
                            "sha256": "cda2ba2eab1076a5f8b6c66654295a9b9aa8e9b302407e9580a4d75b7872b76f",
                            "urls": [
                                {"env": "IAC_CODE_INFRAGUARD_DARWIN_ARM64_URL"},
                                "https://ros-public-tools.oss-cn-beijing.aliyuncs.com/github-releases/aliyun/infraguard/0.10.1/infraguard-v0.10.1-darwin-arm64",
                                "https://github.com/aliyun/infraguard/releases/download/v0.10.1/infraguard-v0.10.1-darwin-arm64",
                            ],
                        },
                        {
                            "platforms": ["darwin"],
                            "architectures": ["amd64"],
                            "filename": "infraguard-v0.10.1-darwin-amd64",
                            "sha256": "d9e8963250de8a13bbe4a6e9e528ad638f6b8fb4ad6928e2bfc27771a1db3260",
                            "urls": [
                                {"env": "IAC_CODE_INFRAGUARD_DARWIN_AMD64_URL"},
                                "https://ros-public-tools.oss-cn-beijing.aliyuncs.com/github-releases/aliyun/infraguard/0.10.1/infraguard-v0.10.1-darwin-amd64",
                                "https://github.com/aliyun/infraguard/releases/download/v0.10.1/infraguard-v0.10.1-darwin-amd64",
                            ],
                        },
                        {
                            "platforms": ["linux"],
                            "architectures": ["amd64"],
                            "filename": "infraguard-v0.10.1-linux-amd64",
                            "sha256": "a0f66d5390df1746b10bdd0e87dfe68b6418c22d43add22a4ca9cc99f22ef66b",
                            "urls": [
                                {"env": "IAC_CODE_INFRAGUARD_LINUX_AMD64_URL"},
                                "https://ros-public-tools.oss-cn-beijing.aliyuncs.com/github-releases/aliyun/infraguard/0.10.1/infraguard-v0.10.1-linux-amd64",
                                "https://github.com/aliyun/infraguard/releases/download/v0.10.1/infraguard-v0.10.1-linux-amd64",
                            ],
                        },
                        {
                            "platforms": ["linux"],
                            "architectures": ["arm64"],
                            "filename": "infraguard-v0.10.1-linux-arm64",
                            "sha256": "5d929b89ff6ef5e8d6cd95ce3d84cb1747452f055706037b972463126065e262",
                            "urls": [
                                {"env": "IAC_CODE_INFRAGUARD_LINUX_ARM64_URL"},
                                "https://ros-public-tools.oss-cn-beijing.aliyuncs.com/github-releases/aliyun/infraguard/0.10.1/infraguard-v0.10.1-linux-arm64",
                                "https://github.com/aliyun/infraguard/releases/download/v0.10.1/infraguard-v0.10.1-linux-arm64",
                            ],
                        },
                        {
                            "platforms": ["windows"],
                            "architectures": ["amd64"],
                            "filename": "infraguard-v0.10.1-windows-amd64.exe",
                            "sha256": "48dd98cec9cc825fd273280e3ca8502fc65ab31170394b18d6e388ccb4c80332",
                            "urls": [
                                {"env": "IAC_CODE_INFRAGUARD_WINDOWS_AMD64_URL"},
                                "https://ros-public-tools.oss-cn-beijing.aliyuncs.com/github-releases/aliyun/infraguard/0.10.1/infraguard-v0.10.1-windows-amd64.exe",
                                "https://github.com/aliyun/infraguard/releases/download/v0.10.1/infraguard-v0.10.1-windows-amd64.exe",
                            ],
                        },
                        {
                            "platforms": ["windows"],
                            "architectures": ["arm64"],
                            "filename": "infraguard-v0.10.1-windows-arm64.exe",
                            "sha256": "7ab067e0af3e59173bf04d8a2624a2cf6b42eda0fe578bc0be2540394d49636e",
                            "urls": [
                                {"env": "IAC_CODE_INFRAGUARD_WINDOWS_ARM64_URL"},
                                "https://ros-public-tools.oss-cn-beijing.aliyuncs.com/github-releases/aliyun/infraguard/0.10.1/infraguard-v0.10.1-windows-arm64.exe",
                                "https://github.com/aliyun/infraguard/releases/download/v0.10.1/infraguard-v0.10.1-windows-arm64.exe",
                            ],
                        },
                    ],
                },
            },
        ],
        "post_install": {"timeout_seconds": 300, "commands": [["infraguard", "policy", "update"]]},
    }


def test_selling_direct_binary_assets_all_declare_sha256() -> None:
    loaded = _load_selling()
    installers = loaded.prerequisites["infraguard"]["installers"]
    direct_binary = next(installer for installer in installers if installer["id"] == "direct-binary")

    assets = direct_binary["download"]["assets"]

    assert assets
    assert all(asset.get("sha256") for asset in assets)


def test_selling_only_offers_direct_binary_infraguard_installer() -> None:
    loaded = _load_selling()
    installers = loaded.prerequisites["infraguard"]["installers"]

    assert [installer["id"] for installer in installers] == ["direct-binary"]


def test_selling_public_direct_binary_assets_do_not_require_env_url(monkeypatch) -> None:
    from iac_code.pipeline.engine.prerequisites import prepare_prerequisites

    loaded = _load_selling()
    offered_installer_ids = []

    for key in tuple(os.environ):
        if key.startswith("IAC_CODE_INFRAGUARD_"):
            monkeypatch.delenv(key, raising=False)

    def choose_installer(_name, installers):
        offered_installer_ids.extend(installer.id for installer in installers)
        return None

    prepare_prerequisites(
        loaded.prerequisites,
        feature_flags={"enable_reviewing": True},
        surface="repl",
        platform_system="linux",
        platform_machine="amd64",
        command_exists=lambda _command: None,
        choose_installer=choose_installer,
    )

    assert offered_installer_ids == ["direct-binary"]
