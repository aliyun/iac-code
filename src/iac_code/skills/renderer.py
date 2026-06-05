"""Prompt rendering pipeline — argument substitution, variable replacement, shell execution."""

from __future__ import annotations

import asyncio
import re
import shlex
import sys
from dataclasses import dataclass
from typing import Literal

from iac_code.skills.skill_definition import SkillContext, SkillDefinition

# Shell command matching patterns
BLOCK_PATTERN = re.compile(r"```!\s*\n?([\s\S]*?)\n?```")
INLINE_PATTERN = re.compile(r"(?:^|\s)!`([^`]+)`")

SegmentKind = Literal["text", "inline_shell", "block_shell"]


@dataclass(frozen=True)
class PromptSegment:
    kind: SegmentKind
    content: str


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

    return await render_prompt_segments(
        segments,
        args,
        context=context,
        argument_names=skill.frontmatter.arguments,
        append_if_no_placeholder=True,
    )


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

        command = render_builtin_variables(segment.content, context)
        output = await _run_shell(command, cwd=context.cwd)
        rendered_parts.append(output.strip() if segment.kind == "inline_shell" else output)

    rendered = "".join(rendered_parts)
    if args and append_if_no_placeholder and not text_placeholder_used:
        rendered += f"\n\nARGUMENTS: {args}"
    return rendered


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
    return render_builtin_variables(rendered, context), used


def render_builtin_variables(content: str, context: SkillContext) -> str:
    """Render built-in variables that are not supplied by skill arguments."""
    content = content.replace("${SKILL_DIR}", context.skill_dir or "")
    content = content.replace("${SESSION_ID}", context.session_id or "")
    return content


def substitute_arguments(
    content: str,
    args: str,
    *,
    append_if_no_placeholder: bool = True,
    argument_names: list[str] | None = None,
) -> str:
    """Substitute argument placeholders in skill content.

    Supports:
    - Named arguments: $argName
    - Indexed arguments: $ARGUMENTS[0], $ARGUMENTS[1]
    - Short indexed: $0, $1
    - Full arguments: $ARGUMENTS
    """
    if not args:
        return content

    original = content
    parsed_args = _parse_arguments(args)
    argument_names = argument_names or []

    # 1. Named argument substitution: $foo, $bar
    for i, name in enumerate(argument_names):
        if i < len(parsed_args):
            content = re.sub(
                rf"\${re.escape(name)}(?![\[\w])",
                parsed_args[i],
                content,
            )

    # 2. Indexed argument substitution: $ARGUMENTS[0], $ARGUMENTS[1]
    content = re.sub(
        r"\$ARGUMENTS\[(\d+)\]",
        lambda m: parsed_args[int(m.group(1))] if int(m.group(1)) < len(parsed_args) else "",
        content,
    )

    # 3. Short indexed: $0, $1
    content = re.sub(
        r"\$(\d+)(?!\w)",
        lambda m: parsed_args[int(m.group(1))] if int(m.group(1)) < len(parsed_args) else "",
        content,
    )

    # 4. Full arguments substitution: $ARGUMENTS
    content = content.replace("$ARGUMENTS", args)

    # 5. Append if no placeholder was used
    if content == original and append_if_no_placeholder and args:
        content += f"\n\nARGUMENTS: {args}"

    return content


def _parse_arguments(args: str) -> list[str]:
    """Parse space-separated arguments, respecting quotes."""
    try:
        return shlex.split(args)
    except ValueError:
        return args.split()


async def execute_shell_commands(content: str, *, cwd: str = "") -> str:
    """Execute renderer shell commands from original content and replace with output.

    Two syntaxes:
    - Inline: !`command`  -> replaced with stdout (trimmed)
    - Block:  ```!\\ncommand\\n```  -> replaced with stdout
    """
    context = SkillContext(cwd=cwd)
    return await render_prompt_segments(
        parse_prompt_segments(content),
        "",
        context=context,
        append_if_no_placeholder=False,
    )


async def _run_shell(cmd: str, *, cwd: str = "", timeout: float = 30.0) -> str:
    """Run a shell command and return its stdout."""
    proc = None
    try:
        if sys.platform == "win32":
            from iac_code.utils.platform import PlatformInfo

            platform_info = PlatformInfo.detect()
            proc = await asyncio.create_subprocess_exec(
                platform_info.shell_path,
                "-c",
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd or None,
            )
        else:
            proc = await asyncio.create_subprocess_shell(
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd or None,
                start_new_session=True,
            )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return stdout.decode("utf-8", errors="replace")
    except asyncio.TimeoutError as e:
        if proc is not None:
            from iac_code.utils.platform import kill_process_tree

            await kill_process_tree(proc)
            try:
                await asyncio.wait_for(proc.communicate(), timeout=5)
            except (asyncio.TimeoutError, OSError):
                pass
        return f"[shell error: {e}]"
    except OSError as e:
        return f"[shell error: {e}]"


async def _replace_async(text: str, pattern: re.Pattern, replacer) -> str:
    """Async version of re.sub — awaits the replacer coroutine for each match."""
    result_parts: list[str] = []
    last_end = 0
    for match in pattern.finditer(text):
        result_parts.append(text[last_end : match.start()])
        replacement = await replacer(match)
        result_parts.append(replacement)
        last_end = match.end()
    result_parts.append(text[last_end:])
    return "".join(result_parts)
