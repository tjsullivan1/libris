# A merged-away Libris ID leaves a forwarding address on the survivor

Merging two Book Notes destroys one of them. Until now the loser's Libris ID went with it:
`merge.py` deleted the file and nothing recorded what the note had become. That was harmless
while the Shelf was the only replica, because no other Surface had ever seen the ID.

It stops being harmless once Intents exist. A person marks a book Read from their phone; the
desktop merges that book's duplicate before sync runs, and the note they referred to is the
one that lost. Sync would then reject an Intent for a note Libris itself destroyed, and could
not even say what it became.

So the surviving note carries the IDs it superseded, as a `superseded_ids` list in its
frontmatter. An Intent naming a superseded ID resolves to the survivor and applies normally.
Matching stays a separate, fuzzy judgement (ADR 0001); this is identity, and it is exact.

The forwarding address lives in the Shelf rather than in a tombstone file beside it, because
a second store would be state that has to be kept in step with the notes - the situation
ADR 0002 exists to avoid. Keeping it in frontmatter means it travels with the note through
sync, backup, and any future replica, at the cost of one more field in the canonical schema
and a vault index that has to look through it.

A note that is deleted in Obsidian rather than merged leaves nothing behind, and an Intent
naming it is rejected. That is correct: Libris did not cause it and cannot know what was
meant.
