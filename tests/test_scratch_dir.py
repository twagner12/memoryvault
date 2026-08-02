"""Large entries spill next to the vault, not into RAM.

`archive.py` writes every entry over 50 MB to `tempfile`, in five places. On
this machine — and most modern Linux desktops — the system temp directory is
tmpfs, 7.7 GB of RAM, so an 11 GB Takeout zip full of video can exhaust it
mid-run. The default therefore has to be somewhere real.

A sibling of the destination rather than a child, for a second reason:
`scan_folder` walks the vault with no ignore list, so scratch files left
behind by a crashed run would be indexed as media.
"""

import os
import tempfile
from pathlib import Path

from memoryvault.ingest import _scratch, ingest_archive, scratch_path
from tests.conftest import make_jpeg_bytes


class TestScratchPath:
    def test_it_defaults_beside_the_destination(self):
        assert scratch_path(Path("/data/vault")) == Path("/data/vault.mvtmp")

    def test_it_is_not_inside_the_destination(self):
        """Otherwise a rescan of the vault would index whatever is left."""
        vault = Path("/data/vault")
        assert vault not in scratch_path(vault).parents

    def test_an_explicit_tmpdir_wins(self, tmp_path):
        assert scratch_path(Path("/data/vault"), tmp_path) == tmp_path.resolve()


class TestScratchContext:
    def test_tempfile_writes_there(self, tmp_path):
        scratch = tmp_path / "scratch"

        with _scratch(scratch):
            with tempfile.NamedTemporaryFile(delete=False) as handle:
                spilled = Path(handle.name)
            assert spilled.parent == scratch
            spilled.unlink()

    def test_subprocesses_see_it_too(self, tmp_path):
        """`_stream_7z` shells out, so TMPDIR has to move with it."""
        scratch = tmp_path / "scratch"

        with _scratch(scratch):
            assert os.environ["TMPDIR"] == str(scratch)

    def test_the_setting_does_not_leak(self, tmp_path):
        before_tempdir = tempfile.tempdir
        before_env = os.environ.get("TMPDIR")

        with _scratch(tmp_path / "scratch"):
            pass

        assert tempfile.tempdir == before_tempdir
        assert os.environ.get("TMPDIR") == before_env

    def test_it_is_restored_even_when_the_run_raises(self, tmp_path):
        before = tempfile.tempdir

        try:
            with _scratch(tmp_path / "scratch"):
                raise RuntimeError("archive exploded")
        except RuntimeError:
            pass

        assert tempfile.tempdir == before

    def test_an_empty_scratch_dir_is_removed(self, tmp_path):
        scratch = tmp_path / "scratch"

        with _scratch(scratch):
            assert scratch.is_dir()

        assert not scratch.exists()

    def test_a_scratch_dir_holding_a_spill_is_kept(self, tmp_path):
        """Whatever is left is the only evidence of a failed extraction."""
        scratch = tmp_path / "scratch"

        with _scratch(scratch):
            (scratch / "half-written.mp4").write_bytes(b"partial")

        assert (scratch / "half-written.mp4").exists()


class TestIngestUsesIt:
    def test_the_default_dir_appears_and_is_cleaned_up(self, tmp_path, db,
                                                       make_zip):
        vault = tmp_path / "vault"
        ingest_archive(make_zip({"T/P/a.jpg": make_jpeg_bytes()}), vault, db,
                       allow_unreachable_volumes=True)

        assert (vault / "a.jpg").exists()
        assert not (tmp_path / "vault.mvtmp").exists()

    def test_an_explicit_tmpdir_is_honoured(self, tmp_path, db, make_zip):
        chosen = tmp_path / "elsewhere"
        vault = tmp_path / "vault"

        ingest_archive(make_zip({"T/P/a.jpg": make_jpeg_bytes()}), vault, db,
                       allow_unreachable_volumes=True, tmpdir=chosen)

        assert (vault / "a.jpg").exists()
        assert not (tmp_path / "vault.mvtmp").exists()
