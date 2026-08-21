# billboard/charts/models.py
#
# Core relational shape: chart -> chart_entry -> recording -> artist,
# with a chart_era table so a chart's printed name can change over the
# decades without treating renames as separate charts, and a
# recording_credit join table so a recording can list more than one
# artist (the old workbook's " - "-delimited Artist string is replaced
# by one row per credited artist, with billing order preserved).
#
# Design record: docs/models_draft.py in the repo root is the dated design
# doc this file was built from -- the comments here are the live version,
# kept in sync as the schema evolves. CLAUDE.md's "Current schema" section
# points at docs/schema.sql (regenerated after every migration), not here,
# for the actual applied SQL.

from django.db import models
from django.core.exceptions import ValidationError


class Label(models.Model):
    # A record label ("Decca", "Capitol", ...). Flat lookup table, no
    # history needed -- labels don't get renamed the way charts do.
    name = models.CharField(max_length=100, unique=True)

    def __str__(self):
        return self.name


class Artist(models.Model):
    # The CANONICAL artist. What's actually credited on a given release
    # goes on RecordingCredit / Recording.credit_text instead -- this row
    # is the thing autocomplete resolves to and the thing aliases point at.
    canonical_name = models.CharField(max_length=200)

    def __str__(self):
        return self.canonical_name


class ArtistAlias(models.Model):
    # Every spelling of this artist ever seen in print points here, e.g.
    # "Conway Twitty", "Conway Twitty & Loretta Lynn" -> canonical Conway
    # Twitty (for the solo alias) plus a *separate* Artist row for the duo
    # if the duo is credited often enough to deserve its own canonical
    # entry (that's a judgment call the UI leaves to you -- see the
    # "never auto-merge" rule in CLAUDE.md).
    artist = models.ForeignKey(Artist, on_delete=models.CASCADE, related_name="aliases")
    alias_text = models.CharField(max_length=200, unique=True)  # unique globally: one alias -> one artist

    def __str__(self):
        return self.alias_text


class Song(models.Model):
    # Canonical song title. Kept separate from Recording because the same
    # song is re-recorded by multiple artists across chart history.
    title = models.CharField(max_length=300)

    def __str__(self):
        return self.title


class Recording(models.Model):
    # One row per physical release: a specific label + catalog number +
    # side. This is the thing a chart position actually points at.
    label = models.ForeignKey(Label, on_delete=models.PROTECT, related_name="recordings")
    catalog_number = models.CharField(max_length=30)  # CharField: numbers like "20-2700" aren't integers
    # 'A' / 'B' normally, blank if unknown/not applicable, or 'AB' for a
    # song listed as spanning both sides (e.g. a "Parts I and II" release
    # catalogued as one listing rather than two separate A/B recordings).
    side = models.CharField(max_length=4, blank=True)
    song = models.ForeignKey(Song, on_delete=models.PROTECT, related_name="recordings")

    # The credit EXACTLY as printed on the chart, e.g. "Bing Crosby -
    # Andrews Sisters". Non-negotiable #4: the magazine is the source of
    # truth, so this raw text is never derived/regenerated from
    # RecordingCredit -- it's what you typed from the page, kept verbatim.
    credit_text = models.CharField(max_length=300)

    # If this release is a straight reissue of an earlier release (same
    # performance/masters, repackaged under a new label/catalog number --
    # e.g. Bluebird 33-0521B reissued as RCA Victor 20-1864A), point at
    # that original Recording row. Left null for original issues and for
    # genuinely separate recordings that just happen to share the same
    # Song and Artist (different session/arrangement, e.g. Lionel
    # Hampton's 1940 Victor small-group "Flying Home" vs. his 1942 Decca
    # big-band version) -- those need no special marker, since two
    # independent Recording rows sharing a Song/Artist is already exactly
    # correct for that case.
    reissue_of = models.ForeignKey(
        "self", null=True, blank=True, on_delete=models.SET_NULL, related_name="reissues"
    )

    class Meta:
        # Catches "same release entered twice under different rec_ids"
        # at insert time, the way the old rec_id string key did informally.
        constraints = [
            models.UniqueConstraint(
                fields=["label", "catalog_number", "side"], name="unique_label_catalog_side"
            )
        ]

    @property
    def catalog_display(self):
        # "Label CatalogNumber Side", e.g. "MGM 10645 A" -- formatting
        # only, computed on read. label/catalog_number/side stay separate,
        # unformatted fields in the database (that's what
        # unique_label_catalog_side is built on) -- never bake display
        # formatting into stored data.
        parts = [self.label.name, self.catalog_number]
        if self.side:
            parts.append(self.side)
        return " ".join(parts)

    def __str__(self):
        return f"{self.credit_text} — {self.song} ({self.catalog_display})"


class RecordingCredit(models.Model):
    # The many-to-many join that replaces the old " - "-delimited Artist
    # string. One row per artist actually credited on a recording.
    recording = models.ForeignKey(Recording, on_delete=models.CASCADE, related_name="credits")
    artist = models.ForeignKey(Artist, on_delete=models.PROTECT, related_name="recording_credits")
    # Billing order as printed -- "Bing Crosby - Andrews Sisters" has
    # Crosby at credit_order=1, Andrews Sisters at credit_order=2.
    credit_order = models.PositiveSmallIntegerField(default=1)

    class Meta:
        constraints = [
            # An artist can't be credited twice on the same recording...
            models.UniqueConstraint(fields=["recording", "artist"], name="unique_recording_artist"),
            # ...and two artists can't claim the same billing slot.
            models.UniqueConstraint(fields=["recording", "credit_order"], name="unique_recording_credit_order"),
        ]
        ordering = ["credit_order"]


# =============================================================================
# Discogs linking -- OPTIONAL, after-the-fact enrichment. Nothing below is
# part of the chart-entry path.
# =============================================================================
#
# Why a separate model instead of fields on Recording: this data is
# optional, separately populated, separately corrected, and comes from an
# outside source that can be wrong. Keeping it off Recording means the
# entry form's hot-path model is untouched by any of it, and a Recording
# with no Discogs link at all is still a perfectly complete row.
#
# Note the granularity mismatch, which is expected and fine: a Recording
# is ONE SIDE of a disc (Victor 20-1873 A and B are two Recording rows),
# while a Discogs *Release* is the whole disc. So the A and B sides
# legitimately share a discogs_release_id. Songwriter/producer credits
# are per-TRACK though, so those get pulled by finding this side's track
# within the release's tracklist (see charts/discogs.py), falling back to
# release-level credits when the track can't be identified.


class DiscogsReferenceMixin:
    """
    Shared by DiscogsMatch and DiscogsCandidate: both carry the same pair
    of "which kind of Discogs object is this actually pointing at" fields
    (discogs_release_id / discogs_master_id), and the same rule for
    telling a reader which one is in play -- a Release (a specific,
    identified pressing -- the precise, common case, since search only
    ever looks for releases) or, only when a pressing that specific
    couldn't be pinned down, a Master (just "this is the song", not which
    physical copy of it). At most one is ever set at a time.
    """

    @property
    def reference_type(self):
        if self.discogs_release_id:
            return "Release"
        if self.discogs_master_id:
            return "Master"
        return ""

    @property
    def reference_id(self):
        return self.discogs_release_id or self.discogs_master_id

    @property
    def discogs_url(self):
        # Plain outbound link for eyeballing this on discogs.com. Just a
        # URL string -- rendering it makes no API call.
        if self.discogs_release_id:
            return f"https://www.discogs.com/release/{self.discogs_release_id}"
        if self.discogs_master_id:
            return f"https://www.discogs.com/master/{self.discogs_master_id}"
        return ""


class DiscogsMatch(DiscogsReferenceMixin, models.Model):
    """One Recording's link (or deliberate non-link) to Discogs."""

    # --- status: where this recording is in the review workflow ---
    UNMATCHED = "unmatched"    # never looked up
    SUGGESTED = "suggested"    # candidates fetched, waiting on a human to decide
    CONFIRMED = "confirmed"    # a human picked one of the suggested candidates
    MANUAL = "manual"          # a human typed/pasted a Discogs ID directly
    NO_MATCH = "no_match"      # searched, Discogs returned nothing plausible
    REJECTED = "rejected"      # a human looked at the candidates and said none of them are it

    STATUS_CHOICES = [
        (UNMATCHED, "Not looked up yet"),
        (SUGGESTED, "Suggested -- needs review"),
        (CONFIRMED, "Confirmed"),
        (MANUAL, "Manually entered"),
        (NO_MATCH, "No match found on Discogs"),
        (REJECTED, "Rejected -- none of the suggestions fit"),
    ]
    # Statuses that mean "a human has settled this one" -- used by the
    # review queue to decide what still needs attention.
    SETTLED_STATUSES = [CONFIRMED, MANUAL, NO_MATCH, REJECTED]

    # --- how good the machine thought the match was ---
    # Recorded so the review page can offer "bulk-confirm the exact ones"
    # without re-deriving it, and so a weak match stays visibly weak even
    # after it's been confirmed.
    EXACT = "exact"      # label AND catalog number both matched (normalized)
    STRONG = "strong"    # catalog number matched exactly, label didn't
    WEAK = "weak"        # only found via the title/artist fallback search
    CONFIDENCE_CHOICES = [(EXACT, "Exact"), (STRONG, "Strong"), (WEAK, "Weak")]

    recording = models.OneToOneField(Recording, on_delete=models.CASCADE, related_name="discogs")
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default=UNMATCHED)
    confidence = models.CharField(max_length=6, choices=CONFIDENCE_CHOICES, blank=True)

    discogs_release_id = models.PositiveIntegerField(null=True, blank=True)
    discogs_master_id = models.PositiveIntegerField(null=True, blank=True)

    # --- cached snapshot of what Discogs says ---
    # Cached, not fetched live, so the recordings matrix and review queue
    # render with zero network calls. Refreshed only when a human asks.
    #
    # matched_title/matched_artist are the RELEASE's own -- the whole
    # disc, e.g. "When The Lights Go On Again / That's All" credited to
    # BOTH sides' artists combined. matched_track_title/matched_track_
    # artist are the ones that actually correspond to THIS Recording (one
    # side): Discogs' own tracklist entry for that side, where
    # identifiable (charts.discogs._find_track). A single credited to two
    # different acts, one per side, is exactly why this distinction
    # matters -- the release-level artist can include a name that never
    # appears on this side at all.
    matched_title = models.CharField(max_length=300, blank=True)
    matched_artist = models.CharField(max_length=300, blank=True)
    matched_track_title = models.CharField(max_length=300, blank=True)
    matched_track_artist = models.CharField(max_length=300, blank=True)
    matched_label = models.CharField(max_length=200, blank=True)
    matched_catalog_number = models.CharField(max_length=100, blank=True)
    matched_year = models.PositiveSmallIntegerField(null=True, blank=True)
    matched_country = models.CharField(max_length=100, blank=True)
    matched_format = models.CharField(max_length=200, blank=True)
    genres = models.JSONField(default=list, blank=True)
    styles = models.JSONField(default=list, blank=True)

    # --- the actual payoff: what Billboard never printed ---
    songwriters = models.CharField(max_length=500, blank=True)
    producers = models.CharField(max_length=500, blank=True)

    # Whole API response kept verbatim. Costs a little disk, but means
    # surfacing a new field later is a template change rather than
    # re-fetching all ~1,191 recordings over the API again.
    raw_data = models.JSONField(null=True, blank=True)

    # Free-text, for the human -- "this is the 1949 reissue, not the
    # original", "Discogs has the B-side title wrong", etc.
    note = models.CharField(max_length=500, blank=True)

    searched_at = models.DateTimeField(null=True, blank=True)   # last time candidates were fetched
    # When the CURRENT link was established (confirmed or manually
    # entered). Distinct from updated_at, which is auto_now and so gets
    # bumped by ANY later save -- adding a note, re-fetching details,
    # a schema backfill. That makes updated_at useless for "show me what
    # I linked most recently, so I can check the one I think I got
    # wrong", which is exactly what the audit view sorts by.
    # Re-linking updates it; resetting clears it back to null.
    linked_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name_plural = "Discogs matches"
        indexes = [models.Index(fields=["status"])]

    @property
    def is_settled(self):
        # "A human has made a decision about this one" -- drives the
        # review queue's default filter and the progress count.
        return self.status in self.SETTLED_STATUSES

    @property
    def is_linked(self):
        # Points at an actual Discogs record -- a Release OR (for a
        # manually-entered reference that could only be pinned down to
        # the work, not the specific pressing) a Master -- as opposed to
        # settled-but-deliberately-unlinked (no_match / rejected).
        return self.status in (self.CONFIRMED, self.MANUAL) and bool(self.reference_id)

    @property
    def needs_detail(self):
        # Linked AND specifically has a Release id -- the expensive
        # per-release fetch (charts.discogs.fetch_release_detail) hits
        # Discogs' /releases/ endpoint, which needs an actual release,
        # not a master. A master-only manual match has nothing this can
        # fetch, so it deliberately never shows as needing it.
        return self.is_linked and bool(self.discogs_release_id) and self.raw_data is None

    @property
    def has_ambiguous_exact_tie(self):
        # More than one candidate scored EXACT -- the same catalog+label
        # matches more than one distinct Discogs release (a US and a
        # Canadian pressing sharing a catalog series, an original and a
        # same-numbered reissue, etc. -- see the reasoning in
        # charts.discogs.search_candidates). Bulk-confirm refuses to
        # guess between these; this is what the review row uses to
        # explain why a manual pick is needed here specifically.
        return sum(1 for c in self.candidates.all() if c.confidence == self.EXACT) > 1

    def __str__(self):
        return f"{self.recording} -> {self.get_status_display()}"


class DiscogsCandidate(DiscogsReferenceMixin, models.Model):
    """
    One of the handful of possible Discogs releases turned up for a
    recording, held until a human picks one (or rejects them all).

    Deliberately a real table rather than a JSON list on DiscogsMatch:
    it matches the house style (ChartEra, RecordingCredit and ArtistAlias
    are all proper tables too), and the review page can order/filter these
    directly. Rows are transient -- refetching a recording's candidates
    replaces them, and confirming one copies its snapshot up onto the
    DiscogsMatch before they're cleared.

    Only SUMMARY data from the search endpoint lives here. The expensive
    per-release detail fetch (tracklist, songwriters, producers) happens
    once, at confirm time, for the one candidate that was actually
    chosen -- not for every candidate of every recording, which would be
    thousands of extra API calls for data mostly destined to be discarded.
    """

    match = models.ForeignKey(DiscogsMatch, on_delete=models.CASCADE, related_name="candidates")
    discogs_release_id = models.PositiveIntegerField(null=True, blank=True)
    discogs_master_id = models.PositiveIntegerField(null=True, blank=True)

    title = models.CharField(max_length=300, blank=True)
    artist = models.CharField(max_length=300, blank=True)
    label = models.CharField(max_length=200, blank=True)
    catalog_number = models.CharField(max_length=100, blank=True)
    year = models.PositiveSmallIntegerField(null=True, blank=True)
    country = models.CharField(max_length=100, blank=True)
    format = models.CharField(max_length=200, blank=True)
    thumb_url = models.URLField(max_length=500, blank=True)

    confidence = models.CharField(max_length=6, choices=DiscogsMatch.CONFIDENCE_CHOICES, blank=True)
    rank = models.PositiveSmallIntegerField(default=0)  # display order, best first
    raw_data = models.JSONField(null=True, blank=True)

    class Meta:
        ordering = ["rank"]

    def __str__(self):
        return f"{self.title} ({self.label} {self.catalog_number})"


class Chart(models.Model):
    # One row per continuous chart LINEAGE, e.g. "Country Jukebox" -- not
    # per printed name. Billboard renamed this chart three times between
    # 1944 and 1954; those renames live in ChartEra below, not here.
    CATEGORY_CHOICES = [("country", "Country"), ("rnb", "R&B")]
    METHOD_CHOICES = [("jockeys", "Jockeys/Radio"), ("jukebox", "Jukebox"), ("retail", "Retail")]

    category = models.CharField(max_length=10, choices=CATEGORY_CHOICES)
    method = models.CharField(max_length=10, choices=METHOD_CHOICES)
    # Day of the week the issue's cover date falls on, almost always
    # Saturday for this era -- validated against chart_entry.week_date
    # in application code (see ChartEntry.clean()).
    publication_weekday = models.PositiveSmallIntegerField(default=5)  # 5 = Saturday, Monday=0

    class Meta:
        constraints = [models.UniqueConstraint(fields=["category", "method"], name="unique_chart_category_method")]

    def __str__(self):
        return f"{self.get_category_display()} {self.get_method_display()}"


class ChartEra(models.Model):
    # A chart's printed name for a date range. start_date/end_date bound
    # the era; end_date null means "current" (or "last era on file").
    # Also carries the chart's declared size for that era, since Billboard
    # expanded these charts from ~10-15 positions up to 100 over the
    # decades -- position-in-range validation reads this, not a fixed
    # DB constant. Chart size is also allowed to vary week-to-week within
    # an era (charts don't always fill every slot, and can "overflow"
    # past max_position when the very last position is itself a tie) --
    # max_position is a ceiling for validation, not a requirement that
    # every week reach it.
    chart = models.ForeignKey(Chart, on_delete=models.CASCADE, related_name="eras")
    printed_name = models.CharField(max_length=200)  # e.g. "Most-Played Juke Box Folk Records"
    start_date = models.DateField()
    end_date = models.DateField(null=True, blank=True)
    max_position = models.PositiveSmallIntegerField()

    def __str__(self):
        return self.printed_name


class ChartEntry(models.Model):
    # One row per position per week. The heart of the schema.
    chart = models.ForeignKey(Chart, on_delete=models.PROTECT, related_name="entries")
    week_date = models.DateField()  # ISO week-ENDING date, per CLAUDE.md convention
    position = models.PositiveSmallIntegerField()
    recording = models.ForeignKey(Recording, on_delete=models.PROTECT, related_name="chart_entries")

    # --- printed values (what the magazine actually says) ---
    # Null covers both "-" (debut/re-entry, no prior position) and "chart
    # just started, no prior week exists at all" -- operationally the same
    # case: nothing to resolve a carryover from.
    printed_last_week_position = models.PositiveSmallIntegerField(null=True, blank=True)
    # Only some eras/charts print these columns at all; null = not printed.
    printed_peak_position = models.PositiveSmallIntegerField(null=True, blank=True)
    printed_weeks_on_chart = models.PositiveSmallIntegerField(null=True, blank=True)

    # A handful of import-time exceptions are known, deliberate departures
    # from a clean 1-recording-1-slot-per-week chart -- e.g. the two 1950
    # cases where Billboard itself printed "Double Crossing Blues" / Savoy
    # 731 twice in the same week under inconsistent catalog numbers and
    # artist billing (see docs/audit/same_recording_repeated.csv). Rather
    # than let the UNIQUE constraint or the "no repeated recording in a
    # week" rule silently reject or dedupe these, they're imported as-is
    # with this flag set, so they read as "verified against the magazine,
    # kept intentionally" rather than as a bug that slipped through.
    printed_anomaly_note = models.CharField(max_length=300, blank=True)

    class Meta:
        constraints = [
            # NOTE: this is UNIQUE(chart, week_date, position, RECORDING),
            # not just (chart, week_date, position) as CLAUDE.md's example
            # SQL originally showed. The audit (docs/audit/position_skip_review.csv)
            # established that competition/skip-ranking TIES are the normal
            # convention in this data -- two different recordings sharing
            # one printed position in the same week is legitimate and
            # common (1,499 tie groups found), not an error. Including
            # `recording` in the constraint is what tells those two cases
            # apart: it still catches an exact duplicate row (same
            # recording, same position, same week -- a real double-save),
            # while allowing a genuine tie (same position, two DIFFERENT
            # recordings) through.
            models.UniqueConstraint(
                fields=["chart", "week_date", "position", "recording"], name="unique_chart_week_position_recording"
            ),
        ]
        indexes = [models.Index(fields=["chart", "week_date"])]
        verbose_name_plural = "chart entries"

    def clean(self):
        # Application-level checks that can't be plain SQL CHECK
        # constraints because SQLite can't reference another table from one:
        # (1) week_date must land on the chart's publication weekday,
        # (2) position must be within the era's declared max_position
        #     for that week.
        #
        # NOTE: Django does not call clean()/full_clean() automatically on
        # save() -- only ModelForm.is_valid() does that for you (so the
        # weekly entry form and Django admin both get this for free). Any
        # code that creates ChartEntry rows directly, like the one-time
        # import management command, must call full_clean() itself before
        # save() or these checks are silently skipped.
        if self.chart_id and self.week_date:
            if self.week_date.weekday() != self.chart.publication_weekday:
                raise ValidationError("week_date is not on this chart's publication weekday.")
            era = self.chart.eras.filter(start_date__lte=self.week_date).filter(
                models.Q(end_date__isnull=True) | models.Q(end_date__gte=self.week_date)
            ).first()
            if era and self.position > era.max_position:
                raise ValidationError(f"position {self.position} exceeds chart size {era.max_position} for this era.")

    def __str__(self):
        return f"{self.chart} {self.week_date} #{self.position}"

    # --- computed fields, exposed as properties, never stored ---
    # weeks_on = previous week's weeks_on + 1; peak = MIN(previous peak, this position).
    # These are computed on read (e.g. in the entry-form view) and compared
    # against printed_peak_position / printed_weeks_on_chart so a mismatch
    # can be surfaced in the UI -- never auto-corrected, per CLAUDE.md.
