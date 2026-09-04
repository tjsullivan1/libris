# A search is decided by rare words, and filler is a curated list

Follows ADR 0003.

`search_library` weighs each matched word by how few Book Notes carry it, and refuses a note
that matched only filler. Both halves were added after the first implementation was measured
against the real Shelf rather than designed in.

Counting matched words alike is wrong, and obviously so once run. "that mistborn one" returned
91 matches with no Mistborn book among the first six: "one" appears in 50 notes and "mistborn"
in 3, both counted as one match, and the tie then went to the shortest title - "The Hot One",
"Trust No One", "Eat That Frog". Weighing by rarity returns exactly the three Mistborn books.
This is the ADR 0003 failure in a new place: an answer that is confident, wrong, and carries
nothing that shows it is wrong.

Weighting alone does not finish it, and the arithmetic says why. Weighted by rarity a note
matching "that" and "one" scores above a note matching "mistborn", so filler still wins by
accumulating. Nor can a frequency threshold cut it out: on this Shelf "poems" appears in 51
notes and "one" in 50. They are statistically identical and only one of them names a book. The
difference is linguistic, so the list is curated - pronouns, determiners, common prepositions
and the filler of a spoken request - and holds no word that could title a book on its own. A
query made of nothing but filler is taken at face value, because answering "the" with silence
is worse than offering "The Road".

The list is the net, not the mechanism. The MCP tool description asks the model for the words
that identify a book rather than a whole spoken sentence, which is the right place for the
problem: a language model is the best thing in this system at telling "mistborn" from "that
... one I just finished". But an instruction is a promise and not a guarantee, and a model that
passes the sentence through unedited must not get 91 wrong answers.

No relevance floor was added. A search whose book is absent still returns whatever shares a
word - "the way of kings" returns 38 notes led by "Kings of the Wyld", for a book this Shelf
does not hold - and that is the behaviour ADR 0003 asks for. Cutting the tail would mean a
confidence threshold, which is the one thing that ADR refuses by name.

Words are weighed across what the status filter left rather than the whole Shelf, so narrowing
to one status weighs words by how well they separate the books still in play.
