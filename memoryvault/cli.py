"""MemoryVault CLI — scan folders and find duplicates."""

from pathlib import Path

import click

from memoryvault.database import Database
from memoryvault.scanner import scan_folder
from memoryvault.dedup import find_duplicates
from memoryvault.metadata import (
    get_exif_date, get_exif_gps, write_exif_date, write_exif_gps, can_have_exif,
)


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
