"""Resolve complete result-reference shell arguments before permission checks."""

from __future__ import annotations

import re
import shlex

from tree_sitter import Node

from iac_code.tools.bash.command_parser import _argv_text_from, _parser
from iac_code.tools.result_storage import (
    RESULT_URI_PREFIX,
    bash_result_snapshot,
    missing_bash_result_input,
    resolve_result_reference,
)

_ARGUMENT_NODES = frozenset({"word", "string", "raw_string", "concatenation"})
_OPTION_REFERENCE = re.compile(r"^(--?[A-Za-z0-9_-]+=)(tool-result://.*)$", re.DOTALL)


def resolve_command_references(
    command: str,
    storage_dir: str | None,
    *,
    snapshot: bool = False,
    references: list[tuple[str, str]] | None = None,
) -> str:
    """Replace static URI arguments with shell-quoted, current-session file paths.

    Do not rewrite arbitrary program text. Python and nested shells can receive
    references as separate argv operands rather than embedding them in code.
    """
    if RESULT_URI_PREFIX not in command:
        return command
    source = command.encode("utf-8")
    root = _parser.parse(source).root_node
    if root.has_error:
        raise ValueError("Cannot resolve tool-result:// references in an invalid shell command.")
    replacements: list[tuple[int, int, bytes]] = []

    def visit(node: Node) -> None:
        text = source[node.start_byte : node.end_byte].decode("utf-8")
        if RESULT_URI_PREFIX not in text or node.type == "comment":
            return
        if node.type in _ARGUMENT_NODES:
            # A quoted command substitution contains real shell commands, not
            # a literal operand. Resolve their operands without rewriting the
            # surrounding string or accepting a URI assembled by expansion.
            substitutions = [child for child in node.named_children if child.type == "command_substitution"]
            if substitutions:
                literal = source[node.start_byte : node.end_byte]
                for child in reversed(substitutions):
                    start, end = child.start_byte - node.start_byte, child.end_byte - node.start_byte
                    literal = literal[:start] + literal[end:]
                if RESULT_URI_PREFIX.encode() not in literal:
                    for child in substitutions:
                        visit(child)
                    return
            value = _argv_text_from(node, source)
            if _is_text_argument(node, source):
                return
            option = _OPTION_REFERENCE.fullmatch(value)
            prefix, reference = (option[1], option[2]) if option else ("", value)
            if not reference.startswith(RESULT_URI_PREFIX) or any(
                descendant.type in {"expansion", "simple_expansion", "command_substitution"}
                for descendant in _descendants(node)
            ):
                raise ValueError(
                    "Pass tool-result:// references as complete, static file arguments, "
                    "including for python -c or bash -c; do not embed them in program text or expansions."
                )
            _reject_write_operand(node, source)
            try:
                path = (
                    bash_result_snapshot(reference, storage_dir)
                    if snapshot
                    else resolve_result_reference(reference, storage_dir)
                )
            except FileNotFoundError:
                # Preserve shell conditionals and `test -f`: an absent input
                # must not abort branches that never read it. Invalid scopes,
                # traversal, changed content, and symlinks still fail closed.
                path = resolve_result_reference(reference, storage_dir, must_exist=False)
                if snapshot:
                    path = missing_bash_result_input(reference, storage_dir)
            if references is not None:
                references.append((reference, path))
            replacements.append((node.start_byte, node.end_byte, shlex.quote(prefix + path).encode("utf-8")))
            return
        if node.type in {"heredoc_body", "heredoc_content"}:
            raise ValueError("Pass tool-result:// references as file arguments, not inside a heredoc.")
        for child in node.named_children:
            visit(child)

    visit(root)
    for start, end, replacement in reversed(replacements):
        source = source[:start] + replacement + source[end:]
    return source.decode("utf-8")


def _descendants(node: Node):
    for child in node.named_children:
        yield child
        yield from _descendants(child)


def _command_arguments(node: Node, source: bytes) -> tuple[str, list[Node]]:
    parent = node.parent
    while parent is not None and parent.type in _ARGUMENT_NODES:
        parent = parent.parent
    if parent is None or parent.type != "command":
        return "", []
    name = next((child for child in parent.named_children if child.type == "command_name"), None)
    base = _argv_text_from(name, source).rsplit("/", 1)[-1] if name is not None else ""
    return base, [child for child in parent.named_children if child.type in _ARGUMENT_NODES]


def _is_text_argument(node: Node, source: bytes) -> bool:
    base, arguments = _command_arguments(node, source)
    if base in {"echo", "printf"}:
        return True
    if base == "jq":
        text_values = 0
        file_value = False
        program_seen = False
        for argument in arguments:
            value = _argv_text_from(argument, source)
            is_text = False
            if text_values:
                is_text = True
                text_values -= 1
            elif file_value:
                file_value = False
            elif value in {"--arg", "--argjson"}:
                text_values = 2
            elif value in {"--rawfile", "--slurpfile", "--argfile"}:
                text_values = 1
                file_value = True
            elif value in {"-f", "--from-file"}:
                file_value = True
                program_seen = True
            elif not value.startswith("-") and not program_seen:
                program_seen = True
                is_text = True
            if argument == node:
                return is_text
        return False
    if base not in {"grep", "egrep", "fgrep", "rg"}:
        return False
    pattern_seen = False
    explicit_pattern = False
    option_value = False
    after_options = False
    for argument in arguments:
        value = _argv_text_from(argument, source)
        is_text = option_value
        if option_value:
            option_value = False
        elif not after_options and value == "--":
            after_options = True
        elif not after_options and value.startswith("-"):
            is_text = True
            if value in {"-e", "--regexp", "-f", "--file"}:
                explicit_pattern = True
                option_value = True
            elif value in {"-g", "--glob", "-t", "--type", "-A", "-B", "-C", "-m", "--max-count"}:
                option_value = True
            elif value.startswith(("--regexp=", "--file=")) or value.startswith(("-e", "-f")):
                explicit_pattern = True
        elif not pattern_seen and not explicit_pattern:
            pattern_seen = True
            is_text = True
        if argument == node:
            if value.startswith("--file="):
                return False
            # -f/--file actually reads a pattern file; its value is a file operand.
            previous = arguments[arguments.index(argument) - 1] if arguments.index(argument) else None
            if previous is not None and _argv_text_from(previous, source) in {"-f", "--file"}:
                return False
            return is_text
    return False


def _reject_write_operand(node: Node, source: bytes) -> None:
    parent = node.parent
    while parent is not None and parent.type in _ARGUMENT_NODES:
        parent = parent.parent
    if parent is not None and parent.type == "file_redirect":
        text = source[parent.start_byte : parent.end_byte].decode("utf-8")
        if re.match(r"\d*(?:>|<>|&>)", text):
            raise ValueError("Tool result references are read-only; write to a separate workspace file.")
    base, arguments = _command_arguments(node, source)
    values = [_argv_text_from(argument, source) for argument in arguments]
    mutation = base in {"rm", "mv", "tee", "chmod", "chown", "truncate", "touch", "dd", "ln", "install"}
    mutation |= base in {"sed", "perl"} and any(value.startswith("-i") for value in values)
    mutation |= base == "cp" and bool(arguments) and arguments[-1] == node
    if mutation:
        raise ValueError("Tool result references are read-only; write to a separate workspace file.")
