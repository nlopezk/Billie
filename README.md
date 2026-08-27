# Billboard Chart Database

A fully local Django + SQLite database of historical Billboard charts (Country and R&B,
retail/jukebox/jockeys), hand-transcribed from scanned magazine issues. See `CLAUDE.md`
for the full project rationale, design decisions, and rules for working on this codebase
— that file is the actual source of truth for how this project should be extended, read
it before making schema changes.

## Quick start

```bash
python manage.py runserver
```

Then open **http://127.0.0.1:8000/admin/** and log in.

- **Username:** `admin`
- **Password:** set when the project was first scaffolded — if you don't have it, reset
  it rather than hunt for it:

  ```bash
  python manage.py changepassword admin
  ```

## What's already here

- The schema is fully migrated (`charts/models.py` — chart, chart era, chart entry,
  recording, recording credit, song, artist, artist alias, label).
- The database is already populated from `Bb Charts fixed.xlsx`: **10,750 chart entries**
  across 5 charts (1942–1950 so far), 1,161 recordings, 350 artists, 967 songs, 77 labels.
  Verified row-for-row against the workbook — see the import summary in `CLAUDE.md`.
- **The weekly entry form is built** — `/<chart_id>/entry/` (also linked from the chart
  browser and chart list). Type a last week's position and it resolves the recording for
  you; a genuine tie last week shows a pick-one prompt instead of guessing; search covers
  existing recordings; a recording that's never charted before can be created right there
  inline. The whole week saves as one transaction — if anything's wrong, nothing gets
  written and everything you typed is still there to fix. Uses HTMX for the live
  lookups/search, vendored locally as a static file (`charts/static/charts/htmx.min.js`)
  — no CDN, no runtime internet dependency.
- **Editing an already-saved week** has its own page too — from the chart browser,
  "edit this week." Same form as entering a new week (same resolve/search/create/delete
  tools), just pre-filled with what's already saved, and it REPLACES that week's rows on
  save rather than adding new ones.
- There's also a small **read-only chart browser** at the site root (`/`) — pick a chart,
  page through it week by week, see rankings/movement/ties without going through the flat
  admin list. Not a replacement for the admin or the entry form, just a nicer way to look.
- Admin is still there for everything else — every table browsable/editable, with
  search-as-you-type on every foreign key.
- **Admin changes are logged, entry/edit-form changes are not** — see "Auditing changes"
  below. Worth knowing if you're relying on the log to catch a mistake: a correction made
  through "edit this week" won't show up there, only ones made directly in `/admin/`.

## Auditing changes

`/admin/` → **Log entries** (under Administration) lists every addition, change, and
deletion made *through the admin*, with who and when. Read-only by design — you can view
it but not edit or delete entries, since a log you can rewrite isn't an audit trail.

**What it does NOT catch:** anything done outside the admin UI — `manage.py shell`, the
one-time import command, any future script, and both the weekly entry form and "edit
this week." All of those write via direct ORM calls, not through `ModelAdmin`, so none
of them appear in this log. The 10,750 imported chart entries have zero log entries, on
purpose. Also, `change_message` only records *which fields* changed on a "Change" entry,
not the actual before/after values.

If you ever need full before/after field-level history that also covers non-admin
changes, that's a job for a separate library (e.g. `django-simple-history`) — not set up
yet, ask before adding it since it's a real schema decision (a shadow history table per
model you opt into), not a small tweak.

## Important things to know before touching data

- **Ties are normal, not bugs.** Two different recordings can legitimately share one
  printed position in the same chart-week (competition/skip-ranking, common in the
  1940s data) — the database allows this on purpose. `docs/audit/position_skip_review.csv`
  has the full reasoning.
- **~35 artist entries are unsplit combined credits** (e.g. "Bing Crosby - Andrews
  Sisters" is one `Artist` row, not two). This was a deliberate choice — see
  `docs/audit/combined_credit_artists.txt` for the checklist of which ones to review and
  split into proper multi-artist credits, at your own pace, via the `RecordingCredit`
  inline on a Recording's admin page. Never auto-merge or auto-split artists — always a
  human judgment call, per CLAUDE.md.
- **2 chart entries are intentional duplicates**, not errors: Billboard itself printed
  "Double Crossing Blues" (Savoy 731A) twice in the same week under inconsistent catalog
  numbers. Flagged via `printed_anomaly_note` on those `ChartEntry` rows rather than
  removed. See `docs/audit/same_recording_repeated.csv`.
- **6 recordings have a confirmed reissue link** (`Recording.reissue_of`) — same
  performance, repackaged under a new label/catalog number (e.g. Elton Britt's "Someday",
  Bluebird → RCA Victor). Sourced against real discography records — see
  `docs/audit/other_issues.csv`.
- **The one-time import command won't run twice.** `python manage.py import_workbook`
  refuses to run if `ChartEntry` already has rows, specifically so it can't accidentally
  create duplicates. If you ever need to redo the import from scratch, you'd need to
  clear the relevant tables first (or start from a fresh `billboard.db`).
- **`Bb Charts fixed.xlsx` is still the archival original.** The database was migrated
  from it, but the workbook itself is untouched and kept as a record of the owner's
  original hand-transcribed work.
- **Migrations are never edited after being applied** — always add a new one. This
  already happened once: migration `0002` fixed a constraint bug found during the import
  (see `CLAUDE.md`'s project status section for what happened and why).

## Project layout

```
config/                   Django settings (app config, not a data model)
charts/                   the one app -- models, admin, the import command
  management/commands/
    import_workbook.py    one-time import from the Excel workbook
docs/
  models_draft.py         dated design record (charts/models.py is the live version)
  schema.sql              regenerated after every migration -- @-included in CLAUDE.md
  audit/                  data-quality audit trail, all findings resolved or documented
Bb Charts fixed.xlsx      original data source, hand-transcribed, archival
billboard.db              the actual database -- TRACKED IN GIT, see "Backups" below
CLAUDE.md                 project rules, design rationale, current status
```

## Backups

**`billboard.db` is committed to git and pushed to the private GitHub repo. That is the
backup.** There is no other copy and no automatic process — nothing backs it up on a
timer, on save, or when you close the server.

It was excluded from git early on, on the theory that `import_workbook.py` could always
regenerate it from `Bb Charts fixed.xlsx`. That stopped being true the moment anything was
entered or edited after the import: weeks typed into the entry form, admin corrections,
artist aliases, every Discogs link. None of that is in the workbook, and `import_workbook`
refuses to run against a non-empty table anyway. **Re-running the import would not recover
a single one of those.** So the database is now tracked, and committing it is the only
thing standing between you and losing that work.

### After an entry session, do this

```bash
git add billboard.db
git commit -m "Chart entry: <what you did>"
git push
```

Committing after each real session is the point — code commits happen on their own
schedule and will not reliably carry your data with them. If you enter ten weeks and
don't commit, ten weeks is what's at risk.

### Restoring

```bash
git log --oneline -- billboard.db        # every version, newest first
git show <commit>:billboard.db > recovered.db   # inspect one without touching the live file
git checkout <commit> -- billboard.db    # roll the live database back to that version
```

The middle one is the safe move: recover to a separate file, open it, confirm it's the
version you want, and only then replace the live one.

### A note on repo size

SQLite is a single binary file, so git stores a **complete copy on every commit** — it
can't diff inside it the way it does with text. The file is ~11 MB and grows as Discogs
detail (the cached raw JSON per match) accumulates, so each commit that touches it adds
roughly that much to repo history permanently. Fine at this scale, and worth knowing
before committing it hundreds of times in a day. Commit per session, not per week entered.

## Common commands

```bash
python manage.py runserver          # start the local server
python manage.py makemigrations     # after editing charts/models.py
python manage.py migrate            # apply schema changes
python manage.py test               # run the test suite
python manage.py createsuperuser    # make another admin login
python manage.py changepassword admin   # reset the admin password
```

After any migration, regenerate `docs/schema.sql` (the `sqlite3` CLI isn't on PATH in
this environment — see CLAUDE.md's "Current schema" section for the Python one-liner that
does the same thing).
