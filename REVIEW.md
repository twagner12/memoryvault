# photo-dedup (MemoryVault) — Architecture Review

## Context

Review requested before any code changes. Target scale is 1 TB+ / 200k+ files across
Google Drive, OneDrive, Google Photos (Takeout) and iCloud. Findings below are ranked by
damage-at-scale, not style, and are grounded in the **live `memoryvault.db` (142 MB)**
already in the repo — 160,198 files, 596 GB indexed, 14 archives ingested. Where a claim
is backed by a query or an empirical test against real files, the number is quoted.

No files were modified.

---

## What actually exists

Local-only, single-threaded, ~3.4k LOC:

- `scanner.py` — walks a folder, hashes every file, upserts to SQLite
- `archive.py` — streams entries out of zips (libarchive, 7z CLI fallback)
- `ingest.py` — single pass: stream entry → hash → skip-if-known → write + merge sidecar
- `dedup.py` — groups by `blake3_full`, scores to pick a winner
- `metadata.py` — piexif read/write, Takeout sidecar parsing
- `web/` — Flask UI, thumbnails, background `TaskManager`

**There is no provider layer.** `grep -riE "oauth|onedrive|icloud|graph\.microsoft|googleapis|429|backoff|token|requests\.|httpx"` returns zero hits across the source and `pyproject.toml`. Drive/OneDrive/iCloud are reached only by the user manually exporting a zip or mounting a sync folder. That single fact answers questions 1, 4, and most of 5.

---

# Tier 1 — Data loss, live right now

### 1. "Already have it" is decided by a DB row, never by a file on disk

`ingest.py:283` — `db.get_files_by_full_hash(full_hash)` returns a row → entry is logged
`skipped/duplicate` and **the bytes are never written**. Nothing checks that the winning
copy still exists. `_try_merge_from_duplicate` does check `existing_path.exists()`
(`ingest.py:379`) but returns silently, so the metadata is dropped too.

Measured on the live DB right now:

```
129,143 rows (376 GB, source='local') point at /run/media/tim/Seagate Backup Plus Drive/…
$ mount | grep -i seagate   →  Seagate NOT mounted
disk check: 3000/3000 sampled 'local' rows MISSING from disk
```

**Why it matters:** the index currently asserts ownership of 376 GB that is not attached.
Ingest a Takeout zip today and every photo also present on that drive is discarded — not
moved, not copied, discarded — with no warning. Unplug or wipe the drive and those files
exist nowhere. This is the one finding that destroys originals rather than wasting time.

**Smallest fix:** in `_process_media_entry_inner`, before returning `skipped`, require
`Path(existing["path"]).exists()`; if it doesn't, treat the entry as new. Add a
`volume_root` column (or reuse the `source` label) and refuse to start an ingest when a
volume backing indexed rows is unreachable — fail loud, don't silently dedupe against ghosts.

### 2. Every duplicate group is auto-resolved with no human review, by a scorer that is inverted for this corpus

`web/blueprints/battle.py:21-36` loops over **all** unresolved groups and calls
`resolve_group(..., confidence=100, auto_resolved=True)` before checking `remaining` —
which is therefore always empty, so `/battle/arena` is unreachable. Live DB: `resolutions
= 2,903`, duplicate groups = `2,903`, **100% auto**.

The sole arbiter is `score_file` (`dedup.py:24`), and it is wrong twice over:

- `width`/`height` are **NULL for all 160,198 rows** — nothing anywhere populates them —
  so the resolution term contributes 0 always. Score reduces to size + extension + two flags.
- `FORMAT_SCORES` ranks `.heic` 70 over `.jpg` 40, but the extension lies. Sampling 4,000
  takeout `.heic` files by magic bytes: **471 (12%) are actually JPEG** — Google transcoded
  them and kept the name. Those Google-compressed JPEGs outrank the true originals.

**Why it matters:** nothing deletes yet, so this is latent — but `resolutions` is the table
a future "apply" step will read, and it is already fully populated with unreviewed verdicts.
The first delete feature ships pre-loaded with 2,903 machine-picked winners.

**Smallest fix:** in `battle.index`, only auto-resolve when the group is unambiguous
(single distinct extension and equal size); route the rest to the arena. In `score_file`,
sniff magic bytes rather than `Path.suffix`, and drop the resolution term until width/height
are actually recorded.

### 3. Zero-byte files collapse into one duplicate group

`scan_folder` has no size guard (`hasher.find_exact_duplicates` skips `size == 0`, but that
function is **dead code** — the scan path calls `hash_file_progressive`). All empty files
hash to blake3's empty digest.

```
size=0 files: 18   →  all in one group, blake3 af1349b9f5f9…41f3262
```

**Why it matters:** small today, but these are 18 distinct files presented as 17 deletable
duplicates. Same class of bug as #1: identity asserted from a hash that carries no identity.

**Smallest fix:** `if size == 0: continue` in `scan_folder`, and store `blake3_full = NULL`
for empty entries in ingest.

### 4. A mid-stream libarchive failure silently restarts the whole archive under 7z

`archive.py:53-64` — `iter_entries` wraps `yield from _stream_libarchive(...)` in
`except Exception: yield from _stream_7z(...)`. Because it wraps a **generator**, a failure
at entry 50,000 is caught and the archive replays from entry 0 under 7z. `skip_entries` was
snapshotted before the run, so entries already processed *this run* are re-yielded, re-hashed,
and — now present in the DB — logged `skipped/duplicate`, **overwriting their `kept` status
and `kept_path`** via the upsert in `log_archive_entry`.

**Why it matters:** the audit trail that says "this file was kept, here" is silently rewritten
to "this was a duplicate, discarded." That is the record you would use to verify nothing was
lost. Note `_stream_7z` also spawns one `7z e` per entry — an O(n) full-archive scan each time.

**Smallest fix:** decide the backend once, before iterating (probe with `list_entries`), and
let mid-stream errors propagate instead of triggering a silent restart.

### 5. 2,156 entries dropped as `corrupt` with no retry and no surfaced report

Live DB: `status='error', skip_reason='corrupt'` → **2,156** entries (~7% of the 30,989 kept).
These are entries where `get_blocks()` raised and `_read_to_temp` returned
`ArchiveEntry(size=0, data=None)` (`archive.py:146-152`). The CLI prints a count; nothing
lists them.

**Why it matters:** 2,156 files may or may not exist elsewhere. Nobody knows, and the run
reports success.

**Smallest fix:** a `memoryvault errors <archive>` command that dumps `archive_entries WHERE
status='error'`, and mark the archive `complete_with_errors` so it is not treated as done.

---

# Tier 2 — Correctness at scale

### 6. 43.1% of Takeout sidecars never bind to their media file

`_media_name_for_sidecar` (`ingest.py:42`) handles exactly two patterns. Measured across all
43,701 ingested sidecars:

```
 24,861  56.9%  matched
 16,582  37.9%  FAIL: DSC_0109.JPG.supplemental-metadata(9).json  ← media is DSC_0109(9).JPG
  1,428   3.3%  FAIL: truncated — .supplemental-meta.json / .supplemental-metada.json
    678   1.6%  FAIL: other
    152   0.3%  FAIL: IMG_….jpeg..json  (double dot)
─────────────────
 18,840  43.1%  unmatched
```

Google puts its `(n)` disambiguation counter **inside** the sidecar name and truncates the
`supplemental-metadata` suffix to fit a filename length cap. Neither is handled.

**Why it matters:** Takeout exposes no hashes and often strips EXIF; the sidecar is frequently
the *only* record of when and where a photo was taken. 18,840 of them were parsed, discarded,
and logged as `skipped/sidecar` — indistinguishable from success.

**Smallest fix:** rewrite `_media_name_for_sidecar` to regex off
`^(?P<stem>.+?)\.(supplemental-met\w*)?(?P<n>\(\d+\))?\.json$`, re-attach `(n)` to the stem,
and fall back to fuzzy stem match within the archive directory. Count and report unmatched
sidecars in `stats`.

### 7. HEIC and TIFF metadata writes fail silently — verified

`EXIF_EXTENSIONS` (`metadata.py:12`) includes `.heic .heif .tiff .tif`, but piexif supports
only JPEG and WebP. Tested against a genuine HEIC from the vault:

```
piexif.load   → InvalidImageDataError: Given file is neither JPEG nor TIFF.
piexif.insert → InvalidImageDataError
TIFF insert   → InvalidImageDataError
```

Every call site wraps these in `except Exception: pass` (`ingest.py:244`, `:327`, `:391`;
`metadata.py:271`). The evidence in the live DB is unambiguous:

```
.heic files indexed:            17,164
metadata_log rows targeting .heic:   11   ← and all 3 sampled were actually JPEG-in-.HEIC
```

**Why it matters:** HEIC is the iCloud default and 17k files here. Reads return None (so
`has_exif_date` is false-negative for every real HEIC, which then feeds `score_file`), and
writes are no-ops that report success.

**Smallest fix:** cut `.heic/.heif/.tiff/.tif` from `EXIF_EXTENSIONS` so the code stops
pretending, and add a sidecar table (`metadata_pending`) recording date/GPS that could not be
embedded. Longer term the writer needs `exiftool` — it is the only thing that handles HEIC,
video containers, and XMP.

### 8. Video metadata is dropped wholesale

`can_have_exif` is False for `.mp4/.mov`, so in `_process_media_entry_inner` the entire
sidecar block is skipped — no EXIF, no XMP, and **the file mtime is not set either**.

```
takeout .mp4/.mov indexed: 4,741   with has_exif_date: 0
```

**Why it matters:** 4,741 videos landed in the vault with their Takeout date discarded and no
fallback. Every photo-management tool downstream will date them by filesystem mtime, which is
the ingest time.

**Smallest fix:** when a sidecar has a date and the target can't hold EXIF, at minimum
`os.utime(dest, (ts, ts))` and log the merge. One line, recovers the date for all 4,741.

### 9. Takeout timestamps are written as UTC into a local-time field

`_parse_sidecar_data` (`ingest.py:212`) builds `datetime.fromtimestamp(ts, tz=timezone.utc)`;
`write_exif_date` (`metadata.py:113`) then `strftime`s it into `DateTimeOriginal`, which by
EXIF convention is **camera-local wall-clock time with no zone**.

**Why it matters:** every date recovered from a sidecar is shifted by the shooting timezone
offset — up to ±14 h. Photos land on the wrong day, which breaks date-based organization for
exactly the files that had no EXIF to begin with.

**Smallest fix:** prefer the sidecar's `photoTakenTime.formatted` local rendering, or carry
the offset from a sibling file's `OffsetTimeOriginal`. If neither is available, write UTC
*and* set `OffsetTimeOriginal = "+00:00"` so the ambiguity is recorded rather than lost.

### 10. Writing EXIF wipes EXIF when the file can't be parsed

Both writers do:

```python
try:    exif = piexif.load(str(path))
except: exif = {"0th": {}, "Exif": {}, "GPS": {}, "1st": {}}   # metadata.py:118-119, 131-133
...     piexif.insert(piexif.dump(exif), str(path))
```

A file whose EXIF piexif cannot parse — malformed maker notes are common — gets its Exif APP1
segment **replaced with a near-empty one**. Separately, `write_exif_gps` assigns
`exif["GPS"] = gps_ifd` wholesale (`metadata.py:144`), discarding GPSAltitude, GPSTimeStamp,
GPSDateStamp and GPSImgDirection whenever it merges a lat/lon.

**Smallest fix:** on load failure, abort the write instead of substituting an empty dict.
For GPS, update keys in the existing IFD rather than replacing it.

### 11. Stored hashes drift from the bytes on disk

In `_process_media_entry_inner` the order is: hash source bytes → write file → **write EXIF
into the file** → compute head/tail from the now-modified file → store the *pre-write*
`blake3_full` and *pre-write* `size`. `_try_merge_from_duplicate` likewise mutates an
already-indexed file without touching its row.

**Why it matters:** the row's `blake3_full`, `blake3_head` and `size` describe three different
byte streams. Re-scanning the vault (`memoryvault scan`) recomputes a *different*
`blake3_full` for the same photo — so the next Takeout zip containing it will not match and it
gets kept a second time. With a 200k-file, multi-part Takeout that is a compounding source of
false uniques.

**Smallest fix:** re-hash `dest_path` after all metadata writes and store that, keeping the
source-bytes hash in a separate `source_blake3` column for archive-entry provenance.

### 12. Rescanning re-hashes all 1 TB

`scan_folder` (`scanner.py:38-57`) calls `hash_file_progressive` on every path
unconditionally. There is no `size`+`mtime` comparison against the existing row, even though
both columns exist and are populated. `hash_file_progressive` also reads the full file always
(and opens it three times — head, tail, full).

**Why it matters:** this is the direct answer to "if a scan dies at hour six." Batches of 500
are committed (`scanner.py:51`), so the partial index survives and is crash-safe under WAL —
but the rerun re-hashes from byte zero. At 596 GB already indexed and 1 TB targeted, that is
hours of pure re-read every time.

**Smallest fix:** before hashing, `SELECT size, mtime FROM files WHERE path = ?` and skip if
both match. Three lines, and it makes resume nearly free.

### 13. Nothing parallel, and a commit per file

`TaskManager` uses `ThreadPoolExecutor(max_workers=1)` (`tasks.py:39`), and the scan loop is
serial. Meanwhile `upsert_file`, `log_archive_entry`, `log_metadata_merge` and `resolve_group`
each end in `self.conn.commit()` — ingest does ~3 fsyncs per file. `PRAGMA synchronous` is
left at the default `FULL`.

**Why it matters:** BLAKE3 is fast enough that this workload is I/O- and fsync-bound, not
CPU-bound. 200k files × 3 commits is 600k fsyncs of pure overhead.

**Smallest fix:** `PRAGMA synchronous=NORMAL` (safe under WAL) and batch archive-entry logging
the way `bulk_upsert_files` already batches scans — commit every N entries, which still bounds
re-work on crash to N. Then a `ThreadPoolExecutor` over the hashing stage in `scan_folder`,
which is embarrassingly parallel.

---

# Tier 3 — Structural gaps against the stated target

### 14. No hash reconciliation layer exists (question 1)

The schema carries `blake3_head/tail/full` and nothing else. There is no column for
Drive's `md5Checksum`, OneDrive's `quickXorHash`, or an iCloud digest, no notion of "which
digests this file has," and no fallback ladder. The architecture assumes local bytes are
always available, so it always computes the content hash — meaning **for a cloud source you
must download all 1 TB before you can dedup any of it**. There is no trigger condition for a
download because there is no download.

**Smallest change that opens the door:** a `file_digests(file_id, algo, value)` table plus a
reconciliation rule — match on any shared `algo`; if two candidates share *no* algorithm,
mark the pair `needs_content_hash` and let a separate, resumable, rate-limited pass fetch
bytes only for those. That keeps `files` unchanged and makes the download an explicit,
budgeted queue rather than an implicit precondition.

### 15. No near-duplicate detection (question 2)

Nothing perceptual anywhere. An original and Google's re-compressed copy are, to this code,
two unrelated files — which is exactly the corpus here: 596 GB spanning a Takeout export
*and* a local drive that already contains a Takeout folder.

**Where it slots in without restructuring the index:** `dedup.py` groups on `blake3_full`;
near-duplicates need a *second* grouping pass over files that are already unique by exact
hash. So: add a `phash INTEGER` column plus an index, compute a 64-bit dHash in the path that
already decodes images (`web/thumbnails.py` — Pillow is already a dependency, and thumbnails
are already cached by `blake3_full`), then group by Hamming distance using 4×16-bit banded
prefix lookups. `find_duplicates` gains a sibling `find_near_duplicates`; the `resolutions`
table already carries a `confidence` column that was clearly meant for this, and the battle
arena UI already exists to review sub-100% matches. No schema restructuring, no re-read of
file bytes beyond thumbnail generation.

### 16. Nothing can express a 429 or an expired token (question 4)

No network code, so nothing happens today — but the call graph cannot accommodate it either:
`iter_entries` is a bare generator with no cursor, `scan_folder` catches only `OSError`,
`TaskManager` has no notion of resuming from a page token, and `progress_callback` has no
"paused/retrying" state.

**Smallest change:** define a `Source` protocol (`list(cursor) -> (entries, next_cursor)`,
`fetch(entry) -> bytes`) and persist `next_cursor` per source in a `sources` table. Retry and
backoff then live in one adapter, and a token expiry becomes "stop, refresh, resume from
cursor" rather than "lose the run."

### 17. The scan/fetch split (question 5)

Genuinely single-pass, and for a local zip that is correct — zip entries are sequential, so
you must decompress to hash. Two costs are avoidable, though:

- Entries over 50 MB are written to a **temp file on disk before hashing**
  (`archive.py:110-112`), then deleted if duplicate. The live DB shows **51,863 entries
  skipped as duplicates** — every large video among them paid a full write-then-delete.
  Hash the stream as it is read and only materialize once known-unique.
- Resume still decompresses skipped entries (`archive.py:103` — `for _ in entry.get_blocks(): pass`),
  which is unavoidable for zip, but it means resuming a 50 GB archive at 90% still costs 90%
  of the decompression. Worth telling the user rather than showing a stalled progress bar.

Also: `entries_total` is **0 for all 14 archives** — `ingest_archive` passes `entries_total=0`
and never calls the imported `count_entries`, so every progress readout divides by a fake
total (`total=processed_count`, `ingest.py:165`).

### 18. Live Photos are split; HEIC/MOV pairing does not exist

No pairing concept anywhere. The `.mov` half is deduped independently of its `.heic` half,
so a shared motion clip is discarded as a duplicate while its still is kept. Worse,
`_unique_dest_path` (`ingest.py:427`) renames collisions to `IMG_1234(1).mov`, breaking the
stem match that any downstream tool would use to re-pair them.

```
takeout files with a (n) collision suffix: 7,258
```

**Smallest fix:** name destination files by `blake3_full` prefix in a date-sharded directory
and keep the original name in the DB — this removes the `(n)` renaming entirely — then add a
`pair_id` linking same-stem HEIC/MOV entries and resolve them as a unit.

### 19. The scanner indexes everything; ingest filters. They disagree.

`scan_folder` has no media filter, so `memoryvault scan` on a folder containing an unzipped
Takeout indexes the sidecars as first-class files:

```
.json rows in files: 19,047   (all source='local')
```

Those JSON files then participate in duplicate grouping and get scored by `score_file` with
the default extension score of 20. Ingest, by contrast, applies `is_media_file`.

**Smallest fix:** apply `is_media_file` in `scan_folder` too, and index sidecars into a
separate table where they can actually be *used* — a scanned Takeout folder currently throws
away the same metadata that `ingest` tries hard to preserve.

### 20. Minor, but they will bite

- `find_duplicate_groups` (`database.py:161`) issues one query per group — 2,904 queries per
  call, and `/api/stats` calls it on **every dashboard poll**. Replace with a single
  self-join.
- `iter_files` (`scanner.py:15`) materializes the entire tree into a list before hashing;
  at 200k+ files that is a long, silent, unpersisted phase. Make it a generator and emit
  progress.
- `extract_entry_to_path` writes straight to the final path, then the DB row is committed
  after. A crash between the two leaves an orphan file that the next run re-extracts as
  `name(1).ext`. Write to `dest.tmp` and `os.replace` after the row is committed.
- Archive 14 (`OneDrive.zip`) is stuck `status='in_progress', entries_processed=0` — a
  crashed run with no way to distinguish "resumable" from "abandoned" in the UI.
- Archives 2 and 3 are the **same zip** under different filenames (`…-4-007.zip` and
  `…-4-007 (1).zip`). Both were fully decompressed and hashed; #3 produced 8,550 duplicates
  and 0 keeps. The fingerprint in `ingest.py:78` is computed but only ever compared by
  *path* via `register_archive`'s `ON CONFLICT(path)`, so identical zips are never
  short-circuited. Query `archives` by `blake3` before streaming.

---

# The three I would fix first

**1. Make "duplicate" require a verifiable surviving copy (finding #1).**
Everything else on this list wastes time, storage, or metadata. This one destroys originals.
376 GB of the current index points at an unmounted drive, and the tool will happily discard
incoming files against it without a word. Until a skip decision is backed by a file that
demonstrably exists, no other improvement is safe to run at scale — and the fix is a
`.exists()` check plus a startup guard on unreachable volumes.

**2. Fix sidecar matching and stop pretending HEIC/video writes succeed (findings #6, #7, #8).**
These three are one problem: metadata is silently discarded while the run reports success.
43.1% of sidecars unmatched, 17,164 HEIC files whose writes are verified no-ops, 4,741 videos
with zero dates recovered. Takeout exposes no hashes and often no EXIF — the sidecar *is* the
data, and it is the one thing that cannot be recovered later by re-reading the file. The
regex fix and the `os.utime` fallback are both small; cutting HEIC/TIFF from `EXIF_EXTENSIONS`
is a one-line change that converts silent failure into an honest, loggable gap.

**3. Skip unchanged files on rescan, and stop auto-resolving every group (findings #12, #2).**
Paired because together they make the tool usable at 1 TB and safe to iterate on. Without the
`size`+`mtime` skip, every fix above costs a full 596 GB re-hash to validate — which in
practice means nobody re-runs it. And `battle.index` has already written 2,903 unreviewed
verdicts into the table a future delete step will read, chosen by a scorer whose resolution
term is dead (160,198 NULL widths) and whose format ranking is inverted for the 12% of
`.heic` files that are really JPEG. Fixing both now is cheap; fixing them after a delete
feature ships is not.

---

## Verification

Nothing here has been changed. To reproduce the measurements (all read-only):

```bash
cd /home/tim/projects/photo-dedup
sqlite3 -readonly memoryvault.db "SELECT source,COUNT(*),SUM(size)/1e9 FROM files GROUP BY source;"
sqlite3 -readonly memoryvault.db "SELECT status,skip_reason,COUNT(*) FROM archive_entries GROUP BY 1,2;"
sqlite3 -readonly memoryvault.db "SELECT COUNT(*) FROM files WHERE width IS NULL;"   # → 160198
mount | grep -i seagate                                                              # → not mounted
./.venv/bin/python -c "import piexif; piexif.load('<a real .heic>')"                 # → InvalidImageDataError
```

The sidecar-matching breakdown and the `.heic`-magic-byte sample were produced by ad-hoc
scripts run against a read-only connection; both are reproducible from the queries above plus
a magic-byte check on `files.path`.

If any of these findings should become work, the natural first branch is #1 + #12 together —
both touch only `ingest.py` and `scanner.py`, and #12 makes #1 cheap to verify against the
full corpus.
