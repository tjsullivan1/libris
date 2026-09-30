# "Two books" is an answer kept on both notes

Follows ADR 0014 and ADR 0018. Settles #142.

`libris duplicates` is a report, not a queue. It recomputes the Duplicate Candidates from the
Shelf on every run and remembers nothing, so an answer of "these are one book" sticks because the
merge removes the pair, while "these are two books" changed nothing and the pair was offered again
on the next run. ADR 0018 says where judgement happens but gave the second answer nowhere to go.
A decisions file marked "different" was reported as `SKIPPED: recorded as two books` and nothing
was written.

When #142 was filed the candidate list was empty and the gap was theoretical. By the time it was
built the real Shelf offered *Unreasonable Hospitality* and *Unreasonable Hospitality: The Field
Guide*. The Field Guide is a companion book with a colon-separated subtitle, which is exactly the
shape the tightened rule is looking for.

**The answer is written on the notes, keyed by Libris ID.** Each note gets a `distinct_from` list
holding the other's Libris ID. A title or a path would not work as the key, because both change.
The ID survives a rename and, because merges keep `superseded_ids`, a merge too. For the same
reason as ADR 0014, the list lives in frontmatter rather than a side file: a second store would
have to be kept in step with the notes, which ADR 0002 rules out.

**A record on either note is enough.** Recording writes both notes, so reading either one shows the
pair is settled. Two writes are still not one, though, and a record where only one write landed
should still hold rather than reopen the question. An entry also counts when it names an identity
the other note absorbed in a merge.

**A merge carries both notes' answers.** Saying the note being merged away is not some third book
is a claim about the book the surviving note now is. An entry that names the survivor itself is
dropped. That only happens when someone merges a pair they had recorded as two books, which means
they changed their mind.

**It is not a modelled field.** `distinct_from` is absent on all but the few notes someone has
answered for. Like `superseded_ids`, adding it to the canonical shape would write
`distinct_from: null` into every note.

The report says how many pairs it skipped because they were recorded as two books, so a settled
pair and a pair that was never found don't look the same. `libris distinct A B` records an answer
from the terminal, and a decisions file answered "different" now records it too.
