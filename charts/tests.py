# charts/tests.py
#
# Per CLAUDE.md's Conventions section: derived-field/aggregation logic is
# worth testing, UI is not. The recordings matrix's aggregation is exactly
# that kind of logic -- it's easy to get subtly wrong (see the test cases
# below, which each target a real way the aggregate could silently
# produce a number nobody ever printed anywhere).

from datetime import date, timedelta
from unittest import mock

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from . import credits as credits_lib
from . import discogs as discogs_api
from .models import (
    Artist, ArtistAlias, Chart, ChartEntry, DiscogsCandidate, DiscogsMatch,
    Label, Recording, RecordingCredit, Song,
)


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

    def _get(self, **params):
        return self.client.get(reverse("charts:recordings_matrix"), params)

    def test_peak_weeks_first_last(self):
        w1, w2, w3 = date(1948, 1, 3), date(1948, 1, 10), date(1948, 1, 17)  # 3 consecutive Saturdays
        self._entry(self.chart, w1, 5)
        self._entry(self.chart, w2, 2)
        self._entry(self.chart, w3, 4)

        response = self._get()
        self.assertEqual(response.status_code, 200)
        rows = response.context["rows"]
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

        rows = self._get().context["rows"]
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

        row = self._get().context["rows"][0]
        self.assertEqual(row["weeks_charted"], 1)
        self.assertEqual(row["peak_position"], 1)

    def test_genre_filter(self):
        self._entry(self.chart, date(1948, 1, 3), 1)  # country
        self._entry(self.other_chart, date(1948, 1, 3), 1)  # rnb

        rows = self._get(genre="country").context["rows"]
        self.assertEqual([r["chart_id"] for r in rows], [self.chart.id])

    def test_chart_filter(self):
        self._entry(self.chart, date(1948, 1, 3), 1)
        self._entry(self.other_chart, date(1948, 1, 3), 1)

        rows = self._get(chart=self.other_chart.id).context["rows"]
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
        self.assertEqual(len(self._get(q="Test Song").context["rows"]), 1)
        # ...or by catalog number...
        self.assertEqual(len(self._get(q="200").context["rows"]), 1)
        # ...and a query with no match returns nothing, not everything.
        self.assertEqual(len(self._get(q="Nonexistent").context["rows"]), 0)

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
        params.setdefault("group", "recording")
        return self.client.get(reverse("charts:recordings_matrix"), params)

    def test_one_row_per_recording_however_many_charts(self):
        w1, w2 = date(1951, 1, 6), date(1951, 1, 13)
        self._entry(self.jukebox, w1, 3)
        self._entry(self.retail, w1, 5)
        self._entry(self.rnb, w2, 9)

        # Default mode still gives one row per chart...
        self.assertEqual(len(self.client.get(reverse("charts:recordings_matrix")).context["rows"]), 3)
        # ...and the collapsed mode gives exactly one, with a count of
        # how many charts went into it.
        rows = self._collapsed().context["rows"]
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

        row = self._collapsed().context["rows"][0]
        self.assertEqual(ChartEntry.objects.count(), 4)
        self.assertEqual(row["weeks_charted"], 2)

    def test_peak_is_best_across_charts_and_names_its_source_chart(self):
        # Merged, "peak" is the best position reached on ANY chart -- a
        # derived number nobody printed, so the row has to say which
        # chart it came from (CLAUDE.md non-negotiable #4).
        w1 = date(1951, 1, 6)
        self._entry(self.jukebox, w1, 7)
        self._entry(self.retail, w1, 2)

        row = self._collapsed().context["rows"][0]
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

        row = self._collapsed().context["rows"][0]
        self.assertEqual(row["first_entry"], first)
        self.assertEqual(row["last_entry"], last)
        self.assertEqual(row["first_chart_id"], self.retail.id)
        self.assertEqual(row["last_chart_id"], self.jukebox.id)
        # And the rendered links point at those two different charts.
        html = self._collapsed().content.decode()
        self.assertIn(f'href="/{self.retail.id}/1950-12-16/"', html)
        self.assertIn(f'href="/{self.jukebox.id}/1951-02-17/"', html)

    def test_per_chart_breakdown_badges_keep_each_charts_own_peak(self):
        # Collapsing hides the per-chart peaks, so they come back as the
        # Charts column's hover badges rather than being lost.
        w1 = date(1951, 1, 6)
        self._entry(self.jukebox, w1, 1)
        self._entry(self.retail, w1, 6)

        row = self._collapsed().context["rows"][0]
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

        row = self._collapsed(genre="country").context["rows"][0]
        self.assertEqual(row["chart_count"], 1)
        self.assertEqual(row["peak_position"], 4)  # NOT the R&B chart's 1
        self.assertEqual(row["weeks_charted"], 1)

    def test_default_mode_is_per_chart_and_a_bad_value_falls_back(self):
        self._entry(self.jukebox, date(1951, 1, 6), 1)
        self._entry(self.retail, date(1951, 1, 6), 1)

        self.assertEqual(self.client.get(reverse("charts:recordings_matrix")).context["group"], "chart")
        self.assertEqual(len(self.client.get(reverse("charts:recordings_matrix")).context["rows"]), 2)
        junk = self.client.get(reverse("charts:recordings_matrix"), {"group": "nonsense"})
        self.assertEqual(junk.context["group"], "chart")
        self.assertEqual(len(junk.context["rows"]), 2)

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

        rows = self._collapsed(sort="chart", dir="desc").context["rows"]
        self.assertEqual(len(rows), 2)  # still one row each, not three
        # Descending by chart count: the 2-chart recording comes first.
        self.assertEqual([r["chart_count"] for r in rows], [2, 1])

    def test_mode_and_filters_ride_along_on_sort_links(self):
        # Clicking a column heading is a plain link (full page load), so
        # if it didn't carry ?group= and the filters, sorting a filtered
        # collapsed view would bounce it back to unfiltered per-chart.
        self._entry(self.jukebox, date(1951, 1, 6), 1)

        columns = self._collapsed(genre="country", q="Boogie").context["columns"]
        for key in ("peak", "song", "weeks"):
            self.assertIn("group=recording", columns[key]["href"])
            self.assertIn("genre=country", columns[key]["href"])
            self.assertIn("q=Boogie", columns[key]["href"])

    def test_headers_rename_the_two_columns_that_change_meaning(self):
        self._entry(self.jukebox, date(1951, 1, 6), 1)

        per_chart = self.client.get(reverse("charts:recordings_matrix")).context["columns"]
        self.assertEqual(per_chart["peak"]["label"], "Peak")
        self.assertEqual(per_chart["chart"]["label"], "Chart")

        collapsed = self._collapsed().context["columns"]
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


class EntryCreateDuplicateCheckTests(TestCase):
    # entry_create's backstop check: search-first is the INTENDED workflow
    # (the search box handles the common case), so what's worth testing
    # here specifically is the two signals the check is built on -- see
    # the big comment above the check in views.entry_create.

    def setUp(self):
        self.chart = Chart.objects.create(category="country", method="jukebox", publication_weekday=5)
        self.label = Label.objects.create(name="Capitol")
        self.song = Song.objects.create(title="Bouquet of Roses")
        self.existing = Recording.objects.create(
            label=self.label, catalog_number="40065", side="A",
            song=self.song, credit_text="Eddy Arnold",
        )

    def _post_create(self, row="0", confirm_create=None, **fields):
        data = {"row": row, "week": "1949-01-01"}
        if confirm_create is not None:
            data["confirm_create"] = confirm_create
        for key, value in fields.items():
            data[f"new_{key}_{row}"] = value
        return self.client.post(reverse("charts:entry_create", args=[self.chart.id]), data)

    def test_no_similar_recording_creates_directly(self):
        before = Recording.objects.count()
        response = self._post_create(
            song="A Completely Different Song", artist="Some New Artist",
            credit_text="", label="Decca", catalog="46234", side="B",
        )
        self.assertTemplateUsed(response, "charts/_entry_resolved.html")
        self.assertTemplateNotUsed(response, "charts/_entry_create_duplicate_check.html")
        self.assertEqual(Recording.objects.count(), before + 1)

    def test_matching_song_and_artist_blocks_creation_and_offers_the_match(self):
        # Same song + same artist credit typed, but a DIFFERENT (say,
        # mistyped) catalog number -- signal 1: looks like the same
        # recording, catalog number aside.
        before = Recording.objects.count()
        response = self._post_create(
            song="Bouquet of Roses", artist="Eddy Arnold",
            credit_text="", label="Capitol", catalog="40065-X", side="A",
        )
        self.assertTemplateUsed(response, "charts/_entry_create_duplicate_check.html")
        self.assertEqual(list(response.context["matches"]), [self.existing])
        self.assertEqual(Recording.objects.count(), before)  # nothing created yet

    def test_matching_label_and_catalog_blocks_creation_even_with_different_artist_text(self):
        # Same label + catalog number as what's on file, but the artist
        # was typed differently -- signal 2, and the more dangerous one to
        # miss: get_or_create's own dedup (label+catalog+side) wouldn't
        # even fire here on a plain word-AND search, since "Eddy Arnold"
        # doesn't appear anywhere in what was typed.
        before = Recording.objects.count()
        response = self._post_create(
            song="Some Other Title", artist="A Totally Different Artist",
            credit_text="", label="Capitol", catalog="40065", side="A",
        )
        self.assertTemplateUsed(response, "charts/_entry_create_duplicate_check.html")
        self.assertEqual(list(response.context["matches"]), [self.existing])
        self.assertEqual(Recording.objects.count(), before)

    def test_confirm_create_bypasses_the_check(self):
        # Same song+artist as the existing recording (would normally
        # trigger the check via signal 1), but confirm_create=1 skips
        # straight past it -- and since the catalog number is genuinely
        # different, a real second Recording gets created (confirming
        # only bypasses the SOFT fuzzy check, not the hard label+catalog+
        # side uniqueness constraint underneath it).
        before = Recording.objects.count()
        response = self._post_create(
            song="Bouquet of Roses", artist="Eddy Arnold",
            credit_text="", label="Capitol", catalog="40065-X", side="A",
            confirm_create="1",
        )
        # (confirm_create is pulled out by _post_create, not name-mangled
        # like the other new_*_{row} fields -- see its signature above.)
        self.assertTemplateUsed(response, "charts/_entry_resolved.html")
        self.assertTemplateNotUsed(response, "charts/_entry_create_duplicate_check.html")
        self.assertEqual(Recording.objects.count(), before + 1)


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
    /credits/ queue exists to clean up.
    """

    def setUp(self):
        self.chart = Chart.objects.create(category="country", method="jukebox", publication_weekday=5)

    def _create(self, artists, row="0", **overrides):
        data = {
            "row": row, "week": "1949-01-01", "confirm_create": "1",
            "new_song_0": "Tennessee Border No. 2", "new_artist_0": artists,
            "new_credit_text_0": "", "new_label_0": "Decca",
            "new_catalog_0": "46200", "new_side_0": "A", "new_discogs_0": "",
        }
        data.update(overrides)
        return self.client.post(reverse("charts:entry_create", args=[self.chart.id]), data)

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
        # "+ artist" just adds another box under the same field name, so
        # any number of artists goes through the same getlist.
        self._create(["Ernest Tubb", "Red Foley", "Minnie Pearl"])
        recording = Recording.objects.get(catalog_number="46200")
        credits = recording.credits.order_by("credit_order")
        self.assertEqual([c.artist.canonical_name for c in credits],
                         ["Ernest Tubb", "Red Foley", "Minnie Pearl"])
        self.assertEqual([c.credit_order for c in credits], [1, 2, 3])
        self.assertEqual(recording.credit_text, "Ernest Tubb - Red Foley - Minnie Pearl")

    def test_credit_text_defaults_to_the_printed_joining_convention(self):
        self._create(["Ernest Tubb", "Red Foley"])
        self.assertEqual(Recording.objects.get(catalog_number="46200").credit_text,
                         "Ernest Tubb - Red Foley")

    def test_explicitly_typed_credit_text_wins_over_the_default(self):
        # credit_text is the as-printed billing; a default must never
        # override what was actually typed from the page.
        self._create(["Ernest Tubb", "Red Foley"],
                     new_credit_text_0="Ernest Tubb and Red Foley with the Texas Troubadours")
        self.assertEqual(Recording.objects.get(catalog_number="46200").credit_text,
                         "Ernest Tubb and Red Foley with the Texas Troubadours")

    def test_single_artist_still_works_unchanged(self):
        self._create(["Ernest Tubb"])
        recording = Recording.objects.get(catalog_number="46200")
        self.assertEqual(recording.credits.count(), 1)
        self.assertEqual(recording.credit_text, "Ernest Tubb")

    def test_blank_second_box_is_ignored(self):
        self._create(["Ernest Tubb", "   "])
        self.assertEqual(Recording.objects.get(catalog_number="46200").credits.count(), 1)

    def test_the_same_artist_typed_twice_is_credited_once(self):
        # unique_recording_artist would reject the second row outright.
        self._create(["Ernest Tubb", "Ernest Tubb"])
        self.assertEqual(Recording.objects.get(catalog_number="46200").credits.count(), 1)

    def test_existing_artist_is_reused_via_alias_not_duplicated(self):
        real = Artist.objects.create(canonical_name="Big Joe Turner")
        ArtistAlias.objects.create(artist=real, alias_text="Joe Turner")
        self._create(["Joe Turner"])
        recording = Recording.objects.get(catalog_number="46200")
        self.assertEqual(recording.credits.get().artist, real)
        self.assertFalse(Artist.objects.filter(canonical_name="Joe Turner").exists())

    def test_joined_name_in_one_box_still_creates_but_warns(self):
        # Non-blocking on purpose -- credit_text records the billing
        # correctly either way and /credits/ can unpick it -- but the
        # warning is what makes it cheap to fix now instead of later.
        response = self._create(["Ernest Tubb - Red Foley"])
        self.assertTrue(Recording.objects.filter(catalog_number="46200").exists())
        self.assertContains(response, "looks like 2 artists")

    def test_single_act_name_containing_and_is_not_warned_about(self):
        # "Homer & Jethro" is one act. Warning here would be noise, and
        # noise is what makes warnings get ignored.
        response = self._create(["Homer & Jethro"])
        self.assertNotContains(response, "looks like")


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
        from .views import _discogs_progress
        progress = _discogs_progress()
        self.assertEqual(progress["needs_detail"], 1)
        self.assertEqual(progress["settled"], 2)   # done + master_only, not pending

    def test_pending_row_renders_a_self_triggering_fetch(self):
        response = self.client.get(reverse("charts:discogs_queue"))
        self.assertContains(response, 'hx-trigger="load"', count=1)

    def test_a_failed_fetch_renders_retry_not_another_auto_trigger(self):
        # Otherwise a dead connection or missing token would re-fire the
        # load trigger on every swap, forever.
        with mock.patch.object(discogs_api, "_request",
                               side_effect=discogs_api.DiscogsError("no internet")):
            response = self.client.post(
                reverse("charts:discogs_fetch_detail", args=[self.pending.recording_id]))
        self.assertContains(response, "no internet")
        self.assertContains(response, "Retry fetching details")
        self.assertNotContains(response, 'hx-trigger="load"')

    def test_bulk_fetch_skips_master_only_rows_instead_of_stalling_on_them(self):
        release = {
            "id": 5435918, "title": "Mother Earth", "artists": [{"name": "Memphis Slim"}],
            "labels": [{"name": "Premium", "catno": "867"}], "formats": [], "year": 1951,
            "tracklist": [{"position": "B", "title": "Mother Earth"}],
        }
        with mock.patch.object(discogs_api, "_request", return_value=release):
            self.client.post(reverse("charts:discogs_bulk"),
                             {"action": "fetch_details", "querystring": ""})
        self.pending.refresh_from_db()
        self.assertFalse(self.pending.needs_detail)


class DiscogsWorkflowTests(TestCase):
    """The review queue's state transitions."""

    def setUp(self):
        self.label = Label.objects.create(name="Specialty")
        self.song = Song.objects.create(title="Pink Champagne")
        self.recording = Recording.objects.create(
            label=self.label, catalog_number="355", side="A",
            song=self.song, credit_text="Joe Liggins",
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

    def _release_payload(self):
        # What /releases/{id} returns for this recording, for the detail
        # fetch that confirming now triggers.
        return {
            "id": 42, "title": "Pink Champagne", "artists": [{"name": "Joe Liggins"}],
            "labels": [{"name": "Specialty", "catno": "355"}], "formats": [{"name": "Shellac"}],
            "year": 1950, "country": "US", "genres": ["Blues"], "styles": ["Jump Blues"],
            "tracklist": [{"position": "A", "title": "Pink Champagne",
                           "extraartists": [{"name": "Joe Liggins", "role": "Written-By"}]}],
        }

    def test_queue_creates_a_match_row_for_every_recording(self):
        # A recording created later (e.g. a debut typed into the entry
        # form) gets picked up automatically -- no signal, no extra step.
        new = Recording.objects.create(
            label=self.label, catalog_number="999", side="A",
            song=Song.objects.create(title="Later Addition"), credit_text="Someone",
        )
        self.assertFalse(DiscogsMatch.objects.filter(recording=new).exists())
        self.client.get(reverse("charts:discogs_queue"))
        self.assertTrue(DiscogsMatch.objects.filter(recording=new).exists())

    def test_search_stores_candidates_without_confirming_anything(self):
        payload = {"results": [{"id": 42, "title": "Joe Liggins - Pink Champagne",
                                "catno": "355", "label": ["Specialty"]}]}
        with mock.patch.object(discogs_api, "_request", return_value=payload):
            self.client.post(reverse("charts:discogs_search", args=[self.recording.id]))

        self.match.refresh_from_db()
        # SUGGESTED, never CONFIRMED -- picking is always a human action.
        self.assertEqual(self.match.status, DiscogsMatch.SUGGESTED)
        self.assertEqual(self.match.candidates.count(), 1)
        self.assertIsNone(self.match.discogs_release_id)

    def test_empty_search_marks_no_match_rather_than_leaving_it_pending(self):
        with mock.patch.object(discogs_api, "_request", return_value={"results": []}):
            self.client.post(reverse("charts:discogs_search", args=[self.recording.id]))
        self.match.refresh_from_db()
        self.assertEqual(self.match.status, DiscogsMatch.NO_MATCH)

    def test_api_failure_is_shown_not_raised(self):
        # A dead connection must not 500 the page or change any state.
        with mock.patch.object(discogs_api, "_request", side_effect=discogs_api.DiscogsError("no internet")):
            response = self.client.post(reverse("charts:discogs_search", args=[self.recording.id]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "no internet")
        self.match.refresh_from_db()
        self.assertEqual(self.match.status, DiscogsMatch.UNMATCHED)

    def test_confirming_copies_the_candidate_and_also_fetches_full_details(self):
        # A single-row confirm finishes the job in one click -- unlike
        # the BULK "confirm exact matches" action (see
        # test_bulk_confirm_only_touches_exact_matches... and
        # discogs_bulk's confirm_exact, which stays detail-free on
        # purpose), a single confirm is exactly one more API call, same
        # cost as the "Search Discogs" click that got it here.
        candidate = self._candidate()
        self.match.status = DiscogsMatch.SUGGESTED
        self.match.save()

        release = {
            "id": 42, "title": "Pink Champagne", "artists": [{"name": "Joe Liggins"}],
            "labels": [{"name": "Specialty", "catno": "355"}], "formats": [{"name": "Shellac"}],
            "year": 1950, "country": "US", "genres": ["Blues"], "styles": ["Jump Blues"],
            "tracklist": [{"position": "A", "title": "Pink Champagne",
                           "extraartists": [{"name": "Joe Liggins", "role": "Written-By"}]}],
        }
        with mock.patch.object(discogs_api, "_request", return_value=release) as request:
            self.client.post(
                reverse("charts:discogs_confirm", args=[self.recording.id]),
                {"candidate_id": candidate.id},
            )
        request.assert_called_once()  # confirm + detail = exactly one API call, not zero and not two

        self.match.refresh_from_db()
        self.assertEqual(self.match.status, DiscogsMatch.CONFIRMED)
        self.assertEqual(self.match.discogs_release_id, 42)
        self.assertEqual(self.match.matched_catalog_number, "355")
        self.assertEqual(self.match.candidates.count(), 0)  # shortlist cleared
        self.assertEqual(self.match.songwriters, "Joe Liggins")
        self.assertFalse(self.match.needs_detail)  # fetched already, not pending

    def test_confirming_still_stands_even_if_the_detail_fetch_fails(self):
        # The confirm itself (candidate -> match) must not be undone by a
        # rate limit or a dropped connection on the SECOND call -- only
        # the detail fetch is left pending, retryable via the same
        # per-row button as always.
        candidate = self._candidate()
        self.match.status = DiscogsMatch.SUGGESTED
        self.match.save()

        with mock.patch.object(discogs_api, "_request", side_effect=discogs_api.DiscogsError("rate limited")):
            response = self.client.post(
                reverse("charts:discogs_confirm", args=[self.recording.id]),
                {"candidate_id": candidate.id},
            )

        self.assertContains(response, "rate limited")
        self.match.refresh_from_db()
        self.assertEqual(self.match.status, DiscogsMatch.CONFIRMED)  # confirm stands
        self.assertEqual(self.match.discogs_release_id, 42)
        self.assertTrue(self.match.needs_detail)  # detail fetch still pending, can retry

    def test_manual_release_entry_also_fetches_full_details(self):
        release = {
            "id": 12345, "title": "x", "artists": [], "labels": [{"name": "L", "catno": "C"}],
            "formats": [], "year": 1950, "country": "US", "genres": [], "styles": [],
            "tracklist": [], "extraartists": [{"name": "A Writer", "role": "Written-By"}],
        }
        with mock.patch.object(discogs_api, "_request", return_value=release):
            self.client.post(
                reverse("charts:discogs_manual", args=[self.recording.id]),
                {"discogs_ref": "https://www.discogs.com/release/12345"},
            )
        self.match.refresh_from_db()
        self.assertEqual(self.match.songwriters, "A Writer")
        self.assertFalse(self.match.needs_detail)

    def test_manual_master_entry_does_not_attempt_a_detail_fetch(self):
        # No release id -- nothing for /releases/{id} to fetch, so this
        # must not even try (a doomed API call would just show an error
        # for something that was never going to work).
        with mock.patch.object(discogs_api, "_request", side_effect=AssertionError("must not call the API")):
            self.client.post(
                reverse("charts:discogs_manual", args=[self.recording.id]),
                {"discogs_ref": "https://www.discogs.com/master/999"},
            )
        self.match.refresh_from_db()
        self.assertEqual(self.match.status, DiscogsMatch.MANUAL)
        self.assertEqual(self.match.discogs_master_id, 999)

    def test_fetch_detail_fills_in_credits(self):
        self.match.status = DiscogsMatch.CONFIRMED
        self.match.discogs_release_id = 42
        self.match.save()
        release = {
            "id": 42, "title": "Pink Champagne", "artists": [{"name": "Joe Liggins"}],
            "labels": [{"name": "Specialty", "catno": "355"}], "formats": [{"name": "Shellac"}],
            "year": 1950, "country": "US", "genres": ["Blues"], "styles": ["Jump Blues"],
            "tracklist": [{"position": "A", "title": "Pink Champagne",
                           "extraartists": [{"name": "Joe Liggins", "role": "Written-By"}]}],
        }
        with mock.patch.object(discogs_api, "_request", return_value=release):
            self.client.post(reverse("charts:discogs_fetch_detail", args=[self.recording.id]))

        self.match.refresh_from_db()
        self.assertEqual(self.match.songwriters, "Joe Liggins")
        self.assertEqual(self.match.genres, ["Blues"])
        self.assertFalse(self.match.needs_detail)

    def test_manual_entry_accepts_a_url_or_a_bare_id(self):
        # A release reference triggers the same auto-detail-fetch as
        # discogs_confirm (see test_manual_release_entry_also_fetches_
        # full_details for that behavior specifically) -- mocked here
        # since it's incidental to what THIS test is actually checking
        # (parsing), and must never hit the real network either way.
        release = {"id": 12345, "title": "x", "labels": [{"name": "L", "catno": "C"}], "formats": []}
        for reference in ["12345", "https://www.discogs.com/release/12345", "release/12345-Some-Slug"]:
            with mock.patch.object(discogs_api, "_request", return_value=release):
                self.client.post(
                    reverse("charts:discogs_manual", args=[self.recording.id]),
                    {"discogs_ref": reference},
                )
            self.match.refresh_from_db()
            self.assertEqual(self.match.discogs_release_id, 12345, msg=f"failed for {reference!r}")
            self.assertIsNone(self.match.discogs_master_id, msg=f"failed for {reference!r}")
            self.assertEqual(self.match.status, DiscogsMatch.MANUAL)

    def test_manual_entry_recognizes_a_master_url_and_does_not_store_it_as_a_release(self):
        # The bug this guards against: a naive "just grab the first
        # number" parser would treat .../master/12345 as release id
        # 12345, which is wrong -- a Master isn't a Release, and would
        # later break the "fetch full details" call (that endpoint needs
        # an actual release id).
        for reference in ["https://www.discogs.com/master/12345", "master/12345-Some-Slug"]:
            self.client.post(
                reverse("charts:discogs_manual", args=[self.recording.id]),
                {"discogs_ref": reference},
            )
            self.match.refresh_from_db()
            self.assertIsNone(self.match.discogs_release_id, msg=f"failed for {reference!r}")
            self.assertEqual(self.match.discogs_master_id, 12345, msg=f"failed for {reference!r}")
            self.assertEqual(self.match.status, DiscogsMatch.MANUAL)
            self.assertEqual(self.match.reference_type, "Master")
            self.assertFalse(self.match.needs_detail)  # can't fetch release detail for a master-only match

    def test_manual_entry_rejects_garbage(self):
        response = self.client.post(
            reverse("charts:discogs_manual", args=[self.recording.id]),
            {"discogs_ref": "not a url or id"},
        )
        self.assertContains(response, "Couldn&#x27;t read a release or master ID")
        self.match.refresh_from_db()
        self.assertEqual(self.match.status, DiscogsMatch.UNMATCHED)

    def test_reset_clears_the_link_and_everything_cached(self):
        self.match.status = DiscogsMatch.CONFIRMED
        self.match.discogs_release_id = 42
        self.match.songwriters = "Someone"
        self.match.matched_title = "Something"
        self.match.raw_data = {"id": 42}
        self.match.save()
        self._candidate()

        self.client.post(
            reverse("charts:discogs_set_status", args=[self.recording.id]),
            {"status": "unmatched"},
        )

        self.match.refresh_from_db()
        self.assertEqual(self.match.status, DiscogsMatch.UNMATCHED)
        self.assertIsNone(self.match.discogs_release_id)
        self.assertEqual(self.match.songwriters, "")
        self.assertEqual(self.match.matched_title, "")
        self.assertIsNone(self.match.raw_data)
        self.assertEqual(self.match.candidates.count(), 0)

    def test_bulk_confirm_only_touches_exact_matches(self):
        exact_match = self.match
        exact_match.status = DiscogsMatch.SUGGESTED
        exact_match.save()
        self._candidate(confidence=DiscogsMatch.EXACT)

        # A second recording whose only candidate is a WEAK guess -- it
        # must be left alone even though its id is submitted too.
        weak_recording = Recording.objects.create(
            label=self.label, catalog_number="777", side="A",
            song=Song.objects.create(title="Something Else"), credit_text="Someone",
        )
        weak_match = DiscogsMatch.objects.create(recording=weak_recording, status=DiscogsMatch.SUGGESTED)
        DiscogsCandidate.objects.create(
            match=weak_match, discogs_release_id=99, title="A guess",
            confidence=DiscogsMatch.WEAK, rank=0,
        )

        with mock.patch.object(discogs_api, "_request", return_value=self._release_payload()):
            self.client.post(reverse("charts:discogs_bulk"), {
                "action": "confirm_exact",
                "match_id": [exact_match.id, weak_match.id],
                "querystring": "",
            })

        exact_match.refresh_from_db()
        weak_match.refresh_from_db()
        self.assertEqual(exact_match.status, DiscogsMatch.CONFIRMED)
        self.assertEqual(weak_match.status, DiscogsMatch.SUGGESTED)  # untouched
        self.assertIsNone(weak_match.discogs_release_id)

    def test_bulk_confirm_also_fetches_details_so_no_row_is_left_incomplete(self):
        # Bulk-confirm used to skip the detail fetch to stay instant,
        # which left rows showing release-level stand-ins with no
        # songwriters until someone remembered a follow-up step. It
        # doesn't any more -- confirming and enriching are one operation.
        self.match.status = DiscogsMatch.SUGGESTED
        self.match.save()
        self._candidate(confidence=DiscogsMatch.EXACT)

        with mock.patch.object(discogs_api, "_request", return_value=self._release_payload()):
            response = self.client.post(reverse("charts:discogs_bulk"), {
                "action": "confirm_exact", "match_id": [self.match.id], "querystring": "",
            }, follow=True)

        self.match.refresh_from_db()
        self.assertEqual(self.match.status, DiscogsMatch.CONFIRMED)
        self.assertFalse(self.match.needs_detail)
        self.assertTrue(self.match.has_track_detail)
        self.assertEqual(self.match.songwriters, "Joe Liggins")
        self.assertContains(response, "full details fetched for 1")

    def test_bulk_confirm_keeps_the_confirm_when_the_detail_fetch_fails(self):
        # A network failure must not roll back a decision that was
        # correctly made -- the row stays confirmed, just not yet
        # enriched, and the message says so.
        self.match.status = DiscogsMatch.SUGGESTED
        self.match.save()
        self._candidate(confidence=DiscogsMatch.EXACT)

        with mock.patch.object(discogs_api, "_request", side_effect=discogs_api.DiscogsError("rate limited")):
            response = self.client.post(reverse("charts:discogs_bulk"), {
                "action": "confirm_exact", "match_id": [self.match.id], "querystring": "",
            }, follow=True)

        self.match.refresh_from_db()
        self.assertEqual(self.match.status, DiscogsMatch.CONFIRMED)  # decision stands
        self.assertTrue(self.match.needs_detail)                     # recoverable
        self.assertContains(response, "still need it")

    def test_bulk_confirm_skips_a_genuine_tie_between_two_exact_candidates(self):
        # Two DIFFERENT Discogs releases (different ids -- e.g. a US and
        # a Canadian pressing) both score EXACT for the same recording.
        # Bulk-confirm must refuse to guess between them rather than
        # picking whichever happened to come first.
        self.match.status = DiscogsMatch.SUGGESTED
        self.match.save()
        self._candidate(discogs_release_id=1, confidence=DiscogsMatch.EXACT, rank=0)
        self._candidate(discogs_release_id=2, confidence=DiscogsMatch.EXACT, rank=1)

        response = self.client.post(reverse("charts:discogs_bulk"), {
            "action": "confirm_exact", "match_id": [self.match.id], "querystring": "",
        }, follow=True)

        self.match.refresh_from_db()
        self.assertEqual(self.match.status, DiscogsMatch.SUGGESTED)  # untouched, still needs a human
        self.assertIsNone(self.match.discogs_release_id)
        self.assertEqual(self.match.candidates.count(), 2)  # shortlist NOT cleared
        self.assertContains(response, "Skipped 1 with more than one exact match")

    def test_has_ambiguous_exact_tie(self):
        self.assertFalse(self.match.has_ambiguous_exact_tie)
        self._candidate(discogs_release_id=1, confidence=DiscogsMatch.EXACT, rank=0)
        self.assertFalse(self.match.has_ambiguous_exact_tie)
        self._candidate(discogs_release_id=2, confidence=DiscogsMatch.EXACT, rank=1)
        self.assertTrue(self.match.has_ambiguous_exact_tie)

    def test_page_exact_count_excludes_ambiguous_ties(self):
        # The "confirm N exact matches" button's own count must agree
        # with what the action will actually do -- a match with two
        # EXACT candidates doesn't count towards N, since confirm_exact
        # will skip it.
        self.match.status = DiscogsMatch.SUGGESTED
        self.match.save()
        self._candidate(discogs_release_id=1, confidence=DiscogsMatch.EXACT, rank=0)
        self._candidate(discogs_release_id=2, confidence=DiscogsMatch.EXACT, rank=1)

        response = self.client.get(reverse("charts:discogs_queue"), {"status": "suggested"})
        self.assertEqual(response.context["page_exact_ids"], [])


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
