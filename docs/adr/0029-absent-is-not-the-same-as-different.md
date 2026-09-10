# Absent is not the same as different, and a report says which it has

Follows ADR 0003.

A value that may be missing has three states, not two: it agrees, it disagrees, or it is not
there. Anything that reports a conclusion about such a value says which of the three it
found, and never folds the third into the second.

`doctor` reports Book Notes contesting one Libris ID, and whether they name the same ISBN
decides the repair — notes that agree are usually one note copied and edited, where merging
is right; notes that disagree are two books that ended up sharing an identity, where
re-minting one id is right. That signal was first written as a boolean, `share_an_isbn`, and
a boolean cannot carry three states. It returned `False` when the notes named different
ISBNs and also when one of them named none, so the report printed "These name different
books, so re-minting one id is probably right" for a pair about which it knew nothing (#75).

The same boolean produced a worse answer by a second route. When one colliding note's
frontmatter would not parse, its ISBN read as absent, so two copies of one book — plainly
carrying the same ISBN in their text — came back as disagreeing, and the report recommended
re-minting where merging was right. Not a missing detail: the opposite answer, stated with
confidence. `IsbnAgreement` now answers `SAME`, `DIFFERENT` or `UNKNOWN`, and the third is
phrased as ignorance rather than as a finding — "At least one names no ISBN, so nothing here
says whether these are one book."

This is ADR 0003's rule applied to reporting rather than to resolution. That ADR refuses a
confident-but-wrong match because its failure is silent; a report that treats silence as
disagreement fails the same way and is read by the same person. Two silences are not
agreement either, which is why `find_id_collisions` cannot ask whether the set of ISBNs has
one member: two notes that both name nothing would satisfy that test.

Reporting the fact does not mean withholding what it usually means. `doctor` prints the ISBNs
themselves and then, indented beneath, the repair that usually follows — "Usually one note
copied and edited, where merging is the repair." A report that prints only "the ISBNs match"
makes its reader re-derive the consequence every time, and the consequence is why the fact was
worth printing. What ADR 0003 forbids is acting on a guess, and its stated failure is a wrong
match marking the wrong book Read. Nothing here writes anything; the decision stays with the
person, and the report says so outright.

The practical form of this is a question to ask wherever a conclusion is computed from a
field: what does this say when the field is missing rather than wrong? Every instance so far
has come from answering that question by accident. No test enforces it, and none is proposed:
unlike a duplicated reader, which can be enumerated, this is a habit.
