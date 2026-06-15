"""U-I10: async-bearing files must not use deprecated asyncio.get_event_loop() inside async contexts."""

from __future__ import annotations

import pathlib


def test_repl_no_deprecated_get_event_loop():
    """In async contexts, asyncio.get_event_loop() is deprecated and slated
    for removal. Use asyncio.get_running_loop() instead.

    This is a static grep test — if a future change reintroduces
    get_event_loop() (e.g. from copy-paste), this catches it before merge.
    """
    src = pathlib.Path("src/iac_code/ui/repl.py").read_text(encoding="utf-8")
    # Allow other identifiers that happen to contain "get_event_loop":
    # e.g. asyncio.get_event_loop_policy() is a separate API and isn't deprecated.
    bad = "asyncio.get_event_loop()"
    assert bad not in src, f"Found {bad!r} in repl.py — use asyncio.get_running_loop() in async contexts."


def test_no_deprecated_get_event_loop_in_async_files():
    """Broader guard: extend the no-deprecated-event-loop check to other files
    known to host async code (renderer.py, cli/main.py).

    These files invoke asyncio APIs (run_in_executor, set_exception_handler) inside
    `async def` bodies; the running loop is always available there, so
    `asyncio.get_running_loop()` is both more correct and not deprecated.
    """
    files = [
        "src/iac_code/ui/repl.py",
        "src/iac_code/ui/renderer.py",
        "src/iac_code/cli/main.py",
    ]
    bad = "asyncio.get_event_loop()"
    violations = [f for f in files if bad in pathlib.Path(f).read_text(encoding="utf-8")]
    assert not violations, (
        f"Deprecated asyncio.get_event_loop() found in: {violations}. Use asyncio.get_running_loop() in async contexts."
    )
