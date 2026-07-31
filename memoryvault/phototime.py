"""Turn a Takeout UTC epoch into the local wall-clock a camera would have written (#9).

Takeout records `photoTakenTime.timestamp` as seconds since the epoch, UTC.
EXIF `DateTimeOriginal` is camera-local wall-clock time carrying no zone at
all; the zone travels separately in `OffsetTimeOriginal`. Copying one into the
other — which is what ingest did — shifts every recovered date by the shooting
offset, up to ±14 h. The photos affected are precisely those that had no EXIF
date to begin with, so nothing downstream can detect or repair the error.

Four tiers of evidence, best first. Whichever fires, the offset is *recorded*
alongside the time, so a photo's timezone provenance stays auditable instead of
being baked irreversibly into a wall clock.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from functools import lru_cache
from pathlib import Path

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# A sibling shot more than this far from the target says nothing useful about
# the target's timezone — it is likely a different day, place, or trip.
SIBLING_WINDOW = timedelta(hours=12)

# Coordinates are memoised at ~100 m precision. Timezone boundaries are far
# coarser than that, and rounding keeps the cache useful across a burst of
# photos taken in one spot.
_COORD_PRECISION = 3


class TzSource(Enum):
    EXIF = "exif"
    GPS = "gps"
    SIBLING = "sibling"
    UTC_FALLBACK = "utc_fallback"


@dataclass(frozen=True)
class CaptureTime:
    """A resolved capture time and the evidence behind its offset.

    `local_dt` is deliberately naive: it is a wall clock, and attaching a
    tzinfo would invite exactly the aware/naive confusion that caused #9.
    """
    local_dt: datetime
    offset: str
    tz_name: str | None
    tz_source: TzSource

    @property
    def is_assumed(self) -> bool:
        """True when the offset is a guess that should be recorded as deferred."""
        return self.tz_source is TzSource.UTC_FALLBACK


@lru_cache(maxsize=8192)
def _zone_name_for_coords(lat_rounded: float, lon_rounded: float) -> str | None:
    """Map coordinates to an IANA zone name.

    Only this lookup is cached. The offset must never be, because it depends
    on the instant — the same coordinates are -05:00 in July and -06:00 in
    January, and caching a fixed offset per location silently reintroduces a
    one-hour error across half the year.
    """
    return _finder().timezone_at(lat=lat_rounded, lng=lon_rounded)


@lru_cache(maxsize=1)
def _finder():
    """TimezoneFinder loads a sizeable dataset; build it once, lazily."""
    from timezonefinder import TimezoneFinder

    return TimezoneFinder()


def _format_offset(delta: timedelta) -> str:
    """Render a UTC offset as EXIF's ±HH:MM, including sub-hour zones."""
    total_minutes = int(delta.total_seconds() // 60)
    sign = "+" if total_minutes >= 0 else "-"
    total_minutes = abs(total_minutes)
    return f"{sign}{total_minutes // 60:02d}:{total_minutes % 60:02d}"


def _parse_offset(text: str) -> timedelta | None:
    """Parse EXIF's ±HH:MM. Returns None if it is not a usable offset."""
    text = text.strip()
    if len(text) != 6 or text[0] not in "+-" or text[3] != ":":
        return None
    try:
        hours, minutes = int(text[1:3]), int(text[4:6])
    except ValueError:
        return None
    if not (0 <= minutes < 60):
        return None
    delta = timedelta(hours=hours, minutes=minutes)
    return -delta if text[0] == "-" else delta


def _existing_offset(path: Path) -> timedelta | None:
    """Read OffsetTimeOriginal from the target file, if it declares one."""
    from memoryvault.metadata import get_exif_offset

    raw = get_exif_offset(path)
    return _parse_offset(raw) if raw else None


def _sibling_offset(utc_dt: datetime, siblings: list[Path]) -> timedelta | None:
    """Borrow an offset from a nearby file, only when the evidence agrees.

    A single unambiguous answer within the window is usable; two different
    answers mean we do not know, and guessing would be worse than deferring.
    """
    from memoryvault.metadata import get_exif_date, get_exif_offset

    candidates: set[timedelta] = set()
    for sibling in siblings:
        raw_offset = get_exif_offset(sibling)
        if not raw_offset:
            continue
        offset = _parse_offset(raw_offset)
        if offset is None:
            continue

        taken = get_exif_date(sibling)
        if not taken:
            continue
        try:
            sibling_local = datetime.fromisoformat(taken)
        except ValueError:
            continue

        # The sibling's wall clock plus its own offset gives its UTC instant.
        sibling_utc = sibling_local.replace(tzinfo=timezone.utc) - offset
        if abs(sibling_utc - utc_dt) <= SIBLING_WINDOW:
            candidates.add(offset)

    return candidates.pop() if len(candidates) == 1 else None


def resolve_capture_time(utc_epoch: int, geo: dict | None, target_path: Path,
                         siblings: list[Path]) -> CaptureTime:
    """Resolve a UTC epoch to local wall-clock time plus a recorded offset.

    Tiers, in order:
      1. `OffsetTimeOriginal` already on the target — the camera's own claim.
      2. GPS coordinates → IANA zone → offset *at this instant* (resolves DST).
      3. An unambiguous sibling offset within ±12 h.
      4. UTC, flagged `is_assumed` so the caller records it as deferred.
    """
    utc_dt = datetime.fromtimestamp(utc_epoch, tz=timezone.utc)

    offset = _existing_offset(target_path)
    if offset is not None:
        return _build(utc_dt, offset, None, TzSource.EXIF)

    zone_name = _zone_name_from_geo(geo)
    if zone_name:
        try:
            zone = ZoneInfo(zone_name)
        except (ZoneInfoNotFoundError, ValueError):
            zone = None
        if zone is not None:
            # Recomputed per photo: this is what makes DST correct.
            zone_offset = utc_dt.astimezone(zone).utcoffset()
            if zone_offset is not None:
                return _build(utc_dt, zone_offset, zone_name, TzSource.GPS)

    offset = _sibling_offset(utc_dt, siblings)
    if offset is not None:
        return _build(utc_dt, offset, None, TzSource.SIBLING)

    return _build(utc_dt, timedelta(0), None, TzSource.UTC_FALLBACK)


def _zone_name_from_geo(geo: dict | None) -> str | None:
    if not geo:
        return None
    lat, lon = geo.get("lat"), geo.get("lon")
    if lat is None or lon is None:
        return None
    # Google writes 0,0 to mean "no location". The real point is open ocean in
    # the Gulf of Guinea, so treating it as a location would invent a zone.
    if lat == 0 and lon == 0:
        return None
    return _zone_name_for_coords(round(lat, _COORD_PRECISION),
                                 round(lon, _COORD_PRECISION))


def _build(utc_dt: datetime, offset: timedelta, tz_name: str | None,
           tz_source: TzSource) -> CaptureTime:
    local = (utc_dt + offset).replace(tzinfo=None)
    return CaptureTime(local_dt=local, offset=_format_offset(offset),
                       tz_name=tz_name, tz_source=tz_source)
