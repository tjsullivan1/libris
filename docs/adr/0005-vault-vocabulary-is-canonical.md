# The vault's frontmatter vocabulary is canonical

Three vocabularies had drifted apart: the code's `DEFAULT_FRONTMATTER`, the frontmatter
actually present in the 3,137 Book Notes, and the field names in `Reading List.base`. The
Bases view was silently broken as a result, sorting on `author`, `genre` and `Status` when
the notes hold `authors`, `genres` and `status`.

The notes win. They are the data; the code and the view drifted from them. Canonical names
are `authors`, `date_published`, `cover_thumbnail`, `genres`, `status`. Three of the four
are also the better name: `authors` and `genres` are lists, and `date_published` matches
the existing `date_added`, `date_started`, `date_finished`.

`priority` and `series` are promoted to modelled fields. Every Book Note carries every
modelled key, null where unset — an invariant the Shelf already satisfies, and one worth
keeping because it makes the schema and the Bases columns predictable.

Fields Libris does not model — `date_created`, `date_modified`, `aliases`, and anything a
plugin adds later — are carried through verbatim and never rewritten. Dropping them would
not make them go away: the linter re-adds them on the next edit, producing an edit loop
that Obsidian Sync would replicate to every device.
