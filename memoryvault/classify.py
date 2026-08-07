"""Classify each vault file as original, derivative, non-photographic or unknown.

This is the G1 half of the Phase 5 design: a statement *about one file*, never a
claim that two files are the same photo. It exists because the measurements said
linkage is not reliably decidable in this corpus while classification is — and
because the question Phase 5 actually needs answered ("is this incoming file
better than what I already hold?") does not require knowing which original a
derivative came from.

Deliberately absent: any grouping, any winner, any move, any delete.
`classify_record` sees one record and has no access to any other, so a wrong
grouping is not a bug it can have.

WHY THE RULES ARE ORDERED THE WAY THEY ARE

Filename provenance is tested before app tags, because Instagram and Picasa both
strip camera EXIF and both leave a Software tag — so the tag alone cannot tell a
photo an app rewrote from a meme an app produced. IMG_3705.JPG (Instagram,
1440x1440, camera-style name) is a rewritten photo; the UUID-named Picasa file
at 414x490 never was one. The name is what separates them.

Google's downscale ladder is tested before the camera-EXIF rules, because a
Google downscale *keeps the camera tags*. 19,687 files sit at a max dimension of
1600 or 2048 while still carrying Make and Model. Treating camera EXIF as proof
of an original would call every one of them an original, which was the single
biggest error in the first crude split of this vault.

WHY EVERY TEST IS EVALUATED, NOT JUST THE ONE THAT FIRES

`evidence` records the outcome of every predicate whether or not it decided the
verdict. Recording only the winner makes the residual un-revisitable: 778 files
land in `no_camera_evidence`, and a future rule aimed at them would need another
pass over the vault to learn their aspect ratios. With the full set stored, that
rule can be tried against `file_class.evidence` in SQL.
"""

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

CLASSIFIER_VERSION = "g1-2026-08-06"

VERDICTS = ("original", "derivative", "non_photographic", "unknown")

# Only dimensions that are *evidenced* as screenshots in this vault are listed.
# Each has zero files carrying camera Make/Model and is ~100% .png:
#
#     1242x2688  12,722 files   0 with camera EXIF
#     1179x2556   8,846 files   0 with camera EXIF
#     1242x2208   1,275 files   0 with camera EXIF
#     1284x2778     157 files   0 with camera EXIF
#
# A first draft also carried plausible-looking iPhone and iPad resolutions that
# turned out to be photograph sizes here — 1536x2048 has 554 files of which 527
# carry camera EXIF, and 1080x1920 has 34 of 35. Listing a dimension because a
# phone happens to use it, rather than because the vault shows it behaving like
# a screenshot, mislabels real photographs. Any addition needs the same evidence.
PHONE_SCREENS = {(1242, 2688), (1179, 2556), (1242, 2208), (1284, 2778)}
PHONE_SCREENS |= {(h, w) for (w, h) in PHONE_SCREENS}   # landscape captures

# Google's rendition ladder caps the long edge. Native camera output does not
# land on these values with this regularity.
GOOGLE_LONG_EDGE = {1600, 2048}

# Software strings meaning a third-party app rewrote the file. Deliberately
# excludes camera-vendor transfer utilities such as "Nikon Transfer 1.0 W" and
# bare firmware versions like "12.1", which are camera-adjacent, not edits.
APP_RE = re.compile(
    r"instagram|picasa|snapseed|vsco|photoshop|lightroom|gimp|facebook|"
    r"whatsapp|messenger|prisma|befunky|pixlr|canva|irfan|acdsee|"
    r"google photos|photo ?editor|paint\.net", re.I)

MESSENGER_RE = re.compile(r"^\d{15,20}$")
UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
COLLISION_RE = re.compile(r"(\(\d+\))+$")

TINY_PIXELS = 500_000
ORIGINAL_PIXELS = 2_000_000
MIN_CAMERA_PIXELS = 500_000


def _stem(name):
    return COLLISION_RE.sub("", Path(name).stem)


def gather_signals(rec: dict) -> dict:
    """Every signal, and every predicate's outcome, evaluated unconditionally.

    Separated from `classify_record` so the evidence cannot drift from the rules
    that read it, and so the full set is stored even for the file whose verdict
    was settled by the first test.
    """
    w, h = rec.get("width"), rec.get("height")
    px = w * h if (w and h) else None
    make, model = rec.get("make") or None, rec.get("model") or None
    software = str(rec.get("software") or "") or None
    stem = _stem(rec.get("name") or "")
    return {
        "container": rec.get("container"),
        "width": w, "height": h, "px": px,
        "aspect": round(w / h, 4) if (w and h) else None,
        "max_dim": max(w, h) if (w and h) else None,
        "make": make, "model": model, "software": software,
        "exposure": bool(rec.get("exposure")), "gps": bool(rec.get("gps")),
        "stem": stem,
        "dims_error": rec.get("dims_error"),
        "tests": {
            "camera_tags": bool(make or model),
            "screen_dimensions": bool(px and (w, h) in PHONE_SCREENS),
            "generated_filename": bool(MESSENGER_RE.match(stem)
                                       or UUID_RE.match(stem)),
            "app_software": bool(software and APP_RE.search(software)),
            "google_long_edge": bool(w and h and max(w, h) in GOOGLE_LONG_EDGE),
            "tiny": bool(px and px < TINY_PIXELS),
            "camera_sized": bool(px and px >= MIN_CAMERA_PIXELS),
            "original_sized": bool(px and px >= ORIGINAL_PIXELS),
            "has_pixels": px is not None,
        },
    }


def classify_record(rec: dict) -> tuple:
    """Return (verdict, reason, confidence, evidence). First match wins."""
    ev = gather_signals(rec)
    t = ev["tests"]

    # 1. Positively identified as a screen capture, not a photograph.
    if t["screen_dimensions"] and not t["camera_tags"]:
        return "non_photographic", "screenshot", "high", ev

    # 2. A filename the owner never chose: Messenger's numeric ids, or a UUID.
    #    Before app tags, because a Picasa-tagged UUID file is a meme while a
    #    Picasa-tagged IMG_#### file is a photo someone edited.
    if not t["camera_tags"] and t["generated_filename"]:
        return "non_photographic", "generated_filename", "medium", ev

    # 3. A third-party app rewrote it. It may have been a photograph once; what
    #    it is now is that app's output.
    if t["app_software"]:
        return "derivative", "app_rewritten", "high", ev

    # 4. Too small to be anything but a thumbnail, and nothing says camera.
    if t["tiny"] and not t["camera_tags"]:
        return "derivative", "thumbnail", "high", ev

    # 5. Google's ladder. Fires even with camera tags, because a downscale keeps
    #    them — the rule the first crude split was missing.
    if t["google_long_edge"]:
        return "derivative", "google_downscale", "medium", ev

    # 6. Camera tags, full size, and a real exposure or a fix.
    if t["camera_tags"] and t["original_sized"] and (ev["exposure"] or ev["gps"]):
        return "original", "camera_native", "high", ev

    # 7. Camera tags and a plausible size, weaker corroboration.
    if t["camera_tags"] and t["camera_sized"]:
        return "original", "camera_probable", "medium", ev

    # 8. Video and unreadable containers carry no pixels to reason about.
    if not t["has_pixels"]:
        return "unknown", "no_pixels", "low", ev

    # 9. An image with no camera evidence at all. Deliberately NOT called
    #    non-photographic: Google strips EXIF, so this may be a real photograph
    #    whose provenance was destroyed. "Unknown" is the honest answer, and the
    #    full evidence is stored so a later rule can revisit these 778 files
    #    without another pass over the vault.
    if not t["camera_tags"]:
        return "unknown", "no_camera_evidence", "low", ev

    return "unknown", "insufficient_signal", "low", ev


def rows_from_csv(csv_path) -> list:
    """Records from a date-provenance.csv. The fast path: reuses work already
    done rather than decoding 99k images again on an enclosure that has
    faulted twice."""
    import csv as _csv
    out = []
    with open(csv_path, newline="", encoding="utf-8") as f:
        for r in _csv.DictReader(f):
            out.append({
                "path": r["path"], "name": r["name"], "container": r["container"],
                "width": int(r["width"]) if r["width"] else None,
                "height": int(r["height"]) if r["height"] else None,
                "make": r["make"] or None, "model": r["model"] or None,
                "software": r["software"] or None,
                "exposure": r["exposure"] or None,
                "gps": r["gps"] == "True",
            })
    return out


def rows_from_vault(vault: Path, progress=None) -> list:
    """Records read live from the files. Self-contained but slow — a full decode
    pass. Prefer `rows_from_csv` when a current provenance CSV exists."""
    import os
    import piexif
    from PIL import Image
    from memoryvault.containers import detect_container
    from memoryvault.metadata import read_exif

    out = []
    entries = sorted((e.path for e in os.scandir(vault) if e.is_file()))
    for i, path in enumerate(entries, 1):
        p = Path(path)
        try:
            container = detect_container(p).value
        except OSError:
            container = "unreadable"
        exif = read_exif(p) or {}
        zeroth, sub = exif.get("0th", {}), exif.get("Exif", {})

        def txt(v):
            if isinstance(v, bytes):
                return v.decode("utf-8", "replace").strip("\x00 ").strip() or None
            return v
        w = h = None
        dims_error = None
        try:
            with Image.open(p) as im:
                w, h = im.size
        except Exception as exc:
            # Not swallowed: the reason is carried into the record so it reaches
            # `evidence`, where it explains a `has_pixels: False` that would
            # otherwise look like a file with no dimensions rather than a file
            # whose dimensions could not be read.
            dims_error = f"{type(exc).__name__}: {exc}"[:120]
            logger.debug("no dimensions for %s: %s", p, exc)
        out.append({
            "path": path, "name": p.name, "container": container,
            "width": w, "height": h,
            "make": txt(zeroth.get(piexif.ImageIFD.Make)),
            "model": txt(zeroth.get(piexif.ImageIFD.Model)),
            "software": txt(zeroth.get(piexif.ImageIFD.Software)),
            "exposure": sub.get(piexif.ExifIFD.ExposureTime),
            "gps": bool(exif.get("GPS")),
            "dims_error": dims_error,
        })
        if progress and i % 500 == 0:
            progress(i, len(entries))
    return out


def classify_all(records) -> list:
    out = []
    for rec in records:
        verdict, reason, confidence, ev = classify_record(rec)
        out.append({"path": rec["path"], "name": rec["name"], "verdict": verdict,
                    "reason": reason, "confidence": confidence, "evidence": ev})
    return out


def write_verdicts(db, results, classifier=CLASSIFIER_VERSION) -> int:
    """Insert verdicts. Touches no file and no other table.

    The table is owned by `database.SCHEMA`, so it exists as soon as the
    database is opened — no schema is applied from here.
    """
    now = datetime.now(timezone.utc).isoformat()
    ids = {r["path"]: r["id"] for r in db.conn.execute("SELECT id, path FROM files")}
    with db.transaction():
        db.conn.executemany(
            "INSERT INTO file_class (file_id, path, verdict, reason, confidence,"
            " evidence, classifier, classified_at) VALUES (?,?,?,?,?,?,?,?) "
            "ON CONFLICT(path, classifier) DO UPDATE SET "
            "verdict=excluded.verdict, reason=excluded.reason, "
            "confidence=excluded.confidence, evidence=excluded.evidence, "
            "classified_at=excluded.classified_at",
            [(ids.get(r["path"]), r["path"], r["verdict"], r["reason"],
              r["confidence"], json.dumps(r["evidence"], sort_keys=True),
              classifier, now) for r in results])
    return len(results)
