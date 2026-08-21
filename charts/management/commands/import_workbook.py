# charts/management/commands/import_workbook.py
#
# One-time import of the owner's hand-transcribed workbook (default:
# "Bb Charts fixed.xlsx" in the repo root) into the charts app schema.
#
# This is explicitly a ONE-TIME migration of the owner's own prior manual
# work, not a substitute for the weekly entry form going forward -- see
# CLAUDE.md Non-negotiable #1. It refuses to run against a non-empty
# ChartEntry table so it can't accidentally double-import.
#
# A few things below are hardcoded rather than re-derived from the
# workbook at import time, on purpose: they represent one-time human
# judgment calls made during the audit (see docs/audit/) that shouldn't
# silently change if this command is ever re-run against a re-cleaned copy
# of the workbook:
#   - ERAS: chart era boundaries/printed names/sizes. Derived from the
#     workbook's (sparsely-filled) Chart Name column by forward-filling
#     blanks and finding each name's first appearance -- see the chat
#     history around 2026-08-12 for the derivation. Country JOCKEYS never
#     had this column filled in at all; its printed name was supplied
#     directly by the owner.
#   - REISSUES: the 6 confirmed same-recording/different-catalog-number
#     pairs from the audit, each checked against a real discography
#     source (see docs/audit/other_issues.csv and the chat history for
#     citations). Lionel Hampton's "Flying Home" is deliberately excluded
#     -- confirmed as a genuine separate re-recording, not a reissue.
#   - ANOMALIES: the 2 accepted cases of Billboard itself printing the
#     same recording twice in one week (docs/audit/same_recording_repeated.csv).

from pathlib import Path

import pandas as pd
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from charts.models import (
    Artist,
    Chart,
    ChartEntry,
    ChartEra,
    Label,
    Recording,
    RecordingCredit,
    Song,
)

# Sheet name -> (Chart.category, Chart.method)
CHART_SHEETS = {
    "Country JOCKEYS": ("country", "jockeys"),
    "Country JUKEBOX": ("country", "jukebox"),
    "Country RETAIL": ("country", "retail"),
    "R&B JUKEBOX": ("rnb", "jukebox"),
    "R&B RETAIL": ("rnb", "retail"),
}

# (category, method) -> [(printed_name, start_date, end_date_or_None, max_position), ...]
ERAS = {
    ("country", "jukebox"): [
        ("Most-Played Juke Box Folk Records", "1944-01-08", "1947-08-30", 9),
        ("Most-Played Juke Box Hillbilly Records", "1947-09-06", "1949-06-18", 15),
        ("Most-Played Juke Box Country & Western Records", "1949-06-25", None, 15),
    ],
    ("country", "retail"): [
        ("Best Selling Folk Retail Records", "1948-05-15", "1949-06-18", 15),
        ("Best Selling Retail Folk - Country & Western Records", "1949-06-25", None, 15),
    ],
    ("country", "jockeys"): [
        ("Country and Western Records Most Played by Folk Disc Jockeys", "1949-12-10", None, 10),
    ],
    ("rnb", "jukebox"): [
        ("Harlem Hit Parade", "1942-10-24", "1945-02-10", 10),
        ("Most-Played Juke Box Race Records", "1945-02-17", "1946-09-21", 8),
        ("Juke Box Race Records", "1946-09-28", "1949-06-18", 15),
        ("Juke Box Rhythm & Blues Records", "1949-06-25", None, 15),
    ],
    ("rnb", "retail"): [
        ("Best Selling Retail Race Records", "1948-05-22", "1949-06-18", 15),
        ("Best Selling Retail Rhythm & Blues Records", "1949-06-25", None, 15),
    ],
}

# (original_rec_id, reissue_rec_id) -- see docs/audit/other_issues.csv for sources
REISSUES = [
    ("Bluebird_33-0521B", "Victor_20-1864A"),  # Elton Britt, "Someday"
    ("Bluebird_10879A", "Victor_20-1977A"),  # Erskine Hawkins, "After Hours"
    ("Columbia_37942A", "Columbia_20377A"),  # Gene Autry, "Here Comes Santa Claus"
    ("De Luxe_1093A", "De Luxe_3093A"),  # Roy Brown, "Good Rockin' Tonight"
    ("Pacific_637A", "Four Star_1254A"),  # Ivory Joe Hunter, "Pretty Mama Blues"
    ("Exclusive_254B", "Exclusive_63XB"),  # Johnny Moore's Three Blazers, "Merry Christmas Baby"
]

# (sheet, date "YYYY-MM-DD", position, rec_id) -> note
#
# NOTE: rec_id is part of the key, not just (sheet, date, position) --
# position alone isn't unique within a chart-week once legitimate ties are
# allowed (see the ChartEntry.Meta constraint comment), so a
# position-only key would tag every recording tied at that position, not
# just the one the anomaly is actually about. Caught this the hard way on
# the first import run: R&B RETAIL 1950-02-18 #8 turned out to be a real
# 4-way tie, and 3 unrelated recordings got the Savoy 731A note before
# this was keyed by rec_id.
ANOMALIES = {
    ("R&B JUKEBOX", "1950-02-18", 2, "Savoy_731A"): (
        "Printed as Savoy 730, a catalog number that does not exist; artist billed as "
        "Little Esther though the recording is a combined Little Esther/Johnny Otis credit."
    ),
    ("R&B JUKEBOX", "1950-02-18", 10, "Savoy_731A"): (
        "Printed as Savoy 731 here and lists Johnny Otis; same recording as position 2 "
        "this week, printed twice under inconsistent catalog numbers/billing."
    ),
    ("R&B RETAIL", "1950-02-18", 3, "Savoy_731A"): (
        "Printed as Savoy 730, a catalog number that does not exist; artist billed as "
        "Little Esther though the recording is a combined Little Esther/Johnny Otis credit."
    ),
    ("R&B RETAIL", "1950-02-18", 8, "Savoy_731A"): (
        "Printed as Savoy 731 here and lists Johnny Otis; same recording as position 3 "
        "this week, printed twice under inconsistent catalog numbers/billing."
    ),
}


def normalize_catalog_number(value):
    # The workbook's "Label number" column is a mix of plain int (4366) and
    # str ("20-2700") depending on whether the real catalog number has a
    # hyphen -- normalize both to a clean string without turning an int
    # into something like "4366.0".
    if isinstance(value, int):
        return str(value)
    return str(value).strip()


def parse_past_number(value):
    # "Past Number" is either an int, the literal string "-" (debut/
    # re-entry -- no prior position), or blank (chart's very first week on
    # file, same operational meaning). Both collapse to None per the
    # ChartEntry.printed_last_week_position docstring.
    if value is None:
        return None
    if isinstance(value, float) and pd.isna(value):
        return None
    if isinstance(value, str):
        v = value.strip()
        if v in ("", "-"):
            return None
        return int(v)
    return int(value)


class Command(BaseCommand):
    help = "One-time import of the hand-transcribed chart workbook into the charts app."

    def add_arguments(self, parser):
        parser.add_argument("workbook", nargs="?", default="Bb Charts fixed.xlsx")

    def handle(self, *args, **options):
        path = Path(options["workbook"])
        if not path.exists():
            raise CommandError(f"Workbook not found: {path}")

        if ChartEntry.objects.exists():
            raise CommandError(
                "ChartEntry already has rows -- this command is for a one-time import "
                "into an empty database and refuses to run twice (it would create "
                "duplicate entries). Clear the tables first if you really want to redo it."
            )

        self.stdout.write(f"Reading {path} ...")
        xls = pd.ExcelFile(path)

        with transaction.atomic():
            self.import_charts_and_eras()
            label_by_name = self.import_labels(xls)
            song_by_title = self.import_songs(xls)
            artist_by_name = self.import_artists(xls)
            recording_by_rec_id = self.import_recordings(xls, label_by_name, song_by_title, artist_by_name)
            self.link_reissues(recording_by_rec_id)
            entry_count = self.import_chart_entries(xls, recording_by_rec_id)

        self.stdout.write(self.style.SUCCESS(
            f"Import complete: {len(label_by_name)} labels, {len(song_by_title)} songs, "
            f"{len(artist_by_name)} artists, {len(recording_by_rec_id)} recordings, "
            f"{entry_count} chart entries."
        ))

    def import_charts_and_eras(self):
        self.stdout.write("Creating charts and eras...")
        for (category, method), eras in ERAS.items():
            chart = Chart.objects.create(category=category, method=method, publication_weekday=5)  # 5 = Saturday
            for printed_name, start, end, max_position in eras:
                ChartEra.objects.create(
                    chart=chart,
                    printed_name=printed_name,
                    start_date=start,
                    end_date=end,
                    max_position=max_position,
                )

    def import_labels(self, xls):
        # Keyed by the workbook's own label_id, not by name -- see
        # import_recordings for why (a stale cached formula value was
        # found in Recordings' text columns; joining by ID instead of by
        # text sidesteps that whole class of problem).
        self.stdout.write("Importing labels...")
        df = xls.parse("Labels")
        df = df[df["Label"].notna()]
        by_id = {}
        for _, row in df.iterrows():
            by_id[row["label_id"]] = Label.objects.create(name=row["Label"])
        return by_id

    def import_songs(self, xls):
        self.stdout.write("Importing songs...")
        df = xls.parse("Songs")
        df = df[df["Song"].notna()]
        by_id = {}
        for _, row in df.iterrows():
            by_id[row["song_id"]] = Song.objects.create(title=row["Song"])
        return by_id

    def import_artists(self, xls):
        self.stdout.write("Importing artists...")
        df = xls.parse("Artists")
        df = df[df["Artist"].notna()]
        by_id = {}
        for _, row in df.iterrows():
            by_id[row["artist_id"]] = Artist.objects.create(canonical_name=row["Artist"])
        return by_id

    def import_recordings(self, xls, label_by_id, song_by_id, artist_by_id):
        # Joins by label_id/song_id/artist_id rather than by the
        # Recordings sheet's own Label/Song/Artist text columns. Those
        # text columns are Excel formulas (INDEX/MATCH against the master
        # sheets) and a stale cached value was found for one row (a song
        # title edited after the last recalculation) -- joining on the ID
        # columns instead reads the master sheets directly and can't go
        # stale that way.
        self.stdout.write("Importing recordings...")
        df = xls.parse("Recordings")
        df = df[df["rec_id"].notna()]
        by_rec_id = {}
        for _, row in df.iterrows():
            side = str(row["Side"]).strip() if pd.notna(row["Side"]) else ""
            artist = artist_by_id[row["artist_id"]]
            recording = Recording.objects.create(
                label=label_by_id[row["label_id"]],
                catalog_number=normalize_catalog_number(row["Label number"]),
                side=side,
                song=song_by_id[row["song_id"]],
                credit_text=artist.canonical_name,
            )
            RecordingCredit.objects.create(
                recording=recording,
                artist=artist,
                credit_order=1,
            )
            by_rec_id[row["rec_id"]] = recording
        return by_rec_id

    def link_reissues(self, recording_by_rec_id):
        self.stdout.write("Linking known reissues...")
        for original_id, reissue_id in REISSUES:
            original = recording_by_rec_id[original_id]
            reissue = recording_by_rec_id[reissue_id]
            reissue.reissue_of = original
            reissue.save(update_fields=["reissue_of"])

    def import_chart_entries(self, xls, recording_by_rec_id):
        self.stdout.write("Importing chart entries...")
        total = 0
        for sheet_name, (category, method) in CHART_SHEETS.items():
            chart = Chart.objects.get(category=category, method=method)
            df = xls.parse(sheet_name)
            for _, row in df.iterrows():
                week_date = row["Date"].date()
                position = int(row["Number"])
                anomaly = ANOMALIES.get((sheet_name, week_date.isoformat(), position, row["rec_id"]), "")
                entry = ChartEntry(
                    chart=chart,
                    week_date=week_date,
                    position=position,
                    recording=recording_by_rec_id[row["rec_id"]],
                    printed_last_week_position=parse_past_number(row["Past Number"]),
                    printed_anomaly_note=anomaly,
                )
                # full_clean() (not just save()) so the weekday and
                # position-range checks in ChartEntry.clean() actually run
                # -- see the NOTE in that method about save() not calling
                # it automatically.
                entry.full_clean()
                entry.save()
                total += 1
            self.stdout.write(f"  {sheet_name}: {len(df)} rows")
        return total
