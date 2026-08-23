"""Minimal HCL scanner for ``resource`` blocks inside a ROS Workspace.

Only what naming checks need is extracted: the resource type, the resource name
and the top-level argument names of each block.  Nested blocks, expressions and
values are skipped, and a block whose braces do not balance is dropped rather
than guessed at.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterator

_RESOURCE_HEADER = re.compile(r'(?m)^[ \t]*resource[ \t]+"([^"\r\n]+)"[ \t]+"([^"\r\n]+)"[ \t]*\{')
_ARGUMENT = re.compile(r"^[ \t]*([A-Za-z_][A-Za-z0-9_-]*)[ \t]*=")
_HEREDOC_HEADER = re.compile(r"<<[-~]?([A-Za-z_][A-Za-z0-9_]*)")


@dataclass(frozen=True)
class ResourceBlock:
    resource_type: str
    resource_name: str
    arguments: tuple[str, ...]


def _blank(text: str) -> str:
    return "".join("\n" if character == "\n" else " " for character in text)


def _scrub(content: str) -> str:
    """Blank comments, quoted strings and heredocs, keeping every offset stable.

    Offsets stay aligned with the original text so resource headers can be matched
    on the source while brace balancing and argument scanning use the scrubbed
    copy, where braces inside strings or comments can no longer mislead.
    """

    result: list[str] = []
    index = 0
    length = len(content)
    while index < length:
        character = content[index]
        if character == "#" or content.startswith("//", index):
            end = content.find("\n", index)
            end = length if end == -1 else end
        elif content.startswith("/*", index):
            end = content.find("*/", index + 2)
            end = length if end == -1 else end + 2
        elif heredoc := _HEREDOC_HEADER.match(content, index):
            terminator = re.compile(r"(?m)^[ \t]*{}[ \t]*$".format(re.escape(heredoc.group(1))))
            match = terminator.search(content, heredoc.end())
            end = length if match is None else match.end()
        elif character == '"':
            end = index + 1
            while end < length and content[end] not in {'"', "\n"}:
                end += 2 if content[end] == "\\" else 1
            end = min(end + 1, length)
        else:
            result.append(character)
            index += 1
            continue
        result.append(_blank(content[index:end]))
        index = end
    return "".join(result)


def _block_body(scrubbed: str, open_brace: int) -> str | None:
    depth = 0
    for index in range(open_brace, len(scrubbed)):
        character = scrubbed[index]
        if character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
            if depth == 0:
                return scrubbed[open_brace + 1 : index]
    return None


def _top_level_arguments(body: str) -> tuple[str, ...]:
    arguments: list[str] = []
    depth = 0
    for line in body.splitlines():
        if depth == 0 and (match := _ARGUMENT.match(line)):
            if match.group(1) not in arguments:
                arguments.append(match.group(1))
        opened = line.count("{") + line.count("[") + line.count("(")
        closed = line.count("}") + line.count("]") + line.count(")")
        depth = max(depth + opened - closed, 0)
    return tuple(arguments)


def iter_resource_blocks(content: str) -> Iterator[ResourceBlock]:
    scrubbed = _scrub(content)
    for header in _RESOURCE_HEADER.finditer(content):
        if scrubbed[header.end() - 1] != "{":
            continue
        body = _block_body(scrubbed, header.end() - 1)
        if body is None:
            continue
        yield ResourceBlock(
            resource_type=header.group(1),
            resource_name=header.group(2),
            arguments=_top_level_arguments(body),
        )
