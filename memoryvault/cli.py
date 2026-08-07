"""MemoryVault CLI — scan folders and find duplicates."""

from datetime import datetime
from pathlib import Path

import click

from memoryvault.database import Database, mark_auto_resolutions_stale
from memoryvault.scanner import scan_folder
from memoryvault.dedup import find_duplicates
from memoryvault.metadata import get_exif_date, get_exif_gps, can_have_exif
from memoryvault.ingest import (
    ingest_archive, merge_metadata_between_files, scratch_path,
)
from memoryvault.rebind import rebind_sidecars
from memoryvault.repair import repair_mtimes
from memoryvault.volumes import UnreachableVolumeError, find_unreachable_volumes


@click.group()
@click.option("--db", default="memoryvault.db", help="Path to the database file.")
@click.pass_context
def cli(ctx, db):
    """MemoryVault — Get all your files into one deduplicated collection."""
    ctx.ensure_object(dict)
    ctx.obj["db_path"] = Path(db)


@cli.command()
@click.argument("folder", type=click.Path(exists=True, file_okay=False, resolve_path=True))
@click.option("--source", default="local", help="Label for where these files came from.")
@click.pass_context
def scan(ctx, folder, source):
    """Scan a folder and index all files into the database."""
    db = Database(ctx.obj["db_path"])

    def progress(stage, total, current, error=None):
        if stage == "start":
            click.echo(f"Scanning {total:,} files in {folder}")
        elif stage == "progress":
            click.echo(f"  {current:,} / {total:,} files scanned", nl=False)
            click.echo("\r", nl=False)
        elif stage == "error":
            click.echo(f"  Error: {error}", err=True)
        elif stage == "done":
            click.echo(f"\nDone. {current:,} files scanned and indexed.")

    try:
        count = scan_folder(Path(folder), db, source=source, progress_callback=progress)
        click.echo(f"Total files in database: {db.file_count():,}")
    finally:
        db.close()


@cli.command()
@click.option("--limit", default=0, help="Max number of groups to show (0 = all).")
@click.pass_context
def dupes(ctx, limit):
    """Find and display duplicate file groups."""
    db = Database(ctx.obj["db_path"], read_only=True)
    try:
        results = find_duplicates(db)
        if not results:
            click.echo("No duplicates found.")
            return

        total_dupes = sum(len(r["losers"]) for r in results)
        total_wasted = sum(
            sum(f["size"] for f in r["losers"]) for r in results
        )
        click.echo(f"Found {len(results):,} duplicate groups ({total_dupes:,} extra files, "
                    f"{total_wasted / (1024**3):.1f} GB wasted)\n")

        shown = results[:limit] if limit else results
        for i, group in enumerate(shown, 1):
            winner = group["winner"]
            click.echo(f"Group {i} (hash: {group['blake3'][:12]}...)")
            click.echo(f"  KEEP:  {winner['path']}")
            click.echo(f"         score={group['winner_score']}  size={winner['size']:,}")
            for loser in group["losers"]:
                click.echo(f"  DUPE:  {loser['path']}")
                click.echo(f"         size={loser['size']:,}")
            click.echo()
    finally:
        db.close()


@cli.command()
@click.option("--dry-run", is_flag=True, help="Show what would be merged without writing.")
@click.pass_context
def merge(ctx, dry_run):
    """Merge date/GPS metadata from duplicate losers into winners."""
    db = Database(ctx.obj["db_path"])
    try:
        results = find_duplicates(db)
        if not results:
            click.echo("No duplicates found.")
            return

        merged_count = 0
        failed_count = 0
        for group in results:
            winner_path = Path(group["winner"]["path"])
            if not winner_path.exists() or not can_have_exif(winner_path):
                continue

            for loser in group["losers"]:
                loser_path = Path(loser["path"])
                if not loser_path.exists() or not can_have_exif(loser_path):
                    continue

                if get_exif_date(winner_path) and get_exif_gps(winner_path):
                    break  # nothing left for this winner to gain

                if dry_run:
                    for field, value in (("date", get_exif_date(loser_path)),
                                         ("gps", get_exif_gps(loser_path))):
                        if value:
                            click.echo(f"  Would merge {field} {value} from "
                                       f"{loser_path} → {winner_path}")
                    continue

                # Routed through the ingest choke point so a failed write is
                # recorded in metadata_pending instead of vanishing.
                outcome = merge_metadata_between_files(winner_path, loser_path, db)
                for field in outcome.merged:
                    click.echo(f"  Merged {field} → {winner_path.name}")
                for field in outcome.failed:
                    click.echo(f"  FAILED {field} → {winner_path.name} "
                               f"(recorded as pending)", err=True)
                merged_count += len(outcome.merged)
                failed_count += len(outcome.failed)

        prefix = "Would merge" if dry_run else "Merged"
        click.echo(f"\n{prefix} {merged_count} metadata fields across {len(results)} duplicate groups.")
        if failed_count:
            click.echo(f"{failed_count} write(s) failed and were recorded — "
                       f"see `memoryvault pending`.")
    finally:
        db.close()


@cli.command()
@click.argument("archive", type=click.Path(exists=True, dir_okay=False, resolve_path=True))
@click.option("--dest", required=True, type=click.Path(file_okay=False, resolve_path=True),
              help="Destination folder for unique files.")
@click.option("--allow-unreachable-volumes", is_flag=True,
              help="Ingest even though indexed files live on a detached volume. "
                   "Duplicates will be judged against rows that cannot be read.")
@click.option("--tmpdir", type=click.Path(file_okay=False, resolve_path=True),
              help="Where to spill entries larger than 50 MB. Defaults to "
                   "<dest>.mvtmp, a sibling of the destination — same "
                   "filesystem, and not inside the tree `scan` walks. The "
                   "system temp dir is deliberately not used: it is tmpfs "
                   "(RAM) on most Linux desktops, and a large Takeout zip "
                   "will exhaust it. TMPDIR in the environment is ignored.")
@click.pass_context
def ingest(ctx, archive, dest, allow_unreachable_volumes, tmpdir):
    """Ingest a Takeout zip: extract, dedup, and keep unique files."""
    db = Database(ctx.obj["db_path"])

    def progress(stage, **kwargs):
        if stage == "already_done":
            click.echo(f"Archive already processed: {kwargs['archive']}")
        elif stage == "progress":
            processed = kwargs["processed"]
            total = kwargs["total"]
            action = kwargs["action"]
            path = Path(kwargs["path"]).name
            symbol = "+" if action == "kept" else "~" if action == "skipped" else "!"
            click.echo(f"  [{processed}/{total}] {symbol} {path}")
        elif stage == "done":
            s = kwargs["stats"]
            click.echo(f"\nDone. Kept: {s['kept']}, Skipped: {s['skipped']}, "
                        f"Errors: {s['errors']}, Metadata merged: {s['merged_metadata']}")
            click.echo(f"      Deferred: {s['metadata_deferred']}, "
                       f"Failed: {s['metadata_failed']}, "
                       f"Already present: {s['metadata_already_present']}")

    scratch = scratch_path(Path(dest), Path(tmpdir) if tmpdir else None)
    try:
        click.echo(f"Ingesting {archive}")
        click.echo(f"Destination: {dest}")
        click.echo(f"Scratch:     {scratch}")
        stats = ingest_archive(
            Path(archive), Path(dest), db, progress_callback=progress,
            allow_unreachable_volumes=allow_unreachable_volumes,
            tmpdir=Path(tmpdir) if tmpdir else None,
        )
        click.echo(f"\nTotal files in database: {db.file_count():,}")
    except UnreachableVolumeError as e:
        raise click.ClickException(str(e))
    finally:
        db.close()


@cli.command()
@click.pass_context
def stats(ctx):
    """Show database statistics."""
    db = Database(ctx.obj["db_path"], read_only=True)
    try:
        total = db.file_count()
        dupes = db.find_duplicate_groups()
        dupe_files = sum(len(g) - 1 for g in dupes)
        wasted = sum(sum(f["size"] for f in g[1:]) for g in dupes) if dupes else 0

        click.echo(f"Files indexed:     {total:,}")
        click.echo(f"Duplicate groups:  {len(dupes):,}")
        click.echo(f"Extra copies:      {dupe_files:,}")
        click.echo(f"Wasted space:      {wasted / (1024**3):.1f} GB")

        pending = db.get_pending_summary()
        unmatched = db.get_unmatched_summary()
        outstanding = sum(p["count"] for p in pending)
        unbound = sum(u["count"] for u in unmatched)

        click.echo(f"Metadata pending:  {outstanding:,}"
                   + ("   (memoryvault pending)" if outstanding else ""))
        click.echo(f"Sidecars unmatched:{unbound:,}"
                   + ("   (memoryvault unmatched | rebind --dry-run)"
                      if unbound else ""))
    finally:
        db.close()


@cli.command()
@click.option("--limit", default=20, help="Max rows to list (0 = summary only).")
@click.pass_context
def pending(ctx, limit):
    """List metadata that could not be embedded, and why.

    These are the outcomes that used to disappear: a date destined for a HEIC
    or a video, or a write that failed. Each row carries enough to be applied
    later without the original sidecar.
    """
    db = Database(ctx.obj["db_path"], read_only=True)
    try:
        summary = db.get_pending_summary()
        if not summary:
            click.echo("No outstanding metadata.")
            return

        total = sum(row["count"] for row in summary)
        click.echo(f"{total:,} outstanding metadata value(s):\n")
        for row in summary:
            click.echo(f"  {row['count']:>7,}  {row['state']:<9} "
                       f"{row['field']:<5} {row['reason']}")

        if limit:
            rows = db.get_pending()[:limit]
            click.echo(f"\nFirst {len(rows)} of {total:,}:")
            for row in rows:
                click.echo(f"  {row['state']:<9} {row['field']:<5} "
                           f"{Path(row['file_path']).name}")
                click.echo(f"      reason: {row['reason']}")
                click.echo(f"      value:  {row['value']}")
    finally:
        db.close()


@cli.command()
@click.option("--limit", default=20, help="Max rows to list (0 = summary only).")
@click.pass_context
def unmatched(ctx, limit):
    """List sidecars whose media file could not be identified.

    Takeout exposes no hashes and often strips EXIF, so the sidecar is
    frequently the only record of when and where a photo was taken. These
    rows keep the parsed payload, so a rebind pass needs neither the zip nor
    the JSON file — both of which are gone after ingest.
    """
    db = Database(ctx.obj["db_path"], read_only=True)
    try:
        summary = db.get_unmatched_summary()
        if not summary:
            click.echo("No unmatched sidecars.")
            return

        total = sum(row["count"] for row in summary)
        click.echo(f"{total:,} unmatched sidecar(s):\n")
        for row in summary:
            click.echo(f"  {row['count']:>7,}  {row['reason']}")

        if limit:
            rows = db.get_unmatched()[:limit]
            click.echo(f"\nFirst {len(rows)} of {total:,}:")
            for row in rows:
                click.echo(f"  {row['reason']:<16} {row['entry_name']}")
                click.echo(f"      sought: {row['media_stem']}"
                           + (f"  counter={row['counter']}" if row["counter"] else "")
                           + f"  in {row['archive_dir']}")
    finally:
        db.close()


@cli.command()
@click.option("--dry-run", is_flag=True,
              help="Report what would bind without applying or recording anything.")
@click.pass_context
def rebind(ctx, dry_run):
    """Re-offer unmatched sidecars to the files now in the vault.

    Google splits an album directory across zip parts, so a sidecar in part 9
    often describes a photo kept from part 8. Ingest sees one part at a time
    and cannot bind across that boundary; this pass runs once every part is in.

    Binding obeys every rule the ingest matcher does — same directory only,
    counter arithmetic, and a refusal whenever more than one file could match.
    """
    # A dry run opens read-only, so it cannot write even by accident — not
    # even the schema migration that opening normally applies.
    db = Database(ctx.obj["db_path"], read_only=dry_run)
    try:
        stats = rebind_sidecars(db, dry_run=dry_run)

        verb = "Would bind" if dry_run else "Bound"
        click.echo(f"{verb} {stats['bound']:,} sidecar(s).")

        if stats["by_archive"]:
            click.echo("\nBy the archive the sidecar came from:")
            for archive_id, count in sorted(stats["by_archive"].items()):
                archive = db.conn.execute(
                    "SELECT path FROM archives WHERE id = ?",
                    (archive_id,)).fetchone()
                name = Path(archive["path"]).name if archive else f"id={archive_id}"
                click.echo(f"  {count:>7,}  {name}")

        if stats["duplicate_rows"]:
            tail = ("would be marked without re-applying" if dry_run
                    else "were marked without re-applying")
            click.echo(f"\n{stats['duplicate_rows']:,} further row(s) carried "
                       f"an identical offer and {tail}.")

        if stats["refused"]:
            total = sum(stats["refused"].values())
            click.echo(f"\n{total:,} sidecar(s) still unbound:")
            for reason, count in sorted(stats["refused"].items(),
                                        key=lambda kv: -kv[1]):
                click.echo(f"  {count:>7,}  {reason}")
            click.echo("These rows are left in place for a later run.")

        if dry_run:
            click.echo("\nDry run — nothing was written.")
    finally:
        db.close()


@cli.command("repair-mtime")
@click.option("--apply", "apply_changes", is_flag=True,
              help="Actually write. Without this the command only reports: the "
                   "default is a dry run, so the direction that modifies "
                   "62,409 files is the one that needs the flag.")
@click.option("--limit", default=0,
              help="Repair at most this many files (0 = all).")
@click.option("--no-verify-hash", is_flag=True,
              help="Skip the content check entirely. Only honoured on a dry "
                   "run; --apply always guards.")
@click.option("--full-hash", is_flag=True,
              help="Guard by re-hashing every byte instead of size plus 64 KB "
                   "head and 4 KB tail. ~200 GiB rather than ~3.5 GiB across "
                   "the vault, over an enclosure that has already faulted "
                   "twice. Only worth it against mid-file corruption that "
                   "leaves size intact.")
@click.option("--log", "log_path", default=None,
              help="Append every change to this file: path, old mtime, new "
                   "mtime, and where the value came from.")
@click.option("--sample", default=20, help="Before/after rows to print.")
@click.pass_context
def repair_mtime(ctx, apply_changes, limit, no_verify_hash, full_hash,
                 log_path, sample):
    """Set mtimes that ingest stamped with its own run time.

    Ingest rewrote files in place without restoring mtime, so files it touched
    claim they were modified during the run while their own EXIF says
    otherwise. This sets mtime to the instant the file's own metadata
    describes. Content is never altered, so every hash stays valid.

    The value comes from the file's own EXIF wherever that resolves to an
    absolute instant. Google's recorded timestamp is used only where it
    corroborates the file rather than contradicting it — a file that arrived
    with its own date got no write precisely because it knew better, and
    Google's photoTakenTime is not reliable enough to overwrite that.

    Dry run by default. Pass --apply to write.
    """
    dry_run = not apply_changes
    # The content guard is never waived on a real run, whatever was asked for.
    verify_hash = True if apply_changes else not no_verify_hash

    db = Database(ctx.obj["db_path"], read_only=dry_run)
    try:
        def progress(done, total):
            click.echo(f"  {done:,} processed", nl=False)
            click.echo("\r", nl=False)

        stats = repair_mtimes(db, dry_run=dry_run, limit=limit,
                              verify_hash=verify_hash, full_hash=full_hash,
                              log_path=log_path, progress=progress)

        verb = "Would set" if dry_run else "Set"
        n = stats["would_repair"] if dry_run else stats["repaired"]
        click.echo(f"\nCandidates in the ingest window: {stats['candidates']:,}")
        click.echo(f"{verb} mtime on {n:,} file(s).")

        if stats["by_source"]:
            click.echo("\nWhere the value came from:")
            for k, v in sorted(stats["by_source"].items(), key=lambda kv: -kv[1]):
                click.echo(f"  {v:>8,}  {k}")

        if stats["by_skip"]:
            click.echo(f"\nSkipped {stats['skipped']:,}:")
            for k, v in sorted(stats["by_skip"].items(), key=lambda kv: -kv[1]):
                click.echo(f"  {v:>8,}  {k}")

        if sample and stats["changes"]:
            click.echo(f"\nFirst {min(sample, len(stats['changes']))} "
                       f"before/after:")
            for p, old, new, src, _ in stats["changes"][:sample]:
                click.echo(f"  {Path(p).name[:38]:<38} "
                           f"{datetime.fromtimestamp(old):%Y-%m-%d %H:%M} -> "
                           f"{datetime.fromtimestamp(new):%Y-%m-%d %H:%M}  [{src}]")

        interesting = [s for s in stats["skips"]
                       if s[1] in ("hash_mismatch", "google_contradicts_exif",
                                   "file_missing")]
        if interesting:
            click.echo(f"\nSkips worth reading ({len(interesting)}):")
            for p, why, detail in interesting[:20]:
                click.echo(f"  {why:<26} {Path(p).name[:34]}")
                if detail:
                    click.echo(f"      {detail}")

        if dry_run:
            click.echo("\nDry run — nothing was written. Pass --apply to write.")
    finally:
        db.close()


@cli.command()
@click.option("--yes", is_flag=True, help="Apply without confirmation.")
@click.pass_context
def migrate(ctx, yes):
    """Apply schema migrations and retire unreviewed auto-resolutions.

    Opening the database applies any missing columns. This command then marks
    every verdict written by the old blanket auto-resolve as stale, so no
    future apply step can mistake it for a reviewed decision.
    """
    db = Database(ctx.obj["db_path"])
    try:
        pending = db.conn.execute(
            "SELECT COUNT(*) c FROM resolutions "
            "WHERE auto_resolved = 1 AND COALESCE(stale, 0) = 0"
        ).fetchone()["c"]

        click.echo(f"Schema is up to date ({ctx.obj['db_path']}).")
        if pending == 0:
            click.echo("No unreviewed auto-resolutions to retire.")
            return

        click.echo(f"{pending:,} auto-resolved verdict(s) will be marked stale.")
        if not yes:
            click.confirm("Apply?", abort=True)

        marked = mark_auto_resolutions_stale(db)
        click.echo(f"Marked {marked:,} resolution(s) stale.")
        click.echo("Those groups are now unresolved again and will reappear for review.")
    finally:
        db.close()


@cli.command()
@click.pass_context
def volumes(ctx):
    """Report indexed volumes that are not currently attached."""
    db = Database(ctx.obj["db_path"], read_only=True)
    try:
        unreachable = find_unreachable_volumes(db)
        if not unreachable:
            click.echo("All indexed volumes are reachable.")
            return

        total = sum(v["row_count"] for v in unreachable)
        click.echo(f"{len(unreachable)} unreachable volume(s), {total:,} indexed file(s):\n")
        for v in unreachable:
            click.echo(f"  {v['volume']}")
            click.echo(f"      rows:    {v['row_count']:,}")
            click.echo(f"      example: {v['sample_path']}")
        click.echo("\nIngest will refuse to run until these are attached.")
    finally:
        db.close()


@cli.command()
@click.option("--port", default=5000, help="Port to serve on.")
@click.pass_context
def serve(ctx, port):
    """Launch the MemoryVault web UI."""
    import webbrowser
    from memoryvault.web import create_app

    db_path = ctx.obj["db_path"]
    app = create_app(db_path=str(db_path))

    # Add basename filter for templates
    @app.template_filter("basename")
    def basename_filter(path):
        return Path(path).name

    click.echo(f"Starting MemoryVault at http://localhost:{port}")
    webbrowser.open(f"http://localhost:{port}")
    app.run(host="127.0.0.1", port=port, debug=False, threaded=True)
