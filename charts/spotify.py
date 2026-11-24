# charts/spotify.py
#
# Thin Spotify Web API client, plus the matching rules that turn one of our
# Recordings into a ranked shortlist of Spotify tracks.
#
# Scope (CLAUDE.md non-negotiable #2, as amended 2026-11-14): network calls
# are fine OUTSIDE chart entry. This module and charts/discogs.py are the
# only two that touch the network, and nothing in the chart browser or the
# weekly entry form imports either. No credentials, no internet, Spotify
# down: only the /spotify/ section notices.
#
# Stdlib-only (urllib), like discogs.py, so requirements.txt stays at Django.
#
# HOW SPOTIFY ACCESS WORKS HERE (the "client credentials" flow):
#   * The owner creates an app at developer.spotify.com/dashboard while
#     logged in to their Premium account (since February 2026 a personal,
#     "Development Mode" app only works while its owner has Premium).
#   * Its Client ID and Client Secret go in the git-ignored .env file.
#   * This module trades those for a short-lived access token (about an
#     hour), cached in memory and renewed when it runs out. No user login
#     is involved, and the token can only read the public catalogue --
#     which is all matching needs. (Creating playlists is GitHub issue #5
#     and will need a real login; nothing here does that.)
#
# LIMITS WORTH KNOWING (February 2026 Development Mode rules):
#   * search returns at most 10 results per request, so a recording gets a
#     few targeted searches rather than one wide one;
#   * album "label" and track "popularity" are no longer returned, so there
#     is no label + catalog # check (unlike Discogs) and no play count --
#     Spotify's own search order stands in for "most listened".

import base64
import json
import math
import os
import re
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from django.conf import settings

API_ROOT = "https://api.spotify.com/v1"
TOKEN_URL = "https://accounts.spotify.com/api/token"

# The most results Spotify returns per search for a Development Mode app.
SEARCH_LIMIT = 10
# How many candidates are kept on a recording's shortlist.
SHORTLIST_SIZE = 8

# Spotify's rate limit is counted over a rolling 30-second window and its
# number isn't published -- only that a Development Mode app's is "much
# lower" than an approved app's. 0.35 s between calls (~85 per window) DID
# hit it (a 429 on 2026-11-19), so: one call per second, ~30 per window. A
# 50-row batch takes about a minute (most rows need a single search).
MIN_SECONDS_BETWEEN_REQUESTS = 1.0

# A 78 rpm side runs roughly 2:20-3:30 (a 12-inch disc up to ~4:30). A
# Spotify track far outside this is probably an edit, a medley or a live
# version, not the side itself.
PLAUSIBLE_MIN_SECONDS = 100
PLAUSIBLE_MAX_SECONDS = 290

# A result on the artist's OWN (non-compilation) album released more than
# this many years after the recording's last chart week is flagged as a
# possible re-recording. Compilations are deliberately not flagged: nearly
# every copy of a 1940s side on Spotify sits on a compilation dated 1990 or
# later, so flagging those would flag almost every correct match.
RERECORDING_YEARS = 5

# Copies of the SAME performance have (nearly) the same length -- the first
# live test (2026-11-19) showed them within a few seconds of each other --
# while a re-recording stands apart: Floyd Tillman's "Slipping Around" on a
# Crazy Cajun release ran 2:06 against 2:43-2:47 for every other copy. So a
# candidate whose length is more than this fraction away from the MEDIAN of
# the agreeing candidates (title + artist both match) is flagged too. This
# catches what the date rule can't: re-recordings sitting on compilations.
LENGTH_TOLERANCE = 0.08
# The median only means "the usual length" when there are enough copies to
# have a majority; with fewer, nothing is flagged on length.
LENGTH_MIN_COPIES = 3

_last_request_at = 0.0
# Held through the sleep so concurrent rows (the queue's batch searches each
# row as its own request on the threaded dev server) queue up one gap apart.
_throttle_lock = threading.Lock()
# Cached access token and when it stops working (time.monotonic seconds).
_token = {"value": None, "expires_at": 0.0}
_token_lock = threading.Lock()


class SpotifyError(Exception):
    """Anything that went wrong talking to Spotify, as a readable message."""


class SpotifyRateLimited(SpotifyError):
    """Spotify answered 429 (or we're still inside the wait it asked for).
    `retry_after` is how many seconds remain before calling again."""

    def __init__(self, retry_after):
        self.retry_after = retry_after
        super().__init__(f"Spotify's rate limit: it asked us to wait {retry_after} s before the next call.")


# When the current Retry-After block ends (time.monotonic seconds); 0 = none.
_blocked_until = 0.0


# ------------------------------------------------------------ configuration

def _env_value(key):
    # One setting from the environment, else from the project's .env file
    # (KEY=VALUE per line, # comments) -- the same two places discogs.py
    # looks for DISCOGS_TOKEN. None when it isn't set anywhere.
    value = os.environ.get(key, "").strip()
    if value:
        return value
    env_path = Path(settings.BASE_DIR) / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, _, raw = line.partition("=")
            if name.strip() == key:
                return raw.strip().strip("'\"") or None
    return None


def get_credentials():
    """(client_id, client_secret), or None when either is missing -- so the
    page can show setup steps instead of failing on every button."""
    client_id, client_secret = _env_value("SPOTIFY_CLIENT_ID"), _env_value("SPOTIFY_CLIENT_SECRET")
    return (client_id, client_secret) if client_id and client_secret else None


def get_market():
    """The country whose catalogue is searched (ISO code, e.g. "CL"). What's
    on Spotify varies by country, and a track that can't play in the
    owner's country is useless for their playlists. Defaults to Chile, the
    owner's choice (2026-11-19)."""
    return (_env_value("SPOTIFY_MARKET") or "CL").upper()


# ------------------------------------------------------------ HTTP

def _access_token():
    # The cached token, or a fresh one when it's missing or about to expire.
    credentials = get_credentials()
    if not credentials:
        raise SpotifyError(
            "No Spotify credentials configured. Create an app at "
            "https://developer.spotify.com/dashboard and put its Client ID and "
            "Client Secret in the project's .env file as SPOTIFY_CLIENT_ID=... "
            "and SPOTIFY_CLIENT_SECRET=..."
        )
    with _token_lock:
        # A minute's margin, so a token can't expire half-way through a batch.
        if _token["value"] and time.monotonic() < _token["expires_at"] - 60:
            return _token["value"]
        basic = base64.b64encode(f"{credentials[0]}:{credentials[1]}".encode()).decode()
        request = urllib.request.Request(
            TOKEN_URL,
            data=urllib.parse.urlencode({"grant_type": "client_credentials"}).encode(),
            headers={
                # Credentials in the Authorization header, never in a URL,
                # so they can't leak into logs or history.
                "Authorization": f"Basic {basic}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code in (400, 401):
                raise SpotifyError(
                    "Spotify rejected the Client ID / Client Secret. Check both in .env, "
                    "and that the app owner's account still has Premium."
                ) from exc
            raise SpotifyError(f"Spotify's login service returned HTTP {exc.code}.") from exc
        except urllib.error.URLError as exc:
            raise SpotifyError(f"Couldn't reach Spotify ({exc.reason}). Check your connection.") from exc
        _token["value"] = data["access_token"]
        _token["expires_at"] = time.monotonic() + int(data.get("expires_in", 3600))
        return _token["value"]


def _throttle():
    global _last_request_at
    with _throttle_lock:
        elapsed = time.monotonic() - _last_request_at
        if elapsed < MIN_SECONDS_BETWEEN_REQUESTS:
            time.sleep(MIN_SECONDS_BETWEEN_REQUESTS - elapsed)
        _last_request_at = time.monotonic()


def _request(path, params=None, _retried=False):
    """GET one Web API endpoint and return the decoded JSON."""
    global _blocked_until  # set on a 429, read on every call
    url = f"{API_ROOT}{path}"
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    # Still inside a Retry-After block from an earlier 429: refuse without
    # touching Spotify at all.
    remaining = _blocked_until - time.monotonic()
    if remaining > 0:
        raise SpotifyRateLimited(math.ceil(remaining))
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {_access_token()}"})
    _throttle()
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 401 and not _retried:
            # The token expired early or was revoked: drop it, try once more.
            _token["value"] = None
            return _request(path, params, _retried=True)
        if exc.code == 429:
            # Spotify says how long to wait (Retry-After, seconds). Remember
            # it for EVERY caller, so nothing calls again before then --
            # calling during the block counts against the window and keeps
            # it going. The caller decides what waiting looks like: the
            # queue's batch pauses and resumes, the command sleeps.
            try:
                wait = max(1, int(exc.headers.get("Retry-After") or 30))
            except ValueError:
                wait = 30
            _blocked_until = time.monotonic() + wait
            raise SpotifyRateLimited(wait) from exc
        if exc.code == 403:
            raise SpotifyError(
                "Spotify refused the request (403). A Development Mode app stops working "
                "when its owner's Premium subscription lapses."
            ) from exc
        if exc.code in (400, 404):
            raise SpotifyError("Spotify has no such track (or the id is malformed).") from exc
        raise SpotifyError(f"Spotify returned HTTP {exc.code}.") from exc
    except urllib.error.URLError as exc:
        raise SpotifyError(f"Couldn't reach Spotify ({exc.reason}). Check your connection.") from exc
    except json.JSONDecodeError as exc:
        raise SpotifyError("Spotify returned something that wasn't JSON.") from exc


def search_tracks(query):
    """One search request: up to SEARCH_LIMIT tracks, in Spotify's order."""
    data = _request("/search", {"q": query, "type": "track", "limit": SEARCH_LIMIT, "market": get_market()})
    return [item for item in (data.get("tracks") or {}).get("items") or [] if item]


def fetch_track(track_id):
    """One track by id, as a snapshot dict (see track_snapshot). Used when a
    link is pasted by hand, to cache what it points at."""
    return track_snapshot(_request(f"/tracks/{track_id}", {"market": get_market()}))


# ------------------------------------------------------------ text rules

# Words that carry no identity in an act's name: "Bob Wills and His Texas
# Playboys" vs "Bob Wills & His Texas Playboys" should agree on what's
# left. Measured on the SHORTER side, so a billing that adds a band name
# ("Louis Jordan" vs "Louis Jordan & His Tympany Five") still agrees.
_ARTIST_STOPWORDS = {
    "the", "and", "his", "her", "their", "with", "featuring", "feat", "ft",
    "orchestra", "orch", "band", "trio", "quartet", "quintet", "sextet",
    "septet", "octet", "boys", "girls", "y", "su", "e", "et",
}

# What a streaming release appends to a title that the printed one never
# had: "- Remastered 2003", "- Single Version", "(feat. X)", "[Live]".
_TITLE_SUFFIX = re.compile(r"\s+-\s+.*$")
_TITLE_BRACKETS = re.compile(r"[\(\[].*?[\)\]]")


def _fold(text):
    # Lowercase, accents stripped ("Ó" -> "o"), "&" read as "and".
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    return text.lower().replace("&", " and ")


def normalize_title(title):
    """A title reduced to what identifies the song: no streaming suffixes,
    no bracketed asides, no punctuation, single spaces."""
    text = _TITLE_SUFFIX.sub("", _fold(title))
    text = _TITLE_BRACKETS.sub(" ", text)
    return " ".join(re.findall(r"[a-z0-9]+", text))


def titles_agree(ours, theirs):
    """Same song title, allowing streaming-era decoration and small
    wording drift. Equal after normalising, either containing the other
    (a "Part 1 & 2" listing), or sharing at least 3 in 4 words of the
    shorter title."""
    a, b = normalize_title(ours), normalize_title(theirs)
    if not a or not b:
        return False
    if a == b or a in b or b in a:
        return True
    words_a, words_b = set(a.split()), set(b.split())
    shorter = min(len(words_a), len(words_b))
    return shorter >= 2 and len(words_a & words_b) / shorter >= 0.75


def _artist_words(name):
    return {w for w in re.findall(r"[a-z0-9]+", _fold(name)) if w not in _ARTIST_STOPWORDS}


def artists_agree(our_names, their_names):
    """Does ANY of our spellings of the act (printed credit, credited
    artists, their aliases, the Discogs billing) agree with what Spotify
    lists? Each side is a list of names; agreement is at least half of
    the shorter name's significant words appearing in the other."""
    theirs = [_artist_words(n) for n in their_names if n]
    theirs.append(set().union(*theirs) if theirs else set())  # all Spotify artists together
    for name in our_names:
        mine = _artist_words(name)
        if not mine:
            continue
        for other in theirs:
            if not other:
                continue
            shorter = min(len(mine), len(other))
            if len(mine & other) / shorter >= 0.5:
                return True
    return False


def duration_plausible(duration_ms):
    # Unknown length isn't evidence against a match.
    if not duration_ms:
        return True
    return PLAUSIBLE_MIN_SECONDS <= duration_ms / 1000 <= PLAUSIBLE_MAX_SECONDS


# ------------------------------------------------------------ the recording side

def recording_profile(recording):
    """Everything matching needs to know about one of our recordings, read
    once: the title(s) and artist spelling(s) to search and compare with,
    and the last year it charted (for the re-recording flag).

    The Discogs billing is used when the recording has a linked Discogs
    match: it often fixes exactly the cases where our printed credit is a
    poor search term ("James - Sinatra" vs "Harry James And His Orchestra,
    Frank Sinatra"). It's an extra spelling, never a replacement, and
    never required."""
    from django.db.models import Max  # local: keep module import light

    titles = [recording.song.title]
    artists = [recording.credit_text]
    primary = ""
    for credit in recording.credits.select_related("artist").prefetch_related("artist__aliases").order_by("credit_order"):
        primary = primary or credit.artist.canonical_name
        artists.append(credit.artist.canonical_name)
        artists.extend(alias.alias_text for alias in credit.artist.aliases.all())
    # No structured credit: the printed one, up to a collaboration's " - ".
    primary = primary or recording.credit_text.split(" - ")[0]

    discogs_title = discogs_artist = ""
    # getattr's default covers "no DiscogsMatch row" (Django raises an
    # AttributeError subclass for a missing reverse one-to-one).
    match = getattr(recording, "discogs", None)
    if match is not None and match.status in ("confirmed", "manual"):
        discogs_title = match.matched_track_title or ""
        discogs_artist = match.matched_track_artist or ""
        if discogs_title:
            titles.append(discogs_title)
        if discogs_artist:
            artists.append(discogs_artist)

    last_week = recording.chart_entries.aggregate(last=Max("week_date"))["last"]
    return {
        "titles": titles,
        "artists": [a for a in artists if a],
        "primary_artist": primary,
        "discogs_title": discogs_title,
        "discogs_artist": discogs_artist.split(",")[0].strip() if discogs_artist else "",
        "last_chart_year": last_week.year if last_week else None,
    }


# ------------------------------------------------------------ the Spotify side

def track_snapshot(item):
    """A Spotify track object reduced to the fields we cache (see
    SpotifyTrackFieldsMixin), plus a trimmed copy of the raw JSON."""
    album = item.get("album") or {}
    raw = {k: v for k, v in item.items() if k not in ("available_markets",)}
    raw["album"] = {k: v for k, v in album.items() if k not in ("images", "available_markets")}
    return {
        "spotify_track_id": item.get("id") or "",
        "track_name": item.get("name") or "",
        "track_artists": ", ".join(a.get("name", "") for a in item.get("artists") or []),
        "album_name": album.get("name") or "",
        "album_type": album.get("album_type") or "",
        "album_artists": ", ".join(a.get("name", "") for a in album.get("artists") or []),
        "release_date": (album.get("release_date") or "")[:10],
        "duration_ms": item.get("duration_ms"),
        "isrc": ((item.get("external_ids") or {}).get("isrc") or "")[:20],
        "raw_data": raw,
    }


def score_track(profile, snapshot):
    """(confidence, possible_rerecording) for one Spotify track against one
    recording. The same rules the audit uses to highlight disagreements,
    so what scores STRONG and what the audit calls agreement can't drift."""
    title_ok = any(titles_agree(t, snapshot["track_name"]) for t in profile["titles"])
    their_artists = [n.strip() for n in snapshot["track_artists"].split(",")]
    artist_ok = artists_agree(profile["artists"], their_artists)
    if title_ok and artist_ok:
        confidence = "strong" if duration_plausible(snapshot["duration_ms"]) else "possible"
    else:
        confidence = "weak"
    return confidence, is_possible_rerecording(profile, snapshot)


def is_possible_rerecording(profile, snapshot):
    """The artist's own (non-compilation) album, released more than
    RERECORDING_YEARS after the recording last charted. See the constant's
    comment for why compilations are left out."""
    year = int(snapshot["release_date"][:4]) if snapshot["release_date"][:4].isdigit() else None
    if year is None or profile["last_chart_year"] is None:
        return False
    return snapshot["album_type"] != "compilation" and year > profile["last_chart_year"] + RERECORDING_YEARS


def _mmss(ms):
    # 126000 -> "2:06", for the flag's reason text.
    seconds = round(ms / 1000)
    return f"{seconds // 60}:{seconds % 60:02d}"


def flag_length_outliers(candidates):
    """Flag every candidate whose length is far from the usual length of
    the copies (see LENGTH_TOLERANCE). Works on the whole result set,
    before ranking and before the shortlist is cut, so the reference is as
    wide as possible. Changes the dicts in place and returns them.

    The reference is the median length of the candidates whose title AND
    artist agree (strong or possible) -- a cover or an unrelated track
    mustn't pull it. Limitation worth knowing: if most results ARE the
    re-recording, the median follows them and the original is the one
    flagged. A flag only warns and keeps a row out of bulk confirm, so a
    human still sees and can pick either."""
    lengths = sorted(
        c["duration_ms"] for c in candidates
        if c["confidence"] in ("strong", "possible") and c.get("duration_ms")
    )
    if len(lengths) < LENGTH_MIN_COPIES:
        return candidates
    middle = len(lengths) // 2
    median = lengths[middle] if len(lengths) % 2 else (lengths[middle - 1] + lengths[middle]) / 2
    for c in candidates:
        if c.get("duration_ms") and abs(c["duration_ms"] - median) / median > LENGTH_TOLERANCE:
            c["possible_rerecording"] = True
            reason = f"{_mmss(c['duration_ms'])}, other copies ~{_mmss(median)}"
            c["flag_reason"] = f"{c['flag_reason']}; {reason}" if c.get("flag_reason") else reason
    return candidates


def _is_own_album(snapshot):
    # An album or single credited to (one of) the track's own artists, as
    # opposed to a various-artists compilation.
    if snapshot["album_type"] == "compilation":
        return False
    album_artists = {a.strip().lower() for a in snapshot["album_artists"].split(",") if a.strip()}
    track_artists = {a.strip().lower() for a in snapshot["track_artists"].split(",") if a.strip()}
    return bool(album_artists & track_artists)


_CONFIDENCE_ORDER = {"strong": 0, "possible": 1, "weak": 2}


def rank_candidates(candidates):
    """Best first. Confidence, then not flagged as a re-recording, then --
    among copies of the same performance -- the owner's order (agreed
    2026-11-19): the artist's own album over a compilation, then Spotify's
    search order (standing in for "most listened", since play counts are no
    longer available), then the earliest release date."""
    def key(c):
        return (
            _CONFIDENCE_ORDER.get(c["confidence"], 3),
            c["possible_rerecording"],
            not _is_own_album(c),
            c["search_order"],
            c["release_date"] or "9999",
        )
    return sorted(candidates, key=key)


def _quote(text):
    # A field filter value without the characters that break Spotify's
    # query syntax (double quotes) or add nothing (brackets).
    return re.sub(r'["\(\)\[\]]', " ", text or "").strip()


def build_queries(profile):
    """The searches to try, in order: strict field filters first, then the
    Discogs spelling (when there is one), then a loose query with no
    filters -- forum reports say field filters sometimes return nothing
    for a track a plain search finds."""
    title = _quote(_TITLE_SUFFIX.sub("", profile["titles"][0]))
    artist = _quote(profile["primary_artist"])
    queries = [f'track:"{title}" artist:"{artist}"']
    if profile["discogs_title"] or profile["discogs_artist"]:
        d_title = _quote(profile["discogs_title"] or title)
        d_artist = _quote(profile["discogs_artist"] or artist)
        discogs_query = f'track:"{d_title}" artist:"{d_artist}"'
        if discogs_query != queries[0]:
            queries.append(discogs_query)
    queries.append(f"{title} {artist}")
    return queries


def search_candidates(recording, search=None):
    """The ranked shortlist for one recording: a list of candidate dicts
    (SpotifyCandidate fields plus raw_data, best first), empty when
    nothing relates at all.

    Runs the strict query, then the Discogs one, then the loose one,
    stopping as soon as something STRONG has turned up -- 1 to 3 requests
    per recording, usually 1.
    `search` is the one-query function (search_tracks by default), so the
    rules can be tested with canned results."""
    search = search or search_tracks
    profile = recording_profile(recording)
    queries = build_queries(profile)
    seen, found = set(), []
    order = 0
    for query in queries:
        # Once a STRONG match is in hand, further queries only cost calls:
        # the first search already returned up to 10 copies to rank.
        if any(c["confidence"] == "strong" for c in found):
            break
        for item in search(query):
            snapshot = track_snapshot(item)
            if not snapshot["spotify_track_id"] or snapshot["spotify_track_id"] in seen:
                continue
            seen.add(snapshot["spotify_track_id"])
            confidence, flagged = score_track(profile, snapshot)
            # A track that shares neither title nor artist isn't worth
            # showing; "weak" means at least one of the two agreed.
            title_ok = any(titles_agree(t, snapshot["track_name"]) for t in profile["titles"])
            artist_ok = artists_agree(profile["artists"], [n.strip() for n in snapshot["track_artists"].split(",")])
            if not (title_ok or artist_ok):
                continue
            year = snapshot["release_date"][:4]
            found.append({
                **snapshot, "confidence": confidence, "possible_rerecording": flagged,
                "flag_reason": f"released {year}" if flagged else "", "search_order": order,
            })
            order += 1
    # The second, independent re-recording check: a length far from the
    # other copies'. Run on everything found, then rank, then cut.
    flag_length_outliers(found)
    return rank_candidates(found)[:SHORTLIST_SIZE]


# ------------------------------------------------------------ pasted links

_ID = re.compile(r"^[A-Za-z0-9]{22}$")


def parse_track_reference(raw):
    """A Spotify track id from whatever was pasted: an open.spotify.com
    link (with or without ?si=... or an /intl-xx/ prefix), a spotify:track:
    URI, or the bare 22-character id. None when it's none of those."""
    raw = (raw or "").strip()
    match = re.search(r"open\.spotify\.com/(?:intl-[a-z-]+/)?track/([A-Za-z0-9]{22})", raw) \
        or re.search(r"spotify:track:([A-Za-z0-9]{22})", raw)
    if match:
        return match.group(1)
    return raw if _ID.match(raw) else None


# =============================================================================
# Acting AS the owner: creating and updating playlists (added 2026-11-23)
# =============================================================================
#
# Everything above reads the public catalogue with the app's own token.
# Creating a playlist in the owner's account needs the owner's permission,
# given once through Spotify's "Authorization Code" flow:
#
#   1. /spotify/connect/ sends the browser to Spotify's consent page
#      (authorize_url), with a random `state` kept in the session.
#   2. The owner clicks Agree; Spotify sends the browser back to
#      /spotify/callback/?code=...&state=...
#   3. exchange_code() trades that one-time code for an access token (about
#      an hour) and a REFRESH token (long-lived), saved to
#      .spotify_token.json next to .env -- git-ignored, and deliberately NOT
#      in billboard.db, which is pushed to GitHub.
#   4. Later calls use the access token, renewing it from the refresh token
#      when it runs out (_user_token). "Disconnect" deletes the file.
#
# Spotify's redirect rules (2025): a plain-http return address is accepted
# only on a loopback IP -- http://127.0.0.1:8000/spotify/callback/, never
# "localhost" -- and it must be registered, exactly, in the app's settings.

AUTHORIZE_URL = "https://accounts.spotify.com/authorize"
# Only what pushing playlists needs: create and change the owner's
# playlists, private or public. Nothing that reads listening history.
USER_SCOPES = "playlist-modify-private playlist-modify-public"
# Spotify accepts at most 100 track URIs per add/replace request.
PLAYLIST_CHUNK = 100


class SpotifyNotConnected(SpotifyError):
    """No owner login on file (or it was revoked): press Connect Spotify."""


class SpotifyNotFound(SpotifyError):
    """The thing asked for doesn't exist -- e.g. a playlist deleted in the
    Spotify app. The playlist push offers to create a new one."""


def token_path():
    # Next to .env, at the project root. Listed in .gitignore.
    return Path(settings.BASE_DIR) / ".spotify_token.json"


def is_connected():
    return token_path().exists()


def disconnect():
    """Forget the owner's login (delete the token file). Spotify-side, the
    app's access can also be removed at spotify.com/account/apps."""
    try:
        token_path().unlink()
    except FileNotFoundError:
        pass


def authorize_url(state, redirect_uri):
    """Where to send the browser for the owner's consent."""
    credentials = get_credentials()
    if not credentials:
        raise SpotifyError("No Spotify credentials configured (SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET in .env).")
    return AUTHORIZE_URL + "?" + urllib.parse.urlencode({
        "client_id": credentials[0],
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": USER_SCOPES,
        # Echoed back by Spotify; the callback refuses a mismatch, so a
        # forged link can't attach someone else's account.
        "state": state,
    })


def _token_request(form):
    # POST to Spotify's token endpoint with the app's own credentials, for
    # both the first code exchange and later refreshes.
    credentials = get_credentials()
    if not credentials:
        raise SpotifyError("No Spotify credentials configured.")
    basic = base64.b64encode(f"{credentials[0]}:{credentials[1]}".encode()).decode()
    request = urllib.request.Request(
        TOKEN_URL, data=urllib.parse.urlencode(form).encode(),
        headers={"Authorization": f"Basic {basic}", "Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 400:
            # invalid_grant: the code was used/expired, or the refresh token
            # was revoked (the owner removed the app at spotify.com).
            raise SpotifyNotConnected("Spotify no longer accepts this login -- press Connect Spotify again.") from exc
        raise SpotifyError(f"Spotify's login service returned HTTP {exc.code}.") from exc
    except urllib.error.URLError as exc:
        raise SpotifyError(f"Couldn't reach Spotify ({exc.reason}). Check your connection.") from exc


def _save_token(data, previous_refresh=None):
    # Write the token file. A refresh response may omit refresh_token, in
    # which case the old one stays valid and is kept.
    # Keep the stored account name across refreshes.
    account = None
    if token_path().exists():
        try:
            account = json.loads(token_path().read_text(encoding="utf-8")).get("account")
        except (OSError, ValueError):
            account = None
    token = {
        "account": account,
        "access_token": data["access_token"],
        "refresh_token": data.get("refresh_token") or previous_refresh,
        # Wall-clock (epoch) time, since the file outlives the process.
        "expires_at": time.time() + int(data.get("expires_in", 3600)),
        "scope": data.get("scope", ""),
    }
    token_path().write_text(json.dumps(token, indent=2), encoding="utf-8")
    return token


def exchange_code(code, redirect_uri):
    """Step 3 above: the one-time code -> a saved token, plus the account's
    name (one /me call, once), so the Spotify page can say who's connected
    without calling Spotify on every page load."""
    _save_token(_token_request({"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri}))
    account = current_user()
    token = json.loads(token_path().read_text(encoding="utf-8"))
    token["account"] = account
    token_path().write_text(json.dumps(token, indent=2), encoding="utf-8")
    return account


def connected_account():
    """{"id", "display_name"} of the connected account, from the token file
    (no API call), or None when not connected."""
    if not token_path().exists():
        return None
    try:
        token = json.loads(token_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return token.get("account") or {"id": "", "display_name": "(connected)"}


def _user_token():
    # A valid access token for the owner, refreshed when it's (nearly) out.
    path = token_path()
    if not path.exists():
        raise SpotifyNotConnected("Spotify isn't connected -- press Connect Spotify on the Spotify page.")
    token = json.loads(path.read_text(encoding="utf-8"))
    if time.time() < token.get("expires_at", 0) - 60:
        return token["access_token"]
    if not token.get("refresh_token"):
        raise SpotifyNotConnected("The Spotify login has expired -- press Connect Spotify again.")
    fresh = _token_request({"grant_type": "refresh_token", "refresh_token": token["refresh_token"]})
    return _save_token(fresh, previous_refresh=token["refresh_token"])["access_token"]


def _user_request(method, path, body=None, _retried=False):
    """One Web API call AS the owner (GET/POST/PUT, JSON body). Shares the
    pacing and the 429 block with the catalogue calls above."""
    global _blocked_until
    remaining = _blocked_until - time.monotonic()
    if remaining > 0:
        raise SpotifyRateLimited(math.ceil(remaining))
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(f"{API_ROOT}{path}", data=data, method=method, headers={
        "Authorization": f"Bearer {_user_token()}",
        "Content-Type": "application/json",
    })
    _throttle()
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as exc:
        if exc.code == 401 and not _retried:
            # The access token was rejected early: force a refresh, once.
            token = json.loads(token_path().read_text(encoding="utf-8"))
            token["expires_at"] = 0
            token_path().write_text(json.dumps(token), encoding="utf-8")
            return _user_request(method, path, body, _retried=True)
        if exc.code == 401:
            raise SpotifyNotConnected("Spotify rejected the login -- press Connect Spotify again.") from exc
        if exc.code == 429:
            try:
                wait = max(1, int(exc.headers.get("Retry-After") or 30))
            except ValueError:
                wait = 30
            _blocked_until = time.monotonic() + wait
            raise SpotifyRateLimited(wait) from exc
        if exc.code == 403:
            raise SpotifyError(
                "Spotify refused (403). For a Development Mode app, add your Spotify account under "
                "User Management in the app's dashboard; also check the owner still has Premium."
            ) from exc
        if exc.code == 404:
            raise SpotifyNotFound("Spotify says that no longer exists (404).") from exc
        raise SpotifyError(f"Spotify returned HTTP {exc.code}.") from exc
    except urllib.error.URLError as exc:
        raise SpotifyError(f"Couldn't reach Spotify ({exc.reason}). Check your connection.") from exc


def current_user():
    """The connected account: {"id", "display_name"} -- shown on /spotify/."""
    me = _user_request("GET", "/me")
    return {"id": me.get("id", ""), "display_name": me.get("display_name") or me.get("id", "")}


def create_playlist(name, description, public=False):
    """A new, empty playlist in the owner's account. Returns its id."""
    created = _user_request("POST", "/me/playlists", {"name": name, "description": description, "public": public})
    return created["id"]


def update_playlist_details(playlist_id, name, description):
    """Refresh name and description (a local rename carries over)."""
    _user_request("PUT", f"/playlists/{playlist_id}", {"name": name, "description": description})


def replace_playlist_tracks(playlist_id, track_ids):
    """Make the playlist hold exactly these tracks, in this order. The
    first 100 REPLACE whatever is there; the rest are appended 100 at a
    time (Spotify's per-request cap)."""
    uris = [f"spotify:track:{tid}" for tid in track_ids]
    _user_request("PUT", f"/playlists/{playlist_id}/items", {"uris": uris[:PLAYLIST_CHUNK]})
    for start in range(PLAYLIST_CHUNK, len(uris), PLAYLIST_CHUNK):
        _user_request("POST", f"/playlists/{playlist_id}/items", {"uris": uris[start:start + PLAYLIST_CHUNK]})


def playlist_url(playlist_id):
    # Plain outbound link; not a request.
    return f"https://open.spotify.com/playlist/{playlist_id}" if playlist_id else ""
