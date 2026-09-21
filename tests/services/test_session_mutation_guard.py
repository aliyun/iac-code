from __future__ import annotations

import subprocess
import sys

import pytest

from iac_code.services.session_mutation_guard import SESSION_MUTATION_LOCK_FILENAME, session_mutation_guard


def test_session_guard_rejects_symlinked_lock_leaf(tmp_path):
    session = tmp_path / "session"
    session.mkdir()
    outside = tmp_path / "outside"
    outside.write_text("untouched", encoding="utf-8")
    try:
        (session / SESSION_MUTATION_LOCK_FILENAME).symlink_to(outside)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"symlinks unavailable: {exc}")
    with pytest.raises(OSError):
        with session_mutation_guard(session):
            pytest.fail("must not enter with a symlinked lock leaf")
    assert outside.read_text(encoding="utf-8") == "untouched"


def test_session_guard_reenters_through_directory_symlink(tmp_path):
    session = tmp_path / "session"
    session.mkdir()
    alias = tmp_path / "alias"
    try:
        alias.symlink_to(session, target_is_directory=True)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"directory symlinks unavailable: {exc}")
    # An independent interpreter bounds a real flock deadlock without leaving
    # a blocked thread behind in the test process.
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from pathlib import Path\n"
            "import sys\n"
            "from iac_code.services.session_mutation_guard import session_mutation_guard\n"
            "with session_mutation_guard(sys.argv[1]):\n"
            "    with session_mutation_guard(sys.argv[2]):\n"
            "        Path(sys.argv[1], 'committed').write_text('complete', encoding='utf-8')\n",
            str(session),
            str(alias),
        ],
        capture_output=True,
        text=True,
        timeout=5,
        check=True,
    )
    assert result.returncode == 0
    assert (session / "committed").read_text(encoding="utf-8") == "complete"
