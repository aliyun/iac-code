"""Tests for bare-text normalization of step conclusion fields."""

from __future__ import annotations

from iac_code.pipeline.engine.conclusion_text import strip_markdown_code_fence

TEMPLATE = "ROSTemplateFormatVersion: '2015-09-01'\nResources:\n  MyVpc:\n    Type: ALIYUN::ECS::VPC\n"


def test_bare_text_is_unchanged():
    assert strip_markdown_code_fence(TEMPLATE) == TEMPLATE


def test_strips_language_tagged_fence():
    wrapped = f"```yaml\n{TEMPLATE}```"
    assert strip_markdown_code_fence(wrapped) == TEMPLATE.rstrip("\n")


def test_strips_untagged_fence():
    wrapped = f"```\n{TEMPLATE}```"
    assert strip_markdown_code_fence(wrapped) == TEMPLATE.rstrip("\n")


def test_strips_tilde_fence():
    wrapped = f"~~~yaml\n{TEMPLATE}~~~"
    assert strip_markdown_code_fence(wrapped) == TEMPLATE.rstrip("\n")


def test_strips_explanatory_preamble_before_fence():
    wrapped = f"已根据候选架构生成模板：\n\n```yaml\n{TEMPLATE}```"
    assert strip_markdown_code_fence(wrapped) == TEMPLATE.rstrip("\n")


def test_trailing_whitespace_after_closing_fence_is_tolerated():
    wrapped = f"```yaml\n{TEMPLATE}```\n\n"
    assert strip_markdown_code_fence(wrapped) == TEMPLATE.rstrip("\n")


def test_preserves_yaml_comments_inside_template():
    template = "# 业务 VPC\nResources:\n  MyVpc:\n    Type: ALIYUN::ECS::VPC\n"
    assert strip_markdown_code_fence(f"```yaml\n{template}```") == template.rstrip("\n")


def test_content_after_closing_fence_is_left_untouched():
    wrapped = f"```yaml\n{TEMPLATE}```\n\n以上模板已通过校验。"
    assert strip_markdown_code_fence(wrapped) == wrapped


def test_unterminated_fence_is_left_untouched():
    wrapped = f"```yaml\n{TEMPLATE}"
    assert strip_markdown_code_fence(wrapped) == wrapped


def test_json_template_fence_is_stripped():
    template = '{"ROSTemplateFormatVersion": "2015-09-01"}'
    assert strip_markdown_code_fence(f"```json\n{template}\n```") == template


def test_text_without_fence_characters_is_returned_as_is():
    assert strip_markdown_code_fence("Resources: {}") == "Resources: {}"


def test_empty_text_is_returned_as_is():
    assert strip_markdown_code_fence("") == ""
