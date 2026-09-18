"""Cross-process barrier for session mutations and snapshots."""

from __future__ import annotations

import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from iac_code.utils.path_locks import PathLockRegistry
from iac_code.utils.state_io import cross_process_file_lock

SESSION_MUTATION_LOCK_FILENAME = ".session-mutation.lock"

_LOCAL_LOCKS = PathLockRegistry()
_DEPTH = threading.local()


@contextmanager
def session_mutation_guard(session_dir: str | Path) -> Iterator[None]:
    """Serialize a complete session mutation or snapshot across processes.

    The guard is reentrant in the current thread so a compound writer can hold
    it while calling lower-level session writers. This is a synchronous
    critical section: callers must not await while holding it. The required
    lock order is this session guard first, followed by any file-specific lock.
    """

    # Canonicalize directory aliases, but preserve the lock leaf so state_io's
    # no-follow open can still reject a substituted symlink or reparse point.
    lock_path = Path(session_dir).resolve() / SESSION_MUTATION_LOCK_FILENAME
    key = str(lock_path)
    with _LOCAL_LOCKS.lock_for(lock_path):
        depths = getattr(_DEPTH, "values", None)
        if depths is None:
            depths = {}
            _DEPTH.values = depths
        depth = depths.get(key, 0)
        if depth:
            depths[key] = depth + 1
            try:
                yield
            finally:
                depths[key] -= 1
            return
        with cross_process_file_lock(lock_path):
            depths[key] = 1
            try:
                yield
            finally:
                depths.pop(key, None)
