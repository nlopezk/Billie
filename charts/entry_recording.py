"""
charts/entry_recording.py -- creating a recording from the entry form's
"new recording" box.

Used for a debut whose recording has never been entered at all. The box's
fields become a NewRecording; then, in order:

    new = NewRecording.from_post(request.POST, row)
    new.missing()               # required fields left blank -> show them, stop
    possible_duplicates(new)    # anything on file this might already be -> ask
    recording, notes = create_recording(new)

possible_duplicates is a backstop, not the main defence: the intended
workflow is to search first and only create when the search finds nothing.
create_recording never duplicates (label + catalog # + side is unique, so
an existing recording is reused), and returns plain-language notes for
anything the person should look at -- never a silent change.
"""
from dataclasses import dataclass, field

from django.db import transaction

from . import credits as credits_lib
from . import search as search_lib
from .models import Label, Recording, RecordingCredit, Song

# How many possible duplicates the check shows at most.
DUPLICATE_LIMIT = 8


@dataclass
class NewRecording:
    song_title: str
    # Canonical artist names in billing order -- which IS credit_order.
    artist_names: list = field(default_factory=list)
    # The billing exactly as printed (non-negotiable #4). Defaults to the
    # artist names joined with " - ", the old workbook's own convention.
    credit_text: str = ""
    label_name: str = ""
    catalog_number: str = ""
    side: str = ""
    # A hand-typed Discogs release/master URL or id, or "". Never looked up.
    discogs_ref: str = ""

    @classmethod
    def from_post(cls, post, row):
        """The create box for entry-form row `row`, from request.POST."""
        # getlist, not get: the box can hold any number of artist inputs
        # under one name, so a collaboration gets one RecordingCredit per
        # act instead of a single fake "A - B" artist that /credits/ then
        # has to unpick. De-duplicated with order preserved.
        artist_names = list(dict.fromkeys(
            n.strip() for n in post.getlist(f"new_artist_{row}") if n.strip()
        ))
        return cls(
            song_title=post.get(f"new_song_{row}", "").strip(),
            artist_names=artist_names,
            # Whatever is typed into the Credit field wins, because that
            # field is the as-printed value; the joined names are only a default.
            credit_text=post.get(f"new_credit_text_{row}", "").strip() or " - ".join(artist_names),
            label_name=post.get(f"new_label_{row}", "").strip(),
            catalog_number=post.get(f"new_catalog_{row}", "").strip(),
            side=post.get(f"new_side_{row}", "").strip().upper(),
            discogs_ref=post.get(f"new_discogs_{row}", "").strip(),
        )

    def missing(self):
        """Names of the required fields left blank, e.g. ["artist", "label"]."""
        required = [
            ("song title", self.song_title), ("artist", self.artist_names),
            ("label", self.label_name), ("catalog number", self.catalog_number),
        ]
        return [name for name, value in required if not value]


def possible_duplicates(new):
    """Recordings already on file that `new` might really be, or [].

    Two DIFFERENT signals, unioned rather than one combined query:
      1. Song + credit look similar -- the same recording typed with a
         mistyped or different catalog number.
      2. Label + catalog number match what's on file -- literally the same
         physical release, even if the artist was typed differently. The
         more dangerous case to miss: create_recording reuses on label +
         catalog + side alone, so a typo'd artist would quietly land on
         the existing recording.
    A single AND-across-every-word search would miss signal 2 whenever the
    artist typed doesn't match what's on file -- exactly the case worth
    flagging.
    """
    candidates = {}
    for r in search_lib.search_recordings(f"{new.song_title} {new.credit_text}"):
        candidates[r.id] = r
    for r in Recording.objects.filter(
        label__name__icontains=new.label_name, catalog_number__icontains=new.catalog_number
    ).select_related("song", "label"):
        candidates[r.id] = r
    return list(candidates.values())[:DUPLICATE_LIMIT]


def create_recording(new):
    """Create `new` (or reuse the identical label + catalog # + side) ->
    (recording, notes).

    Song and Label are reused by exact name. Artists are reused through
    credits.resolve_or_create_artist -- case-insensitive and through
    aliases, so typing "Joe Turner" reuses "Big Joe Turner" instead of
    quietly creating a second act. Credits are written only for a
    recording created HERE; a reused one keeps the credits it has.
    """
    with transaction.atomic():
        song, _ = Song.objects.get_or_create(title=new.song_title)
        artists = [credits_lib.resolve_or_create_artist(name)[0] for name in new.artist_names]
        label, _ = Label.objects.get_or_create(name=new.label_name)
        # get_or_create on the actual unique constraint -- if this exact
        # label+catalog+side already exists, reuse it (and its existing
        # song/credit_text) rather than erroring or silently duplicating.
        recording, created = Recording.objects.get_or_create(
            label=label, catalog_number=new.catalog_number, side=new.side,
            defaults={"song": song, "credit_text": new.credit_text},
        )
        if created:
            for order, artist in enumerate(artists, start=1):
                RecordingCredit.objects.create(recording=recording, artist=artist, credit_order=order)

    notes = []
    # The exact mistake that produced 43 combined-credit rows needing
    # cleanup: typing "Ernest Tubb - Red Foley" into ONE artist box. Not
    # blocking -- credit_text still records the billing correctly and
    # /credits/ can unpick it later -- but said plainly at the moment it
    # happens, which is the only time it's cheap to fix.
    if len(new.artist_names) == 1:
        parts, is_strong = credits_lib.split_name(new.artist_names[0])
        if is_strong:
            notes.append(
                f"“{new.artist_names[0]}” looks like {len(parts)} artists. If it is, "
                f"Reset this row and use a separate artist box for each — otherwise it "
                f"gets filed as one act."
            )
    return recording, notes
