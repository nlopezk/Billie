"""
charts/playlists.py -- everything a playlist does, kept out of views.py.

A PLAYLIST (GLOSSARY.md) is a named list of recordings picked by hand. The
picker page (/playlists/<id>/pick/) is the recordings matrix with a tick box
per row; every tick is saved the moment it's made, so changing a filter never
loses a pick. "Add all" adds every recording the current filters match --
ALL of them, not a page (the matrix has no paging).

The pieces, in the order the pages use them:

    create(name)                 -> (playlist, error)
    member_ids(playlist)         -> set of recording ids already in it
    set_member(playlist, id, on) -> tick / untick one recording
    add_all / remove_all         -> the bulk buttons, given the matrix rows
    move(playlist, id, "up")     -> reorder by one place
    rows(playlist)               -> the playlist page's table, in order
    export_csv / export_text     -> the downloadable copies

Every number on the playlist page comes from the recordings matrix's
one-row-per-recording query over ALL data (recordings_matrix.build with a
`playlist` scope), so a playlist shows the same best peak / weeks as
/recordings/ -- not the windowed numbers of whatever filter it was picked
with. A playlist doesn't remember those filters, by design.
"""
import csv
import io

from django.db import IntegrityError, transaction
from django.db.models import Max

from . import recordings_matrix as recordings_matrix_lib
from .models import Playlist, PlaylistItem, Recording, SpotifyMatch, discogs_url_for, format_catalog, spotify_url_for


def create(name, description=""):
    """A new, empty playlist. Returns (playlist, None), or (None, message)
    when the name is blank or already taken -- names are unique so two
    playlists can't be mixed up."""
    name = (name or "").strip()
    if not name:
        return None, "Give the playlist a name."
    if Playlist.objects.filter(name__iexact=name).exists():
        # Case-insensitive on purpose: "R&B 1950" and "r&b 1950" side by
        # side in the list would be exactly the confusion uniqueness is for.
        return None, f"A playlist called “{name}” already exists."
    return Playlist.objects.create(name=name, description=(description or "").strip()), None


def rename(playlist, name, description=None):
    """Change the name (and optionally the description). Returns an error
    message, or None on success."""
    name = (name or "").strip()
    if not name:
        return "Give the playlist a name."
    if Playlist.objects.filter(name__iexact=name).exclude(pk=playlist.pk).exists():
        return f"A playlist called “{name}” already exists."
    playlist.name = name
    if description is not None:
        playlist.description = description.strip()
    playlist.save()
    return None


def member_ids(playlist):
    """Recording ids already in the playlist -- what the picker ticks."""
    return set(playlist.items.values_list("recording_id", flat=True))


def _next_position(playlist):
    # One past the current last place; 1 for an empty playlist.
    return (playlist.items.aggregate(m=Max("position"))["m"] or 0) + 1


def set_member(playlist, recording_id, on):
    """Tick (on=True) or untick one recording. Idempotent both ways: ticking
    something already in, or unticking something already out, changes
    nothing -- a double-click or a retried request can't duplicate a row."""
    if on:
        if not Recording.objects.filter(pk=recording_id).exists():
            return
        try:
            # atomic() so a unique-constraint clash (two near-simultaneous
            # ticks of the same box) rolls back cleanly instead of breaking
            # the surrounding transaction.
            with transaction.atomic():
                PlaylistItem.objects.get_or_create(
                    playlist=playlist, recording_id=recording_id,
                    defaults={"position": _next_position(playlist)},
                )
        except IntegrityError:
            pass  # the other request already added it -- same end state
    else:
        playlist.items.filter(recording_id=recording_id).delete()


def recording_ids_in_rows(rows):
    """The distinct recording ids in matrix rows, in the order shown. In
    one-row-per-chart mode one recording can be several rows; it counts
    once, at its first row."""
    seen, ordered = set(), []
    for row in rows:
        rid = row["recording_id"]
        if rid not in seen:
            seen.add(rid)
            ordered.append(rid)
    return ordered


@transaction.atomic
def add_all(playlist, recording_ids):
    """Append every recording not already in the playlist, in the given
    order (the matrix's current sort -- so sorting by first entry and adding
    all gives a chronological playlist). Ones already in keep their place.
    Returns how many were added."""
    already = member_ids(playlist)
    position = _next_position(playlist)
    new_items = []
    for rid in recording_ids:
        if rid in already:
            continue
        already.add(rid)
        new_items.append(PlaylistItem(playlist=playlist, recording_id=rid, position=position))
        position += 1
    PlaylistItem.objects.bulk_create(new_items)
    return len(new_items)


def remove_all(playlist, recording_ids):
    """Take every given recording out of the playlist. Returns how many."""
    deleted, _ = playlist.items.filter(recording_id__in=list(recording_ids)).delete()
    return deleted


@transaction.atomic
def move(playlist, recording_id, direction):
    """Swap a recording with its neighbour above ("up") or below ("down").
    Does nothing at either end."""
    item = playlist.items.filter(recording_id=recording_id).first()
    if item is None:
        return
    if direction == "up":
        neighbour = playlist.items.filter(position__lt=item.position).order_by("-position").first()
    else:
        neighbour = playlist.items.filter(position__gt=item.position).order_by("position").first()
    if neighbour is None:
        return
    # Plain swap of the two sort keys. position has no unique constraint,
    # so there's no intermediate clash to dodge.
    item.position, neighbour.position = neighbour.position, item.position
    item.save(update_fields=["position"])
    neighbour.save(update_fields=["position"])


def rows(playlist):
    """The playlist page's table, in playlist order: one row per recording
    with the recordings matrix's all-data, one-row-per-recording numbers.

    A recording with no chart entries left (a week was re-entered without
    it after it was picked) has no matrix row; it still gets a row here with
    its identity and blank numbers, so nothing vanishes from a playlist
    without the owner seeing it."""
    matrix = recordings_matrix_lib.build({"group": "recording", "playlist": str(playlist.pk)})
    by_id = {r["recording_id"]: r for r in matrix["rows"]}
    items = list(playlist.items.select_related("recording__song", "recording__label"))
    result = []
    for index, item in enumerate(items):
        rec = item.recording
        row = by_id.get(rec.id) or {
            # The same keys the matrix rows use, so one template renders both.
            "recording_id": rec.id,
            "recording__song__title": rec.song.title,
            "recording__credit_text": rec.credit_text,
            "catalog_display": format_catalog(rec.label.name, rec.catalog_number, rec.side),
            "recording__catalog_number": rec.catalog_number,
            "no_entries": True,
            **_spotify_fields(rec),
        }
        result.append({**row, "is_first": index == 0, "is_last": index == len(items) - 1})
    return result


def _spotify_fields(recording):
    # The Spotify keys a matrix row carries, for a recording that has no
    # matrix row (see rows() above).
    match = getattr(recording, "spotify", None)
    status = match.status if match else None
    linked = bool(match and match.status in SpotifyMatch.LINKED_STATUSES)
    return {
        "recording__spotify__status": status,
        "spotify_url": spotify_url_for(match.spotify_track_id) if linked else "",
    }


def spotify_readiness(playlist):
    """How ready the playlist is to be pushed to Spotify (GitHub issue #5):
    how many recordings are matched (a copy of the same performance), how
    many have only a stand-in, how many aren't on Spotify, and how many
    are still to be decided (never searched, or a shortlist waiting)."""
    counts = {"matched": 0, "stand_in": 0, "not_on_spotify": 0, "to_do": 0}
    statuses = playlist.items.values_list("recording__spotify__status", flat=True)
    for status in statuses:
        if status in (SpotifyMatch.CONFIRMED, SpotifyMatch.MANUAL):
            counts["matched"] += 1
        elif status == SpotifyMatch.STAND_IN:
            counts["stand_in"] += 1
        elif status == SpotifyMatch.NO_MATCH:
            counts["not_on_spotify"] += 1
        else:
            counts["to_do"] += 1  # unmatched, suggested, or no match row yet
    counts["total"] = sum(counts.values())
    return counts


# The export columns, in order -- one list so the CSV header and every row
# can't drift apart.
EXPORT_COLUMNS = [
    "#", "Song", "Artist (as printed)", "Label", "Catalog #", "Side",
    "Best peak", "Weeks", "First entry", "Last entry", "Discogs", "Spotify",
]


def _export_records(playlist):
    # The rows for both export formats, straight from the database rather
    # than from the HTML table's dicts, so an export carries plain fields
    # (label and catalog # separately, not the combined display string).
    numbers = {r["recording_id"]: r for r in rows(playlist)}
    items = playlist.items.select_related(
        "recording__song", "recording__label", "recording__discogs", "recording__spotify",
    )
    for n, item in enumerate(items, start=1):
        rec = item.recording
        row = numbers.get(rec.id, {})
        match = getattr(rec, "discogs", None)
        yield {
            "#": n,
            "Song": rec.song.title,
            "Artist (as printed)": rec.credit_text,
            "Label": rec.label.name,
            "Catalog #": rec.catalog_number,
            "Side": rec.side,
            "Best peak": row.get("peak_position", ""),
            "Weeks": row.get("weeks_charted", ""),
            "First entry": row["first_entry"].isoformat() if row.get("first_entry") else "",
            "Last entry": row["last_entry"].isoformat() if row.get("last_entry") else "",
            "Discogs": discogs_url_for(match.discogs_release_id, match.discogs_master_id) if match else "",
            # Only a linked match (a stand-in included) -- never a suggestion.
            "Spotify": _spotify_fields(rec)["spotify_url"],
        }


def export_csv(playlist):
    """The playlist as CSV text, one recording per line, in playlist order."""
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=EXPORT_COLUMNS)
    writer.writeheader()
    for record in _export_records(playlist):
        writer.writerow(record)
    return out.getvalue()


def export_text(playlist):
    """The playlist as plain "Artist - Song" lines: the shape most
    "paste a list into a streaming service" tools expect."""
    return "".join(f"{r['Artist (as printed)']} - {r['Song']}\n" for r in _export_records(playlist))
