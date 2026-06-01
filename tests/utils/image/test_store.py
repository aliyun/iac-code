import sys
from pathlib import Path

import pytest

from iac_code.utils.image.pasted_content import PastedContent
from iac_code.utils.image.store import ImageStore, cleanup_old_image_caches


def test_store_writes_per_session_file_with_0o600(tmp_path, monkeypatch):
    monkeypatch.setattr("iac_code.utils.image.store._get_base_dir", lambda: tmp_path / "image-cache")
    store = ImageStore(session_id="sess-a")
    pc = PastedContent(id=7, type="image", content="aGVsbG8=", media_type="image/png")
    path = store.store(pc)
    assert path is not None
    p = Path(path)
    assert p.exists()
    assert p.parent.name == "sess-a"
    assert p.name == "7.png"
    import os
    import stat

    if os.name == "posix":
        assert stat.S_IMODE(p.stat().st_mode) == 0o600


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes are not meaningful on Windows")
def test_store_directories_are_owner_only(tmp_path, monkeypatch):
    monkeypatch.setattr("iac_code.utils.image.store._get_base_dir", lambda: tmp_path / "image-cache")
    store = ImageStore(session_id="sess-a")
    pc = PastedContent(id=7, type="image", content="aGVsbG8=", media_type="image/png")

    path = store.store(pc)

    assert path is not None
    base_dir = tmp_path / "image-cache"
    session_dir = base_dir / "sess-a"
    assert oct(base_dir.stat().st_mode & 0o777) == "0o700"
    assert oct(session_dir.stat().st_mode & 0o777) == "0o700"


def test_lru_eviction_cap(tmp_path, monkeypatch):
    monkeypatch.setattr("iac_code.utils.image.store._get_base_dir", lambda: tmp_path / "image-cache")
    monkeypatch.setattr("iac_code.utils.image.store.MAX_STORED_IMAGE_PATHS", 3)
    store = ImageStore(session_id="sess")
    for i in range(5):
        store.cache_path(i, str(tmp_path / f"f{i}.png"))
    assert store.get_path(0) is None  # evicted
    assert store.get_path(4) is not None


def test_cleanup_only_deletes_other_sessions(tmp_path, monkeypatch):
    import os
    import time

    base = tmp_path / "image-cache"
    (base / "current").mkdir(parents=True)
    (base / "old").mkdir(parents=True)
    (base / "current" / "x.png").write_bytes(b"1")
    (base / "old" / "y.png").write_bytes(b"2")
    # Backdate "old" past the cleanup threshold; "current" stays fresh.
    stale = time.time() - (48 * 60 * 60)
    os.utime(base / "old", (stale, stale))
    monkeypatch.setattr("iac_code.utils.image.store._get_base_dir", lambda: base)
    cleanup_old_image_caches(current_session_id="current")
    assert (base / "current" / "x.png").exists()
    assert not (base / "old").exists()


def test_cleanup_preserves_recent_sibling_sessions(tmp_path, monkeypatch):
    """Concurrent REPL sessions: a sibling session's fresh dir must NOT be
    purged just because we're not it. Regression for the cross-session
    cache-wipe race introduced with multimodal image input."""
    base = tmp_path / "image-cache"
    (base / "current").mkdir(parents=True)
    (base / "sibling-active").mkdir(parents=True)
    (base / "sibling-active" / "y.png").write_bytes(b"2")
    monkeypatch.setattr("iac_code.utils.image.store._get_base_dir", lambda: base)
    cleanup_old_image_caches(current_session_id="current")
    assert (base / "sibling-active" / "y.png").exists()


def test_cleanup_max_age_threshold_is_configurable(tmp_path, monkeypatch):
    import os
    import time

    base = tmp_path / "image-cache"
    (base / "current").mkdir(parents=True)
    (base / "older").mkdir(parents=True)
    (base / "older" / "z.png").write_bytes(b"3")
    aged = time.time() - 120
    os.utime(base / "older", (aged, aged))
    monkeypatch.setattr("iac_code.utils.image.store._get_base_dir", lambda: base)
    # Threshold below the dir's age → eligible for deletion.
    cleanup_old_image_caches(current_session_id="current", max_age_seconds=60)
    assert not (base / "older").exists()


def test_store_returns_none_on_invalid_image(tmp_path, monkeypatch):
    monkeypatch.setattr("iac_code.utils.image.store._get_base_dir", lambda: tmp_path / "image-cache")
    store = ImageStore(session_id="sess")
    pc = PastedContent(id=1, type="text", content="hello")
    assert store.store(pc) is None


def test_store_returns_none_on_bad_base64(tmp_path, monkeypatch):
    monkeypatch.setattr("iac_code.utils.image.store._get_base_dir", lambda: tmp_path / "image-cache")
    store = ImageStore(session_id="sess")
    pc = PastedContent(id=2, type="image", content="!!!not-base64!!!", media_type="image/png")
    assert store.store(pc) is None


def test_cache_path_re_promotes_existing_entry(tmp_path, monkeypatch):
    monkeypatch.setattr("iac_code.utils.image.store.MAX_STORED_IMAGE_PATHS", 2)
    monkeypatch.setattr("iac_code.utils.image.store._get_base_dir", lambda: tmp_path / "image-cache")
    store = ImageStore(session_id="sess")
    store.cache_path(1, "/p/1.png")
    store.cache_path(2, "/p/2.png")
    # Touch 1 → 1 should now be most-recent → adding 3 evicts 2 (not 1).
    store.cache_path(1, "/p/1.png")
    store.cache_path(3, "/p/3.png")
    assert store.get_path(1) is not None
    assert store.get_path(2) is None
    assert store.get_path(3) is not None


def test_invalid_session_id_rejected():
    with pytest.raises(ValueError):
        ImageStore(session_id="")
    with pytest.raises(ValueError):
        ImageStore(session_id="../escape")
    with pytest.raises(ValueError):
        ImageStore(session_id="a/b")


def test_cleanup_with_invalid_session_id():
    with pytest.raises(ValueError):
        cleanup_old_image_caches(current_session_id="../escape")
