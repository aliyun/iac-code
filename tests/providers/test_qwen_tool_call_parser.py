from __future__ import annotations

import pytest

from iac_code.providers.base import ToolDefinition
from iac_code.providers.qwen_tool_call_parser import (
    StrictToolCallAssembler,
    ToolCallProtocolError,
    recover_xml_tool_calls,
    strict_tool_arguments,
)
from tests.providers._fakes import ns


def _tools():
    return [ToolDefinition(name="read_file", description="Read", input_schema={"type": "object"})]


def test_strict_arguments_accept_only_missing_empty_or_json_object():
    assert strict_tool_arguments(None, present=False) == {}
    assert strict_tool_arguments(None) == {}
    assert strict_tool_arguments("") == {}
    assert strict_tool_arguments('{"path":"a"}') == {"path": "a"}
    for malformed in ("   ", "[]", "null", "{bad", 123):
        with pytest.raises(ToolCallProtocolError):
            strict_tool_arguments(malformed)


def test_assembler_handles_arguments_before_identity_and_parallel_calls():
    assembler = StrictToolCallAssembler()
    assembler.feed([ns(index=0, id=None, function=ns(name=None, arguments='{"path":'))])
    assembler.feed(
        [
            ns(index=1, id="call_2", function=ns(name="read_file", arguments='{"path":"b"}')),
            ns(index=0, id="call_1", function=ns(name="read_file", arguments='"a"}')),
        ]
    )
    assert [call["input"] for call in assembler.finalize("tool_calls")] == [
        {"path": "a"},
        {"path": "b"},
    ]


def test_assembler_rejects_ambiguous_anonymous_fragment():
    assembler = StrictToolCallAssembler()
    assembler.feed(
        [
            ns(index=0, id="call_1", function=ns(name="read_file", arguments="")),
            ns(index=0, id="call_2", function=ns(name="read_file", arguments="")),
            ns(index=0, id=None, function=ns(name=None, arguments="{}")),
        ]
    )
    with pytest.raises(ToolCallProtocolError):
        assembler.finalize("tool_calls")


def test_assembler_claims_name_then_id_and_routes_same_id_across_new_index():
    assembler = StrictToolCallAssembler()
    assembler.feed([ns(index=4, id=None, function=ns(name=None, arguments='{"path":'))])
    assembler.feed([ns(index=4, id=None, function=ns(name="read_file", arguments=None))])
    assembler.feed([ns(index=7, id="call_1", function=ns(name="read_file", arguments='"a.py"}'))])
    assert assembler.finalize("tool_calls")[0]["input"] == {"path": "a.py"}


def test_assembler_allows_completed_index_reuse_but_rejects_incomplete_conflict():
    complete = StrictToolCallAssembler()
    complete.feed([ns(index=0, id="call_1", function=ns(name="read_file", arguments="{}"))])
    complete.feed([ns(index=0, id="call_2", function=ns(name="read_file", arguments="{}"))])
    assert [call["id"] for call in complete.finalize("tool_calls")] == ["call_1", "call_2"]

    incomplete = StrictToolCallAssembler()
    incomplete.feed([ns(index=0, id="call_1", function=ns(name="read_file", arguments="{"))])
    incomplete.feed([ns(index=0, id="call_2", function=ns(name="read_file", arguments="{}"))])
    with pytest.raises(ToolCallProtocolError, match="Conflicting"):
        incomplete.finalize("tool_calls")


def test_user_visible_protocol_errors_use_runtime_translation(monkeypatch):
    import iac_code.providers.qwen_tool_call_parser as parser

    monkeypatch.setattr(parser, "_", lambda message: f"translated:{message}")
    with pytest.raises(ToolCallProtocolError, match=r"^translated:Qwen tool arguments"):
        strict_tool_arguments("   ")


@pytest.mark.parametrize(
    "fragments",
    [
        [ns(index=-1, id="call_1", function=ns(name="read_file", arguments="{}"))],
        [ns(index=0, id="call_1", function=ns(name=None, arguments="{}"))],
        [ns(index=0, id=None, function=ns(name="read_file", arguments="{}"))],
        [ns(index=0, id="call_1", function=ns(name="read_file", arguments='{"path":"a'))],
        [ns(index=0, id="call_1", function=ns(name="read_file", arguments="[]"))],
        [ns(index=0, id="call_1", function=ns(name="read_file", arguments="   "))],
    ],
)
def test_assembler_rejects_invalid_final_shapes(fragments):
    assembler = StrictToolCallAssembler()
    assembler.feed(fragments)
    with pytest.raises(ToolCallProtocolError):
        assembler.finalize("tool_calls")


def test_assembler_requires_a_call_for_tool_calls_finish_and_accepts_zero_arguments():
    with pytest.raises(ToolCallProtocolError):
        StrictToolCallAssembler().finalize("tool_calls")
    assembler = StrictToolCallAssembler()
    assembler.feed([ns(index=0, id="call_1", function=ns(name="read_file", arguments=None))])
    assert assembler.finalize("tool_calls")[0]["input"] == {}


def test_assembler_ignores_dashscope_identity_free_empty_delimiters():
    assembler = StrictToolCallAssembler()
    assembler.feed([ns(index=0, id="", function=ns(name=None, arguments=""))])
    assembler.feed([ns(index=0, id="call_1", function=ns(name="read_file", arguments=""))])
    assembler.feed([ns(index=0, id="", function=ns(name=None, arguments='{"path":'))])
    assembler.feed([ns(index=0, id="", function=ns(name=None, arguments='"a.py"}'))])
    assembler.feed([ns(index=0, id="", function=ns(name=None, arguments=""))])

    assert assembler.finalize("tool_calls")[0]["input"] == {"path": "a.py"}


def test_xml_requires_registered_parameterized_invoke_and_intent_guard():
    valid = '<invoke name="read_file"><parameter name="path">a.py</parameter></invoke>'
    assert recover_xml_tool_calls(valid, _tools()).calls[0]["input"] == {"path": "a.py"}
    assert recover_xml_tool_calls('<invoke name="read_file"></invoke>', _tools()) is None
    recovery = recover_xml_tool_calls(f"Run: {valid}", _tools())
    assert recovery.remaining_text == "Run:"
    tutorial = "This is documentation, not a request to execute a tool. " * 20
    assert recover_xml_tool_calls(f"{tutorial}{valid}", _tools()) is None
    assert recover_xml_tool_calls(valid.replace("read_file", "unknown"), _tools()) is None
    assert recover_xml_tool_calls(f"```xml\n{valid}\n```", _tools()) is None
    assert recover_xml_tool_calls(f"> {valid}", _tools()) is None
    assert recover_xml_tool_calls(f"  > example: {valid}", _tools()) is None
    assert recover_xml_tool_calls(f"> <function_calls>{valid}</function_calls>", _tools()) is None


def test_xml_rejects_duplicate_or_nested_parameters_and_preserves_scalar_strings():
    duplicate = (
        '<invoke name="read_file"><parameter name="path">a</parameter>'
        '<parameter name="path">b</parameter></invoke>'
    )
    nested = '<invoke name="read_file"><parameter name="path"><value>a</value></parameter></invoke>'
    assert recover_xml_tool_calls(duplicate, _tools()) is None
    assert recover_xml_tool_calls(nested, _tools()) is None
    scalar = '<invoke name="read_file"><parameter name="path">8080</parameter></invoke>'
    assert recover_xml_tool_calls(scalar, _tools()).calls[0]["input"] == {"path": "8080"}


def test_xml_multiple_calls_wrapper_entities_newlines_and_unicode():
    tools = _tools() + [ToolDefinition(name="write_file", description="Write", input_schema={"type": "object"})]
    text = (
        '<function_calls><invoke name="read_file"><parameter name="path">\n目录/a.py\n</parameter></invoke>'
        '<invoke name="write_file"><parameter name="content">&lt;x&gt;&amp;&quot;&apos;</parameter>'
        '<parameter name="meta">{"ok":true}</parameter></invoke></function_calls>'
    )
    recovery = recover_xml_tool_calls(text, tools)
    assert recovery.remaining_text == ""
    assert recovery.calls[0]["input"] == {"path": "目录/a.py"}
    assert recovery.calls[1]["input"] == {"content": '<x>&"\'', "meta": {"ok": True}}


@pytest.mark.parametrize(
    "text",
    [
        '<function_calls>text<invoke name="read_file"><parameter name="path">a</parameter></invoke>'
        "</function_calls>",
        '<function_calls><invoke name="read_file"><parameter name="path">a</parameter></invoke>',
        '<invoke name="read_file"><parameter name="path">a</parameter></invoke></function_calls>',
        '~~~xml\n<invoke name="read_file"><parameter name="path">a</parameter></invoke>\n~~~',
        '<invoke name="read_file"><parameter name="path"><nested /></parameter></invoke>',
    ],
)
def test_xml_invalid_wrapper_fence_and_nesting_remain_text(text):
    assert recover_xml_tool_calls(text, _tools()) is None


def test_xml_structured_parse_failure_stays_scalar_and_amp_entity_decodes_once():
    malformed = '<invoke name="read_file"><parameter name="path">{bad</parameter></invoke>'
    assert recover_xml_tool_calls(malformed, _tools()).calls[0]["input"] == {"path": "{bad"}
    entity = '<invoke name="read_file"><parameter name="path">&amp;lt;</parameter></invoke>'
    assert recover_xml_tool_calls(entity, _tools()).calls[0]["input"] == {"path": "&lt;"}
    numeric = '<invoke name="read_file"><parameter name="path">&#65;</parameter></invoke>'
    assert recover_xml_tool_calls(numeric, _tools()) is None


def test_xml_size_limits_are_conservative():
    oversized = "x" * (32 * 1024 + 1)
    text = f'<invoke name="read_file"><parameter name="path">{oversized}</parameter></invoke>'
    assert recover_xml_tool_calls(text, _tools()) is None
