# Billboard Chart Database — Project Instructions

## What this project is

A **fully local** database of historical Billboard charts, filled in manually from scanned
magazine issues, plus a local web tool that makes that manual entry fast and error-resistant.

Currently 6 charts (country and R&B singles, retail, radio, and others). Core relational
shape: **chart → chart entry (one row per position per week) → recording → artist**.

## Project status (updated 2026-08-20)

Where things actually stand right now, so a fresh session doesn't have to re-derive it:

- **Django project is scaffolded, migrated, and imported.** `django-admin startproject
  config .` + a single app, `charts` (holds every model — chart, chart_era, chart_entry,
  recording, recording_credit, song, artist, artist_alias, label). `manage.py` lives at
  the repo root. Database file is `billboard.db` (named to match the schema-regeneration
  command below, not the Django default `db.sqlite3`). `charts/admin.py` registers every
  model with `autocomplete_fields`/`search_fields` per the Stack section's rationale, plus
  inlines for `ArtistAlias`, `RecordingCredit`, and `ChartEra`. `docs/schema.sql` is
  regenerated and current. A local superuser exists (`admin`); `python manage.py
  runserver` + `/admin/` is the interface for browsing/editing everything right now — the
  dedicated weekly entry form (see below) doesn't exist yet.
- **Data is imported.** `charts/management/commands/import_workbook.py` is the one-time
  import command (`python manage.py import_workbook`) — reads `Bb Charts fixed.xlsx`,
  refuses to run against a non-empty `ChartEntry` table so it can't double-import. Current
  counts: 5 `Chart` / 12 `ChartEra` / **10,750 `ChartEntry`** / 1,161 `Recording` / 350
  `Artist` / 967 `Song` / 77 `Label`. The 6 confirmed reissue pairs have `reissue_of` set;
  the 4 accepted Savoy 731A anomaly rows have `printed_anomaly_note` set. Combined-credit
  artists (e.g. "Bing Crosby - Andrews Sisters") were imported as single canonical
  `Artist` rows unsplit, by design — see `docs/audit/combined_credit_artists.txt` for the
  standing checklist of which ones may need splitting into real `RecordingCredit` rows,
  worked through by hand at your own pace, never automatically.
- **A real schema bug was caught and fixed during the import**, worth knowing about:
  `ChartEntry`'s original `UNIQUE(chart, week_date, position)` constraint (straight from
  this file's own earlier example SQL) rejected every legitimate printed tie, because it
  assumed only one recording could ever hold a position. Fixed to
  `UNIQUE(chart, week_date, position, recording)` in migration `0002` — see that section
  below for the corrected constraint and reasoning.
- **Not built yet:** the weekly entry form (the actual point of the project — see that
  section below), and the `combined_credit_artists.txt` splitting work.
- **Data source:** `Bb Charts fixed.xlsx` (workbook: 5 chart sheets — Country JOCKEYS/
  JUKEBOX/RETAIL, R&B JUKEBOX/RETAIL — plus Recordings/Songs/Artists/Labels master
  sheets). This is the owner's own hand-transcribed work going back years, not a
  secondhand dataset — migrating it into the new schema is the agreed plan (see
  Non-negotiable #1: this is a one-time format change, not a substitute for future manual
  entry). ~10,750 real chart-position rows across the five charts.
- **Audit completed and resolved.** `docs/audit/` holds the trail:
  - `same_recording_repeated.csv` — the real defect class (identical recording listed
    twice in one chart-week). 5 found; 3 were data-entry mistakes and have been fixed in
    the workbook; 2 are genuine Billboard printing anomalies (Savoy 731 / "Double Crossing
    Blues" / Little Esther, printed twice the same week under inconsistent catalog numbers
    and artist billing) and are being kept as-is, intentionally, per Non-negotiable #4.
  - `position_skip_review.csv` — chart-position ties. **Competition/skip ranking
    (tie holders share a position, the next entry jumps to position + tie count) is the
    normal convention in this data.** Dense ranking (+1 after a tie) is a documented
    exception, concentrated almost entirely in Country JUKEBOX and R&B JUKEBOX, 1944–1949
    — 257 such cases, currently accepted as correct pending the owner spot-checking
    further against the magazines.
  - `other_issues.csv` — 4 smaller findings, all resolved (a duplicate legacy `rec_num` in
    the old workbook, explained as a genuine reissue rather than a data error; one
    case-mismatched `rec_id` join, fixed; one typo, fixed; 73 non-breaking-space
    inconsistencies, fixed).
  - Along the way, confirmed a real pattern worth keeping in mind for the `Recording`
    model: several songs have more than one physical release on file for the same
    artist. Sourced examples: Elton Britt's "Someday" (Bluebird 33-0521B → RCA Victor
    20-1864A, a straight reissue) vs. Lionel Hampton's "Flying Home" (Victor 1940 vs.
    Decca 1942, a genuinely different recording). The schema below distinguishes these
    with `Recording.reissue_of`.
- **Schema designed, not yet migrated.** Full draft: `docs/models_draft.py`. Key decisions
  baked into it (see the file's comments for the reasoning):
  - `Chart` + `ChartEra` (chart renames over time, e.g. "Most-Played Juke Box Folk
    Records" → "...Hillbilly Records" → "...Country & Western Records", are the same
    chart lineage, not separate charts).
  - `Recording` ↔ `Artist` via a `RecordingCredit` join table with `credit_order`,
    replacing the old workbook's `" - "`-delimited free-text Artist field.
  - `Recording.reissue_of` (self-FK) for the same-recording-different-issue case above.
  - `ChartEntry.printed_anomaly_note` to carry forward the 2 accepted printing anomalies
    without the UNIQUE constraint silently rejecting or deduping them.
  - Position-in-range and publication-weekday checks live in `ChartEntry.clean()`, not a
    SQL CHECK constraint, because SQLite can't reference another table from one.
  - `Recording.side` is `max_length=4`, not 1 — widened in migration `0003` to allow
    `'AB'` for a song listed as spanning both sides (e.g. a "Parts I and II" release
    catalogued as one listing), not just single-letter `'A'`/`'B'`.
- **The weekly entry form is built** (`charts/urls.py`, `charts/views.py`, `charts/forms.py`,
  `charts/templates/charts/entry_week.html` + `_entry_*.html` partials). At `/<chart_id>/entry/`.
  Implements every point in "The weekly entry form" section below: seeded from the
  previous week's row count, last-week number resolves a recording via HTMX (vendored
  locally at `charts/static/charts/htmx.min.js` — downloaded once, served from disk, no
  runtime internet dependency), ties last week correctly show a disambiguation picker
  instead of silently guessing, search covers existing recordings (song/artist/label/
  catalog #, combinable — "Capitol 304" finds a Capitol release cataloged 304), a
  brand-new recording can be created inline (get_or_create — re-submitting doesn't
  duplicate), rows can be deleted client-side (no server round-trip), and the whole
  week saves atomically via `full_clean()` + `transaction.atomic()`, re-rendering with
  everything preserved if any row fails. A computed "prior N weeks" hint shows next to
  the weeks-on-chart input for comparison, never auto-filled, per "Derived fields as a
  typo detector" below — no peak-position field or hint, since the owner has never
  seen that printed on an actual page (the `printed_peak_position` model field stays,
  just unused/hidden in the form; trivial to bring back).
- **Editing an already-saved week is also built** — `/<chart_id>/<week_date>/edit/`
  (linked from the chart browser as "edit this week"). Same page and the same
  resolve/search/create/delete machinery as a new week, just seeded from what's
  actually on file instead of from last week's count, and saved by REPLACING that
  week's rows (delete + recreate, atomically) instead of adding to them. Safe because
  nothing else in the schema has a foreign key pointing at `ChartEntry`.
- **Also added along the way:** a read-only chart **browser** (`/`, `/<chart_id>/`,
  `/<chart_id>/<week_date>/`) for paging through what's already on file week by week —
  separate from the entry form, never writes anything on its own. And a read-only
  **admin change log** (`/admin/` → Log entries, `LogEntry` registered in
  `charts/admin.py`) — tracks admin-made additions/changes/deletions only, not
  programmatic ones (e.g. neither the import command's rows nor the entry/edit forms'
  writes are logged, since none of them go through `ModelAdmin`); see `README.md`'s
  "Auditing changes" section for the full explanation of what it does and doesn't catch.
- **A read-only recordings matrix, added 2026-08-20** — `/recordings/`
  (`views.recordings_matrix`, linked from `chart_list.html` and `chart_week.html`). ONE
  page covering all 5 charts (an earlier same-day version was one matrix per chart —
  replaced same day once it became clear that didn't scale to "just show me everything").
  **Two grouping modes**, picked with the "One row per" dropdown (`?group=`):
  - `chart` (the default) — one row per (chart, recording) pair a recording has actually
    charted on, never merged, since peak/weeks/first/last are only well-defined within a
    single chart (see the docstring on `views._recordings_matrix_query`).
  - `recording` (added 2026-10-14, because filtering to Country still showed the same
    record three times, once per Country chart) — one row per recording, with its charts
    merged. The mechanism is just dropping the chart columns out of the `.values()`, so
    SQL's `GROUP BY` gets coarser; no subquery, no Python-side merge. What that does to
    each column is the part worth knowing: **Peak becomes "Best peak"** — the lowest
    position reached on *any* of its charts, which is a DERIVED number nobody printed, so
    the column is relabelled and the chart that produced it is named under the number
    (non-negotiable #4 — never pass a computed value off as a printed one). **Weeks stays
    distinct `week_date`s**, so a week spent on three charts at once counts ONCE (summing
    the per-chart weeks would claim 27 weeks for "Shot Gun Boogie", which charted for 10).
    **First/last entry span every chart**, and each still links to the chart-week page it
    really came from — which is why one extra aggregate query, re-grouped by
    `(recording, chart)`, runs in this mode: it recovers which chart each merged number
    came from, for the date links, for the "best peak" source, and for the Charts column's
    per-chart peak badges (`CJb` = Country Jukebox, hover for the full name). Sorting by
    the Charts column sorts by chart COUNT, deliberately: `chart__category` isn't in the
    collapsed `GROUP BY`, and ordering by it would re-join the chart table and silently
    un-collapse every row.

  Both modes share the same filters across the top
  for genre (`Chart.category`), a specific chart, and free-text search (song/artist/label/
  catalog #, same word-AND/field-OR matching as the entry form's search), all combinable,
  plus sortable columns via `?sort=&dir=`. Genre + chart + search all live-filter via HTMX
  as you type/select — no page reload — by re-requesting the SAME url+view (`HX-Request`
  header present → render just `_recordings_matrix_results.html`; absent → full page),
  with `hx-push-url` keeping the address bar in sync so a filtered view stays bookmarkable/
  shareable. Weeks charted counts *distinct week_date*, not raw `ChartEntry` rows,
  specifically so the 2 accepted same-week printed-anomaly rows don't inflate it — this,
  the per-(chart, recording) scoping, the filters, and all of the collapsed mode's
  re-aggregation above each have tests in `charts/tests.py` (`RecordingsMatrixTests`,
  `RecordingsMatrixCollapsedTests`). An earlier version of this bullet warned that the
  rest of the app's test coverage was missing from `charts/tests.py`; that is no longer
  true — the suite is 136 tests covering the entry form, credit splitting, and the whole
  Discogs workflow, and it runs in well under a second because nothing in it touches the
  network.
- **Credit splitting, built 2026-08-21** — `/credits/` (`charts/credits.py`,
  `views.credit_*`, `credit_queue.html`). Turns a combined-credit Artist row
  ("Bing Crosby - Andrews Sisters", imported verbatim from the workbook's single
  free-text artist field) into what it actually is: one recording credited to TWO
  artists via `RecordingCredit`. That join table had existed since the schema was
  designed and had **never once been used** — 0 of 1,229 recordings had more than one
  credit before this.
  - **`" - "` is the collaboration delimiter**, and that distinction is load-bearing:
    31 artist rows use it and it's what the old workbook joined two acts with.
    `" and "` / `" & "` (12 rows) are usually part of ONE act's name ("Homer & Jethro",
    "Lester Young and His Kansas City Seven"), so they're surfaced for review but
    ranked below and never presumed to be a split — though "Ink Spots and Ella
    Fitzgerald" is a genuine collaboration hiding in that group.
  - **The proposed part names are editable inputs, not fixed text.** A naive split of
    "James - Sinatra" yields "James" and "Sinatra", neither of which is a canonical name
    you'd want on file; you correct them to "Harry James"/"Frank Sinatra" before
    anything is written. Green border = resolves to an artist already on file, amber =
    will be created.
  - **`Recording.credit_text` is never touched by a split** (non-negotiable #4) — the
    printed billing stays exactly as transcribed; only the structured credit changes.
  - The combined Artist row is **deleted** once nothing credits it (owner's decision —
    `credit_text` preserves the printed billing and the entry-form search covers it, so
    nothing is lost, and the autocomplete stops offering an act that isn't real).
  - `Artist.confirmed_single_act` (migration `0007`) is how "this is one act, not two"
    is remembered, so the queue stops asking. A genuine collaboration never needs it —
    splitting deletes the combined row, which removes it from the queue that way.
  - **Three outcomes, not two** (third added 2026-08-21 at the owner's prompting):
    1. **Split** — two separate acts ("Ernest Tubb - Red Foley") → one `RecordingCredit`
       each.
    2. **Canonical is shorter** — ONE act billed with its band ("Julia Lee And Her Boy
       Friends" *is* Julia Lee). Re-credits to the leader and keeps the full printed
       billing as an `ArtistAlias`, which is precisely what that table is for. Detected
       by `credits.leader_guess()` ("X and his/her/the Y", "X's Y"). This is the case
       that also **merges duplicates**: Lester Young was on file twice, as "…and His
       Kansas City Seven" and "…'s Quartet".
    3. **One act, name as-is** ("Homer & Jethro") → `confirmed_single_act`, reversible
       via the "reopen" list at the bottom of the queue.
    Leader-pattern rows often carry NO delimiter at all, so `pending_artists()` checks
    `leader_guess` as well as `split_name` — otherwise "Lester Young's Quartet" would
    never surface.
  - **`apply_split` refuses when the artist has aliases.** Deleting an Artist CASCADEs
    to `ArtistAlias`, and a split has no unambiguous home for one ("Bing Crosby - Andrews
    Sisters" pointing at just Bing Crosby would be wrong). `apply_canonical` *does* move
    them, because there it's a single destination.
  - **Any number of artists, in both places** (2026-09-11). The entry form's create box
    and the `/credits/` split form each have a "+ artist" button that clones another box
    client-side. All boxes share one field name and the views read them with `getlist`,
    so a third or fourth credited act needs no server-side change.
  - **`docs/audit/combined_credit_artists.txt` is the older hand-written checklist for
    this same work** — superseded by the queue, kept as the record of the original
    import-time decision not to auto-split.
- **An artist-name typo was fixed while building the above:** "Ella Fritzgerald" was on
  file as a canonical `Artist` with 5 credits, and "Ella Fitzgerald" didn't exist
  standalone at all — which had spawned THREE separate combined rows for the same
  Ella+Louis Jordan pairing. Proven a transcription typo rather than a printing
  variation because Decca 18587's A and B sides (one physical disc) were transcribed
  with different spellings. `canonical_name` corrected to "Ella Fitzgerald" and
  "Ella Fritzgerald" kept as an `ArtistAlias` — which is exactly what that table is for,
  and means the typo'd combined rows still resolve to the right artist when split.
  Every `credit_text` left exactly as transcribed.
- **Discogs matching, built 2026-08-21** — `/discogs/` (`views.discogs_*`,
  `charts/discogs.py`, `charts/management/commands/discogs_match.py`,
  `discogs_queue.html` + `_discogs_row.html`). Links each `Recording` to its Discogs
  release, for what Billboard never printed (songwriters, producers, real release year,
  country, genres). Key points:
  - **`charts/discogs.py` is the only module in the project that touches the network**,
    and nothing outside the `/discogs/` section imports it. Stdlib-only (`urllib`, no
    `requests`), so `requirements.txt` stays at just Django. Every failure mode (no
    token, no internet, 401, 429) is caught and shown on the page — it never 500s.
  - **Two models**: `DiscogsMatch` (one-to-one with `Recording`, carries the status
    workflow + cached snapshot + credits + raw JSON) and `DiscogsCandidate` (the
    transient shortlist a search turns up). Deliberately NOT fields on `Recording` —
    the entry form's model stays untouched.
  - **Nothing is ever auto-confirmed.** Searching only ever produces `status=suggested`
    with a shortlist; a human picks. Same "don't silently guess" rule as the entry
    form's tie-picker and duplicate-recording check. Bulk-confirm exists but only acts
    on `EXACT` candidates (label AND catalog number both matched, normalized).
  - **`EXACT` requires label + catalog AND a plausible text link** (added 2026-08-21).
    Label/catalog are the primary signal, but not sufficient alone: labels reuse
    catalog numbers across series and decades. The case that forced this — Capitol 253
    is a Tex Ritter 78, Capitol **H-253** is a *Bozo The Clown* 10" LP. Same label,
    same digits, scored `EXACT`, and sailed through bulk-confirm unattended. Now
    `discogs.text_plausibly_relates()` demotes such a result to `STRONG` when the
    search result's title relates to *neither* our song *nor* our artist. The bar is
    deliberately low (either one relating is enough) because it can only ever demote —
    a B-side matches on song, a retitled recording matches on artist, a re-billed act
    matches on song. Verified against the real data: **zero** of the existing 123
    comparable linked matches get demoted, and the Bozo case does.
  - **Establishing a link ALWAYS fetches its per-track detail, on every path**
    (settled 2026-08-21 after two revisions — worth reading, since the earlier designs
    look reasonable and were wrong). Single-row confirm (`discogs_confirm`), manual
    entry (`discogs_manual`), and the BULK "confirm exact matches"
    (`discogs_bulk`'s `confirm_exact`) all call the shared `_try_fetch_detail()`.
    The bulk one originally skipped it to keep the click instant, which seemed like
    the right performance trade — but in practice the "catch up later" step got
    forgotten and **124 rows sat half-populated**, showing release-level stand-ins
    (the `rel` tag) with no songwriter credits at all. A slower click is a much better
    outcome than a data model with a quietly-incomplete state in it, so now no path
    through the UI can leave a linked row unenriched. Cost is bounded: at most one
    pageful (25) at ~1s each. On an API failure the confirm still stands and only the
    enrichment is pending (`needs_detail`) — the "Fetch details" action and
    `--details` flag remain for exactly that recovery case, not as a routine step.
  - **Granularity note worth remembering:** a `Recording` is one SIDE of a disc, a
    Discogs *Release* is the whole disc — so A and B sides legitimately share a
    `discogs_release_id`. Songwriter credits are per-TRACK, so `_find_track()` matches
    the side to its track by title (then by A/B position) before falling back to
    release-level credits.
  - The recordings matrix has a **Discogs column** showing link state — entirely from
    cached data, no API call, so the matrix still works offline.
  - **`/discogs/audit/`** (added 2026-08-21) is the read-only side-by-side check of
    what's already linked: our transcribed data vs Discogs', newest link first, with
    disagreements highlighted. Answers the opposite question from the queue ("which of
    the ones I just linked did I get wrong?" rather than "what still needs deciding").
    Sorted by `DiscogsMatch.linked_at`, a field that exists precisely because
    `updated_at` is `auto_now` and gets bumped by any later edit — see that field's
    comment. Migration `0006` backfills it from `updated_at` for pre-existing links.
  - **"Do these agree?" is defined once and shared**: `catalog_numbers_agree()` and
    `labels_agree()` in `charts/discogs.py`, plus `_loose_text_match()` in views, are
    used by BOTH match scoring and the audit view, so what scores `EXACT` and what the
    audit calls agreement can't drift apart. All three are deliberately forgiving, and
    that tuning is load-bearing: against the real 331 linked matches, a naive
    exact-string version flagged 94 (mostly `Cadet (2)` vs `Cadet`, `RCA Victor` vs
    `Victor`, `B-10879` vs `10879`, reordered credits) — noise at that rate makes the
    audit useless. Current rules flag 7. The tests in `DiscogsAgreementTests` are all
    real cases from that data; don't tighten these without re-checking the flag count.
  - `python manage.py discogs_match` does the same searching in bulk from a terminal
    (where a long-running process is normal); the web section is for interactive review
    and small batches. Neither ever confirms anything.
  - **The entry form can record a Discogs link, typed by hand** (added 2026-08-21).
    The inline "create new recording" sub-form has an optional Discogs release URL/ID
    field. It is stored, never looked up — the entry form still makes **zero** network
    calls, so a week can always be entered offline. This is exactly the shape
    non-negotiable #2 names as acceptable ("a manually typed value, never a live lookup
    that blocks saving a week"). The link lands as `status=MANUAL` with
    `needs_detail=True`; `/discogs/` enriches it later. That's the one remaining
    legitimate source of un-enriched rows, and it's unavoidable — fetching there would
    break the offline guarantee. A bad/unparseable value never blocks the recording,
    and an existing link on a reused recording is never silently overwritten.
    **Fixed 2026-09-11:** those rows used to count as *settled* (`status=manual`), so the
    queue's default filter hid them and nothing ever enriched them -- three sat invisible.
    "Needs attention" now includes any linked row still needing detail
    (`DiscogsMatch.needs_detail_q()`, the ONE shared definition -- the old hand-written
    filters also counted master-only rows, which can never be fetched). They sort to the
    top and each fetches itself on page load via `hx-trigger="load"`; a failed fetch
    renders a retry button instead, so it can't loop. Also fixed: picking an existing
    recording off the duplicate-check list silently dropped the typed Discogs ID -- both
    paths now go through `views._link_discogs_from_entry`. `discogs._throttle` gained a
    lock, since several rows can now fetch concurrently on the threaded dev server.
  - **Token setup:** a Discogs personal access token from
    https://www.discogs.com/settings/developers, in a git-ignored `.env` file at the
    project root as `DISCOGS_TOKEN=...` (or the same-named env var). The `/discogs/`
    page shows setup instructions when it's missing.
  - Tests mock `charts.discogs._request` throughout — **the suite never hits the real
    API** (it would be slow, rate-limited, non-deterministic, and would fail on a
    machine with no token). What's tested is our logic: confidence scoring/normalization,
    the two-tier search, per-track credit extraction, and the workflow's state
    transitions.
- **A latent CSRF bug was found and fixed while building the above:** HTMX `hx-post`
  from an element that isn't inside a `<form>` sends no CSRF token, so
  `CsrfViewMiddleware` 403s it. `base.html`'s `<body>` now carries
  `hx-headers='{"X-CSRFToken": "{{ csrf_token }}"}'`, which every descendant inherits —
  verified with `Client(enforce_csrf_checks=True)` (403 without the header, 200 with).
- **UI consistency pass, 2026-10-15.** All styling lives in ONE stylesheet in
  `charts/templates/charts/base.html`, organised as tokens -> base elements -> layout ->
  components -> page-specific (see its header comment). Conventions to keep:
  - **An action is a button, a link is for data.** "Enter a week", "Edit", "Fix", "Search"
    are `<button>`/`<a class="btn">`; a chart name, a week date, a Discogs release are
    links. Before this pass, most actions were underlined links in each page's subtitle.
  - **No hard-coded colours or font sizes below the tokens.** Four sizes exist
    (`--fs-xs/sm/control/base`); inputs, selects and buttons all share `--fs-control` and
    `--control-h`, which is what makes a button line up with the box beside it. Button
    variants: `.btn-primary` (one per screen), `.btn-ghost`, `.btn-danger`, `.btn-sm`,
    `.btn-icon`. Every badge class shares one geometry; alerts are `.alert-success/
    warning/error`.
  - **Top nav bar** (Charts / Dashboard / Recordings / Discogs / Credits / Admin) replaces the
    per-page "back to ... / ... ->" links; the active section comes from
    `request.resolver_match.url_name`, so a new page only needs adding to the `if` there.
    Discogs queue/audit are tabs (`_discogs_tabs.html`).
  - **One page width** (`--page-width`) so the left edge never moves between pages;
    reading pages opt into `{% block main_class %}page-narrow{% endblock %}`, which caps
    content width without re-centring it. Dark mode has its own token values.
  - **Bug fixed along the way:** pressing Enter in the entry form's last-week box (how a
    row resolves) also submitted the whole week -- the browser's implicit form submission.
    The form's `onkeydown` now blocks that; only the Save button saves a week.
  - **The week page's title is a chart switcher** (2026-10-26): a `<select>` styled as the
    h1 (`.title-select`, `field-sizing: content` so the arrow hugs the name). Each option
    goes to the SAME week on that chart, or its closest week on file, labelled with that
    date when it differs (`views.chart_switch_options`; `ChartSwitcherTests`).
  - Visual checks were done with Playwright + Edge against a second dev server, with
    every non-GET request aborted so nothing could write to `billboard.db`.
- **Dashboard, built 2026-10-16** — `/dashboard/` (`charts/dashboard.py` for every
  number, `views.dashboard` thin, `dashboard.html` + `_dashboard_results.html`). One
  filter row (two-handle date slider over the 437 Saturdays on file + Year shortcuts,
  Genre, Chart, Top N) scopes everything below it: five headline numbers, *Debuts per
  month* (per week for ranges of 26 weeks or fewer), *Top labels*, and three Top-N rankings
  (most weeks at #1, most weeks on chart, top artists). Live-updates via HTMX like the
  matrix, bookmarkable via `hx-push-url`. Read-only. The definitions are the part to
  keep — they're in `dashboard.py`'s docstring and each has a test in `DashboardTests`:
  - **A week is a calendar week**: `Count("week_date", distinct=True)`, never rows. A
    record at #1 on three charts one Saturday has ONE week at #1. Same rule as the
    collapsed matrix, so the two pages agree.
  - **Ties credit everyone** (39 chart-weeks have a tie at #1), and rankings use
    competition ranking ("1, 2, 2, 4"). When the Top-N cut lands mid-tie, the extra
    rows aren't listed but a footnote counts them.
  - **Artists come from `RecordingCredit`**, not `credit_text` — a collaboration credits
    both acts. Ranked by *weeks present*; *record-weeks* (each hit's weeks summed) rides
    along as a column because the two genuinely diverge: on all data Louis Jordan leads
    on weeks present (337) but Eddy Arnold on record-weeks (645).
  - **Debuts exclude records already on a chart in that chart's first week on file**
    (their real debut predates the data). Note the debut graph still rises in 1948–49
    when ALL charts are selected, partly because the Retail charts begin on file then —
    more charts tracked means more first appearances, not necessarily more releases.
  - The graphs are **plain HTML/CSS, built server-side** — no chart library, nothing from
    a CDN, so the page is fully offline like the rest. Bar colour is its own token
    (`--series-1`), validated with the dataviz palette checker against both page surfaces.
    Each graph has a "Show as table" twin; marks are focusable with a shared tooltip.
  - Verified against a brute-force recount in plain Python sets: zero mismatches on all
    data and on Country/1950.
- **Movement comes from the data, not from typed last-week numbers (fixed 2026-10-18).**
  The chart browser used to decide NEW/up/down purely from `printed_last_week_position`,
  which is only filled when a last-week number is *typed* — so a week entered by search
  (Country Jockeys 1951-04-07) read as ten debuts when nine were carryovers; 131 entries
  across the database were shown wrongly as NEW. Now `views.last_week_on_file()` is the
  single source of "was it on the chart last week?" and `views.classify_movement()`
  turns it into up / down / same / **new** (never on this chart before) / **re**
  (re-entry: on it before, not last week) / **unknown** (previous week not on file — a
  gap or the chart's first week; *not* "new", since nothing says it wasn't there). With
  no previous week on file, a typed number is the only evidence and is used.
  - **The printed field is never written or changed.** Not typed → "Last wk" shows the
    on-file position in muted italics (with a key under the table), so a computed number
    can't pass for a transcribed one. Typed and disagreeing → ⚠ with both numbers on
    hover, plus a count above the table (non-negotiable #4).
  - **Those disagreements are listed and diagnosed at `/audit/last-week/`** (built
    2026-10-18; `charts/lastweek_audit.py`, button on the chart list, and a "See why"
    button in each week's warning). Of ~9,100 typed last-week numbers, 489 disagree with
    the previous week on file. They split into two very different things, reported
    differently on purpose:
    - **460 sit in three unbroken stretches of R&B Jukebox** (1943-08-07 for 9 weeks,
      1943-10-16 for 42, 1944-08-12 for 15), where most of each week's column is wrong.
      Evidence gathered (all in the page): the SET of carryovers agrees (526 typed vs 529
      records really on the previous week) so only the numbers are off; no other week on
      file matches the columns (not a mis-dated week); only 2 weeks look copied down from
      the week above; and — the strongest signal — positions on file in these weeks
      **never tie (0 of 66 weeks), while the rest of R&B Jukebox ties in ~75% of weeks**.
      Leading hypothesis, unconfirmed: positions in these stretches were entered as the
      order of lines on the page rather than the printed position numbers. Needs one week
      checked against the magazine before anything is changed; per-row guesses are
      deliberately NOT made inside stretches (they'd be coincidence).
    - **29 individual cases**, each with a likely cause: missing from last week (on the
      chart two weeks ago and this week but not in between — 9), different version of the
      same song at that position (5, incl. Elton Britt's "Someday", a known reissue pair),
      swapped pair, tie counting, one place off, lookalike digit, matches two weeks back,
      wrong record picked, debut with a last-week number.
    - Nothing is changed by the page. A row clears itself once the data agrees; a genuine
      misprint is acknowledged by writing a `printed_anomaly_note` on the entry, which moves
      it to an "already noted" list — the existing field, so no schema change. Tests:
      `LastWeekAuditTests` (one per cause, plus: total equals the week pages' ⚠ count,
      and titles are escaped inside the HTML explanations).
  - The entry form shows "last week #N" on a row picked by search (or an edited week
    saved without a number) — `views._last_week_hint`; a hint only, the box is never
    filled from it. Tests: `ChartWeekMovementTests`.
- **Next step when resuming:** get a Discogs token in place and run a first batch
  (`/discogs/` → "Search next 10", or `python manage.py discogs_match --limit 20`) to
  see how well 1940s-50s 78rpm singles actually resolve on Discogs — the matching logic
  is tested against mocked responses but has never been run against the real API.
  Expect a meaningful proportion of `no_match`; the manual paste-a-URL box exists for
  exactly that.
- **Also still open:** use the entry form for more real weeks and see what breaks in
  practice, and the `combined_credit_artists.txt` splitting work.

## Non-negotiables

Read these before proposing any change in direction.

1. **Data entry stays manual.** The point of this project is that the owner learns the charts
   week by week while entering them. Never propose scraping, bulk-importing from Wikipedia /
   Whitburn / secondhand datasets, or OCR-ing the magazines into the database. The tool's job
   is to *reduce typing and prevent mistakes*, not to replace the human.
2. **Everything that matters runs locally, and chart entry never needs the internet.**
   SQLite on disk, local dev server. No cloud database, no hosted service, no telemetry.
   The browser and the weekly entry form must keep working with the network unplugged —
   entering a chart week is *never* allowed to depend on an external service being
   reachable.

   *Amended 2026-08-21 (owner's decision).* This rule originally read "no API calls that
   need an internet connection at runtime," full stop. That was too broad: the real
   intent is (a) never automate *filling in the charts* from an outside source, and
   (b) never make internet a prerequisite for the owner to sit down and work. It was
   never meant to forbid deliberately looking a record up somewhere after the fact.
   So: the **Discogs section** (`/discogs/`, see below) *is* allowed to make live API
   calls, because it's a separate, opt-in, after-the-fact enrichment screen the owner
   chooses to open — not part of the entry path. The entry form still makes zero network
   calls, and nothing in the chart browser or entry flow degrades when offline. A future
   "Discogs ID" field on the entry form would be acceptable *only* as a manually typed
   value, never as a live lookup that blocks saving a week.
3. **Comment code thoroughly.** The owner is using this project to learn. Every non-obvious
   line gets an inline comment explaining *why*, not just *what*. When you present code in
   chat, explain what each block does in prose as well. Verbose is correct here.
4. **The magazine is the source of truth.** When printed values and computed values disagree,
   surface the conflict — never silently overwrite what the magazine printed.

## Stack

- **Python 3.11+**
- **Django** — chosen deliberately over Flask/FastAPI/Streamlit because:
  - Django admin gives a complete browsable/editable view of every table for free
    (the "base looker"), with no code beyond declaring the models.
  - `autocomplete_fields` turns every foreign key into a search-as-you-type widget,
    which removes manual ID matching entirely.
  - Migrations keep the schema versioned as the model evolves.
- **SQLite** — single file, portable, zero setup, correct for a single-user local app.
- Plain Django templates. Add **HTMX** only where a partial page update genuinely helps
  (autocomplete dropdowns, inline row validation). No SPA, no build step.

## Current schema

@docs/schema.sql

Regenerate that file after any migration so these instructions never drift. The `sqlite3`
CLI isn't on PATH in this environment, so use Python's `sqlite3` module instead of the
`.schema` shell command:

```bash
python -c "
import sqlite3
conn = sqlite3.connect('billboard.db')
rows = conn.execute(\"SELECT sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type DESC, name\").fetchall()
open('docs/schema.sql', 'w', encoding='utf-8').write(''.join(sql.strip() + ';\n' for (sql,) in rows))
"
```

(If the `sqlite3` CLI is ever available, `sqlite3 billboard.db .schema > docs/schema.sql`
does the same thing.)

## The weekly entry form — the heart of the project

This is the one screen worth optimizing hard. Design principle:

> On a typical week, 85–95% of a chart's positions are carryovers from the previous week.
> The form must never ask the user to re-identify a record it can already infer.

Required behavior for the chart-week entry screen:

1. **Seeded from the previous week.** Given a chart and a week date, load the previous
   week's entries into memory before rendering.
2. **Last-week number resolves the recording.** The user types only the "last week" position
   printed in the magazine; the form resolves it to a `recording_id`:

   ```sql
   -- Given chart_id, the PREVIOUS week's date, and the last-week number typed by the user
   SELECT recording_id
   FROM chart_entry
   WHERE chart_id  = :chart_id
     AND week_date = :previous_week_date
     AND position  = :last_week_position;
   ```

   Show the resolved title + artist back to the user immediately so a mistyped number is
   caught on the spot.
3. **Search only for debuts and re-entries.** The recording/artist autocomplete is used for
   the handful of rows per week that aren't carryovers, not for all 100.
4. **Keyboard-first.** Tab moves between fields, Enter commits a row. No mouse required to
   enter a full chart week.
5. **Save the whole week atomically.** One transaction per chart-week, so a partial save
   never leaves a half-entered chart in the database.

## Derived fields as a typo detector

`weeks_on_chart` and `peak_position` are computable:

- `weeks_on = previous week's weeks_on + 1`
- `peak = MIN(previous peak, this week's position)`

Still capture what the magazine printed, then **flag mismatches** in the UI rather than
auto-correcting. A mismatch means one of two things, both worth knowing: a typo by the user,
or a genuine printing error by Billboard (more common than you'd expect). Store the printed
values; keep the computed values available for comparison.

## Artist and recording identity

Billboard credits the same act inconsistently across decades
("Conway Twitty" / "Conway Twitty & Loretta Lynn" / "Conway Twitty and the Twitty Birds").

- The recording stores the **credit exactly as printed**, plus a foreign key to a canonical artist.
- A separate **alias table** holds every observed spelling, and the autocomplete searches it.
- Never auto-merge two artists. Suggest a possible duplicate and let the user decide.

## Constraints belong in the schema

Push integrity down to the database so bad data can't be written in the first place:

```sql
-- Catches an exact duplicate row (same recording, same position, same week --
-- a real accidental double-save) at insert time, WITHOUT blocking a genuine
-- printed tie (two DIFFERENT recordings sharing one position in the same
-- week). The audit found competition/skip-ranking ties are the normal
-- convention in this data (1,499 tie groups) -- an earlier version of this
-- constraint that omitted `recording` rejected every one of them. See
-- docs/audit/position_skip_review.csv.
UNIQUE (chart_id, week_date, position, recording_id)
```

Also enforce: position within the chart's declared size, week dates aligned to the chart's
publication day, and foreign keys ON (SQLite needs `PRAGMA foreign_keys = ON` per connection).

## Conventions

- Dates are stored as ISO `YYYY-MM-DD`, always the **week-ending date** printed on the issue.
- Migrations are never edited after being applied — add a new one instead.
- Every schema change is followed by regenerating `docs/schema.sql`.
- Tests for the resolution logic (`last_week → recording_id`) and the derived-field
  comparison are worth writing; UI tests are not.

## Commands

```bash
python manage.py runserver          # start the local server
python manage.py makemigrations     # after editing models.py
python manage.py migrate            # apply schema changes
python manage.py test               # run the test suite
```

## When in doubt

Ask before changing the data model. The database already contains hand-entered weeks that
represent real hours of work — schema changes must come with a migration that preserves
every existing row.
