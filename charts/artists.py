"""
charts/artists.py -- every row and number on the artist pages:
the artist browser (/artists/) and an artist profile (/artists/<id>/).

Like dashboard.py and recordings_matrix.py, this is counting logic kept
out of views.py so it can be tested directly; the views hand the
querystring in and get a template-ready dict back:

    context = browse(request.GET)            # the browser
    context = profile(artist, request.GET)   # one artist's profile

WHICH RECORDS ARE AN ARTIST'S: every recording they're CREDITED on
(RecordingCredit), never a match on the printed credit text. So a
collaboration counts in full for each act in it ("Ernest Tubb - Red Foley"
is on both profiles), and a billing like "Julia Lee And Her Boy Friends"
counts for Julia Lee. Same definition as the dashboard's Top artists.

Every number uses the dashboard's definitions (dashboard.py docstring):
a week is a CALENDAR week, so a week on three charts at once counts once,
and ties credit everyone. A profile and the dashboard therefore agree.
"""
from collections import defaultdict
from datetime import date
from urllib.parse import urlencode

from django.db.models import Count, Max, Min, Q

from . import dashboard as dashboard_lib
from . import recordings_matrix as recordings_matrix_lib
from .models import Artist, Chart, ChartEntry, RecordingCredit

# The browser's sortable columns: URL key -> (row key, header label).
BROWSE_SORTS = {
    "name": ("name", "Artist"),
    "weeks": ("weeks_on", "Weeks"),
    "records": ("records", "Records"),
    "peak": ("peak", "Best peak"),
    "first": ("first", "First week"),
    "last": ("last", "Last week"),
}
# Sorting by these reads best biggest-first, so a click starts there.
_DESCENDING_FIRST = {"weeks", "records", "first", "last"}


# --------------------------------------------------------------- browser

def browse(params):
    """The artist browser: every artist with at least one chart entry in
    scope, filtered by genre / chart / search text, sorted, plus the state
    the page's filters and column headers need."""
    q = params.get("q", "").strip()
    genre = params.get("genre", "")
    if genre not in dict(Chart.CATEGORY_CHOICES):
        genre = ""
    chart_id = params.get("chart", "")
    if not chart_id.isdigit():
        chart_id = ""
    sort = params.get("sort", "weeks")
    if sort not in BROWSE_SORTS:
        sort = "weeks"
    direction = params.get("dir", "desc" if sort in _DESCENDING_FIRST else "asc")
    if direction not in ("asc", "desc"):
        direction = "asc"

    entries = ChartEntry.objects.all()
    if genre:
        entries = entries.filter(chart__category=genre)
    if chart_id:
        entries = entries.filter(chart_id=chart_id)
    if q:
        entries = entries.filter(recording__credits__artist_id__in=_search_artist_ids(q))

    # One grouped query for every artist's numbers, through their credits.
    rows = []
    for row in (
        entries.values("recording__credits__artist_id", "recording__credits__artist__canonical_name")
        .annotate(
            weeks_on=Count("week_date", distinct=True),
            records=Count("recording_id", distinct=True),
            peak=Min("position"),
            first=Min("week_date"),
            last=Max("week_date"),
        )
    ):
        if row["recording__credits__artist_id"] is None:
            continue  # defensive: every recording has a credit today
        rows.append({
            "id": row["recording__credits__artist_id"],
            "name": row["recording__credits__artist__canonical_name"],
            "weeks_on": row["weeks_on"],
            "records": row["records"],
            "peak": row["peak"],
            "first": row["first"],
            "last": row["last"],
        })

    # Chart badges: which charts each artist appeared on (in scope), one
    # small extra query rather than one per artist.
    charts_by_id = {c.id: c for c in Chart.objects.order_by("category", "method")}
    order = {cid: i for i, cid in enumerate(charts_by_id)}
    on_charts = defaultdict(set)
    for artist_id, cid in entries.values_list("recording__credits__artist_id", "chart_id").distinct():
        on_charts[artist_id].add(cid)
    for row in rows:
        row["charts"] = [_badge(charts_by_id[cid]) for cid in sorted(on_charts[row["id"]], key=order.get)]

    key, _ = BROWSE_SORTS[sort]
    # The name always breaks ties, alphabetically, whichever way the main
    # sort runs -- so equal rows keep a stable, readable order.
    rows.sort(key=lambda r: r["name"].casefold())
    rows.sort(key=lambda r: r[key].casefold() if key == "name" else r[key], reverse=direction == "desc")

    columns = {}
    for k, (_, label) in BROWSE_SORTS.items():
        is_active = k == sort
        if is_active:
            next_dir = "asc" if direction == "desc" else "desc"
        else:
            next_dir = "desc" if k in _DESCENDING_FIRST else "asc"
        columns[k] = {
            "label": label,
            "href": "?" + urlencode({
                name: value for name, value in
                [("q", q), ("genre", genre), ("chart", chart_id), ("sort", k), ("dir", next_dir)] if value
            }),
            "is_active": is_active,
            "arrow": ("▲" if direction == "asc" else "▼") if is_active else "",
        }

    return {
        "rows": rows,
        "columns": columns,
        "q": q, "genre": genre, "chart": chart_id, "sort": sort, "dir": direction,
    }


def _search_artist_ids(text):
    """Artists matching `text`: EVERY word must appear in the canonical
    name, an alias, or the printed credit of a recording they're credited
    on -- so "sinatra" finds Frank Sinatra through "James - Sinatra", and
    "fritzgerald" finds Ella Fitzgerald through her alias."""
    artists = Artist.objects.all()
    for word in text.split():
        artists = artists.filter(
            Q(canonical_name__icontains=word)
            | Q(aliases__alias_text__icontains=word)
            | Q(recording_credits__recording__credit_text__icontains=word)
        )
    return artists.values("id").distinct()


def _badge(chart):
    # The same short chart codes the matrix uses ("CJb" = Country Jukebox).
    return {"code": recordings_matrix_lib._chart_code(chart), "label": str(chart), "id": chart.id}


# --------------------------------------------------------------- profile

def profile(artist, params):
    """Everything an artist profile renders, for the date / genre / chart
    scope in `params`. The date slider runs over the artist's OWN span on
    file (first to last week they charted), not all 450-odd weeks -- most
    artists charted for a couple of months, which would be a sliver."""
    recording_ids = list(RecordingCredit.objects.filter(artist=artist).values_list("recording_id", flat=True))
    mine = ChartEntry.objects.filter(recording_id__in=recording_ids)
    span = mine.aggregate(first=Min("week_date"), last=Max("week_date"))
    if span["first"] is None:
        return {"artist": artist, "has_data": False, **_header(artist, recording_ids, mine)}

    # Every chart week on file inside the artist's span -- weeks they were
    # NOT on a chart included, so the slider and the graph show the gaps.
    weeks = [w for w in dashboard_lib.all_weeks() if span["first"] <= w <= span["last"]]
    filters = dashboard_lib.parse_filters(params, weeks)
    entries = dashboard_lib.filtered_entries(filters).filter(recording_id__in=recording_ids)

    discography = recordings_matrix_lib.build({
        "group": "recording", "artist": str(artist.id),
        "genre": filters["genre"], "chart": filters["chart"],
        "from": filters["date_from"].isoformat(), "to": filters["date_to"].isoformat(),
        "sort": params.get("sort", "first"), "dir": params.get("dir", "asc"),
    })
    return {
        "artist": artist,
        "has_data": True,
        "filters": filters,
        **_header(artist, recording_ids, mine),
        **dashboard_lib.range_context(filters, weeks),
        "kpis": headline_numbers(entries),
        "records_over_time": records_over_time(entries, filters, weeks),
        "discography": discography,
        "collaborators": collaborators(artist, entries),
        "labels": labels(entries),
    }


def _header(artist, recording_ids, mine):
    # Who this is and how they were printed -- the whole career, not the
    # filtered window, since it describes the artist rather than a range.
    span = mine.aggregate(first=Min("week_date"), last=Max("week_date"))
    charts = Chart.objects.filter(entries__recording_id__in=recording_ids).distinct().order_by("category", "method")
    printed = (
        mine.values("recording__credit_text")
        .annotate(records=Count("recording_id", distinct=True))
        .order_by("-records", "recording__credit_text")
    )
    return {
        # Every printed credit their records carried, most-used first: the
        # magazine's own words for them, never altered (non-negotiable #4).
        "printed_as": [(p["recording__credit_text"], p["records"]) for p in printed],
        "aliases": list(artist.aliases.order_by("alias_text").values_list("alias_text", flat=True)),
        "first_week": span["first"],
        "last_week": span["last"],
        "charts": list(charts),
    }


def headline_numbers(entries):
    """The KPI row: one number each, dashboard definitions throughout."""
    def tile(label, value, hint, sub=""):
        return {"label": label, "value": value, "hint": hint, "sub": sub}

    best = entries.order_by("position", "week_date").select_related("recording__song").first()
    weeks_at_1 = entries.filter(position=1).values("week_date").distinct().count()
    return [
        tile("Weeks on chart", f"{entries.values('week_date').distinct().count():,}",
             "Calendar weeks with at least one of their records on a selected chart"),
        tile("Records", f"{entries.values('recording_id').distinct().count():,}",
             "Different records of theirs that charted (collaborations included)"),
        tile("Best peak", f"#{best.position}" if best else "—",
             "Best position any of their records reached",
             sub=best.recording.song.title if best else ""),
        tile("Weeks at #1", f"{weeks_at_1:,}", "Calendar weeks with one of their records at #1 on a selected chart"),
        tile("Charts", f"{entries.values('chart_id').distinct().count():,}", "Different charts they appeared on"),
    ]


def records_over_time(entries, filters, weeks):
    """How many of their records were on a selected chart, per week (short
    windows) or per month -- the shape of a career at a glance. A record on
    the chart for three weeks of one month counts once in that month."""
    window = [w for w in weeks if filters["date_from"] <= w <= filters["date_to"]]
    weekly = len(window) <= dashboard_lib.WEEKLY_BUCKETS_UP_TO
    on_chart = defaultdict(set)
    for recording_id, week in entries.values_list("recording_id", "week_date").distinct():
        on_chart[week if weekly else date(week.year, week.month, 1)].add(recording_id)

    starts = window if weekly else list(dashboard_lib._month_starts(filters["date_from"], filters["date_to"]))
    top, ticks = dashboard_lib._nice_axis(max((len(v) for v in on_chart.values()), default=0))
    buckets = []
    for i, start in enumerate(starts):
        value = len(on_chart.get(start, ()))
        if weekly:
            label = start.strftime("%b %d, %Y").replace(" 0", " ")
            axis = start.strftime("%b %d").replace(" 0", " ") if i % 4 == 0 else ""
        else:
            label = start.strftime("%b %Y")
            axis = dashboard_lib._month_axis_label(start, i, len(starts))
        buckets.append({"label": label, "axis_label": axis, "value": value,
                        "height_pct": round(value / top * 100, 2)})
    return {
        "unit": "week" if weekly else "month",
        "buckets": buckets,
        "ticks": [{"value": t, "pct": round(t / top * 100, 2)} for t in ticks],
    }


def collaborators(artist, entries):
    """Who they shared a credit with, on records charting in scope: each
    other artist, how many records together, best first. [] for most
    artists -- the section is only shown when there are any."""
    shared = (
        RecordingCredit.objects
        .filter(recording_id__in=entries.values("recording_id"))
        .filter(recording__credits__artist=artist)
        .exclude(artist=artist)
        .values("artist_id", "artist__canonical_name")
        .annotate(records=Count("recording_id", distinct=True))
        .order_by("-records", "artist__canonical_name")
    )
    return [{"id": s["artist_id"], "name": s["artist__canonical_name"], "records": s["records"]} for s in shared]


def labels(entries):
    """Records per label, most first. Only shown when there's more than one
    label -- a single label is already in every discography row."""
    rows = (
        entries.values("recording__label__name")
        .annotate(records=Count("recording_id", distinct=True))
        .order_by("-records", "recording__label__name")
    )
    biggest = rows[0]["records"] if rows else 1
    return [
        {"label": r["recording__label__name"], "value": r["records"],
         "width_pct": round(r["records"] / biggest * 100, 2)}
        for r in rows
    ]
