from iac_code.utils.image.format_detect import (
    IMAGE_EXTENSION_REGEX,
    detect_image_format,
)


def test_png_magic_bytes():
    assert detect_image_format(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16) == "image/png"


def test_jpeg_magic_bytes():
    assert detect_image_format(b"\xff\xd8\xff\xe0" + b"\x00" * 16) == "image/jpeg"


def test_gif_magic_bytes():
    assert detect_image_format(b"GIF89a" + b"\x00" * 16) == "image/gif"


def test_webp_magic_bytes():
    buf = b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 16
    assert detect_image_format(buf) == "image/webp"


def test_unknown_defaults_to_png():
    assert detect_image_format(b"junkjunk") == "image/png"


def test_extension_regex():
    assert IMAGE_EXTENSION_REGEX.search("foo.png")
    assert IMAGE_EXTENSION_REGEX.search("/tmp/Bar.JPEG")
    assert IMAGE_EXTENSION_REGEX.search("a.WEBP")
    assert not IMAGE_EXTENSION_REGEX.search("foo.txt")
