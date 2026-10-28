"""
charts/chart_week.py -- one chart, one week-ending date, and everything the
app needs to know about how it relates to the PREVIOUS CHART.

"Was this recording on the previous chart, and where?" is the question
behind carryovers, debuts, re-entries, resolving a typed last-week number
in the entry form, the ⚠ conflict marks on the week pages, and the whole
last-week audit. Before this module existed that question was answered in
six places, each restating "week minus 7 days, best position if listed
twice" in its own words. Now it's answered here, once:

    week = ChartWeek(chart, date(1951, 4, 7))
    week.previous_on_file           # is the previous chart entered at all?
    week.resolve(4)                 # last-week "#4" -> its chart entries (2+ = a tie)
    week.last_week_position(rec_id) # where it sat on the previous chart, or None
    week.movement(rec_id, position, printed_last_week)
    week.streak(recording)          # (consecutive weeks so far, best position)

The previous chart is the previous PUBLICATION date -- never "the most
recent week that happens to be on file". See
docs/adr/0001-previous-chart-is-previous-publication.md for why; the short
version is that after a skipped week, "#4" would quietly resolve against a
chart two issues old.

Every query a ChartWeek needs runs lazily, the first time it's asked, and
is cached on the object -- so a page that asks about 40 recordings costs
the same couple of queries as one that asks about one.

The two RULES at the bottom (best_positions, last_week_conflict) are also
plain functions, because the last-week audit checks ~1,300 chart weeks in
one pass from a bulk load: building a ChartWeek per week would be ~1,300
sets of queries. Sharing the rules rather than the object keeps the audit
fast AND keeps it agreeing with the week pages by construction.
"""
from collections import namedtuple
from datetime import timedelta
from functools import cached_property

# Every chart on file publishes weekly on a Saturday (Chart.publication_weekday),
# and every consecutive pair of chart weeks on file is exactly 7 days apart
# (checked 2026-10-27: 1,202 of 1,202). If Billboard ever turns out to have
# skipped or doubled an issue, previous_chart_date / next_chart_date are the
# ONE place that exception goes.
PUBLICATION_INTERVAL = timedelta(days=7)


def previous_chart_date(week_date):
    """The week-ending date of the chart published just before `week_date`.

    Whether or not that chart is on file -- that's a separate question
    (ChartWeek.previous_on_file), and deliberately so: see the ADR.
    """
    return week_date - PUBLICATION_INTERVAL


def next_chart_date(week_date):
    """The week-ending date of the chart published just after `week_date`."""
    return week_date + PUBLICATION_INTERVAL


# What movement() returns. `code` is one of:
#   "up" / "down" / "same" -- compared against the previous chart
#   "new"     -- a DEBUT: never on this chart before
#   "re"      -- a RE-ENTRY: on this chart before, but not on the previous chart
#   "unknown" -- the previous chart isn't on file and no last-week number was
#                typed, so nothing says whether it was there or not
# `conflict` is True when a typed last-week number disagrees with the
# previous chart on file. `last_on_file` is where it really sat, or None.
Movement = namedtuple("Movement", "code conflict last_on_file")


class ChartWeek:
    def __init__(self, chart, week_date):
        self.chart = chart
        self.week_date = week_date
        self.previous_date = previous_chart_date(week_date)

    # ------------------------------------------------------------ queries

    @cached_property
    def _previous_positions(self):
        # {recording_id: best position on the previous chart}. One query.
        return best_positions(
            self.chart.entries.filter(week_date=self.previous_date)
            .values_list("recording_id", "position")
        )

    @cached_property
    def _charted_before(self):
        # Every recording that was on this chart at ANY earlier week -- what
        # separates a re-entry from a debut. One query; a few hundred ids at
        # most per chart, so a set is cheaper than asking per recording.
        return set(
            self.chart.entries.filter(week_date__lt=self.week_date)
            .values_list("recording_id", flat=True)
        )

    @property
    def previous_on_file(self):
        """Is the previous chart entered at all?

        If not (a gap, or this chart's first week on file), "wasn't on the
        previous chart" can't be told apart from "the previous chart simply
        isn't here" -- which is why movement() says "unknown" then, not "new".
        A chart week exists exactly when it has entries, so the positions
        query already answers this.
        """
        return bool(self._previous_positions)

    # ------------------------------------------------------------ answers

    def resolve(self, last_week_number):
        """The previous chart's entries at position `last_week_number`.

        What the entry form does with a typed last-week number. Returns a
        list, because the answer has three shapes and the caller must
        handle all three: [] -- nothing printed there (a typo, or the
        previous chart isn't on file); one entry -- resolved; two or more
        -- a TIE on the previous chart, and the position alone can't say
        which recording carried over, so the caller asks rather than
        guessing. Ordered by id so a tie's options keep a stable order.
        """
        return list(
            self.chart.entries.filter(week_date=self.previous_date, position=last_week_number)
            .select_related("recording", "recording__song", "recording__label")
            .order_by("id")
        )

    def last_week_position(self, recording_id):
        """Where `recording_id` sat on the previous chart, per the data, or
        None if it wasn't there (or the previous chart isn't on file).

        Its best position if a printed anomaly lists it twice that week.
        Never reads printed_last_week_position: that's only filled when a
        number happened to be typed, so treating an empty one as "wasn't on
        the chart" made every row picked by search look like a debut
        (2026-10-18: a whole Country Jockeys week, 1951-04-07, read as ten
        debuts when nine were carryovers).
        """
        return self._previous_positions.get(recording_id)

    def movement(self, recording_id, position, printed_last_week):
        """How a chart entry moved since the previous chart -> Movement.

        * Previous chart ON FILE: compare against where the record actually
          sat on it -- the data, which stays consistent with the
          neighbouring weeks however this week's row was entered. Not on it
          at all: "new" if it never charted here before, "re" if it did.
        * Previous chart NOT on file: a typed last-week number is the only
          evidence, so use it if there is one; otherwise "unknown".

        `conflict` flags a typed number that disagrees with the previous
        chart on file -- a typo, or a genuine printing quirk. Per CLAUDE.md
        non-negotiable #4 it's SURFACED, never silently resolved; the
        printed value is never changed here.
        """
        on_file = self.last_week_position(recording_id)
        basis = on_file if self.previous_on_file else printed_last_week
        if basis is not None:
            code = "up" if basis > position else ("down" if basis < position else "same")
        elif self.previous_on_file:
            code = "re" if recording_id in self._charted_before else "new"
        else:
            code = "unknown"
        conflict = last_week_conflict(printed_last_week, on_file, self.previous_on_file)
        return Movement(code, conflict, on_file)

    def streak(self, recording):
        """(weeks so far, best position so far) for `recording`'s unbroken
        run of consecutive previous charts, walking back one publication at
        a time. Both None if it wasn't on the previous chart.

        Only ever shown as a "computed, for comparison" hint next to the
        printed weeks-on-chart input -- never auto-filled (CLAUDE.md,
        "Derived fields as a typo detector").
        """
        weeks_so_far = 0
        best_position = None
        cursor = self.week_date
        while True:
            cursor = previous_chart_date(cursor)
            entry = (
                self.chart.entries.filter(week_date=cursor, recording=recording)
                .order_by("position").first()
            )
            if entry is None:
                break
            weeks_so_far += 1
            if best_position is None or entry.position < best_position:
                best_position = entry.position
        return (weeks_so_far or None, best_position)


# ------------------------------------------------------------ shared rules

def best_positions(recording_positions):
    """{recording_id: position} from (recording_id, position) pairs, keeping
    each recording's BEST (lowest) position.

    A recording normally appears once per chart week; the 2 accepted
    printed anomalies (docs/audit/same_recording_repeated.csv) list one
    twice, and its best position is the one that counts.
    """
    best = {}
    for recording_id, position in recording_positions:
        if recording_id not in best or position < best[recording_id]:
            best[recording_id] = position
    return best


def last_week_conflict(printed_last_week, on_file, previous_on_file):
    """Does a typed last-week number disagree with the previous chart on file?

    Only when there IS a previous chart on file to disagree with, and only
    when a number was typed at all. `on_file` is None when the recording
    wasn't on the previous chart -- a typed number then is a conflict too.
    """
    return previous_on_file and printed_last_week is not None and printed_last_week != on_file
