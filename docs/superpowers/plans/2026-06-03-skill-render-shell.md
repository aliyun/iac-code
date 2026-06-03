# Skill Renderer Shell Boundary Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent skill arguments and shell output from creating renderer-executed shell commands while preserving explicitly authored renderer shell snippets.

**Architecture:** Parse the original skill prompt into text and shell segments before argument substitution. Apply skill argument substitution only to text segments, execute only shell segments found in the original prompt, and reuse the same shell detection rules for skill permission classification.

**Tech Stack:** Python 3.10+, asyncio subprocess execution, pytest, existing `uv` test workflow.

---

## File Structure

- Modify `src/iac_code/skills/renderer.py`: add segment parsing, render segments without rescanning mutated content, expose `contains_shell_commands()`.
- Modify `src/iac_code/skills/skill_tool.py`: replace ad hoc shell marker checks with `contains_shell_commands()`.
- Modify `tests/skills/test_renderer.py`: add red/green tests for argument-created shell syntax, shell segment argument isolation, output rescanning, and shared detection.
- Modify `tests/skills/test_skill_tool.py`: add a permission classification case aligned with renderer shell detection.

---

### Task 1: Add Renderer Regression Tests

**Files:**
- Modify: `tests/skills/test_renderer.py`

- [ ] **Step 1: Write failing tests for renderer shell boundaries**

Add imports:

```python
from unittest.mock import AsyncMock, call
```

Update the renderer import list:

```python
from iac_code.skills.renderer import (
    _parse_arguments,
    _replace_async,
    _run_shell,
    contains_shell_commands,
    execute_shell_commands,
    render_skill_prompt,
    substitute_arguments,
)
```

Add these tests under `TestRendererPipeline`:

```python
    @pytest.mark.asyncio
    async def test_argument_rendered_shell_block_stays_text(self, monkeypatch):
        skill = SkillDefinition(
            name="demo",
            description="demo",
            frontmatter=SkillFrontmatter(description="demo"),
            content="User input:\n$ARGUMENTS",
            source=SkillSource.PROJECT,
        )
        context = SkillContext(cwd="/tmp/work")
        run_mock = AsyncMock(return_value="unexpected")
        monkeypatch.setattr("iac_code.skills.renderer._run_shell", run_mock)

        result = await render_skill_prompt(skill, "```!\necho pwned\n```", context)

        assert "```!\necho pwned\n```" in result
        run_mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_argument_rendered_inline_shell_stays_text(self, monkeypatch):
        skill = SkillDefinition(
            name="demo",
            description="demo",
            frontmatter=SkillFrontmatter(description="demo"),
            content="User input: $ARGUMENTS",
            source=SkillSource.PROJECT,
        )
        context = SkillContext(cwd="/tmp/work")
        run_mock = AsyncMock(return_value="unexpected")
        monkeypatch.setattr("iac_code.skills.renderer._run_shell", run_mock)

        result = await render_skill_prompt(skill, "!`echo pwned`", context)

        assert "User input: !`echo pwned`" == result
        run_mock.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_shell_segment_does_not_substitute_skill_arguments(self, monkeypatch):
        skill = SkillDefinition(
            name="demo",
            description="demo",
            frontmatter=SkillFrontmatter(description="demo", arguments=["name"]),
            content='```!\necho "$ARGUMENTS" "$0" "$name"\n```',
            source=SkillSource.PROJECT,
        )
        context = SkillContext(cwd="/tmp/work")
        run_mock = AsyncMock(return_value="shell output\n")
        monkeypatch.setattr("iac_code.skills.renderer._run_shell", run_mock)

        result = await render_skill_prompt(skill, 'danger; echo injected', context)

        assert result == "shell output\n"
        run_mock.assert_awaited_once_with('echo "$ARGUMENTS" "$0" "$name"', cwd="/tmp/work")

    @pytest.mark.asyncio
    async def test_shell_output_is_not_rescanned_for_inline_shell(self, monkeypatch):
        content = "```!\nprintf '!`echo second`'\n```"
        run_mock = AsyncMock(return_value="!`echo second`")
        monkeypatch.setattr("iac_code.skills.renderer._run_shell", run_mock)

        result = await execute_shell_commands(content)

        assert result == "!`echo second`"
        run_mock.assert_awaited_once_with("printf '!`echo second`'", cwd="")

    @pytest.mark.asyncio
    async def test_multiple_original_shell_segments_execute_in_order(self, monkeypatch):
        content = "a !`one` b\n```!\ntwo\n```\nc !`three`"
        run_mock = AsyncMock(side_effect=["ONE\n", "TWO\n", "THREE\n"])
        monkeypatch.setattr("iac_code.skills.renderer._run_shell", run_mock)

        result = await execute_shell_commands(content, cwd="/tmp/work")

        assert result == "aONE b\nTWO\ncTHREE"
        assert run_mock.await_args_list == [
            call("one", cwd="/tmp/work"),
            call("two", cwd="/tmp/work"),
            call("three", cwd="/tmp/work"),
        ]
```

Add these tests near renderer helper tests:

```python
class TestShellDetection:
    def test_contains_shell_commands_detects_inline_and_block(self):
        assert contains_shell_commands("Run !`echo hi`")
        assert contains_shell_commands("```!\necho hi\n```")

    def test_contains_shell_commands_ignores_plain_text(self):
        assert not contains_shell_commands("Plain $ARGUMENTS and $PATH")
        assert not contains_shell_commands("```python\nprint('not shell')\n```")
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
PATH="$HOME/.local/bin:$PATH" uv run pytest tests/skills/test_renderer.py -v
```

Expected: FAIL because `contains_shell_commands` is missing and current renderer executes shell syntax created by argument substitution or output rescanning.

---

### Task 2: Implement Original-Segment Renderer Execution

**Files:**
- Modify: `src/iac_code/skills/renderer.py`

- [ ] **Step 1: Add segment data structures and parser**

Add imports:

```python
from dataclasses import dataclass
from typing import Literal
```

Add after the shell patterns:

```python
SegmentKind = Literal["text", "inline_shell", "block_shell"]


@dataclass(frozen=True)
class PromptSegment:
    kind: SegmentKind
    content: str
```

Add parser helpers:

```python
def contains_shell_commands(content: str) -> bool:
    """Return True when content contains renderer shell syntax."""
    return any(segment.kind != "text" for segment in parse_prompt_segments(content))


def parse_prompt_segments(content: str) -> list[PromptSegment]:
    """Split original skill content into text and executable shell segments."""
    matches: list[tuple[int, int, SegmentKind, str]] = []
    block_spans: list[tuple[int, int]] = []

    for match in BLOCK_PATTERN.finditer(content):
        block_spans.append((match.start(), match.end()))
        matches.append((match.start(), match.end(), "block_shell", match.group(1).strip()))

    for match in INLINE_PATTERN.finditer(content):
        if any(start <= match.start() < end for start, end in block_spans):
            continue
        matches.append((match.start(), match.end(), "inline_shell", match.group(1).strip()))

    matches.sort(key=lambda item: item[0])

    segments: list[PromptSegment] = []
    last_end = 0
    for start, end, kind, shell_content in matches:
        if start < last_end:
            continue
        if start > last_end:
            segments.append(PromptSegment("text", content[last_end:start]))
        segments.append(PromptSegment(kind, shell_content))
        last_end = end

    if last_end < len(content):
        segments.append(PromptSegment("text", content[last_end:]))

    return segments
```

- [ ] **Step 2: Render parsed segments without rescanning mutated content**

Replace `render_skill_prompt()` with:

```python
async def render_skill_prompt(
    skill: SkillDefinition,
    args: str,
    context: SkillContext,
) -> str:
    """Complete rendering pipeline for a skill prompt."""
    segments: list[PromptSegment] = []

    if skill.skill_root:
        segments.append(PromptSegment("text", f"Base directory for this skill: {skill.skill_root}\n\n"))
    segments.extend(parse_prompt_segments(skill.content))

    rendered = await render_prompt_segments(
        segments,
        args,
        context=context,
        argument_names=skill.frontmatter.arguments,
        append_if_no_placeholder=True,
    )
    return rendered
```

Add:

```python
async def render_prompt_segments(
    segments: list[PromptSegment],
    args: str,
    *,
    context: SkillContext,
    argument_names: list[str] | None = None,
    append_if_no_placeholder: bool = False,
) -> str:
    """Render pre-parsed prompt segments without treating rendered text as shell syntax."""
    rendered_parts: list[str] = []
    text_placeholder_used = False

    for segment in segments:
        if segment.kind == "text":
            rendered_text, used = render_text_segment(
                segment.content,
                args,
                context=context,
                argument_names=argument_names,
            )
            text_placeholder_used = text_placeholder_used or used
            rendered_parts.append(rendered_text)
            continue

        output = await _run_shell(segment.content, cwd=context.cwd)
        rendered_parts.append(output.strip() if segment.kind == "inline_shell" else output)

    rendered = "".join(rendered_parts)
    if args and append_if_no_placeholder and not text_placeholder_used:
        rendered += f"\n\nARGUMENTS: {args}"
    return rendered
```

Add:

```python
def render_text_segment(
    content: str,
    args: str,
    *,
    context: SkillContext,
    argument_names: list[str] | None = None,
) -> tuple[str, bool]:
    """Render arguments and built-in variables in a non-shell text segment."""
    rendered = substitute_arguments(
        content,
        args,
        append_if_no_placeholder=False,
        argument_names=argument_names,
    )
    used = rendered != content
    rendered = rendered.replace("${SKILL_DIR}", context.skill_dir or "")
    rendered = rendered.replace("${SESSION_ID}", context.session_id or "")
    return rendered, used
```

- [ ] **Step 3: Update `execute_shell_commands()` to use parsed segments**

Replace `execute_shell_commands()` with:

```python
async def execute_shell_commands(content: str, *, cwd: str = "") -> str:
    """Execute renderer shell commands from the original content and replace them with output."""
    context = SkillContext(cwd=cwd)
    return await render_prompt_segments(
        parse_prompt_segments(content),
        "",
        context=context,
        append_if_no_placeholder=False,
    )
```

- [ ] **Step 4: Run renderer tests to verify green**

Run:

```bash
PATH="$HOME/.local/bin:$PATH" uv run pytest tests/skills/test_renderer.py -v
```

Expected: PASS.

---

### Task 3: Align Skill Permission Shell Detection

**Files:**
- Modify: `src/iac_code/skills/skill_tool.py`
- Modify: `tests/skills/test_skill_tool.py`

- [ ] **Step 1: Write failing permission classification test**

Add to `TestSkillTool`:

```python
    def test_has_only_safe_properties_uses_renderer_shell_detection(self):
        multiline_shell = SimpleNamespace(
            frontmatter=SimpleNamespace(allowed_tools=[]),
            content="Before\n```!\necho hello\n```\nAfter",
        )

        assert SkillTool._has_only_safe_properties(multiline_shell) is False
```

- [ ] **Step 2: Run test to verify current behavior**

Run:

```bash
PATH="$HOME/.local/bin:$PATH" uv run pytest tests/skills/test_skill_tool.py::TestSkillTool::test_has_only_safe_properties_uses_renderer_shell_detection -v
```

Expected: PASS after Task 2 helper exists only if current string checks already catch this case; if it passes, keep it as regression coverage and continue to Step 3.

- [ ] **Step 3: Replace ad hoc shell marker checks**

In `_has_only_safe_properties()`, replace:

```python
        if "!`" in skill.content or "```!" in skill.content:
            return False
```

with:

```python
        from iac_code.skills.renderer import contains_shell_commands

        if contains_shell_commands(skill.content):
            return False
```

- [ ] **Step 4: Run skill tool tests**

Run:

```bash
PATH="$HOME/.local/bin:$PATH" uv run pytest tests/skills/test_skill_tool.py -v
```

Expected: PASS.

---

### Task 4: Focused Verification and Commit

**Files:**
- Verify all modified source and test files.

- [ ] **Step 1: Run focused test suite**

Run:

```bash
PATH="$HOME/.local/bin:$PATH" uv run pytest tests/skills/test_renderer.py tests/skills/test_skill_tool.py -v
```

Expected: PASS.

- [ ] **Step 2: Run broader relevant tests**

Run:

```bash
PATH="$HOME/.local/bin:$PATH" uv run pytest tests/skills tests/services/permissions/test_pipeline.py -v
```

Expected: PASS.

- [ ] **Step 3: Run lint**

Run:

```bash
PATH="$HOME/.local/bin:$PATH" make lint
```

Expected: PASS.

- [ ] **Step 4: Inspect git diff**

Run:

```bash
git diff -- src/iac_code/skills/renderer.py src/iac_code/skills/skill_tool.py tests/skills/test_renderer.py tests/skills/test_skill_tool.py
```

Expected: diff only contains renderer boundary changes and related tests.

- [ ] **Step 5: Commit implementation**

Run:

```bash
git add src/iac_code/skills/renderer.py src/iac_code/skills/skill_tool.py tests/skills/test_renderer.py tests/skills/test_skill_tool.py
PATH="$HOME/.local/bin:$PATH" git commit -m "fix: isolate skill renderer shell execution"
```

Expected: commit succeeds with pre-commit hooks passing.
