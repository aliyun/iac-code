from __future__ import annotations

from pathlib import Path

import yaml

SKILL_DIR = (
    Path(__file__).resolve().parents[4] / "src" / "iac_code" / "pipeline" / "selling" / "skills" / "iac-aliyun-review"
)
SKILL_MD = SKILL_DIR / "SKILL.md"
REVIEW_PROMPT_MD = SKILL_DIR.parents[1] / "prompts" / "reviewing.md"


def _parse_frontmatter(text: str) -> dict:
    assert text.startswith("---"), "SKILL.md must start with YAML frontmatter"
    end = text.index("---", 3)
    return yaml.safe_load(text[3:end])


def _skill_body() -> str:
    content = SKILL_MD.read_text(encoding="utf-8")
    end = content.index("---", 3) + 3
    return content[end:]


def test_review_skill_frontmatter_declares_template_repair_conclusion() -> None:
    fm = _parse_frontmatter(SKILL_MD.read_text(encoding="utf-8"))

    assert fm["name"] == "iac-aliyun-review"
    assert fm.get("user_invocable") is False
    schema = fm["conclusion_schema"]
    assert schema["required"] == [
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
    ]
    assert schema["properties"]["validated"] == {"const": True}
    assert schema["properties"]["review_passed"] == {"const": True}
    assert schema["properties"]["template_sha256"] == {"type": "string"}
    assert schema["properties"]["selected_review_aspects"] == {"type": "array"}
    assert schema["properties"]["skipped_review_aspects"] == {"type": "array"}
    assert schema["properties"]["resolved_infraguard_policies"] == {"type": "array"}
    assert schema["additionalProperties"] is False


def test_review_skill_instructs_direct_infraguard_repair_flow() -> None:
    body = _skill_body()

    assert "template.file_path" in body
    assert "intent" in body
    assert "candidate" in body
    assert "aspect" in body
    assert "selected_aspects" in body
    assert "selected_review_aspects" in body
    assert "skipped_review_aspects" in body
    assert "read_file" in body
    assert "infraguard_scan" in body
    assert "step config" in body
    assert "write_file" in body or "edit_file" in body
    assert "原模板文件" in body
    assert "ros_validate_template" in body
    assert "ros_validate_template(template_url=template.file_path)" in body
    assert "ValidateTemplate" not in body
    assert 'aliyun_api(product="ros", action="ValidateTemplate"' not in body
    assert "max_fix_rounds" in body
    assert "最终" in body and "infraguard_scan" in body
    assert "blocking_findings" in body
    assert "complete_step" in body
    assert "修复依据只能来自 InfraGuard findings、`ros_validate_template` 错误或用户明确约束" in body
    assert "硬编码额外的安全、合规、架构规则" in body
    assert "0.0.0.0/0" not in body
    assert "AllocatePublicIP: true" not in body
    assert "include_file_content=true" in body
    assert "file_content" in body
    assert "file_sha256" in body
    assert "complete_step.conclusion.template" in body
    assert "初始 `infraguard_scan` 返回 `passed=true`、`blocking_findings=0`" in body
    assert "不要调用 `ros_validate_template`，不要再次调用 `infraguard_scan`" in body
    assert "只要本步骤修改过 `template.file_path`" in body


def test_review_skill_forbids_pipeline_lifecycle_and_placeholder_review_patterns() -> None:
    body = _skill_body()

    assert "不要执行 InfraGuard 的 policy update 命令" in body
    assert "infraguard policy update" not in body
    assert "不要默认使用 rollback_request 回到 `template_generating`" in body
    assert "不要把问题只放进单独的 `review` 字段" in body
    assert "conclusion_field: review" not in body
    assert "target_step 为 `template_generating`" not in body


def test_review_skill_records_actionable_scan_errors_without_success_completion() -> None:
    body = _skill_body()
    prompt = REVIEW_PROMPT_MD.read_text(encoding="utf-8")

    for text in (body, prompt):
        assert "command_not_found" in text
        assert "timeout" in text
        assert "malformed_json" in text
        assert "unexpected_exit_code" in text
        assert "unknown_policy_aspect" in text
        assert "`command`" in text
        assert "`stderr`" in text
        assert "`selected_aspects`" in text
        assert "validated=true" in text
        assert "review_passed=true" in text


def test_review_skill_does_not_embed_policy_catalog_or_rego_package_bodies() -> None:
    assets = "\n".join(path.read_text(encoding="utf-8") for path in SKILL_DIR.rglob("*") if path.is_file())

    assert "package infraguard.rules" not in assets
    assert "package infraguard.packs" not in assets
    assert "references/infraguard-policies/" not in assets
    assert "generate_infraguard_policies" not in assets


def test_review_prompt_repeats_direct_repair_contract() -> None:
    prompt = REVIEW_PROMPT_MD.read_text(encoding="utf-8")

    assert "{template.file_path}" in prompt
    assert "{intent}" in prompt
    assert "{candidate}" in prompt
    assert "{step_config.infraguard}" in prompt
    assert "selected_aspects" in prompt
    assert "aspect_policy_map" in prompt
    assert "selected_review_aspects" in prompt
    assert "skipped_review_aspects" in prompt
    assert "infraguard_scan" in prompt
    assert "step config" in prompt
    assert "原模板文件" in prompt
    assert "ros_validate_template(template_url=template.file_path)" in prompt
    assert "ValidateTemplate" not in prompt
    assert 'aliyun_api(product="ros", action="ValidateTemplate"' not in prompt
    assert "最终" in prompt and "blocking_findings" in prompt
    assert "InfraGuard finding" in prompt
    assert "硬编码安全/合规/架构规则" in prompt
    assert "0.0.0.0/0" not in prompt
    assert "直接公网 IP" not in prompt
    assert "include_file_content=true" in prompt
    assert "file_content" in prompt
    assert "file_sha256" in prompt
    assert "conclusion.template" in prompt
    assert "不要调用 `ros_validate_template`，不要再次调用 `infraguard_scan`" in prompt
    assert "只要本步骤修改过 `template.file_path`" in prompt
    assert "不要执行 InfraGuard 的 policy update 命令" in prompt
    assert "infraguard policy update" not in prompt
    assert "不要默认使用 rollback_request 回到 `template_generating`" in prompt
    assert "不要把问题只放进单独的 `review` 字段" in prompt
    assert "pack:aliyun:" not in prompt
    assert "rule:aliyun:" not in prompt
