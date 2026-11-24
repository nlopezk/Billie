"""
charts/recordings_matrix.py -- every row and number on the /recordings/ page.

Kept out of views.py for the same reason as dashboard.py: this is
aggregation logic (peak, weeks charted, first/last entry, the collapsed
one-row-per-recording mode), and aggregation is exactly what's worth
testing directly. The view hands the querystring in and gets a
template-ready dict back:

    context = build(request.GET)   # rows, columns, total_rows + the filter state

Weeks charted is ALWAYS Count("week_date", distinct=True) -- a calendar
week, never a row -- the same definition dashboard.py uses, so the two
pages agree.
"""
from urllib.parse import urlencode

from django.db.models import Count, Max, Min

from . import search as search_lib
from datetime import date

from .models import (
    Chart, ChartEntry, SpotifyExtraTrack, SpotifyMatch, discogs_url_for, format_catalog, sole_credited_artist_ids, spotify_url_for,
)


def build(params):
    """Everything the matrix page renders, from its querystring `params`."""
    rows, state = _query(params)
    return {
        **state,
        "rows": rows,
        "columns": _columns(state),
        "total_rows": len(rows),
    }


# Sort options for recordings_matrix, below. Maps the short key used in
# the URL (?sort=peak) to the (field, label) it orders by / displays --
# one place to add a sortable column instead of scattering the mapping
# across the view and template.
RECORDINGS_MATRIX_SORTS = {
    "song": ("recording__song__title", "Song"),
    "artist": ("recording__credit_text", "Artist"),
    "chart": ("chart__category", "Chart"),
    "peak": ("peak_position", "Peak"),
    "weeks": ("weeks_charted", "Weeks"),
    "first": ("first_entry", "First entry"),
    "last": ("last_entry", "Last entry"),
}

# The matrix's two grouping modes (?group=...), chosen with the "One row
# per" dropdown. See _query's docstring for what each
# one means and why neither is simply a nicer version of the other.
RECORDINGS_MATRIX_GROUPS = ("chart", "recording")

# In "recording" mode two columns genuinely change meaning, so they need
# a different sort field and a different header label. Everything else
# sorts and reads identically in both modes, which is why only these two
# are listed.
RECORDINGS_MATRIX_COLLAPSED_SORTS = {
    "chart": ("chart_count", "Charts"),
    "peak": ("peak_position", "Best peak"),
}

# Short badges for "recording" mode's Charts column, which may have to
# fit five chart names into one narrow cell: category initial + a
# two-letter method, so "CJb" is Country Jukebox. The full name rides
# along as the badge's hover title, so nothing is actually lost.
_CHART_CODE_METHODS = {"jockeys": "Jk", "jukebox": "Jb", "retail": "Rt"}


def _chart_code(chart):
    # Falls back to the first two letters of an unmapped method instead
    # of raising, so a chart added to the project later still renders.
    method = _CHART_CODE_METHODS.get(chart.method, chart.method[:2].title())
    return f"{chart.category[0].upper()}{method}"


def _query(params):
    """
    The filtered, sorted rows plus the current filter/sort/group state,
    from the page's querystring.

    TWO grouping modes, because they answer two different questions and
    neither is just a better default for the other:

    ?group=chart (the default) -- one row per CHART a recording appeared
    on. Peak/Weeks/First/Last are each scoped to that ONE chart, which is
    the only scope in which they are literally what the magazine printed:
    a record's peak on Country Jukebox and its peak on Country Retail are
    two unrelated numbers off two unrelated pages. Grouping by (chart_id,
    recording_id) in the .values() below is what keeps them that way.

    ?group=recording -- one row per RECORDING, however many charts it
    made, for reading down a genre without the same record appearing
    three times. Dropping the chart columns out of the .values() is the
    entire mechanism: SQL's GROUP BY gets coarser and re-aggregates
    across every chart. What that does to each number matters:

      * Peak becomes BEST peak -- the lowest position reached on ANY of
        its charts, labelled "Best peak" and shown with the chart that
        produced it. It is a DERIVED number, not a printed one (nobody
        ever printed a cross-chart peak), so per CLAUDE.md non-negotiable
        #4 it is named for what it is rather than quietly passed off as
        "the peak".
      * Weeks counts DISTINCT week_dates across every chart, so a week a
        record spent on three charts at once counts ONCE. Summing the
        per-chart weeks instead would claim 27 weeks for "Shot Gun
        Boogie", which charted for 10.
      * First/Last entry span every chart -- the earliest and latest week
        it appeared anywhere -- and each still links to the chart-week
        page it actually came from, which is why the per-chart breakdown
        query below exists.
    """
    genre = params.get("genre", "")
    chart_id = params.get("chart", "")
    q = params.get("q", "").strip()
    sort = params.get("sort", "peak")
    direction = params.get("dir", "asc")
    group = params.get("group", "chart")
    # Two scopes the matrix page itself never sets, used by an artist
    # profile's discography (charts/artists.py): only this artist's
    # recordings (by credit -- a collaboration counts for each act), and
    # only weeks inside the profile's date window.
    artist_id = params.get("artist", "")
    if not str(artist_id).isdigit():
        artist_id = ""
    date_from, date_to = _parse_date(params.get("from")), _parse_date(params.get("to"))
    # Two thresholds on the AGGREGATED numbers (added 2026-11-15 for the
    # playlist picker, shown on this page too): "peak N or better" and "at
    # least N weeks". Blank, zero or junk means no threshold, never an
    # error. "More than 3 weeks" is typed as 4: the box says "at least".
    max_peak = _parse_positive_int(params.get("peak"))
    min_weeks = _parse_positive_int(params.get("weeks"))
    # Only for the playlist page (charts/playlists.py): restrict the rows to
    # one playlist's recordings. Like `artist`, the matrix page never sets it.
    playlist_id = params.get("playlist", "")
    if not str(playlist_id).isdigit():
        playlist_id = ""
    if sort not in RECORDINGS_MATRIX_SORTS:
        sort = "peak"
    if group not in RECORDINGS_MATRIX_GROUPS:
        group = "chart"
    collapsed = group == "recording"

    entries = ChartEntry.objects.all()
    if genre:
        entries = entries.filter(chart__category=genre)
    if chart_id:
        entries = entries.filter(chart_id=chart_id)
    if artist_id:
        # A recording credits an artist at most once (unique_recording_artist),
        # so this join can't duplicate entries and inflate any count.
        entries = entries.filter(recording__credits__artist_id=artist_id)
    if date_from:
        entries = entries.filter(week_date__gte=date_from)
    if date_to:
        entries = entries.filter(week_date__lte=date_to)
    if playlist_id:
        # A playlist holds a recording at most once (unique_playlist_recording),
        # so, like the artist join above, this can't duplicate entries.
        entries = entries.filter(recording__playlist_items__playlist_id=playlist_id)
    if q:
        # The shared recording search (charts/search.py) -- "Capitol 304"
        # finds a Capitol release cataloged 304 here too.
        entries = entries.filter(search_lib.recording_text_q(q, prefix="recording__"))

    # The columns that identify a row -- and therefore ARE the SQL GROUP
    # BY. Everything here is per-recording except the three chart columns,
    # which are prepended only in "chart" mode.
    group_fields = [
        "recording_id", "recording__song__title", "recording__credit_text",
        "recording__label__name", "recording__catalog_number", "recording__side",
        # Discogs link state, for the matrix's status column. Pulled in
        # via the existing join rather than a second query, and entirely
        # from what's CACHED in our own database -- rendering this column
        # makes no API call. Recordings with no DiscogsMatch row yet come
        # back as None, which the template renders as an em-dash. All of
        # these are per-recording, so they stay valid collapsed too.
        "recording__discogs__status",
        "recording__discogs__discogs_release_id",
        "recording__discogs__discogs_master_id",
        "recording__discogs__matched_artist",
        "recording__discogs__matched_catalog_number",
        "recording__discogs__songwriters",
        # Spotify link state (added 2026-11-19), for the Spotify column --
        # also from our own cache, so the matrix still makes no API call.
        # Per-recording, so valid in both grouping modes.
        "recording__spotify__status",
        "recording__spotify__spotify_track_id",
    ]
    if not collapsed:
        group_fields = ["chart_id", "chart__category", "chart__method"] + group_fields

    aggregates = {
        "peak_position": Min("position"),
        # distinct=True on week_date, not a plain Count("id"): the 2
        # accepted printed-anomaly weeks (printed_anomaly_note / see
        # docs/audit/same_recording_repeated.csv) have the SAME recording
        # listed twice in the SAME week under two positions. A plain row
        # count would say those charted one extra week they did not.
        # Collapsed mode leans on this twice over -- there, a week spent
        # on three charts is three rows sharing one week_date, and
        # "Weeks" has to stay an answer to "how many calendar weeks".
        "weeks_charted": Count("week_date", distinct=True),
        "first_entry": Min("week_date"),
        "last_entry": Max("week_date"),
    }
    if collapsed:
        # Only meaningful once the chart columns are gone: in "chart"
        # mode every row IS one chart, so this would be the constant 1.
        aggregates["chart_count"] = Count("chart_id", distinct=True)

    rows = entries.values(*group_fields).annotate(**aggregates)
    # Filtering AFTER .annotate() makes these a SQL HAVING, so they test the
    # very numbers each row shows: the date window, genre and chart filters
    # above have already narrowed the entries those numbers come from. That
    # is what makes them a PEAK IN RANGE (GLOSSARY.md): a record at #1 in
    # 1949 and #9 in 1950 has peak 9 inside a 1950 window, so "1950, peak 5
    # or better" leaves it out. In one-row-per-chart mode the test is per
    # chart; collapsed, it is the best peak / distinct weeks across charts.
    if max_peak:
        rows = rows.filter(peak_position__lte=max_peak)
    if min_weeks:
        rows = rows.filter(weeks_charted__gte=min_weeks)

    order_field, _ = RECORDINGS_MATRIX_SORTS[sort]
    if collapsed and sort in RECORDINGS_MATRIX_COLLAPSED_SORTS:
        # chart__category is not in the collapsed GROUP BY at all --
        # ordering by it would drag the chart join back in and silently
        # un-collapse the rows. "Charts" sorts by how many instead.
        order_field, _ = RECORDINGS_MATRIX_COLLAPSED_SORTS[sort]
    if direction == "desc":
        order_field = "-" + order_field
    # chart__method as a SECOND sort key specifically when sorting by
    # Chart, so same-genre charts cluster together in a stable order
    # (Jockeys/Jukebox/Retail) instead of an arbitrary one; song title as
    # the final tiebreaker always, so rows sharing a value on the primary
    # sort come back in a stable, readable order across reloads.
    tiebreakers = ["recording__song__title"]
    if sort == "chart" and not collapsed:
        tiebreakers = ["chart__method"] + tiebreakers
    rows = list(rows.order_by(order_field, *tiebreakers))

    # .values()/.annotate() returns plain dicts, not Recording/Chart
    # instances -- so the catalog string comes from models.format_catalog
    # (the same function behind Recording.catalog_display) and the chart
    # labels are built once here. Ordered by
    # (category, method) so chart_order below matches the order charts
    # are listed in everywhere else in the app.
    charts_by_id = {c.id: c for c in Chart.objects.order_by("category", "method")}
    chart_labels = {cid: str(c) for cid, c in charts_by_id.items()}
    chart_order = {cid: i for i, cid in enumerate(charts_by_id)}

    breakdown = {}
    if collapsed:
        # ONE extra aggregate query (not one per row), re-grouped by
        # (recording, chart), to recover exactly what collapsing threw
        # away: WHICH chart each merged number came from. That is needed
        # for the Charts badges, for naming the chart behind "best peak",
        # and so First/Last entry can still link to the real chart-week
        # page instead of becoming dead text.
        for item in (
            entries
            .values("recording_id", "chart_id")
            .annotate(chart_peak=Min("position"), chart_first=Min("week_date"), chart_last=Max("week_date"))
        ):
            breakdown.setdefault(item["recording_id"], []).append(item)
        for items in breakdown.values():
            items.sort(key=lambda i: chart_order.get(i["chart_id"], len(chart_order)))

    for row in rows:
        if collapsed:
            per_chart = breakdown.get(row["recording_id"], [])
            row["chart_badges"] = [
                {
                    "code": _chart_code(charts_by_id[i["chart_id"]]),
                    "label": chart_labels[i["chart_id"]],
                    "chart_id": i["chart_id"],
                    "peak": i["chart_peak"],
                }
                for i in per_chart
            ]
            # Which chart produced the best peak, and which produced the
            # first and the last appearance. per_chart is already in
            # chart display order and min()/next() both take the FIRST
            # match, so a tie (the same peak on two charts, or two charts
            # debuting it the same week) resolves stably rather than
            # landing on whatever order SQL happened to return.
            best = min(per_chart, key=lambda i: i["chart_peak"], default=None)
            row["peak_chart_code"] = _chart_code(charts_by_id[best["chart_id"]]) if best else ""
            row["peak_chart_label"] = chart_labels[best["chart_id"]] if best else ""
            first_source = next((i for i in per_chart if i["chart_first"] == row["first_entry"]), None)
            last_source = next((i for i in per_chart if i["chart_last"] == row["last_entry"]), None)
            # first_chart_id/last_chart_id exist in BOTH modes (in "chart"
            # mode they are just this row's own chart), so the template
            # links a date the same way either way.
            row["first_chart_id"] = first_source["chart_id"] if first_source else None
            row["last_chart_id"] = last_source["chart_id"] if last_source else None
        else:
            row["chart_label"] = chart_labels[row["chart_id"]]
            row["first_chart_id"] = row["chart_id"]
            row["last_chart_id"] = row["chart_id"]
        row["catalog_display"] = format_catalog(
            row["recording__label__name"], row["recording__catalog_number"], row["recording__side"],
        )
        # Plain outbound discogs.com URL, built from the cached id -- a
        # string, not a request (see DiscogsReferenceMixin for release vs
        # master). Blank when there is no link at all yet.
        release_id = row["recording__discogs__discogs_release_id"]
        master_id = row["recording__discogs__discogs_master_id"]
        row["discogs_ref_id"] = release_id or master_id
        row["discogs_ref_type"] = "Release" if release_id else ("Master" if master_id else "")
        row["discogs_url"] = discogs_url_for(release_id, master_id)
        # One combined hover tooltip rather than a second visible column
        # -- the matrix is already tight on width (see the earlier "song
        # extremely wide" fix), so Discogs' own Artist credit is here for
        # a quick "does this match" sanity check on hover, not as
        # something that needs its own column.
        tooltip_lines = []
        if row["recording__discogs__matched_artist"]:
            tooltip_lines.append(f"Artist: {row['recording__discogs__matched_artist']}")
        if row["recording__discogs__songwriters"]:
            tooltip_lines.append(f"Written by: {row['recording__discogs__songwriters']}")
        row["discogs_tooltip"] = "\n".join(tooltip_lines) or "No details cached yet"
        # Outbound open.spotify.com link, only for a linked match (confirmed,
        # manual or stand-in) -- a suggestion's track id is never set.
        row["spotify_url"] = (
            spotify_url_for(row["recording__spotify__spotify_track_id"])
            if row["recording__spotify__status"] in SpotifyMatch.LINKED_STATUSES else ""
        )

    # Each row's printed credit links to its artist's profile when exactly
    # ONE artist is credited; collaborations stay plain text.
    sole_artist = sole_credited_artist_ids([row["recording_id"] for row in rows])
    # How many extra Spotify PARTS each linked recording has (a recording
    # split over several tracks), for the Spotify cell's "+1 part" badge.
    # One grouped query for the whole page, not one per row.
    spotify_parts = spotify_part_counts([row["recording_id"] for row in rows if row["spotify_url"]])
    for row in rows:
        row["artist_id"] = sole_artist.get(row["recording_id"])
        row["spotify_parts"] = spotify_parts.get(row["recording_id"], 0)

    state = {
        "genre": genre, "chart": chart_id, "q": q, "sort": sort, "dir": direction, "group": group,
        "artist": artist_id,
        "from": date_from.isoformat() if date_from else "",
        "to": date_to.isoformat() if date_to else "",
        "peak": str(max_peak or ""), "weeks": str(min_weeks or ""),
        # True when the date window actually cuts something off, so the
        # headers can say "Peak in range" / "Weeks in range" instead of
        # passing a windowed number off as the all-time one. A window that
        # covers every week on file (the slider's default) is no window.
        "windowed": _is_windowed(date_from, date_to),
    }
    return rows, state


def _columns(state):
    # Build each sortable column header's link here rather than in the
    # template: its target direction depends on whether IT is the
    # currently-active sort column (click again to flip asc/desc) or not
    # (click switches to it, defaulting to asc), AND it has to carry the
    # CURRENT genre/chart/q filters and the grouping mode forward too, or
    # sorting would silently drop whatever you had just filtered by (or
    # bounce a collapsed view back to one-row-per-chart). Keyed by sort
    # key so the template can place each header exactly where it wants
    # it, e.g. {{ columns.peak.href }}, rather than looping in a fixed
    # order.
    collapsed = state["group"] == "recording"
    columns = {}
    for key, (_, label) in RECORDINGS_MATRIX_SORTS.items():
        if collapsed and key in RECORDINGS_MATRIX_COLLAPSED_SORTS:
            # "Peak" -> "Best peak", "Chart" -> "Charts": the header says
            # what the collapsed number actually is.
            _, label = RECORDINGS_MATRIX_COLLAPSED_SORTS[key]
        if state.get("windowed") and key in ("peak", "weeks"):
            # Inside a date window both numbers only count weeks in it.
            label = f"{label} in range"
        is_active = key == state["sort"]
        next_dir = "desc" if (is_active and state["dir"] == "asc") else "asc"
        params = {
            "genre": state["genre"], "chart": state["chart"], "q": state["q"],
            "group": state["group"], "sort": key, "dir": next_dir,
            # Carried too, so sorting an artist profile's discography keeps
            # its date window (empty -- and so left out -- on the matrix page).
            "from": state["from"], "to": state["to"],
            "peak": state.get("peak", ""), "weeks": state.get("weeks", ""),
        }
        columns[key] = {
            "label": label,
            "href": "?" + urlencode({k: v for k, v in params.items() if v}),
            "is_active": is_active,
            "arrow": ("▲" if state["dir"] == "asc" else "▼") if is_active else "",
        }
    return columns


def spotify_part_counts(recording_ids):
    """{recording_id: number of extra Spotify parts} for the given
    recordings, leaving out those with none. Shared with the playlist page."""
    if not recording_ids:
        return {}
    return dict(
        SpotifyExtraTrack.objects.filter(match__recording_id__in=recording_ids, kind=SpotifyExtraTrack.PART)
        .values_list("match__recording_id").annotate(n=Count("id")).values_list("match__recording_id", "n")
    )


def _parse_positive_int(value):
    # A whole number >= 1, or None -- a blank, zero, negative or non-numeric
    # box just means "no threshold".
    value = str(value or "").strip()
    return int(value) if value.isdigit() and int(value) > 0 else None


def _is_windowed(date_from, date_to):
    # Does the from/to window leave out any week on file? One small
    # aggregate query; no window at all is never "windowed".
    if not (date_from or date_to):
        return False
    span = ChartEntry.objects.aggregate(first=Min("week_date"), last=Max("week_date"))
    if span["first"] is None:
        return False
    return bool((date_from and date_from > span["first"]) or (date_to and date_to < span["last"]))


def _parse_date(value):
    # An ISO date, or None for a missing or malformed one -- a bad value
    # just means "no date limit", never an error page.
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return None

