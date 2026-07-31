"""Container detection by magic bytes (#7).

The extension lies often enough to matter: 12% of the `.heic` files in the
corpus are JPEGs Google transcoded and never renamed. Trusting `Path.suffix`
both blocks writes that would succeed (JPEG-named-`.heic`) and attempts writes
that cannot succeed (genuine HEIC). Every assertion here is against real bytes.
"""

import pytest

from memoryvault.containers import Container, detect_container, supports_exif
from tests.conftest import make_jpeg_bytes


def test_genuine_heic_detected_as_heif(genuine_heic):
    """A real HEIC, despite piexif being unable to read it."""
    assert detect_container(genuine_heic) is Container.HEIF


def test_genuine_heic_does_not_support_exif(genuine_heic):
    assert not supports_exif(detect_container(genuine_heic))


def test_jpeg_named_heic_detected_as_jpeg(jpeg_named_heic):
    """The 12% case: extension says heic, bytes say JPEG. Bytes win."""
    assert detect_container(jpeg_named_heic) is Container.JPEG


def test_jpeg_named_heic_supports_exif(jpeg_named_heic):
    """This is the write that extension-trust was wrongly refusing."""
    assert supports_exif(detect_container(jpeg_named_heic))


def test_mp4_detected(small_mp4):
    assert detect_container(small_mp4) is Container.MP4
    assert not supports_exif(detect_container(small_mp4))


def test_real_jpeg(tmp_path):
    path = tmp_path / "plain.jpg"
    path.write_bytes(make_jpeg_bytes())
    assert detect_container(path) is Container.JPEG
    assert supports_exif(detect_container(path))


def test_real_webp(tmp_path):
    from PIL import Image
    path = tmp_path / "img.webp"
    Image.new("RGB", (16, 16), color="red").save(path, format="WEBP")
    assert detect_container(path) is Container.WEBP
    assert supports_exif(detect_container(path))


def test_real_tiff(tmp_path):
    from PIL import Image
    path = tmp_path / "img.tiff"
    Image.new("RGB", (16, 16), color="red").save(path, format="TIFF")
    assert detect_container(path) is Container.TIFF


def test_tiff_does_not_support_exif_via_piexif(tmp_path):
    """piexif cannot insert into TIFF, so #7 must stop claiming it can."""
    from PIL import Image
    path = tmp_path / "img.tiff"
    Image.new("RGB", (16, 16), color="red").save(path, format="TIFF")
    assert not supports_exif(detect_container(path))


def test_real_png(tmp_path):
    from PIL import Image
    path = tmp_path / "img.png"
    Image.new("RGB", (16, 16), color="red").save(path, format="PNG")
    assert detect_container(path) is Container.PNG
    assert not supports_exif(detect_container(path))


def test_jpeg_renamed_to_mp4_still_jpeg(tmp_path):
    """Detection must not consult the extension even as a tie-breaker."""
    path = tmp_path / "actually_a_photo.mp4"
    path.write_bytes(make_jpeg_bytes())
    assert detect_container(path) is Container.JPEG


def test_unknown_bytes(tmp_path):
    path = tmp_path / "mystery.bin"
    path.write_bytes(b"\x00" * 64)
    assert detect_container(path) is Container.UNKNOWN
    assert not supports_exif(detect_container(path))


def test_empty_file(tmp_path):
    path = tmp_path / "empty.jpg"
    path.write_bytes(b"")
    assert detect_container(path) is Container.UNKNOWN


def test_missing_file_raises(tmp_path):
    """A missing file is a caller bug, not an 'unknown container'."""
    with pytest.raises(OSError):
        detect_container(tmp_path / "nope.jpg")


def test_truncated_header_does_not_crash(tmp_path):
    """Two bytes is shorter than every signature we test for."""
    path = tmp_path / "tiny.jpg"
    path.write_bytes(b"\xff\xd8")
    assert detect_container(path) in (Container.JPEG, Container.UNKNOWN)


@pytest.mark.parametrize("brand,expected", [
    (b"heic", Container.HEIF),
    (b"heix", Container.HEIF),
    (b"mif1", Container.HEIF),
    (b"heim", Container.HEIF),
    (b"mp42", Container.MP4),
    (b"isom", Container.MP4),
    (b"qt  ", Container.MOV),
])
def test_ftyp_brands(tmp_path, brand, expected):
    """ISO-BMFF brands split HEIF stills from video containers."""
    path = tmp_path / "sample.bin"
    path.write_bytes(b"\x00\x00\x00\x18ftyp" + brand + b"\x00" * 32)
    assert detect_container(path) is expected
