# charts/admin.py
#
# This is the "free" browsable/editable view of every table CLAUDE.md
# calls out as the reason Django was chosen over Flask/FastAPI/Streamlit.
# autocomplete_fields is what turns a raw foreign key dropdown (unusable
# once there are hundreds of Recordings) into a type-to-search widget --
# but Django requires the TARGET model's own ModelAdmin to declare
# search_fields before it can be the target of anyone else's
# autocomplete_fields, so every model that gets pointed at by a FK
# elsewhere needs search_fields set here, even ones with few registrations
# of their own (e.g. Chart).

from django.contrib import admin
from django.contrib.admin.models import LogEntry
from django.utils.html import format_html

from .models import (
    Label,
    Artist,
    ArtistAlias,
    Song,
    Recording,
    RecordingCredit,
    Chart,
    ChartEra,
    ChartEntry,
    DiscogsMatch,
    DiscogsCandidate,
)


class ArtistAliasInline(admin.TabularInline):
    # Every spelling of an artist's name, edited right on the Artist page
    # instead of needing to hunt down a separate ArtistAlias admin screen.
    model = ArtistAlias
    extra = 1


@admin.register(Artist)
class ArtistAdmin(admin.ModelAdmin):
    list_display = ["canonical_name"]
    search_fields = ["canonical_name", "aliases__alias_text"]  # search_fields needed so other admins can autocomplete to this one
    inlines = [ArtistAliasInline]


@admin.register(ArtistAlias)
class ArtistAliasAdmin(admin.ModelAdmin):
    # Also registered standalone (not just inline) so you can search across
    # every alias ever seen in print, independent of which artist it's on.
    list_display = ["alias_text", "artist"]
    search_fields = ["alias_text"]
    autocomplete_fields = ["artist"]


@admin.register(Label)
class LabelAdmin(admin.ModelAdmin):
    list_display = ["name"]
    search_fields = ["name"]


@admin.register(Song)
class SongAdmin(admin.ModelAdmin):
    list_display = ["title"]
    search_fields = ["title"]


class RecordingCreditInline(admin.TabularInline):
    # Editing credits right on the Recording page is the whole point of
    # the RecordingCredit join table -- add one row per credited artist
    # instead of typing a " - "-delimited string.
    model = RecordingCredit
    extra = 1
    autocomplete_fields = ["artist"]


@admin.register(Recording)
class RecordingAdmin(admin.ModelAdmin):
    list_display = ["credit_text", "song", "label", "catalog_number", "side", "reissue_of"]
    list_filter = ["label"]
    search_fields = ["credit_text", "catalog_number", "song__title"]
    autocomplete_fields = ["label", "song", "reissue_of"]
    inlines = [RecordingCreditInline]


class ChartEraInline(admin.TabularInline):
    # A chart's renames over time, edited on the Chart page -- see
    # CLAUDE.md's Chart/ChartEra split (renames aren't new charts).
    model = ChartEra
    extra = 1


@admin.register(Chart)
class ChartAdmin(admin.ModelAdmin):
    list_display = ["__str__", "publication_weekday"]
    search_fields = ["category", "method"]  # small table, but still needed for ChartEntry's autocomplete_fields to work
    inlines = [ChartEraInline]


@admin.register(ChartEntry)
class ChartEntryAdmin(admin.ModelAdmin):
    # The bulk of the data lives here -- ~10,750 rows and counting. List
    # filters + autocomplete are what make this screen usable at that size;
    # the actual weekly entry FORM (CLAUDE.md's "heart of the project") is
    # a separate custom view, not this admin screen -- this is the
    # "base looker" for spot-checking and one-off corrections.
    list_display = ["chart", "week_date", "position", "recording", "printed_last_week_position"]
    list_filter = ["chart"]
    date_hierarchy = "week_date"
    search_fields = ["recording__credit_text", "recording__song__title"]
    autocomplete_fields = ["chart", "recording"]


class DiscogsCandidateInline(admin.TabularInline):
    # The shortlist a search turned up, visible on the match's own page.
    # Read-only: candidates are machine-generated and transient (cleared
    # when a match is confirmed), so there's nothing meaningful to hand-
    # edit here -- picking one is what the /discogs/ queue is for.
    model = DiscogsCandidate
    extra = 0
    can_delete = False
    readonly_fields = ["discogs_release_id", "title", "label", "catalog_number", "year", "country", "confidence"]
    fields = readonly_fields

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(DiscogsMatch)
class DiscogsMatchAdmin(admin.ModelAdmin):
    # The correction surface for Discogs links. The /discogs/ review queue
    # is the primary workflow (search, pick, confirm); this is here for
    # the awkward one-offs -- fixing a release id by hand, adding a note
    # about why a match is unusual, or bulk-resetting a batch that went
    # wrong via the admin action below.
    list_display = ["recording", "status", "confidence", "discogs_release_id", "matched_title", "songwriters", "updated_at"]
    list_filter = ["status", "confidence"]
    search_fields = [
        "recording__credit_text", "recording__song__title",
        "recording__catalog_number", "matched_title", "songwriters",
    ]
    autocomplete_fields = ["recording"]
    # raw_data is the verbatim API response -- useful to READ when
    # something looks wrong, never worth hand-editing.
    readonly_fields = ["raw_data", "searched_at", "updated_at"]
    inlines = [DiscogsCandidateInline]
    actions = ["reset_matches"]

    @admin.action(description="Reset selected matches back to 'not looked up yet'")
    def reset_matches(self, request, queryset):
        # Clears the link and every cached field, so the recording goes
        # back to looking untouched and can be re-searched cleanly.
        updated = queryset.update(
            status=DiscogsMatch.UNMATCHED, confidence="",
            discogs_release_id=None, discogs_master_id=None,
            matched_title="", matched_artist="", matched_label="",
            matched_catalog_number="", matched_country="", matched_format="",
            matched_year=None, songwriters="", producers="",
            genres=[], styles=[], raw_data=None,
        )
        DiscogsCandidate.objects.filter(match__in=queryset).delete()
        self.message_user(request, f"Reset {updated} match(es).")


@admin.register(LogEntry)
class LogEntryAdmin(admin.ModelAdmin):
    # Django's admin already writes one of these for every addition,
    # change, and deletion made THROUGH THE ADMIN -- this just makes that
    # existing log browsable instead of only showing up as the small
    # "Recent actions" list on the admin homepage.
    #
    # IMPORTANT LIMITATION: this only catches changes made via the admin
    # UI. Edits from `manage.py shell`, the one-time import command, or
    # any other direct ORM/SQL access are NOT logged here -- e.g. the
    # 10,750 imported chart entries don't appear in this log at all,
    # because import_workbook.py calls the ORM directly, not through
    # ModelAdmin. If you need a record of programmatic changes too, that
    # needs a different tool (e.g. django-simple-history, which tracks
    # every save() regardless of how it happened, and keeps full
    # before/after field values rather than just "these fields changed").
    list_display = ["action_time", "user", "action_description", "content_type", "object_link", "change_message"]
    list_filter = ["action_flag", "user", "content_type"]
    search_fields = ["object_repr", "change_message"]
    date_hierarchy = "action_time"
    ordering = ["-action_time"]

    @admin.display(description="action")
    def action_description(self, obj):
        return obj.get_action_flag_display()

    @admin.display(description="object")
    def object_link(self, obj):
        # Link straight to the object's own admin change page when it
        # still exists (nothing to link to after a deletion -- object_repr
        # is all that survives, since the row itself is gone).
        if obj.action_flag == 3:  # DELETION
            return obj.object_repr
        url = obj.get_admin_url()
        if url:
            return format_html('<a href="{}">{}</a>', url, obj.object_repr)
        return obj.object_repr

    # Read-only by design -- a log you can edit or delete isn't an audit
    # trail. Viewing is all this admin page is for.
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
