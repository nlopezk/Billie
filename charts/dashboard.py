"""
charts/dashboard.py -- every number on the /dashboard/ page.

Kept out of views.py on purpose: this is all counting logic, and counting
logic is exactly what's worth unit-testing (CLAUDE.md, Conventions). The
view just hands the querystring in and a template-ready dict comes back.

Everything here is DERIVED from printed chart positions -- nothing on the
dashboard is itself a printed value -- so the definitions below matter
more than usual. The four that decide what the numbers mean:

1.  A "week" is ONE CALENDAR WEEK. A record sitting at #1 on three charts
    the same Saturday has ONE week at #1, not three. Counting chart-weeks
    instead would let a record triple its total just by being popular in
    three formats at once, and would disagree with the recordings matrix,
    which already counts this way (see views._recordings_matrix_query).
    In SQL terms: always Count("week_date", distinct=True), never Count("id").

2.  Ties are credited to everyone in them. 39 chart-weeks on file have two
    records tied at #1; both get the week. Rankings use COMPETITION
    ranking ("1, 2, 2, 4") -- the same convention the charts themselves
    use for tied positions (docs/audit/position_skip_review.csv).

3.  Artists come from RecordingCredit, the structured credit, not from the
    printed credit_text. So "Ernest Tubb - Red Foley" credits BOTH acts,
    and "Julia Lee And Her Boy Friends" counts as Julia Lee -- which is
    the whole point of the /credits/ work. Every recording on file has at
    least one credit, so nobody falls through the cracks.

4.  A "debut" is a recording's first week on ANY of the selected charts,
    and records already on a chart in that chart's first week on file are
    NOT debuts: their real debut is before the data starts, and counting
    them would put a fake spike at the start of every chart.
"""
from bisect import bisect_left, bisect_right
from collections import defaultdict
from datetime import date
from math import ceil, floor, log10

from django.db.models import Count, Min, Q

from .models import Chart, ChartEntry

DEFAULT_TOP_N = 10
# A ranking is a reading list, not a dump -- past 100 rows the matrix
# (/recordings/, sortable by Weeks) is the better tool.
MAX_TOP_N = 100
# The label chart is a fixed top 10 regardless of N: it's a picture of who
# dominated, and 50 bars stops being a picture.
TOP_LABELS = 10
# Up to this many weeks in the range, debuts are counted per WEEK; past it,
# per MONTH. Half a year of weekly bars is ~26 columns -- readable. Eight
# years of weekly bars would be 437 slivers.
WEEKLY_BUCKETS_UP_TO = 26

# What identifies a recording in every ranking row, pulled in through the
# existing joins in one query (same fields the matrix uses).
_RECORDING_FIELDS = (
    "recording_id",
    "recording__song__title",
    "recording__credit_text",
    "recording__label__name",
    "recording__catalog_number",
    "recording__side",
)


# --------------------------------------------------------------- filters

def all_weeks():
    """Every distinct week_date on file, oldest first.

    One shared list works for every chart because all five publish on
    Saturdays, weekly, with no gaps (checked against the data 2026-10-16:
    437 distinct dates in total). That's what lets ONE date slider drive
    all charts at once.
    """
    return list(
        ChartEntry.objects.order_by("week_date").values_list("week_date", flat=True).distinct()
    )


def _parse_date(value):
    # Bad or missing input falls back to the full range rather than
    # erroring -- a hand-edited or half-copied URL should still show a
    # dashboard, just an unfiltered one.
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return None


def _parse_top_n(value):
    try:
        n = int(value)
    except (TypeError, ValueError):
        return DEFAULT_TOP_N
    return max(1, min(MAX_TOP_N, n))


def parse_filters(params, weeks):
    """Read the querystring into a clean filter dict.

    `from`/`to` are snapped onto real chart weeks -- `from` to the first
    week on or after it, `to` to the last week on or before it -- and
    swapped if they arrive backwards, so every query below (and the
    slider, which works in week INDEXES) can trust them to be in `weeks`.
    """
    genre = params.get("genre", "")
    if genre not in dict(Chart.CATEGORY_CHOICES):
        genre = ""
    chart_id = params.get("chart", "")
    if not chart_id.isdigit():
        chart_id = ""

    first, last = (weeks[0], weeks[-1]) if weeks else (None, None)
    date_from = _parse_date(params.get("from")) or first
    date_to = _parse_date(params.get("to")) or last
    if weeks:
        if date_from > date_to:
            date_from, date_to = date_to, date_from
        # bisect works on the sorted week list: the index of the first
        # week >= date_from, and of the last week <= date_to, each clamped
        # so an out-of-range date lands on the nearest end.
        date_from = weeks[min(bisect_left(weeks, date_from), len(weeks) - 1)]
        date_to = weeks[max(bisect_right(weeks, date_to) - 1, 0)]
        if date_from > date_to:
            # A window falling entirely BETWEEN two Saturdays: show the
            # one week it's closest to rather than an empty dashboard.
            date_to = date_from

    return {
        "genre": genre,
        "chart": chart_id,
        "date_from": date_from,
        "date_to": date_to,
        "n": _parse_top_n(params.get("n")),
    }


def filtered_entries(filters, with_dates=True):
    """The ChartEntry rows every number on the page is computed from.

    with_dates=False keeps the genre/chart scope but drops the date
    window -- needed for debuts, where "first week ever on these charts"
    has to look BEFORE the window to know whether a record is new.
    """
    entries = ChartEntry.objects.all()
    if filters["genre"]:
        entries = entries.filter(chart__category=filters["genre"])
    if filters["chart"]:
        entries = entries.filter(chart_id=filters["chart"])
    if with_dates and filters["date_from"]:
        entries = entries.filter(week_date__gte=filters["date_from"], week_date__lte=filters["date_to"])
    return entries


# --------------------------------------------------------------- rankings

def _rank(rows, key, n):
    """Competition-rank `rows` (already sorted best-first) on `key`, keep n.

    Returns the shown rows (each with a "rank"), plus how many MORE rows
    beyond the cut share the last shown row's value. Those aren't listed
    -- in the #1 ranking, "top 10" could otherwise mean 40 records tied at
    one week -- but the page says how many there are, so the cut is never
    mistaken for "nobody else had that many".
    """
    rank, previous = 0, object()
    for i, row in enumerate(rows):
        if row[key] != previous:
            # Ties share a rank; the next distinct value skips ahead by the
            # size of the tie ("2, 2, 4"), like a printed chart.
            rank, previous = i + 1, row[key]
        row["rank"] = rank
    shown = rows[:n]
    more_tied = 0
    if shown and len(rows) > n:
        cut_value = shown[-1][key]
        # rows is sorted on key, so everything equal to the cut value is
        # contiguous right after it -- stop at the first different one.
        for row in rows[n:]:
            if row[key] != cut_value:
                break
            more_tied += 1
    return {
        "rows": shown,
        "more_tied": more_tied,
        "tied_value": shown[-1][key] if shown else None,
        "total": len(rows),
    }


def _recording_row(row):
    # .values() hands back flat dicts with ORM-path keys; give the template
    # short names and a ready-made catalog string ("Victor 21-0328 B").
    parts = [row["recording__label__name"], row["recording__catalog_number"]]
    if row["recording__side"]:
        parts.append(row["recording__side"])
    return {
        **row,
        "song": row["recording__song__title"],
        "credit_text": row["recording__credit_text"],
        "catalog_display": " ".join(parts),
        # What the matrix's search box needs to find exactly this record:
        # its label + catalog number, word-AND matched there.
        "matrix_query": f'{row["recording__label__name"]} {row["recording__catalog_number"]}',
    }


def top_number_ones(entries, n):
    """Most weeks at #1 in the window. Tie-break: more weeks on chart overall."""
    rows = (
        entries
        .values(*_RECORDING_FIELDS)
        .annotate(
            # COUNT(DISTINCT CASE WHEN position = 1 THEN week_date END):
            # calendar weeks at #1 on ANY selected chart (definition 1).
            weeks_at_1=Count("week_date", filter=Q(position=1), distinct=True),
            weeks_on=Count("week_date", distinct=True),
        )
        .filter(weeks_at_1__gt=0)
        .order_by("-weeks_at_1", "-weeks_on", "recording__song__title")
    )
    return _rank([_recording_row(r) for r in rows], "weeks_at_1", n)


def top_recordings(entries, n):
    """Most weeks on chart, any position. Tie-break: the better (lower) peak."""
    rows = (
        entries
        .values(*_RECORDING_FIELDS)
        .annotate(weeks_on=Count("week_date", distinct=True), peak=Min("position"))
        .order_by("-weeks_on", "peak", "recording__song__title")
    )
    return _rank([_recording_row(r) for r in rows], "weeks_on", n)


def top_artists(entries, n):
    """Most weeks on chart per credited artist (definition 3).

    Ranked by WEEKS PRESENT -- calendar weeks with at least one of the
    artist's records on a selected chart. Two more numbers ride along,
    because "most weeks" honestly has two readings for an artist:
      * recordings   -- how many different records charted
      * record_weeks -- each record's weeks added up, so an artist with
                        three hits on at once earns three per week. This
                        is the tie-break, and the column to read for
                        "who dominated", as opposed to "who was always there".
    """
    present = (
        entries
        .values("recording__credits__artist_id", "recording__credits__artist__canonical_name")
        .annotate(weeks_on=Count("week_date", distinct=True), recordings=Count("recording_id", distinct=True))
    )
    # record_weeks needs DISTINCT over a (recording, week) PAIR, which
    # COUNT(DISTINCT ...) can't express on two columns. So count each
    # (artist, recording)'s weeks in SQL and add them up here -- a few
    # hundred small rows, not worth a raw-SQL workaround.
    record_weeks = defaultdict(int)
    for row in entries.values("recording__credits__artist_id", "recording_id").annotate(w=Count("week_date", distinct=True)):
        record_weeks[row["recording__credits__artist_id"]] += row["w"]

    rows = [
        {
            "artist_id": row["recording__credits__artist_id"],
            "name": row["recording__credits__artist__canonical_name"],
            "weeks_on": row["weeks_on"],
            "recordings": row["recordings"],
            "record_weeks": record_weeks[row["recording__credits__artist_id"]],
        }
        for row in present
        # Defensive only: every recording has a credit today, but a LEFT
        # JOIN onto a credit-less recording would surface as artist None.
        if row["recording__credits__artist_id"] is not None
    ]
    rows.sort(key=lambda r: (-r["weeks_on"], -r["record_weeks"], r["name"]))
    return _rank(rows, "weeks_on", n)


# --------------------------------------------------------------- graphs

def _nice_axis(max_value, target_ticks=4):
    """A clean y-axis: ticks at 1/2/5 x 10^k steps, top tick >= max_value.

    Returns (top, ticks). Ticks are integers because everything plotted
    here is a count -- "2.5 debuts" would be a gridline nobody can read.
    """
    if max_value <= 0:
        return 1, [0, 1]
    raw_step = max_value / target_ticks
    magnitude = 10 ** floor(log10(raw_step))
    step = next(m * magnitude for m in (1, 2, 5, 10) if m * magnitude >= raw_step)
    step = max(1, int(round(step)))
    top = int(ceil(max_value / step) * step)
    return top, list(range(0, top + 1, step))


def _month_starts(date_from, date_to):
    # Every month touched by the window, as the 1st of that month -- so
    # a month with zero debuts still gets its (empty) slot on the axis
    # instead of silently vanishing and squeezing the timeline.
    year, month = date_from.year, date_from.month
    while (year, month) <= (date_to.year, date_to.month):
        yield date(year, month, 1)
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)


def _month_axis_label(start, i, total):
    """Which monthly slots get an x-axis label, so labels never collide.

    The density adapts to how many months are on screen: a single year
    can name every month, eight years can only afford one label a year.
    """
    if total <= 15:
        # Every month; the year rides along on January and on the first slot.
        return start.strftime("%b %Y") if (start.month == 1 or i == 0) else start.strftime("%b")
    if total <= 36:
        # Quarterly: Jan / Apr / Jul / Oct.
        if start.month % 3 != 1:
            return ""
        return str(start.year) if start.month == 1 else start.strftime("%b")
    # Years only, on each January. The first slot is labelled only if it's
    # early enough in its year to leave room before the next January --
    # otherwise "1942" and "1943" print on top of each other.
    if start.month == 1 or (i == 0 and start.month <= 6):
        return str(start.year)
    return ""


def debuts_over_time(filters, weeks):
    """New records entering the selected charts, per week or per month."""
    scope = filtered_entries(filters, with_dates=False)
    # Each chart's first week on file -- anything on it that week was
    # already charting when the data begins (definition 4).
    chart_first = dict(scope.values("chart_id").annotate(first=Min("week_date")).values_list("chart_id", "first"))

    # For each recording: its first week across the selected charts, and
    # which charts it was on that week.
    first_week = {}
    for rec_id, chart_id, first in (
        scope.values("recording_id", "chart_id").annotate(first=Min("week_date"))
        .values_list("recording_id", "chart_id", "first")
    ):
        known = first_week.get(rec_id)
        if known is None or first < known[0]:
            first_week[rec_id] = (first, {chart_id})
        elif first == known[0]:
            known[1].add(chart_id)

    debut_dates = [
        first for first, charts in first_week.values()
        # Unknowable if it was on ANY of those charts in that chart's very
        # first week on file -- it may well have been there before.
        if not any(chart_first[c] == first for c in charts)
        and filters["date_from"] <= first <= filters["date_to"]
    ]

    window = [w for w in weeks if filters["date_from"] <= w <= filters["date_to"]]
    weekly = len(window) <= WEEKLY_BUCKETS_UP_TO
    counts = defaultdict(int)
    for d in debut_dates:
        counts[d if weekly else date(d.year, d.month, 1)] += 1

    starts = window if weekly else list(_month_starts(filters["date_from"], filters["date_to"]))
    top, ticks = _nice_axis(max(counts.values(), default=0))
    buckets = []
    for i, start in enumerate(starts):
        value = counts.get(start, 0)
        if weekly:
            label = start.strftime("%b %d, %Y").replace(" 0", " ")
            # Every 4th week gets an axis label; the rest stay in the
            # tooltip and the table, so labels never collide.
            axis = start.strftime("%b %d").replace(" 0", " ") if i % 4 == 0 else ""
        else:
            label = start.strftime("%b %Y")
            axis = _month_axis_label(start, i, len(starts))
        buckets.append({
            "label": label,
            "axis_label": axis,
            "value": value,
            "height_pct": round(value / top * 100, 2),
        })
    return {
        "unit": "week" if weekly else "month",
        "buckets": buckets,
        "total": len(debut_dates),
        # Gridlines from the top down, each with its height as a % of the
        # plot, so the template can place them with `bottom: X%`.
        "ticks": [{"value": t, "pct": round(t / top * 100, 2)} for t in ticks],
    }


def top_labels(entries, limit=TOP_LABELS):
    """Labels ranked by record-weeks: each record's weeks on chart, summed.

    Record-weeks, not calendar weeks, because a label is a catalogue: a
    week with three Decca records on the chart really IS three times the
    Decca presence of a week with one. Within one record, a week on three
    charts still counts once (definition 1).
    """
    totals = defaultdict(int)
    for row in entries.values("recording__label__name", "recording_id").annotate(w=Count("week_date", distinct=True)):
        totals[row["recording__label__name"]] += row["w"]
    ranked = sorted(totals.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]
    biggest = ranked[0][1] if ranked else 1
    return [
        {"label": name, "value": value, "value_display": f"{value:,}", "width_pct": round(value / biggest * 100, 2)}
        for name, value in ranked
    ]


def headline_numbers(entries, debut_total):
    """The KPI row across the top: one number each, no chart needed."""
    def tile(label, value, hint):
        return {"label": label, "value": f"{value:,}", "hint": hint}

    return [
        tile("Weeks", entries.values("week_date").distinct().count(), "Calendar weeks in the range with at least one chart on file"),
        tile("Recordings", entries.values("recording_id").distinct().count(), "Different records that charted in the range"),
        tile("Artists", entries.values("recording__credits__artist_id").distinct().count(), "Different credited artists (collaborations count each act)"),
        tile("Debuts", debut_total, "Records whose first week on the selected charts falls in the range"),
        tile("No. 1 records", entries.filter(position=1).values("recording_id").distinct().count(), "Different records that reached #1 in the range"),
    ]


# --------------------------------------------------------------- assembly

def build(params):
    """Everything the dashboard template needs, from the raw querystring."""
    weeks = all_weeks()
    filters = parse_filters(params, weeks)
    if not weeks:
        return {"filters": filters, "has_data": False}

    entries = filtered_entries(filters)
    debuts = debuts_over_time(filters, weeks)
    # Safe: parse_filters guarantees both dates are real entries of weeks.
    from_index = weeks.index(filters["date_from"])
    to_index = weeks.index(filters["date_to"])

    # Year shortcuts for the slider: each year's first and last week index.
    years = {}
    for i, w in enumerate(weeks):
        first_i, _ = years.get(w.year, (i, i))
        years[w.year] = (first_i, i)
    year_options = [{"year": y, "first": a, "last": b} for y, (a, b) in sorted(years.items())]
    # Which shortcut (if any) the current window exactly matches, so the
    # Year dropdown shows it after a reload instead of resetting.
    if from_index == 0 and to_index == len(weeks) - 1:
        preset = "all"
    else:
        preset = next(
            (str(o["year"]) for o in year_options if (o["first"], o["last"]) == (from_index, to_index)), ""
        )

    return {
        "has_data": True,
        "filters": filters,
        # Parallel [iso, display] pairs for the slider's JS, which turns an
        # index into a date. Display strings come from HERE, not from JS
        # Date parsing: new Date("1944-01-08") is midnight UTC, which reads
        # as Jan 7 in any timezone west of Greenwich.
        "week_points": [[w.isoformat(), w.strftime("%b %d, %Y").replace(" 0", " ")] for w in weeks],
        "from_index": from_index,
        "to_index": to_index,
        "range_weeks": to_index - from_index + 1,
        "year_options": year_options,
        "preset": preset,
        "kpis": headline_numbers(entries, debuts["total"]),
        "debuts": debuts,
        "labels": top_labels(entries),
        "number_ones": top_number_ones(entries, filters["n"]),
        "top_recordings": top_recordings(entries, filters["n"]),
        "top_artists": top_artists(entries, filters["n"]),
    }
