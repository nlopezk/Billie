"""
charts/spotify_workflow.py -- every step a SpotifyMatch can go through.

The same shape as discogs_workflow.py, on purpose:

    unmatched --search--> suggested (a shortlist) --confirm--> confirmed
              \\--search finds nothing--> no_match            \\--as stand-in--> stand_in
    any row --link_manually (a pasted link)--> manual
    any row --set_status--> no_match ("not on Spotify") / unmatched (a full reset)

Every caller -- the /spotify/ row buttons, the page's bulk confirm, the
spotify_match command -- uses these functions, so the rules exist once.

THE SEAM: the two steps that talk to Spotify (search, link_manually) take a
`client` argument -- any object with `search_candidates(recording)` and
`fetch_track(track_id)` that raises `charts.spotify.SpotifyError` on
failure. In the app it's the real `charts.spotify` module; in tests a small
fake with canned answers, so no test touches the network.

Confirming from the shortlist needs NO network call (unlike Discogs, there's
no second, per-track fetch): the candidate already holds the full snapshot.

Nothing is ever confirmed on the machine's judgement alone: search only
produces a shortlist, and the bulk confirm only takes a row whose top-ranked
candidate is STRONG and not flagged as a possible re-recording.
"""
from collections import defaultdict

from django.db import transaction
from django.db.models import Count
from django.utils import timezone

from . import spotify as spotify_api
from .models import Recording, SpotifyCandidate, SpotifyMatch

# The snapshot fields a match and a candidate share (SpotifyTrackFieldsMixin).
SNAPSHOT_FIELDS = [
    "spotify_track_id", "track_name", "track_artists", "album_name", "album_type",
    "album_artists", "release_date", "duration_ms", "isrc",
]


# ------------------------------------------------------------ setup / progress

def ensure_matches():
    """Give every Recording a SpotifyMatch row (status=unmatched) if it has
    none. Run when the queue opens, so recordings added later through the
    entry form are picked up without the entry path knowing about Spotify."""
    missing = Recording.objects.filter(spotify__isnull=True)
    created = SpotifyMatch.objects.bulk_create(
        [SpotifyMatch(recording=r) for r in missing], ignore_conflicts=True,
    )
    return len(created)


def progress(matches=None):
    """Counts per status for the queue's progress line -- over every match,
    or over the given queryset (the queue's playlist filter)."""
    matches = SpotifyMatch.objects.all() if matches is None else matches
    counts = dict(matches.values_list("status").annotate(n=Count("id")).values_list("status", "n"))
    total = sum(counts.values())
    settled = sum(counts.get(s, 0) for s in SpotifyMatch.SETTLED_STATUSES)
    return {
        "total": total, "settled": settled, "counts": counts,
        "percent": round(100 * settled / total) if total else 0,
    }


# ------------------------------------------------------------ searching

def search(match, client=spotify_api):
    """Search Spotify for `match`'s recording and store the shortlist. 1-3
    API calls. Raises SpotifyError on failure, leaving the match as it was."""
    store_shortlist(match, client.search_candidates(match.recording))


def store_shortlist(match, candidates):
    """Replace the shortlist with freshly-searched candidate dicts (best
    first). An empty search is a real outcome -- "not on Spotify" -- so the
    row leaves the to-do list; it can be searched again later."""
    with transaction.atomic():
        match.candidates.all().delete()
        SpotifyCandidate.objects.bulk_create([
            SpotifyCandidate(
                match=match, rank=rank, confidence=c["confidence"],
                possible_rerecording=c["possible_rerecording"],
                flag_reason=c.get("flag_reason", ""),
                **{f: c.get(f) if f == "duration_ms" else (c.get(f) or "") for f in SNAPSHOT_FIELDS},
            )
            for rank, c in enumerate(candidates)
        ])
        match.searched_at = timezone.now()
        match.status = SpotifyMatch.SUGGESTED if candidates else SpotifyMatch.NO_MATCH
        match.save()


def bulk_confirmable_candidate(match):
    """The candidate the bulk "confirm strong matches" may take, or None:
    the TOP-ranked one, only if it is STRONG and not flagged as a possible
    re-recording. Ranking already put the preferred copy first (own album,
    then search order, then earliest), so taking the top one is taking the
    owner's choice among copies of the same performance. Reads
    match.candidates.all(), so a prefetch avoids a query per row."""
    if match.status != SpotifyMatch.SUGGESTED:
        return None
    candidates = sorted(match.candidates.all(), key=lambda c: c.rank)
    if not candidates:
        return None
    top = candidates[0]
    if top.confidence == SpotifyCandidate.STRONG and not top.possible_rerecording:
        return top
    return None


# ------------------------------------------------------------ linking

def _clear(match):
    # Forget everything cached about the previous link -- every snapshot
    # field, the raw JSON and the shortlist -- so a new link can never show
    # the old track's details (the bug discogs_workflow._clear_cached fixed).
    for field in SNAPSHOT_FIELDS:
        setattr(match, field, None if field == "duration_ms" else "")
    match.raw_data = None
    match.candidates.all().delete()


@transaction.atomic
def confirm(match, candidate, *, stand_in=False):
    """Take a shortlist candidate as the match -- or, with stand_in=True, as
    a stand-in (a same-artist re-recording; GLOSSARY.md). No network call."""
    snapshot = {f: getattr(candidate, f) for f in SNAPSHOT_FIELDS}
    _clear(match)
    for field, value in snapshot.items():
        setattr(match, field, value)
    match.status = SpotifyMatch.STAND_IN if stand_in else SpotifyMatch.CONFIRMED
    match.linked_at = timezone.now()
    match.save()


def link_manually(match, raw_reference, *, stand_in=False, client=spotify_api):
    """Link a pasted Spotify link / URI / id. Fetches the track (one call)
    to cache what it points at. Returns an error message, or None.

    The fetch happens BEFORE anything is changed, so a typo or a dead
    connection leaves the row exactly as it was."""
    track_id = spotify_api.parse_track_reference(raw_reference)
    if not track_id:
        return "That isn't a Spotify track link, URI or id."
    try:
        snapshot = client.fetch_track(track_id)
    except spotify_api.SpotifyError as exc:
        return str(exc)
    with transaction.atomic():
        _clear(match)
        for field in SNAPSHOT_FIELDS:
            setattr(match, field, snapshot.get(field) if field == "duration_ms" else (snapshot.get(field) or ""))
        match.raw_data = snapshot.get("raw_data")
        match.status = SpotifyMatch.STAND_IN if stand_in else SpotifyMatch.MANUAL
        match.linked_at = timezone.now()
        match.save()
    return None


def set_status(match, status):
    """"Not on Spotify" (no_match) or a full reset (unmatched). Either one
    drops the link and its cache; a reset also forgets the search."""
    if status not in (SpotifyMatch.NO_MATCH, SpotifyMatch.UNMATCHED):
        return
    with transaction.atomic():
        _clear(match)
        match.status = status
        match.linked_at = None
        if status == SpotifyMatch.UNMATCHED:
            match.searched_at = None
        match.save()


def confirm_strong_batch(matches):
    """The bulk button: confirm every match whose top candidate is strong
    and unflagged (see bulk_confirmable_candidate). Returns how many."""
    confirmed = 0
    for match in matches:
        candidate = bulk_confirmable_candidate(match)
        if candidate is not None:
            confirm(match, candidate)
            confirmed += 1
    return confirmed


# ------------------------------------------------------------ audit

def _reissue_groups(recording_ids):
    # Split recordings into groups joined by Recording.reissue_of (the
    # "same performance, new label or catalog #" link), with a small
    # union-find so a chain A -> B -> C lands in one group.
    parent = {rid: rid for rid in recording_ids}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    pairs = Recording.objects.filter(id__in=recording_ids, reissue_of_id__in=recording_ids)
    for rid, original in pairs.values_list("id", "reissue_of_id"):
        parent[find(rid)] = find(original)
    return {find(rid) for rid in recording_ids}


def shared_tracks_not_reissues():
    """Spotify tracks linked to more than one of our recordings where those
    recordings are NOT all known reissues of each other. Usually a mistake
    (Lionel Hampton's 1940 and 1942 "Flying Home" are two performances and
    can't share a track), but not always, so this only lists them."""
    shared_ids = (
        SpotifyMatch.objects.filter(status__in=SpotifyMatch.LINKED_STATUSES)
        .exclude(spotify_track_id="")
        .values("spotify_track_id").annotate(n=Count("id")).filter(n__gt=1)
        .values_list("spotify_track_id", flat=True)
    )
    by_track = defaultdict(list)
    for match in (
        SpotifyMatch.objects.filter(spotify_track_id__in=list(shared_ids), status__in=SpotifyMatch.LINKED_STATUSES)
        .select_related("recording__song", "recording__label")
        .order_by("spotify_track_id", "recording__song__title")
    ):
        by_track[match.spotify_track_id].append(match)
    groups = []
    for track_id, matches in by_track.items():
        if len(_reissue_groups([m.recording_id for m in matches])) > 1:
            groups.append({"track_id": track_id, "matches": matches})
    return groups


def audit_row(match):
    """One linked match next to our data, with what disagrees -- using the
    same rules as scoring (charts/spotify.py), so the audit can't call a
    STRONG match a disagreement or the other way round."""
    profile = spotify_api.recording_profile(match.recording)
    their_artists = [n.strip() for n in match.track_artists.split(",")]
    return {
        "match": match,
        "title_ok": any(spotify_api.titles_agree(t, match.track_name) for t in profile["titles"]),
        "artist_ok": spotify_api.artists_agree(profile["artists"], their_artists),
        "length_ok": spotify_api.duration_plausible(match.duration_ms),
        "late": spotify_api.is_possible_rerecording(
            profile, {"release_date": match.release_date, "album_type": match.album_type},
        ),
        "last_chart_year": profile["last_chart_year"],
    }
