# charts/views.py
#
# Two feature areas:
#   - the read-only chart BROWSER (chart_list, chart_week) -- never
#     writes anything, just a nicer way to look at what's on file than
#     paging through the admin's flat entry list.
#   - the weekly entry FORM (entry_week + the entry_* HTMX endpoints) --
#     CLAUDE.md's "heart of the project". See the big comment above
#     entry_week() for how the pieces fit together.

import re
from datetime import datetime, timedelta
from urllib.parse import urlencode

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Count, Max, Min, Q
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render

from .forms import ChartEntryRowForm
from .models import Artist, ChartEntry, Chart, Label, Recording, RecordingCredit, Song


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
