# charts/credits.py
#
# Turning a combined-credit Artist row ("Bing Crosby - Andrews Sisters")
# into what it actually is: one Recording credited to TWO artists, via
# the RecordingCredit join table that has existed for this since the
# schema was designed and -- until this was built -- had never once been
# used (0 of 1,229 recordings had more than one credit).
#
# Why these rows exist at all: the original workbook stored artist as one
# free-text field, and combined credits were imported verbatim as single
# canonical Artist rows rather than being auto-split at import time. That
# was deliberate (CLAUDE.md's "never auto-merge two artists" rule cuts
# both ways -- auto-SPLITTING would have been just as much of a silent
# guess), leaving docs/audit/combined_credit_artists.txt as the standing
# checklist. This module is the tooling for working through it.
#
# Nothing here decides anything on its own. It only ever PROPOSES a
# split; a human confirms, and can edit the proposed names first --
# which matters, because a naive split of "James - Sinatra" yields
# "James" and "Sinatra", neither of which is the canonical name you want.

import re

from django.db import transaction
from django.db.models import Count, Max

from .models import Artist, ArtistAlias, Recording, RecordingCredit

# " - " is THE collaboration delimiter in this data: it's what the old
# workbook used to join two credited acts, and 31 of the 43 delimiter-
# bearing artist rows use it.
STRONG_DELIMITER = " - "

# These join words appear far more often INSIDE a single act's name
# ("Homer & Jethro", "Lester Young and His Kansas City Seven", "Brother
# Bones and His Shadows") than between two separate acts. Still surfaced
# for review -- "Ink Spots and Ella Fitzgerald" is a genuine
# collaboration hiding in this group -- but ranked below the strong ones
# and never presumed to be a split.
SOFT_DELIMITERS = [" and ", " And ", " & ", " + ", " with ", " With "]


def split_name(name):
    """
    Break a canonical name into its apparent parts, plus how much to
    trust the break. Returns (parts, is_strong); parts is [name] when
    nothing looks like a join.
    """
    if STRONG_DELIMITER in name:
        parts = [p.strip() for p in name.split(STRONG_DELIMITER) if p.strip()]
        if len(parts) > 1:
            return parts, True
    for delimiter in SOFT_DELIMITERS:
        if delimiter in name:
            parts = [p.strip() for p in name.split(delimiter) if p.strip()]
            if len(parts) > 1:
                return parts, False
    return [name], False


def resolve_artist(name):
    """
    An existing Artist for this exact name, by canonical name or by any
    recorded alias. Case-insensitive, since that's a spelling difference
    rather than a different act. Returns None if it isn't on file --
    the caller then offers to create it.
    """
    name = (name or "").strip()
    if not name:
        return None
    found = Artist.objects.filter(canonical_name__iexact=name).first()
    if found:
        return found
    alias = ArtistAlias.objects.filter(alias_text__iexact=name).select_related("artist").first()
    return alias.artist if alias else None


def proposed_split(artist):
    """
    What splitting this artist would produce, without doing anything:
    each part's text and the existing Artist it resolves to (or None).
    """
    parts, is_strong = split_name(artist.canonical_name)
    return {
        "artist": artist,
        "is_strong": is_strong,
        "parts": [{"text": p, "resolved": resolve_artist(p)} for p in parts],
        "recordings": list(
            Recording.objects.filter(credits__artist=artist)
            .select_related("song", "label").distinct()
        ),
    }


def pending_artists():
    """
    Combined-credit artists still awaiting a decision: name looks joined,
    still credited on at least one recording, and not already confirmed
    to be a single act. Strong ('A - B') candidates first, then by how
    many recordings ride on the decision.
    """
    candidates = (
        Artist.objects.filter(confirmed_single_act=False)
        .annotate(credit_count=Count("recording_credits"))
        .filter(credit_count__gt=0)
    )
    rows = []
    for artist in candidates:
        parts, is_strong = split_name(artist.canonical_name)
        if len(parts) > 1:
            rows.append((not is_strong, -artist.credit_count, artist.canonical_name, artist))
    rows.sort(key=lambda r: r[:3])
    return [r[3] for r in rows]


@transaction.atomic
def apply_split(combined_artist, part_names):
    """
    Re-credit every recording currently credited to `combined_artist` to
    the named parts instead, creating any part that isn't on file yet,
    then delete the now-uncredited combined row.

    Returns (recordings_touched, created_artists, deleted).

    Deliberately NOT touched: Recording.credit_text. That's the billing
    exactly as printed in the magazine (non-negotiable #4) -- "Bing
    Crosby - Andrews Sisters" is what the page said and stays what the
    page said. Only the structured credit changes.
    """
    part_names = [n.strip() for n in part_names if n and n.strip()]
    if len(part_names) < 2:
        raise ValueError("A split needs at least two artist names.")

    # Resolve/create each part first, de-duplicated while preserving the
    # billing order typed in (which becomes credit_order).
    artists, created = [], []
    for name in part_names:
        found = resolve_artist(name)
        if found is None:
            found = Artist.objects.create(canonical_name=name)
            created.append(found)
        if found.id not in [a.id for a in artists]:
            artists.append(found)
    if len(artists) < 2:
        raise ValueError("Those names all resolve to the same artist.")

    recordings = list(Recording.objects.filter(credits__artist=combined_artist).distinct())
    for recording in recordings:
        # Drop the combined credit BEFORE adding the real ones, so the
        # unique_recording_credit_order constraint has room to reuse
        # order 1 rather than colliding with it.
        RecordingCredit.objects.filter(recording=recording, artist=combined_artist).delete()
        for index, artist in enumerate(artists, start=1):
            existing = RecordingCredit.objects.filter(recording=recording, artist=artist).first()
            if existing:
                continue  # already credited (unique_recording_artist) -- leave its order alone
            # Next free slot, so a recording that already carried an
            # unrelated credit doesn't trip unique_recording_credit_order.
            taken = RecordingCredit.objects.filter(recording=recording).aggregate(m=Max("credit_order"))["m"]
            order = index if (taken is None or index > taken) else taken + 1
            RecordingCredit.objects.create(recording=recording, artist=artist, credit_order=order)

    # Safe to remove now that nothing credits it. Checked rather than
    # assumed -- Artist is PROTECTed by RecordingCredit, so a stray
    # reference would raise here instead of silently orphaning rows.
    deleted = False
    if not RecordingCredit.objects.filter(artist=combined_artist).exists():
        combined_artist.delete()
        deleted = True
    return recordings, created, deleted
