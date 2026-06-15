"""Regression test for pipeline memory injection (问题 3)."""

from iac_code.agent.system_prompt import build_base_sections


def test_build_base_sections_appends_memory_when_provided():
    """问题 3：pipeline step 用 build_base_sections 应能注入 # Memory 段。"""
    result = build_base_sections(
        ["identity"],
        cwd="/tmp",
        memory_content="- [user-role](user_role.md) — Senior cloud engineer",
    )
    assert "# Memory" in result
    assert "Senior cloud engineer" in result


def test_build_base_sections_omits_memory_when_empty():
    """memory 为空时不出现 # Memory 段，避免空标题。"""
    result = build_base_sections(["identity"], cwd="/tmp", memory_content="")
    assert "# Memory" not in result


def test_build_base_sections_default_memory_kwarg():
    """不传 memory_content 时（默认 ""），行为应与之前一致 —— 不带 # Memory 段。"""
    result = build_base_sections(["identity"], cwd="/tmp")
    assert "# Memory" not in result


def test_build_base_sections_memory_only_when_no_sections():
    """空 sections + 非空 memory：应只返回 # Memory 段。"""
    result = build_base_sections([], cwd="/tmp", memory_content="just memory")
    assert result.startswith("# Memory")
    assert "just memory" in result
    assert "\n\n\n" not in result  # 无多余空行
