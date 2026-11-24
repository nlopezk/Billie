# charts/urls.py
#
# Two feature areas:
#   - the read-only chart BROWSER (chart_list, chart_week)
#   - the weekly entry FORM (everything under <chart_id>/entry/) --
#     CLAUDE.md's "heart of the project"
#
# IMPORTANT: the entry/* routes must come BEFORE chart_week's
# <str:week_date> pattern below. `str` matches any non-slash segment,
# including the literal word "entry" -- if chart_week's pattern were
# listed first, a request for /2/entry/ would match it with
# week_date="entry" instead of reaching the entry form at all.

from django.urls import path

from . import views

app_name = "charts"

urlpatterns = [
    # /                                        -- list of the 5 charts
    path("", views.chart_list, name="chart_list"),

    # /recordings/                             -- the recordings matrix: EVERY
    # recording, across ALL charts, filterable by genre/chart and searchable,
    # sortable by peak/weeks/first/last. Not chart_id-scoped, so it lives at
    # the top level rather than under <int:chart_id>/ -- no ordering hazard
    # with the patterns below, since "recordings" never matches <int:...>.
    path("recordings/", views.recordings_matrix, name="recordings_matrix"),

    # /dashboard/                              -- headline numbers, debuts over
    # time, top labels, and the three Top-N rankings, all scoped by one
    # filter row (date range, genre, chart). Read-only. Same top-level
    # placement reasoning as recordings/ above.
    path("dashboard/", views.dashboard, name="dashboard"),
    # Playlists (GLOSSARY.md): hand-picked lists of recordings, for export.
    # The picker is the recordings matrix with tick boxes; every write
    # below is POST-only.
    # Spotify matching (GLOSSARY.md: Spotify match, stand-in). The only
    # section besides /discogs/ that talks to the network.
    path("spotify/", views.spotify_queue, name="spotify_queue"),
    path("spotify/audit/", views.spotify_audit, name="spotify_audit"),
    # The owner's login, for pushing playlists. The callback's address must
    # be registered in the Spotify app as http://127.0.0.1:8000/spotify/callback/
    path("spotify/connect/", views.spotify_connect, name="spotify_connect"),
    path("spotify/callback/", views.spotify_callback, name="spotify_callback"),
    path("spotify/disconnect/", views.spotify_disconnect, name="spotify_disconnect"),
    path("spotify/bulk/", views.spotify_bulk, name="spotify_bulk"),
    path("spotify/<int:recording_id>/search/", views.spotify_search, name="spotify_search"),
    path("spotify/<int:recording_id>/confirm/", views.spotify_confirm, name="spotify_confirm"),
    path("spotify/<int:recording_id>/manual/", views.spotify_manual, name="spotify_manual"),
    path("spotify/<int:recording_id>/status/", views.spotify_set_status, name="spotify_set_status"),
    path("spotify/<int:recording_id>/extra/", views.spotify_add_extra, name="spotify_add_extra"),
    path("spotify/<int:recording_id>/extra/remove/", views.spotify_remove_extra, name="spotify_remove_extra"),
    path("playlists/", views.playlist_list, name="playlist_list"),
    path("playlists/<int:playlist_id>/", views.playlist_detail, name="playlist_detail"),
    path("playlists/<int:playlist_id>/pick/", views.playlist_pick, name="playlist_pick"),
    path("playlists/<int:playlist_id>/toggle/", views.playlist_toggle, name="playlist_toggle"),
    path("playlists/<int:playlist_id>/bulk/", views.playlist_bulk, name="playlist_bulk"),
    path("playlists/<int:playlist_id>/move/<int:recording_id>/", views.playlist_move, name="playlist_move"),
    path("playlists/<int:playlist_id>/remove/<int:recording_id>/", views.playlist_remove, name="playlist_remove"),
    path("playlists/<int:playlist_id>/rename/", views.playlist_rename, name="playlist_rename"),
    path("playlists/<int:playlist_id>/delete/", views.playlist_delete, name="playlist_delete"),
    path("playlists/<int:playlist_id>/spotify/", views.playlist_spotify, name="playlist_spotify"),
    path("playlists/<int:playlist_id>/export.<str:fmt>", views.playlist_export, name="playlist_export"),
    # Artist browser (search + sortable table) and one artist's profile.
    path("artists/", views.artist_list, name="artist_list"),
    path("artists/<int:artist_id>/", views.artist_profile, name="artist_profile"),

    # /audit/last-week/                        -- every typed last-week number
    # that disagrees with the previous week on file, with the likely cause.
    # Read-only; fixes happen in the entry form's edit page.
    path("audit/last-week/", views.lastweek_audit, name="lastweek_audit"),

    # --- credit splitting (combined-credit artists -> real collaborations) ---
    path("credits/", views.credit_queue, name="credit_queue"),
    path("credits/<int:artist_id>/split/", views.credit_split, name="credit_split"),
    path("credits/<int:artist_id>/canonical/", views.credit_canonical, name="credit_canonical"),
    path("credits/<int:artist_id>/keep/", views.credit_keep, name="credit_keep"),
    path("credits/<int:artist_id>/reopen/", views.credit_reopen, name="credit_reopen"),

    # --- Discogs review section (the only network-touching part) ---
    # /discogs/                                -- the review queue
    path("discogs/", views.discogs_queue, name="discogs_queue"),
    # /discogs/audit/                          -- side-by-side audit of what's
    # already linked (ours vs Discogs), newest link first. Read-only.
    path("discogs/audit/", views.discogs_audit, name="discogs_audit"),
    # Page-level batch actions (search N / confirm exact / fetch details):
    path("discogs/bulk/", views.discogs_bulk, name="discogs_bulk"),
    # Per-recording HTMX row actions:
    path("discogs/<int:recording_id>/search/", views.discogs_search, name="discogs_search"),
    path("discogs/<int:recording_id>/confirm/", views.discogs_confirm, name="discogs_confirm"),
    path("discogs/<int:recording_id>/manual/", views.discogs_manual, name="discogs_manual"),
    path("discogs/<int:recording_id>/status/", views.discogs_set_status, name="discogs_set_status"),
    path("discogs/<int:recording_id>/detail/", views.discogs_fetch_detail, name="discogs_fetch_detail"),

    # --- weekly entry form ---
    # /1/entry/                               -- new-week entry form (GET), submit target (POST)
    path("<int:chart_id>/entry/", views.entry_week, name="entry_week"),
    # HTMX fragment endpoints, one row at a time:
    path("<int:chart_id>/entry/resolve/", views.entry_resolve, name="entry_resolve"),
    path("<int:chart_id>/entry/search/", views.entry_search, name="entry_search"),
    path("<int:chart_id>/entry/select/", views.entry_select, name="entry_select"),
    path("<int:chart_id>/entry/create-form/", views.entry_create_form, name="entry_create_form"),
    path("<int:chart_id>/entry/create/", views.entry_create, name="entry_create"),
    path("<int:chart_id>/entry/add-row/", views.entry_add_row, name="entry_add_row"),
    # /1/1944-01-08/edit/                     -- edit an ALREADY-SAVED week (same
    # form/HTMX endpoints as above, seeded from what's on file instead of from
    # last week, and saves by replacing this week's rows instead of adding to them)
    path("<int:chart_id>/<str:week_date>/edit/", views.entry_week_edit, name="entry_week_edit"),

    # --- read-only browser ---
    # /1/                                     -- a chart's MOST RECENT week
    path("<int:chart_id>/", views.chart_week, name="chart_week_latest"),
    # /1/1944-01-08/                          -- a chart's specific week
    path("<int:chart_id>/<str:week_date>/", views.chart_week, name="chart_week"),
]
