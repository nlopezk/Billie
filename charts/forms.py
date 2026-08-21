# charts/forms.py
#
# ChartEntryRowForm validates ONE row of the weekly entry form. It's a
# plain Form, not a ModelForm or a formset -- the template renders each
# row's fields by hand with per-row-suffixed names (position_0,
# position_1, ...) rather than Django's formset naming convention,
# because that makes HTMX targeting for a specific row (resolve a
# recording, swap in a preview) much simpler than fighting formset
# management-form bookkeeping. This Form is still used server-side, in
# the final save view, to validate each row's collected data cleanly.

from django import forms


class ChartEntryRowForm(forms.Form):
    position = forms.IntegerField(min_value=1)
    # Not a ChartEntry field itself -- only used client-side to drive the
    # last-week-resolves-a-recording lookup. What actually gets stored is
    # recording_id (resolved) and printed_last_week_position (the raw
    # printed value, kept regardless of whether it resolved).
    last_week_position = forms.IntegerField(required=False, min_value=1)
    recording_id = forms.IntegerField(
        required=True,
        error_messages={"required": "No recording resolved for this row yet."},
    )
    printed_peak_position = forms.IntegerField(required=False, min_value=1)
    printed_weeks_on_chart = forms.IntegerField(required=False, min_value=1)
    printed_anomaly_note = forms.CharField(required=False, max_length=300)
