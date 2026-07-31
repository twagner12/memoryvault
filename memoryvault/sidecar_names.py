"""Parse Google Takeout sidecar names and bind them to their media file (#6).

Pure name arithmetic — no I/O. Callers pass the directory listing they already
have, which is also what makes a cross-directory bind impossible: this module
never sees a path, only a filename and one directory's candidates.

Three Google behaviours the original two-pattern matcher missed, together
accounting for 43.1% of 43,701 sidecars:

  DSC_0109.JPG.supplemental-metadata(9).json  → media is DSC_0109(9).JPG
      the disambiguation counter lives inside the sidecar name, and belongs
      before the media extension
  photo.jpg.supplemental-meta.json            → suffix truncated to a length cap
      any prefix of "supplemental-metadata" can appear, down to ".s"
  IMG_….jpeg..json                            → empty suffix, double dot

When the cap is tight enough it eats into the media stem as well
(`64122699523__7F4958E8-….json`, extension and all). Those are unreachable by
name arithmetic and fall to a prefix match against the directory, which binds
only when exactly one candidate qualifies.
"""

import re
from dataclasses import dataclass, field
from enum import Enum

# Extensions that can carry a Takeout sidecar. Deliberately broader than
# MEDIA_EXTENSIONS in ingest: a sidecar may describe a .png screenshot that
# ingest itself would still index.
MEDIA_SUFFIXES = frozenset({
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tiff", ".tif", ".heic", ".heif",
    ".webp", ".raw", ".cr2", ".nef", ".arw", ".dng",
    ".mp4", ".mov", ".avi", ".mkv", ".wmv", ".m4v", ".mpg", ".mpeg", ".3gp",
})

_FULL_SUFFIX = "supplemental-metadata"

# Trailing "(12)" immediately before ".json".
_COUNTER = re.compile(r"\((\d+)\)$")

# A stem long enough that a prefix match means something. Below this, a shared
# prefix is coincidence rather than evidence.
_MIN_FUZZY_STEM = 8


class UnmatchedReason(Enum):
    NO_MEDIA_IN_DIR = "no_media_in_dir"
    AMBIGUOUS = "ambiguous"
    UNPARSEABLE = "unparseable"


@dataclass
class ParsedSidecar:
    """The media filename a sidecar name points at, before checking a directory.

    `truncated` records that the `supplemental-metadata` word was cut short.
    `stem_complete` is the stricter, load-bearing one: it says the media name
    survived intact, which it can only claim when a known media extension is
    still on the end. When the length cap ate the extension too, no amount of
    name arithmetic can rebuild the name and only a prefix match can find it.
    """
    media_name: str | None
    counter: str | None = None
    truncated: bool = False
    stem_complete: bool = False
    reason: UnmatchedReason | None = None
    alternatives: list[str] = field(default_factory=list)

    @property
    def all_candidates(self) -> list[str]:
        """Every name worth testing, best first."""
        names = [self.media_name] if self.media_name else []
        return names + [a for a in self.alternatives if a not in names]


@dataclass
class ResolvedSidecar:
    """The outcome of matching a parsed sidecar against a directory listing."""
    media_name: str | None
    parsed: ParsedSidecar
    fuzzy: bool = False
    reason: UnmatchedReason | None = None
    candidate_count: int = 0


def is_sidecar_name(name: str) -> bool:
    """True for Takeout per-media sidecars, excluding album `metadata.json`."""
    lowered = name.lower()
    if not lowered.endswith(".json"):
        return False
    return lowered != "metadata.json"


def _is_supplemental_prefix(text: str) -> bool:
    """True when `text` is a (possibly empty) prefix of the full suffix word."""
    return _FULL_SUFFIX.startswith(text)


def _split_extension(name: str) -> tuple[str, str]:
    """Split a media filename into (stem, extension), extension including the dot."""
    dot = name.rfind(".")
    if dot <= 0:
        return name, ""
    return name[:dot], name[dot:]


def split_sidecar_name(entry_name: str) -> ParsedSidecar:
    """Derive the media filename a sidecar name refers to.

    Returns a ParsedSidecar whose `media_name` is None when the name cannot be
    parsed at all; a name that parses but finds no media is the caller's
    problem to record, not this function's.
    """
    if not entry_name.lower().endswith(".json"):
        return ParsedSidecar(None, reason=UnmatchedReason.UNPARSEABLE)

    body = entry_name[: -len(".json")]
    if not body:
        return ParsedSidecar(None, reason=UnmatchedReason.UNPARSEABLE)

    counter = None
    match = _COUNTER.search(body)
    if match:
        counter = f"({match.group(1)})"
        body = body[: match.start()]

    truncated = False
    dot = body.rfind(".")
    if dot >= 0:
        tail = body[dot + 1:]
        if _is_supplemental_prefix(tail):
            # ".supplemental-metadata" in any truncation, including empty
            # (the `..json` double-dot form).
            truncated = tail != _FULL_SUFFIX
            body = body[:dot]

    if not body:
        return ParsedSidecar(None, reason=UnmatchedReason.UNPARSEABLE)

    stem_complete = _has_media_suffix(body)

    if counter is None:
        return ParsedSidecar(body, truncated=truncated,
                             stem_complete=stem_complete)

    # The counter disambiguates the *media* file, so it sits before the
    # extension: DSC_0109.JPG + (9) → DSC_0109(9).JPG. Some exports append it
    # instead, so that form is offered as a fallback.
    stem, extension = _split_extension(body)
    primary = f"{stem}{counter}{extension}"
    return ParsedSidecar(primary, counter=counter, truncated=truncated,
                         stem_complete=stem_complete,
                         alternatives=[f"{body}{counter}"])


def _has_media_suffix(name: str) -> bool:
    _, extension = _split_extension(name)
    return extension.lower() in MEDIA_SUFFIXES


def resolve_media_name(entry_name: str, candidates: list[str]) -> ResolvedSidecar:
    """Bind a sidecar name to one of `candidates`, or explain why it cannot.

    `candidates` is a single directory's media filenames. Nothing outside that
    list can ever be returned, which is what guarantees no cross-directory bind.
    """
    parsed = split_sidecar_name(entry_name)
    if parsed.media_name is None:
        return ResolvedSidecar(None, parsed, reason=parsed.reason)

    if not candidates:
        return ResolvedSidecar(None, parsed,
                               reason=UnmatchedReason.NO_MEDIA_IN_DIR)

    exact = set(candidates)
    for name in parsed.all_candidates:
        if name in exact:
            return ResolvedSidecar(name, parsed)

    folded = {c.lower(): c for c in candidates}
    for name in parsed.all_candidates:
        hit = folded.get(name.lower())
        if hit is not None:
            return ResolvedSidecar(hit, parsed)

    return _resolve_fuzzy(parsed, candidates)


def _resolve_fuzzy(parsed: ParsedSidecar, candidates: list[str]) -> ResolvedSidecar:
    """Prefix-match a truncated stem, binding only when it is unambiguous.

    Fuzzy matching exists for one reason: Google's filename length cap can cut
    into the media stem itself, leaving a name no amount of arithmetic can
    reconstruct. It is deliberately *not* a general fallback.

    In particular a name that parsed cleanly but carries a `(n)` counter is
    never matched fuzzily. `DSC_1570(1).JPG` and `DSC_1570.JPG` are two
    different photos, and dropping the counter to find "something close"
    attaches one photo's date and location to another — 1,386 of them, when
    replayed against the live database.
    """
    if parsed.stem_complete:
        # The media name survived intact, so the exact tiers above already had
        # everything they needed. Anything further would be a guess.
        return ResolvedSidecar(None, parsed,
                               reason=UnmatchedReason.NO_MEDIA_IN_DIR)

    stem = parsed.media_name or ""
    # The counter was re-attached by us; the truncated original did not carry
    # it in this position, so prefix matching works from the bare stem.
    if parsed.counter:
        stem = stem.replace(parsed.counter, "", 1)

    if len(stem) < _MIN_FUZZY_STEM:
        return ResolvedSidecar(None, parsed,
                               reason=UnmatchedReason.NO_MEDIA_IN_DIR)

    lowered = stem.lower()
    matches = [
        c for c in candidates
        if c.lower().startswith(lowered) and _has_media_suffix(c)
    ]

    # A counter-bearing sidecar must still land on a counter-bearing file.
    if parsed.counter:
        matches = [c for c in matches if parsed.counter in c]

    if len(matches) > 1:
        # An exact stem match outranks a longer prefix. This is the iPhone
        # Live Photo shape: the cap truncates the still's name to exactly the
        # sidecar stem, while its paired video carries one more character
        # before the extension. Both prefix-match; only one is the same name.
        exact_stem = [c for c in matches
                      if _split_extension(c)[0].lower() == lowered]
        if len(exact_stem) == 1:
            return ResolvedSidecar(exact_stem[0], parsed, fuzzy=True,
                                   candidate_count=len(matches))

    if len(matches) == 1:
        return ResolvedSidecar(matches[0], parsed, fuzzy=True,
                               candidate_count=1)
    if len(matches) > 1:
        return ResolvedSidecar(None, parsed, reason=UnmatchedReason.AMBIGUOUS,
                               candidate_count=len(matches))
    return ResolvedSidecar(None, parsed, reason=UnmatchedReason.NO_MEDIA_IN_DIR)
