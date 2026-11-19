# charts/management/commands/spotify_match.py
#
# Bulk Spotify searching from the terminal, for long runs (an overnight pass
# over every recording) where a long-running process is normal. The /spotify/
# page does the same work interactively. Same functions, in a loop.
#
# Nothing here CONFIRMS anything: searching only produces a shortlist
# (status=suggested) for a human to decide on, exactly like the web page.
#
# Usage:
#   python manage.py spotify_match                     # search everything not yet searched
#   python manage.py spotify_match --limit 50          # just the next 50
#   python manage.py spotify_match --playlist 3        # only recordings in playlist 3
#   python manage.py spotify_match --recording-id 578  # one recording, whatever its state
#   python manage.py spotify_match --requeue-failed    # also retry ones marked not on Spotify

from django.core.management.base import BaseCommand, CommandError

from charts import spotify as spotify_api
from charts import spotify_workflow
from charts.models import SpotifyMatch


class Command(BaseCommand):
    help = "Search Spotify for candidate tracks matching each Recording (never confirms anything)."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=None, help="Search at most this many recordings.")
        parser.add_argument("--playlist", type=int, default=None, help="Only recordings in this playlist (its id).")
        parser.add_argument("--recording-id", type=int, default=None, help="Only this recording, whatever its state.")
        parser.add_argument("--requeue-failed", action="store_true", help="Also re-search ones marked not on Spotify.")

    def handle(self, *args, **options):
        if not spotify_api.get_credentials():
            raise CommandError(
                "No Spotify credentials configured.\n"
                "Create an app at https://developer.spotify.com/dashboard, then add to the\n"
                "project's .env file:  SPOTIFY_CLIENT_ID=...  and  SPOTIFY_CLIENT_SECRET=..."
            )
        created = spotify_workflow.ensure_matches()
        if created:
            self.stdout.write(f"Created {created} new SpotifyMatch rows.")

        matches = SpotifyMatch.objects.select_related("recording__song", "recording__label")
        if options["recording_id"]:
            # An explicit id means "this one, whatever state it's in".
            matches = matches.filter(recording_id=options["recording_id"])
        else:
            statuses = [SpotifyMatch.UNMATCHED] + ([SpotifyMatch.NO_MATCH] if options["requeue_failed"] else [])
            matches = matches.filter(status__in=statuses)
        if options["playlist"]:
            matches = matches.filter(recording__playlist_items__playlist_id=options["playlist"])
        matches = matches.order_by("recording__song__title", "recording__catalog_number")
        if options["limit"]:
            matches = matches[:options["limit"]]
        matches = list(matches)

        self.stdout.write(f"Searching {len(matches)} recording(s) in the {spotify_api.get_market()} catalogue...")
        suggested = not_found = 0
        for n, match in enumerate(matches, start=1):
            label = f"[{n}/{len(matches)}] {match.recording.song.title} -- {match.recording.credit_text}"
            try:
                spotify_workflow.search(match)
            except spotify_api.SpotifyError as exc:
                # Stop rather than burn through the list failing: a token,
                # Premium or rate-limit problem affects every row alike.
                raise CommandError(f"{label}: {exc}\nStopped after {n - 1}.")
            if match.status == SpotifyMatch.SUGGESTED:
                suggested += 1
                top = match.candidates.order_by("rank").first()
                self.stdout.write(f"{label}: {match.candidates.count()} suggestion(s), best {top.confidence}")
            else:
                not_found += 1
                self.stdout.write(f"{label}: nothing found")
        self.stdout.write(self.style.SUCCESS(
            f"Done: {suggested} with suggestions, {not_found} not found. Review them at /spotify/."
        ))
