# The store answers exact questions; the service makes every judgement

Follows ADR 0008 and ADR 0020. Settles #88.

`GET /api/v1/books` has to answer the same way from `libris serve` and from the Container App
(ADR 0020). Its exact half ports to Cosmos without difficulty. #88 argued that its fuzzy half,
Near Matches, could not: Cosmos cannot run `titles_match`, so the remote would have to fetch
every document per request or drop Near Matches entirely.

Measuring showed that premise was wrong. When an author is given, `find_similar` still walks
the Shelf locally, but it applies containment only to the notes whose normalized first author
is equal to the one asked about. That filter is an equality test, so a store can answer it
directly and hand back just those notes. On the real Shelf of 3,083 notes, the busiest author
has 75 notes, the median author has 1, and the 99th percentile has 11. The author filter also
does almost all of the useful work: among notes by the same first author, containment finds 9
pairs, and across different authors it finds 1,208, nearly all noise such as "14" matching
"Locked On (Jack Ryan Universe 14)". The "83 pairs" cited in ADR 0026 was an older count.

So the fuzzy part of a Near Match lookup never needed to run in Cosmos. The remote does an
equality query on a normalized first author stored with each document, and the service runs
the same `titles_match` over the results.

**The seam.** The store behind the service answers only equality questions: a Libris ID
(including superseded ones), an ISBN, a Google Books id, a normalized title and first author,
and a normalized first author returning a list. `find_existing` and `find_similar` are service
functions, written once, that ask the store those questions. #88 proposed the seam at the
level of `find_existing` and `find_similar` themselves, with an implementation per location.
That would have put the author filter, the ordering and the limit in two places, where they
could drift apart and give different answers by location, which ADR 0020 rules out and no
test would catch. Keeping every Matching judgement in the service leaves one definition of a
Near Match. Locally the five questions are lookups over `ShelfIndex`; remotely they are
point queries on stored keys.

**No author, no Near Matches, in either location.** A lookup without an author was the only
case that compared titles across the whole Shelf, and the weakest one: title containment across authors is
mostly noise. The remote could only answer it by fetching everything. Rather than let the two
locations disagree, neither checks. The response says why, as `near_match_check: "checked"`
or `"no_author"` next to `near_matches`, because an empty list would read as "nothing
resembles this" (ADR 0029). This is an enum rather than a boolean so a later reason gets a
new value, and it is a new field rather than `near_matches: null` so an older extension still
reading `near_matches.length` keeps working (ADR 0008). An MCP `add_book` with no author
writes the note and reports `no_author`: ADR 0026 stops only when there is a Near Match to
show.

Two consequences. An author spelled differently by the source ("Salvatore, R. A." for "R. A.
Salvatore") misses in both locations, so the two stay consistent even when both are wrong.
And the remote's stored keys are computed by `normalize_for_match`, so changing that function
means re-syncing them.

`search_library` still reads the whole Library, because ADR 0027 weighs words by how rare they
are across the notes. Designing that for the remote is a separate problem and has its own
issue (#150).
