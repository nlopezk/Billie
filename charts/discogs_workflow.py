"""
charts/discogs_workflow.py -- every step a DiscogsMatch can go through.

A recording's link to Discogs moves through a small workflow:

    unmatched --search--> suggested (a shortlist) --confirm--> confirmed
              \\--search finds nothing--> no_match           \\
    any row --link_manually--> manual                          --> enrich
    any row --set_status--> rejected / no_match / unmatched (a full reset)

Each step used to be written out by hand wherever it happened -- the
/discogs/ row buttons, the page's bulk actions, the discogs_match command,
the entry form's typed Discogs link -- and the copies had started to
disagree. Now each step is one function here, and every caller uses it.

THE SEAM: the steps that talk to Discogs take a `client` argument -- any
object with `search_candidates(recording)` and
`fetch_release_detail(release_id, recording)` that raises
`charts.discogs.DiscogsError` on failure. In the app it's the real
`charts.discogs` module (the default); in tests it's a small fake that
returns canned answers, so no test ever touches the network or depends on
the names of charts.discogs's private helpers.

Steps that DON'T talk to Discogs (link_manually, set_status, store_shortlist)
take no client at all -- which is what lets the entry form record a typed
Discogs link with the network unplugged (non-negotiable #2).

Nothing here ever confirms a match on its own judgement: search only ever
produces a shortlist, and the bulk confirm only takes a candidate when it is
the single EXACT one (see sole_exact_candidate).
"""
import re

from django.db import transaction
from django.db.models import Count
from django.utils import timezone

from . import discogs as discogs_api
from .models import DiscogsCandidate, DiscogsMatch, Recording


# ------------------------------------------------------------ setup

def ensure_matches():
    """Give every Recording a DiscogsMatch row (status=unmatched) if it
    doesn't have one yet. Returns how many were created.

    Run whenever the queue page opens (and by the discogs_match command)
    rather than once in a migration, so it self-heals: recordings created
    later through the entry form are picked up with no bookkeeping on the
    entry path.
    """
    missing = Recording.objects.filter(discogs__isnull=True)
    created = DiscogsMatch.objects.bulk_create(
        [DiscogsMatch(recording=recording) for recording in missing],
        # Belt-and-braces against a double-click racing itself; the
        # OneToOne already makes a duplicate impossible at the DB level.
        ignore_conflicts=True,
    )
    return len(created)


# ------------------------------------------------------------ progress

def progress():
    """Counts for the queue's progress line: per status, how many are
    settled, and how many linked rows still need their details. One
    grouped query rather than six separate .count() calls."""
    counts = dict(
        DiscogsMatch.objects.values_list("status").annotate(n=Count("id")).values_list("status", "n")
    )
    total = sum(counts.values())
    # Linked but not yet enriched -- see DiscogsMatch.needs_detail_q.
    needs_detail = DiscogsMatch.objects.filter(DiscogsMatch.needs_detail_q()).count()
    # A row still waiting on its detail fetch is NOT done, whatever its
    # status says. Counting those as settled is what let entry-form links
    # sit un-enriched: the progress bar called them finished.
    settled = sum(counts.get(status, 0) for status in DiscogsMatch.SETTLED_STATUSES) - needs_detail
    return {
        "total": total,
        "settled": settled,
        "percent": round(100 * settled / total) if total else 0,
        "counts": counts,
        "needs_detail": needs_detail,
    }


# ------------------------------------------------------------ searching

def search(match, client=discogs_api):
    """Search Discogs for `match`'s recording and store the shortlist.

    One or two API calls. Raises DiscogsError on failure, leaving the
    match untouched -- callers show it as a message, never a 500.
    """
    store_shortlist(match, client.search_candidates(match.recording))


def store_shortlist(match, candidates):
    """Replace `match`'s shortlist with freshly-searched `candidates`
    (dicts of DiscogsCandidate fields, best first)."""
    with transaction.atomic():
        match.candidates.all().delete()
        for rank, data in enumerate(candidates):
            DiscogsCandidate.objects.create(match=match, rank=rank, **data)
        match.searched_at = timezone.now()
        # NO_MATCH rather than SUGGESTED when the search came back empty:
        # that's a real, informative outcome ("Discogs doesn't have this
        # 78"), not a failure, and it means the row stops cluttering the
        # to-do queue. A human can still re-search it later.
        match.status = DiscogsMatch.SUGGESTED if candidates else DiscogsMatch.NO_MATCH
        match.save()


def sole_exact_candidate(match):
    """The ONE candidate scored EXACT, or None if there are none or several.

    Several EXACT candidates is a genuine tie -- the same label + catalog #
    matching more than one distinct Discogs release (a US and a Canadian
    pressing, an original and a same-numbered reissue) -- and the bulk
    confirm refuses to guess between them. The queue page's "confirm exact
    matches" button counts with this too, so its number is what the action
    will really do.
    """
    exact = match.exact_candidates()
    return exact[0] if len(exact) == 1 else None


# ------------------------------------------------------------ linking

def _clear_cached(match):
    # Forget everything cached about whatever release `match` pointed at:
    # summary, track, songwriters/producers, genres/styles, raw JSON. Done
    # by EVERY new link (and by a reset), so details from the previous
    # release can never survive next to a new id looking like they belong
    # to it -- e.g. a re-link whose detail fetch then fails, or a link to a
    # master, which has no release detail to fetch. Blank until the new
    # fetch fills it beats plausible-looking but wrong. Clearing raw_data
    # is also what marks the row as needing its details (needs_detail).
    match.confidence = ""
    match.matched_title = match.matched_artist = match.matched_label = ""
    match.matched_track_title = match.matched_track_artist = ""
    match.matched_catalog_number = match.matched_country = match.matched_format = ""
    match.matched_year = None
    match.songwriters = match.producers = ""
    match.genres = match.styles = []
    match.raw_data = None


def confirm(match, candidate, client=discogs_api):
    """A human picked `candidate`: link to it, then fetch its details.

    Returns None, or an error string if the detail fetch failed -- the
    confirm stands either way (it's saved before the fetch), and the row
    keeps needs_detail so enrich() can finish it later.
    """
    with transaction.atomic():
        _clear_cached(match)
        # The candidate's summary is already cached -- copying it is free,
        # so it happens regardless of how the detail fetch below goes.
        match.discogs_release_id = candidate.discogs_release_id
        match.discogs_master_id = candidate.discogs_master_id
        match.matched_title = candidate.title
        match.matched_artist = candidate.artist
        match.matched_label = candidate.label
        match.matched_catalog_number = candidate.catalog_number
        match.matched_year = candidate.year
        match.matched_country = candidate.country
        match.matched_format = candidate.format
        match.confidence = candidate.confidence
        match.status = DiscogsMatch.CONFIRMED
        match.linked_at = timezone.now()
        match.save()
        match.candidates.all().delete()
    # Outside the transaction: a network failure can never roll back a
    # decision that was correctly made.
    return enrich(match, client)


def link_manually(match, release_id, master_id, *, overwrite):
    """Link `match` to a release or master someone found by hand.

    Makes NO network call -- the caller decides whether to enrich() after
    (the /discogs/ page does; the entry form must not, so a week can be
    entered offline). Returns a note string if nothing was changed, else None.

    A new link clears everything cached about the old one (_clear_cached)
    and the shortlist. Re-entering the link it ALREADY has changes nothing,
    so a typed id that just repeats an existing, enriched link doesn't
    throw its details away.

    overwrite=True  -- the /discogs/ page's paste box: an explicit decision,
                       so an existing, different link is replaced.
    overwrite=False -- the entry form's optional Discogs box: a passing
                       remark while entering a week, so a link someone
                       already decided on is never silently replaced.
    """
    same_link = (
        match.is_linked
        and match.discogs_release_id == release_id
        and match.discogs_master_id == master_id
    )
    if same_link:
        return None
    if not overwrite and match.is_linked:
        return (
            f"Already linked to Discogs {match.reference_type} {match.reference_id}, "
            f"left as-is -- change it from the Discogs page if that's wrong."
        )
    with transaction.atomic():
        _clear_cached(match)
        match.discogs_release_id = release_id
        match.discogs_master_id = master_id
        match.status = DiscogsMatch.MANUAL
        match.linked_at = timezone.now()
        match.save()
        match.candidates.all().delete()
    return None


def parse_reference(raw):
    """A pasted Discogs URL or bare id -> (release_id, master_id, error).

    At most one id is set; `error` is set (and both ids None) when nothing
    recognizable was found. Handles a full URL with slug
    ("https://www.discogs.com/release/12345-Some-Slug" or .../master/...),
    a bare path ("release/12345" / "master/12345"), and a bare number --
    taken as a RELEASE, since that's what pasting from a normal
    discogs.com release page gives you; paste a full master/... URL to
    mean a master. "master/" is checked first so there's never ambiguity.
    """
    raw = (raw or "").strip()
    master_match = re.search(r"master/(\d+)", raw)
    if master_match:
        return None, int(master_match.group(1)), None
    release_match = re.search(r"release/(\d+)", raw)
    if release_match:
        return int(release_match.group(1)), None, None
    if re.fullmatch(r"\d+", raw):
        return int(raw), None, None
    return None, None, "Couldn't read a release or master ID out of that."


def set_status(match, status):
    """Mark `match` rejected / no_match, or reset it to unmatched.

    Unmatched is a FULL reset: the link and everything cached about it is
    dropped, so the row goes back to looking untouched. Rejected, no_match
    and a reset all clear the shortlist. Returns False (and changes
    nothing) for a status that doesn't exist.
    """
    if status not in {choice for choice, _ in DiscogsMatch.STATUS_CHOICES}:
        return False
    with transaction.atomic():
        match.status = status
        if status == DiscogsMatch.UNMATCHED:
            _clear_cached(match)
            match.discogs_release_id = match.discogs_master_id = None
            match.linked_at = None
        match.save()
        if status in (DiscogsMatch.REJECTED, DiscogsMatch.NO_MATCH, DiscogsMatch.UNMATCHED):
            match.candidates.all().delete()
    return True


# ------------------------------------------------------------ enriching

def enrich(match, client=discogs_api):
    """Fetch and store a linked match's full release detail (songwriters,
    producers, genres, its own track) if it still needs it.

    One API call. Never raises: returns an error string on failure (the
    link stands, needs_detail stays True, so it can be retried) or None on
    success or when there's nothing to do (already enriched, or a
    master-only link, which has no release to fetch).
    """
    if not match.needs_detail:
        return None
    try:
        detail = client.fetch_release_detail(match.discogs_release_id, match.recording)
    except discogs_api.DiscogsError as exc:
        return str(exc)
    for field, value in detail.items():
        setattr(match, field, value)
    match.save()
    return None


def enrich_from_cache(match):
    """Enrich `match` from release JSON ALREADY on file, with no network
    call -> True if it did.

    The two sides of a 78 are one Discogs release, so when one side is
    enriched, the full release JSON -- both sides' tracks, songwriters and
    all -- is already cached in its raw_data. A flip-side linked to the
    same release can read its own track out of that copy instead of
    fetching it again. This is what lets the entry form's "+ flip-side"
    finish a Discogs link completely while staying offline
    (non-negotiable #2). False when there's nothing to do or no cached
    copy of that release; the row then simply still needs its details.
    """
    if not match.needs_detail:
        return False
    cached = (
        DiscogsMatch.objects.filter(discogs_release_id=match.discogs_release_id, raw_data__isnull=False)
        .exclude(pk=match.pk).values_list("raw_data", flat=True).first()
    )
    if cached is None:
        return False
    for field, value in discogs_api.release_detail(cached, match.recording).items():
        setattr(match, field, value)
    match.save()
    return True


# ------------------------------------------------------------ checks

def flip_sides_on_different_releases():
    """Releases whose sides are linked to DIFFERENT Discogs releases.

    The sides of a 78 are one physical release, so they normally share
    one Discogs release id; two different ids usually means one side was
    linked to the wrong pressing. Usually -- a genuinely different pressing
    or country is possible, so this only LISTS them for a human to check
    and changes nothing. Returns one dict per release:
    {"label", "catalog_number", "matches": [DiscogsMatch, ... by side]}.
    """
    linked = (
        DiscogsMatch.objects.filter(
            status__in=[DiscogsMatch.CONFIRMED, DiscogsMatch.MANUAL], discogs_release_id__isnull=False,
        )
        .select_related("recording", "recording__song", "recording__label")
        .order_by("recording__label__name", "recording__catalog_number", "recording__side")
    )
    by_release = {}
    for match in linked:
        key = (match.recording.label_id, match.recording.catalog_number)
        by_release.setdefault(key, []).append(match)
    return [
        {"label": matches[0].recording.label.name, "catalog_number": matches[0].recording.catalog_number,
         "matches": matches}
        for matches in by_release.values()
        if len({m.discogs_release_id for m in matches}) > 1
    ]


# ------------------------------------------------------------ batches

def search_batch(matches, client=discogs_api):
    """Search each of `matches` in turn -> (searched, error or None).

    Stops at the first failure rather than hammering a rate limit or a
    dead connection with the rest; everything already searched is saved.
    """
    done = 0
    for match in matches:
        try:
            search(match, client)
        except discogs_api.DiscogsError as exc:
            return done, str(exc)
        done += 1
    return done, None


def confirm_exact_batch(matches, client=discogs_api):
    """Confirm each SUGGESTED match whose shortlist has a single EXACT
    candidate, fetching its details in the same pass.

    Returns {"confirmed", "enriched", "skipped_ties", "detail_error"}.
    Matches with several EXACT candidates are left for a human pick. Once
    a detail fetch fails, no more are attempted (a rate limit fails the
    same way for every row) -- those links stand, still needing detail.
    """
    result = {"confirmed": 0, "enriched": 0, "skipped_ties": 0, "detail_error": None}
    for match in matches:
        if match.status != DiscogsMatch.SUGGESTED:
            continue
        exact = match.exact_candidates()
        if not exact:
            continue
        if len(exact) > 1:
            result["skipped_ties"] += 1
            continue
        if result["detail_error"] is None:
            error = confirm(match, exact[0], client)
            result["detail_error"] = error
            if error is None:
                result["enriched"] += 1
        else:
            # The API already failed this batch: confirm without trying.
            confirm(match, exact[0], _NoFetchClient())
        result["confirmed"] += 1
    return result


def enrich_batch(matches, client=discogs_api):
    """Fetch details for each of `matches` -> (enriched, error or None).
    Stops at the first failure, like search_batch."""
    done = 0
    for match in matches:
        if not match.needs_detail:
            continue
        error = enrich(match, client)
        if error:
            return done, error
        done += 1
    return done, None


class _NoFetchClient:
    # Stands in for the real client once a batch's API calls have started
    # failing: confirming still works (it needs no call), and enrich()
    # reports "not fetched" instead of trying again.
    def fetch_release_detail(self, release_id, recording):
        raise discogs_api.DiscogsError("not attempted after an earlier failure")
