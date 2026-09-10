# A Book Note is read through one reader

Every piece of code that reads a Book Note goes through `markdown.split_frontmatter`, and
every check that walks the whole Shelf looking for damage goes through `service._read_shelf`.
Neither is a style preference. This project has now spent five separate changes fixing the
same defect, and the defect is always a second reader that did not know what the first one
had learned.

The history is the argument. `update_book_status` replaced a status with an unanchored
`re.sub` over the whole file, so every line containing `status:` was rewritten, including a
reader's own sentences about the book (#92). Nine hand-rolled frontmatter splits existed in
three dialects that disagreed with each other about trailing whitespace, unterminated blocks
and empty frontmatter (#101). Four separate places normalised an ISBN four different ways,
and the one that did not coerce a type could not find 43 of the notes on the Shelf (#105). A
repair pass stripped the indentation off the first line of a body, because the regex it split
on ate the newline and the indent together (#99). Each was found after it shipped.

The failure is not carelessness about a known rule. It is that the knowledge lived in one
function's implementation and nowhere else, so the next author could not inherit it. Every
one of those fixes taught a reader something — that a body may open with an indented code
block, that a fence may carry trailing spaces, that an ISBN may be an integer — and none of
that reached the reader written the following week.

`index_for` is the trap worth naming outright, because it is correct code used in the wrong
place. It builds a cached index of Book Notes for Library queries, and `BookNote.read`
returns `None` for a note whose frontmatter will not parse. That is right for `find_existing`,
which has no use for a note it cannot read. It is exactly wrong for a check whose subject is
damaged notes: `find_id_collisions` used it and silently dropped the unparseable notes, so a
contested identity involving a damaged note went unreported by the command that exists to
report damage (#75). Nothing in either name suggests the incompatibility. A Library query
uses the index; a damage check uses `_read_shelf`.

What raw frontmatter may honestly be read for is part of the same decision. When a block will
not parse as YAML there are no fields, only text, and `_raw_frontmatter_value` reads one
top-level scalar out of it by looking for the key at the start of a line. It is deliberately
naive and must not grow into a second YAML parser — that would be this ADR's own failure
mode. It reads scalars only. A list cannot be recovered from one line, so a damaged note
reports no authors rather than a guess at them, and `_RECOVERABLE_SCALARS` names the four
identifying fields it will attempt.

We rejected leaving each reader to handle what its own callers happened to need. That is the
arrangement that produced all five defects, and it fails quietly: a reader that skips a note
returns a shorter list, not an error. We also rejected a shared reader that guesses harder —
inferring where an unterminated block ends, or parsing a list out of raw text — because a
reader that invents structure is worse than one that reports less. The one guess `_read_shelf`
does make is stated: a file that opens a fence and never closes it is treated as frontmatter
throughout, because such a note states its `libris_id` on the second line and reading it as
prose lost the collision it was in. A file with no fence at all is still all body.

A test enumerates the readers, because this ADR would not have prevented #75. That check was
written in ten minutes with the obvious tool, and being saved by a document requires thinking
to look for one. The document explains the decision to somebody reading it; the test catches
somebody who never did.
