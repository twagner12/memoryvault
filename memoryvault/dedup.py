"""Find duplicate groups and score files to pick the best copy to keep."""

from pathlib import Path

from memoryvault.database import Database

# Format preference: higher = better
FORMAT_SCORES = {
    ".raw": 100, ".cr2": 100, ".nef": 100, ".arw": 100, ".dng": 90,
    ".heic": 70, ".heif": 70,
    ".tiff": 60, ".tif": 60,
    ".png": 50,
    ".bmp": 45,
    ".jpeg": 40, ".jpg": 40,
    ".webp": 35,
    ".gif": 30,
    # Video formats
    ".mov": 80, ".mp4": 75, ".avi": 70, ".mkv": 70, ".wmv": 50,
    # Audio formats
    ".wav": 80, ".flac": 75, ".aiff": 70, ".mp3": 50, ".aac": 45, ".m4a": 45,
}


def score_file(file_record: dict) -> int:
    """Score a file for quality. Higher is better.

    Scoring factors:
      - File size (larger = less compression = better quality)
      - Resolution (width * height, if known)
      - Format preference (RAW > HEIC > JPEG etc.)
      - Has EXIF date
      - Has EXIF GPS
    """
    score = 0

    # File size: normalize to MB, cap contribution at 100 points
    size_mb = file_record.get("size", 0) / (1024 * 1024)
    score += min(int(size_mb * 2), 100)

    # Resolution
    width = file_record.get("width") or 0
    height = file_record.get("height") or 0
    megapixels = (width * height) / 1_000_000
    score += min(int(megapixels * 10), 100)

    # Format preference
    ext = Path(file_record["path"]).suffix.lower()
    score += FORMAT_SCORES.get(ext, 20)

    # Metadata bonuses
    if file_record.get("has_exif_date"):
        score += 25
    if file_record.get("has_exif_gps"):
        score += 25

    return score


def find_duplicates(db: Database) -> list[dict]:
    """Find all duplicate groups and pick winners.

    Returns a list of dicts, each with:
      - blake3: the shared hash
      - winner: the file record to keep
      - losers: list of file records to remove/merge from
      - winner_score: score of the winner
    """
    groups = db.find_duplicate_groups()
    results = []

    for group in groups:
        scored = [(score_file(f), f) for f in group]
        scored.sort(key=lambda x: x[0], reverse=True)

        winner_score, winner = scored[0]
        losers = [f for _, f in scored[1:]]

        results.append({
            "blake3": winner["blake3_full"],
            "winner": winner,
            "losers": losers,
            "winner_score": winner_score,
        })

    return results
