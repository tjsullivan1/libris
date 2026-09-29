# Word counts are stored per Status and rebuilt at every sync

Follows ADR 0027 and ADR 0032. Settles #150.

`search_library` weighs each word by how few Book Notes carry it (ADR 0027). That was the last
read that needed the whole Library, and so the last one that a remote store could not answer
without fetching every document. #150 offered three ways out: fetch everything, count words at
search time, or store the counts. Measuring the real Shelf of 3,084 notes rules out the first
two and makes the third cheap.

**Counting at search time needs more words than the query holds.** `_rank` sums two things: the
weight of the query words a note matched, and the weight of every word in that note's title and
authors. The second sum is the density tiebreak that puts a short title ahead of a long one that
matched the same word by accident, and it needs counts for words the person never said. The
matched notes cannot supply those counts, so working them out from the search results would
change the weighting, which ADR 0020 does not allow. Asking the store for each word separately
keeps the weighting but costs too much. "mistborn" needs 14 extra counts, "the way of kings" 338,
"a short history of nearly everything" 541 and "the" 5,113, each a query of its own.

**Stored counts are small.** The Shelf holds 7,875 distinct words across titles and authors, and
63% of them appear in one note. One document holding a count per word per Status is 150-190 KiB,
depending on how it is keyed. That is under a tenth of Cosmos's 2 MB item limit, and it leaves
room for the Shelf to grow many times over.

**The counts are kept per Status.** ADR 0027 weighs words across the notes the status filter
leaves, not the whole Shelf. Weighing every search against whole-Shelf counts would be simpler,
and measurement shows how little it would change: across 15 queries and each Status, the top
five differed in 6 of 26 cases, and each time near-equal results swapped places rather than a
different book coming out on top. It would still be a ranking that depends on where the Library
is served from, which ADR 0020 rules out. Keeping the counts per Status costs 60-100 KiB more
than one whole-Shelf count.

**Each bucket holds what `_weights` reads, over exactly the notes the local search reads.** A
weight is `log(1 + n/df)`, and `n` is the number of notes the filter left, not a sum of word
counts, so each bucket records its note total alongside its word counts. The buckets must
split the searchable notes exactly, or an unfiltered search will not match the Shelf. So there is
one bucket for each status value found on the Shelf, whether or not it is one the Library
defines, and one for notes with no Status. A note carrying an old status value such as
"reading" is counted when nothing is filtered, as it is locally. An unfiltered search adds up
every bucket. Only notes with a title count, because the local search skips the rest. The
Vault's own files share the Shelf's directory and are not Book Notes, so they neither match nor
carry weight.

**The document is rebuilt from scratch at the end of every sync.** That follows from ADR 0002
rather than being a new choice. The remote never writes Book Notes itself: a remote add or update
records an Intent, and the replica changes only when `libris sync` runs. After a sync succeeds,
the counts describe exactly the notes it pushed. Sync is not built yet (workstream 3), so this is
a requirement on it: a sync that pushes notes without rebuilding the counts leaves the remote
ranking against a Shelf that no longer exists. Keeping the counts current one note at a time, by
incrementing them or by replacing the document on each change, would only matter to a remote that
writes notes between syncs, and Libris has no such remote. It would also bring back what a
rebuild avoids: counts that drift after a partial failure, and Cosmos's limit of ten operations
per patch against a note that changes about nine words.

**While a sync is running, notes and counts can disagree, and that is accepted.** Book documents
are partitioned by Libris ID (ADR 0006), so pushing notes and replacing the counts cannot happen
in one atomic step. A search that runs mid-sync can match a newly pushed note whose words the old
counts do not yet hold. Those words weigh nothing (`weights.get(token, 0.0)`), so the note still
matches but ranks too low until the rebuild lands. Weights decide order, never whether a note
matches, so the gap costs ordering for the length of one sync, and no generation scheme is worth
building to close it. A failed rebuild fails the sync and is reported. The rebuild runs at every
sync, even one that pushed no notes, so the next successful sync repairs it without anyone having
to notice.

**Changing how words are split means re-pushing every note.** Each document stores its own
words, and ADR 0015 pushes only the notes whose content changed. A change to `_search_tokens`,
`normalize_for_match` or the counted fields would leave the stored words on unchanged notes
split by the old code, while the counts reflect the new code. That is the same consequence ADR
0032 draws for stored keys, and it has the same remedy: a full re-push. The counts document
records a version for how words are split. When the version differs, sync re-pushes every
document, so the re-push does not depend on someone remembering to run it.

**The store answers, the service ranks.** Following ADR 0032, the store gains two questions: the
notes within a Status that carry any of a given set of words, and the word counts for a Status.
Each document stores the words its title and authors split into, so the first question is an
`ARRAY_CONTAINS` query. The service decides which words to ask for, using the same rule it
applies locally. When the query holds at least one distinctive word, it asks only for those. That
is exact, because a note matching nothing but filler is never a result (ADR 0027). When every word
is filler, it asks for all of them, so "the" still finds "The Road". Both questions cover only
notes with a title. Stop words, weighting, density and ordering stay in the service, written once. The words each document stores are computed at sync
by the same `_search_tokens` that the local search uses, so the two locations cannot split words
differently.

Two consequences. A query made entirely of filler is taken at face value (ADR 0027), so "the"
fetches every note that carries it, which is 1,478 on the real Shelf. That is the cost of the
behaviour ADR 0027 chose, paid rarely, and it is not reduced here. And the guarantee that the
same query ranks the same way in both locations belongs in a test. It should run one ranking
over the local store and over a stand-in for the remote store built from the same notes, rather
than trust that two stores fed to one function agree. That test is #157.
