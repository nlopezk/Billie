# A release is derived from label + catalog number, not stored as its own table

A release (one physical issue, with its sides) is a real concept here: flip-sides are the
other recordings on the same release, and both sides of a 78 share one Discogs release.
But it stays derived, by grouping recordings on `(label, catalog_number)`, rather than
becoming a `Release` table that `Recording` points at. A table would mean a migration of
every recording, and the entry form would have to create or pick a release before each
new recording. Meanwhile, everything that needs the concept (`entry_recording.flip_sides`,
the flip-side shortcut, the duplicate check, the Discogs "sides on different releases"
check) gets it from one lookup at no cost. Revisit only if a release grows facts of its own
that belong to neither side, such as a release date or a picture-sleeve flag.
