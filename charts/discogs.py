# charts/discogs.py
#
# Thin Discogs API v2 client, plus the matching logic that turns one of
# our Recording rows into a Discogs release candidate list.
#
# Scope note (CLAUDE.md non-negotiable #2, as amended 2026-08-21): this
# module is the ONLY place in the project that touches the network, and
# nothing in the chart browser or the weekly entry form imports it. If
# Discogs is down, unreachable, or the token is missing, every other part
# of the app carries on exactly as before -- the failure is contained to
# the /discogs/ section, which is opt-in by definition.
#
# Deliberately stdlib-only (urllib, no `requests`) so requirements.txt
# stays at a single dependency (Django). The API surface used here is
# small enough that `requests` wouldn't buy much.

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from django.conf import settings

API_ROOT = "https://api.discogs.com"

# Discogs REQUIRES a User-Agent identifying the application (RFC 1945) --
# generic/absent ones get silently throttled or blocked rather than
# warned. See the API docs' "General Information" section.
USER_AGENT = "BillboardChartDatabase/1.0 +https://github.com/nlopezk/Billie"

# Authenticated requests are allowed 60/minute; unauthenticated 25/minute.
# One request per second keeps us comfortably under the authenticated
# limit even with clock jitter, and is polite regardless. The real
# remaining-quota headers are honoured too (see _throttle/_request).
MIN_SECONDS_BETWEEN_REQUESTS = 1.05

_last_request_at = 0.0


class DiscogsError(Exception):
    """Anything that went wrong talking to Discogs, with a human-readable message."""


def get_token():
    """
    The Discogs personal access token, or None if it hasn't been set up.

    Looked for in two places, in order:
      1. the DISCOGS_TOKEN environment variable
      2. a `.env` file in the project root, holding `DISCOGS_TOKEN=...`

    Both are kept out of version control (.gitignore covers `.env`), so
    the token never ends up in a commit. Returning None rather than
    raising lets callers show a friendly "set your token up" screen
    instead of a traceback.
    """
    token = os.environ.get("DISCOGS_TOKEN", "").strip()
    if token:
        return token

    env_path = Path(settings.BASE_DIR) / ".env"
    if env_path.exists():
        # Deliberately a 6-line parser rather than a python-dotenv
        # dependency -- one KEY=VALUE per line is all this needs to read.
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            if key.strip() == "DISCOGS_TOKEN":
                return value.strip().strip("'\"")
    return None


def _throttle():
    # Simple client-side spacing between calls. The API docs ask
    # applications to throttle locally rather than just retrying into a
    # 429, so this runs before every request regardless of quota state.
    global _last_request_at
    elapsed = time.monotonic() - _last_request_at
    if elapsed < MIN_SECONDS_BETWEEN_REQUESTS:
        time.sleep(MIN_SECONDS_BETWEEN_REQUESTS - elapsed)
    _last_request_at = time.monotonic()


def _request(path, params=None):
    """GET one Discogs endpoint and return the decoded JSON."""
    token = get_token()
    if not token:
        raise DiscogsError(
            "No Discogs token configured. Generate one at "
            "https://www.discogs.com/settings/developers and put it in a `.env` "
            "file in the project root as DISCOGS_TOKEN=your_token_here"
        )

    params = dict(params or {})
    url = f"{API_ROOT}{path}"
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"

    request = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        # Token in the Authorization header rather than the query string,
        # so it can't leak into logs/history via a URL.
        "Authorization": f"Discogs token={token}",
        "Accept": "application/vnd.discogs.v2.discogs+json",
    })

    _throttle()
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            raise DiscogsError("Discogs rate limit hit -- wait a minute and try again.") from exc
        if exc.code == 401:
            raise DiscogsError("Discogs rejected the token (401). Check DISCOGS_TOKEN is correct.") from exc
        if exc.code == 404:
            raise DiscogsError("Discogs has no such release/master (404).") from exc
        raise DiscogsError(f"Discogs returned HTTP {exc.code}.") from exc
    except urllib.error.URLError as exc:
        # No internet, DNS failure, TLS problem, timeout. Surfaced as a
        # normal error so the /discogs/ page can say "couldn't reach
        # Discogs" instead of 500-ing.
        raise DiscogsError(f"Couldn't reach Discogs ({exc.reason}). Check your connection.") from exc
    except json.JSONDecodeError as exc:
        raise DiscogsError("Discogs returned something that wasn't JSON.") from exc


# -----------------------------------------------------------------------------
# Matching
# -----------------------------------------------------------------------------

def normalize_catalog_number(value):
    # "20-2700", "20 2700" and "202700" are the same catalog number as far
    # as matching goes -- Discogs and our workbook don't agree on
    # punctuation. Strip everything that isn't alphanumeric and uppercase
    # what's left, then compare THOSE.
    return re.sub(r"[^A-Z0-9]", "", (value or "").upper())


def normalize_label(value):
    # Discogs often carries the fuller legal label name ("Decca Records")
    # where the magazine printed the short one ("Decca"). Lowercased,
    # punctuation-stripped, with a trailing "records" removed so those
    # two compare equal.
    text = re.sub(r"[^a-z0-9 ]", "", (value or "").lower()).strip()
    return re.sub(r"\s+records$", "", text).strip()


def _score_confidence(recording, result):
    """
    How much to trust one search result, given what we already know about
    the recording from the magazine. See DiscogsMatch.CONFIDENCE_CHOICES.
    """
    from .models import DiscogsMatch

    ours_catno = normalize_catalog_number(recording.catalog_number)
    theirs_catno = normalize_catalog_number(result.get("catno", ""))
    catno_matches = bool(ours_catno) and ours_catno == theirs_catno

    ours_label = normalize_label(recording.label.name)
    # A Discogs result's `label` is a LIST -- a release can carry several
    # (imprint, parent company, distributor). Any one matching is enough.
    labels = result.get("label") or []
    if isinstance(labels, str):
        labels = [labels]
    label_matches = any(normalize_label(name) == ours_label for name in labels)

    if catno_matches and label_matches:
        return DiscogsMatch.EXACT
    if catno_matches:
        return DiscogsMatch.STRONG
    return DiscogsMatch.WEAK


def search_candidates(recording, limit=5):
    """
    Find plausible Discogs releases for one Recording, best first.

    Two-tier, because the two searches have very different reliability:

      1. Catalog number + label. A specific catalog number on a specific
         label is close to unique, so a hit here is trustworthy. This is
         the search that produces EXACT/STRONG confidence.
      2. Title + artist, only if (1) found nothing. Much weaker -- a
         popular song has dozens of pressings and covers, so these come
         back as WEAK and really do need a human to look.

    Returns a list of plain dicts (not model instances) -- the caller
    decides whether to persist them as DiscogsCandidate rows.
    """
    results = []

    # --- tier 1: what's printed on the record label itself ---
    if recording.catalog_number:
        payload = _request("/database/search", {
            "catno": recording.catalog_number,
            "label": recording.label.name,
            "type": "release",
            "per_page": limit,
        })
        results = payload.get("results", []) or []

        # Catalog number alone, if pairing it with the label found
        # nothing -- covers Discogs filing the release under a label name
        # we don't normalize to (a subsidiary, a country-specific imprint).
        if not results:
            payload = _request("/database/search", {
                "catno": recording.catalog_number,
                "type": "release",
                "per_page": limit,
            })
            results = payload.get("results", []) or []

    # --- tier 2: fall back to what the song actually is ---
    if not results:
        payload = _request("/database/search", {
            "release_title": recording.song.title,
            "artist": recording.credit_text,
            "type": "release",
            "per_page": limit,
        })
        results = payload.get("results", []) or []

    candidates = []
    for result in results[:limit]:
        labels = result.get("label") or []
        if isinstance(labels, str):
            labels = [labels]
        formats = result.get("format") or []
        if isinstance(formats, str):
            formats = [formats]

        year = result.get("year")
        try:
            # Discogs sends year as a string, sometimes empty, sometimes
            # a full date. PositiveSmallIntegerField wants an int or None.
            year = int(str(year)[:4]) if year else None
        except (TypeError, ValueError):
            year = None

        candidates.append({
            "discogs_release_id": result.get("id"),
            "discogs_master_id": result.get("master_id") or None,
            "title": (result.get("title") or "")[:300],
            # Search results fold artist into "title" as "Artist - Title";
            # there's no separate artist field at this level, so take the
            # part before the first " - " as a best-effort display value.
            "artist": (result.get("title") or "").split(" - ")[0][:300],
            "label": (labels[0] if labels else "")[:200],
            "catalog_number": (result.get("catno") or "")[:100],
            "year": year,
            "country": (result.get("country") or "")[:100],
            "format": ", ".join(formats)[:200],
            "thumb_url": (result.get("thumb") or "")[:500],
            "confidence": _score_confidence(recording, result),
            "raw_data": result,
        })

    # Best first: EXACT, then STRONG, then WEAK, preserving Discogs' own
    # relevance order within each tier.
    from .models import DiscogsMatch
    tier = {DiscogsMatch.EXACT: 0, DiscogsMatch.STRONG: 1, DiscogsMatch.WEAK: 2}
    candidates.sort(key=lambda c: tier.get(c["confidence"], 3))
    return candidates


# Discogs credit "role" strings vary a lot in the wild. These are matched
# case-insensitively as substrings, so "Written-By" also catches
# "Written-By [Uncredited]" and similar annotations.
SONGWRITER_ROLES = ["written", "composed", "compos", "lyrics", "music by", "songwriter"]
PRODUCER_ROLES = ["produce"]


def _collect_credits(extraartists, role_keywords):
    names = []
    for credit in extraartists or []:
        role = (credit.get("role") or "").lower()
        if any(keyword in role for keyword in role_keywords):
            name = (credit.get("name") or "").strip()
            # Discogs disambiguates same-named artists with a trailing
            # "(2)", "(3)" -- noise for our purposes.
            name = re.sub(r"\s*\(\d+\)$", "", name)
            if name and name not in names:
                names.append(name)
    return names


def _find_track(release, recording):
    """
    The one track on this release that corresponds to our Recording's
    SIDE. Needed because a Discogs release is the whole disc while a
    Recording is one side of it, and songwriter credits are per-track.

    Title match first (most reliable), then position (A/B) as a fallback.
    """
    tracks = release.get("tracklist") or []
    target_title = re.sub(r"[^a-z0-9]", "", (recording.song.title or "").lower())

    for track in tracks:
        track_title = re.sub(r"[^a-z0-9]", "", (track.get("title") or "").lower())
        if track_title and track_title == target_title:
            return track

    # Loose containment, for "Blue Light Boogie (Part I & II)" vs
    # "Blue Light Boogie" and similar printed-vs-catalogued differences.
    for track in tracks:
        track_title = re.sub(r"[^a-z0-9]", "", (track.get("title") or "").lower())
        if track_title and target_title and (track_title in target_title or target_title in track_title):
            return track

    # Fall back to the physical side: Discogs positions singles as
    # "A"/"B" (sometimes "A1"/"B1").
    if recording.side:
        for track in tracks:
            position = (track.get("position") or "").upper()
            if position.startswith(recording.side.upper()[:1]):
                return track
    return None


def _track_artist_names(track, release):
    """
    Who's actually credited on ONE track -- not the release's combined
    credit. Discogs only attaches its own `artists` list to a track when
    that track's artist differs from the release as a whole (exactly the
    case a 2-sided single with a different act on each side needs); when
    a track has none, it's credited the same as the release, so that's
    the fallback.

    This is the whole reason a release-level "Artist" isn't reliable
    enough on its own: a release credited to "Lucky Millinder And His
    Orchestra, Sister Rosetta Tharpe" (Discogs' combined credit for BOTH
    sides of one single) does NOT mean Tharpe is on the side we actually
    have -- she might only be on the OTHER side.
    """
    names = [a.get("name", "") for a in ((track or {}).get("artists") or [])]
    if not names:
        names = [a.get("name", "") for a in (release.get("artists") or [])]
    # Same "(2)"/"(3)" disambiguator stripping as credit names, and
    # de-duplicated while preserving order (a name can legitimately
    # appear twice in Discogs' own list).
    names = [re.sub(r"\s*\(\d+\)$", "", n).strip() for n in names if n]
    return ", ".join(dict.fromkeys(names))


def fetch_release_detail(release_id, recording=None):
    """
    Full detail for one release -- the expensive call, made once per
    CONFIRMED match rather than for every candidate of every recording
    (which would be thousands of calls for data mostly thrown away).

    Returns a dict of the fields DiscogsMatch caches. When `recording` is
    given, the songwriter/producer credits AND the track-level title/
    artist are all taken from that recording's own track within the
    release where identifiable (see _find_track / _track_artist_names),
    falling back to release-level data when a track can't be pinned down.
    matched_title/matched_artist stay the RELEASE's own (the whole disc,
    both sides) -- matched_track_title/matched_track_artist are the ones
    that actually correspond to this one Recording.
    """
    release = _request(f"/releases/{release_id}")

    labels = release.get("labels") or []
    formats = release.get("formats") or []
    artists = release.get("artists") or []

    songwriters = []
    producers = []
    track_title = ""
    track_artist = ""
    if recording is not None:
        track = _find_track(release, recording)
        if track:
            songwriters = _collect_credits(track.get("extraartists"), SONGWRITER_ROLES)
            producers = _collect_credits(track.get("extraartists"), PRODUCER_ROLES)
            track_title = (track.get("title") or "")[:300]
            track_artist = _track_artist_names(track, release)[:300]
    # Release-level credits, used when there's no per-track credit (very
    # common for 78rpm-era singles, where Discogs often only credits the
    # release as a whole).
    if not songwriters:
        songwriters = _collect_credits(release.get("extraartists"), SONGWRITER_ROLES)
    if not producers:
        producers = _collect_credits(release.get("extraartists"), PRODUCER_ROLES)

    year = release.get("year")
    try:
        year = int(str(year)[:4]) if year else None
    except (TypeError, ValueError):
        year = None

    return {
        "discogs_release_id": release.get("id"),
        "discogs_master_id": release.get("master_id") or None,
        "matched_title": (release.get("title") or "")[:300],
        "matched_artist": ", ".join(a.get("name", "") for a in artists)[:300],
        "matched_track_title": track_title,
        "matched_track_artist": track_artist,
        "matched_label": (labels[0].get("name", "") if labels else "")[:200],
        "matched_catalog_number": (labels[0].get("catno", "") if labels else "")[:100],
        "matched_year": year,
        "matched_country": (release.get("country") or "")[:100],
        "matched_format": ", ".join(f.get("name", "") for f in formats)[:200],
        "genres": release.get("genres") or [],
        "styles": release.get("styles") or [],
        "songwriters": ", ".join(songwriters)[:500],
        "producers": ", ".join(producers)[:500],
        "raw_data": release,
    }
