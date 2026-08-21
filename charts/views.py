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
from datetime import datetime, timedelta
from urllib.parse import urlencode

from django.core.exceptions import ValidationError
from django.core.paginator import Paginator
from django.db import IntegrityError, transaction
from django.db.models import Count, Max, Min, Q
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from . import discogs as discogs_api
from .forms import ChartEntryRowForm
from .models import (
    Artist, ChartEntry, Chart, DiscogsCandidate, DiscogsMatch, Label,
    Recording, RecordingCredit, Song,
)


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

    # Prev/next week for the navigation arrows -- just the neighbors of
    # selected_week in the sorted list already loaded above, no extra query.
    idx = all_weeks.index(selected_week)
    prev_week = all_weeks[idx - 1] if idx > 0 else None
    next_week = all_weeks[idx + 1] if idx < len(all_weeks) - 1 else None

    # The printed chart name active on this specific week, from ChartEra --
    # NOT stored per-entry, looked up by date range (see CLAUDE.md's
    # Chart/ChartEra design).
    era = chart.eras.filter(start_date__lte=selected_week).filter(
        Q(end_date__isnull=True) | Q(end_date__gte=selected_week)
    ).first()

    entries = (
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

    rows = []
    for e in entries:
        last = e.printed_last_week_position
        if last is None:
            movement = "new"
        elif last > e.position:
            movement = "up"
        elif last < e.position:
            movement = "down"
        else:
            movement = "same"
        rows.append({
            "entry": e,
            "movement": movement,
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
        "era": era,
        "selected_week": selected_week,
        "prev_week": prev_week,
        "next_week": next_week,
        "rows": rows,
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


def _recordings_matrix_query(request):
    """
    Shared by recordings_matrix's full-page render and its own HTMX live-
    filter response (same view, see below): builds the filtered, sorted
    (chart, recording) rows plus the current filter/sort state.

    One row per CHART a recording has appeared on, not one row per
    recording overall -- Peak/Weeks/First/Last are only ever well-defined
    WITHIN a single chart (a record's peak on Country Jukebox and its
    peak on R&B Retail are two unrelated numbers printed in two unrelated
    magazines' pages -- merging them would invent a "peak" nobody ever
    printed). Filtering by genre or a specific chart just narrows which
    of those (chart, recording) rows show; grouping by (chart_id,
    recording_id) via .values() below is what keeps every row's numbers
    scoped to just that one chart, same guarantee the old per-chart-only
    page had, without needing this chart to be fixed ahead of time.
    """
    genre = request.GET.get("genre", "")
    chart_id = request.GET.get("chart", "")
    q = request.GET.get("q", "").strip()
    sort = request.GET.get("sort", "peak")
    direction = request.GET.get("dir", "asc")
    if sort not in RECORDINGS_MATRIX_SORTS:
        sort = "peak"

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

    rows = (
        entries
        .values(
            "chart_id", "chart__category", "chart__method",
            "recording_id", "recording__song__title", "recording__credit_text",
            "recording__label__name", "recording__catalog_number", "recording__side",
            # Discogs link state, for the matrix's status column. Pulled
            # in via the existing join rather than a second query, and
            # entirely from what's CACHED in our own database -- rendering
            # this column makes no API call. Recordings with no
            # DiscogsMatch row yet come back as None, which the template
            # renders as an em-dash.
            "recording__discogs__status",
            "recording__discogs__discogs_release_id",
            "recording__discogs__discogs_master_id",
            "recording__discogs__matched_artist",
            "recording__discogs__matched_catalog_number",
            "recording__discogs__songwriters",
        )
        .annotate(
            peak_position=Min("position"),
            # distinct=True on week_date, not a plain Count("id"): the 2
            # accepted printed-anomaly weeks (printed_anomaly_note / see
            # docs/audit/same_recording_repeated.csv) have the SAME
            # recording listed twice in the SAME week under two
            # positions. A plain row count would say those charted one
            # extra week they didn't; counting distinct week_dates means
            # "Weeks" always answers "how many calendar weeks", whatever
            # the row count behind it.
            weeks_charted=Count("week_date", distinct=True),
            first_entry=Min("week_date"),
            last_entry=Max("week_date"),
        )
    )

    order_field, _ = RECORDINGS_MATRIX_SORTS[sort]
    if direction == "desc":
        order_field = "-" + order_field
    # chart__method as a SECOND sort key specifically when sorting by
    # Chart, so same-genre charts cluster together in a stable order
    # (Jockeys/Jukebox/Retail) instead of an arbitrary one; song title as
    # the final tiebreaker always, so rows sharing a value on the primary
    # sort come back in a stable, readable order across reloads.
    tiebreakers = ["recording__song__title"]
    if sort == "chart":
        tiebreakers = ["chart__method"] + tiebreakers
    rows = list(rows.order_by(order_field, *tiebreakers))

    # .values()/.annotate() returns plain dicts, not Recording/Chart
    # instances -- there's no catalog_display property or __str__ to
    # reuse here, so both are built by hand, once, per row.
    chart_labels = {c.id: str(c) for c in Chart.objects.all()}
    for row in rows:
        row["chart_label"] = chart_labels[row["chart_id"]]
        parts = [row["recording__label__name"], row["recording__catalog_number"]]
        if row["recording__side"]:
            parts.append(row["recording__side"])
        row["catalog_display"] = " ".join(parts)
        # Plain outbound discogs.com URL, built from the cached id -- a
        # string, not a request. Release preferred (the precise, common
        # case); Master is the fallback for a manually-entered reference
        # that could only be pinned down to the work, not the specific
        # pressing (see DiscogsReferenceMixin). Blank when there's no
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

    state = {"genre": genre, "chart": chart_id, "q": q, "sort": sort, "dir": direction}
    return rows, state


def _recordings_matrix_columns(state):
    # Build each sortable column header's link here rather than in the
    # template: its target direction depends on whether IT is the
    # currently-active sort column (click again to flip asc/desc) or not
    # (click switches to it, defaulting to asc), AND it has to carry the
    # CURRENT genre/chart/q filters forward too, or sorting would silently
    # drop whatever you'd just filtered by. Keyed by sort key so the
    # template can place each header exactly where it wants it, e.g.
    # {{ columns.peak.href }}, rather than looping in a fixed order.
    columns = {}
    for key, (_, label) in RECORDINGS_MATRIX_SORTS.items():
        is_active = key == state["sort"]
        next_dir = "desc" if (is_active and state["dir"] == "asc") else "asc"
        params = {"genre": state["genre"], "chart": state["chart"], "q": state["q"], "sort": key, "dir": next_dir}
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
#   entry_week (POST)     The actual save. Every row is validated through
#                         ChartEntryRowForm; if ANY row fails, nothing is
#                         written (wrapped in transaction.atomic()) and the
#                         whole form re-renders with everything the user
#                         already entered still in place, per CLAUDE.md's
#                         "save the whole week atomically" rule.


def _week_streak(chart, recording, before_week):
    """
    Walk backward week-by-week (7-day steps) from `before_week`, counting
    how many CONSECUTIVE prior weeks `recording` appears in `chart` (at
    any position), and the best (lowest) position seen in that streak.

    This is ONLY ever shown as a "computed, for comparison" hint next to
    the printed peak/weeks-on-chart inputs -- never auto-filled. Per
    CLAUDE.md's "Derived fields as a typo detector" section, the magazine's
    printed value is what gets stored; this just helps a mismatch catch
    the eye immediately instead of needing a separate audit pass later.

    Returns (weeks_so_far, best_position_so_far), both None if there's no
    unbroken run of prior appearances immediately before this week.
    """
    weeks_so_far = 0
    best_position = None
    cursor = before_week
    while True:
        cursor = cursor - timedelta(days=7)
        entry = chart.entries.filter(week_date=cursor, recording=recording).order_by("position").first()
        if entry is None:
            break
        weeks_so_far += 1
        if best_position is None or entry.position < best_position:
            best_position = entry.position
    return (weeks_so_far or None, best_position)


def entry_week(request, chart_id):
    chart = get_object_or_404(Chart, pk=chart_id)

    if request.method == "POST":
        return _save_entry_week(request, chart)

    week_param = request.GET.get("week")
    last_entered = chart.entries.order_by("-week_date").values_list("week_date", flat=True).first()

    if week_param:
        try:
            week_date = datetime.strptime(week_param, "%Y-%m-%d").date()
        except ValueError:
            raise Http404("Malformed week date.")
    elif last_entered:
        # Default to the week right after whatever's most recently on file.
        week_date = last_entered + timedelta(days=7)
    else:
        # A chart with literally nothing on file yet (not the case for any
        # of the 5 real charts today, but the schema allows adding a 6th
        # someday) -- there's no sensible date to guess, so just ask.
        return render(request, "charts/entry_week.html", {"chart": chart, "week_date": None})

    previous_week_date = week_date - timedelta(days=7)
    previous_week_count = chart.entries.filter(week_date=previous_week_date).count()
    row_count = previous_week_count or 10  # first-ever week for this chart: a reasonable starting guess

    rows = [
        {
            "index": i, "position": i + 1, "last_week_position": None, "recording": None,
            "printed_peak_position": None, "printed_weeks_on_chart": None, "printed_anomaly_note": "",
            "weeks_hint": None, "peak_hint": None,
        }
        for i in range(row_count)
    ]

    context = {
        "chart": chart,
        "week_date": week_date,
        "week_date_str": week_date.isoformat(),
        "previous_week_date": previous_week_date,
        "rows": rows,
        "next_index": row_count,
        "labels": Label.objects.order_by("name"),
        "artists": Artist.objects.order_by("canonical_name"),
        "songs": Song.objects.order_by("title"),
    }
    return render(request, "charts/entry_week.html", context)


def _parse_submitted_rows(request):
    """
    Shared by both a brand-new week's save and an existing week's edit
    save. Discovers which row indices were actually submitted (rather
    than assuming a contiguous 0..N-1 range -- a row deleted from the
    middle, see the delete button in _entry_row.html, leaves a gap, and
    that has to still work correctly), validates each one through
    ChartEntryRowForm, and builds a repopulated row list suitable for
    re-rendering entry_week.html if anything's wrong.

    Returns (cleaned_rows, rows_for_rerender, errors, next_index).
    """
    row_indices = sorted({
        int(m.group(1)) for key in request.POST if (m := re.fullmatch(r"position_(\d+)", key))
    })
    next_index = (max(row_indices) + 1) if row_indices else 0

    cleaned_rows = []
    errors = []
    rows_for_rerender = []

    for i in row_indices:
        raw = {
            "position": request.POST.get(f"position_{i}"),
            "last_week_position": request.POST.get(f"last_week_position_{i}") or None,
            "recording_id": request.POST.get(f"recording_id_{i}"),
            "printed_peak_position": request.POST.get(f"printed_peak_position_{i}") or None,
            "printed_weeks_on_chart": request.POST.get(f"printed_weeks_on_chart_{i}") or None,
            "printed_anomaly_note": request.POST.get(f"printed_anomaly_note_{i}", ""),
        }
        form = ChartEntryRowForm(raw)

        recording = None
        if raw["recording_id"]:
            recording = Recording.objects.filter(pk=raw["recording_id"]).select_related("song", "label").first()

        if form.is_valid():
            cleaned_rows.append(form.cleaned_data)
        else:
            field_errors = "; ".join(f"{field}: {', '.join(msgs)}" for field, msgs in form.errors.items())
            errors.append(f"Row {i + 1}: {field_errors}")

        rows_for_rerender.append({
            "index": i,
            "position": raw["position"] or (i + 1),
            "last_week_position": raw["last_week_position"],
            "recording": recording,
            "printed_peak_position": raw["printed_peak_position"],
            "printed_weeks_on_chart": raw["printed_weeks_on_chart"],
            "printed_anomaly_note": raw["printed_anomaly_note"],
            "weeks_hint": None,
            "peak_hint": None,
        })

    return cleaned_rows, rows_for_rerender, errors, next_index


def _write_chart_entries(chart, week_date, cleaned_rows, replace_existing):
    """
    Atomically writes `cleaned_rows` as this chart-week's entries. If
    replace_existing, first deletes whatever's currently on file for this
    exact (chart, week_date) -- safe because nothing else in the schema
    has a foreign key pointing AT ChartEntry, so there's nothing left
    dangling. Used by both a fresh week's save (replace_existing=False,
    there's nothing to replace) and an edit of an existing week
    (replace_existing=True, "save the whole week atomically" extended to
    "re-save the whole week atomically" for corrections).

    Returns a list of error strings; empty means it succeeded.
    """
    errors = []
    try:
        with transaction.atomic():
            if replace_existing:
                chart.entries.filter(week_date=week_date).delete()
            for cleaned in cleaned_rows:
                entry = ChartEntry(
                    chart=chart,
                    week_date=week_date,
                    position=cleaned["position"],
                    recording_id=cleaned["recording_id"],
                    printed_last_week_position=cleaned.get("last_week_position"),
                    printed_peak_position=cleaned.get("printed_peak_position"),
                    printed_weeks_on_chart=cleaned.get("printed_weeks_on_chart"),
                    printed_anomaly_note=cleaned.get("printed_anomaly_note") or "",
                )
                # full_clean(), not just save() -- see the NOTE in
                # ChartEntry.clean() about save() not calling it
                # automatically. This is what enforces the publication-
                # weekday and position-in-range checks here.
                entry.full_clean()
                entry.save()
    except ValidationError as e:
        errors.append("Save failed: " + "; ".join(e.messages))
    except IntegrityError as e:
        errors.append(f"Save failed (a database constraint rejected this): {e}")
    return errors


def _save_entry_week(request, chart):
    week_str = request.POST.get("week_date")
    try:
        week_date = datetime.strptime(week_str, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return redirect("charts:entry_week", chart_id=chart.id)

    previous_week_date = week_date - timedelta(days=7)
    cleaned_rows, rows_for_rerender, errors, next_index = _parse_submitted_rows(request)

    if not errors:
        errors = _write_chart_entries(chart, week_date, cleaned_rows, replace_existing=False)

    if errors:
        # Nothing was written -- transaction.atomic() rolled back the
        # whole batch if we got as far as trying to save. Re-render with
        # everything the user already entered still in place rather than
        # making them start the week over.
        context = {
            "chart": chart,
            "week_date": week_date,
            "week_date_str": week_date.isoformat(),
            "previous_week_date": previous_week_date,
            "rows": rows_for_rerender,
            "next_index": next_index,
            "labels": Label.objects.order_by("name"),
        "artists": Artist.objects.order_by("canonical_name"),
        "songs": Song.objects.order_by("title"),
            "errors": errors,
        }
        return render(request, "charts/entry_week.html", context)

    return redirect("charts:chart_week", chart_id=chart.id, week_date=week_date.isoformat())


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

    previous_week_date = target_week - timedelta(days=7)

    if request.method == "POST":
        cleaned_rows, rows_for_rerender, errors, next_index = _parse_submitted_rows(request)
        if not errors:
            errors = _write_chart_entries(chart, target_week, cleaned_rows, replace_existing=True)
        if errors:
            context = {
                "chart": chart,
                "week_date": target_week,
                "week_date_str": target_week.isoformat(),
                "previous_week_date": previous_week_date,
                "rows": rows_for_rerender,
                "next_index": next_index,
                "labels": Label.objects.order_by("name"),
        "artists": Artist.objects.order_by("canonical_name"),
        "songs": Song.objects.order_by("title"),
                "errors": errors,
                "is_edit": True,
            }
            return render(request, "charts/entry_week.html", context)
        return redirect("charts:chart_week", chart_id=chart.id, week_date=target_week.isoformat())

    # GET: seed rows from what's already saved, not from last week's count.
    # Ordered by (position, id) so a tie's row order stays stable across
    # reloads instead of shuffling.
    existing = (
        chart.entries.filter(week_date=target_week)
        .select_related("recording", "recording__song", "recording__label")
        .order_by("position", "id")
    )
    rows = []
    for i, e in enumerate(existing):
        weeks_hint, peak_hint = _week_streak(chart, e.recording, target_week)
        rows.append({
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

    context = {
        "chart": chart,
        "week_date": target_week,
        "week_date_str": target_week.isoformat(),
        "previous_week_date": previous_week_date,
        "rows": rows,
        "next_index": len(rows),
        "labels": Label.objects.order_by("name"),
        "artists": Artist.objects.order_by("canonical_name"),
        "songs": Song.objects.order_by("title"),
        "is_edit": True,
    }
    return render(request, "charts/entry_week.html", context)


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

    previous_week_date = week_date - timedelta(days=7)
    matches = list(
        chart.entries.filter(week_date=previous_week_date, position=last_week_position)
        .select_related("recording", "recording__song", "recording__label")
    )

    if not matches:
        error = f"nothing at #{last_week_position} on {previous_week_date}"
        return render(request, "charts/_entry_resolved.html", {"row": row, "recording": None, "error": error, "include_wks_hint_oob": True})

    if len(matches) > 1:
        # A genuine tie last week -- can't tell which one continues into
        # this row from the position number alone.
        return render(request, "charts/_entry_ambiguous.html", {
            "chart": chart, "row": row, "week_date_str": week_str, "matches": matches,
        })

    recording = matches[0].recording
    weeks_hint, peak_hint = _week_streak(chart, recording, week_date)
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


def entry_select(request, chart_id):
    # HTMX target of clicking a search result (or an ambiguous-tie pick).
    # Returns the same preview fragment entry_resolve uses, so a row looks
    # identical no matter which of the three paths resolved it.
    chart = get_object_or_404(Chart, pk=chart_id)
    row = request.POST.get("row", "0")
    week_str = request.POST.get("week")
    recording = get_object_or_404(Recording.objects.select_related("song", "label"), pk=request.POST.get("recording_id"))

    weeks_hint = peak_hint = None
    if week_str:
        try:
            week_date = datetime.strptime(week_str, "%Y-%m-%d").date()
            weeks_hint, peak_hint = _week_streak(chart, recording, week_date)
        except ValueError:
            pass

    return render(request, "charts/_entry_resolved.html", {
        "row": row, "recording": recording, "weeks_hint": weeks_hint, "peak_hint": peak_hint,
        "include_wks_hint_oob": True,
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
    artist_name = request.POST.get(f"new_artist_{row}", "").strip()
    credit_text = request.POST.get(f"new_credit_text_{row}", "").strip() or artist_name
    label_name = request.POST.get(f"new_label_{row}", "").strip()
    catalog_number = request.POST.get(f"new_catalog_{row}", "").strip()
    side = request.POST.get(f"new_side_{row}", "").strip().upper()

    missing = [
        label for label, value in [
            ("song title", song_title), ("artist", artist_name),
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
        artist, _ = Artist.objects.get_or_create(canonical_name=artist_name)
        label, _ = Label.objects.get_or_create(name=label_name)
        # get_or_create on the actual unique constraint -- if this exact
        # label+catalog+side already exists, reuse it (and its existing
        # song/credit_text) rather than erroring or silently duplicating.
        recording, created = Recording.objects.get_or_create(
            label=label, catalog_number=catalog_number, side=side,
            defaults={"song": song, "credit_text": credit_text},
        )
        if created:
            RecordingCredit.objects.create(recording=recording, artist=artist, credit_order=1)

    weeks_hint = peak_hint = None
    if week_str:
        try:
            week_date = datetime.strptime(week_str, "%Y-%m-%d").date()
            weeks_hint, peak_hint = _week_streak(chart, recording, week_date)
        except ValueError:
            pass

    return render(request, "charts/_entry_resolved.html", {
        "row": row, "recording": recording, "weeks_hint": weeks_hint, "peak_hint": peak_hint,
        "include_wks_hint_oob": True,
    })


def entry_add_row(request, chart_id):
    # HTMX target of "+ Add row" (ties, or extra debuts beyond the row
    # count seeded from last week). Appends one empty row and, via an
    # out-of-band swap, bumps the hidden total_rows counter the final
    # save reads -- one response updating two places in the page.
    chart = get_object_or_404(Chart, pk=chart_id)
    next_index = int(request.GET.get("next_index", 0))
    week_str = request.GET.get("week", "")

    row = {
        "index": next_index, "position": next_index + 1, "last_week_position": None, "recording": None,
        "printed_peak_position": None, "printed_weeks_on_chart": None, "printed_anomaly_note": "",
        "weeks_hint": None, "peak_hint": None,
    }
    return render(request, "charts/_entry_add_row_response.html", {
        "chart": chart, "row": row, "week_date_str": week_str, "new_total": next_index + 1,
    })


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
    settled = sum(counts.get(status, 0) for status in DiscogsMatch.SETTLED_STATUSES)
    return {
        "total": total,
        "settled": settled,
        "percent": round(100 * settled / total) if total else 0,
        "counts": counts,
        # Confirmed/manual matches whose expensive detail fetch hasn't run
        # yet -- surfaced as a "fetch these" prompt on the page.
        "needs_detail": DiscogsMatch.objects.filter(
            status__in=[DiscogsMatch.CONFIRMED, DiscogsMatch.MANUAL], raw_data__isnull=True
        ).count(),
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
        "needs_detail_count": DiscogsMatch.objects.filter(
            status__in=[DiscogsMatch.CONFIRMED, DiscogsMatch.MANUAL],
            raw_data__isnull=True, discogs_release_id__isnull=False,
        ).count(),
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
        # The default view: everything a human hasn't settled yet.
        matches = matches.exclude(status__in=DiscogsMatch.SETTLED_STATUSES)
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

    # suggested first (they're the ones actually awaiting a decision),
    # then a stable alphabetical order within each status.
    matches = matches.order_by("status", "recording__song__title", "recording__catalog_number")

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
        # Confirm every EXACT-confidence suggestion on the current page.
        # Deliberately STILL no API calls here, unlike the single-row
        # discogs_confirm (which now also fetches full details in the
        # same click -- see the comment there): this can act on up to a
        # pageful of rows at once, and turning that into one API call per
        # row would make an instant click take as long as a batch search.
        # Use the "Fetch details" bulk action afterward to catch these up.
        ids = request.POST.getlist("match_id")
        confirmed = 0
        skipped_ties = 0
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
        message = f"Confirmed {confirmed} exact match{'' if confirmed == 1 else 'es'}."
        if skipped_ties:
            message += f" Skipped {skipped_ties} with more than one exact match -- needs a manual pick."
        messages.append(message)

    elif action == "fetch_details":
        # Fill in songwriters/producers/etc for confirmed matches that
        # don't have them yet, a batch at a time.
        pending = list(
            DiscogsMatch.objects.filter(
                status__in=[DiscogsMatch.CONFIRMED, DiscogsMatch.MANUAL], raw_data__isnull=True
            ).select_related("recording", "recording__song")[:DISCOGS_WEB_BATCH_SIZE]
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
