"""MemoryVault CLI — scan folders and find duplicates."""

from pathlib import Path

import click

from memoryvault.database import Database, mark_auto_resolutions_stale
from memoryvault.scanner import scan_folder
from memoryvault.dedup import find_duplicates
from memoryvault.metadata import (
    get_exif_date, get_exif_gps, write_exif_date, write_exif_gps, can_have_exif,
)
from memoryvault.ingest import ingest_archive
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
    db = Database(ctx.obj["db_path"])
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
        for group in results:
            winner_path = Path(group["winner"]["path"])
            if not can_have_exif(winner_path) or not winner_path.exists():
                continue

            winner_date = get_exif_date(winner_path)
            winner_gps = get_exif_gps(winner_path)

            for loser in group["losers"]:
                loser_path = Path(loser["path"])
                if not can_have_exif(loser_path) or not loser_path.exists():
                    continue

                # Try to get date from loser if winner doesn't have it
                if not winner_date:
                    loser_date = get_exif_date(loser_path)
                    if loser_date:
                        if dry_run:
                            click.echo(f"  Would merge date {loser_date} from {loser_path} → {winner_path}")
                        else:
                            write_exif_date(winner_path, loser_date)
                            db.log_metadata_merge(str(winner_path), str(loser_path), "date", loser_date)
                            click.echo(f"  Merged date {loser_date} → {winner_path.name}")
                        winner_date = loser_date
                        merged_count += 1

                # Try to get GPS from loser if winner doesn't have it
                if not winner_gps:
                    loser_gps = get_exif_gps(loser_path)
                    if loser_gps:
                        if dry_run:
                            click.echo(f"  Would merge GPS {loser_gps} from {loser_path} → {winner_path}")
                        else:
                            write_exif_gps(winner_path, loser_gps[0], loser_gps[1])
                            db.log_metadata_merge(str(winner_path), str(loser_path), "gps",
                                                  f"{loser_gps[0]},{loser_gps[1]}")
                            click.echo(f"  Merged GPS {loser_gps} → {winner_path.name}")
                        winner_gps = loser_gps
                        merged_count += 1

        prefix = "Would merge" if dry_run else "Merged"
        click.echo(f"\n{prefix} {merged_count} metadata fields across {len(results)} duplicate groups.")
    finally:
        db.close()


@cli.command()
@click.argument("archive", type=click.Path(exists=True, dir_okay=False, resolve_path=True))
@click.option("--dest", required=True, type=click.Path(file_okay=False, resolve_path=True),
              help="Destination folder for unique files.")
@click.option("--allow-unreachable-volumes", is_flag=True,
              help="Ingest even though indexed files live on a detached volume. "
                   "Duplicates will be judged against rows that cannot be read.")
@click.pass_context
def ingest(ctx, archive, dest, allow_unreachable_volumes):
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

    try:
        click.echo(f"Ingesting {archive}")
        click.echo(f"Destination: {dest}")
        stats = ingest_archive(
            Path(archive), Path(dest), db, progress_callback=progress,
            allow_unreachable_volumes=allow_unreachable_volumes,
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
    db = Database(ctx.obj["db_path"])
    try:
        total = db.file_count()
        dupes = db.find_duplicate_groups()
        dupe_files = sum(len(g) - 1 for g in dupes)
        wasted = sum(sum(f["size"] for f in g[1:]) for g in dupes) if dupes else 0

        click.echo(f"Files indexed:     {total:,}")
        click.echo(f"Duplicate groups:  {len(dupes):,}")
        click.echo(f"Extra copies:      {dupe_files:,}")
        click.echo(f"Wasted space:      {wasted / (1024**3):.1f} GB")
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
    db = Database(ctx.obj["db_path"])
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
