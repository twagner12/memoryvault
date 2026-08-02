"""Container detection by magic bytes (#7).

The extension lies often enough to matter. Across the 12,924-file shakedown
vault, 4,771 files (37%) are not what their name claims — and the dominant
case is no longer `.heic`:

    .png    4,593 →  4,238 JPEG (92%),  355 PNG
    .heic   3,086 →  2,553 HEIF,        533 JPEG (17%)
    .mp4    1,288 →  1,243 MOV,          45 MP4
    .mov      469 →    289 MOV,         180 MP4

Trusting `Path.suffix` both blocks writes that would succeed
(JPEG-named-`.png`, JPEG-named-`.heic`) and attempts writes that cannot
succeed (genuine HEIC). Every assertion here is against real bytes.
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
    """The 17%-of-`.heic` case: extension says heic, bytes say JPEG. Bytes win."""
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


# --- Leading atoms other than `ftyp` ---

# Copied verbatim from the corpus: 96 `.mp4` files in the 12,924-file shakedown
# vault begin with an 8-byte `wide` placeholder atom followed straight by
# `mdat`, with no `ftyp` anywhere near the front. That is the older QuickTime
# layout, and testing `header[4:8] == b"ftyp"` at a fixed offset classified
# every one of them as UNKNOWN.
WIDE_MDAT = (
    b"\x00\x00\x00\x08wide"          # size 8, type 'wide' — a placeholder
    b"\x00\x3d\x17\xf9mdat"          # size 4,003,321, type 'mdat' — the payload
)


def test_wide_then_mdat_is_quicktime(tmp_path):
    """The 96-file case: a `wide` placeholder ahead of `mdat`."""
    path = tmp_path / "old.mp4"
    path.write_bytes(WIDE_MDAT + b"\x00" * 64)
    assert detect_container(path) is Container.MOV


def test_wide_then_mdat_does_not_support_exif(tmp_path):
    """Reclassifying must not accidentally invite a write into a video."""
    path = tmp_path / "old.mp4"
    path.write_bytes(WIDE_MDAT + b"\x00" * 64)
    assert not supports_exif(detect_container(path))


def test_leading_mdat_is_quicktime(tmp_path):
    """No placeholder at all — `mdat` first is still the QuickTime layout."""
    path = tmp_path / "old.mov"
    path.write_bytes(b"\x00\x00\x10\x00mdat" + b"\x00" * 64)
    assert detect_container(path) is Container.MOV


def test_leading_moov_is_quicktime(tmp_path):
    path = tmp_path / "old.mov"
    path.write_bytes(b"\x00\x00\x02\x00moov" + b"\x00" * 64)
    assert detect_container(path) is Container.MOV


def test_free_atom_before_ftyp_still_reads_the_brand(tmp_path):
    """Skippable atoms are walked past, not treated as the verdict."""
    path = tmp_path / "padded.heic"
    path.write_bytes(b"\x00\x00\x00\x08free"
                     b"\x00\x00\x00\x18ftypheic" + b"\x00" * 32)
    assert detect_container(path) is Container.HEIF


def test_a_zero_sized_atom_does_not_loop(tmp_path):
    """A size of 0 would advance the cursor nowhere. Refuse, don't spin."""
    path = tmp_path / "malformed.mp4"
    path.write_bytes(b"\x00\x00\x00\x00wide" + b"\x00" * 64)
    assert detect_container(path) is Container.UNKNOWN


def test_an_unrecognised_leading_atom_stays_unknown(tmp_path):
    """The walk identifies; it does not guess."""
    path = tmp_path / "mystery.bin"
    path.write_bytes(b"\x00\x00\x00\x08zzzz" + b"\x00" * 64)
    assert detect_container(path) is Container.UNKNOWN


def test_a_truncated_atom_chain_stays_unknown(tmp_path):
    """`wide` whose successor is past the end of the buffer we read."""
    path = tmp_path / "cut.mp4"
    path.write_bytes(b"\x00\x00\xff\xffwide" + b"\x00" * 4)
    assert detect_container(path) is Container.UNKNOWN


def test_ftyp_leading_mp4_is_unchanged(small_mp4):
    """Regression: the normal layout must not be disturbed by the walk."""
    assert detect_container(small_mp4) is Container.MP4


# --- GIF ---

@pytest.mark.parametrize("signature", [b"GIF87a", b"GIF89a"])
def test_gif_signatures(tmp_path, signature):
    """41 GIFs in the shakedown vault, every one of them GIF87a."""
    path = tmp_path / "anim.gif"
    path.write_bytes(signature + b"\x10\x00\x10\x00\x80\x00\x00" + b"\x00" * 32)
    assert detect_container(path) is Container.GIF


def test_gif_does_not_support_exif(tmp_path):
    path = tmp_path / "anim.gif"
    path.write_bytes(b"GIF89a" + b"\x00" * 32)
    assert not supports_exif(detect_container(path))


def test_real_gif_from_pillow(tmp_path):
    from PIL import Image
    path = tmp_path / "img.gif"
    Image.new("RGB", (16, 16), color="red").save(path, format="GIF")
    assert detect_container(path) is Container.GIF


def test_gif_named_jpg_is_still_a_gif(tmp_path):
    path = tmp_path / "photo.jpg"
    path.write_bytes(b"GIF87a" + b"\x00" * 32)
    assert detect_container(path) is Container.GIF
