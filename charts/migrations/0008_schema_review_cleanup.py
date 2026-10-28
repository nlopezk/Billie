# Schema review, 2026-10-27 (see GLOSSARY.md and docs/adr/0002-*).
#
# DATA steps run first, while the old columns still exist, so nothing the
# magazine printed is lost:
#   1. The only two printed weeks-on-chart values ever typed are copied
#      into their entries' printed_anomaly_note before that column goes.
#   2. "It's A Sin" (Ivory Joe Hunter, MGM 10818) is the same composition
#      as "It's a Sin" (Eddy Arnold, Victor 20-2241) -- a Song is a
#      composition, and the capital A was a transcription detail. Its
#      recording moves to the one Song row; the other row is deleted.
#   3. "Waltz of The Wind" -- a Song no recording uses ("The Waltz of the
#      Wind" is the one in use) -- is deleted.
#   4. "Rob Wills (Tommy Duncan)" -- an Artist credited on nothing, a
#      typo'd leftover of Bob Wills -- becomes an alias of Bob Wills, so
#      that spelling still resolves, and the row is deleted.
#   5. "Jonny Tyler" -- an Artist credited on nothing -- is deleted.
# Every data step looks its rows up by value and does nothing if they're
# not there in the expected shape, so it can't touch anything else (and a
# fresh, empty database migrates cleanly).
#
# SCHEMA steps then: drop printed_peak_position (never used) and
# printed_weeks_on_chart (now computed), add CHECKs that position and the
# printed last-week number are >= 1, and drop ChartEntry's redundant
# one-column chart_id index.

from django.db import migrations, models


def forwards(apps, schema_editor):
    ChartEntry = apps.get_model("charts", "ChartEntry")
    Song = apps.get_model("charts", "Song")
    Recording = apps.get_model("charts", "Recording")
    Artist = apps.get_model("charts", "Artist")
    ArtistAlias = apps.get_model("charts", "ArtistAlias")
    RecordingCredit = apps.get_model("charts", "RecordingCredit")

    # 1. Keep every printed weeks-on value as a note.
    for entry in ChartEntry.objects.filter(printed_weeks_on_chart__isnull=False):
        note = f"Printed weeks on chart: {entry.printed_weeks_on_chart}"
        entry.printed_anomaly_note = f"{entry.printed_anomaly_note}; {note}" if entry.printed_anomaly_note else note
        entry.save(update_fields=["printed_anomaly_note"])

    # 2. Merge the case-variant "It's a Sin".
    keep = Song.objects.filter(title="It's a Sin").first()
    merge = Song.objects.filter(title="It's A Sin").first()
    if keep and merge and keep.pk != merge.pk:
        Recording.objects.filter(song=merge).update(song=keep)
        merge.delete()

    # 3. The unused duplicate song.
    Song.objects.filter(title="Waltz of The Wind", recordings__isnull=True).delete()

    # 4. "Rob Wills (Tommy Duncan)" -> alias of Bob Wills, only if uncredited.
    typo = Artist.objects.filter(canonical_name="Rob Wills (Tommy Duncan)").first()
    bob = Artist.objects.filter(canonical_name="Bob Wills").first()
    if typo and bob and not RecordingCredit.objects.filter(artist=typo).exists():
        ArtistAlias.objects.get_or_create(alias_text=typo.canonical_name, defaults={"artist": bob})
        typo.delete()

    # 5. "Jonny Tyler", only if uncredited and with no aliases.
    tyler = Artist.objects.filter(canonical_name="Jonny Tyler").first()
    if tyler and not RecordingCredit.objects.filter(artist=tyler).exists() and not tyler.aliases.exists():
        tyler.delete()


class Migration(migrations.Migration):

    dependencies = [
        ('charts', '0007_artist_confirmed_single_act'),
    ]

    operations = [
        # No reverse: the merged/deleted rows can't be told apart again.
        # The pre-migration copy of billboard.db is the way back.
        migrations.RunPython(forwards, migrations.RunPython.noop),
        migrations.RemoveField(model_name='chartentry', name='printed_peak_position'),
        migrations.RemoveField(model_name='chartentry', name='printed_weeks_on_chart'),
        migrations.AlterField(
            model_name='chartentry',
            name='chart',
            field=models.ForeignKey(db_index=False, on_delete=models.deletion.PROTECT, related_name='entries', to='charts.chart'),
        ),
        migrations.AddConstraint(
            model_name='chartentry',
            constraint=models.CheckConstraint(condition=models.Q(position__gte=1), name='chartentry_position_gte_1'),
        ),
        migrations.AddConstraint(
            model_name='chartentry',
            constraint=models.CheckConstraint(
                condition=models.Q(printed_last_week_position__isnull=True) | models.Q(printed_last_week_position__gte=1),
                name='chartentry_printed_last_week_gte_1',
            ),
        ),
    ]
