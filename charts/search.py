"""
charts/search.py -- the one text-matching rule for finding recordings.

Typed text is split into words, and EACH word must match at least one of a
recording's song title, printed credit, label name or catalog number (AND
across words, OR across fields). That's what makes "Capitol 304" find a
Capitol release cataloged 304, though no single field contains both words.

The entry form's search, the recordings matrix, and both Discogs pages all
search this way. They used to carry four hand-written copies of the rule;
now they all build it here, so the same text finds the same records on
every page.

Deliberately NOT here: a minimum length. The entry form waits for two
characters before searching, but that's about not firing a request on
every first keystroke -- a property of that search box, not of matching --
so it stays in the entry form's view.
"""
from django.db.models import Q

from .models import Recording

# The recording fields every search matches against, relative to Recording.
RECORDING_FIELDS = ("song__title", "credit_text", "label__name", "catalog_number")


def recording_text_q(text, prefix="", extra_fields=()):
    """A Q object matching `text` against a recording's fields.

    prefix       -- the path from the model being filtered to its Recording,
                    e.g. "recording__" when filtering ChartEntry or
                    DiscogsMatch. "" when filtering Recording itself.
    extra_fields -- more fields (full paths, not prefixed) that a word may
                    match instead, e.g. a Discogs match's track title.

    Text with no words in it matches NOTHING, never everything -- an empty
    Q() would otherwise turn a blank search into "every recording".
    """
    words = text.split()
    if not words:
        return Q(pk__in=[])
    fields = [prefix + f for f in RECORDING_FIELDS] + list(extra_fields)
    query = Q()
    for word in words:
        any_field = Q()
        for f in fields:
            any_field |= Q(**{f"{f}__icontains": word})
        query &= any_field
    return query


def search_recordings(text, limit=15):
    """Recordings matching `text`, alphabetical by song, at most `limit`."""
    return (
        Recording.objects.filter(recording_text_q(text))
        .select_related("song", "label").order_by("song__title")[:limit]
    )
