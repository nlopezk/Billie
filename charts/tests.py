# charts/tests.py
#
# Per CLAUDE.md's Conventions section: derived-field/aggregation logic is
# worth testing, UI is not. The recordings matrix's aggregation is exactly
# that kind of logic -- it's easy to get subtly wrong (see the test cases
# below, which each target a real way the aggregate could silently
# produce a number nobody ever printed anywhere).

from datetime import date

from django.test import TestCase
from django.urls import reverse

from .models import Chart, ChartEntry, Label, Recording, Song


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
