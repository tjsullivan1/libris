# Format is a list of media, drawn from a closed vocabulary

`format` records how the reader has this Book: on paper, as an ebook, as an audiobook. It is
reading state rather than metadata about the work, so it belongs to the reader and not to the
edition.

It arrived at this decision holding eleven shapes across two types. Measuring the Shelf showed
why, and the answer was not drift that happened once:

- **1,341 notes hold a bare string**, only ever `Audiobook`, `audiobook` or `ebook`, and 1,280 of
  them were added in a single month. That is the Audible import: `importer.py` writes
  `format="Audiobook"` as a scalar.
- **873 notes hold a list**, only ever `Physical`, `Ebook` or `Audiobook`. No Libris code writes
  `Physical` at all, so these came from Obsidian's own property editor, which writes YAML lists.

Two writers, two shapes, both still running. A migration alone would have been undone by the next
import.

**Format is a list.** The only notes carrying deliberate human input about more than one medium
are lists - three of them say `['Physical', 'Audiobook']` and the like - and owning the hardback
while listening to the audiobook is ordinary rather than exceptional. Flattening to a scalar would
match what Libris happens to write, at the cost of information a person entered on purpose.

**The vocabulary is closed at `Physical`, `Ebook`, `Audiobook`** - exactly what the vault holds
(ADR 0005). It joins the field vocabularies introduced for `status` and `priority`, so an
unknown value is refused rather than written. We considered adding `Hardcover` and `Paperback`
up front and rejected it: 538 notes have managed without the distinction, and two overlapping
values on day one is how a vocabulary rots. Adding a fourth later is a line in a tuple and a test.

**Both writers change, or the vault re-drifts.** The importer writes `["Audiobook"]`. `libris add`
takes `--format` repeatably, so the CLI can express everything the field can hold and no shape
exists that the daemon accepts and the CLI cannot produce. The old help text advertised
"paperback, kindle, audiobook" in lowercase, which is where the 35 lowercase values came from -
the code was generating the drift it then could not validate.

**Obsidian is a writer Libris cannot guard**, and it is where these notes are actually edited. So
`ensure_frontmatter_fields` normalises shape and case on every `cleanup`, exactly as it already
does for `authors`. The migration is the first run of a rule that keeps applying, rather than a
one-off. A value outside the vocabulary cannot be repaired by guessing, so those are reported
instead of silently rewritten.
