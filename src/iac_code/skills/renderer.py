"""Prompt rendering pipeline — argument substitution, variable replacement, shell execution."""

from __future__ import annotations

import asyncio
import re
import shlex
import sys

from iac_code.skills.skill_definition import SkillContext, SkillDefinition

# Shell command matching patterns
BLOCK_PATTERN = re.compile(r"```!\s*\n?([\s\S]*?)\n?```")
INLINE_PATTERN = re.compile(r"(?:^|\s)!`([^`]+)`")


async def render_skill_prompt(
    skill: SkillDefinition,
    args: str,
    context: SkillContext,
) -> str:
    """Complete rendering pipeline for a skill prompt."""
    content = skill.content

    # Step 1: Base directory prefix
    if skill.skill_root:
        content = f"Base directory for this skill: {skill.skill_root}\n\n{content}"

    # Step 2: Argument substitution
    content = substitute_arguments(
        content, args, append_if_no_placeholder=True, argument_names=skill.frontmatter.arguments
    )

    # Step 3: Built-in variable substitution
    content = content.replace("${SKILL_DIR}", context.skill_dir or "")
    content = content.replace("${SESSION_ID}", context.session_id or "")

    # Step 4: Shell command execution
    content = await execute_shell_commands(content, cwd=context.cwd)

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
    """Execute inline shell commands in skill content and replace with output.

    Two syntaxes:
    - Inline: !`command`  -> replaced with stdout (trimmed)
    - Block:  ```!\\ncommand\\n```  -> replaced with stdout
    """

    async def _replace_block(match: re.Match) -> str:
        cmd = match.group(1).strip()
        return await _run_shell(cmd, cwd=cwd)

    async def _replace_inline(match: re.Match) -> str:
        cmd = match.group(1).strip()
        output = await _run_shell(cmd, cwd=cwd)
        return output.strip()

    # Replace block commands
    content = await _replace_async(content, BLOCK_PATTERN, _replace_block)

    # Replace inline commands
    content = await _replace_async(content, INLINE_PATTERN, _replace_inline)

    return content


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
