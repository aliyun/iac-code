"""N-M1: lint regression catching `except (FooError, Exception)` patterns where
Exception subsumes FooError, leaving the explicit class as dead code.

Excludes (Exception, KeyboardInterrupt) and (Exception, SystemExit) — those are
legitimate (KeyboardInterrupt/SystemExit are BaseException, not Exception).
"""

from __future__ import annotations

import pathlib
import re


def test_no_redundant_exception_clauses():
    # Catch `except (XxxError, Exception)` patterns — XxxError is always a subclass
    # of Exception, so listing both is dead code.
    pattern = re.compile(
        r"except\s*\(\s*([A-Z]\w*Error)\s*,\s*Exception\s*\)"
        r"|except\s*\(\s*Exception\s*,\s*([A-Z]\w*Error)\s*\)"
    )
    violations: list[str] = []
    for f in pathlib.Path("src").rglob("*.py"):
        src = f.read_text(encoding="utf-8")
        for m in pattern.finditer(src):
            line = src[: m.start()].count("\n") + 1
            violations.append(f"{f}:{line}: {m.group(0)}")
    assert not violations, f"Redundant exception clauses: {violations}"
