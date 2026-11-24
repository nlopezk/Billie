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

from collections import defaultdict
from datetime import datetime
from urllib.parse import urlencode

from django.core.paginator import Paginator
from django.db.models import Case, Count, IntegerField, Q, Value, When
from django.http import Http404, HttpResponse
from django.views.decorators.http import require_POST
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from . import artists as artists_lib
from . import chart_week as chart_week_lib
from . import credits as credits_lib
from . import dashboard as dashboard_lib
from . import entry_recording as entry_recording_lib
from . import lastweek_audit as lastweek_audit_lib
from . import playlists as playlists_lib
from . import recordings_matrix as recordings_matrix_lib
from . import search as search_lib
from . import week_submission
from . import discogs as discogs_api
from . import discogs_workflow
from . import spotify as spotify_api
from . import spotify_workflow
from . import spotify_push
from .models import (
    Artist, ChartEntry, Chart, DiscogsCandidate, DiscogsMatch, Label,
    Playlist, Recording, Song, SpotifyCandidate, SpotifyMatch, sole_credited_artist_ids,
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
    # Weeks on chart / run / peak: COMPUTED from the entries on file (one
    # query for the whole week), never typed -- shown as computed.
    standings = week.standings({e.recording_id for e in entries})
    # A printed credit links to its artist's profile when exactly one
    # artist is credited; collaborations stay plain text.
    sole_artist = sole_credited_artist_ids([e.recording_id for e in entries])
    rows = []
    for e in entries:
        moved = week.movement(e.recording_id, e.position, e.printed_last_week_position)
        rows.append({
            "standing": standings[e.recording_id],
            "artist_id": sole_artist.get(e.recording_id),
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
    # Every row and number comes from recordings_matrix_lib.build(), next
    # to its tests -- this view only picks which template to render.
    context = _matrix_context(request.GET)
    if request.headers.get("HX-Request"):
        return render(request, "charts/_recordings_matrix_results.html", context)
    return render(request, "charts/recordings_matrix.html", context)


def _matrix_context(params, default_group="chart"):
    """Everything the shared matrix filter row (_matrix_filters.html) and
    results table need, for the recordings page and the playlist picker.

    The date slider is the dashboard's (dashboard.parse_filters snaps the
    from/to onto real chart weeks, range_context feeds the slider), and the
    snapped dates are what the matrix query gets -- so the slider and the
    numbers in the table can never disagree about the window."""
    weeks = dashboard_lib.all_weeks()
    query = {key: params.get(key) for key in params}
    query.setdefault("group", default_group)
    context = {}
    if weeks:
        filters = dashboard_lib.parse_filters(params, weeks)
        query["from"] = filters["date_from"].isoformat()
        query["to"] = filters["date_to"].isoformat()
        context.update(filters=filters, **dashboard_lib.range_context(filters, weeks))
    context.update(
        recordings_matrix_lib.build(query),
        genre_choices=Chart.CATEGORY_CHOICES,
        all_charts=Chart.objects.order_by("category", "method"),
    )
    return context


# =============================================================================
# Playlists -- /playlists/ (GLOSSARY.md: a hand-picked list of recordings)
# =============================================================================
#
# Every rule lives in charts/playlists.py; these views only read the request
# and pick a template. Writes are POST-only (@require_POST), so a prefetching
# browser or a stray GET can never add, remove or delete anything.

def playlist_list(request):
    # GET: every playlist with its size. POST: create one, then go straight
    # to its picker -- a new playlist is empty, so picking is the next step.
    error = None
    if request.method == "POST":
        playlist, error = playlists_lib.create(request.POST.get("name"), request.POST.get("description"))
        if playlist:
            return redirect("charts:playlist_pick", playlist_id=playlist.pk)
    playlists = Playlist.objects.annotate(size=Count("items")).order_by("name")
    return render(request, "charts/playlist_list.html", {
        "playlists": playlists, "error": error, "typed_name": request.POST.get("name", ""),
    })


def playlist_detail(request, playlist_id):
    # One playlist, in its order, with all-data numbers per recording.
    playlist = get_object_or_404(Playlist, pk=playlist_id)
    return render(request, "charts/playlist_detail.html", {
        "playlist": playlist,
        "rows": playlists_lib.rows(playlist),
        "spotify": playlists_lib.spotify_readiness(playlist),
        # Its copy on Spotify: changes since the last push (None = never
        # pushed), worked out locally -- no Spotify call on this page.
        "spotify_changes": spotify_push.changes(playlist),
        "spotify_playlist_url": spotify_api.playlist_url(playlist.spotify_playlist_id),
        "message": request.session.pop("playlist_message", None),
        "error": request.session.pop("playlist_error", None),
    })


def _picker_context(playlist, params):
    # The matrix (one row per recording by default -- each recording once,
    # so one tick per record) plus what the tick boxes and bulk buttons need.
    context = _matrix_context(params, default_group="recording")
    context.update(
        playlist=playlist,
        picker=True,
        members=playlists_lib.member_ids(playlist),
        member_count=playlist.items.count(),
        # Distinct recordings, not rows: in one-row-per-chart mode a record
        # on three charts is three rows but one "add".
        pick_count=len(playlists_lib.recording_ids_in_rows(context["rows"])),
    )
    return context


def playlist_pick(request, playlist_id):
    # The picker: the recordings matrix with a tick box per row. Same
    # one-URL, two-renders pattern as recordings_matrix.
    playlist = get_object_or_404(Playlist, pk=playlist_id)
    context = _picker_context(playlist, request.GET)
    if request.headers.get("HX-Request"):
        return render(request, "charts/_recordings_matrix_results.html", context)
    return render(request, "charts/playlist_pick.html", context)


@require_POST
def playlist_toggle(request, playlist_id):
    # One tick box. A ticked box sends on=1; an unticked one sends nothing
    # (that's how HTML checkboxes work), so absence means "remove". Answers
    # with just the updated count, swapped out-of-band into the picker.
    playlist = get_object_or_404(Playlist, pk=playlist_id)
    recording_id = request.POST.get("recording", "")
    if recording_id.isdigit():
        playlists_lib.set_member(playlist, int(recording_id), bool(request.POST.get("on")))
    return render(request, "charts/_playlist_count.html", {
        "playlist": playlist, "member_count": playlist.items.count(), "oob": True,
    })


@require_POST
def playlist_bulk(request, playlist_id):
    # "Add all N" / "Remove all N". The filter form's fields ride along in
    # the POST (hx-include), so the matrix query re-runs server-side and the
    # action covers EVERY matching recording -- not just what was on screen,
    # and never a stale list sent from the browser.
    playlist = get_object_or_404(Playlist, pk=playlist_id)
    context = _matrix_context(request.POST, default_group="recording")
    ids = playlists_lib.recording_ids_in_rows(context["rows"])
    if request.POST.get("action") == "remove":
        playlists_lib.remove_all(playlist, ids)
    else:
        playlists_lib.add_all(playlist, ids)
    return render(request, "charts/_recordings_matrix_results.html", _picker_context(playlist, request.POST))


@require_POST
def playlist_move(request, playlist_id, recording_id):
    playlist = get_object_or_404(Playlist, pk=playlist_id)
    playlists_lib.move(playlist, recording_id, request.POST.get("direction", "down"))
    return redirect("charts:playlist_detail", playlist_id=playlist.pk)


@require_POST
def playlist_remove(request, playlist_id, recording_id):
    playlist = get_object_or_404(Playlist, pk=playlist_id)
    playlists_lib.set_member(playlist, recording_id, False)
    return redirect("charts:playlist_detail", playlist_id=playlist.pk)


@require_POST
def playlist_rename(request, playlist_id):
    playlist = get_object_or_404(Playlist, pk=playlist_id)
    error = playlists_lib.rename(playlist, request.POST.get("name"), request.POST.get("description", ""))
    if error:
        # Through the session so the redirect (a plain GET) can show it.
        request.session["playlist_error"] = error
    return redirect("charts:playlist_detail", playlist_id=playlist.pk)


@require_POST
def playlist_delete(request, playlist_id):
    # Removes the list only; recordings are untouched (PlaylistItem cascades).
    get_object_or_404(Playlist, pk=playlist_id).delete()
    return redirect("charts:playlist_list")


def playlist_export(request, playlist_id, fmt):
    # A download: CSV for a spreadsheet, plain "Artist - Song" text for
    # pasting into a streaming service's import tool.
    playlist = get_object_or_404(Playlist, pk=playlist_id)
    if fmt not in ("csv", "txt"):
        raise Http404("Unknown export format")
    if fmt == "csv":
        body, content_type = playlists_lib.export_csv(playlist), "text/csv; charset=utf-8"
    else:
        body, content_type = playlists_lib.export_text(playlist), "text/plain; charset=utf-8"
    response = HttpResponse(body, content_type=content_type)
    # A filename from the playlist's name, stripped to safe characters.
    safe = "".join(c if c.isalnum() or c in " -_" else "_" for c in playlist.name).strip() or "playlist"
    response["Content-Disposition"] = f'attachment; filename="{safe}.{fmt}"'
    return response


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


def artist_list(request):
    # The artist browser: search (name, alias or printed credit), genre and
    # chart filters, sortable table -- every number from artists_lib.browse.
    # Same one-URL, two-renders pattern as the recordings matrix: an HTMX
    # live-filter request gets just the results partial back.
    context = {
        **artists_lib.browse(request.GET),
        "genre_choices": Chart.CATEGORY_CHOICES,
        "all_charts": Chart.objects.order_by("category", "method"),
    }
    if request.headers.get("HX-Request"):
        return render(request, "charts/_artist_list_results.html", context)
    return render(request, "charts/artist_list.html", context)


def artist_profile(request, artist_id):
    # One artist's profile: the dashboard's filter row and definitions,
    # scoped to the recordings they're credited on (artists_lib.profile).
    # The filter row's HTMX requests get just the results partial back.
    artist = get_object_or_404(Artist, pk=artist_id)
    context = {
        **artists_lib.profile(artist, request.GET),
        "genre_choices": Chart.CATEGORY_CHOICES,
        "all_charts": Chart.objects.order_by("category", "method"),
    }
    if request.headers.get("HX-Request"):
        return render(request, "charts/_artist_profile_results.html", context)
    return render(request, "charts/artist_profile.html", context)


def lastweek_audit(request):
    # Every typed last-week number that disagrees with the previous week on
    # file, grouped into whole-week stretches and individual cases, each
    # with its likely cause -- all worked out in charts/lastweek_audit.py.
    # Read-only: the owner fixes the data in the entry form's edit page, and
    # a row disappears from here by itself once the two sides agree.
    context = lastweek_audit_lib.build(request.GET)
    context["chart_options"] = Chart.objects.order_by("category", "method")
    context["recordings_to_check"] = lastweek_audit_lib.recordings_to_check()
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
        "printed_anomaly_note": "",
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
    extra = {"errors": result.errors, "week_on_file": result.already_on_file}
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
    # Asked for a week that's already entered (e.g. ?week= from an old
    # link)? Say so up front, with the way to edit it -- saving here would
    # be refused anyway (week_submission.submit_week).
    week_on_file = chart.entries.filter(week_date=week_date).exists()
    return _entry_page(request, chart, week_date, rows, row_count, week_on_file=week_on_file)


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
        rows.append({
            "last_week_hint": (
                week.last_week_position(e.recording_id)
                if e.printed_last_week_position is None else None
            ),
            "index": i,
            "position": e.position,
            "last_week_position": e.printed_last_week_position,
            "recording": e.recording,
            "printed_anomaly_note": e.printed_anomaly_note,
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
        return render(request, "charts/_entry_resolved.html", {"row": row, "recording": None, "is_response": True})

    try:
        week_date = datetime.strptime(week_str, "%Y-%m-%d").date()
        last_week_position = int(raw_value)
    except (ValueError, TypeError):
        return render(request, "charts/_entry_resolved.html", {"row": row, "recording": None, "error": "not a number", "is_response": True})

    week = chart_week_lib.ChartWeek(chart, week_date)
    matches = week.resolve(last_week_position)

    if not matches:
        error = f"nothing at #{last_week_position} on {week.previous_date}"
        return render(request, "charts/_entry_resolved.html", {"row": row, "recording": None, "error": error, "is_response": True})

    if len(matches) > 1:
        # A genuine tie last week -- can't tell which one continues into
        # this row from the position number alone.
        return render(request, "charts/_entry_ambiguous.html", {
            "chart": chart, "row": row, "week_date_str": week_str, "matches": matches,
        })

    return render(request, "charts/_entry_resolved.html", {
        "row": row, "recording": matches[0].recording, "is_response": True,
    })


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

    # Two characters before searching at all -- a property of this search
    # box (don't fire on the first keystroke), not of matching, so it lives
    # here rather than in charts/search.py.
    results = list(search_lib.search_recordings(q)) if len(q) >= 2 else []
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
    release_id, master_id, parse_error = discogs_workflow.parse_reference(discogs_ref)
    if parse_error:
        # Never block the chart data over a bad optional field.
        notes.append(f"{parse_error} Saved without a Discogs link.")
        return notes
    match, _ = DiscogsMatch.objects.get_or_create(recording=recording)
    # overwrite=False: a reused or picked recording may already carry a
    # link someone decided on -- never silently replace it. No enrich()
    # here: that would be a network call on the entry path.
    note = discogs_workflow.link_manually(match, release_id, master_id, overwrite=False)
    if note:
        notes.append(note)
    elif discogs_workflow.enrich_from_cache(match):
        # Its release was already fetched for another side -- read from
        # that copy on disk, so the link is complete with no network call.
        notes.append("Discogs details filled in from its flip-side's release, already on file.")
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
    return _render_resolved(request, chart, row, week_str, recording, notes)


def _render_resolved(request, chart, row, week_str, recording, notes):
    # A row resolved by picking (entry_select) or creating (entry_create)
    # a recording, rather than by a typed last-week number: the same
    # preview fragment, plus where it sat on the previous chart, since
    # nothing was typed to show that. A hint only; no box is filled from it.
    last_week_hint = None
    if week_str:
        try:
            week_date = datetime.strptime(week_str, "%Y-%m-%d").date()
        except ValueError:
            week_date = None
        if week_date:
            last_week_hint = chart_week_lib.ChartWeek(chart, week_date).last_week_position(recording.id)

    return render(request, "charts/_entry_resolved.html", {
        "row": row, "recording": recording, "last_week_hint": last_week_hint,
        "is_response": True, "notes": notes,
    })


def entry_create_form(request, chart_id):
    # HTMX target of "+ create new recording" in the search-results
    # fragment -- swaps in the inline create sub-form for that row.
    #
    # ?flip_of=<recording id> (the "+ flip-side" button on a search
    # result) opens it pre-filled as the OTHER side of that recording's
    # release -- see entry_recording.flip_side_draft. Otherwise only the
    # song title is pre-filled, from what was typed in the search box.
    chart = get_object_or_404(Chart, pk=chart_id)
    row = request.GET.get("row", "0")
    week_str = request.GET.get("week", "")
    flip_of = None
    if request.GET.get("flip_of", "").isdigit():
        flip_of = Recording.objects.select_related("song", "label").filter(pk=request.GET["flip_of"]).first()
    if flip_of:
        draft = entry_recording_lib.flip_side_draft(flip_of)
    else:
        draft = entry_recording_lib.NewRecording(song_title=request.GET.get("q", ""))
    return render(request, "charts/_entry_create_form.html", {
        "chart": chart, "row": row, "week_date_str": week_str,
        "draft": draft, "flip_of": flip_of,
        # One box per artist already known, plus an empty second box when
        # there's only one -- the same two boxes a blank form starts with.
        "artist_boxes": (draft.artist_names + [""] * 2)[:max(2, len(draft.artist_names))],
        "labels": Label.objects.order_by("name"),
        "artists": Artist.objects.order_by("canonical_name"),
        "songs": Song.objects.order_by("title"),
    })


def entry_create(request, chart_id):
    # HTMX target of the inline create sub-form's submit button. The
    # rules -- what's required, what counts as a possible duplicate, how
    # artists/label/song are reused -- live in charts/entry_recording.py;
    # this only decides which fragment the row shows next.
    chart = get_object_or_404(Chart, pk=chart_id)
    row = request.POST.get("row", "0")
    week_str = request.POST.get("week")
    new = entry_recording_lib.NewRecording.from_post(request.POST, row)

    missing = new.missing()
    if missing:
        return render(request, "charts/_entry_resolved.html", {
            "row": row, "recording": None, "error": "missing: " + ", ".join(missing),
            "is_response": True,
        })

    # Same title as a song on file except for capitalization/spacing? Ask
    # before creating anything (_entry_song_spelling_check.html). Its two
    # buttons re-submit this form with song_spelling set: "existing" (use
    # the song on file, its title) or "typed" (a different song, as typed).
    song_spelling = request.POST.get("song_spelling", "")
    variant = entry_recording_lib.song_spelling_variant(new)
    if variant and song_spelling == "existing":
        new.song_title = variant.title
    elif variant and song_spelling != "typed":
        return render(request, "charts/_entry_song_spelling_check.html", {
            "chart": chart, "row": row, "week_date_str": week_str,
            "typed": new.song_title, "existing": variant,
        })

    # Set by _entry_create_duplicate_check.html's "create new anyway"
    # button on a SECOND submission of this same form -- the user already
    # looked at the possible matches and said none of them are it.
    if request.POST.get("confirm_create") != "1":
        matches = entry_recording_lib.possible_duplicates(new)
        if matches:
            return render(request, "charts/_entry_create_duplicate_check.html", {
                "chart": chart, "row": row, "week_date_str": week_str, "matches": matches,
                # Carried through, so "create new anyway" doesn't ask the
                # spelling question a second time.
                "song_spelling": song_spelling,
            })

    recording, notes = entry_recording_lib.create_recording(new)
    # Optional hand-typed Discogs link: recorded, never looked up (the
    # entry form makes zero network calls -- non-negotiable #2).
    notes += _link_discogs_from_entry(recording, new.discogs_ref)
    return _render_resolved(request, chart, row, week_str, recording, notes)


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
        # The shared recording search, plus what Discogs calls the track --
        # so a song can be found by either side's spelling of it.
        matches = matches.filter(search_lib.recording_text_q(
            q, prefix="recording__", extra_fields=("matched_track_title", "matched_track_artist"),
        ))

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
        # Releases whose sides point at different Discogs releases -- a
        # check across rows, so it sits above the table, not in it.
        "split_releases": discogs_workflow.flip_sides_on_different_releases(),
        "base_querystring": urlencode({k: v for k, v in {
            "status": status if status != "all" else "", "q": q,
            "mismatches": "1" if only_mismatches else "",
            "sort": sort, "dir": direction,
        }.items() if v}),
    }
    return render(request, "charts/discogs_audit.html", context)


def discogs_queue(request):
    discogs_workflow.ensure_matches()

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
        matches = matches.filter(search_lib.recording_text_q(q, prefix="recording__"))

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
        if match.status == DiscogsMatch.SUGGESTED and discogs_workflow.sole_exact_candidate(match)
    ]

    progress = discogs_workflow.progress()

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
        discogs_workflow.search(match)
    except discogs_api.DiscogsError as exc:
        # Network/token/rate-limit problems are shown on the row, not
        # raised -- the rest of the page (and the app) stays usable.
        return render(request, "charts/_discogs_row.html", {
            "match": match, "error": str(exc), "querystring": request.POST.get("querystring", ""),
        })
    return _render_discogs_row(request, match)


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
    error = discogs_workflow.confirm(match, candidate)
    return render(request, "charts/_discogs_row.html", {
        "match": match, "error": error, "querystring": request.POST.get("querystring", ""),
    })


def discogs_manual(request, recording_id):
    # HTMX target of the "paste a Discogs release/master URL or ID" box,
    # for when the search genuinely can't find it but you've found it
    # yourself on discogs.com. Same "finish the job in one click" as
    # discogs_confirm above: a release reference gets its full details
    # fetched right here too. A master-only reference can't (see
    # needs_detail on DiscogsMatch) -- enrich() is a no-op then.
    match, _ = DiscogsMatch.objects.get_or_create(recording_id=recording_id)
    raw = (request.POST.get("discogs_ref") or "").strip()

    release_id, master_id, parse_error = discogs_workflow.parse_reference(raw)
    if parse_error:
        return render(request, "charts/_discogs_row.html", {
            "match": match, "error": parse_error,
            "querystring": request.POST.get("querystring", ""),
        })

    # overwrite=True: pasting here is an explicit decision to (re)link.
    discogs_workflow.link_manually(match, release_id, master_id, overwrite=True)
    fetch_error = discogs_workflow.enrich(match)
    return render(request, "charts/_discogs_row.html", {
        "match": match, "error": fetch_error, "querystring": request.POST.get("querystring", ""),
    })


def discogs_set_status(request, recording_id):
    # HTMX target of "None of these" / "Not on Discogs" / "Reset".
    match, _ = DiscogsMatch.objects.get_or_create(recording_id=recording_id)
    discogs_workflow.set_status(match, request.POST.get("status"))
    return _render_discogs_row(request, match)


def discogs_fetch_detail(request, recording_id):
    # HTMX target of a linked row's "fetch full details" button -- the
    # explicit retry/catch-up path for whatever enrich()'s
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
    error = discogs_workflow.enrich(match)
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
        done, error = discogs_workflow.search_batch(pending)
        if error:
            messages.append(f"Stopped after {done}: {error}")
        else:
            messages.append(f"Searched {done} recording{'' if done == 1 else 's'}.")

    elif action == "confirm_exact":
        # Confirm every single-EXACT suggestion on the current page, AND
        # fetch each one's per-track detail in the same click.
        #
        # This used to skip the detail fetch to stay instant, leaving
        # those rows showing release-level stand-ins (the "rel" tag) with
        # no songwriter credits until someone remembered to run the
        # catch-up step. In practice that follow-up got forgotten and
        # 124 rows sat half-populated, which is a worse outcome than a
        # slower click. Cost is bounded: at most one pageful (25) at
        # roughly a second each. A match with several EXACT candidates is
        # a genuine tie and is left for a deliberate pick.
        ids = request.POST.getlist("match_id")
        result = discogs_workflow.confirm_exact_batch(
            DiscogsMatch.objects.filter(id__in=ids, status=DiscogsMatch.SUGGESTED).prefetch_related("candidates")
        )
        confirmed, enriched = result["confirmed"], result["enriched"]
        message = f"Confirmed {confirmed} exact match{'' if confirmed == 1 else 'es'}"
        message += f", with full details fetched for {enriched}." if enriched else "."
        if result["detail_error"]:
            message += (
                f" Detail fetch stopped after {enriched} ({result['detail_error']}) -- "
                f"{confirmed - enriched} still need it; use \"Fetch details\" to finish."
            )
        if result["skipped_ties"]:
            message += f" Skipped {result['skipped_ties']} with more than one exact match -- needs a manual pick."
        messages.append(message)

    elif action == "fetch_details":
        # Fill in songwriters/producers/etc for linked matches that don't
        # have them yet, a batch at a time. needs_detail_q, not a
        # hand-written filter: master-only matches can never be fetched.
        pending = list(
            DiscogsMatch.objects.filter(DiscogsMatch.needs_detail_q())
            .select_related("recording", "recording__song")[:DISCOGS_WEB_BATCH_SIZE]
        )
        done, error = discogs_workflow.enrich_batch(pending)
        if error:
            messages.append(f"Stopped after {done}: {error}")
        else:
            messages.append(f"Fetched details for {done} match{'' if done == 1 else 'es'}.")

    request.session["discogs_messages"] = messages
    return redirect(f"{reverse('charts:discogs_queue')}?{querystring}")


# =============================================================================
# Spotify matching -- /spotify/ (GLOSSARY.md: Spotify track, Spotify match,
# stand-in). Same shape as the Discogs section above; every rule lives in
# charts/spotify_workflow.py and charts/spotify.py, these views only read the
# request and pick a template. Writes are POST-only.
# =============================================================================

# How many rows "Search next N" offers by default (the box is editable).
SPOTIFY_DEFAULT_BATCH = 50
# The most the box accepts in one go -- a batch runs one row at a time in
# the browser, so this only bounds how long a single page stays busy.
SPOTIFY_MAX_BATCH = 200


def _spotify_filters(params):
    # The queue's filter state, cleaned: status, search text, playlist.
    status = params.get("status", "todo")
    if status not in {"todo", "all"} | {value for value, _ in SpotifyMatch.STATUS_CHOICES}:
        status = "todo"
    playlist_id = params.get("playlist", "")
    if not str(playlist_id).isdigit():
        playlist_id = ""
    return {"status": status, "q": params.get("q", "").strip(), "playlist": playlist_id}


def _spotify_scope(filters):
    # Every match the playlist + search filters cover (status not applied).
    # recording__discogs too: each row shows its Discogs link for comparison.
    matches = SpotifyMatch.objects.select_related("recording__song", "recording__label", "recording__discogs")
    if filters["playlist"]:
        matches = matches.filter(recording__playlist_items__playlist_id=filters["playlist"])
    if filters["q"]:
        matches = matches.filter(search_lib.recording_text_q(filters["q"], prefix="recording__"))
    return matches


def spotify_queue(request):
    # The review queue. Two modes:
    #   * normal -- the filtered rows, 25 a page;
    #   * batch (?batch=N) -- the next N never-searched rows in the current
    #     filter, each searching ITSELF one after another in the browser
    #     (see spotify_queue.html's script), with an "n / N" progress line,
    #     so a 50-row run shows progress instead of a frozen page.
    spotify_workflow.ensure_matches()
    filters = _spotify_filters(request.GET)
    scope = _spotify_scope(filters)

    batch_size = request.GET.get("batch", "")
    batch = int(batch_size) if batch_size.isdigit() and int(batch_size) > 0 else 0
    batch = min(batch, SPOTIFY_MAX_BATCH)

    if batch:
        rows = list(
            scope.filter(status=SpotifyMatch.UNMATCHED)
            .prefetch_related("candidates", "extra_tracks")
            .order_by("recording__song__title", "recording__catalog_number")[:batch]
        )
        page = None
    else:
        matches = scope.prefetch_related("candidates", "extra_tracks")
        if filters["status"] == "todo":
            matches = matches.filter(status__in=[SpotifyMatch.UNMATCHED, SpotifyMatch.SUGGESTED])
        elif filters["status"] != "all":
            matches = matches.filter(status=filters["status"])
        # Suggested first (they need a decision), then a stable order.
        matches = matches.annotate(
            needs_decision=Case(
                When(status=SpotifyMatch.SUGGESTED, then=Value(0)), default=Value(1), output_field=IntegerField(),
            )
        ).order_by("needs_decision", "recording__song__title", "recording__catalog_number")
        page = Paginator(matches, 25).get_page(request.GET.get("page"))
        rows = list(page)

    progress = spotify_workflow.progress(scope)
    # Built here: each option shows its own count, which a template can't
    # look up by a loop variable.
    counts = progress["counts"]
    status_options = [
        {"value": "todo", "label": "Needs attention",
         "count": counts.get(SpotifyMatch.UNMATCHED, 0) + counts.get(SpotifyMatch.SUGGESTED, 0)},
        {"value": "all", "label": "All", "count": progress["total"]},
    ] + [
        {"value": value, "label": label, "count": counts.get(value, 0)}
        for value, label in SpotifyMatch.STATUS_CHOICES
    ]
    querystring = urlencode({k: v for k, v in {
        **filters, "page": page.number if page else "",
    }.items() if v})

    return render(request, "charts/spotify_queue.html", {
        "rows": rows,
        "page": page,
        "batch": batch,
        "batch_default": batch or SPOTIFY_DEFAULT_BATCH,
        "filters": filters,
        "status_options": status_options,
        "progress": progress,
        "playlists": Playlist.objects.order_by("name"),
        "has_credentials": bool(spotify_api.get_credentials()),
        "market": spotify_api.get_market(),
        "unmatched_count": counts.get(SpotifyMatch.UNMATCHED, 0),
        # What the bulk button would really confirm on this page (the
        # same function the action uses, so its number can't lie).
        "strong_ids": [m.id for m in rows if spotify_workflow.bulk_confirmable_candidate(m)],
        "querystring": querystring,
        "messages": request.session.pop("spotify_messages", []),
        # Who's connected for pushing playlists (from the token file, no
        # API call); None = not connected.
        "spotify_account": spotify_api.connected_account(),
    })


def _render_spotify_row(request, match, error=None, retry_after=None):
    # Every per-row action answers with the same row fragment, so a row
    # looks the same however it got to its current state. `batch` carries
    # through so a batch row keeps its marker (the progress count reads it).
    match = SpotifyMatch.objects.prefetch_related("candidates", "extra_tracks").select_related(
        "recording__song", "recording__label", "recording__discogs"
    ).get(pk=match.pk)
    return render(request, "charts/_spotify_row.html", {
        "match": match,
        "error": error,
        # Seconds Spotify asked us to wait (a 429). The row carries it, so
        # the batch script pauses that long and then retries this row.
        "retry_after": retry_after,
        "in_batch": bool(request.POST.get("batch")),
        "querystring": request.POST.get("querystring", ""),
    })


@require_POST
def spotify_search(request, recording_id):
    # A row's "Search Spotify" (or a batch row searching itself). Live call;
    # a failure is shown on the row, never as an error page.
    match, _ = SpotifyMatch.objects.get_or_create(recording_id=recording_id)
    try:
        spotify_workflow.search(match)
    except spotify_api.SpotifyRateLimited as exc:
        return _render_spotify_row(request, match, error=str(exc), retry_after=exc.retry_after)
    except spotify_api.SpotifyError as exc:
        return _render_spotify_row(request, match, error=str(exc))
    return _render_spotify_row(request, match)


@require_POST
def spotify_confirm(request, recording_id):
    # A candidate's "Match" or "Stand-in" button. No network call: the
    # candidate already holds the full snapshot.
    match = get_object_or_404(SpotifyMatch, recording_id=recording_id)
    candidate = SpotifyCandidate.objects.filter(match=match, pk=request.POST.get("candidate_id") or 0).first()
    if candidate is None:
        return _render_spotify_row(request, match, error="That suggestion is gone -- search again.")
    spotify_workflow.confirm(match, candidate, stand_in=bool(request.POST.get("stand_in")))
    return _render_spotify_row(request, match)


@require_POST
def spotify_manual(request, recording_id):
    # A pasted Spotify link / URI / id. One call, to cache what it points at.
    match, _ = SpotifyMatch.objects.get_or_create(recording_id=recording_id)
    error = spotify_workflow.link_manually(
        match, request.POST.get("spotify_ref", ""), stand_in=bool(request.POST.get("stand_in")),
    )
    return _render_spotify_row(request, match, error=error)


@require_POST
def spotify_add_extra(request, recording_id):
    # "+ Part" / "+ Alternate" on a linked row: from a remaining shortlist
    # suggestion (candidate_id, no network) or a pasted link (spotify_ref,
    # one fetch). The rare recording split over several Spotify tracks.
    match = get_object_or_404(SpotifyMatch, recording_id=recording_id)
    kind = request.POST.get("kind", "")
    candidate_id = request.POST.get("candidate_id")
    if candidate_id:
        candidate = SpotifyCandidate.objects.filter(match=match, pk=candidate_id).first()
        error = (spotify_workflow.add_extra_from_candidate(match, candidate, kind)
                 if candidate else "That suggestion is gone -- search again.")
    else:
        error = spotify_workflow.add_extra_manually(match, request.POST.get("spotify_ref", ""), kind)
    return _render_spotify_row(request, match, error=error)


@require_POST
def spotify_remove_extra(request, recording_id):
    match = get_object_or_404(SpotifyMatch, recording_id=recording_id)
    spotify_workflow.remove_extra(match, request.POST.get("extra_id") or 0)
    return _render_spotify_row(request, match)


@require_POST
def spotify_set_status(request, recording_id):
    # "Not on Spotify" or "Reset".
    match = get_object_or_404(SpotifyMatch, recording_id=recording_id)
    spotify_workflow.set_status(match, request.POST.get("status", ""))
    return _render_spotify_row(request, match)


@require_POST
def spotify_bulk(request):
    # "Confirm N strong matches on this page". Full POST + redirect, since
    # it changes many rows. Only rows whose top candidate is strong and not
    # flagged as a possible re-recording are touched.
    ids = request.POST.getlist("match_id")
    confirmed = spotify_workflow.confirm_strong_batch(
        SpotifyMatch.objects.filter(id__in=ids, status=SpotifyMatch.SUGGESTED).prefetch_related("candidates")
    )
    plural = "" if confirmed == 1 else "es"
    request.session["spotify_messages"] = [f"Confirmed {confirmed} strong match{plural}."]
    return redirect(reverse("charts:spotify_queue") + "?" + request.POST.get("querystring", ""))


def spotify_audit(request):
    # Read-only: what's already linked, newest first, next to our data with
    # disagreements highlighted -- plus Spotify tracks shared by recordings
    # that aren't known reissues of each other.
    linked = (
        SpotifyMatch.objects.filter(status__in=SpotifyMatch.LINKED_STATUSES)
        .select_related("recording__song", "recording__label", "recording__discogs")
        .order_by("-linked_at", "-id")
    )
    page = Paginator(linked, 50).get_page(request.GET.get("page"))
    return render(request, "charts/spotify_audit.html", {
        "page": page,
        "rows": [spotify_workflow.audit_row(m) for m in page],
        "shared": spotify_workflow.shared_tracks_not_reissues(),
    })


# -----------------------------------------------------------------------------
# Connecting the owner's Spotify account, and pushing playlists to it
# (charts/spotify_push.py has the rules; see the comment block above
# spotify.authorize_url for how the login works).
# -----------------------------------------------------------------------------

# The host Spotify accepts for a plain-http return address: a loopback IP,
# never "localhost" (Spotify's 2025 rules).
SPOTIFY_LOOPBACK_HOST = "127.0.0.1"


def _spotify_redirect_uri(request):
    # Must match, character for character, the Redirect URI registered in
    # the app's dashboard: http://127.0.0.1:<port>/spotify/callback/
    return f"http://{request.get_host()}{reverse('charts:spotify_callback')}"


def spotify_connect(request):
    # Step 1: send the browser to Spotify's consent page.
    host = request.get_host()
    if not host.startswith(SPOTIFY_LOOPBACK_HOST):
        # Opened as http://localhost:8000: hop to the same page on
        # 127.0.0.1 first. The session (which holds `state`) is per-host, so
        # the whole round trip must happen on the address Spotify returns to.
        port = host.rsplit(":", 1)[1] if ":" in host else "80"
        return redirect(f"http://{SPOTIFY_LOOPBACK_HOST}:{port}{reverse('charts:spotify_connect')}")
    import secrets
    state = secrets.token_urlsafe(24)
    request.session["spotify_oauth_state"] = state
    try:
        return redirect(spotify_api.authorize_url(state, _spotify_redirect_uri(request)))
    except spotify_api.SpotifyError as exc:
        request.session["spotify_messages"] = [str(exc)]
        return redirect("charts:spotify_queue")


def spotify_callback(request):
    # Step 2-3: Spotify sends the browser back here with a one-time code.
    expected = request.session.pop("spotify_oauth_state", None)
    if request.GET.get("error"):
        # The owner pressed Cancel, or Spotify refused (e.g. account not
        # added under the app's User Management).
        message = f"Spotify didn't connect: {request.GET['error']}."
    elif not expected or request.GET.get("state") != expected:
        message = "That Spotify sign-in link didn't come from this app (state mismatch) -- press Connect Spotify again."
    else:
        try:
            account = spotify_api.exchange_code(request.GET.get("code", ""), _spotify_redirect_uri(request))
            message = f"Connected to Spotify as {account['display_name']}."
        except spotify_api.SpotifyError as exc:
            spotify_api.disconnect()  # never leave a half-written login behind
            message = f"Spotify didn't connect: {exc}"
    request.session["spotify_messages"] = [message]
    return redirect("charts:spotify_queue")


@require_POST
def spotify_disconnect(request):
    # Forget the login (delete .spotify_token.json). Pushed playlists stay
    # on Spotify untouched.
    spotify_api.disconnect()
    request.session["spotify_messages"] = ["Disconnected from Spotify. Playlists already pushed are untouched."]
    return redirect("charts:spotify_queue")


def playlist_spotify(request, playlist_id):
    # GET: the confirmation page -- what will be sent, what's missing and
    # why, and (for an update) the warning that Spotify-side edits are
    # replaced. POST: do it.
    playlist = get_object_or_404(Playlist, pk=playlist_id)
    plan = spotify_push.plan(playlist)
    context = {
        "playlist": playlist,
        "plan": plan,
        "changes": spotify_push.changes(playlist, plan),
        "spotify_account": spotify_api.connected_account(),
        "spotify_playlist_url": spotify_api.playlist_url(playlist.spotify_playlist_id),
        "error": None,
        "gone": False,
    }
    if request.method == "POST":
        try:
            result = spotify_push.push(playlist, create_new=bool(request.POST.get("create_new")))
        except spotify_push.PlaylistGone:
            # Ask before creating a new one -- never silently.
            context["gone"] = True
            return render(request, "charts/playlist_spotify.html", context)
        except spotify_api.SpotifyError as exc:
            context["error"] = str(exc)
            return render(request, "charts/playlist_spotify.html", context)
        verb = "Created" if result["created"] else "Updated"
        skipped = f", {result['missing']} recording(s) not on it" if result["missing"] else ""
        request.session["playlist_message"] = f"{verb} on Spotify: {result['sent']} track(s){skipped}."
        return redirect("charts:playlist_detail", playlist_id=playlist.pk)
    return render(request, "charts/playlist_spotify.html", context)
