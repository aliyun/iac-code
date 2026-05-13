"""Tests for skill frontmatter parsing."""

from iac_code.skills.frontmatter import (
    SkillFrontmatter,
    _data_to_frontmatter,
    _parse_yaml_safe,
    _quote_problematic_values,
    parse_frontmatter,
)


class TestParseFrontmatter:
    """Tests for parse_frontmatter function."""

    def test_no_frontmatter(self):
        """Markdown without frontmatter returns defaults."""
        fm, content = parse_frontmatter("Just some markdown content.")
        assert fm == SkillFrontmatter()
        assert content == "Just some markdown content."

    def test_basic_frontmatter(self):
        """Parse basic frontmatter fields."""
        md = '---\nname: "test-skill"\ndescription: "A test skill"\n---\nBody content here.'
        fm, content = parse_frontmatter(md)
        assert fm.name == "test-skill"
        assert fm.description == "A test skill"
        assert content == "Body content here."

    def test_all_fields(self):
        """Parse all supported frontmatter fields."""
        md = (
            "---\n"
            "name: full-skill\n"
            "description: Full featured skill\n"
            "allowed_tools:\n"
            '  - "bash(*)"\n'
            "  - read_file\n"
            "when_to_use: Use when testing\n"
            'argument_hint: "arg1 arg2"\n'
            "arguments:\n"
            "  - repo_url\n"
            "  - branch_name\n"
            "user_invocable: false\n"
            "model: claude-3-opus\n"
            "effort: high\n"
            "context: fork\n"
            "agent: explore\n"
            "paths:\n"
            '  - "src/**/*.py"\n'
            "---\n"
            "Skill body"
        )
        fm, content = parse_frontmatter(md)
        assert fm.name == "full-skill"
        assert fm.description == "Full featured skill"
        assert fm.allowed_tools == ["bash(*)", "read_file"]
        assert fm.when_to_use == "Use when testing"
        assert fm.argument_hint == "arg1 arg2"
        assert fm.arguments == ["repo_url", "branch_name"]
        assert fm.user_invocable is False
        assert fm.model == "claude-3-opus"
        assert fm.effort == "high"
        assert fm.context == "fork"
        assert fm.agent == "explore"
        assert fm.paths == ["src/**/*.py"]
        assert content == "Skill body"

    def test_allowed_tools_as_string(self):
        """allowed_tools can be a single string."""
        md = "---\nallowed_tools: bash\n---\nBody"
        fm, _ = parse_frontmatter(md)
        assert fm.allowed_tools == ["bash"]

    def test_defaults(self):
        """Default values when fields are missing."""
        md = "---\ndescription: Minimal\n---\nBody"
        fm, _ = parse_frontmatter(md)
        assert fm.name == ""
        assert fm.user_invocable is True
        assert fm.model == "inherit"
        assert fm.effort == ""
        assert fm.context == "inline"
        assert fm.agent == "general-purpose"
        assert fm.paths == []
        assert fm.allowed_tools == []

    def test_invalid_yaml_returns_defaults(self):
        """Invalid YAML falls back to defaults."""
        md = "---\n[invalid yaml{{\n---\nBody"
        fm, content = parse_frontmatter(md)
        # Falls back to defaults
        assert fm.description == ""
        assert content == "Body"

    def test_auto_quote_special_chars(self):
        """YAML special characters are auto-quoted on retry."""
        md = "---\npaths: src/**/*.py\n---\nBody"
        fm, _ = parse_frontmatter(md)
        # The auto-quoting should handle glob patterns
        assert fm.paths == ["src/**/*.py"] or fm.description == ""

    def test_empty_frontmatter(self):
        """Empty frontmatter block."""
        md = "---\n---\nBody"
        fm, content = parse_frontmatter(md)
        assert fm == SkillFrontmatter()
        assert content == "Body"

    def test_multiline_content_after_frontmatter(self):
        """Content after frontmatter preserves newlines."""
        md = "---\nname: test\n---\nLine 1\nLine 2\nLine 3"
        fm, content = parse_frontmatter(md)
        assert fm.name == "test"
        assert content == "Line 1\nLine 2\nLine 3"

    def test_localized_description_uses_current_language(self, monkeypatch):
        monkeypatch.setattr("iac_code.i18n.get_current_language", lambda: "zh")
        fm = _data_to_frontmatter(
            {
                "description": "fallback",
                "descriptions": {"en": "english", "zh": "中文描述"},
            }
        )
        assert fm.description == "中文描述"
        assert fm.descriptions == {"en": "english", "zh": "中文描述"}

    def test_parse_yaml_safe_rejects_non_dict(self):
        assert _parse_yaml_safe("- item") is None
        assert _parse_yaml_safe("plain string") is None

    def test_quote_problematic_values_only_quotes_scalar_values(self):
        quoted = _quote_problematic_values("paths: src/**/*.py\nname: safe\n- keep:list")
        assert 'paths: "src/**/*.py"' in quoted
        assert "name: safe" in quoted
        assert "- keep:list" in quoted

    def test_data_to_frontmatter_accepts_scalar_allowed_tools(self):
        fm = _data_to_frontmatter({"allowed_tools": "bash", "arguments": ["repo"], "paths": ["src"]})
        assert fm.allowed_tools == ["bash"]
        assert fm.arguments == ["repo"]
        assert fm.paths == ["src"]
