"""Tests for skill loader."""

from iac_code.skills.loader import load_skill_from_path


class TestLoadSkillFromPath:
    """Tests for load_skill_from_path."""

    def test_load_simple_skill(self, tmp_path):
        """Load a simple skill markdown file."""
        skill_file = tmp_path / "greet.md"
        skill_file.write_text("---\ndescription: Say hello\n---\nHello $ARGUMENTS!")

        skill = load_skill_from_path(skill_file, skill_name="greet")
        assert skill is not None
        assert skill.name == "greet"
        assert skill.description == "Say hello"
        assert skill.content == "Hello $ARGUMENTS!"

    def test_load_skill_with_frontmatter_name(self, tmp_path):
        """Frontmatter name overrides filesystem name."""
        skill_file = tmp_path / "old-name.md"
        skill_file.write_text("---\nname: new-name\ndescription: Test\n---\nBody")

        skill = load_skill_from_path(skill_file, skill_name="old-name")
        assert skill is not None
        assert skill.name == "new-name"

    def test_load_skill_without_frontmatter(self, tmp_path):
        """Skill without frontmatter gets defaults."""
        skill_file = tmp_path / "raw.md"
        skill_file.write_text("Just raw content.")

        skill = load_skill_from_path(skill_file, skill_name="raw")
        assert skill is not None
        assert skill.name == "raw"
        assert skill.description == ""
        assert skill.content == "Just raw content."

    def test_load_nonexistent_file(self, tmp_path):
        """Non-existent file returns None."""
        skill_file = tmp_path / "nonexistent.md"
        skill = load_skill_from_path(skill_file, skill_name="none")
        assert skill is None

    def test_content_length_set(self, tmp_path):
        """content_length is set to length of content."""
        skill_file = tmp_path / "test.md"
        skill_file.write_text("---\ndescription: Test\n---\nHello World")

        skill = load_skill_from_path(skill_file, skill_name="test")
        assert skill is not None
        assert skill.content_length == len("Hello World")

    def test_file_path_set(self, tmp_path):
        """file_path is recorded."""
        skill_file = tmp_path / "test.md"
        skill_file.write_text("---\ndescription: Test\n---\nBody")

        skill = load_skill_from_path(skill_file, skill_name="test")
        assert skill is not None
        assert skill.file_path == str(skill_file)
