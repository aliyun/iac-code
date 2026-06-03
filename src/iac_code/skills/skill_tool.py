"""SkillTool — registered in ToolRegistry, allows the model to invoke skills."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from loguru import logger

from iac_code.i18n import _
from iac_code.tools.base import Tool, ToolContext, ToolResult
from iac_code.types.skill_source import SkillSource

if TYPE_CHECKING:
    from iac_code.commands.registry import CommandRegistry


class SkillTool(Tool):
    """Tool that allows the model to invoke skills during conversation.

    Looks up skills from the unified CommandRegistry (PromptCommand instances).
    Supports two execution modes:
    - inline: Expands skill content into the current conversation context.
    - fork: Runs skill in an isolated sub-agent.
    """

    def __init__(
        self,
        command_registry: CommandRegistry,
        *,
        session_id: str = "",
        cwd: str = "",
        provider_manager: Any = None,
        tool_registry: Any = None,
        system_prompt: str = "",
        disabled_skills: dict[str, Any] | None = None,
    ) -> None:
        self._command_registry = command_registry
        self._session_id = session_id
        self._cwd = cwd
        self._provider_manager = provider_manager
        self._tool_registry = tool_registry
        self._system_prompt = system_prompt
        self._disabled_skills = {
            self._normalize_name(name): command for name, command in (disabled_skills or {}).items()
        }

    @property
    def name(self) -> str:
        return "skill"

    @property
    def description(self) -> str:
        return "Execute a skill within the current conversation."

    @property
    def input_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "skill": {
                    "type": "string",
                    "description": "The skill name to execute.",
                },
                "args": {
                    "type": "string",
                    "description": "Optional arguments for the skill.",
                },
            },
            "required": ["skill"],
        }

    async def execute(self, *, tool_input: dict[str, Any], context: ToolContext) -> ToolResult:
        import time

        from iac_code.services.telemetry import log_event
        from iac_code.services.telemetry.names import Events
        from iac_code.services.telemetry.sanitize import sanitize_skill_name

        skill_name = self._normalize_name(tool_input["skill"])
        args = tool_input.get("args", "")

        from iac_code.commands.registry import PromptCommand

        if skill_name in self._disabled_skills:
            return ToolResult.error(_("Skill '{name}' is disabled. Run /skills to enable it.").format(name=skill_name))

        command = self._command_registry.get(skill_name)
        if not isinstance(command, PromptCommand):
            return ToolResult.error(f"Skill not found: '{skill_name}'")

        # Record usage
        self._command_registry.record_skill_usage(skill_name)

        skill = command.skill
        if skill is None:
            return ToolResult.error(f"Skill definition missing for: '{skill_name}'")

        # Emit skill invoked event
        safe_name = sanitize_skill_name(skill_name)
        log_event(
            Events.SKILL_INVOKED,
            {
                "skill_name": safe_name,
                "invocation_source": "explicit",
            },
        )
        started = time.monotonic()

        # Execute based on context mode
        if skill.context_mode == "fork":
            return await self._execute_forked(command, args, context, safe_name, started)
        else:
            return await self._execute_inline(command, args, safe_name, started)

    async def _execute_inline(
        self, command: Any, args: str, safe_name: str | None = None, started: float | None = None
    ) -> ToolResult:
        """Inline mode: expand skill content into current conversation context."""
        import time

        from iac_code.services.telemetry import log_event
        from iac_code.services.telemetry.names import Events
        from iac_code.skills.processor import process_prompt_command

        try:
            result = await process_prompt_command(command, args, session_id=self._session_id)
            if safe_name is not None and started is not None:
                log_event(
                    Events.SKILL_COMPLETED,
                    {
                        "skill_name": safe_name,
                        "duration_ms": int((time.monotonic() - started) * 1000),
                        "outcome": "success",
                    },
                )
            return ToolResult(
                content=_("Skill '{name}' loaded (inline).").format(name=result.skill_name),
                is_error=False,
                new_messages=result.new_messages,
                context_modifier=result.context_modifier,
            )
        except Exception as e:
            if safe_name is not None and started is not None:
                log_event(
                    Events.SKILL_COMPLETED,
                    {
                        "skill_name": safe_name,
                        "duration_ms": int((time.monotonic() - started) * 1000),
                        "outcome": "error",
                    },
                )
            logger.exception("Skill inline execution failed: %s", command.name)
            return ToolResult.error(f"Skill execution failed: {e}")

    async def _execute_forked(
        self,
        command: Any,
        args: str,
        tool_context: ToolContext,
        safe_name: str | None = None,
        started: float | None = None,
    ) -> ToolResult:
        """Fork mode: run skill in an isolated sub-agent."""
        import time

        from iac_code.agent.agent_tool import run_sub_agent
        from iac_code.services.telemetry import log_event
        from iac_code.services.telemetry.names import Events
        from iac_code.skills.processor import process_prompt_command

        try:
            result = await process_prompt_command(command, args, session_id=self._session_id)
            skill = command.skill
            result_text, progress = await run_sub_agent(
                prompt=result.prompt_content,
                agent_type=skill.agent_type,
                cwd=tool_context.cwd,
                parent_provider_manager=self._provider_manager,
                parent_tool_registry=self._tool_registry,
                parent_system_prompt=self._system_prompt,
            )
            if safe_name is not None and started is not None:
                log_event(
                    Events.SKILL_COMPLETED,
                    {
                        "skill_name": safe_name,
                        "duration_ms": int((time.monotonic() - started) * 1000),
                        "outcome": "success",
                    },
                )
            return ToolResult.success(
                f"{result_text}\n\n"
                f"[Skill '{skill.name}' completed: "
                f"{progress.tool_use_count} tool calls, "
                f"{progress.token_count} tokens]"
            )
        except Exception as e:
            if safe_name is not None and started is not None:
                log_event(
                    Events.SKILL_COMPLETED,
                    {
                        "skill_name": safe_name,
                        "duration_ms": int((time.monotonic() - started) * 1000),
                        "outcome": "error",
                    },
                )
            logger.exception("Skill forked execution failed: %s", command.name)
            return ToolResult.error(f"Skill forked execution failed: {e}")

    @staticmethod
    def _normalize_name(name: str) -> str:
        """Normalize skill name: strip leading /, lowercase."""
        return name.lstrip("/").strip().lower()

    # --- UI rendering ---

    def render_tool_use_message(self, input: dict, *, verbose: bool = False) -> str | None:
        return input.get("skill", "") or None

    def user_facing_name(self, input: dict | None = None) -> str:
        return _("Skill")

    def is_read_only(self, input: dict | None = None) -> bool:
        return True  # Skill itself is just prompt injection

    def is_concurrency_safe(self, tool_input: dict[str, Any]) -> bool:
        return True

    # --- Permission check ---

    async def check_permissions(self, input: dict, context: dict | None = None) -> Any:
        """Skill permission check:

        1. Bundled skill -> auto-allow
        2. Skill with only safe properties -> auto-allow
        3. Others -> ask user
        """
        from iac_code.commands.registry import PromptCommand
        from iac_code.types.permissions import PermissionResult

        skill_name = self._normalize_name(input.get("skill", ""))
        if skill_name in self._disabled_skills:
            return PermissionResult(
                behavior="deny",
                message=_("Skill disabled: {name}").format(name=skill_name),
            )

        command = self._command_registry.get(skill_name)
        if not isinstance(command, PromptCommand):
            return PermissionResult(behavior="deny", message=f"Skill not found: {skill_name}")
        skill = command.skill

        # Bundled skills are fully trusted
        if command.source == SkillSource.BUNDLED:
            return PermissionResult(behavior="allow")

        # Safe-only skills auto-allow
        if self._has_only_safe_properties(skill):
            return PermissionResult(behavior="allow")

        # Others ask user
        return PermissionResult(
            behavior="ask",
            message=f"Allow skill '{skill_name}' (source: {skill.source if skill else 'unknown'})?",
        )

    @staticmethod
    def _has_only_safe_properties(skill: Any) -> bool:
        """Check if a skill only has safe properties (no tools, no shell commands)."""
        if skill.frontmatter.allowed_tools:
            return False
        from iac_code.skills.renderer import contains_shell_commands

        if contains_shell_commands(skill.content):
            return False
        return True
