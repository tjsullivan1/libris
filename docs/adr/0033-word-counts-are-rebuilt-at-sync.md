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
than one whole-Shelf count. The document also holds a bucket for notes with no Status, so that
adding up the buckets always gives the unfiltered count and a search with no filter needs no
separate total.

**The document is rebuilt from scratch at the end of every sync.** That follows from ADR 0002
rather than being a new choice. The remote never writes Book Notes itself: a remote add or update
records an Intent, and the replica changes only when `libris sync` runs. Counts rebuilt at each
sync are therefore exactly as fresh as the notes they describe, and never fresher or staler.
Sync is not built yet (workstream 3), so this is a requirement on it: a sync that pushes notes
without rebuilding the counts leaves the remote ranking against a Shelf that no longer exists.
Keeping them up to date one note at a time (by incrementing counts, or by replacing the document
on each change) would only matter to a remote that writes notes between syncs, and Libris does
not have one. It would also bring what a rebuild avoids: counts that drift after a partial
failure, and Cosmos's limit of ten operations per patch against a note that changes about nine
words. A rebuild runs even when no note changed. Changing how words are split
(`normalize_for_match`, or which fields are counted) then corrects the counts at the next sync,
rather than leaving them computed by old code until some note happens to be edited.

**The store answers, the service ranks.** Following ADR 0032, the store gains two questions: the
notes within a Status that carry any of a given set of words, and the word counts for a Status.
Each document stores the words its title and authors split into, so the first question is an
`ARRAY_CONTAINS` query. Only the query's distinctive words are asked for, which is exact: a note
matching nothing but filler is never a result (ADR 0027). Stop words, weighting, density and
ordering stay in the service, written once. The words each document stores are computed at sync
by the same `_search_tokens` that the local search uses, so the two locations cannot split words
differently.

Two consequences. A query made entirely of filler is taken at face value (ADR 0027), so "the"
fetches every note that carries it, which is 1,478 on the real Shelf. That is the cost of the
behaviour ADR 0027 chose, paid rarely, and it is not reduced here. And the guarantee that the
same query ranks the same way in both locations belongs in a test. It should run one ranking
over the local store and over a stand-in for the remote store built from the same notes, rather
than trust that two stores fed to one function agree.
