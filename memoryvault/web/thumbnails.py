"""Thumbnail generation and caching."""

import subprocess
from pathlib import Path

from PIL import Image

THUMB_SIZE = 400
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tiff", ".tif", ".webp", ".heic", ".heif"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".wmv", ".m4v", ".mpg", ".mpeg", ".3gp"}


def get_or_create_thumbnail(file_path: str, blake3_hash: str, thumb_dir: Path,
                            size: int = THUMB_SIZE) -> Path | None:
    """Get a cached thumbnail or create one. Returns path to thumbnail or None."""
    if not blake3_hash:
        return None

    cached = thumb_dir / f"{blake3_hash}_{size}.jpg"
    if cached.exists():
        return cached

    source = Path(file_path)
    if not source.exists():
        return None

    ext = source.suffix.lower()

    if ext in IMAGE_EXTENSIONS:
        return _thumbnail_image(source, cached, size)
    elif ext in VIDEO_EXTENSIONS:
        return _thumbnail_video(source, cached, size)

    return None


def _thumbnail_image(source: Path, dest: Path, size: int) -> Path | None:
    """Generate thumbnail for an image file."""
    try:
        with Image.open(source) as img:
            img.thumbnail((size, size))
            if img.mode in ("RGBA", "P"):
                img = img.convert("RGB")
            img.save(dest, "JPEG", quality=80)
            return dest
    except Exception:
        return None


def _thumbnail_video(source: Path, dest: Path, size: int) -> Path | None:
    """Generate thumbnail from video using ffmpeg."""
    try:
        result = subprocess.run(
            ["ffmpeg", "-i", str(source), "-ss", "1", "-vframes", "1",
             "-vf", f"scale={size}:-1", "-y", str(dest)],
            capture_output=True, timeout=30,
        )
        if result.returncode == 0 and dest.exists():
            return dest
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return None
