import hashlib
import sys
from pathlib import Path

import pytest

from iac_code.services.session_layout import UnsupportedSessionLayoutError
from iac_code.services.session_metadata import SessionMetadata, write_session_metadata
from iac_code.utils.image.pasted_content import PastedContent
from iac_code.utils.image.store import ImageStore, cleanup_old_image_caches


def _symlink_or_skip(target: Path, link: Path, *, target_is_directory: bool = False) -> None:
    try:
        link.symlink_to(target, target_is_directory=target_is_directory)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"symlink creation unsupported: {exc}")


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


def test_store_with_session_root_writes_under_session_image_cache(tmp_path, monkeypatch):
    monkeypatch.setattr("iac_code.utils.image.store._get_base_dir", lambda: tmp_path / "config" / "image-cache")
    session_root = tmp_path / "sessions" / "sess-a"
    store = ImageStore(session_id="sess-a", session_root=session_root)

    path = store.store(PastedContent(id=7, type="image", content="aGVsbG8=", media_type="image/png"))

    assert path == str(session_root / "image-cache" / "7.png")
    assert Path(path).read_bytes() == b"hello"
    assert not (tmp_path / "config" / "image-cache" / "sess-a").exists()


def test_store_with_session_root_rejects_symlink_image_cache(tmp_path, monkeypatch):
    monkeypatch.setattr("iac_code.utils.image.store._get_base_dir", lambda: tmp_path / "config" / "image-cache")
    session_root = tmp_path / "sessions" / "sess-a"
    write_session_metadata(session_root, SessionMetadata(session_id="sess-a", cwd=str(tmp_path), layout_version=2))
    outside = tmp_path / "outside-cache"
    outside.mkdir()
    _symlink_or_skip(outside, session_root / "image-cache", target_is_directory=True)
    store = ImageStore(session_id="sess-a", session_root=session_root)

    with pytest.raises(UnsupportedSessionLayoutError, match="session-owned path"):
        store.store(PastedContent(id=7, type="image", content="aGVsbG8=", media_type="image/png"))

    assert list(outside.iterdir()) == []


def test_store_with_future_session_root_raises_without_global_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr("iac_code.utils.image.store._get_base_dir", lambda: tmp_path / "config" / "image-cache")
    session_root = tmp_path / "sessions" / "sess-a"
    write_session_metadata(
        session_root,
        SessionMetadata(session_id="sess-a", cwd=str(tmp_path), layout_version=99),
    )
    store = ImageStore(session_id="sess-a", session_root=session_root)

    with pytest.raises(UnsupportedSessionLayoutError):
        store.store(PastedContent(id=7, type="image", content="aGVsbG8=", media_type="image/png"))

    assert not (session_root / "image-cache").exists()
    assert not (tmp_path / "config" / "image-cache" / "sess-a").exists()


def test_store_with_session_root_discovers_legacy_cached_file(tmp_path, monkeypatch):
    legacy_dir = tmp_path / "config" / "image-cache" / "sess-a"
    legacy_dir.mkdir(parents=True)
    legacy_path = legacy_dir / "7.png"
    legacy_path.write_bytes(b"legacy")
    monkeypatch.setattr("iac_code.utils.image.store._get_base_dir", lambda: tmp_path / "config" / "image-cache")
    store = ImageStore(session_id="sess-a", session_root=tmp_path / "sessions" / "sess-a")

    assert store.get_path(7) == str(legacy_path)


def test_store_with_session_root_prefers_session_scoped_file_over_legacy(tmp_path, monkeypatch):
    legacy_dir = tmp_path / "config" / "image-cache" / "sess-a"
    session_cache_dir = tmp_path / "sessions" / "sess-a" / "image-cache"
    legacy_dir.mkdir(parents=True)
    session_cache_dir.mkdir(parents=True)
    legacy_path = legacy_dir / "7.png"
    session_path = session_cache_dir / "7.png"
    legacy_path.write_bytes(b"legacy")
    session_path.write_bytes(b"session")
    monkeypatch.setattr("iac_code.utils.image.store._get_base_dir", lambda: tmp_path / "config" / "image-cache")
    store = ImageStore(session_id="sess-a", session_root=tmp_path / "sessions" / "sess-a")

    assert store.get_path(7) == str(session_path)


def test_get_path_with_session_root_rejects_symlink_image_cache_read(tmp_path, monkeypatch):
    monkeypatch.setattr("iac_code.utils.image.store._get_base_dir", lambda: tmp_path / "config" / "image-cache")
    session_root = tmp_path / "sessions" / "sess-a"
    write_session_metadata(session_root, SessionMetadata(session_id="sess-a", cwd=str(tmp_path), layout_version=2))
    outside = tmp_path / "outside-cache"
    outside.mkdir()
    (outside / "7.png").write_bytes(b"outside")
    _symlink_or_skip(outside, session_root / "image-cache", target_is_directory=True)
    store = ImageStore(session_id="sess-a", session_root=session_root)

    assert store.get_path(7) is None


def test_get_path_with_session_root_rejects_symlink_image_file(tmp_path, monkeypatch):
    monkeypatch.setattr("iac_code.utils.image.store._get_base_dir", lambda: tmp_path / "config" / "image-cache")
    session_root = tmp_path / "sessions" / "sess-a"
    write_session_metadata(session_root, SessionMetadata(session_id="sess-a", cwd=str(tmp_path), layout_version=2))
    image_cache = session_root / "image-cache"
    image_cache.mkdir()
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"outside")
    _symlink_or_skip(outside, image_cache / "7.png")
    store = ImageStore(session_id="sess-a", session_root=session_root)

    assert store.get_path(7) is None


def test_store_with_session_root_next_image_id_counts_session_and_legacy_files(tmp_path, monkeypatch):
    legacy_dir = tmp_path / "config" / "image-cache" / "sess-a"
    session_cache_dir = tmp_path / "sessions" / "sess-a" / "image-cache"
    legacy_dir.mkdir(parents=True)
    session_cache_dir.mkdir(parents=True)
    (legacy_dir / "7.png").write_bytes(b"legacy")
    (session_cache_dir / "3.png").write_bytes(b"session")
    monkeypatch.setattr("iac_code.utils.image.store._get_base_dir", lambda: tmp_path / "config" / "image-cache")
    store = ImageStore(session_id="sess-a", session_root=tmp_path / "sessions" / "sess-a")

    assert store.next_image_id() == 8


def test_next_image_id_with_session_root_rejects_symlink_image_cache_read(tmp_path, monkeypatch):
    legacy_dir = tmp_path / "config" / "image-cache" / "sess-a"
    legacy_dir.mkdir(parents=True)
    (legacy_dir / "7.png").write_bytes(b"legacy")
    monkeypatch.setattr("iac_code.utils.image.store._get_base_dir", lambda: tmp_path / "config" / "image-cache")
    session_root = tmp_path / "sessions" / "sess-a"
    write_session_metadata(session_root, SessionMetadata(session_id="sess-a", cwd=str(tmp_path), layout_version=2))
    outside = tmp_path / "outside-cache"
    outside.mkdir()
    (outside / "99.png").write_bytes(b"outside")
    _symlink_or_skip(outside, session_root / "image-cache", target_is_directory=True)
    store = ImageStore(session_id="sess-a", session_root=session_root)

    assert store.next_image_id() == 8


def test_next_image_id_with_session_root_rejects_symlink_image_file(tmp_path, monkeypatch):
    legacy_dir = tmp_path / "config" / "image-cache" / "sess-a"
    legacy_dir.mkdir(parents=True)
    (legacy_dir / "7.png").write_bytes(b"legacy")
    monkeypatch.setattr("iac_code.utils.image.store._get_base_dir", lambda: tmp_path / "config" / "image-cache")
    session_root = tmp_path / "sessions" / "sess-a"
    write_session_metadata(session_root, SessionMetadata(session_id="sess-a", cwd=str(tmp_path), layout_version=2))
    image_cache = session_root / "image-cache"
    image_cache.mkdir()
    outside = tmp_path / "99.png"
    outside.write_bytes(b"outside")
    _symlink_or_skip(outside, image_cache / "99.png")
    store = ImageStore(session_id="sess-a", session_root=session_root)

    assert store.next_image_id() == 8


def test_store_block_with_session_root_replaces_symlink_image_file(tmp_path, monkeypatch):
    monkeypatch.setattr("iac_code.utils.image.store._get_base_dir", lambda: tmp_path / "config" / "image-cache")
    session_root = tmp_path / "sessions" / "sess-a"
    write_session_metadata(session_root, SessionMetadata(session_id="sess-a", cwd=str(tmp_path), layout_version=2))
    image_cache = session_root / "image-cache"
    image_cache.mkdir()
    outside = tmp_path / "outside.png"
    outside.write_bytes(b"outside")
    digest = hashlib.sha256("aGVsbG8=".encode()).hexdigest()[:32]
    image_path = image_cache / f"block-{digest}.png"
    _symlink_or_skip(outside, image_path)
    block = type("ImageBlock", (), {"data": "aGVsbG8=", "media_type": "image/png"})()
    store = ImageStore(session_id="sess-a", session_root=session_root)

    assert store.store_block(block) == str(image_path)
    assert image_path.read_bytes() == b"hello"
    assert not image_path.is_symlink()
    assert outside.read_bytes() == b"outside"


def test_get_path_discovers_cached_file_after_store_recreated(tmp_path, monkeypatch):
    monkeypatch.setattr("iac_code.utils.image.store._get_base_dir", lambda: tmp_path / "image-cache")
    first = ImageStore(session_id="sess-a")
    pc = PastedContent(id=7, type="image", content="aGVsbG8=", media_type="image/png")
    path = first.store(pc)
    assert path is not None

    restored = ImageStore(session_id="sess-a")

    assert restored.get_path(7) == path


def test_next_image_id_skips_existing_cached_files_after_store_recreated(tmp_path, monkeypatch):
    monkeypatch.setattr("iac_code.utils.image.store._get_base_dir", lambda: tmp_path / "image-cache")
    first = ImageStore(session_id="sess-a")
    assert first.store(PastedContent(id=1, type="image", content="MQ==", media_type="image/png")) is not None

    restored = ImageStore(session_id="sess-a")

    assert restored.next_image_id() == 2


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
