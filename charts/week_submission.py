"""
charts/week_submission.py -- saving one chart week from the entry form.

The entry form's whole job ends here: a list of submitted rows becomes that
chart week's chart entries, ALL of them or NONE of them (CLAUDE.md, "Save
the whole week atomically"). A brand-new week and an edit of a saved week
are the same operation with one switch:

    result = submit_week(chart, week_date, rows, replace=False)   # a new week
    result = submit_week(chart, week_date, rows, replace=True)    # an edit
    if result.saved: ...
    else: re-show result.rows, with result.errors

`rows` is plain data -- a list of dicts, one per submitted row, as
rows_from_post() builds them from the form -- so this can be called and
tested without a web request. Nothing is ever partly written: any invalid
row, or any entry the database rejects, leaves the chart week exactly as it
was, and `result.rows` carries everything that was typed so the page can be
re-shown with nothing lost.
"""
import re
from dataclasses import dataclass, field

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from .forms import ChartEntryRowForm
from .models import ChartEntry, Recording


@dataclass
class Submission:
    saved: bool
    errors: list = field(default_factory=list)
    # Every submitted row as a re-renderable dict (see _row_for_rerender),
    # in submitted order -- what the page shows again when saved is False.
    rows: list = field(default_factory=list)
    # The next free row index: one past the highest SUBMITTED index, which
    # isn't len(rows) when a row was deleted from the middle of the page.
    next_index: int = 0
    # True when a NEW week was refused because that chart week is already
    # on file -- the page then offers "edit this week" instead.
    already_on_file: bool = False


def rows_from_post(post):
    """The form's submitted rows as plain dicts, from request.POST.

    The page names each row's inputs with its index (position_3,
    last_week_position_3, ...). Indices are discovered from what was
    actually submitted rather than assumed to run 0..N-1, because deleting
    a row from the middle of the page (the delete button in
    _entry_row.html) leaves a gap, and that has to keep working.
    """
    indices = sorted({
        int(m.group(1)) for key in post if (m := re.fullmatch(r"position_(\d+)", key))
    })
    return [
        {
            "index": i,
            "position": post.get(f"position_{i}"),
            "last_week_position": post.get(f"last_week_position_{i}") or None,
            "recording_id": post.get(f"recording_id_{i}"),
            "printed_peak_position": post.get(f"printed_peak_position_{i}") or None,
            "printed_weeks_on_chart": post.get(f"printed_weeks_on_chart_{i}") or None,
            "printed_anomaly_note": post.get(f"printed_anomaly_note_{i}", ""),
        }
        for i in indices
    ]


def submit_week(chart, week_date, rows, *, replace):
    """Validate `rows` and write them as `chart`'s entries for `week_date`.

    replace=False -- a new chart week. REFUSED if this chart week already
                     has entries on file: adding rows on top of a saved
                     week would mix two charts together (an exact
                     double-submit is caught by the unique constraint, but
                     a different set of rows wouldn't be). Changing a saved
                     week only ever happens through an edit.
    replace=True  -- an edit: whatever is on file for this chart week is
                     deleted first and the rows written in its place, in
                     the same transaction. Safe because nothing in the
                     schema has a foreign key pointing AT ChartEntry, so
                     nothing is left dangling.
    """
    next_index = (max(r["index"] for r in rows) + 1) if rows else 0
    if not replace and chart.entries.filter(week_date=week_date).exists():
        return Submission(
            saved=False, already_on_file=True, next_index=next_index,
            errors=[f"This chart week ({week_date.isoformat()}) is already on file. "
                    f"Nothing was added to it -- to change it, edit that week instead."],
            rows=[_row_for_rerender(raw) for raw in rows],
        )

    cleaned_rows = []
    errors = []
    for raw in rows:
        form = ChartEntryRowForm(raw)
        if form.is_valid():
            cleaned_rows.append(form.cleaned_data)
        else:
            field_errors = "; ".join(f"{name}: {', '.join(msgs)}" for name, msgs in form.errors.items())
            errors.append(f"Row {raw['index'] + 1}: {field_errors}")

    # Only reach the database once every row is valid -- an invalid row
    # means nothing is attempted at all.
    if not errors:
        errors = _write(chart, week_date, cleaned_rows, replace)

    if not errors:
        return Submission(saved=True, next_index=next_index)
    return Submission(
        saved=False, errors=errors, next_index=next_index,
        rows=[_row_for_rerender(raw) for raw in rows],
    )


def _write(chart, week_date, cleaned_rows, replace):
    # Returns a list of error strings; empty means it succeeded. Any error
    # inside the atomic block rolls back EVERYTHING, including the delete
    # of an edited week's old entries.
    try:
        with transaction.atomic():
            if replace:
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
        return ["Save failed: " + "; ".join(e.messages)]
    except IntegrityError as e:
        return [f"Save failed (a database constraint rejected this): {e}"]
    return []


def _row_for_rerender(raw):
    # The row exactly as typed, plus its resolved recording (for the
    # "✓ title — artist" preview), in the shape _entry_row.html expects.
    # The computed hints aren't recomputed on a failed save -- they're
    # comparison aids, and the typed values are what matter here.
    recording = None
    if raw["recording_id"]:
        recording = Recording.objects.filter(pk=raw["recording_id"]).select_related("song", "label").first()
    return {
        "index": raw["index"],
        "position": raw["position"] or (raw["index"] + 1),
        "last_week_position": raw["last_week_position"],
        "recording": recording,
        "printed_peak_position": raw["printed_peak_position"],
        "printed_weeks_on_chart": raw["printed_weeks_on_chart"],
        "printed_anomaly_note": raw["printed_anomaly_note"],
        "weeks_hint": None,
        "peak_hint": None,
    }
