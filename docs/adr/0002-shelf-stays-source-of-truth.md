# The Shelf stays the source of truth; the remote serves other surfaces

We considered making a remote store the source of truth for the Library, with the Shelf as
a rendered projection. Investigating the vault showed that most of what such a move would
buy is already in place: Obsidian mobile plus file sync give multi-device access and
backup, and `autoenrich` already turns a bare note into an enriched Book Note using the
filename as its query. Capture and multi-device editing are solved problems here.

What is not solved is reaching the Library from a surface that has no vault — a mobile app,
or an LLM agent asked "I just finished Oathbringer." That, and not synchronisation, is why
a remote exists.

So the Shelf remains authoritative. The remote holds a replica so other surfaces can read
it, and accepts changes from them as field-level intents rather than whole records. Whole
Book Notes travel Shelf-to-remote; only intents travel remote-to-Shelf. This asymmetry is
deliberate: it removes conflict resolution from the design entirely, because nothing except
the Shelf ever asserts a value for a field it did not change.

It also matches how the clients actually behave. An agent saying "I finished this book"
knows one field. Letting it send a whole record would let it silently erase the ones it
never knew about.
