"""Photo Battle blueprint — gamified duplicate review."""

from pathlib import Path

from flask import Blueprint, current_app, jsonify, redirect, render_template, request, url_for

from memoryvault.dedup import find_duplicates, score_file
from memoryvault.web import get_db

bp = Blueprint("battle", __name__)


def is_unambiguous(group: list[dict]) -> bool:
    """True when picking a winner cannot lose anything.

    Requires every member to have the same size and the same extension. A
    shared hash alone is not enough: the scorer that picks the winner ranks by
    file extension, and extensions in this corpus are frequently wrong (Google
    hands back transcoded JPEGs still named .HEIC). When members differ, a
    human decides.
    """
    if len(group) < 2:
        return False
    sizes = {f["size"] for f in group}
    extensions = {Path(f["path"]).suffix.lower() for f in group}
    return len(sizes) == 1 and len(extensions) == 1


@bp.route("/")
def index():
    """Auto-resolve unambiguous matches, then redirect to arena or complete."""
    db = get_db()

    unresolved = db.find_unresolved_duplicate_groups()
    auto_resolved = 0
    space_freed = 0

    for group in unresolved:
        if not is_unambiguous(group):
            # Left unresolved on purpose — it goes to the arena for review.
            continue

        scored = [(score_file(f), f) for f in group]
        scored.sort(key=lambda x: x[0], reverse=True)
        winner = scored[0][1]

        db.resolve_group(
            blake3_full=winner["blake3_full"],
            winner_path=winner["path"],
            action="keep_winner",
            confidence=100,
            auto_resolved=True,
        )
        auto_resolved += 1
        space_freed += sum(f["size"] for _, f in scored[1:])

    # Check if there are still unresolved groups (future: fuzzy matches)
    remaining = db.find_unresolved_duplicate_groups()

    if remaining:
        return redirect(url_for("battle.arena"))

    return render_template("battle/complete.html",
                           auto_resolved=auto_resolved,
                           space_freed=space_freed,
                           manually_resolved=0,
                           has_battles=False)


@bp.route("/arena")
def arena():
    """Show the current battle — next unresolved duplicate group."""
    db = get_db()
    unresolved = db.find_unresolved_duplicate_groups()

    if not unresolved:
        return redirect(url_for("battle.complete_view"))

    # Get the first unresolved group
    group = unresolved[0]
    scored = [(score_file(f), f) for f in group]
    scored.sort(key=lambda x: x[0], reverse=True)

    # Build tournament: start with top two
    fighters = [{"file": f, "score": s} for s, f in scored]

    # Resolution stats
    res_stats = db.get_resolution_stats()

    return render_template("battle/arena.html",
                           left=fighters[0],
                           right=fighters[1] if len(fighters) > 1 else None,
                           remaining_challengers=fighters[2:],
                           group_hash=group[0]["blake3_full"],
                           groups_remaining=len(unresolved),
                           total_resolved=res_stats["total"],
                           confidence=100)


@bp.route("/resolve", methods=["POST"])
def resolve():
    """Handle a battle resolution."""
    db = get_db()
    action = request.form.get("action")  # keep_left, keep_right, keep_both, skip
    group_hash = request.form.get("group_hash")
    winner_path = request.form.get("winner_path")

    if not group_hash:
        return redirect(url_for("battle.arena"))

    if action == "keep_both":
        db.resolve_group(group_hash, winner_path or "", "keep_both", confidence=100)
    elif action == "skip":
        db.resolve_group(group_hash, "", "skip", confidence=100)
    elif action in ("keep_left", "keep_right"):
        db.resolve_group(group_hash, winner_path, "keep_winner", confidence=100)

    return redirect(url_for("battle.arena"))


@bp.route("/complete")
def complete_view():
    """Victory screen."""
    db = get_db()
    res_stats = db.get_resolution_stats()
    return render_template("battle/complete.html",
                           auto_resolved=res_stats["auto_resolved"],
                           manually_resolved=res_stats["total"] - res_stats["auto_resolved"],
                           space_freed=0,
                           has_battles=True)
