from pathlib import Path

from iac_code.pipeline.engine.loader import load_pipeline_dir


def _selling_dir() -> Path:
    return Path(__file__).resolve().parents[3] / "src" / "iac_code" / "pipeline" / "selling"


def _template_generating_step():
    loaded = load_pipeline_dir(_selling_dir())
    evaluate_candidate = loaded.sub_pipelines["evaluate_candidate"]
    return next(step for step in evaluate_candidate.steps if step.step_id == "template_generating")


def test_template_generating_prompt_requires_validate_failure_recovery() -> None:
    selling_dir = _selling_dir()
    step = _template_generating_step()
    prompt = (selling_dir / step.prompt_file).read_text(encoding="utf-8")

    # ros_validate_template failure must trigger in-place fix + same-url retry, not exit/skip.
    assert "校验与失败恢复" in prompt
    assert "重新调用 `ros_validate_template` 重试" in prompt
    assert "最多重试 5 轮" in prompt
    assert "不要退出、不要跳过本候选" in prompt


def test_template_generating_prompt_requires_non_empty_template_output() -> None:
    selling_dir = _selling_dir()
    step = _template_generating_step()
    prompt = (selling_dir / step.prompt_file).read_text(encoding="utf-8")

    # Even after exhausting retries, the candidate must still emit a non-empty template.
    assert "提交**非空** `template`" in prompt
    assert "绝不能因为工具失败而跳过候选或返回空 `template`" in prompt


def test_template_generating_skill_documents_failure_recovery() -> None:
    selling_dir = _selling_dir()
    skill = (selling_dir / "skills" / "iac-aliyun-template-generating" / "SKILL.md").read_text(encoding="utf-8")

    assert "失败时不得退出、不得跳过本候选、不得返回空模板" in skill
    assert "达到 5 轮上限仍未通过" in skill
    assert "绝不空跳过候选" in skill
