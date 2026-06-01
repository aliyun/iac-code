"""Tests for SkillProvider."""

from __future__ import annotations

import pytest

from iac_code.commands.registry import CommandRegistry, LocalCommand, PromptCommand
from iac_code.ui.suggestions.skill_provider import SkillProvider
from iac_code.ui.suggestions.types import CompletionToken


async def _dummy_handler(**kwargs):
    return "ok"


@pytest.fixture
def registry() -> CommandRegistry:
    reg = CommandRegistry()
    # Local commands — must NOT appear under the "$" trigger.
    reg.register(LocalCommand(name="help", description="Show help", handler=_dummy_handler))
    reg.register(LocalCommand(name="model", description="Switch model", handler=_dummy_handler))
    # Skills — the only things "$" should surface.
    reg.register(PromptCommand(name="deploy", description="Deploy a stack"))
    reg.register(PromptCommand(name="review", description="Review a template"))
    return reg


@pytest.fixture
def provider(registry) -> SkillProvider:
    return SkillProvider(registry)


def make_token(text: str, start: int = 0) -> CompletionToken:
    return CompletionToken(text=text, start=start, end=start + len(text), trigger="$")


class TestSkillProvider:
    def test_trigger(self, provider):
        assert provider.trigger == "$"

    def test_empty_query_returns_only_skills(self, provider):
        """Just '$' → returns all skills, no local commands."""
        items = provider.provide(make_token("$"))
        names = {item.display_text for item in items}
        assert names == {"deploy", "review"}
        assert "help" not in names
        assert "model" not in names

    def test_partial_match_skill(self, provider):
        """'$dep' → results contain 'deploy'."""
        items = provider.provide(make_token("$dep"))
        names = [item.display_text for item in items]
        assert "deploy" in names

    def test_local_command_name_not_matched(self, provider):
        """'$help' → empty (help is a local command, not a skill)."""
        items = provider.provide(make_token("$help"))
        assert items == []

    def test_no_match(self, provider):
        """'$xyzabc' → empty results."""
        items = provider.provide(make_token("$xyzabc"))
        assert items == []

    def test_source_and_icon(self, provider):
        """All items have source='skill' and icon='$'."""
        items = provider.provide(make_token("$"))
        assert items
        for item in items:
            assert item.source == "skill"
            assert item.icon == "$"

    def test_id_format(self, provider):
        """Item ids should be prefixed with 'skill:'."""
        items = provider.provide(make_token("$dep"))
        assert items
        for item in items:
            assert item.id.startswith("skill:")

    def test_completion_format(self, provider):
        """Completion inserts '$<name> '."""
        items = provider.provide(make_token("$dep"))
        deploy = [i for i in items if i.display_text == "deploy"]
        assert len(deploy) == 1
        assert deploy[0].completion == "$deploy "

    def test_partial_match_mid_sentence(self, provider):
        """A token starting mid-input still resolves skills."""
        items = provider.provide(make_token("$rev", start=6))
        names = [item.display_text for item in items]
        assert "review" in names
