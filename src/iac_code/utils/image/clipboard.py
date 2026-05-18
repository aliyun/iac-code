"""Cross-platform clipboard image reader.

Mirrors the CC shell-fallback path — we deliberately avoid native bindings.
Returns None when no image is on the clipboard or the platform is unsupported.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loguru import logger

from iac_code.utils.image.format_detect import IMAGE_EXTENSION_REGEX, detect_image_format

_SUBPROCESS_TIMEOUT = 2.0
_SWIFT_SUBPROCESS_TIMEOUT = 10.0  # Swift first launch can be slow

# AppleScript that walks PNG → TIFF → JPEG and returns the matched short name
# (or empty string). Single subprocess call covers all three formats so the
# common screenshot path stays as fast as before while Preview/Browser-style
# TIFF/JPEG sources also get picked up.
_DARWIN_PROBE_SCRIPT = (
    "try\n"
    "    set _ to (the clipboard as «class PNGf»)\n"
    '    return "PNGf"\n'
    "end try\n"
    "try\n"
    "    set _ to (the clipboard as «class TIFF»)\n"
    '    return "TIFF"\n'
    "end try\n"
    "try\n"
    "    set _ to (the clipboard as «class JPEG»)\n"
    '    return "JPEG"\n'
    "end try\n"
    'return ""\n'
)


def _darwin_save_script(tmp_path: str) -> str:
    """Return an AppleScript that writes whichever of PNG/TIFF/JPEG is on the
    clipboard to ``tmp_path``. stdout carries the matched format name (or
    empty string)."""
    # tempfile-generated paths on macOS are sanitized and don't contain quotes,
    # but escape defensively in case a future caller passes a custom path.
    safe = tmp_path.replace("\\", "\\\\").replace('"', '\\"')
    return (
        "on writeData(theData, thePath)\n"
        "    set theFile to (open for access POSIX file thePath with write permission)\n"
        "    set eof of theFile to 0\n"
        "    write theData to theFile\n"
        "    close access theFile\n"
        "end writeData\n"
        "try\n"
        "    set img to (the clipboard as «class PNGf»)\n"
        f'    my writeData(img, "{safe}")\n'
        '    return "PNGf"\n'
        "end try\n"
        "try\n"
        "    set img to (the clipboard as «class TIFF»)\n"
        f'    my writeData(img, "{safe}")\n'
        '    return "TIFF"\n'
        "end try\n"
        "try\n"
        "    set img to (the clipboard as «class JPEG»)\n"
        f'    my writeData(img, "{safe}")\n'
        '    return "JPEG"\n'
        "end try\n"
        'return ""\n'
    )


@dataclass
class ClipboardImage:
    data: bytes
    media_type: str
    source_path: str | None = None
    filename: str | None = None


def _which(*names: str) -> str | None:
    for name in names:
        path = shutil.which(name)
        if path:
            return path
    return None


def _run(
    cmd: list[str], timeout: float = _SUBPROCESS_TIMEOUT, **kwargs: Any
) -> subprocess.CompletedProcess[bytes] | None:
    """Run *cmd* with a timeout, returning None on TimeoutExpired or OSError."""
    try:
        return subprocess.run(cmd, capture_output=True, timeout=timeout, **kwargs)
    except (subprocess.TimeoutExpired, OSError):
        return None


def _darwin_has_image_via_uti() -> bool:
    """Fallback: use Swift/NSPasteboard to detect modern UTI image types.

    Chromium-based apps (Chrome, Figma, Electron) only write modern UTI types
    (public.png, public.tiff) to the pasteboard, which the legacy AppleScript
    four-char-code probe cannot see. This function shells out to Swift to check
    NSPasteboard directly.
    """
    swift_code = (
        "import Cocoa\n"
        "let pb = NSPasteboard.general\n"
        "let types = pb.types?.map { $0.rawValue } ?? []\n"
        'let imageUTIs = ["public.png", "public.tiff", "public.jpeg", "com.apple.pict"]\n'
        "for uti in imageUTIs {\n"
        "    if types.contains(uti) {\n"
        "        print(uti)\n"
        "        exit(0)\n"
        "    }\n"
        "}\n"
        "exit(1)\n"
    )
    r = _run(["swift", "-e", swift_code], timeout=_SWIFT_SUBPROCESS_TIMEOUT)
    if r is None or r.returncode != 0:
        if r is not None and r.stderr:
            logger.debug("clipboard._darwin_has_image_via_uti: swift stderr={!r}", r.stderr[:300])
        return False
    return True


def _darwin_read_image_via_uti(tmp_path: str) -> str | None:
    """Fallback: read image data from clipboard using Swift/NSPasteboard.

    Writes the first matched image type (public.png > public.tiff > public.jpeg)
    to *tmp_path* (passed as a command-line argument to avoid path-escaping
    issues) and returns the UTI type string on success, None otherwise.
    """
    swift_code = (
        "import Cocoa\n"
        "let pb = NSPasteboard.general\n"
        "let imageTypes: [(String, NSPasteboard.PasteboardType)] = [\n"
        '    ("public.png", .png),\n'
        '    ("public.tiff", .tiff),\n'
        '    ("public.jpeg", NSPasteboard.PasteboardType("public.jpeg")),\n'
        "]\n"
        'let outPath = CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : "/tmp/clipboard_img"\n'
        "for (name, type) in imageTypes {\n"
        "    if let data = pb.data(forType: type) {\n"
        "        let url = URL(fileURLWithPath: outPath)\n"
        "        try! data.write(to: url)\n"
        "        print(name)\n"
        "        exit(0)\n"
        "    }\n"
        "}\n"
        "exit(1)\n"
    )
    r = _run(["swift", "-e", swift_code, tmp_path], timeout=_SWIFT_SUBPROCESS_TIMEOUT)
    if r is None or r.returncode != 0:
        if r is not None and r.stderr:
            logger.debug("clipboard._darwin_read_image_via_uti: swift stderr={!r}", r.stderr[:300])
        return None
    return r.stdout.strip().decode("ascii", errors="replace")


def has_image_in_clipboard() -> bool:
    if sys.platform == "darwin":
        r = _run(["osascript", "-e", _DARWIN_PROBE_SCRIPT])
        if r is None:
            logger.debug("clipboard.has_image_in_clipboard[darwin]: osascript timed out")
            return False
        if r.returncode != 0:
            logger.debug(
                "clipboard.has_image_in_clipboard[darwin]: osascript exit={} stderr={!r}",
                r.returncode,
                r.stderr[:200],
            )
            return False
        fmt = r.stdout.strip().decode("ascii", errors="replace")
        if fmt:
            logger.info("clipboard.has_image_in_clipboard[darwin]: format={!r}", fmt)
            return True
        # AppleScript found nothing – try modern UTI types via Swift/NSPasteboard
        logger.debug("clipboard.has_image_in_clipboard[darwin]: AppleScript probe empty, trying UTI fallback")
        if _darwin_has_image_via_uti():
            logger.info("clipboard.has_image_in_clipboard[darwin]: UTI fallback detected image")
            return True
        logger.info("clipboard.has_image_in_clipboard[darwin]: no image detected")
        return False
    if sys.platform.startswith("linux"):
        yes = False
        if os.environ.get("WAYLAND_DISPLAY") and _which("wl-paste"):
            r = _run(["wl-paste", "-l"])
            if r is not None and r.returncode == 0 and b"image/" in r.stdout.lower():
                yes = True
        if not yes and _which("xclip"):
            r = _run(["xclip", "-selection", "clipboard", "-t", "TARGETS", "-o"])
            if r is not None and r.returncode == 0 and b"image/" in r.stdout.lower():
                yes = True
        return yes
    if sys.platform == "win32":
        ps = "$null -ne (Get-Clipboard -Format Image)"
        r = _run(["powershell", "-NoProfile", "-Command", ps])
        if r is None:
            return False
        return r.returncode == 0 and b"True" in r.stdout
    return False


def get_image_from_clipboard() -> ClipboardImage | None:
    if not has_image_in_clipboard():
        return None
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        if sys.platform == "darwin":
            r = _run(["osascript", "-e", _darwin_save_script(tmp_path)])
            if r is None:
                logger.warning("clipboard.get_image_from_clipboard[darwin]: osascript save timed out")
                return None
            fmt = ""
            if r.returncode == 0:
                fmt = r.stdout.strip().decode("ascii", errors="replace")
            if not fmt:
                # AppleScript could not save – try modern UTI fallback via Swift
                logger.debug("clipboard.get_image_from_clipboard[darwin]: AppleScript save empty, trying UTI fallback")
                uti = _darwin_read_image_via_uti(tmp_path)
                if not uti:
                    logger.info("clipboard.get_image_from_clipboard[darwin]: no image class on clipboard")
                    return None
                logger.info("clipboard.get_image_from_clipboard[darwin]: UTI fallback matched type={!r}", uti)
            else:
                logger.info("clipboard.get_image_from_clipboard[darwin]: matched class={!r}", fmt)
            # NOTE: detect_image_format does not recognise TIFF magic bytes
            # and will return "image/png" as a fallback. That's harmless —
            # handle_image_paste passes the bytes straight to
            # maybe_resize_and_downsample which uses Pillow to decode TIFF
            # and re-encode to PNG/JPEG with a correct media_type.
        elif sys.platform.startswith("linux"):
            wrote_bytes = False
            if os.environ.get("WAYLAND_DISPLAY") and _which("wl-paste"):
                with open(tmp_path, "wb") as f:
                    try:
                        subprocess.run(
                            ["wl-paste", "--type", "image/png"],
                            stdout=f,
                            timeout=_SUBPROCESS_TIMEOUT,
                        )
                    except subprocess.TimeoutExpired:
                        pass
                wrote_bytes = Path(tmp_path).stat().st_size > 0
            if not wrote_bytes and _which("xclip"):
                with open(tmp_path, "wb") as f:
                    try:
                        subprocess.run(
                            ["xclip", "-selection", "clipboard", "-t", "image/png", "-o"],
                            stdout=f,
                            timeout=_SUBPROCESS_TIMEOUT,
                        )
                    except subprocess.TimeoutExpired:
                        pass
                wrote_bytes = Path(tmp_path).stat().st_size > 0
            if not wrote_bytes:
                return None
        elif sys.platform == "win32":
            # Defense in depth: tempfile.NamedTemporaryFile already produces a
            # safe path, but escape single quotes so this remains injection-safe
            # if a future caller passes an externally-supplied path.
            safe_tmp_path = tmp_path.replace("'", "''")
            ps = (
                "Add-Type -AssemblyName System.Windows.Forms;"
                "$img = [System.Windows.Forms.Clipboard]::GetImage();"
                f"if ($img -ne $null) {{ $img.Save('{safe_tmp_path}') }}"
            )
            r = _run(["powershell", "-NoProfile", "-Command", ps])
            if r is None:
                return None
        else:
            return None

        data = Path(tmp_path).read_bytes()
        if not data:
            logger.warning("clipboard.get_image_from_clipboard: tempfile is empty after write")
            return None
        media_type = detect_image_format(data)
        logger.info(
            "clipboard.get_image_from_clipboard: returning {} bytes, media_type={}",
            len(data),
            media_type,
        )
        return ClipboardImage(data=data, media_type=media_type)
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def try_read_image_from_path(text: str) -> ClipboardImage | None:
    """If *text* looks like a path to an existing image file, read and return it."""
    candidate = text.strip().strip("'").strip('"').replace("\\ ", " ")
    if not IMAGE_EXTENSION_REGEX.search(candidate):
        return None
    p = Path(candidate)
    if not p.is_absolute() or not p.exists() or not p.is_file():
        logger.debug("clipboard.try_read_image_from_path: candidate {!r} not a readable file", candidate)
        return None
    try:
        data = p.read_bytes()
    except OSError as exc:
        logger.warning("clipboard.try_read_image_from_path: read error {}", exc)
        return None
    if not data:
        return None
    logger.info("clipboard.try_read_image_from_path: read {} bytes from {}", len(data), p)
    return ClipboardImage(
        data=data,
        media_type=detect_image_format(data),
        source_path=str(p),
        filename=p.name,
    )
