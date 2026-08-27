# Merging asks a person only what only a person can answer

Merging destroys a Book Note, so the questions it asks have to be worth stopping for. Getting
that wrong in either direction is expensive: a merge that asks nothing loses data quietly, and
one that asks about everything trains the reader to wave it through.

## A conflict means a modelled field disagrees

`merge_two_books` compared every key in either note. 414 notes carry `date_created` and
`date_modified`, which Obsidian writes and Libris does not model, and two different files never
agree on them. So most merges reported a conflict, and the only way past was
`allow_conflicts=True` - a flag that also suppresses a genuine disagreement about an ISBN.

Noise that trains someone to disable a safety check is worse than no check. Conflicts are now
reported only for fields in the canonical schema. Fields outside it take the surviving note's
value without comment, and `aliases`, which is genuinely multi-valued, is unioned like the
others. Obsidian rewrites `date_modified` on the next edit anyway, so nothing is lost that the
editor will not restore.

## Fields that hold several values are combined, and sameness is judged loosely

`authors`, `genres`, `tags` and `format` hold several values, so a merge unions them rather than
picking a winner. Two notes crediting different authors describe one book by both of them.

Deduplicating that union on exact strings would not work here: 185 notes carry irregular
whitespace in an author name and 8 write authors as wikilinks, so `Dan   Harris` and
`Dan Harris`, or `[[Leo Tolstoy]]` and `Leo Tolstoy`, would each survive as two people. Sameness
is therefore judged on the fully normalized name - wikilink unwrapped, whitespace collapsed.

What gets *written* is narrower than what gets compared. The surviving value has its whitespace
collapsed but keeps its wikilink, because in Obsidian a wikilink is the graph edge to that
author's note and unwrapping it would quietly delete a connection. Comparing loosely and writing
conservatively is deliberate: the loose half prevents doubles, and the conservative half keeps a
merge from editing values nobody asked it to touch.

## Judgement happens where the evidence is; merging happens where the data is fresh

Roughly seventy-four of the Shelf's duplicate pairs are Duplicate Candidates - matched by title
containment rather than by an identifier - and each needs a person to say whether two notes
describe one book. A terminal is a poor place to compare two notes and a fine place to merge
them.

So the two halves are split. A person triages on a surface built for it, and `libris merge`
consumes the exported decisions, which are keyed by Libris ID rather than by title or path.
Before acting the command re-derives the candidate pairs from the live Shelf and applies a
decision only where the file and the vault still agree, reporting anything that has drifted
instead of acting on a stale answer. A Libris ID that was merged away in the meantime still
resolves, because the survivor records it (ADR 0014).

Nothing in this path merges without an answer. A candidate with no decision recorded is left
alone.
