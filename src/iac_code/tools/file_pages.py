"""Bounded, lossless text pages and file/query-bound continuation cursors."""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from typing import Any

from iac_code.tools.result_storage import DEFAULT_MAX_INLINE_CHARS, RESULT_URI_PREFIX, result_digest
from iac_code.utils.state_io import open_text_no_follow

PAGE_RESERVE = 1_024


def cursor_binding(path: str, options: Any, *, reference: str | None = None) -> str:
    stat = Path(path).stat()
    identity = (
        [reference, result_digest(path), options]
        if reference is not None
        else [str(Path(path).resolve()), stat.st_size, stat.st_mtime_ns, options]
    )
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()


def encode_cursor(binding: str, state: dict[str, int]) -> str:
    data = json.dumps({"binding": binding, **state}, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(data).decode()


def decode_cursor(cursor: str, binding: str) -> dict[str, int]:
    try:
        if len(cursor) > 2_048:
            raise ValueError
        data = json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
        if not isinstance(data, dict) or data.pop("binding", None) != binding:
            raise ValueError
        if any(type(value) is not int or value < 0 or value > 2**63 - 1 for value in data.values()):
            raise ValueError
        return data
    except (ValueError, TypeError, KeyError) as error:
        raise ValueError("Invalid or stale cursor. Repeat the original query without a cursor.") from error


def positive_int(value: Any, name: str, *, minimum: int = 1) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}.")
    return value


def read_text_page(
    path: str,
    display_path: str,
    options: dict[str, Any],
    *,
    max_read_bytes: int,
    max_read_lines: int,
) -> tuple[str, dict[str, Any]]:
    """Read UTF-8 without allocating a long line or silently dropping its remainder."""
    character_mode = "start_char" in options or "end_char" in options
    if character_mode and ("start_line" in options or "end_line" in options):
        raise ValueError("Character and line ranges cannot be combined.")
    first = (
        positive_int(options.get("start_char", 0), "start_char", minimum=0)
        if character_mode
        else positive_int(options.get("start_line", 1), "start_line")
    )
    end_key = "end_char" if character_mode else "end_line"
    last = positive_int(options[end_key], end_key, minimum=0 if character_mode else 1) if end_key in options else None
    if last is not None and last < first:
        raise ValueError("Range end must not precede its start.")
    max_chars = positive_int(options.get("max_chars", DEFAULT_MAX_INLINE_CHARS), "max_chars")
    header = f"File: {display_path}"
    budget = min(max_chars, DEFAULT_MAX_INLINE_CHARS) - len(header) - PAGE_RESERVE
    if budget < 1:
        raise ValueError("max_chars is too small for the file header and cursor.")
    query = {"character_mode": character_mode, "first": first, "last": last}
    binding = cursor_binding(
        path,
        query,
        reference=display_path if display_path.startswith(RESULT_URI_PREFIX) else None,
    )
    state = decode_cursor(options["cursor"], binding) if options.get("cursor") else None
    with open_text_no_follow(path, "r", encoding="utf-8", newline="\n") as stream:
        line, column, char_offset, byte_position = 1, 0, 0, 0
        if state is not None:
            if set(state) != {"position", "line", "column", "char_offset"} or state["line"] < 1:
                raise ValueError("Invalid read cursor.")
            if state["position"] > Path(path).stat().st_size:
                raise ValueError("Cursor is past the end of the file.")
            stream.seek(state["position"])
            byte_position = state["position"]
            line, column, char_offset = state["line"], state["column"], state["char_offset"]
        else:
            while char_offset < first if character_mode else line < first:
                amount = min(4_096, first - char_offset) if character_mode else 4_096
                chunk = stream.read(amount) if character_mode else stream.readline(amount)
                if not chunk:
                    break
                char_offset += len(chunk)
                byte_position += len(chunk.encode("utf-8"))
                if byte_position > max_read_bytes:
                    raise ValueError("Requested offset exceeds the scan limit. Use read_file continuation cursors.")
                line += chunk.count("\n")
                column = len(chunk.rsplit("\n", 1)[-1]) if "\n" in chunk else column + len(chunk)

        start_char, start_line = char_offset, line
        chunks: list[str] = []
        used_chars = used_bytes = lines_read = 0
        while used_chars < budget and used_bytes < max_read_bytes and lines_read < max_read_lines:
            if last is not None and (char_offset >= last if character_mode else line > last):
                break
            prefix = "" if character_mode else f"{line:>6}\t" if column == 0 else f"{line}:{column}\t"
            available = budget - used_chars - len(prefix)
            if character_mode and last is not None:
                available = min(available, last - char_offset)
            if available <= 0:
                break
            # At most one page is allocated, including when a file has a multi-megabyte line.
            position = stream.tell()
            chunk = stream.readline(available)
            if not chunk:
                break
            byte_budget = max_read_bytes - used_bytes
            encoded = chunk.encode("utf-8")
            if len(encoded) > byte_budget:
                chunk = encoded[:byte_budget].decode("utf-8", errors="ignore")
                stream.seek(position)
                stream.read(len(chunk))
            if not chunk:
                break
            chunks.append(prefix + chunk)
            used_chars += len(prefix) + len(chunk)
            used_bytes += len(chunk.encode("utf-8"))
            byte_position += len(chunk.encode("utf-8"))
            char_offset += len(chunk)
            newline_count = chunk.count("\n")
            line += newline_count
            lines_read += max(1, newline_count)
            column = len(chunk.rsplit("\n", 1)[-1]) if newline_count else column + len(chunk)

        position = byte_position
        eof = stream.read(1) == ""
        finished_range = last is not None and (char_offset >= last if character_mode else line > last)
        more = not eof and not finished_range
        if more and char_offset == start_char:
            raise ValueError("The read budget cannot fit the next character. Increase max_chars.")
        next_cursor = (
            encode_cursor(
                binding,
                {
                    "position": position,
                    "line": line,
                    "column": column,
                    "char_offset": char_offset,
                },
            )
            if more
            else None
        )
    end_line = line - int(column == 0 and line > start_line)
    if character_mode:
        description = f"characters {start_char}-{char_offset} (0-based, end exclusive)"
    else:
        total = str(end_line) if eof else "unknown"
        if not eof and Path(path).stat().st_size <= DEFAULT_MAX_INLINE_CHARS:
            with open(path, encoding="utf-8", newline="\n") as small:
                text = small.read(DEFAULT_MAX_INLINE_CHARS + 1)
            total = str(text.count("\n") + int(bool(text) and not text.endswith("\n")))
        description = (
            f"{end_line} lines"
            if eof and "start_line" not in options and "end_line" not in options and state is None
            else f"lines {start_line}-{end_line} of {total}"
        )
    page_info = f"\nMore: {str(more).lower()}"
    if next_cursor:
        page_info += f"\nNext cursor: {next_cursor}"
    body = "".join(chunks) or "(empty file or range)"
    # Keep pagination outside the body: callers may materialize a template from it.
    content = f"{header} ({description}){page_info}\n\n{body}"
    return content, {
        "start_char": start_char,
        "end_char": char_offset,
        "start_line": start_line,
        "end_line": end_line,
        "has_more": more,
        "next_cursor": next_cursor,
    }
