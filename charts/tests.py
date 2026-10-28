# charts/tests.py
#
# Per CLAUDE.md's Conventions section: derived-field/aggregation logic is
# worth testing, UI is not. The recordings matrix's aggregation is exactly
# that kind of logic -- it's easy to get subtly wrong (see the test cases
# below, which each target a real way the aggregate could silently
# produce a number nobody ever printed anywhere).

from datetime import date, timedelta
from unittest import mock

from django.http import QueryDict
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from . import chart_week as chart_week_lib
from . import credits as credits_lib
from . import dashboard as dashboard_lib
from . import entry_recording as entry_recording_lib
from . import lastweek_audit as lastweek_audit_lib
from . import recordings_matrix as recordings_matrix_lib
from . import search as search_lib
from . import discogs as discogs_api
from . import discogs_workflow
from . import views
from . import week_submission
from .models import (
    Artist, ArtistAlias, Chart, ChartEntry, DiscogsCandidate, DiscogsMatch,
    Label, Recording, RecordingCredit, Song,
)


class RecordingSearchTests(TestCase):
    # charts/search.py: the one word-AND / field-OR rule every search page uses.

    def setUp(self):
        capitol = Label.objects.create(name="Capitol")
        decca = Label.objects.create(name="Decca")
        self.tex = Recording.objects.create(
            label=capitol, catalog_number="304", side="A",
            song=Song.objects.create(title="Jealous Heart"), credit_text="Tex Ritter",
        )
        self.other = Recording.objects.create(
            label=decca, catalog_number="3040", side="A",
            song=Song.objects.create(title="Heart of Stone"), credit_text="Red Foley",
        )

    def _found(self, text, **kw):
        return set(Recording.objects.filter(search_lib.recording_text_q(text, **kw)))

    def test_words_combine_across_fields(self):
        # "Capitol 304": no single field holds both words.
        self.assertEqual(self._found("Capitol 304"), {self.tex})

    def test_every_word_must_match_something(self):
        self.assertEqual(self._found("heart"), {self.tex, self.other})
        self.assertEqual(self._found("heart foley"), {self.other})
        self.assertEqual(self._found("heart nobody"), set())

    def test_blank_text_matches_nothing_not_everything(self):
        self.assertEqual(self._found("   "), set())

    def test_prefix_searches_through_a_related_model(self):
        chart = Chart.objects.create(category="country", method="jukebox", publication_weekday=5)
        entry = ChartEntry.objects.create(chart=chart, week_date=date(1948, 1, 3), position=1, recording=self.tex)
        found = ChartEntry.objects.filter(search_lib.recording_text_q("ritter", prefix="recording__"))
        self.assertEqual(list(found), [entry])

    def test_extra_fields_can_match_instead(self):
        match = DiscogsMatch.objects.create(recording=self.other, matched_track_title="Stone Heart Blues")
        found = DiscogsMatch.objects.filter(search_lib.recording_text_q(
            "blues", prefix="recording__", extra_fields=("matched_track_title",),
        ))
        self.assertEqual(list(found), [match])

    def test_search_recordings_is_alphabetical_and_limited(self):
        titles = [r.song.title for r in search_lib.search_recordings("heart")]
        self.assertEqual(titles, ["Heart of Stone", "Jealous Heart"])
        self.assertEqual(len(search_lib.search_recordings("heart", limit=1)), 1)


class RecordingsMatrixTests(TestCase):
    def setUp(self):
        self.chart = Chart.objects.create(category="country", method="jukebox", publication_weekday=5)
        self.other_chart = Chart.objects.create(category="rnb", method="jukebox", publication_weekday=5)
        self.label = Label.objects.create(name="Test Label")
        self.song = Song.objects.create(title="Test Song")
        self.recording = Recording.objects.create(
            label=self.label, catalog_number="100", side="A",
            song=self.song, credit_text="Test Artist",
        )

    def _entry(self, chart, week_date, position, recording=None, **kw):
        return ChartEntry.objects.create(
            chart=chart, week_date=week_date, position=position,
            recording=recording or self.recording, **kw,
        )

    def _matrix(self, **params):
        # The matrix's rows and state straight from the module -- no request.
        return recordings_matrix_lib.build(params)

    def _get(self, **params):
        return self.client.get(reverse("charts:recordings_matrix"), params)

    def test_peak_weeks_first_last(self):
        w1, w2, w3 = date(1948, 1, 3), date(1948, 1, 10), date(1948, 1, 17)  # 3 consecutive Saturdays
        self._entry(self.chart, w1, 5)
        self._entry(self.chart, w2, 2)
        self._entry(self.chart, w3, 4)

        rows = self._matrix()["rows"]
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["peak_position"], 2)  # lowest (best) position across the 3 weeks
        self.assertEqual(row["weeks_charted"], 3)
        self.assertEqual(row["first_entry"], w1)
        self.assertEqual(row["last_entry"], w3)

    def test_same_recording_on_two_charts_gets_two_correctly_scoped_rows(self):
        # The whole point of the "one row per (chart, recording)" design:
        # a recording that charted on TWO different charts must show up as
        # TWO rows, each reflecting ONLY that chart's own entries -- not
        # one merged row, and not one row's numbers leaking into the other's.
        w1, w2 = date(1948, 1, 3), date(1948, 1, 10)
        self._entry(self.chart, w1, 5)
        self._entry(self.other_chart, w1, 1)
        self._entry(self.other_chart, w2, 1)

        rows = self._matrix()["rows"]
        self.assertEqual(len(rows), 2)
        by_chart = {r["chart_id"]: r for r in rows}

        self.assertEqual(by_chart[self.chart.id]["peak_position"], 5)
        self.assertEqual(by_chart[self.chart.id]["weeks_charted"], 1)
        self.assertEqual(by_chart[self.other_chart.id]["peak_position"], 1)
        self.assertEqual(by_chart[self.other_chart.id]["weeks_charted"], 2)

    def test_same_week_double_listing_counts_as_one_week_charted(self):
        # Mirrors the 2 accepted printed-anomaly rows in the real data
        # (docs/audit/same_recording_repeated.csv): the same recording
        # listed twice in the same printed week, at two different
        # positions. "Weeks" should still read 1 -- it's one calendar
        # week, however many rows it took to print it.
        w1 = date(1950, 6, 3)
        self._entry(self.chart, w1, 1, printed_anomaly_note="printed anomaly")
        self._entry(self.chart, w1, 2, printed_anomaly_note="printed anomaly")

        row = self._matrix()["rows"][0]
        self.assertEqual(row["weeks_charted"], 1)
        self.assertEqual(row["peak_position"], 1)

    def test_genre_filter(self):
        self._entry(self.chart, date(1948, 1, 3), 1)  # country
        self._entry(self.other_chart, date(1948, 1, 3), 1)  # rnb

        rows = self._matrix(genre="country")["rows"]
        self.assertEqual([r["chart_id"] for r in rows], [self.chart.id])

    def test_chart_filter(self):
        self._entry(self.chart, date(1948, 1, 3), 1)
        self._entry(self.other_chart, date(1948, 1, 3), 1)

        rows = self._matrix(chart=self.other_chart.id)["rows"]
        self.assertEqual([r["chart_id"] for r in rows], [self.other_chart.id])

    def test_search_filters_by_song_artist_label_or_catalog(self):
        self._entry(self.chart, date(1948, 1, 3), 1)
        other_song = Song.objects.create(title="Completely Different")
        other_recording = Recording.objects.create(
            label=self.label, catalog_number="200", side="B",
            song=other_song, credit_text="Someone Else",
        )
        self._entry(self.chart, date(1948, 1, 3), 2, recording=other_recording)

        # Matches by song title...
        self.assertEqual(len(self._matrix(q="Test Song")["rows"]), 1)
        # ...or by catalog number...
        self.assertEqual(len(self._matrix(q="200")["rows"]), 1)
        # ...and a query with no match returns nothing, not everything.
        self.assertEqual(len(self._matrix(q="Nonexistent")["rows"]), 0)

    def test_hx_request_gets_partial_not_full_page(self):
        self._entry(self.chart, date(1948, 1, 3), 1)

        full_page = self._get()
        self.assertTemplateUsed(full_page, "charts/recordings_matrix.html")

        live_update = self.client.get(reverse("charts:recordings_matrix"), HTTP_HX_REQUEST="true")
        self.assertTemplateUsed(live_update, "charts/_recordings_matrix_results.html")
        self.assertTemplateNotUsed(live_update, "charts/recordings_matrix.html")


class RecordingsMatrixCollapsedTests(TestCase):
    """
    The matrix's ?group=recording mode: one row per RECORDING instead of
    one per (chart, recording). What's worth testing here isn't that the
    rows collapse -- it's that collapsing them doesn't quietly turn each
    column into a different, wrong number. Every test below is a thing
    the obvious implementation gets wrong.
    """

    def setUp(self):
        # TWO charts in the SAME genre, which is the case that motivated
        # this mode: filtering to Country still showed one record three
        # times, once per Country chart.
        self.jukebox = Chart.objects.create(category="country", method="jukebox", publication_weekday=5)
        self.retail = Chart.objects.create(category="country", method="retail", publication_weekday=5)
        self.rnb = Chart.objects.create(category="rnb", method="jukebox", publication_weekday=5)
        self.label = Label.objects.create(name="Capitol")
        self.song = Song.objects.create(title="Shot Gun Boogie")
        self.recording = Recording.objects.create(
            label=self.label, catalog_number="1295", side="A",
            song=self.song, credit_text="Tennessee Ernie",
        )

    def _entry(self, chart, week_date, position, recording=None, **kw):
        return ChartEntry.objects.create(
            chart=chart, week_date=week_date, position=position,
            recording=recording or self.recording, **kw,
        )

    def _collapsed(self, **params):
        # Collapsed mode, straight from the module -- no request.
        params.setdefault("group", "recording")
        return recordings_matrix_lib.build(params)

    def test_one_row_per_recording_however_many_charts(self):
        w1, w2 = date(1951, 1, 6), date(1951, 1, 13)
        self._entry(self.jukebox, w1, 3)
        self._entry(self.retail, w1, 5)
        self._entry(self.rnb, w2, 9)

        # Default mode still gives one row per chart...
        self.assertEqual(len(recordings_matrix_lib.build({})["rows"]), 3)
        # ...and the collapsed mode gives exactly one, with a count of
        # how many charts went into it.
        rows = self._collapsed()["rows"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["chart_count"], 3)

    def test_weeks_counts_calendar_weeks_not_rows(self):
        # The headline trap. One record on three charts in the SAME week
        # is three ChartEntry rows but ONE week charted -- summing the
        # per-chart weeks (or counting rows) would report 4 weeks for a
        # record that charted for 2.
        w1, w2 = date(1951, 1, 6), date(1951, 1, 13)
        self._entry(self.jukebox, w1, 3)
        self._entry(self.retail, w1, 5)
        self._entry(self.rnb, w1, 9)
        self._entry(self.jukebox, w2, 2)

        row = self._collapsed()["rows"][0]
        self.assertEqual(ChartEntry.objects.count(), 4)
        self.assertEqual(row["weeks_charted"], 2)

    def test_peak_is_best_across_charts_and_names_its_source_chart(self):
        # Merged, "peak" is the best position reached on ANY chart -- a
        # derived number nobody printed, so the row has to say which
        # chart it came from (CLAUDE.md non-negotiable #4).
        w1 = date(1951, 1, 6)
        self._entry(self.jukebox, w1, 7)
        self._entry(self.retail, w1, 2)

        row = self._collapsed()["rows"][0]
        self.assertEqual(row["peak_position"], 2)
        self.assertEqual(row["peak_chart_label"], str(self.retail))
        self.assertEqual(row["peak_chart_code"], "CRt")

    def test_first_and_last_span_charts_and_link_to_the_right_one(self):
        # First and last appearance can come from two DIFFERENT charts.
        # Each date still has to link to the chart week it actually
        # appeared in -- linking both to one chart would produce a URL
        # for a week that chart never printed this record in.
        first, last = date(1950, 12, 16), date(1951, 2, 17)
        self._entry(self.retail, first, 8)
        self._entry(self.jukebox, date(1951, 1, 6), 1)
        self._entry(self.jukebox, last, 4)

        row = self._collapsed()["rows"][0]
        self.assertEqual(row["first_entry"], first)
        self.assertEqual(row["last_entry"], last)
        self.assertEqual(row["first_chart_id"], self.retail.id)
        self.assertEqual(row["last_chart_id"], self.jukebox.id)
        # And the rendered links point at those two different charts.
        html = self.client.get(reverse("charts:recordings_matrix"), {"group": "recording"}).content.decode()
        self.assertIn(f'href="/{self.retail.id}/1950-12-16/"', html)
        self.assertIn(f'href="/{self.jukebox.id}/1951-02-17/"', html)

    def test_per_chart_breakdown_badges_keep_each_charts_own_peak(self):
        # Collapsing hides the per-chart peaks, so they come back as the
        # Charts column's hover badges rather than being lost.
        w1 = date(1951, 1, 6)
        self._entry(self.jukebox, w1, 1)
        self._entry(self.retail, w1, 6)

        row = self._collapsed()["rows"][0]
        self.assertEqual(
            [(b["code"], b["peak"]) for b in row["chart_badges"]],
            [("CJb", 1), ("CRt", 6)],
        )

    def test_filters_still_apply_and_scope_the_merge(self):
        # genre=country must not merely hide the R&B row -- the R&B
        # entries must be excluded from the surviving row's numbers too,
        # or the merge would silently reach across the filter.
        self._entry(self.jukebox, date(1951, 1, 6), 4)
        self._entry(self.rnb, date(1951, 1, 13), 1)

        row = self._collapsed(genre="country")["rows"][0]
        self.assertEqual(row["chart_count"], 1)
        self.assertEqual(row["peak_position"], 4)  # NOT the R&B chart's 1
        self.assertEqual(row["weeks_charted"], 1)

    def test_default_mode_is_per_chart_and_a_bad_value_falls_back(self):
        self._entry(self.jukebox, date(1951, 1, 6), 1)
        self._entry(self.retail, date(1951, 1, 6), 1)

        self.assertEqual(recordings_matrix_lib.build({})["group"], "chart")
        self.assertEqual(len(recordings_matrix_lib.build({})["rows"]), 2)
        junk = recordings_matrix_lib.build({"group": "nonsense"})
        self.assertEqual(junk["group"], "chart")
        self.assertEqual(len(junk["rows"]), 2)

    def test_sorting_by_charts_column_does_not_un_collapse(self):
        # ?sort=chart orders by chart__category, a column that isn't in
        # the collapsed GROUP BY -- ordering by it anyway would re-join
        # the chart table and split the merged rows back apart. Collapsed
        # mode sorts by the chart COUNT instead.
        one_chart = Recording.objects.create(
            label=self.label, catalog_number="1296", side="A",
            song=Song.objects.create(title="Mule Train"), credit_text="Tennessee Ernie",
        )
        self._entry(self.jukebox, date(1951, 1, 6), 1)
        self._entry(self.retail, date(1951, 1, 6), 1)
        self._entry(self.jukebox, date(1951, 1, 6), 2, recording=one_chart)

        rows = self._collapsed(sort="chart", dir="desc")["rows"]
        self.assertEqual(len(rows), 2)  # still one row each, not three
        # Descending by chart count: the 2-chart recording comes first.
        self.assertEqual([r["chart_count"] for r in rows], [2, 1])

    def test_mode_and_filters_ride_along_on_sort_links(self):
        # Clicking a column heading is a plain link (full page load), so
        # if it didn't carry ?group= and the filters, sorting a filtered
        # collapsed view would bounce it back to unfiltered per-chart.
        self._entry(self.jukebox, date(1951, 1, 6), 1)

        columns = self._collapsed(genre="country", q="Boogie")["columns"]
        for key in ("peak", "song", "weeks"):
            self.assertIn("group=recording", columns[key]["href"])
            self.assertIn("genre=country", columns[key]["href"])
            self.assertIn("q=Boogie", columns[key]["href"])

    def test_headers_rename_the_two_columns_that_change_meaning(self):
        self._entry(self.jukebox, date(1951, 1, 6), 1)

        per_chart = recordings_matrix_lib.build({})["columns"]
        self.assertEqual(per_chart["peak"]["label"], "Peak")
        self.assertEqual(per_chart["chart"]["label"], "Chart")

        collapsed = self._collapsed()["columns"]
        self.assertEqual(collapsed["peak"]["label"], "Best peak")
        self.assertEqual(collapsed["chart"]["label"], "Charts")

    def test_htmx_live_filter_keeps_the_mode(self):
        self._entry(self.jukebox, date(1951, 1, 6), 1)
        self._entry(self.retail, date(1951, 1, 6), 1)

        live = self.client.get(
            reverse("charts:recordings_matrix"), {"group": "recording"}, HTTP_HX_REQUEST="true",
        )
        self.assertTemplateUsed(live, "charts/_recordings_matrix_results.html")
        self.assertTemplateNotUsed(live, "charts/recordings_matrix.html")
        self.assertEqual(len(live.context["rows"]), 1)


class EntryRecordingDuplicateCheckTests(TestCase):
    # entry_recording.possible_duplicates, the backstop check before a new
    # recording is created: search-first is the INTENDED workflow, so
    # what's worth testing is the two signals the check is built on.

    def setUp(self):
        self.existing = Recording.objects.create(
            label=Label.objects.create(name="Capitol"), catalog_number="40065", side="A",
            song=Song.objects.create(title="Bouquet of Roses"), credit_text="Eddy Arnold",
        )

    def _new(self, **fields):
        return entry_recording_lib.NewRecording(**{
            "song_title": "A Completely Different Song", "artist_names": ["Some New Artist"],
            "credit_text": "Some New Artist", "label_name": "Decca", "catalog_number": "46234",
            "side": "B", **fields,
        })

    def test_nothing_similar_on_file(self):
        self.assertEqual(entry_recording_lib.possible_duplicates(self._new()), [])

    def test_matching_song_and_artist(self):
        # Signal 1: same song + credit, a different (say, mistyped) catalog #.
        new = self._new(song_title="Bouquet of Roses", credit_text="Eddy Arnold",
                        label_name="Capitol", catalog_number="40065-X", side="A")
        self.assertEqual(entry_recording_lib.possible_duplicates(new), [self.existing])

    def test_matching_label_and_catalog_with_different_artist_text(self):
        # Signal 2, the more dangerous one to miss: the same release, artist
        # typed differently -- "Eddy Arnold" appears nowhere in what was typed.
        new = self._new(song_title="Some Other Title", credit_text="A Totally Different Artist",
                        label_name="Capitol", catalog_number="40065", side="A")
        self.assertEqual(entry_recording_lib.possible_duplicates(new), [self.existing])

    def test_the_flip_side_of_the_release_is_not_a_duplicate(self):
        # Entering the B-side of Capitol 40065 used to trip signal 2 every time.
        new = self._new(song_title="A Heart Full of Love", credit_text="Eddy Arnold",
                        label_name="Capitol", catalog_number="40065", side="B")
        self.assertEqual(entry_recording_lib.possible_duplicates(new), [])

    def test_same_release_same_side_is_still_a_duplicate(self):
        new = self._new(song_title="A Heart Full of Love", credit_text="Eddy Arnold",
                        label_name="Capitol", catalog_number="40065", side="A")
        self.assertEqual(entry_recording_lib.possible_duplicates(new), [self.existing])

    def test_same_release_same_song_other_side_is_still_a_duplicate(self):
        # Same song on "the other side" is a mistyped side, not a flip-side.
        new = self._new(song_title="bouquet of  roses", credit_text="Eddy Arnold",
                        label_name="Capitol", catalog_number="40065", side="B")
        self.assertEqual(entry_recording_lib.possible_duplicates(new), [self.existing])

    def test_missing_fields_are_named(self):
        new = self._new(song_title="", artist_names=[], label_name="")
        self.assertEqual(new.missing(), ["song title", "artist", "label"])
        self.assertEqual(self._new().missing(), [])


class EntryRecordingPageSmokeTests(TestCase):
    # The create box's wiring: the duplicate check stops a first submit,
    # "create new anyway" (confirm_create=1) goes past it.

    def setUp(self):
        self.chart = Chart.objects.create(category="country", method="jukebox", publication_weekday=5)
        Recording.objects.create(
            label=Label.objects.create(name="Capitol"), catalog_number="40065", side="A",
            song=Song.objects.create(title="Bouquet of Roses"), credit_text="Eddy Arnold",
        )

    def _post(self, **extra):
        return self.client.post(reverse("charts:entry_create", args=[self.chart.id]), {
            "row": "0", "week": "1949-01-01", "new_song_0": "Bouquet of Roses",
            "new_artist_0": "Eddy Arnold", "new_label_0": "Capitol",
            "new_catalog_0": "40065-X", "new_side_0": "A", **extra,
        })

    def test_a_flip_side_goes_straight_through(self):
        response = self._post(new_song_0="A Heart Full of Love", new_catalog_0="40065", new_side_0="B")
        self.assertTemplateUsed(response, "charts/_entry_resolved.html")
        self.assertContains(response, "Bouquet of Roses")   # its flip-side, named in a note
        self.assertEqual(Recording.objects.count(), 2)

    def test_a_possible_duplicate_stops_creation(self):
        response = self._post()
        self.assertTemplateUsed(response, "charts/_entry_create_duplicate_check.html")
        self.assertEqual(Recording.objects.count(), 1)

    def test_confirm_create_goes_past_the_check(self):
        response = self._post(confirm_create="1")
        self.assertTemplateUsed(response, "charts/_entry_resolved.html")
        self.assertEqual(Recording.objects.count(), 2)

    def test_a_missing_field_is_reported_not_created(self):
        response = self._post(new_label_0="")
        self.assertContains(response, "missing: label")
        self.assertEqual(Recording.objects.count(), 1)


# =============================================================================
# Discogs matching
# =============================================================================
#
# EVERY test below mocks charts.discogs._request. Nothing in this suite is
# ever allowed to hit the real API: it would be slow, rate-limited,
# non-deterministic, and would fail entirely on a machine with no token or
# no internet. What's worth testing here is OUR logic -- the confidence
# scoring, the two-tier search, the per-track credit extraction, and the
# review workflow's state transitions -- not Discogs' behaviour.


class FlipSideTests(TestCase):
    """The "+ flip-side" shortcut: a release's other side, pre-filled from
    the side already on file, and its Discogs details read from the release
    JSON that side already fetched -- no network (non-negotiable #2)."""

    RELEASE_JSON = {
        "id": 4025819, "title": "Slipping Around / My Tennessee Baby",
        "artists": [{"name": "Ernest Tubb"}], "labels": [{"name": "Decca", "catno": "46173"}],
        "formats": [{"name": "Shellac"}], "year": 1949, "country": "US",
        "genres": ["Folk, World, & Country"], "styles": ["Honky Tonk"],
        "tracklist": [
            {"position": "A", "title": "Slipping Around",
             "extraartists": [{"name": "Floyd Tillman", "role": "Written-By"}]},
            {"position": "B", "title": "My Tennessee Baby",
             "extraartists": [{"name": "Cliff Stone", "role": "Written-By"}]},
        ],
    }

    def setUp(self):
        self.chart = Chart.objects.create(category="country", method="jukebox", publication_weekday=5)
        self.a_side = Recording.objects.create(
            label=Label.objects.create(name="Decca"), catalog_number="46173", side="A",
            song=Song.objects.create(title="Slipping Around"), credit_text="Ernest Tubb",
        )
        RecordingCredit.objects.create(
            recording=self.a_side, artist=Artist.objects.create(canonical_name="Ernest Tubb"), credit_order=1,
        )

    def _link_a_side(self, enriched=True):
        return DiscogsMatch.objects.create(
            recording=self.a_side, status=DiscogsMatch.CONFIRMED, discogs_release_id=4025819,
            raw_data=self.RELEASE_JSON if enriched else None, linked_at=timezone.now(),
        )

    # ---- the draft

    def test_draft_copies_the_release_and_flips_the_side(self):
        self._link_a_side()
        draft = entry_recording_lib.flip_side_draft(self.a_side)
        self.assertEqual(draft.song_title, "")            # the one thing that's always different
        self.assertEqual(draft.side, "B")
        self.assertEqual((draft.label_name, draft.catalog_number), ("Decca", "46173"))
        self.assertEqual(draft.artist_names, ["Ernest Tubb"])
        self.assertEqual(draft.credit_text, "Ernest Tubb")
        self.assertEqual(draft.discogs_ref, "4025819")

    def test_draft_side_flips_both_ways_and_ab_is_left_blank(self):
        for side, flipped in (("A", "B"), ("B", "A"), ("AB", ""), ("", "")):
            self.a_side.side = side
            self.assertEqual(entry_recording_lib.flip_side_draft(self.a_side).side, flipped, side)

    def test_draft_has_no_discogs_ref_when_the_side_is_not_linked(self):
        self.assertEqual(entry_recording_lib.flip_side_draft(self.a_side).discogs_ref, "")

    # ---- enriching from the cache

    def _b_side_linked(self):
        b_side, _ = entry_recording_lib.create_recording(entry_recording_lib.NewRecording(
            song_title="My Tennessee Baby", artist_names=["Ernest Tubb"], credit_text="Ernest Tubb",
            label_name="Decca", catalog_number="46173", side="B",
        ))
        match = DiscogsMatch.objects.create(recording=b_side)
        discogs_workflow.link_manually(match, 4025819, None, overwrite=False)
        return match

    def test_a_flip_side_is_enriched_from_the_release_already_cached(self):
        self._link_a_side()
        match = self._b_side_linked()
        with mock.patch.object(discogs_api, "_request", side_effect=AssertionError("no network allowed")):
            self.assertTrue(discogs_workflow.enrich_from_cache(match))
        match.refresh_from_db()
        self.assertFalse(match.needs_detail)
        # ITS OWN track, not the A-side's.
        self.assertEqual(match.matched_track_title, "My Tennessee Baby")
        self.assertEqual(match.songwriters, "Cliff Stone")

    def test_without_a_cached_copy_it_simply_still_needs_details(self):
        self._link_a_side(enriched=False)
        match = self._b_side_linked()
        self.assertFalse(discogs_workflow.enrich_from_cache(match))
        self.assertTrue(match.needs_detail)

    def test_release_detail_reads_json_without_fetching(self):
        with mock.patch.object(discogs_api, "_request", side_effect=AssertionError("no network allowed")):
            detail = discogs_api.release_detail(self.RELEASE_JSON, self.a_side)
        self.assertEqual(detail["songwriters"], "Floyd Tillman")
        self.assertEqual(detail["matched_track_title"], "Slipping Around")


class FlipSidePageSmokeTests(TestCase):
    # The entry form's wiring: the button on a search result, the
    # pre-filled box, and an offline-complete Discogs link on create.

    RELEASE_JSON = FlipSideTests.RELEASE_JSON

    def setUp(self):
        FlipSideTests.setUp(self)
        FlipSideTests._link_a_side(self)

    def test_search_results_offer_a_flip_side_button(self):
        response = self.client.post(reverse("charts:entry_search", args=[self.chart.id]),
                                    {"row": "0", "week": "1949-10-01", "search_0": "Decca 46173"})
        self.assertContains(response, f"flip_of={self.a_side.id}")

    def test_the_box_opens_pre_filled_with_the_cursor_on_the_title(self):
        response = self.client.get(reverse("charts:entry_create_form", args=[self.chart.id]),
                                   {"row": "0", "week": "1949-10-01", "flip_of": self.a_side.id})
        self.assertContains(response, "Flip-side of Decca 46173 A")
        self.assertContains(response, 'value="46173"')
        self.assertContains(response, 'value="B"')
        self.assertContains(response, 'value="Ernest Tubb"')
        self.assertContains(response, 'value="4025819"')
        self.assertContains(response, "autofocus")

    def test_creating_the_flip_side_links_and_enriches_it_offline(self):
        with mock.patch.object(discogs_api, "_request", side_effect=AssertionError("no network allowed")):
            response = self.client.post(reverse("charts:entry_create", args=[self.chart.id]), {
                "row": "0", "week": "1949-10-01", "confirm_create": "1",
                "new_song_0": "My Tennessee Baby", "new_artist_0": "Ernest Tubb",
                "new_credit_text_0": "Ernest Tubb", "new_label_0": "Decca",
                "new_catalog_0": "46173", "new_side_0": "B", "new_discogs_0": "4025819",
            })
        self.assertContains(response, "filled in from its flip-side")
        b_side = Recording.objects.get(catalog_number="46173", side="B")
        self.assertEqual(b_side.discogs.songwriters, "Cliff Stone")


class CreditCanonicalTests(TestCase):
    """
    The third case: one act billed with a backing band. "Julia Lee And
    Her Boy Friends" is Julia Lee -- so the canonical name is shorter
    and the printed billing belongs in ArtistAlias, which is exactly
    what that table is for.
    """

    def setUp(self):
        self.label = Label.objects.create(name="Capitol")
        self.billed = Artist.objects.create(canonical_name="Julia Lee And Her Boy Friends")
        self.recording = Recording.objects.create(
            label=self.label, catalog_number="40028", side="A",
            song=Song.objects.create(title="Snatch And Grab It"),
            credit_text="Julia Lee And Her Boy Friends",
        )
        RecordingCredit.objects.create(recording=self.recording, artist=self.billed, credit_order=1)

    def test_leader_guess_recognises_band_names_but_not_collaborations(self):
        self.assertEqual(credits_lib.leader_guess("Julia Lee And Her Boy Friends"), "Julia Lee")
        self.assertEqual(credits_lib.leader_guess("Lester Young's Quartet"), "Lester Young")
        self.assertEqual(credits_lib.leader_guess("Cozy Cole's All Star Band"), "Cozy Cole")
        # A duo act name and a genuine collaboration are NOT this case.
        self.assertIsNone(credits_lib.leader_guess("Homer & Jethro"))
        self.assertIsNone(credits_lib.leader_guess("Ernest Tubb - Red Foley"))
        self.assertIsNone(credits_lib.leader_guess("Eddy Arnold"))

    def test_recredits_to_the_leader_and_keeps_the_billing_as_an_alias(self):
        recordings, canonical, created = credits_lib.apply_canonical(self.billed, "Julia Lee")

        self.assertTrue(created)
        self.assertEqual(canonical.canonical_name, "Julia Lee")
        self.assertEqual(len(recordings), 1)
        self.assertEqual(self.recording.credits.get().artist, canonical)
        # The long form survives as a searchable spelling, not as an artist.
        alias = ArtistAlias.objects.get(alias_text="Julia Lee And Her Boy Friends")
        self.assertEqual(alias.artist, canonical)
        self.assertFalse(Artist.objects.filter(canonical_name="Julia Lee And Her Boy Friends").exists())

    def test_credit_text_is_untouched(self):
        credits_lib.apply_canonical(self.billed, "Julia Lee")
        self.recording.refresh_from_db()
        self.assertEqual(self.recording.credit_text, "Julia Lee And Her Boy Friends")

    def test_merges_into_an_existing_canonical_artist(self):
        # The real payoff: Lester Young was on file twice, as "...and His
        # Kansas City Seven" and "...'s Quartet". Reducing both to the
        # leader collapses them into one artist.
        existing = Artist.objects.create(canonical_name="Lester Young")
        for n, (cat, band) in enumerate([("603", "Lester Young's Quartet"),
                                         ("604", "Lester Young and His Kansas City Seven")]):
            billed = Artist.objects.create(canonical_name=band)
            rec = Recording.objects.create(
                label=self.label, catalog_number=cat, side="A",
                song=Song.objects.create(title=f"Song {n}"), credit_text=band)
            RecordingCredit.objects.create(recording=rec, artist=billed, credit_order=1)
            _, canonical, created = credits_lib.apply_canonical(billed, "Lester Young")
            self.assertEqual(canonical, existing)
            self.assertFalse(created)

        self.assertEqual(existing.recording_credits.count(), 2)
        self.assertEqual(
            set(ArtistAlias.objects.filter(artist=existing).values_list("alias_text", flat=True)),
            {"Lester Young's Quartet", "Lester Young and His Kansas City Seven"})

    def test_refuses_the_same_name(self):
        with self.assertRaises(ValueError):
            credits_lib.apply_canonical(self.billed, "Julia Lee And Her Boy Friends")
        self.assertTrue(Artist.objects.filter(pk=self.billed.pk).exists())

    def test_refuses_a_blank_name(self):
        with self.assertRaises(ValueError):
            credits_lib.apply_canonical(self.billed, "   ")

    def test_leader_rows_reach_the_queue_even_with_no_delimiter(self):
        # "Lester Young's Quartet" contains no joining delimiter at all,
        # so the split check alone would never surface it.
        billed = Artist.objects.create(canonical_name="Cozy Cole's All Star Band")
        rec = Recording.objects.create(
            label=self.label, catalog_number="605", side="A",
            song=Song.objects.create(title="Jazz"), credit_text="Cozy Cole's All Star Band")
        RecordingCredit.objects.create(recording=rec, artist=billed, credit_order=1)
        self.assertIn(billed, credits_lib.pending_artists())

    def test_split_refuses_rather_than_cascade_deleting_aliases(self):
        # Deleting an Artist CASCADEs to its aliases, and a split has no
        # unambiguous home for one. Refuse loudly instead of losing it.
        ArtistAlias.objects.create(artist=self.billed, alias_text="Julia Lee & Her Boyfriends")
        with self.assertRaises(ValueError):
            credits_lib.apply_split(self.billed, ["Julia Lee", "Her Boy Friends"])
        self.assertTrue(ArtistAlias.objects.filter(alias_text="Julia Lee & Her Boyfriends").exists())

    def test_existing_aliases_move_across_rather_than_being_lost(self):
        ArtistAlias.objects.create(artist=self.billed, alias_text="Julia Lee & Her Boyfriends")
        _, canonical, _ = credits_lib.apply_canonical(self.billed, "Julia Lee")
        moved = ArtistAlias.objects.get(alias_text="Julia Lee & Her Boyfriends")
        self.assertEqual(moved.artist, canonical)

    def test_view_end_to_end(self):
        response = self.client.post(
            reverse("charts:credit_canonical", args=[self.billed.id]),
            {"canonical": "Julia Lee"}, follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.recording.credits.get().artist.canonical_name, "Julia Lee")


class EntryFormCollaborationTests(TestCase):
    """
    Entering a collaboration correctly at entry time -- one
    RecordingCredit per artist -- rather than filing it as a single
    fake "A - B" Artist row, which is what created the 43 rows the
    /credits/ queue exists to clean up. Through entry_recording directly.
    """

    def _create(self, artists, credit_text=""):
        # credit_text defaults the way NewRecording.from_post does.
        names = list(dict.fromkeys(n.strip() for n in artists if n.strip()))
        return entry_recording_lib.create_recording(entry_recording_lib.NewRecording(
            song_title="Tennessee Border No. 2", artist_names=names,
            credit_text=credit_text or " - ".join(names),
            label_name="Decca", catalog_number="46200", side="A",
        ))

    def test_two_artists_produce_two_credits_in_billing_order(self):
        self._create(["Ernest Tubb", "Red Foley"])
        recording = Recording.objects.get(catalog_number="46200")
        self.assertEqual(
            [c.artist.canonical_name for c in recording.credits.order_by("credit_order")],
            ["Ernest Tubb", "Red Foley"])
        self.assertEqual([c.credit_order for c in recording.credits.order_by("credit_order")], [1, 2])
        # No fake combined artist row was created.
        self.assertFalse(Artist.objects.filter(canonical_name="Ernest Tubb - Red Foley").exists())

    def test_three_artists_produce_three_credits_in_order(self):
        self._create(["Ernest Tubb", "Red Foley", "Minnie Pearl"])
        recording = Recording.objects.get(catalog_number="46200")
        credits = recording.credits.order_by("credit_order")
        self.assertEqual([c.artist.canonical_name for c in credits],
                         ["Ernest Tubb", "Red Foley", "Minnie Pearl"])
        self.assertEqual([c.credit_order for c in credits], [1, 2, 3])
        self.assertEqual(recording.credit_text, "Ernest Tubb - Red Foley - Minnie Pearl")

    def test_explicitly_typed_credit_text_wins_over_the_default(self):
        # credit_text is the as-printed billing; a default must never
        # override what was actually typed from the page.
        self._create(["Ernest Tubb", "Red Foley"],
                     credit_text="Ernest Tubb and Red Foley with the Texas Troubadours")
        self.assertEqual(Recording.objects.get(catalog_number="46200").credit_text,
                         "Ernest Tubb and Red Foley with the Texas Troubadours")

    def test_single_artist_still_works_unchanged(self):
        self._create(["Ernest Tubb"])
        recording = Recording.objects.get(catalog_number="46200")
        self.assertEqual(recording.credits.count(), 1)
        self.assertEqual(recording.credit_text, "Ernest Tubb")

    def test_existing_artist_is_reused_via_alias_not_duplicated(self):
        real = Artist.objects.create(canonical_name="Big Joe Turner")
        ArtistAlias.objects.create(artist=real, alias_text="Joe Turner")
        self._create(["Joe Turner"])
        recording = Recording.objects.get(catalog_number="46200")
        self.assertEqual(recording.credits.get().artist, real)
        self.assertFalse(Artist.objects.filter(canonical_name="Joe Turner").exists())

    def test_the_same_release_and_side_is_reused_not_duplicated(self):
        first, _ = self._create(["Ernest Tubb"])
        again, _ = self._create(["Ernest Tubb"])
        self.assertEqual(first, again)
        self.assertEqual(Recording.objects.filter(catalog_number="46200").count(), 1)

    def test_a_reused_recording_with_different_artists_warns_and_changes_nothing(self):
        first, _ = self._create(["Ernest Tubb"])
        again, notes = self._create(["Red Foley"])
        self.assertEqual(again, first)
        self.assertEqual([c.artist.canonical_name for c in again.credits.all()], ["Ernest Tubb"])
        self.assertEqual(len(notes), 1)
        self.assertIn("Ernest Tubb", notes[0])
        self.assertIn("Red Foley", notes[0])
        self.assertIn("NOT saved", notes[0])

    def test_a_reused_recording_with_the_same_artists_says_nothing(self):
        self._create(["Ernest Tubb"])
        _, notes = self._create(["Ernest Tubb"])
        self.assertEqual(notes, [])

    def _flip(self, artists, credit_text="", side="B", title="The Flip"):
        names = list(artists)
        return entry_recording_lib.create_recording(entry_recording_lib.NewRecording(
            song_title=title, artist_names=names, credit_text=credit_text or " - ".join(names),
            label_name="Decca", catalog_number="46200", side=side,
        ))

    def test_a_flip_side_billed_differently_gets_a_note_but_is_kept(self):
        # Decca 14505 really is printed this way: Red Foley on A,
        # "Red Foley With Jordanaires" on B.
        self._create(["Red Foley"])
        flip, notes = self._flip(["Red Foley"], credit_text="Red Foley With Jordanaires")
        self.assertEqual(flip.credit_text, "Red Foley With Jordanaires")   # kept as typed
        self.assertEqual(len(notes), 1)
        self.assertIn("“Red Foley”", notes[0])
        self.assertIn("“Red Foley With Jordanaires”", notes[0])

    def test_a_flip_side_billed_the_same_gets_only_a_quiet_note(self):
        self._create(["Ernest Tubb", "Red Foley"])
        _, notes = self._flip(["Ernest Tubb", "Red Foley"])
        self.assertEqual(notes, ["Flip-side of “Tennessee Border No. 2” (Decca 46200 A), already on file."])

    def test_flip_sides_lists_the_other_sides_of_the_release(self):
        a, _ = self._create(["Ernest Tubb"])
        b, _ = self._flip(["Ernest Tubb"])
        self.assertEqual(entry_recording_lib.flip_sides(a), [b])
        self.assertEqual(entry_recording_lib.flip_sides(b), [a])

    def test_joined_name_in_one_box_still_creates_but_warns(self):
        # Non-blocking on purpose -- credit_text records the billing
        # correctly either way and /credits/ can unpick it -- but the
        # warning is what makes it cheap to fix now instead of later.
        recording, notes = self._create(["Ernest Tubb - Red Foley"])
        self.assertIsNotNone(recording.pk)
        self.assertTrue(any("looks like 2 artists" in n for n in notes))

    def test_single_act_name_containing_and_is_not_warned_about(self):
        # "Homer & Jethro" is one act. Warning here would be noise, and
        # noise is what makes warnings get ignored.
        _, notes = self._create(["Homer & Jethro"])
        self.assertEqual(notes, [])


class EntryFormCollaborationPostTests(TestCase):
    # How the create box's inputs become a NewRecording: any number of
    # artist boxes under one field name, blanks and repeats dropped.

    def _from_post(self, artists, **fields):
        post = QueryDict(mutable=True)
        post.setlist("new_artist_0", artists)
        for k, v in {"new_song_0": "Song", "new_label_0": "decca ", "new_side_0": "a", **fields}.items():
            post[k] = v
        return entry_recording_lib.NewRecording.from_post(post, "0")

    def test_artist_boxes_in_order_blanks_and_repeats_dropped(self):
        new = self._from_post(["Ernest Tubb", "   ", "Red Foley", "Ernest Tubb"])
        self.assertEqual(new.artist_names, ["Ernest Tubb", "Red Foley"])

    def test_credit_defaults_to_the_printed_joining_convention(self):
        self.assertEqual(self._from_post(["Ernest Tubb", "Red Foley"]).credit_text, "Ernest Tubb - Red Foley")
        typed = self._from_post(["Ernest Tubb"], new_credit_text_0="Ernest Tubb and his Texas Troubadours")
        self.assertEqual(typed.credit_text, "Ernest Tubb and his Texas Troubadours")

    def test_fields_are_trimmed_and_side_uppercased(self):
        new = self._from_post(["X"])
        self.assertEqual((new.label_name, new.side), ("decca", "A"))


class CreditSplitTests(TestCase):
    """
    Splitting a combined-credit Artist into a real multi-artist credit.
    This rewrites RecordingCredit rows and deletes an Artist row, so the
    destructive edges matter more here than the happy path does.
    """

    def setUp(self):
        self.label = Label.objects.create(name="Decca")
        self.combined = Artist.objects.create(canonical_name="Bing Crosby - Andrews Sisters")
        self.recording = Recording.objects.create(
            label=self.label, catalog_number="23277", side="A",
            song=Song.objects.create(title="Pistol Packin Mama"),
            credit_text="Bing Crosby - Andrews Sisters",
        )
        RecordingCredit.objects.create(recording=self.recording, artist=self.combined, credit_order=1)

    def test_dash_is_a_strong_signal_and_and_is_not(self):
        self.assertEqual(credits_lib.split_name("Bing Crosby - Andrews Sisters"),
                         (["Bing Crosby", "Andrews Sisters"], True))
        # " and "/"&" sit inside a single act's name far more often than
        # between two acts, so they're surfaced but never presumed.
        self.assertEqual(credits_lib.split_name("Homer & Jethro"), (["Homer", "Jethro"], False))
        self.assertEqual(credits_lib.split_name("Eddy Arnold"), (["Eddy Arnold"], False))

    def test_split_creates_both_credits_in_billing_order(self):
        Artist.objects.create(canonical_name="Bing Crosby")
        recordings, created, deleted = credits_lib.apply_split(
            self.combined, ["Bing Crosby", "Andrews Sisters"])

        credits = list(self.recording.credits.select_related("artist").order_by("credit_order"))
        self.assertEqual([c.artist.canonical_name for c in credits],
                         ["Bing Crosby", "Andrews Sisters"])
        self.assertEqual([c.credit_order for c in credits], [1, 2])
        self.assertEqual(len(recordings), 1)
        self.assertTrue(deleted)
        self.assertEqual([a.canonical_name for a in created], ["Andrews Sisters"])

    def test_split_into_three(self):
        credits_lib.apply_split(self.combined, ["Bing Crosby", "Andrews Sisters", "Vic Schoen"])
        self.assertEqual(
            [c.artist.canonical_name for c in self.recording.credits.order_by("credit_order")],
            ["Bing Crosby", "Andrews Sisters", "Vic Schoen"])

    def test_split_never_touches_credit_text(self):
        # credit_text is the billing exactly as the magazine printed it
        # (non-negotiable #4). Restructuring OUR credits must not rewrite
        # what the page said.
        credits_lib.apply_split(self.combined, ["Bing Crosby", "Andrews Sisters"])
        self.recording.refresh_from_db()
        self.assertEqual(self.recording.credit_text, "Bing Crosby - Andrews Sisters")

    def test_combined_row_is_deleted_once_nothing_credits_it(self):
        credits_lib.apply_split(self.combined, ["Bing Crosby", "Andrews Sisters"])
        self.assertFalse(Artist.objects.filter(canonical_name="Bing Crosby - Andrews Sisters").exists())

    def test_edited_names_are_used_not_the_naive_parse(self):
        # Precisely why the parts are editable inputs: splitting
        # "James - Sinatra" literally would put "James" and "Sinatra" on
        # file as canonical names, which is wrong.
        combined = Artist.objects.create(canonical_name="James - Sinatra")
        rec = Recording.objects.create(
            label=self.label, catalog_number="35587", side="B",
            song=Song.objects.create(title="Flash"), credit_text="James - Sinatra",
        )
        RecordingCredit.objects.create(recording=rec, artist=combined, credit_order=1)

        credits_lib.apply_split(combined, ["Harry James", "Frank Sinatra"])

        self.assertEqual(
            [c.artist.canonical_name for c in rec.credits.order_by("credit_order")],
            ["Harry James", "Frank Sinatra"])
        self.assertFalse(Artist.objects.filter(canonical_name="James").exists())

    def test_split_applies_to_every_recording_using_that_credit(self):
        second = Recording.objects.create(
            label=self.label, catalog_number="23277", side="B",
            song=Song.objects.create(title="Victory Polka"),
            credit_text="Bing Crosby - Andrews Sisters",
        )
        RecordingCredit.objects.create(recording=second, artist=self.combined, credit_order=1)

        recordings, _, _ = credits_lib.apply_split(self.combined, ["Bing Crosby", "Andrews Sisters"])
        self.assertEqual(len(recordings), 2)
        self.assertEqual(second.credits.count(), 2)

    def test_existing_artist_is_reused_case_insensitively(self):
        existing = Artist.objects.create(canonical_name="Bing Crosby")
        credits_lib.apply_split(self.combined, ["bing crosby", "Andrews Sisters"])
        self.assertEqual(Artist.objects.filter(canonical_name__iexact="bing crosby").count(), 1)
        self.assertTrue(self.recording.credits.filter(artist=existing).exists())

    def test_an_alias_resolves_to_its_artist(self):
        real = Artist.objects.create(canonical_name="Big Joe Turner")
        ArtistAlias.objects.create(artist=real, alias_text="Joe Turner")
        self.assertEqual(credits_lib.resolve_artist("Joe Turner"), real)

    def test_refuses_a_split_that_is_not_two_artists(self):
        with self.assertRaises(ValueError):
            credits_lib.apply_split(self.combined, ["Only One Name"])
        self.assertTrue(Artist.objects.filter(pk=self.combined.pk).exists())
        self.assertEqual(self.recording.credits.count(), 1)

    def test_refuses_when_both_names_are_the_same_artist(self):
        Artist.objects.create(canonical_name="Bing Crosby")
        with self.assertRaises(ValueError):
            credits_lib.apply_split(self.combined, ["Bing Crosby", "bing crosby"])
        self.assertTrue(Artist.objects.filter(pk=self.combined.pk).exists())

    def test_keep_removes_it_from_the_queue_without_changing_data(self):
        single = Artist.objects.create(canonical_name="Homer & Jethro")
        rec = Recording.objects.create(
            label=self.label, catalog_number="10352", side="A",
            song=Song.objects.create(title="Some Song"), credit_text="Homer & Jethro",
        )
        RecordingCredit.objects.create(recording=rec, artist=single, credit_order=1)

        self.assertIn(single, credits_lib.pending_artists())
        self.client.post(reverse("charts:credit_keep", args=[single.id]))
        single.refresh_from_db()
        self.assertTrue(single.confirmed_single_act)
        self.assertNotIn(single, credits_lib.pending_artists())
        self.assertEqual(rec.credits.count(), 1)  # nothing about the data moved

    def test_keep_can_be_undone(self):
        # Without an undo, a misclick -- or a decision made before the
        # "canonical is" option existed -- removed a row from the queue
        # permanently, recoverable only through the admin.
        single = Artist.objects.create(canonical_name="Actually A Collaboration - Really")
        rec = Recording.objects.create(
            label=self.label, catalog_number="7777", side="A",
            song=Song.objects.create(title="Whatever"), credit_text="x")
        RecordingCredit.objects.create(recording=rec, artist=single, credit_order=1)

        self.client.post(reverse("charts:credit_keep", args=[single.id]))
        single.refresh_from_db()
        self.assertNotIn(single, credits_lib.pending_artists())

        self.client.post(reverse("charts:credit_reopen", args=[single.id]))
        single.refresh_from_db()
        self.assertFalse(single.confirmed_single_act)
        self.assertIn(single, credits_lib.pending_artists())

    def test_uncredited_combined_artist_is_not_offered(self):
        # Looks combined, but nothing credits it -- there's no
        # re-crediting work to do, so it isn't queue material.
        Artist.objects.create(canonical_name="Nobody - Nowhere")
        self.assertNotIn("Nobody - Nowhere",
                         [a.canonical_name for a in credits_lib.pending_artists()])

    def test_split_view_end_to_end(self):
        response = self.client.post(
            reverse("charts:credit_split", args=[self.combined.id]),
            {"part": ["Bing Crosby", "Andrews Sisters"]}, follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.recording.credits.count(), 2)


class EntryFormDiscogsLinkTests(TestCase):
    """
    The optional hand-typed Discogs field on the entry form's inline
    "create new recording" sub-form.

    The load-bearing property here is that entering a chart week NEVER
    touches the network (CLAUDE.md non-negotiable #2), so every test
    below patches _request to explode if called.
    """

    def setUp(self):
        self.chart = Chart.objects.create(category="country", method="jukebox", publication_weekday=5)

    def _create(self, discogs_ref="", **overrides):
        data = {
            "row": "0", "week": "1949-01-01", "confirm_create": "1",
            "new_song_0": "A Brand New Song", "new_artist_0": "A New Artist",
            "new_credit_text_0": "", "new_label_0": "Decca",
            "new_catalog_0": "12345", "new_side_0": "A",
            "new_discogs_0": discogs_ref,
        }
        data.update(overrides)
        # side_effect, not return_value: any network call at all is a
        # failure of the offline guarantee, not just a wrong one.
        with mock.patch.object(discogs_api, "_request", side_effect=AssertionError("entry form must never call the API")):
            return self.client.post(reverse("charts:entry_create", args=[self.chart.id]), data)

    def test_typed_release_url_links_the_new_recording(self):
        self._create(discogs_ref="https://www.discogs.com/release/98765")
        recording = Recording.objects.get(catalog_number="12345")
        match = DiscogsMatch.objects.get(recording=recording)
        self.assertEqual(match.discogs_release_id, 98765)
        self.assertEqual(match.status, DiscogsMatch.MANUAL)
        self.assertIsNotNone(match.linked_at)
        # Detail deliberately NOT fetched -- that needs the network.
        self.assertTrue(match.needs_detail)

    def test_bare_id_and_master_url_both_work(self):
        self._create(discogs_ref="4242")
        self.assertEqual(DiscogsMatch.objects.get(recording__catalog_number="12345").discogs_release_id, 4242)

        self._create(discogs_ref="https://www.discogs.com/master/777", new_catalog_0="99999")
        other = DiscogsMatch.objects.get(recording__catalog_number="99999")
        self.assertEqual(other.discogs_master_id, 777)
        self.assertIsNone(other.discogs_release_id)

    def test_leaving_it_blank_creates_no_match_row(self):
        self._create(discogs_ref="")
        recording = Recording.objects.get(catalog_number="12345")
        self.assertFalse(DiscogsMatch.objects.filter(recording=recording).exists())

    def test_unparseable_reference_still_creates_the_recording(self):
        # The chart data is what matters; a bad optional field must never
        # block it.
        response = self._create(discogs_ref="not a url")
        recording = Recording.objects.get(catalog_number="12345")
        self.assertFalse(DiscogsMatch.objects.filter(recording=recording).exists())
        self.assertContains(response, "without a Discogs link")

    def test_picking_an_existing_recording_from_the_duplicate_check_keeps_the_typed_link(self):
        # Regression: the duplicate-check "pick one" path went through
        # entry_select, which never read the create box's Discogs field,
        # so a typed ID was silently discarded.
        existing = Recording.objects.create(
            label=Label.objects.create(name="Chess"), catalog_number="1441", side="A",
            song=Song.objects.create(title="Louisiana Blues"), credit_text="Muddy Waters")
        with mock.patch.object(discogs_api, "_request",
                               side_effect=AssertionError("entry form must never call the API")):
            response = self.client.post(reverse("charts:entry_select", args=[self.chart.id]), {
                "row": "0", "week": "1950-09-02", "recording_id": existing.id,
                "new_discogs_0": "https://www.discogs.com/release/14001059",
            })
        self.assertEqual(response.status_code, 200)
        match = DiscogsMatch.objects.get(recording=existing)
        self.assertEqual(match.discogs_release_id, 14001059)
        self.assertEqual(match.status, DiscogsMatch.MANUAL)

    def test_plain_search_pick_links_nothing(self):
        # An ordinary search pick carries no Discogs field at all.
        existing = Recording.objects.create(
            label=Label.objects.create(name="Chess"), catalog_number="1441", side="A",
            song=Song.objects.create(title="Louisiana Blues"), credit_text="Muddy Waters")
        self.client.post(reverse("charts:entry_select", args=[self.chart.id]), {
            "row": "0", "week": "1950-09-02", "recording_id": existing.id})
        self.assertFalse(DiscogsMatch.objects.filter(
            recording=existing, status=DiscogsMatch.MANUAL).exists())

    def test_does_not_overwrite_an_existing_link(self):
        # get_or_create can hand back a recording someone already linked;
        # replacing that silently would overwrite a human's decision.
        existing = Recording.objects.create(
            label=Label.objects.create(name="Decca"), catalog_number="12345", side="A",
            song=Song.objects.create(title="A Brand New Song"), credit_text="A New Artist",
        )
        DiscogsMatch.objects.create(
            recording=existing, status=DiscogsMatch.CONFIRMED,
            discogs_release_id=111, linked_at=timezone.now(),
        )

        response = self._create(discogs_ref="999")

        existing.discogs.refresh_from_db()
        self.assertEqual(existing.discogs.discogs_release_id, 111)   # untouched
        self.assertEqual(existing.discogs.status, DiscogsMatch.CONFIRMED)
        self.assertContains(response, "Already linked to Discogs")


class DiscogsAgreementTests(TestCase):
    """
    catalog_numbers_agree / labels_agree / loose_text_match -- the
    "do these two refer to the same thing" rules shared by match scoring
    and the audit view. Every case below came from a real disagreement
    the audit flagged against the actual database; they're here so the
    tuning doesn't silently regress.
    """

    def test_catalog_numbers_agree_across_cataloguing_conventions(self):
        for ours, theirs in [
            ("20-2806", "20 2806"),      # punctuation
            ("10879", "B-10879"),        # Discogs label-prefix letter
            ("6627", "06627"),           # leading-zero padding
            ("603", "K-603"),            # prefix letter again
            ("500", "500 CG1"),          # trailing matrix suffix
            ("11547", "B-11547"),
        ]:
            self.assertTrue(
                discogs_api.catalog_numbers_agree(ours, theirs),
                msg=f"{ours!r} should agree with {theirs!r}",
            )

    def test_genuinely_different_catalog_numbers_disagree(self):
        # The whole point -- these must still be caught.
        self.assertFalse(discogs_api.catalog_numbers_agree("2966", "AMO 2963"))
        self.assertFalse(discogs_api.catalog_numbers_agree("8638", "B-9042"))
        self.assertFalse(discogs_api.catalog_numbers_agree("75126", "64021"))

    def test_labels_agree_across_naming_conventions(self):
        for ours, theirs in [
            ("Capitol", "Capitol Records"),
            ("Cadet", "Cadet (2)"),              # Discogs same-name disambiguator
            ("Bluebird", "Bluebird (3)"),
            ("Philo", "Philo Recordings"),
            ("Victor", "RCA Victor"),            # parent/successor name
            ("Hit", "The Hit Record"),
            ("De Luxe", "DeLuxe (2)"),           # spacing
        ]:
            self.assertTrue(
                discogs_api.labels_agree(ours, theirs),
                msg=f"{ours!r} should agree with {theirs!r}",
            )

    def test_genuinely_different_labels_disagree(self):
        self.assertFalse(discogs_api.labels_agree("Decca", "Bluebird"))
        self.assertFalse(discogs_api.labels_agree("Capitol", "Savoy"))

    def test_missing_values_are_not_treated_as_disagreement(self):
        # Nothing cached to compare against isn't evidence of a problem.
        self.assertTrue(discogs_api.catalog_numbers_agree("355", ""))
        self.assertTrue(discogs_api.labels_agree("Decca", ""))
        self.assertTrue(discogs_api.loose_text_match("Some Song", ""))

    def test_loose_text_match_tolerates_expanded_and_reordered_credits(self):
        for ours, theirs in [
            ("Louis Jordan", "Louis Jordan And His Tympany Five"),
            ("Blue Light Boogie", "Blue Light Boogie (Part I & II)"),
            ("Louis Armstrong - Ella Fitzgerald", "Ella Fitzgerald, Louis Armstrong"),
            ("Andy Kirk - The Jubalaires", "Andy Kirk And His Orchestra, The Jubalaires"),
            ("White Cliffs of Dover", "The White Cliffs Of Dover"),
            ("Salt Pork, W. VA.", "Salt Pork, West Virginia"),
        ]:
            self.assertTrue(discogs_api.loose_text_match(ours, theirs), msg=f"{ours!r} vs {theirs!r}")

    def test_loose_text_match_still_catches_a_different_act_or_song(self):
        self.assertFalse(discogs_api.loose_text_match("Louis Jordan", "Jazz Gillum"))
        self.assertFalse(discogs_api.loose_text_match("Bycicle Bounce", "Jumpin' In A Julep Joint"))
        self.assertFalse(discogs_api.loose_text_match("Don't Get Around Much Anymore", "Never No Lament"))


class DiscogsConfidenceTests(TestCase):
    """The scoring that decides what's safe to bulk-confirm."""

    def setUp(self):
        self.label = Label.objects.create(name="Capitol")
        self.song = Song.objects.create(title="Bouquet of Roses")
        self.recording = Recording.objects.create(
            label=self.label, catalog_number="20-2806", side="B",
            song=self.song, credit_text="Eddy Arnold",
        )

    def test_exact_when_label_and_catalog_both_match(self):
        result = {"catno": "20-2806", "label": ["Capitol"]}
        self.assertEqual(discogs_api._score_confidence(self.recording, result), DiscogsMatch.EXACT)

    def test_catalog_punctuation_is_normalized_away(self):
        # Discogs writes catalog numbers inconsistently; "20 2806",
        # "20-2806" and "202806" are the same number as far as
        # matching goes.
        for catno in ["20 2806", "202806", "20-2806", "20/2806"]:
            result = {"catno": catno, "label": ["Capitol"]}
            self.assertEqual(
                discogs_api._score_confidence(self.recording, result), DiscogsMatch.EXACT,
                msg=f"{catno!r} should normalize to match",
            )

    def test_label_records_suffix_is_normalized_away(self):
        # The magazine prints "Capitol"; Discogs often carries the fuller
        # "Capitol Records". Same label.
        result = {"catno": "20-2806", "label": ["Capitol Records"]}
        self.assertEqual(discogs_api._score_confidence(self.recording, result), DiscogsMatch.EXACT)

    def test_strong_when_only_catalog_matches(self):
        result = {"catno": "20-2806", "label": ["Some Other Label"]}
        self.assertEqual(discogs_api._score_confidence(self.recording, result), DiscogsMatch.STRONG)

    def test_weak_when_catalog_differs(self):
        result = {"catno": "99-9999", "label": ["Capitol"]}
        self.assertEqual(discogs_api._score_confidence(self.recording, result), DiscogsMatch.WEAK)

    def test_any_of_several_labels_matching_is_enough(self):
        # A Discogs release can list several labels (imprint, parent,
        # distributor) -- matching any one of them counts.
        result = {"catno": "20-2806", "label": ["Universal", "Capitol", "EMI"]}
        self.assertEqual(discogs_api._score_confidence(self.recording, result), DiscogsMatch.EXACT)

    # --- the text veto on an otherwise-EXACT catalog+label score -------
    # Labels reuse catalog numbers across series and decades, so
    # catalog+label alone is not proof. This is the real case that
    # motivated it: Capitol 253 is a Tex Ritter 78, Capitol H-253 is a
    # Bozo The Clown 10" LP. Scoring that EXACT let it through the
    # bulk-confirm button unattended.

    def test_unrelated_title_demotes_an_otherwise_exact_match(self):
        result = {
            "catno": "20-2806", "label": ["Capitol"],
            "title": "Bozo The Clown - Bozo's Circus Band",
        }
        self.assertEqual(discogs_api._score_confidence(self.recording, result), DiscogsMatch.STRONG)

    def test_matching_song_alone_keeps_it_exact(self):
        # Differently-billed act, right song -- still exact.
        result = {
            "catno": "20-2806", "label": ["Capitol"],
            "title": "Some Other Billing - Bouquet Of Roses / Texarkana Baby",
        }
        self.assertEqual(discogs_api._score_confidence(self.recording, result), DiscogsMatch.EXACT)

    def test_matching_artist_alone_keeps_it_exact(self):
        # Retitled recording, right artist -- still exact. Real pattern:
        # Duke Ellington's "Never No Lament" was later retitled "Don't
        # Get Around Much Anymore"; same record.
        result = {
            "catno": "20-2806", "label": ["Capitol"],
            "title": "Eddy Arnold - Some Alternate Title / Another Side",
        }
        self.assertEqual(discogs_api._score_confidence(self.recording, result), DiscogsMatch.EXACT)

    def test_missing_title_does_not_demote(self):
        # Nothing to judge on isn't evidence of a problem -- the veto
        # only fires on positive evidence of unrelatedness.
        result = {"catno": "20-2806", "label": ["Capitol"], "title": ""}
        self.assertEqual(discogs_api._score_confidence(self.recording, result), DiscogsMatch.EXACT)

    def test_veto_does_not_promote_a_weak_match(self):
        # The veto only ever demotes. A result whose catalog doesn't
        # match stays WEAK even if the title is a perfect textual match.
        result = {
            "catno": "99-9999", "label": ["Capitol"],
            "title": "Eddy Arnold - Bouquet Of Roses",
        }
        self.assertEqual(discogs_api._score_confidence(self.recording, result), DiscogsMatch.WEAK)


class DiscogsSearchTests(TestCase):
    """The two-tier search, and that it never picks a winner on its own."""

    def setUp(self):
        self.label = Label.objects.create(name="Specialty")
        self.song = Song.objects.create(title="Pink Champagne")
        self.recording = Recording.objects.create(
            label=self.label, catalog_number="355", side="A",
            song=self.song, credit_text="Joe Liggins",
        )

    def test_catalog_search_used_first_and_title_search_skipped_on_a_hit(self):
        payload = {"results": [{"id": 42, "title": "Joe Liggins - Pink Champagne",
                                "catno": "355", "label": ["Specialty"], "year": "1950"}]}
        with mock.patch.object(discogs_api, "_request", return_value=payload) as request:
            candidates = discogs_api.search_candidates(self.recording)

        # Exactly one call: the catalog+label search hit, so neither the
        # catalog-only retry nor the weak title/artist fallback ran.
        self.assertEqual(request.call_count, 1)
        _, params = request.call_args[0]
        self.assertEqual(params["catno"], "355")
        self.assertEqual(params["label"], "Specialty")
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["discogs_release_id"], 42)
        self.assertEqual(candidates[0]["confidence"], DiscogsMatch.EXACT)

    def test_falls_back_through_all_three_searches_when_nothing_matches(self):
        with mock.patch.object(discogs_api, "_request", return_value={"results": []}) as request:
            candidates = discogs_api.search_candidates(self.recording)

        self.assertEqual(candidates, [])
        # catalog+label, then catalog alone, then title+artist.
        self.assertEqual(request.call_count, 3)
        self.assertIn("release_title", request.call_args[0][1])

    def test_candidates_come_back_best_confidence_first(self):
        # Titles have to actually name the recording, or the text veto in
        # _score_confidence correctly refuses to call any of them EXACT.
        payload = {"results": [
            {"id": 1, "title": "Joe Liggins - Pink Champagne", "catno": "999", "label": ["Nope"]},
            {"id": 2, "title": "Joe Liggins - Pink Champagne", "catno": "355", "label": ["Specialty"]},
            {"id": 3, "title": "Joe Liggins - Pink Champagne", "catno": "355", "label": ["Nope"]},
        ]}
        with mock.patch.object(discogs_api, "_request", return_value=payload):
            candidates = discogs_api.search_candidates(self.recording)

        self.assertEqual(
            [c["confidence"] for c in candidates],
            [DiscogsMatch.EXACT, DiscogsMatch.STRONG, DiscogsMatch.WEAK],
        )

    def test_exact_ties_prefer_us_pressing_regardless_of_year(self):
        # Same catalog+label, two countries -- a real thing Discogs has
        # (a shared catalog series pressed in more than one country).
        # US must sort first even though it's the LATER of the two --
        # country beats year, since this whole database is US charts.
        payload = {"results": [
            {"id": 1, "title": "x", "catno": "355", "label": ["Specialty"], "country": "Canada", "year": "1948"},
            {"id": 2, "title": "x", "catno": "355", "label": ["Specialty"], "country": "US", "year": "1950"},
        ]}
        with mock.patch.object(discogs_api, "_request", return_value=payload):
            candidates = discogs_api.search_candidates(self.recording)
        self.assertEqual([c["discogs_release_id"] for c in candidates], [2, 1])

    def test_exact_ties_within_same_country_prefer_earliest_year(self):
        payload = {"results": [
            {"id": 1, "title": "x", "catno": "355", "label": ["Specialty"], "country": "US", "year": "1955"},
            {"id": 2, "title": "x", "catno": "355", "label": ["Specialty"], "country": "US", "year": "1950"},
        ]}
        with mock.patch.object(discogs_api, "_request", return_value=payload):
            candidates = discogs_api.search_candidates(self.recording)
        self.assertEqual([c["discogs_release_id"] for c in candidates], [2, 1])

    def test_unparseable_year_does_not_blow_up(self):
        # Discogs sends year as a string, sometimes empty, sometimes junk.
        payload = {"results": [{"id": 7, "title": "x", "catno": "355", "label": ["Specialty"], "year": ""}]}
        with mock.patch.object(discogs_api, "_request", return_value=payload):
            candidates = discogs_api.search_candidates(self.recording)
        self.assertIsNone(candidates[0]["year"])


class DiscogsDetailTests(TestCase):
    """Per-track credit extraction -- the reason a Recording (one SIDE) has
    to find its own track inside a Release (the whole disc)."""

    def setUp(self):
        self.label = Label.objects.create(name="Decca")
        self.song = Song.objects.create(title="Blue Christmas")
        self.recording = Recording.objects.create(
            label=self.label, catalog_number="46186", side="B",
            song=self.song, credit_text="Ernest Tubb",
        )

    def _release(self, **overrides):
        release = {
            "id": 100, "title": "Blue Christmas / White Christmas",
            "artists": [{"name": "Ernest Tubb"}],
            "labels": [{"name": "Decca", "catno": "46186"}],
            "formats": [{"name": "Shellac"}],
            "year": 1949, "country": "US",
            "genres": ["Folk, World, & Country"], "styles": ["Country"],
            "tracklist": [
                {"position": "A", "title": "White Christmas",
                 "extraartists": [{"name": "Irving Berlin", "role": "Written-By"}]},
                {"position": "B", "title": "Blue Christmas",
                 "extraartists": [{"name": "Billy Hayes", "role": "Written-By"},
                                  {"name": "Jay W. Johnson", "role": "Written-By"}]},
            ],
            "extraartists": [{"name": "Some Producer", "role": "Producer"}],
        }
        release.update(overrides)
        return release

    def test_credits_come_from_this_sides_track_not_the_other_side(self):
        with mock.patch.object(discogs_api, "_request", return_value=self._release()):
            detail = discogs_api.fetch_release_detail(100, self.recording)

        # "Blue Christmas" is the B side -- must NOT pick up the A side's
        # Irving Berlin credit for "White Christmas".
        self.assertEqual(detail["songwriters"], "Billy Hayes, Jay W. Johnson")
        self.assertNotIn("Irving Berlin", detail["songwriters"])
        # And the track-level title is the SIDE's own, not the release's
        # combined "A / B" title.
        self.assertEqual(detail["matched_track_title"], "Blue Christmas")
        self.assertNotEqual(detail["matched_track_title"], detail["matched_title"])

    def test_track_artist_differs_from_release_artist_when_discogs_says_so(self):
        # The exact scenario this whole distinction exists for: a single
        # with a DIFFERENT act on each side. Discogs' release-level
        # credit is the union of both; only the track-level `artists`
        # (when present) says who's ACTUALLY on this one side.
        release = self._release(
            title="When The Lights Go On Again / That's All",
            artists=[{"name": "Lucky Millinder And His Orchestra"}, {"name": "Sister Rosetta Tharpe"}],
            tracklist=[
                {"position": "A", "title": "When The Lights Go On Again (All Over The World)",
                 "artists": [{"name": "Lucky Millinder And His Orchestra"}],
                 "extraartists": [{"name": "Bennie Benjamin", "role": "Written-By"},
                                  {"name": "Eddie Seiler", "role": "Written-By"},
                                  {"name": "Sol Marcus", "role": "Written-By"}]},
                {"position": "B", "title": "That's All",
                 "artists": [{"name": "Sister Rosetta Tharpe"}]},
            ],
        )
        recording = Recording.objects.create(
            label=self.label, catalog_number="18496", side="A",
            song=Song.objects.create(title="When The Lights Go On Again"), credit_text="Lucky Millinder And His Orchestra",
        )
        with mock.patch.object(discogs_api, "_request", return_value=release):
            detail = discogs_api.fetch_release_detail(100, recording)

        # Release-level artist includes Tharpe (she's on side B); the
        # TRACK-level artist for this specific side must not.
        self.assertIn("Sister Rosetta Tharpe", detail["matched_artist"])
        self.assertEqual(detail["matched_track_artist"], "Lucky Millinder And His Orchestra")
        self.assertNotIn("Sister Rosetta Tharpe", detail["matched_track_artist"])
        self.assertEqual(detail["matched_track_title"], "When The Lights Go On Again (All Over The World)")

    def test_track_artist_falls_back_to_release_artist_when_track_has_none(self):
        # Most tracks DON'T carry their own `artists` -- only Discogs
        # entries where a track's artist genuinely differs from the
        # release do. No override present should mean "same as release".
        release = self._release()  # tracks have no "artists" key at all
        with mock.patch.object(discogs_api, "_request", return_value=release):
            detail = discogs_api.fetch_release_detail(100, self.recording)
        self.assertEqual(detail["matched_track_artist"], detail["matched_artist"])

    def test_falls_back_to_release_level_credits_when_track_has_none(self):
        release = self._release(tracklist=[{"position": "B", "title": "Blue Christmas"}])
        release["extraartists"] = [{"name": "Release Writer", "role": "Written-By"}]
        with mock.patch.object(discogs_api, "_request", return_value=release):
            detail = discogs_api.fetch_release_detail(100, self.recording)
        self.assertEqual(detail["songwriters"], "Release Writer")

    def test_producer_falls_back_to_release_level(self):
        with mock.patch.object(discogs_api, "_request", return_value=self._release()):
            detail = discogs_api.fetch_release_detail(100, self.recording)
        self.assertEqual(detail["producers"], "Some Producer")

    def test_loose_title_match_handles_part_i_and_ii_style_differences(self):
        song = Song.objects.create(title="Blue Light Boogie")
        recording = Recording.objects.create(
            label=self.label, catalog_number="48111", side="A",
            song=song, credit_text="Louis Jordan",
        )
        release = self._release(tracklist=[
            {"position": "A", "title": "Blue Light Boogie (Part I & II)",
             "extraartists": [{"name": "Louis Jordan", "role": "Written-By"}]},
        ])
        with mock.patch.object(discogs_api, "_request", return_value=release):
            detail = discogs_api.fetch_release_detail(100, recording)
        self.assertEqual(detail["songwriters"], "Louis Jordan")

    def test_discogs_name_disambiguators_are_stripped(self):
        # Discogs appends "(2)", "(3)" to distinguish same-named artists.
        release = self._release(tracklist=[
            {"position": "B", "title": "Blue Christmas",
             "extraartists": [{"name": "Billy Hayes (2)", "role": "Written-By"}]},
        ])
        with mock.patch.object(discogs_api, "_request", return_value=release):
            detail = discogs_api.fetch_release_detail(100, self.recording)
        self.assertEqual(detail["songwriters"], "Billy Hayes")


class DiscogsReferenceMixinTests(TestCase):
    """reference_type/reference_id/discogs_url, shared by DiscogsMatch and
    DiscogsCandidate -- at most one of release/master id is ever set."""

    def setUp(self):
        self.recording = Recording.objects.create(
            label=Label.objects.create(name="Specialty"), catalog_number="355", side="A",
            song=Song.objects.create(title="Pink Champagne"), credit_text="Joe Liggins",
        )

    def test_release_takes_priority_and_reports_as_release(self):
        match = DiscogsMatch.objects.create(recording=self.recording, discogs_release_id=42, discogs_master_id=99)
        self.assertEqual(match.reference_type, "Release")
        self.assertEqual(match.reference_id, 42)
        self.assertEqual(match.discogs_url, "https://www.discogs.com/release/42")

    def test_master_only_reports_as_master(self):
        match = DiscogsMatch.objects.create(recording=self.recording, discogs_master_id=99)
        self.assertEqual(match.reference_type, "Master")
        self.assertEqual(match.reference_id, 99)
        self.assertEqual(match.discogs_url, "https://www.discogs.com/master/99")

    def test_neither_set_reports_empty(self):
        match = DiscogsMatch.objects.create(recording=self.recording)
        self.assertEqual(match.reference_type, "")
        self.assertIsNone(match.reference_id)
        self.assertEqual(match.discogs_url, "")

    def test_candidate_has_the_same_behavior(self):
        match = DiscogsMatch.objects.create(recording=self.recording)
        candidate = DiscogsCandidate.objects.create(match=match, discogs_master_id=99, title="x", rank=0)
        self.assertEqual(candidate.reference_type, "Master")
        self.assertEqual(candidate.reference_id, 99)


class DiscogsAuditTests(TestCase):
    """The linked-matches audit view."""

    def setUp(self):
        self.label = Label.objects.create(name="Coral")
        self.recording = Recording.objects.create(
            label=self.label, catalog_number="75126", side="A",
            song=Song.objects.create(title="Jealous Heart"), credit_text="Kenny Roberts",
        )
        self.match = DiscogsMatch.objects.create(
            recording=self.recording, status=DiscogsMatch.MANUAL,
            discogs_release_id=42, linked_at=timezone.now(),
            matched_track_title="Jealous Heart", matched_track_artist="Kenny Roberts",
            matched_label="Coral", matched_catalog_number="64021",   # deliberate mismatch
        )

    def test_lists_only_linked_matches(self):
        # An unlinked recording must not appear -- this page is about
        # auditing decisions already made.
        Recording.objects.create(
            label=self.label, catalog_number="999", side="A",
            song=Song.objects.create(title="Unlinked"), credit_text="Nobody",
        )
        response = self.client.get(reverse("charts:discogs_audit"))
        titles = [r["recording"].song.title for r in response.context["page"]]
        self.assertEqual(titles, ["Jealous Heart"])

    def test_flags_a_real_catalog_disagreement(self):
        response = self.client.get(reverse("charts:discogs_audit"))
        row = response.context["page"][0]
        self.assertTrue(row["has_mismatch"])
        self.assertFalse(row["flags"]["catalog"])
        self.assertTrue(row["flags"]["song"])
        self.assertTrue(row["flags"]["artist"])

    def test_mismatches_filter_narrows_to_problem_rows(self):
        clean = Recording.objects.create(
            label=self.label, catalog_number="500", side="A",
            song=Song.objects.create(title="Fine Song"), credit_text="Fine Artist",
        )
        DiscogsMatch.objects.create(
            recording=clean, status=DiscogsMatch.CONFIRMED, discogs_release_id=7,
            linked_at=timezone.now(),
            matched_track_title="Fine Song", matched_track_artist="Fine Artist",
            matched_label="Coral", matched_catalog_number="500",
        )
        all_rows = self.client.get(reverse("charts:discogs_audit"))
        self.assertEqual(len(all_rows.context["page"]), 2)

        only_bad = self.client.get(reverse("charts:discogs_audit"), {"mismatches": "1"})
        self.assertEqual(len(only_bad.context["page"]), 1)
        self.assertEqual(only_bad.context["page"][0]["recording"].song.title, "Jealous Heart")

    def test_sorted_newest_link_first_by_default(self):
        older = Recording.objects.create(
            label=self.label, catalog_number="111", side="A",
            song=Song.objects.create(title="Older Link"), credit_text="X",
        )
        DiscogsMatch.objects.create(
            recording=older, status=DiscogsMatch.CONFIRMED, discogs_release_id=8,
            linked_at=timezone.now() - timedelta(days=1),
        )
        response = self.client.get(reverse("charts:discogs_audit"))
        titles = [r["recording"].song.title for r in response.context["page"]]
        self.assertEqual(titles[0], "Jealous Heart")  # linked just now, so first

    def test_falls_back_to_release_level_values_when_detail_not_fetched(self):
        # A bulk-confirmed match has no per-track detail (that step is
        # skipped on purpose), which used to render as blank Artist/Song
        # cells. It should show the release-level values it DOES have,
        # flagged as not-track-level so they read honestly.
        bulk = Recording.objects.create(
            label=self.label, catalog_number="8633", side="A",
            song=Song.objects.create(title="See See Rider Blues"), credit_text="Bea Booze",
        )
        match = DiscogsMatch.objects.create(
            recording=bulk, status=DiscogsMatch.CONFIRMED, discogs_release_id=99,
            linked_at=timezone.now(),
            matched_title="Bea Booze - See See Rider Blues / Catchin' As Catch Can",
            matched_artist="Bea Booze",
            # matched_track_* deliberately blank -- never detail-fetched
        )
        self.assertFalse(match.has_track_detail)
        self.assertEqual(match.display_artist, "Bea Booze")
        self.assertIn("See See Rider Blues", match.display_title)

        response = self.client.get(reverse("charts:discogs_audit"))
        self.assertContains(response, "Bea Booze")
        self.assertContains(response, "release-level")   # the visual marker
        self.assertContains(response, "no per-track detail yet")  # the prompt

    def test_track_level_values_win_over_release_level_when_present(self):
        self.match.matched_track_title = "Jealous Heart"
        self.match.matched_track_artist = "Kenny Roberts"
        self.match.matched_title = "SOMETHING / ELSE"
        self.match.matched_artist = "Combined Credit"
        self.match.save()
        self.assertTrue(self.match.has_track_detail)
        self.assertEqual(self.match.display_title, "Jealous Heart")
        self.assertEqual(self.match.display_artist, "Kenny Roberts")

    def test_release_level_fallback_is_still_compared_not_skipped(self):
        # Blank compares as "no disagreement", so falling back to blank
        # would quietly under-report problems. A bulk-confirmed match
        # whose release title has nothing to do with our song must still
        # be flagged.
        wrong = Recording.objects.create(
            label=self.label, catalog_number="8633", side="A",
            song=Song.objects.create(title="Totally Different Song"), credit_text="Someone Else",
        )
        DiscogsMatch.objects.create(
            recording=wrong, status=DiscogsMatch.CONFIRMED, discogs_release_id=99,
            linked_at=timezone.now(),
            matched_title="Bea Booze - See See Rider Blues / Catchin' As Catch Can",
            matched_artist="Bea Booze",
        )
        response = self.client.get(reverse("charts:discogs_audit"), {"mismatches": "1"})
        flagged = [r["recording"].song.title for r in response.context["page"]]
        self.assertIn("Totally Different Song", flagged)

    def test_fix_link_targets_a_filter_that_actually_shows_the_row(self):
        # Regression: the 'fix' link used to point at the queue WITHOUT
        # status=all. Every row here is confirmed/manual, which the
        # queue's default 'todo' filter excludes -- so the link landed on
        # an empty page and read as doing nothing.
        audit = self.client.get(reverse("charts:discogs_audit"))
        self.assertContains(audit, "status=all&amp;q=75126")

        # And following it really does show the row.
        queue = self.client.get(reverse("charts:discogs_queue"), {"status": "all", "q": "75126"})
        self.assertContains(queue, f'id="discogs-row-{self.recording.id}"')


class DiscogsPendingDetailTests(TestCase):
    """
    Linked-but-not-enriched rows -- in practice, Discogs IDs typed into
    the entry form, which never goes online. They used to count as
    "settled" and so vanished from the queue without ever being enriched.
    """

    def setUp(self):
        self.label = Label.objects.create(name="Premium")

        def rec(cat, title):
            return Recording.objects.create(
                label=self.label, catalog_number=cat, side="B",
                song=Song.objects.create(title=title), credit_text="Memphis Slim")

        self.pending = DiscogsMatch.objects.create(
            recording=rec("867", "Mother Earth"), status=DiscogsMatch.MANUAL,
            discogs_release_id=5435918, linked_at=timezone.now())
        self.done = DiscogsMatch.objects.create(
            recording=rec("868", "Already Enriched"), status=DiscogsMatch.MANUAL,
            discogs_release_id=1, raw_data={"id": 1}, linked_at=timezone.now())
        self.master_only = DiscogsMatch.objects.create(
            recording=rec("869", "Master Only"), status=DiscogsMatch.MANUAL,
            discogs_master_id=2, linked_at=timezone.now())

    def test_needs_detail_q_agrees_with_the_property(self):
        # Two definitions of one idea only stay correct if they're checked
        # against each other.
        from_query = set(DiscogsMatch.objects.filter(DiscogsMatch.needs_detail_q()))
        from_property = {m for m in DiscogsMatch.objects.all() if m.needs_detail}
        self.assertEqual(from_query, from_property)
        self.assertEqual(from_query, {self.pending})

    def test_pending_rows_show_under_needs_attention_and_come_first(self):
        response = self.client.get(reverse("charts:discogs_queue"))
        shown = [m.id for m in response.context["page"]]
        self.assertEqual(shown[0], self.pending.id)
        self.assertNotIn(self.done.id, shown)          # finished -- stays out
        self.assertNotIn(self.master_only.id, shown)   # can never be fetched

    def test_pending_rows_are_not_counted_as_settled(self):
        progress = discogs_workflow.progress()
        self.assertEqual(progress["needs_detail"], 1)
        self.assertEqual(progress["settled"], 2)   # done + master_only, not pending

    def test_pending_row_renders_a_self_triggering_fetch(self):
        response = self.client.get(reverse("charts:discogs_queue"))
        self.assertContains(response, 'hx-trigger="load"', count=1)

    def test_a_failed_fetch_renders_retry_not_another_auto_trigger(self):
        # Otherwise a dead connection or missing token would re-fire the
        # load trigger on every swap, forever.
        with mock.patch.object(discogs_api, "fetch_release_detail",
                               side_effect=discogs_api.DiscogsError("no internet")):
            response = self.client.post(
                reverse("charts:discogs_fetch_detail", args=[self.pending.recording_id]))
        self.assertContains(response, "no internet")
        self.assertContains(response, "Retry fetching details")
        self.assertNotContains(response, 'hx-trigger="load"')

    def test_bulk_fetch_skips_master_only_rows_instead_of_stalling_on_them(self):
        with mock.patch.object(discogs_api, "fetch_release_detail",
                               return_value=_detail(discogs_release_id=5435918)):
            self.client.post(reverse("charts:discogs_bulk"),
                             {"action": "fetch_details", "querystring": ""})
        self.pending.refresh_from_db()
        self.assertFalse(self.pending.needs_detail)


class FakeDiscogsClient:
    """A stand-in for charts.discogs at discogs_workflow's seam: canned
    answers, a record of what was asked, and no network. `error` makes every
    call fail the way a dead connection or a rate limit does."""

    def __init__(self, candidates=(), detail=None, error=None):
        self.candidates = list(candidates)
        self.detail = detail
        self.error = error
        self.calls = []

    def search_candidates(self, recording):
        self.calls.append(("search", recording.id))
        if self.error:
            raise discogs_api.DiscogsError(self.error)
        return self.candidates

    def fetch_release_detail(self, release_id, recording):
        self.calls.append(("detail", release_id))
        if self.error:
            raise discogs_api.DiscogsError(self.error)
        return dict(self.detail)


def _detail(**overrides):
    # What charts.discogs.fetch_release_detail returns, for Pink Champagne.
    detail = {
        "discogs_release_id": 42, "discogs_master_id": None,
        "matched_title": "Pink Champagne", "matched_artist": "Joe Liggins",
        "matched_track_title": "Pink Champagne", "matched_track_artist": "Joe Liggins",
        "matched_label": "Specialty", "matched_catalog_number": "355", "matched_year": 1950,
        "matched_country": "US", "matched_format": "Shellac", "genres": ["Blues"],
        "styles": ["Jump Blues"], "songwriters": "Joe Liggins", "producers": "",
        "raw_data": {"id": 42},
    }
    detail.update(overrides)
    return detail


class DiscogsWorkflowTests(TestCase):
    """Every DiscogsMatch step (charts/discogs_workflow.py), through a fake
    client -- no request, no network, no patching of discogs.py's internals."""

    def setUp(self):
        self.label = Label.objects.create(name="Specialty")
        self.recording = Recording.objects.create(
            label=self.label, catalog_number="355", side="A",
            song=Song.objects.create(title="Pink Champagne"), credit_text="Joe Liggins",
        )
        self.match = DiscogsMatch.objects.create(recording=self.recording)

    def _candidate(self, **overrides):
        data = {
            "match": self.match, "discogs_release_id": 42, "title": "Pink Champagne",
            "label": "Specialty", "catalog_number": "355", "year": 1950,
            "confidence": DiscogsMatch.EXACT, "rank": 0,
        }
        data.update(overrides)
        return DiscogsCandidate.objects.create(**data)

    def _suggested(self):
        self.match.status = DiscogsMatch.SUGGESTED
        self.match.save()

    # ---- setup

    def test_every_recording_gets_a_match_row(self):
        # A recording created later (e.g. a debut typed into the entry
        # form) gets picked up automatically -- no signal, no extra step.
        new = Recording.objects.create(
            label=self.label, catalog_number="999", side="A",
            song=Song.objects.create(title="Later Addition"), credit_text="Someone",
        )
        self.assertEqual(discogs_workflow.ensure_matches(), 1)
        self.assertTrue(DiscogsMatch.objects.filter(recording=new).exists())
        self.assertEqual(discogs_workflow.ensure_matches(), 0)

    # ---- searching

    def test_search_stores_a_shortlist_without_confirming_anything(self):
        client = FakeDiscogsClient(candidates=[{"discogs_release_id": 42, "title": "Pink Champagne",
                                                "confidence": DiscogsMatch.EXACT}])
        discogs_workflow.search(self.match, client)
        self.match.refresh_from_db()
        # SUGGESTED, never CONFIRMED -- picking is always a human action.
        self.assertEqual(self.match.status, DiscogsMatch.SUGGESTED)
        self.assertEqual(self.match.candidates.count(), 1)
        self.assertIsNone(self.match.discogs_release_id)

    def test_an_empty_search_marks_no_match_rather_than_leaving_it_pending(self):
        discogs_workflow.search(self.match, FakeDiscogsClient(candidates=[]))
        self.match.refresh_from_db()
        self.assertEqual(self.match.status, DiscogsMatch.NO_MATCH)

    def test_a_failed_search_raises_and_changes_nothing(self):
        with self.assertRaises(discogs_api.DiscogsError):
            discogs_workflow.search(self.match, FakeDiscogsClient(error="no internet"))
        self.match.refresh_from_db()
        self.assertEqual(self.match.status, DiscogsMatch.UNMATCHED)

    def test_search_batch_stops_at_the_first_failure(self):
        other = DiscogsMatch.objects.create(recording=Recording.objects.create(
            label=self.label, catalog_number="356", side="A",
            song=Song.objects.create(title="Other"), credit_text="Someone"))
        client = FakeDiscogsClient(error="rate limited")
        self.assertEqual(discogs_workflow.search_batch([self.match, other], client), (0, "rate limited"))
        self.assertEqual(len(client.calls), 1)   # didn't hammer the API for the second one

    # ---- confirming

    def test_confirm_copies_the_candidate_and_fetches_full_details(self):
        # One click finishes the job: confirm, then exactly one detail call.
        self._suggested()
        candidate = self._candidate()
        client = FakeDiscogsClient(detail=_detail())
        self.assertIsNone(discogs_workflow.confirm(self.match, candidate, client))
        self.assertEqual(client.calls, [("detail", 42)])

        self.match.refresh_from_db()
        self.assertEqual(self.match.status, DiscogsMatch.CONFIRMED)
        self.assertEqual(self.match.discogs_release_id, 42)
        self.assertEqual(self.match.matched_catalog_number, "355")
        self.assertEqual(self.match.candidates.count(), 0)  # shortlist cleared
        self.assertEqual(self.match.songwriters, "Joe Liggins")
        self.assertFalse(self.match.needs_detail)

    def test_a_confirm_stands_even_if_the_detail_fetch_fails(self):
        # A rate limit on the SECOND call must not undo the decision --
        # only the detail fetch is left pending, retryable.
        self._suggested()
        error = discogs_workflow.confirm(self.match, self._candidate(), FakeDiscogsClient(error="rate limited"))
        self.assertEqual(error, "rate limited")
        self.match.refresh_from_db()
        self.assertEqual(self.match.status, DiscogsMatch.CONFIRMED)
        self.assertEqual(self.match.discogs_release_id, 42)
        self.assertTrue(self.match.needs_detail)

    def test_sole_exact_candidate_refuses_a_tie(self):
        self.assertIsNone(discogs_workflow.sole_exact_candidate(self.match))
        first = self._candidate(discogs_release_id=1, rank=0)
        self._candidate(discogs_release_id=3, confidence=DiscogsMatch.STRONG, rank=1)
        self.assertEqual(discogs_workflow.sole_exact_candidate(self.match), first)
        self._candidate(discogs_release_id=2, rank=2)
        self.assertIsNone(discogs_workflow.sole_exact_candidate(self.match))
        self.assertTrue(self.match.has_ambiguous_exact_tie)

    def test_bulk_confirm_only_takes_a_single_exact_candidate(self):
        self._suggested()
        self._candidate(confidence=DiscogsMatch.EXACT)
        # A second recording whose only candidate is a WEAK guess, and a
        # third with a genuine tie between two EXACT ones: both left alone.
        def suggested_match(cat):
            return DiscogsMatch.objects.create(status=DiscogsMatch.SUGGESTED, recording=Recording.objects.create(
                label=self.label, catalog_number=cat, side="A",
                song=Song.objects.create(title=f"Song {cat}"), credit_text="Someone"))
        weak, tied = suggested_match("777"), suggested_match("778")
        DiscogsCandidate.objects.create(match=weak, discogs_release_id=99, title="A guess",
                                        confidence=DiscogsMatch.WEAK, rank=0)
        for rank, rid in enumerate((1, 2)):
            DiscogsCandidate.objects.create(match=tied, discogs_release_id=rid, title="Tie",
                                            confidence=DiscogsMatch.EXACT, rank=rank)

        result = discogs_workflow.confirm_exact_batch(
            DiscogsMatch.objects.filter(status=DiscogsMatch.SUGGESTED).prefetch_related("candidates"),
            FakeDiscogsClient(detail=_detail()),
        )
        self.assertEqual(result, {"confirmed": 1, "enriched": 1, "skipped_ties": 1, "detail_error": None})
        for m in (self.match, weak, tied):
            m.refresh_from_db()
        self.assertEqual(self.match.status, DiscogsMatch.CONFIRMED)
        self.assertFalse(self.match.needs_detail)   # enriched in the same pass
        self.assertEqual(weak.status, DiscogsMatch.SUGGESTED)
        self.assertEqual(tied.status, DiscogsMatch.SUGGESTED)
        self.assertEqual(tied.candidates.count(), 2)   # shortlist NOT cleared

    def test_bulk_confirm_keeps_confirming_but_stops_fetching_after_a_failure(self):
        self._suggested()
        self._candidate()
        second = DiscogsMatch.objects.create(status=DiscogsMatch.SUGGESTED, recording=Recording.objects.create(
            label=self.label, catalog_number="356", side="A",
            song=Song.objects.create(title="Second"), credit_text="Someone"))
        DiscogsCandidate.objects.create(match=second, discogs_release_id=43, title="Second",
                                        confidence=DiscogsMatch.EXACT, rank=0)
        client = FakeDiscogsClient(error="rate limited")
        result = discogs_workflow.confirm_exact_batch(
            DiscogsMatch.objects.filter(status=DiscogsMatch.SUGGESTED).prefetch_related("candidates"), client)
        self.assertEqual((result["confirmed"], result["enriched"]), (2, 0))
        self.assertEqual(result["detail_error"], "rate limited")
        self.assertEqual(len(client.calls), 1)   # one failed fetch, then no more attempts
        second.refresh_from_db()
        self.assertEqual(second.status, DiscogsMatch.CONFIRMED)   # decisions stand
        self.assertTrue(second.needs_detail)                      # recoverable

    # ---- manual links

    def test_parse_reference_reads_urls_paths_and_bare_ids(self):
        for reference in ["12345", "https://www.discogs.com/release/12345", "release/12345-Some-Slug"]:
            self.assertEqual(discogs_workflow.parse_reference(reference), (12345, None, None), reference)
        # A master URL must not be stored as a release with the same number.
        for reference in ["https://www.discogs.com/master/12345", "master/12345-Some-Slug"]:
            self.assertEqual(discogs_workflow.parse_reference(reference), (None, 12345, None), reference)
        release_id, master_id, error = discogs_workflow.parse_reference("not a url or id")
        self.assertEqual((release_id, master_id), (None, None))
        self.assertIn("Couldn't read", error)

    def test_link_manually_then_enrich_a_release(self):
        self.assertIsNone(discogs_workflow.link_manually(self.match, 42, None, overwrite=True))
        self.assertEqual(self.match.status, DiscogsMatch.MANUAL)
        self.assertTrue(self.match.needs_detail)
        self.assertIsNone(discogs_workflow.enrich(self.match, FakeDiscogsClient(detail=_detail())))
        self.assertEqual(self.match.songwriters, "Joe Liggins")

    def test_a_master_only_link_never_attempts_a_detail_fetch(self):
        # No release id -- nothing for /releases/{id} to fetch, so enrich
        # must not even try (a doomed call would only show an error).
        discogs_workflow.link_manually(self.match, None, 999, overwrite=True)
        client = FakeDiscogsClient(error="must not be called")
        self.assertIsNone(discogs_workflow.enrich(self.match, client))
        self.assertEqual(client.calls, [])
        self.assertEqual(self.match.reference_type, "Master")

    def test_without_overwrite_an_existing_link_is_left_alone(self):
        # The entry form's path: a link someone decided on is never
        # silently replaced, and the note says so.
        discogs_workflow.link_manually(self.match, 42, None, overwrite=True)
        note = discogs_workflow.link_manually(self.match, 77, None, overwrite=False)
        self.assertIn("Already linked to Discogs Release 42", note)
        self.match.refresh_from_db()
        self.assertEqual(self.match.discogs_release_id, 42)

    def _enriched(self):
        # Linked to release 42 and enriched: songwriters, genres and all.
        self._candidate()
        discogs_workflow.confirm(self.match, self.match.candidates.get(), FakeDiscogsClient(detail=_detail()))
        self.assertEqual(self.match.songwriters, "Joe Liggins")

    def _assert_nothing_left_from_release_42(self):
        self.match.refresh_from_db()
        self.assertEqual(self.match.songwriters, "")
        self.assertEqual(self.match.genres, [])
        self.assertEqual(self.match.styles, [])
        self.assertEqual(self.match.matched_track_title, "")
        self.assertIsNone(self.match.raw_data)

    def test_a_relink_whose_fetch_fails_keeps_no_details_from_the_old_release(self):
        # Pasting release 77 over 42, then the fetch fails: the row must
        # not show 42's songwriters next to 77's id.
        self._enriched()
        discogs_workflow.link_manually(self.match, 77, None, overwrite=True)
        self.assertEqual(discogs_workflow.enrich(self.match, FakeDiscogsClient(error="offline")), "offline")
        self._assert_nothing_left_from_release_42()
        self.assertEqual(self.match.matched_title, "")
        self.assertEqual(self.match.discogs_release_id, 77)
        self.assertTrue(self.match.needs_detail)

    def test_relinking_to_a_master_keeps_no_details_from_the_old_release(self):
        # A master can't be fetched at all, so nothing would ever replace them.
        self._enriched()
        discogs_workflow.link_manually(self.match, None, 999, overwrite=True)
        self._assert_nothing_left_from_release_42()

    def test_confirming_a_new_candidate_on_a_linked_row_clears_the_old_details(self):
        # Searched again on a linked row, then picked a different release:
        # the old details must go -- before, raw_data survived, so the row
        # looked enriched and the new release's details were never fetched.
        self._enriched()
        DiscogsCandidate.objects.create(match=self.match, discogs_release_id=77, title="Other pressing",
                                        confidence=DiscogsMatch.STRONG, rank=0)
        client = FakeDiscogsClient(detail=_detail(discogs_release_id=77, songwriters="Someone Else"))
        discogs_workflow.confirm(self.match, self.match.candidates.get(), client)
        self.assertEqual(client.calls, [("detail", 77)])
        self.match.refresh_from_db()
        self.assertEqual(self.match.songwriters, "Someone Else")

    def test_re_entering_the_same_link_changes_nothing(self):
        # Typing the id a row is already enriched for keeps its details.
        self._enriched()
        self.assertIsNone(discogs_workflow.link_manually(self.match, 42, None, overwrite=False))
        self.match.refresh_from_db()
        self.assertEqual(self.match.status, DiscogsMatch.CONFIRMED)
        self.assertEqual(self.match.songwriters, "Joe Liggins")

    # ---- status changes

    def test_reset_clears_the_link_and_everything_cached(self):
        self.match.status = DiscogsMatch.CONFIRMED
        self.match.discogs_release_id = 42
        self.match.songwriters = "Someone"
        self.match.matched_title = "Something"
        self.match.raw_data = {"id": 42}
        self.match.save()
        self._candidate()

        self.assertTrue(discogs_workflow.set_status(self.match, DiscogsMatch.UNMATCHED))
        self.match.refresh_from_db()
        self.assertEqual(self.match.status, DiscogsMatch.UNMATCHED)
        self.assertIsNone(self.match.discogs_release_id)
        self.assertEqual(self.match.songwriters, "")
        self.assertEqual(self.match.matched_title, "")
        self.assertIsNone(self.match.raw_data)
        self.assertEqual(self.match.candidates.count(), 0)

    def test_an_unknown_status_changes_nothing(self):
        self.assertFalse(discogs_workflow.set_status(self.match, "made-up"))
        self.match.refresh_from_db()
        self.assertEqual(self.match.status, DiscogsMatch.UNMATCHED)

    # ---- enriching

    def test_enrich_batch_skips_rows_with_nothing_to_fetch(self):
        discogs_workflow.link_manually(self.match, 42, None, overwrite=True)
        master_only = DiscogsMatch.objects.create(
            status=DiscogsMatch.MANUAL, discogs_master_id=2, recording=Recording.objects.create(
                label=self.label, catalog_number="357", side="A",
                song=Song.objects.create(title="Master Only"), credit_text="Someone"))
        client = FakeDiscogsClient(detail=_detail())
        self.assertEqual(discogs_workflow.enrich_batch([master_only, self.match], client), (1, None))
        self.assertEqual(client.calls, [("detail", 42)])


class FlipSidesOnDifferentReleasesTests(TestCase):
    # The audit's cross-row check: a release's sides linked to different
    # Discogs releases. Listed for review, never changed.

    def setUp(self):
        self.decca = Label.objects.create(name="Decca")

    def _side(self, catalog, side, release_id, status=DiscogsMatch.CONFIRMED):
        rec = Recording.objects.create(label=self.decca, catalog_number=catalog, side=side,
                                       song=Song.objects.create(title=f"{catalog}{side}"), credit_text="X")
        return DiscogsMatch.objects.create(recording=rec, status=status, discogs_release_id=release_id)

    def test_lists_only_releases_whose_sides_disagree(self):
        a, b = self._side("46173", "A", 4025819), self._side("46173", "B", 10113496)
        self._side("46200", "A", 7), self._side("46200", "B", 7)       # agree: not listed
        self._side("46300", "A", 8)                                    # one side linked: nothing to compare
        found = discogs_workflow.flip_sides_on_different_releases()
        self.assertEqual(len(found), 1)
        self.assertEqual((found[0]["label"], found[0]["catalog_number"]), ("Decca", "46173"))
        self.assertEqual(found[0]["matches"], [a, b])

    def test_unlinked_sides_are_not_compared(self):
        self._side("46173", "A", 4025819)
        self._side("46173", "B", 99, status=DiscogsMatch.REJECTED)
        self.assertEqual(discogs_workflow.flip_sides_on_different_releases(), [])

    def test_audit_page_lists_them(self):
        self._side("46173", "A", 4025819), self._side("46173", "B", 10113496)
        response = self.client.get(reverse("charts:discogs_audit"))
        self.assertContains(response, "flip-sides linked to different Discogs releases")
        self.assertContains(response, "release 10113496")


class DiscogsWorkflowPageSmokeTests(TestCase):
    # The /discogs/ buttons are wired to discogs_workflow. The real client
    # is the charts.discogs module, so its PUBLIC functions are patched
    # here -- never the network, never its private helpers.

    def setUp(self):
        self.recording = Recording.objects.create(
            label=Label.objects.create(name="Specialty"), catalog_number="355", side="A",
            song=Song.objects.create(title="Pink Champagne"), credit_text="Joe Liggins",
        )
        self.match = DiscogsMatch.objects.create(recording=self.recording)

    def test_a_failed_search_is_shown_on_the_row_not_raised(self):
        with mock.patch.object(discogs_api, "search_candidates",
                               side_effect=discogs_api.DiscogsError("no internet")):
            response = self.client.post(reverse("charts:discogs_search", args=[self.recording.id]))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "no internet")

    def test_manual_entry_rejects_garbage_with_a_message(self):
        response = self.client.post(reverse("charts:discogs_manual", args=[self.recording.id]),
                                    {"discogs_ref": "not a url or id"})
        self.assertContains(response, "Couldn&#x27;t read a release or master ID")

    def test_manual_entry_links_and_enriches(self):
        with mock.patch.object(discogs_api, "fetch_release_detail", return_value=_detail(discogs_release_id=12345)):
            self.client.post(reverse("charts:discogs_manual", args=[self.recording.id]),
                             {"discogs_ref": "https://www.discogs.com/release/12345"})
        self.match.refresh_from_db()
        self.assertEqual(self.match.status, DiscogsMatch.MANUAL)
        self.assertFalse(self.match.needs_detail)

    def test_bulk_confirm_reports_what_it_did(self):
        self.match.status = DiscogsMatch.SUGGESTED
        self.match.save()
        DiscogsCandidate.objects.create(match=self.match, discogs_release_id=42, title="Pink Champagne",
                                        confidence=DiscogsMatch.EXACT, rank=0)
        with mock.patch.object(discogs_api, "fetch_release_detail", return_value=_detail()):
            response = self.client.post(reverse("charts:discogs_bulk"), {
                "action": "confirm_exact", "match_id": [self.match.id], "querystring": "",
            }, follow=True)
        self.assertContains(response, "full details fetched for 1")

    def test_the_confirm_exact_count_excludes_ties(self):
        # The button's own number must agree with what the action does.
        self.match.status = DiscogsMatch.SUGGESTED
        self.match.save()
        for rank, rid in enumerate((1, 2)):
            DiscogsCandidate.objects.create(match=self.match, discogs_release_id=rid, title="Tie",
                                            confidence=DiscogsMatch.EXACT, rank=rank)
        response = self.client.get(reverse("charts:discogs_queue"), {"status": "suggested"})
        self.assertEqual(response.context["page_exact_ids"], [])

    def test_reset_button(self):
        self.client.post(reverse("charts:discogs_set_status", args=[self.recording.id]), {"status": "no_match"})
        self.match.refresh_from_db()
        self.assertEqual(self.match.status, DiscogsMatch.NO_MATCH)


class DiscogsTokenTests(TestCase):
    """Token loading, and that its absence degrades gracefully."""

    def test_env_var_wins(self):
        with mock.patch.dict("os.environ", {"DISCOGS_TOKEN": "from-env"}):
            self.assertEqual(discogs_api.get_token(), "from-env")

    def test_missing_token_returns_none_rather_than_raising(self):
        # The queue page uses this to show a setup prompt instead of
        # letting every button fail one at a time.
        with mock.patch.dict("os.environ", {"DISCOGS_TOKEN": ""}), \
             mock.patch("pathlib.Path.exists", return_value=False):
            self.assertIsNone(discogs_api.get_token())

    def test_request_without_a_token_raises_a_helpful_error(self):
        with mock.patch.object(discogs_api, "get_token", return_value=None):
            with self.assertRaises(discogs_api.DiscogsError) as caught:
                discogs_api._request("/database/search")
        self.assertIn("discogs.com/settings/developers", str(caught.exception))

    def test_queue_page_works_with_no_token_configured(self):
        with mock.patch.object(discogs_api, "get_token", return_value=None):
            response = self.client.get(reverse("charts:discogs_queue"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "No Discogs token configured")


# =============================================================================
# Dashboard (charts/dashboard.py) -- every test targets one of the four
# definitions in that module's docstring, i.e. a way the obvious query
# would quietly produce the wrong number.
# =============================================================================

class DashboardTests(TestCase):
    # Consecutive Saturdays -- every chart on file publishes on Saturday.
    W1, W2, W3, W4 = date(1950, 1, 7), date(1950, 1, 14), date(1950, 1, 21), date(1950, 1, 28)

    def setUp(self):
        self.jukebox = Chart.objects.create(category="country", method="jukebox", publication_weekday=5)
        self.retail = Chart.objects.create(category="country", method="retail", publication_weekday=5)
        self.rnb = Chart.objects.create(category="rnb", method="jukebox", publication_weekday=5)
        self.label = Label.objects.create(name="Decca")
        self.tubb = Artist.objects.create(canonical_name="Ernest Tubb")
        self.foley = Artist.objects.create(canonical_name="Red Foley")

    def _recording(self, title, catalog, *artists):
        rec = Recording.objects.create(
            label=self.label, catalog_number=catalog, side="A",
            song=Song.objects.create(title=title),
            credit_text=" - ".join(a.canonical_name for a in artists),
        )
        for order, artist in enumerate(artists, start=1):
            RecordingCredit.objects.create(recording=rec, artist=artist, credit_order=order)
        return rec

    def _entry(self, chart, week, position, rec):
        return ChartEntry.objects.create(chart=chart, week_date=week, position=position, recording=rec)

    def _build(self, **params):
        return dashboard_lib.build(params)

    def test_a_week_at_number_one_on_three_charts_counts_once(self):
        # Definition 1. Three #1 ROWS, one calendar week -- counting rows
        # (or chart-weeks) would claim 3 weeks at #1.
        rec = self._recording("Slipping Around", "100", self.tubb)
        for chart in (self.jukebox, self.retail, self.rnb):
            self._entry(chart, self.W1, 1, rec)
        self._entry(self.jukebox, self.W2, 1, rec)

        row = self._build()["number_ones"]["rows"][0]
        self.assertEqual(row["weeks_at_1"], 2)
        self.assertEqual(row["weeks_on"], 2)

    def test_a_tie_at_number_one_credits_both_records(self):
        # Definition 2: 39 real chart-weeks on file have a tie at #1.
        a = self._recording("A", "101", self.tubb)
        b = self._recording("B", "102", self.foley)
        self._entry(self.jukebox, self.W1, 1, a)
        self._entry(self.jukebox, self.W1, 1, b)

        rows = self._build()["number_ones"]["rows"]
        self.assertEqual([(r["song"], r["weeks_at_1"], r["rank"]) for r in rows], [("A", 1, 1), ("B", 1, 1)])

    def test_competition_ranking_and_the_tied_overflow_count(self):
        # "1, 2, 2, 4" -- and with Top 2, the third record tied at the cut
        # isn't listed but IS counted, so the cut can't mislead.
        lead = self._recording("Lead", "110", self.tubb)
        for i, week in enumerate((self.W1, self.W2, self.W3)):
            self._entry(self.jukebox, week, 1 + i, lead)
        tied = [self._recording(t, c, self.foley) for t, c in (("T1", "111"), ("T2", "112"), ("T3", "113"))]
        for rec in tied:
            self._entry(self.jukebox, self.W1, 5, rec)
            self._entry(self.jukebox, self.W2, 5, rec)
        last = self._recording("Last", "114", self.tubb)
        self._entry(self.jukebox, self.W4, 9, last)

        full = self._build(n="10")["top_recordings"]
        self.assertEqual([r["rank"] for r in full["rows"]], [1, 2, 2, 2, 5])
        cut = self._build(n="2")["top_recordings"]
        self.assertEqual(len(cut["rows"]), 2)
        self.assertEqual(cut["more_tied"], 2)
        self.assertEqual(cut["tied_value"], 2)

    def test_a_collaboration_credits_every_artist_on_it(self):
        # Definition 3: structured credits, not the printed text -- the
        # whole point of the /credits/ split work.
        duet = self._recording("Goodnight Irene", "120", self.tubb, self.foley)
        self._entry(self.jukebox, self.W1, 2, duet)

        names = {a["name"] for a in self._build()["top_artists"]["rows"]}
        self.assertEqual(names, {"Ernest Tubb", "Red Foley"})

    def test_artist_weeks_present_vs_record_weeks(self):
        # Two hits on at once: 2 calendar weeks present, but 4 record-weeks.
        # And a week a hit spends on two charts still counts once.
        hit1 = self._recording("Hit 1", "130", self.tubb)
        hit2 = self._recording("Hit 2", "131", self.tubb)
        for week in (self.W1, self.W2):
            self._entry(self.jukebox, week, 1, hit1)
            self._entry(self.retail, week, 1, hit1)
            self._entry(self.jukebox, week, 3, hit2)

        row = self._build()["top_artists"]["rows"][0]
        self.assertEqual((row["weeks_on"], row["recordings"], row["record_weeks"]), (2, 2, 4))

    def test_date_range_and_genre_scope_everything(self):
        rec = self._recording("Country", "140", self.tubb)
        rnb_rec = self._recording("R&B", "141", self.foley)
        for week in (self.W1, self.W2, self.W3):
            self._entry(self.jukebox, week, 1, rec)
        self._entry(self.rnb, self.W2, 1, rnb_rec)

        ctx = self._build(genre="country", **{"from": "1950-01-14", "to": "1950-01-21"})
        self.assertEqual([r["song"] for r in ctx["top_recordings"]["rows"]], ["Country"])
        self.assertEqual(ctx["top_recordings"]["rows"][0]["weeks_on"], 2)
        self.assertEqual(ctx["range_weeks"], 2)

    def test_dates_snap_onto_real_chart_weeks(self):
        # A hand-typed Wednesday must land on a real Saturday, or the
        # slider's week index would point at the wrong week.
        rec = self._recording("X", "150", self.tubb)
        for week in (self.W1, self.W2, self.W3):
            self._entry(self.jukebox, week, 1, rec)

        ctx = self._build(**{"from": "1950-01-11", "to": "1950-01-18"})
        self.assertEqual((ctx["filters"]["date_from"], ctx["filters"]["date_to"]), (self.W2, self.W2))
        # Backwards and out-of-range dates are corrected, not errors.
        ctx = self._build(**{"from": "1960-01-01", "to": "1900-01-01"})
        self.assertEqual((ctx["filters"]["date_from"], ctx["filters"]["date_to"]), (self.W1, self.W3))

    def test_top_n_is_clamped(self):
        weeks = [self.W1]
        self.assertEqual(dashboard_lib.parse_filters({"n": "0"}, weeks)["n"], 1)
        self.assertEqual(dashboard_lib.parse_filters({"n": "5000"}, weeks)["n"], dashboard_lib.MAX_TOP_N)
        self.assertEqual(dashboard_lib.parse_filters({"n": "abc"}, weeks)["n"], dashboard_lib.DEFAULT_TOP_N)

    def test_debuts_skip_the_charts_first_week_on_file(self):
        # Definition 4. "Old" was already charting in the jukebox chart's
        # first week on file -- its real debut is unknowable, so it isn't
        # one. "New" first appears a week later: a real debut. "Crossover"
        # moves from jukebox to retail -- its retail entry isn't a debut.
        old = self._recording("Old", "160", self.tubb)
        new = self._recording("New", "161", self.foley)
        crossover = self._recording("Crossover", "162", self.tubb)
        self._entry(self.jukebox, self.W1, 1, old)
        self._entry(self.jukebox, self.W2, 1, old)
        self._entry(self.jukebox, self.W2, 2, new)
        self._entry(self.jukebox, self.W2, 3, crossover)
        self._entry(self.retail, self.W3, 1, crossover)

        debuts = self._build()["debuts"]
        self.assertEqual(debuts["total"], 2)  # New + Crossover, each once
        self.assertEqual(debuts["unit"], "week")  # a 3-week window
        self.assertEqual([b["value"] for b in debuts["buckets"]], [0, 2, 0])

    def test_a_debut_before_the_window_is_not_a_debut_inside_it(self):
        rec = self._recording("Long Runner", "170", self.tubb)
        filler = self._recording("Filler", "171", self.foley)
        self._entry(self.jukebox, self.W1, 1, filler)  # the chart's first week
        for week in (self.W2, self.W3, self.W4):
            self._entry(self.jukebox, week, 2, rec)

        ctx = self._build(**{"from": "1950-01-21", "to": "1950-01-28"})
        self.assertEqual(ctx["debuts"]["total"], 0)

    def test_label_chart_counts_record_weeks(self):
        a = self._recording("A", "180", self.tubb)
        b = self._recording("B", "181", self.foley)
        for week in (self.W1, self.W2):
            self._entry(self.jukebox, week, 1, a)
            self._entry(self.retail, week, 1, a)  # same week, another chart: once
            self._entry(self.jukebox, week, 2, b)

        self.assertEqual(self._build()["labels"][0]["value"], 4)

    def test_axis_labels_thin_out_so_they_never_collide(self):
        label = dashboard_lib._month_axis_label
        # Eight years of months: years only. A window starting in October
        # must NOT also label its first slot, or "1942" prints on "1943".
        self.assertEqual(label(date(1942, 10, 1), 0, 102), "")
        self.assertEqual(label(date(1943, 1, 1), 3, 102), "1943")
        self.assertEqual(label(date(1943, 2, 1), 4, 102), "")
        # One year: every month gets named.
        self.assertEqual(label(date(1949, 1, 1), 0, 12), "Jan 1949")
        self.assertEqual(label(date(1949, 2, 1), 1, 12), "Feb")

    def test_page_and_htmx_partial(self):
        rec = self._recording("Page", "190", self.tubb)
        self._entry(self.jukebox, self.W1, 1, rec)

        page = self.client.get(reverse("charts:dashboard"))
        self.assertTemplateUsed(page, "charts/dashboard.html")
        live = self.client.get(reverse("charts:dashboard"), {"n": "5"}, HTTP_HX_REQUEST="true")
        self.assertTemplateUsed(live, "charts/_dashboard_results.html")
        self.assertTemplateNotUsed(live, "charts/dashboard.html")

    def test_empty_database_renders(self):
        response = self.client.get(reverse("charts:dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "No chart weeks on file yet")


# =============================================================================
# Chart week (charts/chart_week.py) -- the previous chart, last-week
# resolution, movement and standings, tested through ChartWeek itself. The
# previous chart is the previous PUBLICATION date, never "the previous week
# on file" (docs/adr/0001-previous-chart-is-previous-publication.md).
# =============================================================================

class ChartWeekTests(TestCase):
    W1, W2, W3, W4 = date(1951, 3, 17), date(1951, 3, 24), date(1951, 3, 31), date(1951, 4, 7)

    def setUp(self):
        self.chart = Chart.objects.create(category="country", method="jockeys", publication_weekday=5)
        self.label = Label.objects.create(name="Victor")
        self.recs = {}

    def _rec(self, title):
        if title not in self.recs:
            self.recs[title] = Recording.objects.create(
                label=self.label, catalog_number=str(100 + len(self.recs)), side="A",
                song=Song.objects.create(title=title), credit_text="Somebody",
            )
        return self.recs[title]

    def _entry(self, week, position, title, typed_lw=None, **kw):
        return ChartEntry.objects.create(
            chart=self.chart, week_date=week, position=position, recording=self._rec(title),
            printed_last_week_position=typed_lw, **kw,
        )

    def _moves(self, week):
        # {title: Movement} for every entry on `week`, straight from ChartWeek.
        cw = chart_week_lib.ChartWeek(self.chart, week)
        return {
            e.recording.song.title: cw.movement(e.recording_id, e.position, e.printed_last_week_position)
            for e in self.chart.entries.filter(week_date=week).select_related("recording__song")
        }

    # ---- the previous chart

    def test_previous_chart_is_one_publication_back(self):
        self.assertEqual(chart_week_lib.previous_chart_date(self.W4), self.W3)
        self.assertEqual(chart_week_lib.next_chart_date(self.W3), self.W4)

    def test_a_gap_is_not_bridged_to_an_older_week_on_file(self):
        # W2 not entered: the previous chart of W3 is W2 -- NOT W1, the most
        # recent week on file -- so "#1" resolves to nothing rather than to
        # W1's #1, a record two issues old.
        self._entry(self.W1, 1, "Two issues old")
        cw = chart_week_lib.ChartWeek(self.chart, self.W3)
        self.assertFalse(cw.previous_on_file)
        self.assertEqual(cw.resolve(1), [])

    # ---- last-week resolution

    def test_resolve_a_last_week_number(self):
        self._entry(self.W3, 1, "Top")
        self._entry(self.W3, 2, "Second")
        matches = chart_week_lib.ChartWeek(self.chart, self.W4).resolve(2)
        self.assertEqual([m.recording.song.title for m in matches], ["Second"])

    def test_resolve_a_tie_returns_every_record_at_that_position(self):
        # The caller asks which one -- never guesses.
        self._entry(self.W3, 3, "Tied first")
        self._entry(self.W3, 3, "Tied second")
        matches = chart_week_lib.ChartWeek(self.chart, self.W4).resolve(3)
        self.assertEqual([m.recording.song.title for m in matches], ["Tied first", "Tied second"])

    def test_resolve_a_position_nobody_held(self):
        self._entry(self.W3, 1, "Only one")
        self.assertEqual(chart_week_lib.ChartWeek(self.chart, self.W4).resolve(9), [])

    def test_last_week_position_for_a_pick_made_by_search(self):
        self._entry(self.W3, 6, "Carried over")
        cw = chart_week_lib.ChartWeek(self.chart, self.W4)
        self.assertEqual(cw.last_week_position(self._rec("Carried over").id), 6)
        self.assertIsNone(cw.last_week_position(self._rec("Not there").id))

    # ---- movement

    def test_carryover_without_a_typed_number_is_not_new(self):
        # The 2026-10-18 report: a week entered by search, no last-week
        # numbers typed, read as all NEW though the records were carryovers.
        self._entry(self.W3, 1, "Stays")
        self._entry(self.W3, 4, "Climbs")
        self._entry(self.W3, 2, "Falls")
        self._entry(self.W4, 1, "Stays")
        self._entry(self.W4, 2, "Climbs")
        self._entry(self.W4, 5, "Falls")

        moves = self._moves(self.W4)
        self.assertEqual(moves["Stays"].code, "same")
        self.assertEqual(moves["Climbs"].code, "up")
        self.assertEqual(moves["Falls"].code, "down")
        self.assertEqual(moves["Climbs"].last_on_file, 4)
        self.assertFalse(any(m.conflict for m in moves.values()))

    def test_new_versus_re_entry(self):
        self._entry(self.W1, 3, "Comes back")
        self._entry(self.W2, 1, "Filler")      # W2 on file, without "Comes back"
        self._entry(self.W3, 1, "Filler")
        self._entry(self.W3, 2, "Comes back")
        self._entry(self.W3, 3, "Brand new")

        moves = self._moves(self.W3)
        self.assertEqual(moves["Comes back"].code, "re")
        self.assertEqual(moves["Brand new"].code, "new")

    def test_a_typed_number_that_disagrees_is_flagged_and_kept(self):
        self._entry(self.W3, 6, "Mismatch")
        self._entry(self.W3, 1, "Phantom filler")
        self._entry(self.W4, 3, "Mismatch", typed_lw=7)   # data says 6
        self._entry(self.W4, 4, "Phantom", typed_lw=2)    # not on W3 at all
        self._entry(self.W4, 1, "Phantom filler", typed_lw=1)  # agrees

        moves = self._moves(self.W4)
        self.assertTrue(moves["Mismatch"].conflict)
        self.assertTrue(moves["Phantom"].conflict)
        self.assertFalse(moves["Phantom filler"].conflict)
        # Movement follows the data; the printed number itself is untouched.
        self.assertEqual(moves["Mismatch"].code, "up")
        self.assertEqual(ChartEntry.objects.get(week_date=self.W4, position=3).printed_last_week_position, 7)

    def test_previous_chart_not_on_file(self):
        # W2 missing (a gap): the typed number is the only evidence.
        self._entry(self.W1, 1, "Old")
        self._entry(self.W3, 2, "Typed", typed_lw=5)
        self._entry(self.W3, 4, "Untyped")

        moves = self._moves(self.W3)
        self.assertEqual(moves["Typed"].code, "up")
        self.assertEqual(moves["Untyped"].code, "unknown")
        self.assertFalse(moves["Typed"].conflict)  # nothing on file to disagree WITH

    def test_the_charts_first_week_on_file_is_unknown_not_new(self):
        self._entry(self.W1, 1, "Already there")
        self.assertEqual(self._moves(self.W1)["Already there"].code, "unknown")

    def test_a_record_listed_twice_last_week_uses_its_best_position(self):
        # The accepted Savoy 731 printing anomaly: one record, two rows, one week.
        self._entry(self.W3, 3, "Double", printed_anomaly_note="printed twice")
        self._entry(self.W3, 7, "Double", printed_anomaly_note="printed twice")
        self._entry(self.W4, 3, "Double", typed_lw=3)

        move = self._moves(self.W4)["Double"]
        self.assertEqual(move.last_on_file, 3)
        self.assertFalse(move.conflict)

    # ---- standings: weeks on chart, run, peak (all computed)

    def _standing(self, week, title):
        rec = self._rec(title)
        return chart_week_lib.ChartWeek(self.chart, week).standings({rec.id})[rec.id]

    def test_weeks_on_counts_every_week_but_run_restarts_on_a_re_entry(self):
        self._entry(self.W1, 2, "Runner")
        self._entry(self.W2, 5, "Gap filler")   # Runner off the chart in W2
        self._entry(self.W3, 4, "Runner")       # a re-entry
        self._entry(self.W4, 9, "Runner")
        self.assertEqual(tuple(self._standing(self.W4, "Runner")), (3, 2, 2))   # weeks_on, run, peak
        self.assertEqual(tuple(self._standing(self.W3, "Runner")), (2, 1, 2))

    def test_peak_is_the_best_position_so_far_not_the_final_best(self):
        self._entry(self.W1, 6, "Climber")
        self._entry(self.W2, 3, "Climber")
        self._entry(self.W3, 1, "Climber")
        self.assertEqual(self._standing(self.W2, "Climber").peak, 3)   # #1 hasn't happened yet
        self.assertEqual(self._standing(self.W3, "Climber").peak, 1)

    def test_a_debut_is_one_week_one_run(self):
        self._entry(self.W2, 7, "Debut")
        self.assertEqual(tuple(self._standing(self.W2, "Debut")), (1, 1, 7))

    def test_a_record_listed_twice_in_a_week_is_one_week(self):
        self._entry(self.W3, 3, "Double", printed_anomaly_note="printed twice")
        self._entry(self.W3, 7, "Double", printed_anomaly_note="printed twice")
        self.assertEqual(tuple(self._standing(self.W3, "Double")), (1, 1, 3))

    def test_other_charts_do_not_count(self):
        other = Chart.objects.create(category="rnb", method="jukebox", publication_weekday=5)
        ChartEntry.objects.create(chart=other, week_date=self.W1, position=1, recording=self._rec("Crossover"))
        self._entry(self.W2, 4, "Crossover")
        self.assertEqual(tuple(self._standing(self.W2, "Crossover")), (1, 1, 4))

    # ---- the shared rules the last-week audit also uses

    def test_shared_rules(self):
        self.assertEqual(chart_week_lib.best_positions([(1, 7), (2, 4), (1, 3)]), {1: 3, 2: 4})
        self.assertTrue(chart_week_lib.last_week_conflict(7, 6, True))
        self.assertTrue(chart_week_lib.last_week_conflict(2, None, True))
        self.assertFalse(chart_week_lib.last_week_conflict(5, None, False))
        self.assertFalse(chart_week_lib.last_week_conflict(None, 4, True))


class ChartWeekPageSmokeTests(TestCase):
    # One request per page that renders ChartWeek's answers, to prove the
    # wiring; the logic itself is covered by ChartWeekTests above.
    W3, W4 = date(1951, 3, 31), date(1951, 4, 7)

    def setUp(self):
        self.chart = Chart.objects.create(category="country", method="jockeys", publication_weekday=5)
        self.rec = Recording.objects.create(
            label=Label.objects.create(name="Victor"), catalog_number="1", side="A",
            song=Song.objects.create(title="Mismatch"), credit_text="Somebody",
        )
        ChartEntry.objects.create(chart=self.chart, week_date=self.W3, position=6, recording=self.rec)

    def test_week_page_shows_computed_weeks_run_and_peak(self):
        ChartEntry.objects.create(chart=self.chart, week_date=self.W4, position=3, recording=self.rec)
        response = self.client.get(reverse("charts:chart_week", args=[self.chart.id, self.W4.isoformat()]))
        self.assertEqual(tuple(response.context["rows"][0]["standing"]), (2, 2, 3))

    def test_week_page_shows_conflicts(self):
        ChartEntry.objects.create(
            chart=self.chart, week_date=self.W4, position=3, recording=self.rec, printed_last_week_position=7,
        )
        response = self.client.get(reverse("charts:chart_week", args=[self.chart.id, self.W4.isoformat()]))
        self.assertEqual(response.context["conflict_count"], 1)
        self.assertEqual(response.context["rows"][0]["movement"], "up")
        self.assertContains(response, "with last week")

    def test_entry_form_search_pick_shows_where_it_was_last_week(self):
        response = self.client.post(reverse("charts:entry_select", args=[self.chart.id]), {
            "row": "0", "recording_id": str(self.rec.id), "week": self.W4.isoformat(),
        })
        self.assertEqual(response.context["last_week_hint"], 6)
        self.assertContains(response, "last week #6")


# =============================================================================
# Chart-week submission (charts/week_submission.py) -- saving a whole chart
# week from the entry form, all or nothing. Called directly with plain
# rows; one smoke test per view below proves the form is wired to it.
# =============================================================================

class WeekSubmissionTests(TestCase):
    WEEK = date(1951, 4, 7)   # a Saturday

    def setUp(self):
        self.chart = Chart.objects.create(category="country", method="jockeys", publication_weekday=5)
        label = Label.objects.create(name="Victor")
        self.recs = [
            Recording.objects.create(
                label=label, catalog_number=str(n), side="A",
                song=Song.objects.create(title=f"Song {n}"), credit_text="Somebody",
            )
            for n in range(1, 5)
        ]

    def _row(self, index, position, recording, **kw):
        # A row as rows_from_post would build it -- strings, like a form post.
        return {
            "index": index, "position": str(position), "last_week_position": kw.get("lw"),
            "recording_id": str(recording.id) if recording else "",
            "printed_anomaly_note": kw.get("note", ""),
        }

    def _on_file(self, week=None):
        return list(
            self.chart.entries.filter(week_date=week or self.WEEK)
            .order_by("position", "id").values_list("position", "recording_id")
        )

    def test_a_new_week_is_saved_with_its_printed_values(self):
        result = week_submission.submit_week(self.chart, self.WEEK, [
            self._row(0, 1, self.recs[0], lw="2"),
            self._row(1, 2, self.recs[1]),
        ], replace=False)
        self.assertTrue(result.saved)
        self.assertEqual(self._on_file(), [(1, self.recs[0].id), (2, self.recs[1].id)])
        first = self.chart.entries.get(position=1)
        self.assertEqual(first.printed_last_week_position, 2)

    def test_a_printed_tie_saves(self):
        result = week_submission.submit_week(self.chart, self.WEEK, [
            self._row(0, 1, self.recs[0]), self._row(1, 1, self.recs[1]), self._row(2, 3, self.recs[2]),
        ], replace=False)
        self.assertTrue(result.saved)
        self.assertEqual([p for p, _ in self._on_file()], [1, 1, 3])

    def test_one_invalid_row_saves_nothing_and_keeps_what_was_typed(self):
        result = week_submission.submit_week(self.chart, self.WEEK, [
            self._row(0, 1, self.recs[0], lw="4"),
            self._row(1, 2, None),            # never resolved to a recording
        ], replace=False)
        self.assertFalse(result.saved)
        self.assertEqual(self._on_file(), [])
        self.assertIn("Row 2", result.errors[0])
        # The page can be re-shown exactly as typed, preview included.
        self.assertEqual(result.rows[0]["recording"], self.recs[0])
        self.assertEqual(result.rows[0]["last_week_position"], "4")

    def test_a_database_rejection_rolls_back_the_whole_week(self):
        # The same recording twice at the same position: the unique
        # constraint rejects the SECOND row, and the first must not survive.
        result = week_submission.submit_week(self.chart, self.WEEK, [
            self._row(0, 1, self.recs[0]), self._row(1, 1, self.recs[0]),
        ], replace=False)
        self.assertFalse(result.saved)
        self.assertEqual(self._on_file(), [])

    def test_a_week_on_the_wrong_weekday_is_refused(self):
        result = week_submission.submit_week(self.chart, date(1951, 4, 6), [
            self._row(0, 1, self.recs[0]),
        ], replace=False)
        self.assertFalse(result.saved)
        self.assertIn("publication weekday", result.errors[0])

    def test_an_edit_replaces_what_was_on_file(self):
        week_submission.submit_week(self.chart, self.WEEK, [
            self._row(0, 1, self.recs[0]), self._row(1, 2, self.recs[1]), self._row(2, 3, self.recs[2]),
        ], replace=False)
        result = week_submission.submit_week(self.chart, self.WEEK, [
            self._row(0, 1, self.recs[1]), self._row(1, 2, self.recs[3]),
        ], replace=True)
        self.assertTrue(result.saved)
        self.assertEqual(self._on_file(), [(1, self.recs[1].id), (2, self.recs[3].id)])

    def test_a_failed_edit_leaves_the_saved_week_untouched(self):
        # The delete of the old entries is inside the same transaction --
        # a failure after it must bring them back.
        week_submission.submit_week(self.chart, self.WEEK, [
            self._row(0, 1, self.recs[0]), self._row(1, 2, self.recs[1]),
        ], replace=False)
        before = self._on_file()
        result = week_submission.submit_week(self.chart, self.WEEK, [
            self._row(0, 1, self.recs[2]), self._row(1, 1, self.recs[2]),   # rejected by the constraint
        ], replace=True)
        self.assertFalse(result.saved)
        self.assertEqual(self._on_file(), before)

    def test_an_edit_touches_no_other_week(self):
        other = date(1951, 3, 31)
        week_submission.submit_week(self.chart, other, [self._row(0, 1, self.recs[0])], replace=False)
        week_submission.submit_week(self.chart, self.WEEK, [self._row(0, 1, self.recs[1])], replace=False)
        week_submission.submit_week(self.chart, self.WEEK, [self._row(0, 1, self.recs[2])], replace=True)
        self.assertEqual(self._on_file(other), [(1, self.recs[0].id)])

    def test_a_new_week_over_a_week_already_on_file_is_refused(self):
        # Adding rows on top of a saved week would mix two charts together.
        # Changing a saved week only ever happens through an edit.
        week_submission.submit_week(self.chart, self.WEEK, [
            self._row(0, 1, self.recs[0]), self._row(1, 2, self.recs[1]),
        ], replace=False)
        before = self._on_file()
        result = week_submission.submit_week(self.chart, self.WEEK, [
            self._row(0, 1, self.recs[2]),
        ], replace=False)
        self.assertFalse(result.saved)
        self.assertTrue(result.already_on_file)
        self.assertIn("already on file", result.errors[0])
        self.assertEqual(self._on_file(), before)
        self.assertEqual(result.rows[0]["recording"], self.recs[2])   # what was typed is kept

    def test_rows_from_post_handles_a_row_deleted_from_the_middle(self):
        post = {
            "position_0": "1", "recording_id_0": "11",
            # row 1 deleted on the page -- its inputs simply aren't posted
            "position_2": "2", "recording_id_2": "12", "last_week_position_2": "5",
        }
        rows = week_submission.rows_from_post(post)
        self.assertEqual([r["index"] for r in rows], [0, 2])
        self.assertEqual(rows[1]["last_week_position"], "5")
        self.assertIsNone(rows[0]["last_week_position"])   # blank -> None, not ""

    def test_next_index_follows_the_highest_submitted_row(self):
        result = week_submission.submit_week(self.chart, self.WEEK, [
            self._row(0, 1, self.recs[0]), self._row(4, 2, None),
        ], replace=False)
        self.assertEqual(result.next_index, 5)


class WeekSubmissionPageSmokeTests(TestCase):
    WEEK = "1951-04-07"

    def setUp(self):
        self.chart = Chart.objects.create(category="country", method="jockeys", publication_weekday=5)
        self.rec = Recording.objects.create(
            label=Label.objects.create(name="Victor"), catalog_number="1", side="A",
            song=Song.objects.create(title="Song"), credit_text="Somebody",
        )

    def test_saving_a_new_week_goes_to_its_page(self):
        response = self.client.post(reverse("charts:entry_week", args=[self.chart.id]), {
            "week_date": self.WEEK, "position_0": "1", "recording_id_0": str(self.rec.id),
        })
        self.assertRedirects(response, reverse("charts:chart_week", args=[self.chart.id, self.WEEK]))
        self.assertEqual(self.chart.entries.count(), 1)

    def test_a_week_already_on_file_offers_edit_instead(self):
        ChartEntry.objects.create(chart=self.chart, week_date=date(1951, 4, 7), position=1, recording=self.rec)
        edit_url = reverse("charts:entry_week_edit", args=[self.chart.id, self.WEEK])
        page = self.client.get(reverse("charts:entry_week", args=[self.chart.id]), {"week": self.WEEK})
        self.assertContains(page, edit_url)
        refused = self.client.post(reverse("charts:entry_week", args=[self.chart.id]), {
            "week_date": self.WEEK, "position_0": "1", "recording_id_0": str(self.rec.id),
        })
        self.assertContains(refused, "already on file")
        self.assertContains(refused, edit_url)
        self.assertEqual(self.chart.entries.count(), 1)

    def test_a_failed_edit_re_shows_the_form(self):
        ChartEntry.objects.create(chart=self.chart, week_date=date(1951, 4, 7), position=1, recording=self.rec)
        response = self.client.post(reverse("charts:entry_week_edit", args=[self.chart.id, self.WEEK]), {
            "position_0": "1", "recording_id_0": "",
        })
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["is_edit"])
        self.assertTrue(response.context["errors"])
        self.assertEqual(self.chart.entries.count(), 1)


# =============================================================================
# Last-week check (charts/lastweek_audit.py)
# =============================================================================

class LastWeekAuditTests(TestCase):
    # Consecutive Saturdays.
    WEEKS = [date(1946, 11, 2) + timedelta(days=7 * i) for i in range(10)]

    def setUp(self):
        self.chart = Chart.objects.create(category="country", method="jukebox", publication_weekday=5)
        self.label = Label.objects.create(name="Decca")
        self.recs = {}

    def _rec(self, title, artist="Somebody"):
        key = (title, artist)
        if key not in self.recs:
            self.recs[key] = Recording.objects.create(
                label=self.label, catalog_number=str(500 + len(self.recs)), side="A",
                song=Song.objects.create(title=title), credit_text=artist,
            )
        return self.recs[key]

    def _e(self, week_i, position, title, lw=None, artist="Somebody", note=""):
        return ChartEntry.objects.create(
            chart=self.chart, week_date=self.WEEKS[week_i], position=position,
            recording=self._rec(title, artist), printed_last_week_position=lw, printed_anomaly_note=note,
        )

    def _cause(self, title):
        rows = [r for r in lastweek_audit_lib.build({})["individual"] if r["song"] == title]
        self.assertEqual(len(rows), 1, f"expected one individual row for {title!r}")
        return rows[0]["cause"]

    def test_swapped_pair(self):
        self._e(0, 1, "A")
        self._e(0, 2, "B")
        self._e(1, 1, "A", lw=2)   # file: A was 1, B was 2 -- typed the other way round
        self._e(1, 2, "B", lw=1)
        self.assertEqual(self._cause("A"), "swapped")
        self.assertEqual(self._cause("B"), "swapped")

    def test_tie_counting(self):
        # Last week: 1, 2, 2, 4 on file; the printed number counted the tie
        # as one place, so the 4th record reads as #3.
        self._e(0, 1, "A")
        self._e(0, 2, "B")
        self._e(0, 2, "C")
        self._e(0, 4, "D")
        self._e(1, 1, "D", lw=3)
        self.assertEqual(self._cause("D"), "ranking")

    def test_other_version_of_the_same_song(self):
        self._e(0, 4, "Someday", artist="Elton Britt")
        self._e(1, 5, "Someday", lw=4, artist="Vaughn Monroe")  # other pressing/version
        self.assertEqual(self._cause("Someday"), "other_version")

    def test_missing_from_last_week(self):
        self._e(0, 4, "Skipped")
        self._e(1, 1, "Filler")                  # week 1 entered WITHOUT "Skipped"
        self._e(2, 4, "Skipped", lw=4)
        self.assertEqual(self._cause("Skipped"), "missing")

    def test_off_by_one_and_lookalike_digit(self):
        self._e(0, 4, "Off")
        self._e(0, 3, "Digit")
        self._e(1, 1, "Off", lw=5)
        self._e(1, 2, "Digit", lw=8)             # 3 read as 8
        self.assertEqual(self._cause("Off"), "off_by_one")
        self.assertEqual(self._cause("Digit"), "digit")

    def test_no_such_position_and_wrong_record(self):
        self._e(0, 1, "Dropped off")
        self._e(1, 1, "Ghost", lw=9)             # no #9 last week
        self._e(1, 2, "Mixup", lw=1)             # last week's #1 isn't on this week's chart
        self.assertEqual(self._cause("Ghost"), "no_position")
        self.assertEqual(self._cause("Mixup"), "wrong_record")

    def test_a_whole_week_mismatch_is_one_stretch_not_individual_guesses(self):
        for i, t in enumerate("ABCDE", start=1):
            self._e(0, i, t)
        # Week 1: all five typed numbers scrambled -- a "column week".
        for pos, (t, lw) in enumerate([("A", 3), ("B", 5), ("C", 1), ("D", 2), ("E", 4)], start=1):
            self._e(1, pos, t, lw=lw)
        for pos, t in enumerate("ABCDE", start=1):
            self._e(2, pos, t, lw=[3, 5, 4, 1, 2][pos - 1])  # and again next week
        ctx = lastweek_audit_lib.build({})
        self.assertEqual(len(ctx["stretches"]), 1)
        self.assertEqual(ctx["stretches"][0]["week_count"], 2)
        self.assertEqual(ctx["individual"], [])

    def test_counts_match_the_week_pages_conflict_marks(self):
        # The audit's bulk loader and ChartWeek (behind the week pages' ⚠
        # marks) share chart_week.last_week_conflict -- the two must agree.
        self._e(0, 1, "A")
        self._e(0, 2, "B")
        self._e(1, 1, "A", lw=1)                 # agrees
        self._e(1, 2, "B", lw=5)                 # disagrees
        self._e(1, 3, "C", lw=7)                 # disagrees (not on last week)
        cw = chart_week_lib.ChartWeek(self.chart, self.WEEKS[1])
        marks = sum(
            cw.movement(e.recording_id, e.position, e.printed_last_week_position).conflict
            for e in self.chart.entries.filter(week_date=self.WEEKS[1])
        )
        self.assertEqual(marks, 2)
        self.assertEqual(lastweek_audit_lib.build({})["total"], marks)

    def test_a_noted_anomaly_moves_to_already_noted(self):
        self._e(0, 1, "A")
        self._e(1, 1, "A", lw=4, note="Misprinted in the magazine")
        ctx = lastweek_audit_lib.build({})
        self.assertEqual(ctx["individual"], [])
        self.assertEqual(len(ctx["noted"]), 1)

    def test_titles_are_escaped_inside_explanations(self):
        self._e(0, 1, "<b>Bold</b> & Co")
        self._e(1, 2, "Mixup", lw=1)
        response = self.client.get(reverse("charts:lastweek_audit"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "&lt;b&gt;Bold&lt;/b&gt; &amp; Co")
        self.assertNotContains(response, "<b>Bold</b> & Co")


# =============================================================================
# Chart switcher on the week page (views.chart_switch_options)
# =============================================================================

class ChartSwitcherTests(TestCase):
    W1, W2, W3 = date(1948, 5, 8), date(1948, 5, 15), date(1948, 5, 22)

    def setUp(self):
        self.jukebox = Chart.objects.create(category="country", method="jukebox", publication_weekday=5)
        self.retail = Chart.objects.create(category="country", method="retail", publication_weekday=5)
        self.empty = Chart.objects.create(category="rnb", method="retail", publication_weekday=5)
        rec = Recording.objects.create(
            label=Label.objects.create(name="Decca"), catalog_number="1", side="A",
            song=Song.objects.create(title="Song"), credit_text="Somebody",
        )
        for w in (self.W1, self.W2, self.W3):
            ChartEntry.objects.create(chart=self.jukebox, week_date=w, position=1, recording=rec)
        # Retail starts a week later, like the real Retail charts (May 1948).
        for w in (self.W2, self.W3):
            ChartEntry.objects.create(chart=self.retail, week_date=w, position=1, recording=rec)

    def _options(self, chart, week):
        # A plain function of (chart, week) -- no request needed.
        return {o["chart"].id: o for o in views.chart_switch_options(chart, week)}

    def test_switching_keeps_the_same_week(self):
        options = self._options(self.jukebox, self.W2)
        self.assertEqual(options[self.retail.id]["url"], reverse("charts:chart_week", args=[self.retail.id, "1948-05-15"]))
        self.assertIsNone(options[self.retail.id]["other_week"])
        self.assertTrue(options[self.jukebox.id]["selected"])

    def test_a_week_the_other_chart_lacks_goes_to_its_closest_and_says_so(self):
        options = self._options(self.jukebox, self.W1)
        self.assertEqual(options[self.retail.id]["other_week"], self.W2)
        self.assertEqual(options[self.retail.id]["url"], reverse("charts:chart_week", args=[self.retail.id, "1948-05-15"]))

    def test_a_chart_with_nothing_on_file_is_left_out(self):
        self.assertNotIn(self.empty.id, self._options(self.jukebox, self.W2))
