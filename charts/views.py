# charts/views.py
#
# Three feature areas:
#   - the read-only chart BROWSER (chart_list, chart_week, plus the
#     recordings_matrix) -- never writes anything, just a nicer way to
#     look at what's on file than paging through the admin's flat lists.
#   - the weekly entry FORM (entry_week + the entry_* HTMX endpoints) --
#     CLAUDE.md's "heart of the project". See the big comment above
#     entry_week() for how the pieces fit together.
#   - the DISCOGS review section (discogs_* below) -- optional,
#     after-the-fact enrichment. The ONLY part of the app that touches
#     the network, and nothing above imports it. See the big comment
#     above discogs_queue().

import re
from collections import defaultdict
from datetime import datetime
from urllib.parse import urlencode

from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Case, Count, IntegerField, Max, Min, Q, Value, When
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from . import chart_week as chart_week_lib
from . import credits as credits_lib
from . import dashboard as dashboard_lib
from . import lastweek_audit as lastweek_audit_lib
from . import week_submission
from . import discogs as discogs_api
from .models import (
    Artist, ChartEntry, Chart, DiscogsCandidate, DiscogsMatch, Label,
    Recording, RecordingCredit, Song,
)


def chart_switch_options(current_chart, week):
    """The week page's chart dropdown: one option per chart, each pointing
    at the SAME week on that chart, so switching charts compares like with
    like instead of dumping you on that chart's latest week.

    If the other chart has no entry for this exact week (it started later,
    or ended earlier), the option points at its CLOSEST week on file and
    says so in its label -- a different date is never a silent surprise.
    Charts with nothing on file yet are left out: there's no page to go to.
    """
    weeks_by_chart = defaultdict(list)
    # One query for every chart's weeks (~1,300 small rows) rather than one
    # query per chart.
    for chart_id, w in ChartEntry.objects.values_list("chart_id", "week_date").distinct():
        weeks_by_chart[chart_id].append(w)
    options = []
    for chart in Chart.objects.order_by("category", "method"):
        weeks = weeks_by_chart.get(chart.id)
        if not weeks:
            continue
        # Exact week if it's on file; otherwise the nearest one either side
        # (ties go to the earlier week -- min() keeps the first it finds).
        target = week if week in weeks else min(sorted(weeks), key=lambda w: abs((w - week).days))
        options.append({
            "chart": chart,
            "url": reverse("charts:chart_week", args=[chart.id, target.isoformat()]),
            "selected": chart.id == current_chart.id,
            # Shown in the option label only when the date would change.
            "other_week": None if target == week else target,
        })
    return options


def chart_list(request):
    # One row per chart, with just enough summary info (date range, how
    # many weeks/entries on file) to help you pick one -- computed here
    # rather than stored, since it's cheap and always accurate this way.
    charts = []
    for chart in Chart.objects.all():
        entries = chart.entries.all()
        first = entries.order_by("week_date").first()
        last = entries.order_by("-week_date").first()
        charts.append({
            "chart": chart,
            "entry_count": entries.count(),
            "first_week": first.week_date if first else None,
            "last_week": last.week_date if last else None,
        })
    return render(request, "charts/chart_list.html", {"charts": charts})


def chart_week(request, chart_id, week_date=None):
    chart = get_object_or_404(Chart, pk=chart_id)

    all_weeks = list(
        chart.entries.order_by("week_date").values_list("week_date", flat=True).distinct()
    )
    if not all_weeks:
        raise Http404("This chart has no entries yet.")

    if week_date is None:
        # No week in the URL -- default to the most recent one on file.
        selected_week = all_weeks[-1]
    else:
        try:
            selected_week = datetime.strptime(week_date, "%Y-%m-%d").date()
        except ValueError:
            raise Http404("Malformed week date.")
        if selected_week not in all_weeks:
            raise Http404("No entries for this chart on that date.")

    # Prev/next arrows: the neighbouring chart weeks ON FILE, for
    # navigation only -- they skip any gap. Deliberately NOT the previous
    # chart that movement is measured against (chart_week.ChartWeek), which
    # is the previous publication date whether or not it's entered; see
    # docs/adr/0001-previous-chart-is-previous-publication.md.
    idx = all_weeks.index(selected_week)
    prev_week_on_file = all_weeks[idx - 1] if idx > 0 else None
    next_week_on_file = all_weeks[idx + 1] if idx < len(all_weeks) - 1 else None

    # The printed chart name active on this specific week, from ChartEra --
    # NOT stored per-entry, looked up by date range (see CLAUDE.md's
    # Chart/ChartEra design).
    era = chart.eras.filter(start_date__lte=selected_week).filter(
        Q(end_date__isnull=True) | Q(end_date__gte=selected_week)
    ).first()

    # list(), not a lazy queryset: it's walked three times below.
    entries = list(
        chart.entries
        .filter(week_date=selected_week)
        .select_related("recording", "recording__song", "recording__label")
        .order_by("position")
    )

    # Flag ties (>1 recording sharing a position) so the template can show
    # a "tie" badge instead of that just looking like a rendering glitch.
    position_counts = {}
    for e in entries:
        position_counts[e.position] = position_counts.get(e.position, 0) + 1

    # Movement and "last week" come from the previous chart on file, not
    # from whether a last-week number was typed -- see ChartWeek.movement.
    week = chart_week_lib.ChartWeek(chart, selected_week)
    rows = []
    for e in entries:
        moved = week.movement(e.recording_id, e.position, e.printed_last_week_position)
        rows.append({
            "entry": e,
            "movement": moved.code,
            # Where it really was last week, per the data -- shown (in
            # italics, as computed) when no number was typed in.
            "last_on_file": moved.last_on_file,
            "conflict": moved.conflict,
            "is_tie": position_counts[e.position] > 1,
        })

    # Two-step navigation instead of one giant flat dropdown: a Year
    # selector, and a Week selector scoped to just the CURRENTLY SELECTED
    # year. Picking a different year in the Year selector jumps to that
    # year's first week, which reloads the page and -- since Week's
    # options are always derived from selected_week's year -- naturally
    # repopulates the Week dropdown with the new year's weeks. No custom
    # JS or client-side data needed, just a page load either way.
    weeks_by_year = {}
    for w in all_weeks:
        weeks_by_year.setdefault(w.year, []).append(w)
    # (year, that year's first week) pairs, for the Year dropdown -- a list
    # of tuples rather than a dict, because Django templates can't do a
    # variable-keyed dict lookup (`somedict.loopvar` looks up the literal
    # string "loopvar", not the value of the variable named loopvar).
    year_options = [(year, weeks[0]) for year, weeks in sorted(weeks_by_year.items())]
    weeks_this_year = weeks_by_year[selected_week.year]

    context = {
        "chart": chart,
        "chart_options": chart_switch_options(chart, selected_week),
        "era": era,
        "selected_week": selected_week,
        "prev_week_on_file": prev_week_on_file,
        "next_week_on_file": next_week_on_file,
        "rows": rows,
        "prev_on_file": week.previous_on_file,
        "conflict_count": sum(r["conflict"] for r in rows),
        # Rows whose "Last wk" is shown computed (no number typed) -- the
        # template adds a one-line key under the table when there are any.
        "computed_lw_count": sum(
            1 for r in rows if r["entry"].printed_last_week_position is None and r["last_on_file"]
        ),
        "year_options": year_options,
        "weeks_this_year": weeks_this_year,
        "first_week": all_weeks[0],
        "last_week": all_weeks[-1],
    }
    return render(request, "charts/chart_week.html", context)


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
# per" dropdown. See _recordings_matrix_query's docstring for what each
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


def _recordings_matrix_query(request):
    """
    Shared by recordings_matrix's full-page render and its own HTMX live-
    filter response (same view, see below): builds the filtered, sorted
    rows plus the current filter/sort/group state.

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
    genre = request.GET.get("genre", "")
    chart_id = request.GET.get("chart", "")
    q = request.GET.get("q", "").strip()
    sort = request.GET.get("sort", "peak")
    direction = request.GET.get("dir", "asc")
    group = request.GET.get("group", "chart")
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
    if q:
        # Same "every word matches at least one field" search as the entry
        # form's recording search (views.entry_search) -- "Capitol 304"
        # finds a Capitol release cataloged 304 here too.
        query = Q()
        for word in q.split():
            query &= (
                Q(recording__song__title__icontains=word) | Q(recording__credit_text__icontains=word)
                | Q(recording__label__name__icontains=word) | Q(recording__catalog_number__icontains=word)
            )
        entries = entries.filter(query)

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
    # instances -- there is no catalog_display property or __str__ to
    # reuse here, so both are built by hand, once, per row. Ordered by
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
        parts = [row["recording__label__name"], row["recording__catalog_number"]]
        if row["recording__side"]:
            parts.append(row["recording__side"])
        row["catalog_display"] = " ".join(parts)
        # Plain outbound discogs.com URL, built from the cached id -- a
        # string, not a request. Release preferred (the precise, common
        # case); Master is the fallback for a manually-entered reference
        # that could only be pinned down to the work, not the specific
        # pressing (see DiscogsReferenceMixin). Blank when there is no
        # link at all yet.
        release_id = row["recording__discogs__discogs_release_id"]
        master_id = row["recording__discogs__discogs_master_id"]
        row["discogs_ref_id"] = release_id or master_id
        row["discogs_ref_type"] = "Release" if release_id else ("Master" if master_id else "")
        if release_id:
            row["discogs_url"] = f"https://www.discogs.com/release/{release_id}"
        elif master_id:
            row["discogs_url"] = f"https://www.discogs.com/master/{master_id}"
        else:
            row["discogs_url"] = ""
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

    state = {"genre": genre, "chart": chart_id, "q": q, "sort": sort, "dir": direction, "group": group}
    return rows, state


def _recordings_matrix_columns(state):
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
        is_active = key == state["sort"]
        next_dir = "desc" if (is_active and state["dir"] == "asc") else "asc"
        params = {
            "genre": state["genre"], "chart": state["chart"], "q": state["q"],
            "group": state["group"], "sort": key, "dir": next_dir,
        }
        columns[key] = {
            "label": label,
            "href": "?" + urlencode({k: v for k, v in params.items() if v}),
            "is_active": is_active,
            "arrow": ("▲" if state["dir"] == "asc" else "▼") if is_active else "",
        }
    return columns


def recordings_matrix(request):
    # One page, one URL (/recordings/), covering every chart -- replaces
    # the earlier one-matrix-per-chart design. Genre/chart/search filters
    # combine (AND), and the results -- count line, table headers, and
    # rows together -- live-update via HTMX as you type or change a
    # dropdown (see recordings_matrix.html's <form>), no page reload.
    #
    # That live-update request hits this SAME view and SAME URL (not a
    # separate endpoint): HX-Request present -> render just the results
    # partial; absent (a normal page load, reload, or shared/bookmarked
    # link -- HTMX's hx-push-url puts the filtered URL in the address bar)
    # -> render the full page. One view, one URL, both cases share
    # identical filter/sort logic with no risk of the two drifting apart.
    rows, state = _recordings_matrix_query(request)
    context = {
        **state,
        "rows": rows,
        "columns": _recordings_matrix_columns(state),
        "total_rows": len(rows),
        "genre_choices": Chart.CATEGORY_CHOICES,
        "all_charts": Chart.objects.order_by("category", "method"),
    }
    if request.headers.get("HX-Request"):
        return render(request, "charts/_recordings_matrix_results.html", context)
    return render(request, "charts/recordings_matrix.html", context)


def dashboard(request):
    # Every number on the page comes from dashboard_lib.build() -- the
    # counting rules live there, next to their tests, not in this view.
    # Same one-URL, two-renders pattern as recordings_matrix above: the
    # filter row's HTMX live update sends HX-Request and gets just the
    # results partial back; a normal load, reload or bookmarked link gets
    # the full page.
    context = {
        **dashboard_lib.build(request.GET),
        "genre_choices": Chart.CATEGORY_CHOICES,
        "all_charts": Chart.objects.order_by("category", "method"),
    }
    if request.headers.get("HX-Request"):
        return render(request, "charts/_dashboard_results.html", context)
    return render(request, "charts/dashboard.html", context)


def lastweek_audit(request):
    # Every typed last-week number that disagrees with the previous week on
    # file, grouped into whole-week stretches and individual cases, each
    # with its likely cause -- all worked out in charts/lastweek_audit.py.
    # Read-only: the owner fixes the data in the entry form's edit page, and
    # a row disappears from here by itself once the two sides agree.
    context = lastweek_audit_lib.build(request.GET)
    context["chart_options"] = Chart.objects.order_by("category", "method")
    return render(request, "charts/lastweek_audit.html", context)


# =============================================================================
# Weekly entry form -- CLAUDE.md's "heart of the project"
# =============================================================================
#
# How the pieces fit together (see CLAUDE.md's "The weekly entry form"
# section for the design principle this all follows):
#
#   entry_week (GET)     renders the whole page: one row per expected
#                         position, seeded from the PREVIOUS week's row
#                         count. Nothing is resolved yet -- each row is
#                         just an empty "last week #" box plus a search box.
#
#   entry_resolve        HTMX target of the "last week #" box. Given this
#                         week's date and the typed number, looks up what
#                         occupied that position LAST week and returns a
#                         small preview fragment (or an ambiguous-tie
#                         picker, or an error) that replaces the row's
#                         "resolved" placeholder -- this is the "last-week
#                         number resolves the recording" behavior.
#
#   entry_search /        For debuts/re-entries: search existing
#   entry_select          Recordings as you type; clicking a result posts
#                         to entry_select, which returns the same preview
#                         fragment entry_resolve uses. One shared fragment
#                         shape means every path (resolve / pick / create)
#                         ends up looking identical once a row is resolved.
#
#   entry_create_form /   For a recording that's never been entered at
#   entry_create           all: an inline create-Song/Artist/Label/
#                         Recording sub-form, which on submit creates the
#                         rows (get_or_create, so re-submitting or a near-
#                         miss doesn't silently duplicate) and returns the
#                         same preview fragment as everything else.
#
#   entry_add_row         "+ Add row", for ties or extra debuts beyond the
#                         seeded row count. Returns a fresh empty row PLUS
#                         an out-of-band update to the hidden total_rows
#                         counter the final save reads.
#
#   entry_week (POST)     The actual save -- week_submission.submit_week,
#   entry_week_edit         which validates every row and writes them all or
#     (POST)                none (transaction.atomic()). If anything fails the
#                         whole form re-renders with everything the user
#                         already entered still in place, per CLAUDE.md's
#                         "save the whole week atomically" rule. A new week
#                         ADDS its rows; an edit REPLACES what's on file.


def _blank_row(index):
    # One empty row of the entry form, in the shape _entry_row.html expects.
    return {
        "index": index, "position": index + 1, "last_week_position": None, "recording": None,
        "printed_peak_position": None, "printed_weeks_on_chart": None, "printed_anomaly_note": "",
        "weeks_hint": None, "peak_hint": None,
    }


def _entry_page(request, chart, week_date, rows, next_index, **extra):
    # The one place entry_week.html's context is built -- a new week, an
    # edit, and a failed save of either all render the same page from the
    # same keys, plus whatever `extra` says (errors, is_edit).
    context = {
        "chart": chart,
        "week_date": week_date,
        "week_date_str": week_date.isoformat(),
        "previous_week_date": chart_week_lib.previous_chart_date(week_date),
        "rows": rows,
        "next_index": next_index,
        # Datalists behind the inline create box's Label/Artist/Song inputs.
        "labels": Label.objects.order_by("name"),
        "artists": Artist.objects.order_by("canonical_name"),
        "songs": Song.objects.order_by("title"),
        **extra,
    }
    return render(request, "charts/entry_week.html", context)


def _save_week(request, chart, week_date, *, replace):
    # The HTTP side of saving: hand the posted rows to submit_week, then
    # either go to the saved week's page or re-show the form as typed.
    result = week_submission.submit_week(
        chart, week_date, week_submission.rows_from_post(request.POST), replace=replace,
    )
    if result.saved:
        return redirect("charts:chart_week", chart_id=chart.id, week_date=week_date.isoformat())
    # Nothing was written -- re-render with everything the user already
    # entered still in place rather than making them start the week over.
    extra = {"errors": result.errors}
    if replace:
        extra["is_edit"] = True
    return _entry_page(request, chart, week_date, result.rows, result.next_index, **extra)


def entry_week(request, chart_id):
    chart = get_object_or_404(Chart, pk=chart_id)

    if request.method == "POST":
        try:
            week_date = datetime.strptime(request.POST.get("week_date"), "%Y-%m-%d").date()
        except (ValueError, TypeError):
            return redirect("charts:entry_week", chart_id=chart.id)
        return _save_week(request, chart, week_date, replace=False)

    week_param = request.GET.get("week")
    last_entered = chart.entries.order_by("-week_date").values_list("week_date", flat=True).first()

    if week_param:
        try:
            week_date = datetime.strptime(week_param, "%Y-%m-%d").date()
        except ValueError:
            raise Http404("Malformed week date.")
    elif last_entered:
        # Default to the week right after whatever's most recently on file.
        week_date = chart_week_lib.next_chart_date(last_entered)
    else:
        # A chart with literally nothing on file yet (not the case for any
        # of the 5 real charts today, but the schema allows adding a 6th
        # someday) -- there's no sensible date to guess, so just ask.
        return render(request, "charts/entry_week.html", {"chart": chart, "week_date": None})

    previous_week_count = chart.entries.filter(
        week_date=chart_week_lib.previous_chart_date(week_date)
    ).count()
    row_count = previous_week_count or 10  # first-ever week for this chart: a reasonable starting guess
    rows = [_blank_row(i) for i in range(row_count)]
    return _entry_page(request, chart, week_date, rows, row_count)


def entry_week_edit(request, chart_id, week_date):
    # Edits an ALREADY-SAVED week -- same page, same resolve/search/
    # create/delete machinery as a brand-new week, just seeded from
    # what's already on file instead of from last week's row count, and
    # saved by REPLACING this week's entries rather than adding to them.
    chart = get_object_or_404(Chart, pk=chart_id)
    try:
        target_week = datetime.strptime(week_date, "%Y-%m-%d").date()
    except ValueError:
        raise Http404("Malformed week date.")

    if not chart.entries.filter(week_date=target_week).exists():
        raise Http404("Nothing on file for this chart and week -- nothing to edit.")

    if request.method == "POST":
        return _save_week(request, chart, target_week, replace=True)

    # GET: seed rows from what's already saved, not from last week's count.
    # Ordered by (position, id) so a tie's row order stays stable across
    # reloads instead of shuffling.
    existing = (
        chart.entries.filter(week_date=target_week)
        .select_related("recording", "recording__song", "recording__label")
        .order_by("position", "id")
    )
    week = chart_week_lib.ChartWeek(chart, target_week)
    rows = []
    for i, e in enumerate(existing):
        weeks_hint, peak_hint = week.streak(e.recording)
        rows.append({
            "last_week_hint": (
                week.last_week_position(e.recording_id)
                if e.printed_last_week_position is None else None
            ),
            "index": i,
            "position": e.position,
            "last_week_position": e.printed_last_week_position,
            "recording": e.recording,
            "printed_peak_position": e.printed_peak_position,
            "printed_weeks_on_chart": e.printed_weeks_on_chart,
            "printed_anomaly_note": e.printed_anomaly_note,
            "weeks_hint": weeks_hint,
            "peak_hint": peak_hint,
        })
    return _entry_page(request, chart, target_week, rows, len(rows), is_edit=True)


def entry_resolve(request, chart_id):
    # HTMX target of a row's "last week #" input. Resolves it against
    # last week's chart_entry table and returns the shared preview
    # fragment -- or, if last week had a TIE at that exact position, an
    # ambiguous-pick fragment instead of silently guessing which one.
    chart = get_object_or_404(Chart, pk=chart_id)
    row = request.POST.get("row", "0")
    week_str = request.POST.get("week")
    raw_value = request.POST.get(f"last_week_position_{row}", "").strip()

    if not raw_value:
        # Field was cleared -- nothing to resolve, not an error.
        return render(request, "charts/_entry_resolved.html", {"row": row, "recording": None, "include_wks_hint_oob": True})

    try:
        week_date = datetime.strptime(week_str, "%Y-%m-%d").date()
        last_week_position = int(raw_value)
    except (ValueError, TypeError):
        return render(request, "charts/_entry_resolved.html", {"row": row, "recording": None, "error": "not a number", "include_wks_hint_oob": True})

    week = chart_week_lib.ChartWeek(chart, week_date)
    matches = week.resolve(last_week_position)

    if not matches:
        error = f"nothing at #{last_week_position} on {week.previous_date}"
        return render(request, "charts/_entry_resolved.html", {"row": row, "recording": None, "error": error, "include_wks_hint_oob": True})

    if len(matches) > 1:
        # A genuine tie last week -- can't tell which one continues into
        # this row from the position number alone.
        return render(request, "charts/_entry_ambiguous.html", {
            "chart": chart, "row": row, "week_date_str": week_str, "matches": matches,
        })

    recording = matches[0].recording
    weeks_hint, peak_hint = week.streak(recording)
    return render(request, "charts/_entry_resolved.html", {
        "row": row, "recording": recording, "weeks_hint": weeks_hint, "peak_hint": peak_hint,
        "include_wks_hint_oob": True,
    })


def _search_recordings(q, limit=15):
    """
    Existing Recordings whose song title, artist credit, label name, or
    catalog number match `q` -- split into words, EACH word required to
    match at least one of those four fields (AND across words, OR across
    fields per word), not the whole query as one literal substring.
    That's what makes "Capitol 304" find a Capitol release cataloged 304
    even though no single field contains "Capitol 304" together.

    Shared by entry_search (the primary debut/re-entry search box) and
    entry_create's pre-creation duplicate check below -- one place for
    this matching rule instead of two copies that could drift apart.
    """
    q = q.strip()
    if len(q) < 2:
        return Recording.objects.none()
    query = Q()
    for word in q.split():
        query &= (
            Q(song__title__icontains=word) | Q(credit_text__icontains=word)
            | Q(label__name__icontains=word) | Q(catalog_number__icontains=word)
        )
    return Recording.objects.filter(query).select_related("song", "label").order_by("song__title")[:limit]


def entry_search(request, chart_id):
    # HTMX target of a row's search box -- existing recordings only (see
    # the "create new" flow below for anything not found here). Matches
    # song title, artist credit, label name, or catalog number, since a
    # debut is often easier to find by what's printed on the label than
    # by song title alone.
    chart = get_object_or_404(Chart, pk=chart_id)
    row = request.POST.get("row", "0")
    week_str = request.POST.get("week")
    q = request.POST.get(f"search_{row}", "").strip()

    results = list(_search_recordings(q)) if len(q) >= 2 else []
    return render(request, "charts/_entry_search_results.html", {
        "chart": chart, "row": row, "week_date_str": week_str, "query": q, "results": results,
    })


def _link_discogs_from_entry(recording, discogs_ref):
    """
    Record a hand-typed Discogs reference from the entry form against a
    recording. Returns a list of plain-language notes for the resolved row.

    Shared by entry_create (a brand-new recording) and entry_select (the
    duplicate-check path, where you picked an EXISTING recording instead
    of creating one). It used to live only in entry_create, so picking an
    existing match off the duplicate-check list silently threw away
    whatever Discogs ID had been typed into the create box.

    NOT looked up here, deliberately: the entry form makes zero network
    calls, so a chart week can always be entered offline (non-negotiable
    #2 names a typed Discogs ID as the acceptable form and a live lookup
    as the forbidden one). The link lands as status=MANUAL with
    needs_detail True; those rows show under "Needs attention" on
    /discogs/ and fetch their own details as that page loads.
    """
    notes = []
    discogs_ref = (discogs_ref or "").strip()
    if not discogs_ref:
        return notes
    release_id, master_id, parse_error = _parse_discogs_reference(discogs_ref)
    if parse_error:
        # Never block the chart data over a bad optional field.
        notes.append(f"{parse_error} Saved without a Discogs link.")
        return notes
    match, _ = DiscogsMatch.objects.get_or_create(recording=recording)
    if match.is_linked and match.reference_id != (release_id or master_id):
        # A reused or picked recording may already carry a link someone
        # decided on. Never silently replace it -- say so and leave it.
        notes.append(
            f"Already linked to Discogs {match.reference_type} {match.reference_id}, "
            f"left as-is -- change it from the Discogs page if that's wrong."
        )
        return notes
    match.discogs_release_id = release_id
    match.discogs_master_id = master_id
    match.status = DiscogsMatch.MANUAL
    match.confidence = ""
    match.linked_at = timezone.now()
    match.save()
    return notes


def entry_select(request, chart_id):
    # HTMX target of clicking a search result (or an ambiguous-tie pick).
    # Returns the same preview fragment entry_resolve uses, so a row looks
    # identical no matter which of the three paths resolved it.
    chart = get_object_or_404(Chart, pk=chart_id)
    row = request.POST.get("row", "0")
    week_str = request.POST.get("week")
    recording = get_object_or_404(Recording.objects.select_related("song", "label"), pk=request.POST.get("recording_id"))
    # Only present when this pick came off the duplicate-check list, whose
    # buttons carry the open create box along (hx-include) -- a Discogs ID
    # typed there has to survive choosing an existing recording instead.
    notes = _link_discogs_from_entry(recording, request.POST.get(f"new_discogs_{row}", ""))

    weeks_hint = peak_hint = last_week_hint = None
    if week_str:
        try:
            week_date = datetime.strptime(week_str, "%Y-%m-%d").date()
            week = chart_week_lib.ChartWeek(chart, week_date)
            weeks_hint, peak_hint = week.streak(recording)
            last_week_hint = week.last_week_position(recording.id)
        except ValueError:
            pass

    return render(request, "charts/_entry_resolved.html", {
        "row": row, "recording": recording, "weeks_hint": weeks_hint, "peak_hint": peak_hint,
        "last_week_hint": last_week_hint,
        "include_wks_hint_oob": True, "notes": notes,
    })


def entry_create_form(request, chart_id):
    # HTMX target of "+ create new recording" in the search-results
    # fragment -- swaps in the inline create sub-form for that row.
    chart = get_object_or_404(Chart, pk=chart_id)
    row = request.GET.get("row", "0")
    week_str = request.GET.get("week", "")
    prefill = request.GET.get("q", "")
    return render(request, "charts/_entry_create_form.html", {
        "chart": chart, "row": row, "week_date_str": week_str, "prefill_song": prefill,
        "labels": Label.objects.order_by("name"),
        "artists": Artist.objects.order_by("canonical_name"),
        "songs": Song.objects.order_by("title"),
    })


def entry_create(request, chart_id):
    # HTMX target of the inline create sub-form's submit button. Creates
    # (or, via get_or_create, reuses) the Song/Artist/Label/Recording, then
    # returns the same shared preview fragment as every other resolution
    # path.
    chart = get_object_or_404(Chart, pk=chart_id)
    row = request.POST.get("row", "0")
    week_str = request.POST.get("week")
    # Set by _entry_create_duplicate_check.html's "create new anyway"
    # button on a SECOND submission of this same form -- skips the check
    # below and goes straight to creation, since the user already looked
    # at the possible matches and said none of them are it.
    confirmed = request.POST.get("confirm_create") == "1"

    song_title = request.POST.get(f"new_song_{row}", "").strip()
    # getlist, not get: the create sub-form renders more than one artist
    # input under this same name, so a collaboration gets credited
    # properly at entry time -- one RecordingCredit per artist -- instead
    # of becoming a single fake "A - B" Artist row that /credits/ then
    # has to unpick. Adding a third input needs no change here.
    # De-duplicated with order preserved, since that order IS credit_order.
    artist_names = list(dict.fromkeys(
        n.strip() for n in request.POST.getlist(f"new_artist_{row}") if n.strip()
    ))
    # Falls back to the workbook's own " - " joining convention when the
    # printed billing isn't given explicitly. Only a default -- whatever
    # is typed into the Credit field wins, because that field is the
    # as-printed value (non-negotiable #4).
    credit_text = request.POST.get(f"new_credit_text_{row}", "").strip() or " - ".join(artist_names)
    label_name = request.POST.get(f"new_label_{row}", "").strip()
    catalog_number = request.POST.get(f"new_catalog_{row}", "").strip()
    side = request.POST.get(f"new_side_{row}", "").strip().upper()

    missing = [
        label for label, value in [
            ("song title", song_title), ("artist", artist_names),
            ("label", label_name), ("catalog number", catalog_number),
        ] if not value
    ]
    if missing:
        return render(request, "charts/_entry_resolved.html", {
            "row": row, "recording": None, "error": "missing: " + ", ".join(missing),
            "include_wks_hint_oob": True,
        })

    if not confirmed:
        # Backstop duplicate check. The INTENDED workflow is search-first
        # (_entry_search_results.html) -- "create new" is only supposed to
        # get used when that search genuinely turns up nothing -- so this
        # is deliberately a second line of defense for when the search
        # step gets skipped or a near-duplicate gets missed there, not the
        # primary defense itself.
        #
        # Two DIFFERENT signals, unioned rather than one combined query:
        #   1. Song + artist credit look similar -- catches "this is the
        #      same recording, just typed with a mistyped/different
        #      catalog number".
        #   2. Label + catalog number match exactly what's on file --
        #      catches "this is literally the same physical release",
        #      even if the artist credit was typed differently (which is
        #      actually a MORE dangerous case to miss: get_or_create below
        #      matches on label+catalog+side alone, so a typo'd artist
        #      name wouldn't even trigger IT -- it would just quietly
        #      reuse the existing recording under a second RecordingCredit
        #      attempt, which the unique_recording_artist constraint on
        #      RecordingCredit would then reject outright at save time).
        # A plain single AND-across-every-typed-word search would miss
        # signal 2 whenever the artist name typed doesn't exactly match
        # what's already on file, which is exactly the case worth flagging.
        candidates = {}
        for r in _search_recordings(f"{song_title} {credit_text}"):
            candidates[r.id] = r
        for r in Recording.objects.filter(
            label__name__icontains=label_name, catalog_number__icontains=catalog_number
        ).select_related("song", "label"):
            candidates[r.id] = r
        matches = list(candidates.values())[:8]
        if matches:
            return render(request, "charts/_entry_create_duplicate_check.html", {
                "chart": chart, "row": row, "week_date_str": week_str, "matches": matches,
            })

    with transaction.atomic():
        song, _ = Song.objects.get_or_create(title=song_title)
        # get_or_create by exact canonical_name match -- NOT the kind of
        # "auto-merge" CLAUDE.md warns against (that's about silently
        # treating two DIFFERENTLY-spelled entries as the same artist).
        # Reusing an EXACT string match is the normal, expected behavior.
        # resolve_artist rather than a raw exact-match get_or_create:
        # it matches case-insensitively AND through ArtistAlias, so
        # typing "Joe Turner" reuses the canonical "Big Joe Turner"
        # instead of quietly creating a second artist for the same act.
        # (CLAUDE.md's Artist-identity section says the autocomplete
        # should search aliases; this is that, applied at create time.)
        artists = []
        for name in artist_names:
            found = credits_lib.resolve_artist(name)
            if found is None:
                found = Artist.objects.create(canonical_name=name)
            artists.append(found)
        label, _ = Label.objects.get_or_create(name=label_name)
        # get_or_create on the actual unique constraint -- if this exact
        # label+catalog+side already exists, reuse it (and its existing
        # song/credit_text) rather than erroring or silently duplicating.
        recording, created = Recording.objects.get_or_create(
            label=label, catalog_number=catalog_number, side=side,
            defaults={"song": song, "credit_text": credit_text},
        )
        if created:
            for order, artist in enumerate(artists, start=1):
                RecordingCredit.objects.create(recording=recording, artist=artist, credit_order=order)

    # --- optional hand-typed Discogs link ---------------------------------
    # NOT looked up here, deliberately. The entry form makes zero network
    # calls so a chart week can always be entered offline (CLAUDE.md
    # non-negotiable #2, which names a manually typed Discogs ID as the
    # acceptable form of this and a live lookup as the forbidden one).
    # The reference is recorded with status=MANUAL and needs_detail left
    # True; /discogs/ enriches it later, exactly like any other manual
    # entry whose detail fetch hasn't run yet.
    notes = []
    # The exact mistake that produced 43 combined-credit rows needing
    # cleanup: typing "Ernest Tubb - Red Foley" into ONE artist box. Not
    # blocking -- credit_text still records the billing correctly and
    # /credits/ can unpick it later -- but said plainly at the moment it
    # happens, which is the only time it's cheap to fix.
    if len(artist_names) == 1 and credits_lib.split_name(artist_names[0])[1]:
        parts = credits_lib.split_name(artist_names[0])[0]
        notes.append(
            f"“{artist_names[0]}” looks like {len(parts)} artists. If it is, "
            f"Reset this row and use a separate artist box for each — otherwise it "
            f"gets filed as one act."
        )

    notes += _link_discogs_from_entry(recording, request.POST.get(f"new_discogs_{row}", ""))

    weeks_hint = peak_hint = last_week_hint = None
    if week_str:
        try:
            week_date = datetime.strptime(week_str, "%Y-%m-%d").date()
            week = chart_week_lib.ChartWeek(chart, week_date)
            weeks_hint, peak_hint = week.streak(recording)
            last_week_hint = week.last_week_position(recording.id)
        except ValueError:
            pass

    return render(request, "charts/_entry_resolved.html", {
        "row": row, "recording": recording, "weeks_hint": weeks_hint, "peak_hint": peak_hint,
        "last_week_hint": last_week_hint,
        "include_wks_hint_oob": True, "notes": notes,
    })


def entry_add_row(request, chart_id):
    # HTMX target of "+ Add row" (ties, or extra debuts beyond the row
    # count seeded from last week). Appends one empty row and, via an
    # out-of-band swap, bumps the hidden total_rows counter the final
    # save reads -- one response updating two places in the page.
    chart = get_object_or_404(Chart, pk=chart_id)
    next_index = int(request.GET.get("next_index", 0))
    week_str = request.GET.get("week", "")

    return render(request, "charts/_entry_add_row_response.html", {
        "chart": chart, "row": _blank_row(next_index), "week_date_str": week_str, "new_total": next_index + 1,
    })


# =============================================================================
# Credit splitting -- turning combined-credit artists into real collaborations
# =============================================================================
#
# "Bing Crosby - Andrews Sisters" is on file as a single Artist row,
# because that's how the original workbook stored it. It's really one
# recording credited to two artists, which RecordingCredit has always
# been able to express and -- before this screen -- never did (0 of 1,229
# recordings had more than one credit).
#
# Read charts/credits.py for the split logic and why it never decides
# anything on its own. This is just the review surface: propose, let a
# human edit the names, apply on confirmation.


def credit_queue(request):
    artists = credits_lib.pending_artists()
    proposals = [credits_lib.proposed_split(a) for a in artists]
    return render(request, "charts/credit_queue.html", {
        "proposals": proposals,
        "pending_count": len(proposals),
        # Listed, not just counted, so a decision made by misclick -- or
        # before the "canonical is" option existed -- can be reopened
        # without going through the admin.
        "confirmed_single": Artist.objects.filter(confirmed_single_act=True).order_by("canonical_name"),
        "confirmed_single_count": Artist.objects.filter(confirmed_single_act=True).count(),
        # Every artist a name could be split into, for the datalist on
        # each part input -- typing suggests an existing act rather than
        # accidentally creating a near-duplicate of one already on file.
        "known_artists": Artist.objects.order_by("canonical_name"),
        "messages": request.session.pop("credit_messages", []),
    })


def credit_split(request, artist_id):
    # Applies a split. The part names come from editable inputs, NOT
    # from re-parsing the name here: a naive split of "James - Sinatra"
    # gives "James" and "Sinatra", and the whole point of showing them
    # in text boxes is that you can correct that to "Harry James" and
    # "Frank Sinatra" before anything is written.
    artist = get_object_or_404(Artist, pk=artist_id)
    part_names = [n for n in request.POST.getlist("part") if n.strip()]
    try:
        recordings, created, deleted = credits_lib.apply_split(artist, part_names)
    except ValueError as exc:
        request.session["credit_messages"] = [f"Couldn't split \"{artist.canonical_name}\": {exc}"]
        return redirect("charts:credit_queue")

    message = (
        f"Split \"{artist.canonical_name}\" across "
        f"{len(recordings)} recording{'' if len(recordings) == 1 else 's'}."
    )
    if created:
        message += " Created " + ", ".join(f'"{a.canonical_name}"' for a in created) + "."
    if not deleted:
        # Shouldn't happen, but say so rather than implying a clean
        # finish -- something else still credits the combined row.
        message += " The combined artist row was kept (something still credits it)."
    request.session["credit_messages"] = [message]
    return redirect("charts:credit_queue")


def credit_canonical(request, artist_id):
    # "One act, but its canonical name is shorter" -- Julia Lee And Her
    # Boy Friends is Julia Lee. Re-credits to the shorter name and keeps
    # the printed billing as an ArtistAlias. See credits.apply_canonical.
    artist = get_object_or_404(Artist, pk=artist_id)
    printed = artist.canonical_name
    try:
        recordings, canonical, created = credits_lib.apply_canonical(
            artist, request.POST.get("canonical", ""))
    except ValueError as exc:
        request.session["credit_messages"] = [
            f'Could not recanonicalise "{printed}": {exc}'
        ]
        return redirect("charts:credit_queue")

    message = (
        f'"{printed}" is now credited to "{canonical.canonical_name}" across '
        f"{len(recordings)} recording{'' if len(recordings) == 1 else 's'}, "
        f'with "{printed}" kept as an alias.'
    )
    if not created:
        message += f' Merged into the existing "{canonical.canonical_name}".'
    request.session["credit_messages"] = [message]
    return redirect("charts:credit_queue")


def credit_keep(request, artist_id):
    # "This is one act, not two" -- e.g. Homer & Jethro, or Lester Young
    # And His Kansas City Seven. Nothing about the data changes; the row
    # is just flagged so the queue stops asking.
    artist = get_object_or_404(Artist, pk=artist_id)
    artist.confirmed_single_act = True
    artist.save(update_fields=["confirmed_single_act"])
    request.session["credit_messages"] = [
        f'Kept "{artist.canonical_name}" as a single act -- it will not be offered again.'
    ]
    return redirect("charts:credit_queue")


def credit_reopen(request, artist_id):
    # Undo for "it's one act". Without this, a misclick -- or a decision
    # made before the "canonical is" option existed -- removed a row from
    # the queue permanently, recoverable only through the admin.
    artist = get_object_or_404(Artist, pk=artist_id)
    artist.confirmed_single_act = False
    artist.save(update_fields=["confirmed_single_act"])
    request.session["credit_messages"] = [
        f'Reopened "{artist.canonical_name}" for review.'
    ]
    return redirect("charts:credit_queue")


# =============================================================================
# Discogs review section
# =============================================================================
#
# Optional, after-the-fact enrichment: link each Recording to the Discogs
# release it corresponds to, so information Billboard never printed
# (songwriters, producers, actual release year, country, genres) is
# available alongside the chart data.
#
# This is the ONLY part of the project that makes network calls, and it
# does so only when a human clicks something here. Per CLAUDE.md
# non-negotiable #2 as amended 2026-08-21: entering charts must never
# require the internet, and nothing outside this section touches it. If
# Discogs is unreachable, every failure is caught and shown as a message
# on the page -- it never 500s and never affects the browser or entry form.
#
# The workflow, and why it's shaped this way:
#
#   search    -> stores a SHORTLIST of candidates (DiscogsCandidate rows),
#                status=suggested. Never auto-picks one, same "don't
#                silently guess" rule as the entry form's tie-picker and
#                duplicate-recording check.
#   confirm   -> a human picks one. Copies that candidate's summary onto
#                the DiscogsMatch and clears the shortlist.
#   details   -> the expensive per-release fetch (tracklist, songwriters,
#                producers) runs SEPARATELY from confirming, so bulk-
#                confirming 25 exact matches is instant rather than 25
#                sequential API calls. `needs_detail` marks the gap.
#
# Rate limits (60/min authenticated) make big runs slow by nature -- the
# `discogs_match` management command exists for grinding through hundreds
# of recordings in a terminal, where a long-running process is normal.
# This section is for interactive review and small batches.


# How many recordings one click of "search the next N" will process.
# Each search is ~1-2 API calls at ~1s each, so 10 is roughly 20 seconds
# of held-open request -- about the most that's comfortable in a browser.
# Bigger runs belong in the management command.
DISCOGS_WEB_BATCH_SIZE = 10


def _ensure_discogs_matches():
    """
    Give every Recording a DiscogsMatch row (status=unmatched) if it
    doesn't have one yet.

    Called on the queue page rather than done once in a data migration so
    it self-heals: recordings created later through the entry form get
    picked up automatically the next time this page is opened, with no
    signal or extra bookkeeping on the entry path.
    """
    missing = Recording.objects.filter(discogs__isnull=True)
    DiscogsMatch.objects.bulk_create(
        [DiscogsMatch(recording=recording) for recording in missing],
        # Belt-and-braces against a double-click racing itself; the
        # OneToOne already makes a duplicate impossible at the DB level.
        ignore_conflicts=True,
    )


def _discogs_progress():
    # Counts for the progress line at the top of the queue -- one grouped
    # query rather than six separate .count() calls.
    counts = dict(
        DiscogsMatch.objects.values_list("status").annotate(n=Count("id")).values_list("status", "n")
    )
    total = sum(counts.values())
    # Linked but not yet enriched -- see DiscogsMatch.needs_detail_q.
    needs_detail = DiscogsMatch.objects.filter(DiscogsMatch.needs_detail_q()).count()
    # A row still waiting on its detail fetch is NOT done, whatever its
    # status says. Counting those as settled is what let entry-form links
    # sit un-enriched: the progress bar called them finished.
    settled = sum(counts.get(status, 0) for status in DiscogsMatch.SETTLED_STATUSES) - needs_detail
    return {
        "total": total,
        "settled": settled,
        "percent": round(100 * settled / total) if total else 0,
        "counts": counts,
        "needs_detail": needs_detail,
    }


# Sort options for the linked-matches audit view. Same (field, label)
# shape as RECORDINGS_MATRIX_SORTS.
DISCOGS_AUDIT_SORTS = {
    "linked": ("linked_at", "Linked"),
    "song": ("recording__song__title", "Song"),
    "artist": ("recording__credit_text", "Artist"),
    "catalog": ("recording__catalog_number", "Catalog #"),
}


def discogs_audit(request):
    """
    Side-by-side audit of every LINKED match: our own transcribed data
    against what Discogs says, newest link first.

    Exists because the review queue answers "what still needs a
    decision", which is the opposite question from "I linked something
    wrong a few minutes ago -- which one was it". Sorting by linked_at
    descending (see that field's comment for why updated_at can't do
    this job) puts the most recent decisions at the top, and the
    mismatch flags make a wrong one stand out without reading all 331
    rows.

    Read-only: it never writes anything and makes no API call. Fixing a
    bad link happens back in the queue, which every row links to.
    """
    status = request.GET.get("status", "all")
    q = request.GET.get("q", "").strip()
    only_mismatches = request.GET.get("mismatches") == "1"
    sort = request.GET.get("sort", "linked")
    direction = request.GET.get("dir", "desc")
    if sort not in DISCOGS_AUDIT_SORTS:
        sort = "linked"

    matches = (
        DiscogsMatch.objects
        .filter(status__in=[DiscogsMatch.CONFIRMED, DiscogsMatch.MANUAL])
        .select_related("recording", "recording__song", "recording__label")
    )
    if status in (DiscogsMatch.CONFIRMED, DiscogsMatch.MANUAL):
        matches = matches.filter(status=status)
    if q:
        for word in q.split():
            matches = matches.filter(
                Q(recording__song__title__icontains=word)
                | Q(recording__credit_text__icontains=word)
                | Q(recording__label__name__icontains=word)
                | Q(recording__catalog_number__icontains=word)
                | Q(matched_track_title__icontains=word)
                | Q(matched_track_artist__icontains=word)
            )

    order_field, _ = DISCOGS_AUDIT_SORTS[sort]
    if direction == "desc":
        order_field = "-" + order_field
    matches = matches.order_by(order_field, "recording__song__title")

    # Comparison flags are computed per row in Python rather than in the
    # query: they need the same normalization rules the matcher itself
    # uses (charts.discogs.normalize_catalog_number / normalize_label),
    # so that what's flagged here agrees with how the match was scored in
    # the first place. Not expressible as a SQL comparison.
    rows = []
    for match in matches:
        recording = match.recording
        catalog_ok = discogs_api.catalog_numbers_agree(
            recording.catalog_number, match.matched_catalog_number
        )
        label_ok = discogs_api.labels_agree(recording.label.name, match.matched_label)
        # display_title/display_artist rather than the matched_track_*
        # fields directly, so a bulk-confirmed match (no detail fetched
        # yet) is compared against its release-level values instead of
        # against blank -- blank compares as "no disagreement", which
        # would quietly under-report problems on 124 of the current 399
        # linked rows. Comparing against the combined release title is
        # forgiving in the right direction: loose_text_match's word
        # overlap matches either side's song against "Side A / Side B".
        song_ok = discogs_api.loose_text_match(recording.song.title, match.display_title)
        artist_ok = discogs_api.loose_text_match(recording.credit_text, match.display_artist)
        flags = {
            "catalog": catalog_ok, "label": label_ok,
            "song": song_ok, "artist": artist_ok,
        }
        has_mismatch = not all(flags.values())
        if only_mismatches and not has_mismatch:
            continue
        rows.append({"match": match, "recording": recording, "flags": flags, "has_mismatch": has_mismatch})

    # Counted BEFORE pagination so the header reports the real total, not
    # just what's on this page.
    mismatch_count = sum(1 for r in rows if r["has_mismatch"])

    paginator = Paginator(rows, 50)
    page = paginator.get_page(request.GET.get("page"))

    # Sort links have to carry the current filters, same reasoning as the
    # recordings matrix's column headers.
    columns = {}
    for key, (_, label) in DISCOGS_AUDIT_SORTS.items():
        is_active = key == sort
        next_dir = "desc" if (is_active and direction == "asc") else "asc"
        params = {
            "status": status if status != "all" else "", "q": q,
            "mismatches": "1" if only_mismatches else "",
            "sort": key, "dir": next_dir,
        }
        columns[key] = {
            "label": label,
            "href": "?" + urlencode({k: v for k, v in params.items() if v}),
            "is_active": is_active,
            "arrow": ("▲" if direction == "asc" else "▼") if is_active else "",
        }

    context = {
        "page": page,
        "columns": columns,
        "status": status,
        "q": q,
        # Passed through so the filter form can carry the current sort as
        # hidden fields -- filtering shouldn't silently reset your sort.
        "sort": sort,
        "dir": direction,
        "only_mismatches": only_mismatches,
        "total_linked": DiscogsMatch.objects.filter(
            status__in=[DiscogsMatch.CONFIRMED, DiscogsMatch.MANUAL]
        ).count(),
        "shown_count": len(rows),
        "mismatch_count": mismatch_count,
        # Linked but never detail-fetched (bulk-confirm skips that step),
        # so their Artist/Song are release-level stand-ins. Surfaced as a
        # prompt rather than left as a silently-degraded display.
        "needs_detail_count": DiscogsMatch.objects.filter(DiscogsMatch.needs_detail_q()).count(),
        "base_querystring": urlencode({k: v for k, v in {
            "status": status if status != "all" else "", "q": q,
            "mismatches": "1" if only_mismatches else "",
            "sort": sort, "dir": direction,
        }.items() if v}),
    }
    return render(request, "charts/discogs_audit.html", context)


def discogs_queue(request):
    _ensure_discogs_matches()

    status = request.GET.get("status", "todo")
    q = request.GET.get("q", "").strip()

    matches = DiscogsMatch.objects.select_related(
        "recording", "recording__song", "recording__label"
    ).prefetch_related("candidates")

    if status == "todo":
        # The default view: everything not finished yet. That's unsettled
        # rows PLUS linked rows still waiting on their detail fetch.
        # Without the second half, a Discogs ID typed into the entry form
        # (status=manual, therefore "settled") vanished from this page
        # entirely and was never enriched, because nothing showed it.
        matches = matches.filter(
            ~Q(status__in=DiscogsMatch.SETTLED_STATUSES) | DiscogsMatch.needs_detail_q()
        )
    elif status != "all":
        matches = matches.filter(status=status)

    if q:
        for word in q.split():
            matches = matches.filter(
                Q(recording__song__title__icontains=word)
                | Q(recording__credit_text__icontains=word)
                | Q(recording__label__name__icontains=word)
                | Q(recording__catalog_number__icontains=word)
            )

    # Rows waiting on a detail fetch go first: they fetch themselves as
    # the page loads (see _discogs_row.html), so at the top you watch them
    # fill in rather than finding them on page 3. Then suggested (awaiting
    # a decision), then a stable alphabetical order within each status.
    matches = matches.annotate(
        detail_pending=Case(
            When(DiscogsMatch.needs_detail_q(), then=Value(0)),
            default=Value(1), output_field=IntegerField(),
        )
    ).order_by("detail_pending", "status", "recording__song__title", "recording__catalog_number")

    paginator = Paginator(matches, 25)
    page = paginator.get_page(request.GET.get("page"))

    # Does this page have anything the "confirm all exact matches" button
    # could act on? Drives whether that button renders at all. Counts
    # only matches with EXACTLY one EXACT candidate -- more than one
    # (the same catalog+label matching several distinct Discogs releases,
    # e.g. a US and a Canadian pressing) is a genuine tie that bulk-
    # confirm refuses to guess between, see discogs_bulk's confirm_exact.
    # This count has to agree with what that action will actually do, or
    # the button's own number would be a lie.
    page_exact_ids = [
        match.id for match in page
        if match.status == DiscogsMatch.SUGGESTED
        and sum(1 for c in match.candidates.all() if c.confidence == DiscogsMatch.EXACT) == 1
    ]

    progress = _discogs_progress()

    # Built here, not in the template: each option needs its own row
    # count next to it, and a Django template can't look a dict up by a
    # loop variable (`counts.value` reads the literal key "value").
    status_options = [
        {"value": "todo", "label": "Needs attention", "count": progress["total"] - progress["settled"]},
        {"value": "all", "label": "All", "count": progress["total"]},
    ] + [
        {"value": value, "label": label, "count": progress["counts"].get(value, 0)}
        for value, label in DiscogsMatch.STATUS_CHOICES
    ]

    context = {
        "page": page,
        "status": status,
        "q": q,
        "status_options": status_options,
        "progress": progress,
        "has_token": bool(discogs_api.get_token()),
        "batch_size": DISCOGS_WEB_BATCH_SIZE,
        "page_exact_ids": page_exact_ids,
        "unmatched_count": DiscogsMatch.objects.filter(status=DiscogsMatch.UNMATCHED).count(),
        # Preserved on every action link/form so acting on a row doesn't
        # bounce you back to page 1 of the default filter.
        "querystring": urlencode({k: v for k, v in {
            "status": status, "q": q, "page": page.number,
        }.items() if v}),
        # Set by discogs_bulk before its redirect -- popped so a refresh
        # doesn't keep re-showing the last batch's result line.
        "messages": request.session.pop("discogs_messages", []),
    }
    return render(request, "charts/discogs_queue.html", context)


def _store_candidates(match, candidates):
    """Replace a match's shortlist with a freshly-searched one."""
    with transaction.atomic():
        match.candidates.all().delete()
        for rank, data in enumerate(candidates):
            DiscogsCandidate.objects.create(match=match, rank=rank, **data)
        match.searched_at = timezone.now()
        # NO_MATCH rather than SUGGESTED when the search came back empty:
        # that's a real, informative outcome ("Discogs doesn't have this
        # 78"), not a failure, and it means the row stops cluttering the
        # to-do queue. A human can still re-search it later.
        match.status = DiscogsMatch.SUGGESTED if candidates else DiscogsMatch.NO_MATCH
        match.save()


def _render_discogs_row(request, match):
    # Every per-row action returns this same fragment, so a row looks
    # identical however it got into its current state -- same shared-
    # fragment approach as the entry form's _entry_resolved.html.
    return render(request, "charts/_discogs_row.html", {
        "match": match,
        "querystring": request.POST.get("querystring", ""),
    })


def discogs_search(request, recording_id):
    # HTMX target of a row's "Search Discogs" button. Live API call.
    match, _ = DiscogsMatch.objects.get_or_create(recording_id=recording_id)
    try:
        candidates = discogs_api.search_candidates(match.recording)
    except discogs_api.DiscogsError as exc:
        # Network/token/rate-limit problems are shown on the row, not
        # raised -- the rest of the page (and the app) stays usable.
        return render(request, "charts/_discogs_row.html", {
            "match": match, "error": str(exc), "querystring": request.POST.get("querystring", ""),
        })
    _store_candidates(match, candidates)
    return _render_discogs_row(request, match)


def _try_fetch_detail(match):
    """
    Fetch and apply full release detail (songwriters, producers, genres)
    for a match that already has a discogs_release_id, if it's missing
    them. One API call. Never raises -- returns an error string on
    failure (the caller shows it as a note; the match stays exactly as
    confirmed either way, just with needs_detail still True so the
    per-row "Fetch full details" button remains as a retry) or None on
    success/nothing-to-do.

    Shared by discogs_confirm and discogs_manual (auto-fetch right after
    a SINGLE-row confirm -- see the big comment above discogs_confirm for
    why single-row is the case this is safe/wanted for) and by
    discogs_fetch_detail itself (the explicit retry button).
    """
    if not match.needs_detail:
        return None
    try:
        detail = discogs_api.fetch_release_detail(match.discogs_release_id, match.recording)
    except discogs_api.DiscogsError as exc:
        return str(exc)
    for field, value in detail.items():
        setattr(match, field, value)
    match.save()
    return None


def discogs_confirm(request, recording_id):
    # HTMX target of a candidate's "This one" button.
    #
    # Confirming AND fetching full details happen in this one click --
    # deliberately, unlike the page's BULK "confirm exact matches"
    # button, which stays confirm-only/instant (see discogs_bulk) because
    # it can act on many rows at once and turning that into N sequential
    # API calls would make an instant click take N seconds. A single row
    # has no such problem: "Search Discogs" already makes one live call
    # per click, so one more call here to finish the job in the same
    # click is a better default than making every match need a second,
    # easy-to-forget button press. If the detail fetch itself fails
    # (rate limit, connection), the confirm still stands -- only the
    # detail fetch is retried, via the same per-row button as always.
    match = get_object_or_404(DiscogsMatch, recording_id=recording_id)
    candidate = get_object_or_404(DiscogsCandidate, pk=request.POST.get("candidate_id"), match=match)

    with transaction.atomic():
        # Copy the candidate's already-cached summary across first --
        # this part is genuinely free (no API call), so it happens
        # regardless of how the detail fetch below goes.
        match.discogs_release_id = candidate.discogs_release_id
        match.discogs_master_id = candidate.discogs_master_id
        match.matched_title = candidate.title
        match.matched_artist = candidate.artist
        match.matched_label = candidate.label
        match.matched_catalog_number = candidate.catalog_number
        match.matched_year = candidate.year
        match.matched_country = candidate.country
        match.matched_format = candidate.format
        match.confidence = candidate.confidence
        match.status = DiscogsMatch.CONFIRMED
        match.linked_at = timezone.now()
        match.save()
        match.candidates.all().delete()

    error = _try_fetch_detail(match)
    return render(request, "charts/_discogs_row.html", {
        "match": match, "error": error, "querystring": request.POST.get("querystring", ""),
    })


def _parse_discogs_reference(raw):
    """
    Turn a pasted Discogs URL or bare ID into (release_id, master_id) --
    at most one of which is set. Handles a full URL with slug
    ("https://www.discogs.com/release/12345-Some-Slug" or .../master/...),
    a bare path ("release/12345" / "master/12345"), and a bare number
    (assumed to mean a RELEASE, since that's what you get pasting from a
    normal discogs.com release page -- paste a full master/... URL if you
    specifically mean a master).

    Checking for "master/" FIRST matters: a release URL never contains
    the substring "master", but the reverse isn't guaranteed to be worth
    risking, and this way there's no ambiguity either way.

    Returns (release_id, master_id, error_message) -- error_message is
    set (and both ids None) when nothing recognizable was found.
    """
    raw = raw.strip()
    master_match = re.search(r"master/(\d+)", raw)
    if master_match:
        return None, int(master_match.group(1)), None
    release_match = re.search(r"release/(\d+)", raw)
    if release_match:
        return int(release_match.group(1)), None, None
    bare_match = re.fullmatch(r"\d+", raw)
    if bare_match:
        return int(raw), None, None
    return None, None, "Couldn't read a release or master ID out of that."


def discogs_manual(request, recording_id):
    # HTMX target of the "paste a Discogs release/master URL or ID" box,
    # for when the search genuinely can't find it but you've found it
    # yourself on discogs.com. Same "finish the job in one click" as
    # discogs_confirm above: a release reference gets its full details
    # fetched right here too. A master-only reference can't (see
    # needs_detail on DiscogsMatch) -- _try_fetch_detail is a no-op then.
    match, _ = DiscogsMatch.objects.get_or_create(recording_id=recording_id)
    raw = (request.POST.get("discogs_ref") or "").strip()

    release_id, master_id, parse_error = _parse_discogs_reference(raw)
    if parse_error:
        return render(request, "charts/_discogs_row.html", {
            "match": match, "error": parse_error,
            "querystring": request.POST.get("querystring", ""),
        })

    with transaction.atomic():
        match.discogs_release_id = release_id
        match.discogs_master_id = master_id
        match.status = DiscogsMatch.MANUAL
        match.linked_at = timezone.now()
        match.confidence = ""
        # Deliberately cleared: whatever summary was cached described a
        # DIFFERENT release, and leaving it would show misleading data
        # next to the new ID until the detail fetch below runs (or fails).
        match.matched_title = match.matched_artist = match.matched_label = ""
        match.matched_track_title = match.matched_track_artist = ""
        match.matched_catalog_number = match.matched_country = match.matched_format = ""
        match.matched_year = None
        match.raw_data = None
        match.save()
        match.candidates.all().delete()

    fetch_error = _try_fetch_detail(match)
    return render(request, "charts/_discogs_row.html", {
        "match": match, "error": fetch_error, "querystring": request.POST.get("querystring", ""),
    })


def discogs_set_status(request, recording_id):
    # HTMX target of "None of these" / "Not on Discogs" / "Reset".
    match, _ = DiscogsMatch.objects.get_or_create(recording_id=recording_id)
    new_status = request.POST.get("status")
    valid = {choice for choice, _ in DiscogsMatch.STATUS_CHOICES}
    if new_status in valid:
        with transaction.atomic():
            match.status = new_status
            if new_status == DiscogsMatch.UNMATCHED:
                # A full reset -- drop the link and everything cached
                # about it, so the row goes back to looking untouched.
                match.discogs_release_id = match.discogs_master_id = None
                match.confidence = ""
                match.linked_at = None
                match.matched_title = match.matched_artist = match.matched_label = ""
                match.matched_track_title = match.matched_track_artist = ""
                match.matched_catalog_number = match.matched_country = match.matched_format = ""
                match.matched_year = None
                match.songwriters = match.producers = ""
                match.genres = match.styles = []
                match.raw_data = None
            match.save()
            if new_status in (DiscogsMatch.REJECTED, DiscogsMatch.NO_MATCH, DiscogsMatch.UNMATCHED):
                match.candidates.all().delete()
    return _render_discogs_row(request, match)


def discogs_fetch_detail(request, recording_id):
    # HTMX target of a linked row's "fetch full details" button -- the
    # explicit retry/catch-up path for whatever _try_fetch_detail's
    # automatic attempt (in discogs_confirm/discogs_manual) didn't cover:
    # it failed the first time, or this match came from the BULK confirm
    # action (which stays detail-free on purpose, see discogs_bulk), or
    # from before this auto-fetch behavior existed at all.
    match = get_object_or_404(DiscogsMatch, recording_id=recording_id)
    if not match.discogs_release_id:
        return render(request, "charts/_discogs_row.html", {
            "match": match, "error": "No release ID on this match yet.",
            "querystring": request.POST.get("querystring", ""),
        })
    error = _try_fetch_detail(match)
    return render(request, "charts/_discogs_row.html", {
        "match": match, "error": error, "querystring": request.POST.get("querystring", ""),
    })


def discogs_bulk(request):
    """
    The page-level batch actions. Full page POST + redirect (not HTMX):
    each of these changes many rows at once, so re-rendering the whole
    queue is both simpler and more honest about what happened than
    trying to patch N rows in place.
    """
    action = request.POST.get("action")
    querystring = request.POST.get("querystring", "")
    messages = []

    if action == "search_batch":
        # Search the next N never-looked-at recordings. Held-open request
        # of roughly N seconds -- see DISCOGS_WEB_BATCH_SIZE.
        pending = list(
            DiscogsMatch.objects.filter(status=DiscogsMatch.UNMATCHED)
            .select_related("recording", "recording__song", "recording__label")[:DISCOGS_WEB_BATCH_SIZE]
        )
        done = 0
        for match in pending:
            try:
                _store_candidates(match, discogs_api.search_candidates(match.recording))
                done += 1
            except discogs_api.DiscogsError as exc:
                # Stop the batch at the first failure rather than
                # hammering a rate limit or a down API N more times.
                messages.append(f"Stopped after {done}: {exc}")
                break
        else:
            messages.append(f"Searched {done} recording{'' if done == 1 else 's'}.")

    elif action == "confirm_exact":
        # Confirm every EXACT-confidence suggestion on the current page,
        # AND fetch each one's per-track detail in the same click.
        #
        # This used to skip the detail fetch to stay instant, leaving
        # those rows showing release-level stand-ins (the "rel" tag) with
        # no songwriter credits until someone remembered to run the
        # catch-up step. In practice that follow-up got forgotten and
        # 124 rows sat half-populated, which is a worse outcome than a
        # slower click. Confirming and enriching are now one operation,
        # so no path through the UI can leave a linked row incomplete.
        #
        # Cost is bounded and predictable: at most one pageful (25) at
        # roughly a second each. On an API failure partway through, the
        # confirms already made stand and the rest are reported as still
        # needing detail -- recoverable via the "Fetch details" action,
        # which stays for exactly that case.
        ids = request.POST.getlist("match_id")
        confirmed = 0
        enriched = 0
        skipped_ties = 0
        detail_error = None
        for match in DiscogsMatch.objects.filter(id__in=ids, status=DiscogsMatch.SUGGESTED).prefetch_related("candidates"):
            exact_candidates = [c for c in match.candidates.all() if c.confidence == DiscogsMatch.EXACT]
            if not exact_candidates:
                continue
            if len(exact_candidates) > 1:
                # A genuine tie -- the same catalog+label matches more
                # than one distinct Discogs release (different country/
                # year/pressing sharing a catalog number, which really
                # happens -- see the reasoning in discogs.search_
                # candidates). Left as 'suggested' rather than picking
                # one: they're already sorted with the most likely one
                # (US, earliest year) first, so this only needs one
                # deliberate click to resolve, not a guess made for you.
                skipped_ties += 1
                continue
            exact = exact_candidates[0]
            with transaction.atomic():
                match.discogs_release_id = exact.discogs_release_id
                match.discogs_master_id = exact.discogs_master_id
                match.matched_title = exact.title
                match.matched_artist = exact.artist
                match.matched_label = exact.label
                match.matched_catalog_number = exact.catalog_number
                match.matched_year = exact.year
                match.matched_country = exact.country
                match.matched_format = exact.format
                match.confidence = exact.confidence
                match.status = DiscogsMatch.CONFIRMED
                match.linked_at = timezone.now()
                match.save()
                match.candidates.all().delete()
                confirmed += 1

            # Enrich immediately, outside the transaction above -- the
            # confirm is already durable, so a network failure here can
            # never roll back a decision that was correctly made.
            if detail_error is None:
                detail_error = _try_fetch_detail(match)
                if detail_error is None:
                    enriched += 1
            # Once the API has failed once, stop trying: a rate limit or
            # a dead connection will fail identically for every
            # remaining row, and hammering it just makes the request
            # longer. Those rows stay confirmed, just not yet enriched.

        message = f"Confirmed {confirmed} exact match{'' if confirmed == 1 else 'es'}"
        message += f", with full details fetched for {enriched}." if enriched else "."
        if detail_error:
            pending = confirmed - enriched
            message += (
                f" Detail fetch stopped after {enriched} ({detail_error}) -- "
                f"{pending} still need it; use \"Fetch details\" to finish."
            )
        if skipped_ties:
            message += f" Skipped {skipped_ties} with more than one exact match -- needs a manual pick."
        messages.append(message)

    elif action == "fetch_details":
        # Fill in songwriters/producers/etc for confirmed matches that
        # don't have them yet, a batch at a time.
        # needs_detail_q, not a hand-written filter: the old one included
        # master-only matches, which can never be fetched -- enough of them
        # could fill a whole batch and make this button do nothing forever.
        pending = list(
            DiscogsMatch.objects.filter(DiscogsMatch.needs_detail_q())
            .select_related("recording", "recording__song")[:DISCOGS_WEB_BATCH_SIZE]
        )
        done = 0
        for match in pending:
            if not match.discogs_release_id:
                continue
            try:
                detail = discogs_api.fetch_release_detail(match.discogs_release_id, match.recording)
            except discogs_api.DiscogsError as exc:
                messages.append(f"Stopped after {done}: {exc}")
                break
            for field, value in detail.items():
                setattr(match, field, value)
            match.save()
            done += 1
        else:
            messages.append(f"Fetched details for {done} match{'' if done == 1 else 'es'}.")

    request.session["discogs_messages"] = messages
    return redirect(f"{reverse('charts:discogs_queue')}?{querystring}")
