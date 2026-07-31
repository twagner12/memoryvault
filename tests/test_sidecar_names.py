"""Sidecar name parsing and media resolution (#6).

43.1% of the 43,701 ingested sidecars never bound to their media file, because
`_media_name_for_sidecar` handled exactly two patterns. Google does two things
it did not expect:

- the `(n)` disambiguation counter goes *inside* the sidecar name
  (`DSC_0109.JPG.supplemental-metadata(9).json` belongs to `DSC_0109(9).JPG`)
- the `supplemental-metadata` suffix is truncated to fit a filename length
  cap, to `.supplemental-metada`, `.supplemental-meta`, or even `.suppl`

Every name shape below was taken from the live database. Pure unit tests: no
I/O, candidate directories are passed in as lists.
"""

import pytest

from memoryvault.sidecar_names import (
    UnmatchedReason, is_sidecar_name, resolve_media_name, split_sidecar_name,
)


class TestSplitFullSuffix:
    def test_plain(self):
        parsed = split_sidecar_name("IMG_6278.HEIC.supplemental-metadata.json")
        assert parsed.media_name == "IMG_6278.HEIC"
        assert parsed.counter is None
        assert parsed.truncated is False

    def test_stem_containing_spaces(self):
        parsed = split_sidecar_name("Charlie 502.JPG.supplemental-metadata.json")
        assert parsed.media_name == "Charlie 502.JPG"

    def test_stem_containing_dots(self):
        parsed = split_sidecar_name("my.photo.v2.jpg.supplemental-metadata.json")
        assert parsed.media_name == "my.photo.v2.jpg"


class TestSplitTruncatedSuffix:
    @pytest.mark.parametrize("suffix", [
        ".supplemental-metadat",
        ".supplemental-metada",
        ".supplemental-metad",
        ".supplemental-meta",
        ".supplemental-met",
        ".supplement",
        ".suppl",
        ".s",
    ])
    def test_every_truncation_length(self, suffix):
        """Google cuts the suffix wherever the length cap lands."""
        parsed = split_sidecar_name(f"photo.jpg{suffix}.json")
        assert parsed.media_name == "photo.jpg"
        assert parsed.truncated is True

    def test_real_truncated_names_from_the_corpus(self):
        cases = {
            "DSC_1813_Original Copy.JPG.supplemental-metada.json":
                "DSC_1813_Original Copy.JPG",
            "20240304_194322_IMG_1187.PNG.supplemental-meta.json":
                "20240304_194322_IMG_1187.PNG",
            "RPReplay_Final1678967907.mp4.supplemental-meta.json":
                "RPReplay_Final1678967907.mp4",
            "1b6f76be-6428-419b-b51b-f1bff5f3ddbc.jpg.suppl.json":
                "1b6f76be-6428-419b-b51b-f1bff5f3ddbc.jpg",
        }
        for name, expected in cases.items():
            assert split_sidecar_name(name).media_name == expected, name


class TestCounter:
    def test_counter_reattached_before_the_extension(self):
        """The counter belongs to the media name, not the sidecar suffix."""
        parsed = split_sidecar_name("DSC_0109.JPG.supplemental-metadata(9).json")
        assert parsed.counter == "(9)"
        assert parsed.media_name == "DSC_0109(9).JPG"

    def test_multi_digit_counter(self):
        parsed = split_sidecar_name("DSC_0103.JPG.supplemental-metadata(18).json")
        assert parsed.counter == "(18)"
        assert parsed.media_name == "DSC_0103(18).JPG"

    def test_counter_with_truncated_suffix(self):
        parsed = split_sidecar_name("photo.jpg.supplemental-meta(3).json")
        assert parsed.counter == "(3)"
        assert parsed.media_name == "photo(3).jpg"

    def test_appended_counter_form_is_offered_as_an_alternative(self):
        """Some exports put the counter after the extension instead."""
        parsed = split_sidecar_name("DSC_0109.JPG.supplemental-metadata(9).json")
        assert "DSC_0109.JPG(9)" in parsed.alternatives

    def test_counter_on_a_stem_with_no_extension(self):
        parsed = split_sidecar_name("lectern.supplemental-metadata(2).json")
        assert parsed.media_name == "lectern(2)"


class TestDoubleDot:
    def test_double_dot_variant(self):
        """`IMG_….jpeg..json` — an empty suffix between the dots."""
        parsed = split_sidecar_name(
            "IMG_8B3A0A6A-EB94-4ED7-B55A-162A326EDD01.jpeg..json")
        assert parsed.media_name == "IMG_8B3A0A6A-EB94-4ED7-B55A-162A326EDD01.jpeg"

    def test_legacy_plain_json(self):
        """The old `photo.jpg.json` form still has to work."""
        parsed = split_sidecar_name("photo.jpg.json")
        assert parsed.media_name == "photo.jpg"


class TestUnparseable:
    def test_not_json_returns_none(self):
        parsed = split_sidecar_name("photo.jpg")
        assert parsed.media_name is None
        assert parsed.reason is UnmatchedReason.UNPARSEABLE

    def test_bare_json_returns_none(self):
        parsed = split_sidecar_name(".json")
        assert parsed.media_name is None
        assert parsed.reason is UnmatchedReason.UNPARSEABLE

    def test_album_metadata_is_not_a_sidecar(self):
        assert not is_sidecar_name("metadata.json")

    def test_real_sidecar_is_recognised(self):
        assert is_sidecar_name("IMG_6278.HEIC.supplemental-metadata.json")


class TestResolveExact:
    def test_exact_match_wins(self):
        result = resolve_media_name(
            "IMG_6278.HEIC.supplemental-metadata.json",
            candidates=["IMG_6278.HEIC", "IMG_6279.HEIC"])
        assert result.media_name == "IMG_6278.HEIC"

    def test_case_insensitive_match(self):
        result = resolve_media_name(
            "img_6278.heic.supplemental-metadata.json",
            candidates=["IMG_6278.HEIC"])
        assert result.media_name == "IMG_6278.HEIC"

    def test_counter_form_resolves_against_real_media(self):
        result = resolve_media_name(
            "DSC_0109.JPG.supplemental-metadata(9).json",
            candidates=["DSC_0109.JPG", "DSC_0109(9).JPG"])
        assert result.media_name == "DSC_0109(9).JPG"

    def test_alternative_counter_form_used_when_primary_absent(self):
        result = resolve_media_name(
            "DSC_0109.JPG.supplemental-metadata(9).json",
            candidates=["DSC_0109.JPG(9)"])
        assert result.media_name == "DSC_0109.JPG(9)"


class TestResolveFuzzy:
    def test_truncated_stem_binds_on_a_single_candidate(self):
        """The stem itself was cut, so only a prefix match can find it."""
        result = resolve_media_name(
            "64122699523__7F4958E8-FB3A-4208-8990-7C62C7623.json",
            candidates=["64122699523__7F4958E8-FB3A-4208-8990-7C62C7623FBA.JPG"])
        assert result.media_name == \
            "64122699523__7F4958E8-FB3A-4208-8990-7C62C7623FBA.JPG"
        assert result.fuzzy is True

    def test_fuzzy_refuses_on_two_candidates(self):
        """Ambiguity must be recorded, never guessed."""
        result = resolve_media_name(
            "64122699523__7F4958E8-FB3A-4208-8990-7C62C7623.json",
            candidates=[
                "64122699523__7F4958E8-FB3A-4208-8990-7C62C7623FBA.JPG",
                "64122699523__7F4958E8-FB3A-4208-8990-7C62C7623CDE.JPG",
            ])
        assert result.media_name is None
        assert result.reason is UnmatchedReason.AMBIGUOUS
        assert result.candidate_count == 2

    def test_exact_stem_beats_a_longer_prefix(self):
        """The Live Photo case, and the single biggest real-corpus gap.

        A truncated sidecar stem prefix-matches both halves of an iPhone Live
        Photo, because Google's cap cut the still's name at exactly the point
        where the video's name continues:

            …0618CB116.json   ← sidecar
            …0618CB116.HEIC   ← stem matches exactly
            …0618CB1166.MP4   ← one more character, then the extension

        Treating that as ambiguous stranded 14,000+ sidecars. A candidate
        whose stem *equals* the parsed name is strictly better evidence than
        one that merely starts with it.
        """
        result = resolve_media_name(
            "67521465421__16DE9C76-9A6F-45FE-A84C-0618CB116.json",
            candidates=[
                "67521465421__16DE9C76-9A6F-45FE-A84C-0618CB116.HEIC",
                "67521465421__16DE9C76-9A6F-45FE-A84C-0618CB1166.MP4",
            ])

        assert result.media_name == \
            "67521465421__16DE9C76-9A6F-45FE-A84C-0618CB116.HEIC"
        assert result.fuzzy is True

    def test_two_exact_stems_are_still_ambiguous(self):
        """Same stem, two extensions — nothing distinguishes them."""
        result = resolve_media_name(
            "67521465421__16DE9C76-9A6F-45FE-A84C-0618CB116.json",
            candidates=[
                "67521465421__16DE9C76-9A6F-45FE-A84C-0618CB116.HEIC",
                "67521465421__16DE9C76-9A6F-45FE-A84C-0618CB116.MP4",
            ])

        assert result.media_name is None
        assert result.reason is UnmatchedReason.AMBIGUOUS

    def test_two_longer_prefixes_are_ambiguous(self):
        result = resolve_media_name(
            "64122699523__7F4958E8-FB3A-4208-8990-7C62C7623.json",
            candidates=[
                "64122699523__7F4958E8-FB3A-4208-8990-7C62C7623FBA.JPG",
                "64122699523__7F4958E8-FB3A-4208-8990-7C62C7623CDE.JPG",
            ])

        assert result.media_name is None
        assert result.reason is UnmatchedReason.AMBIGUOUS

    def test_no_media_in_directory(self):
        result = resolve_media_name(
            "IMG_6278.HEIC.supplemental-metadata.json", candidates=[])
        assert result.media_name is None
        assert result.reason is UnmatchedReason.NO_MEDIA_IN_DIR

    def test_fuzzy_does_not_match_a_non_media_candidate(self):
        result = resolve_media_name(
            "64122699523__7F4958E8-FB3A-4208-8990-7C62C7623.json",
            candidates=["64122699523__7F4958E8-FB3A-4208-8990-7C62C7623FBA.txt"])
        assert result.media_name is None

    def test_a_counter_sidecar_never_fuzzy_binds_to_the_base_file(self):
        """`DSC_1570(1).JPG` and `DSC_1570.JPG` are two different photos.

        Found by replaying against the live DB: dropping the counter during
        prefix matching bound 1,386 sidecars to the wrong file, attaching one
        photo's date and location to another. The counter is a disambiguator,
        so a name that parsed cleanly must match it exactly or not at all.
        """
        result = resolve_media_name(
            "DSC_1570.JPG.supplemental-metadata(1).json",
            candidates=["DSC_1570.JPG"])

        assert result.media_name is None

    def test_counter_sidecar_still_binds_when_the_counter_form_exists(self):
        result = resolve_media_name(
            "DSC_1570.JPG.supplemental-metadata(1).json",
            candidates=["DSC_1570.JPG", "DSC_1570(1).JPG"])

        assert result.media_name == "DSC_1570(1).JPG"

    def test_truncated_name_with_a_counter_may_still_fuzzy_bind(self):
        """A cut stem is what fuzzy is for; the counter still has to appear."""
        result = resolve_media_name(
            "64122699523__7F4958E8-FB3A-4208-8990-7C62C76.suppl(2).json",
            candidates=["64122699523__7F4958E8-FB3A-4208-8990-7C62C76234FB(2).JPG"])

        assert result.media_name == \
            "64122699523__7F4958E8-FB3A-4208-8990-7C62C76234FB(2).JPG"

    def test_exact_match_beats_a_fuzzy_one(self):
        result = resolve_media_name(
            "photo.jpg.supplemental-metadata.json",
            candidates=["photo.jpg", "photo.jpg.backup.jpg"])
        assert result.media_name == "photo.jpg"
        assert result.fuzzy is False


class TestNoCrossDirectoryBind:
    def test_candidates_are_the_only_universe(self):
        """A same-named file in a sibling directory must not be reachable.

        `resolve_media_name` is given one directory's listing and returns a
        bare filename, so a cross-directory bind is impossible by
        construction. This pins that contract: the sibling's file is absent
        from `candidates`, and the answer is 'not found', not the sibling.
        """
        sibling_dir_listing = ["IMG_6278.HEIC"]
        this_dir_listing = ["IMG_9999.HEIC"]
        assert "IMG_6278.HEIC" in sibling_dir_listing

        result = resolve_media_name(
            "IMG_6278.HEIC.supplemental-metadata.json",
            candidates=this_dir_listing)

        assert result.media_name is None
        assert result.reason is UnmatchedReason.NO_MEDIA_IN_DIR

    def test_returned_name_is_never_a_path(self):
        result = resolve_media_name(
            "IMG_6278.HEIC.supplemental-metadata.json",
            candidates=["IMG_6278.HEIC"])
        assert "/" not in result.media_name
