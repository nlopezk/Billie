# charts/management/commands/discogs_match.py
#
# Bulk Discogs searching/detail-fetching from the terminal, for grinding
# through hundreds of recordings where a long-running process is normal
# and a browser request would time out. The /discogs/ web section does
# the same work interactively, in small batches -- this is the same
# functions called in a loop with progress output.
#
# Nothing here CONFIRMS anything: searching only ever produces a
# shortlist for a human to decide on (status=suggested), exactly like the
# web section. Confirming stays a human action, per CLAUDE.md's "never
# auto-merge / never silently accept" rule.
#
# Usage:
#   python manage.py discogs_match                    # search everything not yet looked at
#   python manage.py discogs_match --limit 50         # just the next 50
#   python manage.py discogs_match --recording-id 578 # one specific recording
#   python manage.py discogs_match --details          # fetch details for confirmed matches
#   python manage.py discogs_match --requeue-failed   # retry ones previously marked no_match

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from charts import discogs as discogs_api
from charts.models import DiscogsCandidate, DiscogsMatch, Recording


class Command(BaseCommand):
    help = "Search Discogs for candidate releases matching each Recording (or fetch details for confirmed matches)."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=None, help="Process at most this many recordings.")
        parser.add_argument("--recording-id", type=int, default=None, help="Only this recording.")
        parser.add_argument(
            "--details", action="store_true",
            help="Instead of searching, fetch full release details for confirmed/manual matches that lack them.",
        )
        parser.add_argument(
            "--requeue-failed", action="store_true",
            help="Also re-search recordings previously marked 'no match found'.",
        )

    def handle(self, *args, **options):
        if not discogs_api.get_token():
            raise CommandError(
                "No Discogs token configured.\n"
                "Generate one at https://www.discogs.com/settings/developers, then create a\n"
                "`.env` file in the project root containing:  DISCOGS_TOKEN=your_token_here"
            )

        # Self-healing, same as the web queue: any Recording created since
        # the last run (e.g. a debut entered through the entry form) gets
        # its DiscogsMatch row here rather than needing a separate step.
        missing = Recording.objects.filter(discogs__isnull=True)
        created = DiscogsMatch.objects.bulk_create(
            [DiscogsMatch(recording=recording) for recording in missing], ignore_conflicts=True
        )
        if created:
            self.stdout.write(f"Created {len(created)} new DiscogsMatch rows for unlinked recordings.")

        if options["details"]:
            self._fetch_details(options)
        else:
            self._search(options)

    def _target_matches(self, options, statuses):
        matches = DiscogsMatch.objects.filter(status__in=statuses).select_related(
            "recording", "recording__song", "recording__label"
        )
        if options["recording_id"]:
            matches = matches.filter(recording_id=options["recording_id"])
        matches = matches.order_by("recording__song__title")
        if options["limit"]:
            matches = matches[:options["limit"]]
        return list(matches)

    def _search(self, options):
        statuses = [DiscogsMatch.UNMATCHED]
        if options["requeue_failed"]:
            statuses.append(DiscogsMatch.NO_MATCH)
        # An explicit --recording-id means "do this one, whatever state
        # it's in" -- otherwise asking for a specific id that's already
        # been searched would silently do nothing.
        if options["recording_id"]:
            statuses = [choice for choice, _ in DiscogsMatch.STATUS_CHOICES]

        targets = self._target_matches(options, statuses)
        if not targets:
            self.stdout.write(self.style.SUCCESS("Nothing to search -- every recording has been looked at."))
            return

        self.stdout.write(
            f"Searching Discogs for {len(targets)} recording(s). "
            f"Rate limits mean roughly {len(targets) * 1.2 / 60:.1f} minutes. Ctrl-C is safe -- "
            f"progress is saved after each one."
        )

        counts = {"suggested": 0, "no_match": 0}
        for index, match in enumerate(targets, start=1):
            label = f"[{index}/{len(targets)}] {match.recording.song} ({match.recording.catalog_display})"
            try:
                candidates = discogs_api.search_candidates(match.recording)
            except discogs_api.DiscogsError as exc:
                # Stop rather than plough on: a rate limit or a dead
                # connection will just fail identically for every
                # remaining row. Everything already done is committed.
                self.stderr.write(self.style.ERROR(f"{label}: {exc}"))
                self.stderr.write(self.style.WARNING(f"Stopped after {index - 1}. Re-run to continue."))
                return

            with transaction.atomic():
                match.candidates.all().delete()
                for rank, data in enumerate(candidates):
                    DiscogsCandidate.objects.create(match=match, rank=rank, **data)
                match.searched_at = timezone.now()
                match.status = DiscogsMatch.SUGGESTED if candidates else DiscogsMatch.NO_MATCH
                match.save()

            if candidates:
                counts["suggested"] += 1
                best = candidates[0]
                self.stdout.write(
                    f"{label} -> {len(candidates)} candidate(s), best: "
                    f"{best['title']} [{best['confidence']}]"
                )
            else:
                counts["no_match"] += 1
                self.stdout.write(self.style.WARNING(f"{label} -> nothing found"))

        self.stdout.write(self.style.SUCCESS(
            f"\nDone. {counts['suggested']} with suggestions to review, {counts['no_match']} with nothing found.\n"
            f"Review and confirm them at /discogs/ -- this command never confirms a match itself."
        ))

    def _fetch_details(self, options):
        targets = [
            match for match in self._target_matches(options, [DiscogsMatch.CONFIRMED, DiscogsMatch.MANUAL])
            if match.needs_detail
        ]
        if not targets:
            self.stdout.write(self.style.SUCCESS("Every confirmed match already has its full details."))
            return

        self.stdout.write(f"Fetching full release details for {len(targets)} confirmed match(es).")
        for index, match in enumerate(targets, start=1):
            label = f"[{index}/{len(targets)}] {match.recording.song}"
            try:
                detail = discogs_api.fetch_release_detail(match.discogs_release_id, match.recording)
            except discogs_api.DiscogsError as exc:
                self.stderr.write(self.style.ERROR(f"{label}: {exc}"))
                self.stderr.write(self.style.WARNING(f"Stopped after {index - 1}. Re-run to continue."))
                return
            for field, value in detail.items():
                setattr(match, field, value)
            match.save()
            self.stdout.write(f"{label} -> {match.songwriters or '(no songwriter credits on Discogs)'}")

        self.stdout.write(self.style.SUCCESS(f"\nDone -- {len(targets)} match(es) enriched."))
