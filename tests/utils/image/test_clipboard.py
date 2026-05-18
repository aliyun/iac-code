import sys
from unittest.mock import MagicMock, patch

from iac_code.utils.image import clipboard


def test_macos_has_image_via_osascript(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    fake = MagicMock(returncode=0, stdout=b"PNGf\n", stderr=b"")
    with patch.object(clipboard.subprocess, "run", return_value=fake):
        assert clipboard.has_image_in_clipboard() is True


def test_macos_no_image(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")

    def fake_run(cmd, *a, **kw):
        if cmd[0] == "osascript":
            return MagicMock(returncode=0, stdout=b"", stderr=b"")
        if cmd[0] == "swift":
            # Swift fallback also finds no image
            return MagicMock(returncode=1, stdout=b"", stderr=b"")
        raise AssertionError(f"unexpected cmd: {cmd}")

    with patch.object(clipboard.subprocess, "run", side_effect=fake_run):
        assert clipboard.has_image_in_clipboard() is False


def test_macos_has_image_falls_through_to_tiff(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    fake = MagicMock(returncode=0, stdout=b"TIFF\n", stderr=b"")
    with patch.object(clipboard.subprocess, "run", return_value=fake):
        assert clipboard.has_image_in_clipboard() is True


def test_macos_has_image_falls_through_to_jpeg(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    fake = MagicMock(returncode=0, stdout=b"JPEG\n", stderr=b"")
    with patch.object(clipboard.subprocess, "run", return_value=fake):
        assert clipboard.has_image_in_clipboard() is True


def test_macos_probe_uses_single_osascript_call(monkeypatch):
    """Locks in the perf invariant: detection of any of {PNG, TIFF, JPEG}
    must take exactly one osascript subprocess (the try-chain script)."""
    monkeypatch.setattr(sys, "platform", "darwin")
    calls: list[list[str]] = []

    def fake_run(cmd, *a, **kw):
        calls.append(cmd)
        return MagicMock(returncode=0, stdout=b"TIFF\n", stderr=b"")

    with patch.object(clipboard.subprocess, "run", side_effect=fake_run):
        clipboard.has_image_in_clipboard()
    assert len(calls) == 1, f"expected 1 osascript call, got {len(calls)}: {calls}"


def test_linux_has_image_via_xclip(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setenv("DISPLAY", ":0")
    fake = MagicMock(returncode=0, stdout=b"image/png\nUTF8_STRING\n", stderr=b"")
    with patch.object(clipboard.subprocess, "run", return_value=fake):
        with patch.object(clipboard, "_which", return_value="/usr/bin/xclip"):
            assert clipboard.has_image_in_clipboard() is True


def test_unsupported_platform_returns_false(monkeypatch):
    monkeypatch.setattr(sys, "platform", "openbsd")
    assert clipboard.has_image_in_clipboard() is False


def test_try_read_image_from_path_absolute(tmp_path):
    p = tmp_path / "x.png"
    p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)
    out = clipboard.try_read_image_from_path(str(p))
    assert out is not None
    assert out.media_type == "image/png"


def test_get_image_from_clipboard_macos_writes_png(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "darwin")
    fixed_tmp = tmp_path / "out.png"
    png_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16

    class _FakeNamedTemp:
        def __init__(self, *a, **k):
            self.name = str(fixed_tmp)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(clipboard.tempfile, "NamedTemporaryFile", _FakeNamedTemp)

    def fake_run(cmd, *a, **kw):
        # The save script contains "open for access"; the probe does not.
        joined = " ".join(cmd)
        if cmd[0] == "osascript" and "open for access" in joined:
            fixed_tmp.write_bytes(png_bytes)
            return MagicMock(returncode=0, stdout=b"PNGf\n", stderr=b"")
        if cmd[0] == "osascript":
            # has_image probe
            return MagicMock(returncode=0, stdout=b"PNGf\n", stderr=b"")
        raise AssertionError(f"unexpected cmd: {cmd}")

    with patch.object(clipboard.subprocess, "run", side_effect=fake_run):
        out = clipboard.get_image_from_clipboard()

    assert out is not None
    assert out.media_type == "image/png"
    assert out.data == png_bytes


def test_get_image_from_clipboard_macos_writes_tiff(monkeypatch, tmp_path):
    """When clipboard only carries TIFF (e.g. Preview copy), the reader must
    still return a ClipboardImage. detect_image_format falls back to image/png
    for TIFF magic bytes — that's expected; downstream resizer normalises via
    Pillow re-encode, so the intermediate media_type is harmless."""
    monkeypatch.setattr(sys, "platform", "darwin")
    fixed_tmp = tmp_path / "out.png"
    tiff_bytes = b"II*\x00" + b"\x00" * 16  # little-endian TIFF magic

    class _FakeNamedTemp:
        def __init__(self, *a, **k):
            self.name = str(fixed_tmp)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(clipboard.tempfile, "NamedTemporaryFile", _FakeNamedTemp)

    def fake_run(cmd, *a, **kw):
        joined = " ".join(cmd)
        if cmd[0] == "osascript" and "open for access" in joined:
            fixed_tmp.write_bytes(tiff_bytes)
            return MagicMock(returncode=0, stdout=b"TIFF\n", stderr=b"")
        if cmd[0] == "osascript":
            return MagicMock(returncode=0, stdout=b"TIFF\n", stderr=b"")
        raise AssertionError(f"unexpected cmd: {cmd}")

    with patch.object(clipboard.subprocess, "run", side_effect=fake_run):
        out = clipboard.get_image_from_clipboard()

    assert out is not None
    assert out.data == tiff_bytes


def test_get_image_returns_none_when_no_image(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")

    def fake_run(cmd, *a, **kw):
        if cmd[0] == "osascript":
            return MagicMock(returncode=0, stdout=b"", stderr=b"")
        if cmd[0] == "swift":
            return MagicMock(returncode=1, stdout=b"", stderr=b"")
        raise AssertionError(f"unexpected cmd: {cmd}")

    with patch.object(clipboard.subprocess, "run", side_effect=fake_run):
        assert clipboard.get_image_from_clipboard() is None


def test_subprocess_timeout_treated_as_no_image(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")

    def boom(*a, **kw):
        raise clipboard.subprocess.TimeoutExpired(cmd="osascript", timeout=2.0)

    with patch.object(clipboard.subprocess, "run", side_effect=boom):
        assert clipboard.has_image_in_clipboard() is False


def test_linux_falls_back_to_xclip_when_wayland_empty(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    fixed_tmp = tmp_path / "out.png"
    png_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16

    class _FakeNamedTemp:
        def __init__(self, *a, **k):
            self.name = str(fixed_tmp)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(clipboard.tempfile, "NamedTemporaryFile", _FakeNamedTemp)
    monkeypatch.setattr(clipboard, "_which", lambda *names: "/usr/bin/" + names[0])

    def fake_run(cmd, *a, **kw):
        if cmd[0] == "wl-paste" and "-l" in cmd:
            # has_image probe — Wayland advertises image/png
            return MagicMock(returncode=0, stdout=b"image/png\n", stderr=b"")
        if cmd[0] == "wl-paste":
            # read step — produces nothing (XWayland-only app case)
            return MagicMock(returncode=0, stdout=b"", stderr=b"")
        if cmd[0] == "xclip" and "-t" in cmd and "TARGETS" not in cmd:
            # read step — write png bytes to the stdout fd
            fd = kw.get("stdout")
            if fd is not None and hasattr(fd, "write"):
                fd.write(png_bytes)
            else:
                fixed_tmp.write_bytes(png_bytes)
            return MagicMock(returncode=0, stdout=b"", stderr=b"")
        # default
        return MagicMock(returncode=0, stdout=b"", stderr=b"")

    with patch.object(clipboard.subprocess, "run", side_effect=fake_run):
        out = clipboard.get_image_from_clipboard()

    assert out is not None
    assert out.data == png_bytes


# ── UTI (modern pasteboard) tests ──────────────────────────────────────


def test_darwin_has_image_via_uti_detects_public_png(monkeypatch):
    """Swift/NSPasteboard fallback detects public.png UTI."""
    monkeypatch.setattr(sys, "platform", "darwin")
    fake = MagicMock(returncode=0, stdout=b"public.png\n", stderr=b"")
    with patch.object(clipboard.subprocess, "run", return_value=fake):
        assert clipboard._darwin_has_image_via_uti() is True


def test_darwin_has_image_via_uti_detects_public_tiff(monkeypatch):
    """Swift/NSPasteboard fallback detects public.tiff UTI."""
    monkeypatch.setattr(sys, "platform", "darwin")
    fake = MagicMock(returncode=0, stdout=b"public.tiff\n", stderr=b"")
    with patch.object(clipboard.subprocess, "run", return_value=fake):
        assert clipboard._darwin_has_image_via_uti() is True


def test_darwin_has_image_via_uti_no_image(monkeypatch):
    """Swift/NSPasteboard fallback returns False when no image UTI is found."""
    monkeypatch.setattr(sys, "platform", "darwin")
    fake = MagicMock(returncode=1, stdout=b"", stderr=b"")
    with patch.object(clipboard.subprocess, "run", return_value=fake):
        assert clipboard._darwin_has_image_via_uti() is False


def test_darwin_has_image_via_uti_swift_unavailable(monkeypatch):
    """When swift binary is missing (_run raises OSError → returns None),
    UTI detection degrades gracefully to False."""
    monkeypatch.setattr(sys, "platform", "darwin")

    def boom(*a, **kw):
        raise OSError("No such file or directory: 'swift'")

    with patch.object(clipboard.subprocess, "run", side_effect=boom):
        assert clipboard._darwin_has_image_via_uti() is False


def test_darwin_has_image_via_uti_timeout(monkeypatch):
    """When swift command times out, _run returns None, UTI detection
    returns False without crashing."""
    monkeypatch.setattr(sys, "platform", "darwin")

    def boom(*a, **kw):
        raise clipboard.subprocess.TimeoutExpired(cmd="swift", timeout=10.0)

    with patch.object(clipboard.subprocess, "run", side_effect=boom):
        assert clipboard._darwin_has_image_via_uti() is False


def test_has_image_in_clipboard_falls_back_to_uti(monkeypatch):
    """AppleScript probe returns empty → UTI fallback detects image → True."""
    monkeypatch.setattr(sys, "platform", "darwin")

    def fake_run(cmd, *a, **kw):
        if cmd[0] == "osascript":
            return MagicMock(returncode=0, stdout=b"", stderr=b"")
        if cmd[0] == "swift":
            return MagicMock(returncode=0, stdout=b"public.png\n", stderr=b"")
        raise AssertionError(f"unexpected cmd: {cmd}")

    with patch.object(clipboard.subprocess, "run", side_effect=fake_run):
        assert clipboard.has_image_in_clipboard() is True


def test_has_image_in_clipboard_both_fail(monkeypatch):
    """AppleScript probe empty + Swift UTI also fails → False."""
    monkeypatch.setattr(sys, "platform", "darwin")

    def fake_run(cmd, *a, **kw):
        if cmd[0] == "osascript":
            return MagicMock(returncode=0, stdout=b"", stderr=b"")
        if cmd[0] == "swift":
            return MagicMock(returncode=1, stdout=b"", stderr=b"")
        raise AssertionError(f"unexpected cmd: {cmd}")

    with patch.object(clipboard.subprocess, "run", side_effect=fake_run):
        assert clipboard.has_image_in_clipboard() is False


def test_darwin_read_image_via_uti_success(monkeypatch, tmp_path):
    """Swift writes image data to tmp file and returns UTI type name."""
    monkeypatch.setattr(sys, "platform", "darwin")
    out_file = tmp_path / "clipboard.png"
    png_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16

    def fake_run(cmd, *a, **kw):
        # Simulate Swift writing data to the file embedded in its source
        out_file.write_bytes(png_bytes)
        return MagicMock(returncode=0, stdout=b"public.png\n", stderr=b"")

    with patch.object(clipboard.subprocess, "run", side_effect=fake_run):
        uti = clipboard._darwin_read_image_via_uti(str(out_file))

    assert uti == "public.png"
    assert out_file.read_bytes() == png_bytes


def test_darwin_read_image_via_uti_failure(monkeypatch):
    """Swift returns non-zero exit → _darwin_read_image_via_uti returns None."""
    monkeypatch.setattr(sys, "platform", "darwin")
    fake = MagicMock(returncode=1, stdout=b"", stderr=b"some error")
    with patch.object(clipboard.subprocess, "run", return_value=fake):
        assert clipboard._darwin_read_image_via_uti("/tmp/nope.png") is None


def test_darwin_read_image_via_uti_run_returns_none(monkeypatch):
    """When _run returns None (timeout/OSError), read returns None."""
    monkeypatch.setattr(sys, "platform", "darwin")

    def boom(*a, **kw):
        raise OSError("swift not found")

    with patch.object(clipboard.subprocess, "run", side_effect=boom):
        assert clipboard._darwin_read_image_via_uti("/tmp/nope.png") is None


def test_get_image_from_clipboard_falls_back_to_uti_read(monkeypatch, tmp_path):
    """AppleScript save produces empty format → UTI fallback writes data and
    returns a valid ClipboardImage."""
    monkeypatch.setattr(sys, "platform", "darwin")
    fixed_tmp = tmp_path / "out.png"
    png_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16

    class _FakeNamedTemp:
        def __init__(self, *a, **k):
            self.name = str(fixed_tmp)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(clipboard.tempfile, "NamedTemporaryFile", _FakeNamedTemp)

    def fake_run(cmd, *a, **kw):
        if cmd[0] == "osascript":
            joined = " ".join(cmd)
            if "open for access" in joined:
                # save script — returns empty format (AppleScript can't write)
                return MagicMock(returncode=0, stdout=b"\n", stderr=b"")
            # probe script — returns empty so fallback triggers
            return MagicMock(returncode=0, stdout=b"", stderr=b"")
        if cmd[0] == "swift":
            # UTI fallback — write image data and succeed
            fixed_tmp.write_bytes(png_bytes)
            return MagicMock(returncode=0, stdout=b"public.png\n", stderr=b"")
        raise AssertionError(f"unexpected cmd: {cmd}")

    with patch.object(clipboard.subprocess, "run", side_effect=fake_run):
        out = clipboard.get_image_from_clipboard()

    assert out is not None
    assert out.data == png_bytes
    assert out.media_type == "image/png"
