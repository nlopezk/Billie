# The previous chart is the previous publication date, never "the previous week on file"

Everything that asks "where was this recording last week?" (resolving a typed last-week
number, carryover/debut/re-entry, the last-week audit, the entry form's hints) looks at
the chart published one issue earlier, which is currently always 7 days back since every
chart publishes on Saturday. If that chart week isn't on file yet, the answer is
"unknown". It never falls back to the most recent chart week that *is* on file, because
after a skipped or not-yet-entered week that would be two issues old, and a typed
last-week number like "#4" would quietly resolve to the wrong record.

The rule lives in exactly one function (`charts/chart_week.py`), so if Billboard ever
turns out to have skipped or doubled an issue, that exception is handled in one place.
The chart browser's ‹ prev / next › links are a separate, deliberately different idea
(adjacent chart weeks on file, for navigation) and must not be used for chart logic.
