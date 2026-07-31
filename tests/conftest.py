"""Shared fixtures: a real-schema database and real on-disk image files.

Both fixtures deliberately avoid hand-written SQL and synthetic byte blobs —
the database is built by memoryvault.database.Database so schema changes and
migrations are exercised, and the images are real JPEGs written by Pillow so
piexif/EXIF round-trips behave as they do in production.
"""

import io
import json
import zipfile
from pathlib import Path

import piexif
import pytest
from PIL import Image

from memoryvault.database import Database


def make_jpeg_bytes(color: str = "blue", size: tuple[int, int] = (16, 16),
                    exif: dict | None = None) -> bytes:
    """Build a real JPEG, optionally carrying EXIF."""
    img = Image.new("RGB", size, color=color)
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    data = buf.getvalue()

    if exif is not None:
        # piexif.insert accepts raw bytes and returns the modified bytes when
        # given a bytes-like image, so no temp file round-trip is needed.
        data = piexif.insert(piexif.dump(exif), data)
    return data


def make_sidecar_bytes(timestamp: int, lat: float | None = None,
                       lon: float | None = None) -> bytes:
    """Build a Google Takeout supplemental-metadata sidecar."""
    payload = {"photoTakenTime": {"timestamp": str(timestamp)}}
    if lat is not None:
        payload["geoData"] = {"latitude": lat, "longitude": lon}
    return json.dumps(payload).encode("utf-8")


@pytest.fixture
def db(tmp_path) -> Database:
    """A database created through the real schema/migration code path."""
    database = Database(tmp_path / "test.db")
    yield database
    database.close()


@pytest.fixture
def photos(tmp_path) -> Path:
    """A directory of real, distinct small JPEGs."""
    folder = tmp_path / "photos"
    folder.mkdir()
    for i, color in enumerate(["red", "green", "blue", "yellow"]):
        (folder / f"img_{i}.jpg").write_bytes(make_jpeg_bytes(color=color))
    return folder


@pytest.fixture
def make_zip(tmp_path):
    """Factory building a Takeout-shaped zip from {entry_path: bytes}."""
    counter = {"n": 0}

    def _make(files: dict[str, bytes], name: str | None = None) -> Path:
        counter["n"] += 1
        path = tmp_path / (name or f"takeout_{counter['n']}.zip")
        with zipfile.ZipFile(path, "w") as zf:
            for entry_path, data in files.items():
                zf.writestr(entry_path, data)
        return path

    return _make
