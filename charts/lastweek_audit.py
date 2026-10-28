"""
charts/lastweek_audit.py -- every typed last-week number that disagrees
with the previous week ON FILE, with the most likely reason, for the
owner to check against the magazine (/audit/last-week/).

Read-only. Nothing here changes data: per CLAUDE.md non-negotiable #4
the printed value is never "corrected" by the program; the owner fixes
whichever side is wrong (this week's typed number, or last week's
positions) in the entry form, and a row drops off this page by itself
once the two agree. A genuine Billboard misprint is acknowledged the same
way the other accepted anomalies are: by writing a printed_anomaly_note
on the entry, which moves it to the "already noted" list.

Two kinds of problem, deliberately reported differently:

STRETCHES -- runs of consecutive weeks where most of the typed column
disagrees (a "column week": 4+ disagreements AND at least half the typed
numbers wrong). On the real data that's three runs of R&B Jukebox,
1943-44, 66 weeks, the large majority of all disagreements. Inside them
the numbers are scrambled wholesale, so a per-row guess like "off by one"
would be pure coincidence; each stretch is reported once, with the
evidence about what kind of problem it is.

INDIVIDUAL disagreements -- everything else. For those the per-row
checks below are meaningful, and each row gets the first cause that fits.
"""
from collections import defaultdict

from django.utils.html import escape

from .chart_week import best_positions, last_week_conflict, previous_chart_date
from .models import Chart, ChartEntry, Recording

# A week counts as a "column week" (part of a stretch) at this many
# disagreements, provided they're also at least half its typed numbers.
COLUMN_WEEK_MIN = 4

# Digit pairs easy to confuse on a scanned page. Deliberately short: a
# long list would "explain" any difference and so explain nothing.
LOOKALIKE_DIGITS = {frozenset(p) for p in [(1, 7), (3, 8), (5, 6), (6, 8), (0, 8), (3, 5), (6, 9), (4, 9)]}


def _norm(title):
    # For "same song, different version" -- case/apostrophe/spacing-blind.
    return " ".join(title.lower().replace("'", "").replace("’", "").split())


class _Data:
    """Every chart entry, indexed the ways the checks need. One query;
    ~13,000 small rows, so loading it all is simpler and faster than
    hundreds of targeted queries."""

    def __init__(self):
        self.pos_of = {}                                      # (chart, week) -> {rec: best position}
        pairs = defaultdict(list)                             # (chart, week) -> [(rec, position)]
        self.at_pos = defaultdict(lambda: defaultdict(list))  # (chart, week) -> {position: [rec]}
        self.typed = defaultdict(list)                        # (chart, week) -> [entry dicts with a typed LW]
        self.first_week = {}                                  # (chart, rec) -> first week on that chart
        rows = ChartEntry.objects.values_list(
            "id", "chart_id", "week_date", "recording_id", "position",
            "printed_last_week_position", "printed_anomaly_note",
        )
        for entry_id, chart, week, rec, pos, lw, note in rows:
            key = (chart, week)
            pairs[key].append((rec, pos))
            self.at_pos[key][pos].append(rec)
            if lw is not None:
                self.typed[key].append({"id": entry_id, "rec": rec, "position": pos, "typed": lw, "note": note})
            fk = (chart, rec)
            if fk not in self.first_week or week < self.first_week[fk]:
                self.first_week[fk] = week
        # Best position if a printed anomaly lists one record twice in a
        # week -- the SAME rule function the week pages use, so the two agree.
        self.pos_of = {key: best_positions(p) for key, p in pairs.items()}
        self.recordings = {
            r.id: r for r in Recording.objects.select_related("song", "label")
        }
        self.charts = {c.id: c for c in Chart.objects.all()}

    def week_has_ties(self, key):
        return any(len(recs) > 1 for recs in self.at_pos[key].values())


# --------------------------------------------------------------- finding

def _disagreements(data):
    """Every typed last-week number whose previous chart IS on file and
    disagrees with it -- chart_week.last_week_conflict, the same function
    behind the ⚠ marks on the week pages, so the two always agree."""
    found = []
    for (chart, week), entries in data.typed.items():
        prev = (chart, previous_chart_date(week))
        previous_on_file = prev in data.pos_of
        for e in entries:
            on_file = data.pos_of.get(prev, {}).get(e["rec"])
            if last_week_conflict(e["typed"], on_file, previous_on_file):
                found.append({**e, "chart": chart, "week": week, "on_file": on_file})
    return found


def _stretches(data, rows):
    """Group "column weeks" into runs of consecutive weeks per chart."""
    by_week = defaultdict(list)
    for r in rows:
        by_week[(r["chart"], r["week"])].append(r)
    column_weeks = sorted(
        key for key, rs in by_week.items()
        if len(rs) >= COLUMN_WEEK_MIN and len(rs) * 2 >= len(data.typed[key])
    )
    runs = []
    for key in column_weeks:
        if runs and runs[-1][-1][0] == key[0] and previous_chart_date(key[1]) == runs[-1][-1][1]:
            runs[-1].append(key)
        else:
            runs.append([key])
    return runs, by_week


def _stretch_evidence(data, run, by_week, all_stretch_weeks):
    """What can be said about a stretch from the data alone -- facts, not a
    verdict. The owner decides with the magazine open."""
    chart = run[0][0]
    chart_weeks = [k for k in data.pos_of if k[0] == chart]
    # Compared against the chart's NORMAL weeks: every stretch excluded,
    # or the other stretches' tie-free weeks would water down the contrast.
    rest = [k for k in chart_weeks if k not in all_stretch_weeks]
    typed_n = sum(len(data.typed[k]) for k in run)
    carry_n = sum(
        sum(1 for rec in data.pos_of[k] if rec in data.pos_of.get((chart, previous_chart_date(k[1])), {}))
        for k in run
    )
    ties_in = sum(data.week_has_ties(k) for k in run)
    ties_rest = sum(data.week_has_ties(k) for k in rest)

    # Does ANY week on this chart explain a stretch week's typed column?
    # (A week filed under the wrong date would.)
    other_week_fits = 0
    for key in run:
        typed = {e["rec"]: e["typed"] for e in data.typed[key]}
        for other in chart_weeks:
            if other != key and sum(data.pos_of[other].get(r) == lw for r, lw in typed.items()) >= len(typed) - 1:
                other_week_fits += 1
                break

    # Weeks whose typed column repeats the previous week's ROW BY ROW --
    # the signature of a column copied down in a spreadsheet.
    copied = []
    for key in run:
        prev = (chart, previous_chart_date(key[1]))
        this_col = {e["position"]: e["typed"] for e in data.typed[key]}
        prev_col = {e["position"]: e["typed"] for e in data.typed.get(prev, [])}
        if len(this_col) >= 4 and sum(prev_col.get(p) == v for p, v in this_col.items()) >= len(this_col) - 1:
            copied.append(key[1])

    facts = []
    facts.append(
        f"{typed_n} rows in these weeks have a printed last-week number, and {carry_n} rows really were on "
        f"the previous week's chart on file &mdash; so <em>which</em> records carried over agrees; "
        f"only the numbers don't."
        if abs(typed_n - carry_n) <= max(3, typed_n // 20) else
        f"{typed_n} rows have a printed last-week number, but {carry_n} rows were on the previous week's "
        f"chart on file &mdash; even <em>which</em> records carried over doesn't agree."
    )
    if rest and ties_in * 10 <= len(run) and ties_rest * 10 >= len(rest) * 4:
        facts.append(
            f"Positions on file in these weeks never tie ({ties_in} of {len(run)} weeks), while the rest of "
            f"this chart has a tie in {round(ties_rest * 100 / len(rest))}% of weeks. That suggests these "
            f"positions may be the <em>order of lines on the page</em> (1&ndash;{max(data.pos_of[run[0]].values())}) "
            f"rather than the position numbers printed beside each record."
        )
    facts.append(
        "No other week on file matches these last-week numbers, so it isn't a week filed under the wrong date."
        if other_week_fits == 0 else
        f"In {other_week_fits} of these weeks the last-week numbers DO match another week on file &mdash; "
        f"check for a week filed under the wrong date."
    )
    if copied:
        facts.append(
            "These weeks' last-week columns repeat the previous week's, row for row &mdash; probably copied "
            "down in the spreadsheet rather than read from the page: "
            + ", ".join(d.strftime("%b %d, %Y").replace(" 0", " ") for d in copied) + "."
        )
    return facts, copied


# --------------------------------------------------------------- causes

def _dense_rank(data, key, rec):
    distinct = sorted(set(data.pos_of[key].values()))
    return distinct.index(data.pos_of[key][rec]) + 1


def _competition_rank(data, key, rec):
    here = data.pos_of[key][rec]
    return 1 + sum(len(recs) for p, recs in data.at_pos[key].items() if p < here)


def _label(data, rec):
    # Escaped HERE because the explanations below are HTML (bold, dashes)
    # rendered with |safe -- song titles and credits are data, and a title
    # containing "&" or "<" must show as text, not be read as markup.
    r = data.recordings[rec]
    return escape(f"{r.song.title} — {r.credit_text}")


def _diagnose(data, row):
    """(code, short label, explanation) -- the FIRST cause that fits, in an
    order from most specific to most generic."""
    chart, week, rec = row["chart"], row["week"], row["rec"]
    T, O = row["typed"], row["on_file"]
    prev = (chart, previous_chart_date(week))
    occupants = data.at_pos[prev].get(T, [])
    title = _norm(data.recordings[rec].song.title)
    other_version = next(
        (x for x in occupants if x != rec and _norm(data.recordings[x].song.title) == title), None
    )
    two_back = data.pos_of.get((chart, previous_chart_date(previous_chart_date(week))), {}).get(rec)
    when = lambda d: d.strftime("%b %d, %Y").replace(" 0", " ")

    if O is not None:
        this_week_typed = {e["rec"]: e["typed"] for e in data.typed[(chart, week)]}
        partner = next((x for x in occupants if this_week_typed.get(x) == O), None)
        if partner is not None:
            return ("swapped", "Swapped with another row",
                    f"Last week on file has <b>{_label(data, partner)}</b> at #{T} and this record at #{O}, but "
                    f"this week's printed numbers say the opposite. Either the two last-week numbers were typed "
                    f"the wrong way round, or last week's two records were entered in each other's places.")
        dense, comp = _dense_rank(data, prev, rec), _competition_rank(data, prev, rec)
        if data.week_has_ties(prev) and T in (dense, comp) and dense != comp:
            how = ("counting the tie as one place (1, 2, 2, 3)" if T == dense
                   else "skipping places after the tie (1, 2, 2, 4)")
            return ("ranking", "Tie counting",
                    f"Last week has a tie ahead of this record. On file it's #{O}; #{T} is what you get "
                    f"{how}. Usually a difference in how ties were numbered, not a wrong record.")
        if other_version is not None:
            r = data.recordings[other_version]
            return ("other_version", "Different version",
                    f"Last week's #{T} is the same song by <b>{escape(r.credit_text)}</b> ({escape(r.catalog_display)}). "
                    f"One of the two weeks may be linked to the wrong version of the song.")
        if two_back == T:
            return ("two_back", "Matches two weeks back",
                    f"It was #{T} on {when(previous_chart_date(previous_chart_date(week)))}, the week before last. The printed number may "
                    f"have been taken from the wrong week.")
        if abs(T - O) == 1:
            return ("off_by_one", "One place off",
                    f"Printed #{T}, on file #{O} &mdash; one place apart: a slip in this week's number, or in "
                    f"last week's position.")
        lookalike = (T // 10 == O // 10 and frozenset((T % 10, O % 10)) in LOOKALIKE_DIGITS) or (
            max(T, O) > 9 and str(T) == str(O)[::-1])
        if lookalike:
            return ("digit", "Misread digit?",
                    f"#{T} vs #{O}: digits that are easy to confuse on a scanned page (3/8, 1/7, 5/6, 12/21&hellip;).")
        occ = ", ".join(_label(data, x) for x in occupants) or "nobody"
        return ("differs", "Different position",
                f"Printed #{T}, but last week on file has it at #{O}. Last week's #{T} on file is {occ}.")

    # Not on last week's chart at all.
    if other_version is not None:
        r = data.recordings[other_version]
        return ("other_version", "Different version",
                f"Last week's #{T} is the same song by <b>{escape(r.credit_text)}</b> ({escape(r.catalog_display)}). This week's "
                f"row, or last week's, may be linked to the wrong version.")
    if two_back is not None:
        return ("missing", "Missing from last week?",
                f"On the chart two weeks ago (#{two_back}, {when(previous_chart_date(previous_chart_date(week)))}) and this week, but not on last "
                f"week's chart on file. It may have been left out when last week was entered.")
    if not occupants:
        last = max(data.pos_of[prev].values())
        return ("no_position", f"No #{T} last week",
                f"Last week's chart on file stops at #{last}, so there is no #{T} to have been.")
    if not any(x in data.pos_of[(chart, week)] for x in occupants):
        occ = ", ".join(f"<b>{_label(data, x)}</b>" for x in occupants)
        return ("wrong_record", "Wrong record picked?",
                f"Last week's #{T} was {occ}, which isn't on this week's chart. This row might really be that "
                f"record.")
    if data.first_week.get((chart, rec)) == week:
        return ("debut", "Debut with a last-week number",
                "First week on this chart on file, yet a last-week number is printed: a misprint, or the record "
                "was on last week's chart under a different listing.")
    return ("re_entry", "Not on last week's chart",
            "On this chart in earlier weeks but not last week, yet a last-week number is printed.")


# --------------------------------------------------------------- assembly

def _row_view(data, row):
    rec = data.recordings[row["rec"]]
    prev = (row["chart"], previous_chart_date(row["week"]))
    occupants = data.at_pos[prev].get(row["typed"], [])
    return {
        **row,
        "song": rec.song.title,
        "credit_text": rec.credit_text,
        "catalog_display": rec.catalog_display,
        "chart_obj": data.charts[row["chart"]],
        # Who sat at the printed number last week, on file -- the first thing
        # to look at when deciding which side is wrong.
        # Plain text (not _label's escaped HTML): the template auto-escapes it.
        "at_typed_last_week": [
            f"{data.recordings[x].song.title} — {data.recordings[x].credit_text}"
            for x in occupants if x != row["rec"]
        ],
        "prev_week": previous_chart_date(row["week"]),
    }


def build(params):
    data = _Data()
    rows = _disagreements(data)
    chart_filter = params.get("chart", "")
    if chart_filter.isdigit():
        rows = [r for r in rows if str(r["chart"]) == chart_filter]
    else:
        chart_filter = ""

    # Already acknowledged as a printing anomaly: listed apart, not mixed in.
    noted = [r for r in rows if r["note"]]
    open_rows = [r for r in rows if not r["note"]]

    runs, by_week = _stretches(data, open_rows)
    in_runs = {key for run in runs for key in run}

    stretches = []
    for run in runs:
        facts, copied = _stretch_evidence(data, run, by_week, in_runs)
        weeks = []
        for key in run:
            weeks.append({
                "chart": key[0],
                "week": key[1],
                "copied": key[1] in copied,
                "typed_total": len(data.typed[key]),
                "rows": sorted((_row_view(data, r) for r in by_week[key]), key=lambda r: r["position"]),
            })
        stretches.append({
            "chart": data.charts[run[0][0]],
            "start": run[0][1],
            "end": run[-1][1],
            "week_count": len(run),
            "row_count": sum(len(by_week[k]) for k in run),
            "facts": facts,
            "weeks": weeks,
        })

    individual = []
    for r in open_rows:
        if (r["chart"], r["week"]) in in_runs:
            continue
        code, label, why = _diagnose(data, r)
        individual.append({**_row_view(data, r), "cause": code, "cause_label": label, "why": why})
    individual.sort(key=lambda r: (data.charts[r["chart"]].category, data.charts[r["chart"]].method, r["week"], r["position"]))

    cause_counts = defaultdict(int)
    for r in individual:
        cause_counts[r["cause_label"]] += 1

    return {
        "total": len(rows),
        "noted": sorted((_row_view(data, r) for r in noted), key=lambda r: (r["week"], r["position"])),
        "stretches": stretches,
        "stretch_rows": sum(s["row_count"] for s in stretches),
        "individual": individual,
        "cause_counts": sorted(cause_counts.items(), key=lambda kv: -kv[1]),
        "chart_filter": chart_filter,
        "charts": data.charts,
    }
