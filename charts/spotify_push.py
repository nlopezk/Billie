"""
charts/spotify_push.py -- sending a local playlist to the owner's Spotify.

Agreed with the owner (2026-11-23):

* WHAT IS SENT (plan): each recording in playlist order -- its Spotify
  match, then its parts (Part 2, Part 3...) straight after it. Stand-ins go
  in (chosen deliberately). Alternates stay out (reference only). A
  recording that isn't matched is skipped and LISTED, never silently lost.
* KEPT LINKED: the first push creates a private playlist and remembers its
  id; every later push ("Update on Spotify") rebuilds that same playlist to
  match the local one exactly -- tracks, order, name, description. The
  local playlist is the master copy: edits made inside Spotify are replaced.
* NEVER DELETES anything in the owner's Spotify account. If the linked
  playlist was deleted on Spotify's side, the push stops and offers to
  create a new one (create_new=True) -- it never does that silently.
* UP TO DATE?: each push stores the exact track ids sent; changes() compares
  them with plan() -- no Spotify call -- for "3 changes since last push".

The one step that talks to Spotify (push) takes a `client` argument -- the
real charts.spotify module by default, a fake in tests.
"""
from django.utils import timezone

from . import spotify as spotify_api
from .models import SpotifyExtraTrack, SpotifyMatch

# Appended to every pushed playlist's description, so it's clear on Spotify
# where the playlist came from.
DESCRIPTION_SUFFIX = "Created from the Billboard chart database"
# Spotify's limit on a playlist description.
DESCRIPTION_MAX = 300

# Why a recording isn't in the push, in words for the confirmation page.
_MISSING_REASON = {
    SpotifyMatch.NO_MATCH: "not on Spotify",
    SpotifyMatch.SUGGESTED: "suggestions waiting for review",
    SpotifyMatch.UNMATCHED: "not searched yet",
    None: "not searched yet",
}


class PlaylistGone(spotify_api.SpotifyError):
    """The linked Spotify playlist no longer exists (deleted in Spotify)."""


def plan(playlist):
    """What a push would send now: {"tracks": [...], "missing": [...]}.

    tracks -- dicts {track_id, recording, label} in send order, where label
              is "", "Part 2", "Part 3"... or "stand-in";
    missing -- dicts {recording, reason} for recordings left out.
    """
    tracks, missing = [], []
    items = playlist.items.select_related(
        "recording__song", "recording__label", "recording__spotify",
    ).prefetch_related("recording__spotify__extra_tracks")
    for item in items:
        recording = item.recording
        match = getattr(recording, "spotify", None)  # None when never set up
        if match is None or not match.is_linked:
            missing.append({
                "recording": recording,
                "reason": _MISSING_REASON.get(match.status if match else None, "not matched"),
            })
            continue
        label = "stand-in" if match.status == SpotifyMatch.STAND_IN else ""
        tracks.append({"track_id": match.spotify_track_id, "recording": recording, "label": label})
        # Parts follow their main track, in order. Alternates are skipped.
        parts = sorted(
            (x for x in match.extra_tracks.all() if x.kind == SpotifyExtraTrack.PART),
            key=lambda x: x.order,
        )
        for part in parts:
            tracks.append({"track_id": part.spotify_track_id, "recording": recording, "label": part.label})
    return {"tracks": tracks, "missing": missing}


def track_ids(the_plan):
    return [t["track_id"] for t in the_plan["tracks"]]


def changes(playlist, the_plan=None):
    """How the Spotify copy differs from what would be sent now, from the
    stored record of the last push alone. None if never pushed; otherwise
    {"added", "removed", "reordered", "up_to_date"}."""
    if not playlist.spotify_playlist_id:
        return None
    now = track_ids(the_plan or plan(playlist))
    before = list(playlist.spotify_pushed_tracks or [])
    added = len([t for t in now if t not in before])
    removed = len([t for t in before if t not in now])
    # Same tracks, different order -- e.g. after using the up/down arrows.
    common_now = [t for t in now if t in before]
    common_before = [t for t in before if t in now]
    reordered = common_now != common_before
    return {
        "added": added, "removed": removed, "reordered": reordered,
        "up_to_date": not (added or removed or reordered),
    }


def description_for(playlist):
    """The local description (if any), then where the playlist came from,
    cut to Spotify's limit."""
    text = f"{playlist.description.strip()} · {DESCRIPTION_SUFFIX}" if playlist.description.strip() else DESCRIPTION_SUFFIX
    return text[:DESCRIPTION_MAX]


def push(playlist, *, create_new=False, client=spotify_api):
    """Create (first time, or create_new=True) or rebuild the linked Spotify
    playlist. Returns {"created": bool, "sent": n, "missing": n}.

    Raises PlaylistGone when the linked playlist no longer exists -- the page
    then asks before creating a new one -- and SpotifyError for anything
    else (not connected, rate limit, offline). On any failure the local
    record of the last push is left as it was."""
    the_plan = plan(playlist)
    ids = track_ids(the_plan)
    description = description_for(playlist)
    created = False
    if playlist.spotify_playlist_id and not create_new:
        try:
            client.update_playlist_details(playlist.spotify_playlist_id, playlist.name, description)
        except spotify_api.SpotifyNotFound as exc:
            raise PlaylistGone("That playlist no longer exists on Spotify.") from exc
        playlist_id = playlist.spotify_playlist_id
    else:
        # Private, as agreed. Never public without being asked.
        playlist_id = client.create_playlist(playlist.name, description, public=False)
        created = True
    client.replace_playlist_tracks(playlist_id, ids)

    playlist.spotify_playlist_id = playlist_id
    playlist.spotify_pushed_at = timezone.now()
    playlist.spotify_pushed_tracks = ids
    playlist.save(update_fields=["spotify_playlist_id", "spotify_pushed_at", "spotify_pushed_tracks"])
    return {"created": created, "sent": len(ids), "missing": len(the_plan["missing"])}
