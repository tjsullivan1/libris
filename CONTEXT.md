# Libris

Libris keeps a catalogue of the books Tim has read, is reading, or intends to read. The
catalogue lives as Markdown notes inside an Obsidian vault, enriched from public book
metadata, and is being extended to a remote replica so it can be added to and queried from
devices that do not hold the notes.

## Language

### The collection

**Library**:
The logical collection of books Tim tracks, independent of where it is stored. The Shelf
and the remote replica are two copies of one Library, not two libraries.

**Shelf**:
The local replica of the Library — a directory of Book Notes sitting inside the Vault.
_Avoid_: book vault, vault path

**Vault**:
The Obsidian vault that the Shelf sits inside. Reserved for Obsidian's own meaning; never
used for the Shelf.
_Avoid_: using "vault" to mean the Shelf

**Book**:
The work itself — what an author wrote. Not a file and not a record. Dune is a Book; the
note about it is a Book Note, and a Google Books result describing it is a Book Candidate.

**Book Candidate**:
Metadata about a Book proposed by an external source — a Google Books search result, a row
from an Audible export. A candidate has not been accepted into the Library and may describe
a Book already held, a different edition, or the wrong Book entirely.
_Avoid_: Book (the bare word, for this meaning), search result, volume

**Book Note**:
One Markdown file representing one book: frontmatter fields plus a body.
_Avoid_: book file, note, record

### Identity

**Libris ID**:
The ULID stamped into a Book Note's frontmatter that identifies it across every replica.
Stable across renames, title standardisation, and merges.
_Avoid_: uid, book id, key

**Superseded ID**:
A Libris ID that identified a Book Note which has since merged into another. It is not
reused and never dangles: it resolves to the surviving note.
_Avoid_: dead id, old id, tombstone

**Duplicate Candidate**:
Two Book Notes that may describe one Book, matched by title rather than by a shared identifier.
A candidate is offered to a person to confirm and never merged on its own, because titles are
compared loosely and a wrong answer merges two different Books.
_Avoid_: possible duplicate, near duplicate

**Matching**:
Deciding whether two Book Notes describe the same book — the judgement behind duplicate
detection and import de-duplication. Deliberately separate from identity, and allowed to be
fuzzy because it is not a key.
_Avoid_: dedupe key, book key

### Surfaces

**Surface**:
Anything that reads or changes the Library: the CLI, Obsidian, a mobile app, or an LLM
agent. Only the CLI writes to the Shelf; every other Surface reaches the Library through
the remote replica.
_Avoid_: client, front end

**Duplicate Guarantee**:
What a Surface's duplicate check was made against: the live Shelf, or a replica no fresher than
the last sync. Every add attempt states which one it gave, so a Surface can say "added" or
"queued" truthfully rather than guessing.
_Avoid_: freshness, confidence

**Intent**:
A change to the Library recorded by a Surface and applied to the Shelf later by the CLI —
either adding a Book Note or setting named fields on an existing one. An Intent names only
the fields it changes, so it can never overwrite a field it knows nothing about.
_Avoid_: delta, patch, command, event

**Intent Outcome**:
What became of an Intent once the CLI tried to apply it. *Applied*: the Shelf changed as
asked. *Absorbed*: the Library already satisfied it, and the outcome names the Libris ID it
turned out to mean. *Rejected*: it could not apply and a person has to decide.
_Avoid_: intent status, failure, error

**Resolution**:
Turning a person's reference to a book ("that Sanderson one I just finished") into a Libris
ID. Always performed while the person is still present to disambiguate.
_Avoid_: lookup, matching (Matching is the separate duplicate-detection judgement)

**Publishing**:
Exposing a read-only view of the Library to someone else. A rendering of existing data, not
a grant of access — distinct from multi-user, which Libris does not support.
_Avoid_: sharing

### Book fields

**Status**:
Where a book sits in the reading cycle: To Read, Reading, Read, or Not To Read.

**Priority**: 
How much a To Read book is wanted: Low, Medium, or High. Absent on books never triaged.

**Format**:
The media the reader has a Book in - Physical, Ebook, or Audiobook. Several at once is normal:
a book owned on paper and listened to as an audiobook is both. Reading state, not a property of
the work or of any edition.
_Avoid_: medium, edition, binding

**Series**:
A link to the note for the series a book belongs to. A relationship within the Vault, not a
text label, and meaningless outside it.

**Enrichment**:
Filling a Book Note's metadata from a public book source. A note is unenriched when no
API-sourced field has a value, whatever else it holds.
