# Book identity is a minted `libris_id`

Syncing book notes to a remote store requires a key that survives the things Libris
already does to notes: `clean --rename` rewrites filenames to canonical form, `merge`
collapses duplicate notes, and `standardize_title` rewrites titles. Filenames and titles
are therefore unusable as identity. The natural keys do not cover the library either — of
3,137 notes in the vault, 1,231 (39%) have an empty `google_books_id` and 371 (12%) have
an empty `isbn`, and both are edition-specific rather than work-specific.

We mint a ULID into each note's frontmatter as `libris_id`, via a one-time migration over
the existing vault. It is the primary key everywhere: local notes, remote records, and any
future client. ULIDs sort lexicographically by creation time, which also gives sync a
usable cursor.

This keeps **identity** (which note this *is*) separate from **matching** (which notes
describe the same book). Matching stays where it is today — normalized title and author in
`merge.py` and `importer.py` — and is now free to be fuzzy, because it no longer has to
double as a key.
