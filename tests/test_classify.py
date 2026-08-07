"""The G1 classifier: a verdict about one file, with the evidence behind it.

Two properties matter more than the individual rules.

The first is that nothing is grouped. `classify_record` receives one record and
has no access to any other, so a wrong grouping is not a mistake it is capable
of making — `DSC_0458` and its three siblings are four different photographs
sharing a filename, and each is judged alone.

The second is that `evidence` records every predicate evaluated, not just the
one that decided. Storing only the winner would make the residual
un-revisitable: 778 vault files land in `no_camera_evidence`, and a future rule
aimed at them should be testable against this column rather than requiring
another pass over 99k files.
"""

import json

import pytest

from memoryvault.classify import (
    CLASSIFIER_VERSION, VERDICTS, classify_all, classify_record, gather_signals,
    write_verdicts,
)


def rec(name, container="jpeg", width=None, height=None, make=None, model=None,
        software=None, exposure=None, gps=False, path=None):
    return {"path": path or f"/v/{name}", "name": name, "container": container,
            "width": width, "height": height, "make": make, "model": model,
            "software": software, "exposure": exposure, "gps": gps}


# The real signal values, taken from the vault.
NIKON_DOWNSCALE = rec("DSC_0458.JPG", width=1600, height=1071,
                      make="NIKON CORPORATION", model="NIKON D60",
                      software="Nikon Transfer 1.0 W", exposure=0.008)
IPHONE_ORIGINAL = rec("IMG_3707.HEIC", container="heif", width=3024, height=4032,
                      make="Apple", model="iPhone 8 Plus", software="12.1",
                      exposure=0.033)
THUMBNAIL = rec("dsc_7347.jpg", width=132, height=200)
INSTAGRAM = rec("IMG_3705.JPG", width=1440, height=1440, software="Instagram")
MESSENGER = rec("2825962908668243376.jpg", width=1536, height=2048)
PICASA_MEME = rec("9130a975-6a55-4920-8eaf-4f48f2f3f2f0.jpg", width=414,
                  height=490, software="Picasa")
SCREENSHOT = rec("IMG_5138.PNG", container="png", width=1179, height=2556)
VIDEO = rec("IMG_1255.MOV", container="mov")


class TestRequiredCases:
    @pytest.mark.parametrize("record,expected", [
        (THUMBNAIL, "derivative"),
        (IPHONE_ORIGINAL, "original"),
        (INSTAGRAM, "derivative"),
        (MESSENGER, "non_photographic"),
        (PICASA_MEME, "non_photographic"),
        (SCREENSHOT, "non_photographic"),
        (NIKON_DOWNSCALE, "derivative"),
    ])
    def test_verdict(self, record, expected):
        assert classify_record(record)[0] == expected

    def test_a_google_downscale_is_not_an_original_despite_camera_tags(self):
        """The biggest error in the first crude split. A downscale keeps Make
        and Model, so camera EXIF cannot mean 'original' on its own."""
        verdict, reason, _, ev = classify_record(NIKON_DOWNSCALE)
        assert ev["tests"]["camera_tags"] is True
        assert verdict == "derivative"
        assert reason == "google_downscale"

    def test_app_tag_alone_does_not_separate_a_photo_from_a_meme(self):
        """Instagram and Picasa both strip camera EXIF and both leave a tag;
        the filename is what distinguishes them."""
        assert classify_record(INSTAGRAM)[1] == "app_rewritten"
        assert classify_record(PICASA_MEME)[1] == "generated_filename"


class TestNothingIsGrouped:
    def test_siblings_are_judged_independently(self):
        """DSC_0458 and its three siblings are four different photographs that
        share a filename. Each is classified alone; there is no group."""
        siblings = [
            rec("DSC_0458.JPG", width=1600, height=1071, make="NIKON CORPORATION"),
            rec("DSC_0458(1).JPG", width=1600, height=1071, make="NIKON CORPORATION"),
            rec("DSC_0458(2).JPG", width=3872, height=2592, make="NIKON CORPORATION",
                exposure=0.01),
            rec("DSC_0458(3).JPG", container="mov"),
        ]
        verdicts = [classify_record(s)[0] for s in siblings]
        assert verdicts == ["derivative", "derivative", "original", "unknown"]

    def test_classify_record_sees_only_one_record(self):
        """Structural: the signature takes a single dict. It cannot consult a
        neighbour even if a future rule wanted to."""
        import inspect
        params = inspect.signature(classify_record).parameters
        assert list(params) == ["rec"]


class TestEvidenceRecordsEverySignal:
    def test_untriggered_predicates_are_still_recorded(self):
        """A screenshot is settled by the first rule, but the evidence must
        still say what every other predicate would have decided."""
        _, _, _, ev = classify_record(SCREENSHOT)
        assert set(ev["tests"]) == {
            "camera_tags", "screen_dimensions", "generated_filename",
            "app_software", "google_long_edge", "tiny", "camera_sized",
            "original_sized", "has_pixels"}
        assert ev["tests"]["screen_dimensions"] is True
        assert ev["tests"]["google_long_edge"] is False      # evaluated, not fired

    def test_residual_carries_enough_to_retry_a_rule(self):
        """The 778-file case: a later rule must be testable from this column
        alone, without re-reading the vault."""
        undecidable = rec("IMG_9819.JPG", width=1440, height=814)
        verdict, reason, _, ev = classify_record(undecidable)

        assert (verdict, reason) == ("unknown", "no_camera_evidence")
        assert ev["width"] == 1440 and ev["height"] == 814
        assert ev["aspect"] == pytest.approx(1.7691, abs=1e-4)
        assert ev["max_dim"] == 1440
        assert ev["container"] == "jpeg"

    def test_signals_are_gathered_the_same_way_the_rules_read_them(self):
        """gather_signals is the single source, so evidence cannot drift from
        the predicates that decided."""
        ev_direct = gather_signals(INSTAGRAM)
        _, _, _, ev_from_rules = classify_record(INSTAGRAM)
        assert ev_direct == ev_from_rules

    def test_missing_dimensions_do_not_fabricate_signals(self):
        _, _, _, ev = classify_record(VIDEO)
        assert ev["px"] is None and ev["aspect"] is None and ev["max_dim"] is None
        assert ev["tests"]["has_pixels"] is False


class TestVerdictDomain:
    def test_every_verdict_is_one_of_the_four(self):
        for r in (NIKON_DOWNSCALE, IPHONE_ORIGINAL, THUMBNAIL, INSTAGRAM,
                  MESSENGER, PICASA_MEME, SCREENSHOT, VIDEO):
            assert classify_record(r)[0] in VERDICTS

    def test_confidence_is_always_stated(self):
        for r in (NIKON_DOWNSCALE, THUMBNAIL, VIDEO):
            assert classify_record(r)[2] in ("high", "medium", "low")


class TestWritePath:
    def _results(self):
        return classify_all([THUMBNAIL, IPHONE_ORIGINAL, SCREENSHOT])

    def test_verdicts_are_written_with_their_evidence(self, db):
        write_verdicts(db, self._results())

        rows = db.conn.execute(
            "SELECT * FROM file_class ORDER BY path").fetchall()
        assert len(rows) == 3
        ev = json.loads(rows[0]["evidence"])
        assert "tests" in ev and ev["tests"]
        assert rows[0]["classifier"] == CLASSIFIER_VERSION

    def test_rerunning_updates_rather_than_duplicating(self, db):
        write_verdicts(db, self._results())
        write_verdicts(db, self._results())

        assert db.conn.execute(
            "SELECT COUNT(*) c FROM file_class").fetchone()["c"] == 3

    def test_a_new_classifier_version_coexists(self, db):
        """So two rule revisions can be compared rather than one silently
        overwriting the other."""
        write_verdicts(db, self._results())
        write_verdicts(db, self._results(), classifier="g2-experiment")

        assert db.conn.execute(
            "SELECT COUNT(*) c FROM file_class").fetchone()["c"] == 6

    def test_file_id_is_linked_when_the_file_is_indexed(self, db, tmp_path):
        from tests.conftest import make_jpeg_bytes
        p = tmp_path / "one.jpg"
        p.write_bytes(make_jpeg_bytes())
        db.upsert_file(path=str(p), size=p.stat().st_size,
                       scan_time="2026-01-01T00:00:00Z")

        write_verdicts(db, classify_all([rec("one.jpg", width=100, height=100,
                                             path=str(p))]))

        row = db.conn.execute("SELECT file_id FROM file_class").fetchone()
        assert row["file_id"] == db.get_file_by_path(str(p))["id"]
