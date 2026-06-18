"""W-I4: enforce encoding='utf-8' on all Path.write_text/read_text calls in tests/.

Fixture text is mostly ASCII today and the suite is green on macOS/Linux only
because the default encoding happens to be utf-8. Windows defaults to
cp1252/cp936 and would break the moment a CJK fixture is added.
"""

from __future__ import annotations

import pathlib
import re


def test_test_files_use_utf8_encoding():
    pattern = re.compile(r"\.(write_text|read_text)\s*\(")
    violations: list[str] = []
    for f in pathlib.Path("tests").rglob("*.py"):
        src = f.read_text(encoding="utf-8")
        for m in pattern.finditer(src):
            # Find the matching close paren to extract the full call.
            i = m.end()
            depth = 1
            while i < len(src) and depth > 0:
                if src[i] == "(":
                    depth += 1
                elif src[i] == ")":
                    depth -= 1
                i += 1
            call = src[m.start() : i]
            if "encoding" not in call:
                line = src[: m.start()].count("\n") + 1
                violations.append(f"{f}:{line}")
    assert not violations, (
        f"Path.write_text/read_text without encoding= in {len(violations)} location(s): "
        f"{violations[:5]}{'...' if len(violations) > 5 else ''}"
    )
