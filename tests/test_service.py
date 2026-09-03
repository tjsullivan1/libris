"""Tests for the service layer the adapters sit over.

ADR 0008 puts resolution, creation and querying below the adapters, so the REST
surface and the MCP tools cannot drift apart by reimplementing matching. These
tests exercise that layer directly, with no HTTP involved.
"""

from datetime import date

import pytest

from libris.api import BookCandidate
from libris.markdown import (
    BookNote,  # noqa: F401
    create_book_note,
)
from libris.note_format import InvalidFieldValue
from libris.service import (
    MAX_SEARCH_LIMIT,
    BookNotFound,
    DecisionStatus,
    Outcome,
    add_book,
    apply_decisions,
    build_lookup_query,
    find_by_libris_id,
    find_existing,
    is_isbn10,
    search_library,
    update_book,
)


def _candidate(**overrides) -> BookCandidate:
    fields = {"title": "Dune", "authors": ["Frank Herbert"]}
    fields.update(overrides)
    return BookCandidate(**fields)


# --- ISBN-10 checksum ---


def test_a_real_isbn10_passes_the_checksum():
    # Given a genuine ISBN-10 (Dune)
    # Then it validates
    assert is_isbn10("0441013597") is True


def test_an_isbn10_ending_in_x_passes():
    # Given an ISBN-10 whose check digit is X
    # Then the X is understood as ten rather than rejected
    assert is_isbn10("043942089X") is True


def test_a_kindle_asin_fails_the_checksum():
    # Given a Kindle ASIN, which is ten characters but not an ISBN
    # Then it does not validate, so it is never sent as isbn:
    assert is_isbn10("B000FC0SIM") is False


def test_a_wrong_check_digit_fails():
    # Given an ISBN-10 with a corrupted final digit
    # Then it does not validate
    assert is_isbn10("0441013598") is False


# --- query construction ---


def test_an_isbn_builds_an_isbn_query():
    # Given a scrape that found an ISBN
    # When a query is built
    query = build_lookup_query(
        isbn="9780441013593", title="Dune", authors=["Frank Herbert"]
    )

    # Then the ISBN wins, because it identifies an edition exactly
    assert query == "isbn:9780441013593"


def test_an_asin_that_is_a_valid_isbn10_is_used_as_one():
    # Given an Amazon page whose ASIN is really an ISBN-10, as print books' are
    query = build_lookup_query(
        asin="0441013597", title="Dune", authors=["Frank Herbert"]
    )

    # Then it is searched as an ISBN
    assert query == "isbn:0441013597"


def test_an_asin_that_is_not_an_isbn10_falls_back_to_title_and_author():
    # Given a Kindle ASIN, which is not an ISBN
    query = build_lookup_query(
        asin="B000FC0SIM", title="Dune", authors=["Frank Herbert"]
    )

    # Then the search uses what a person would search with
    assert query == "intitle:Dune inauthor:Frank Herbert"


def test_a_title_alone_builds_a_title_query():
    # Given a scrape that found no author
    query = build_lookup_query(title="Dune")

    # Then only the title constrains the search
    assert query == "intitle:Dune"


def test_nothing_identifying_builds_no_query():
    # Given a page nothing could be scraped from
    # Then there is no query to run, and the caller is told so rather than
    # being handed a search for everything
    assert build_lookup_query() is None


# --- finding an existing note ---


def test_an_existing_note_is_found_by_isbn(tmp_path):
    # Given a Book Note on the Shelf
    create_book_note(_candidate(isbn="9780441013593"), tmp_path)

    # When the same ISBN is looked up
    found = find_existing(tmp_path, isbn="9780441013593")

    # Then the note is found, carrying its identity
    assert found is not None
    assert found.libris_id


def test_an_existing_note_is_found_by_google_books_id(tmp_path):
    # Given a Book Note that came from Google Books
    create_book_note(_candidate(google_books_id="dune1"), tmp_path)

    # When that volume is looked up
    found = find_existing(tmp_path, google_books_id="dune1")

    # Then it is found
    assert found is not None


def test_an_existing_note_is_found_by_title_and_author(tmp_path):
    # Given a Book Note with no identifiers at all
    create_book_note(_candidate(), tmp_path)

    # When the same book is looked up by name
    found = find_existing(tmp_path, title="dune", authors=["frank herbert"])

    # Then normalization matches it despite the case
    assert found is not None


def test_a_book_not_on_the_shelf_is_not_found(tmp_path):
    # Given an empty Shelf
    # When anything is looked up
    # Then nothing is found; a miss is a miss (ADR 0003)
    assert find_existing(tmp_path, isbn="9780441013593") is None


def test_a_different_book_is_not_matched(tmp_path):
    # Given one Book Note
    create_book_note(_candidate(isbn="9780441013593"), tmp_path)

    # When a different book is looked up
    found = find_existing(tmp_path, title="Neuromancer", authors=["William Gibson"])

    # Then it is not confused for the one on the Shelf
    assert found is None


# --- adding ---


def test_adding_a_new_book_writes_it_and_returns_its_identity(tmp_path):
    # Given a Shelf without the book
    # When it is added
    result = add_book(tmp_path, _candidate(isbn="9780441013593"))

    # Then the note exists and the answer carries the durable identity, not
    # just the path, which clean --rename can move (ADR 0016)
    assert result.outcome is Outcome.CREATED
    assert result.path.exists()
    assert result.libris_id
    assert result.libris_id in result.path.read_text(encoding="utf-8")


def test_adding_a_book_already_held_does_not_overwrite_it(tmp_path):
    # Given a Book Note already on the Shelf
    first = add_book(tmp_path, _candidate(isbn="9780441013593"))
    original = first.path.read_text(encoding="utf-8")

    # When the same book is added again
    second = add_book(tmp_path, _candidate(isbn="9780441013593"))

    # Then the Library already satisfied the request, and the existing note is
    # returned untouched rather than rewritten
    assert second.outcome is Outcome.ALREADY_PRESENT
    assert second.libris_id == first.libris_id
    assert second.path == first.path
    assert first.path.read_text(encoding="utf-8") == original


def test_adding_applies_overrides(tmp_path):
    # Given a book being added as already read
    result = add_book(tmp_path, _candidate(), overrides={"status": "Read", "rating": 5})

    # Then the note carries them
    text = result.path.read_text(encoding="utf-8")
    assert "status: Read" in text
    assert "rating: 5" in text


def test_adding_refuses_a_status_the_library_does_not_define(tmp_path):
    # Given an override carrying a value from off this machine
    # When it is added
    # Then it is refused rather than written (#65)
    with pytest.raises(InvalidFieldValue):
        add_book(tmp_path, _candidate(), overrides={"status": "finished"})


def test_adding_refuses_an_unknown_field(tmp_path):
    # Given an override naming a field the canonical schema has no place for
    # When it is added
    # Then it is refused
    with pytest.raises(ValueError):
        add_book(tmp_path, _candidate(), overrides={"nonsense": "x"})


# --- resolution through superseded ids (#64, ADR 0014) ---


def test_a_live_libris_id_resolves(tmp_path):
    # Given a Book Note on the Shelf
    path = create_book_note(_candidate(), tmp_path)
    note = BookNote.read(path)

    # When it is looked up by its identity
    found = find_by_libris_id(tmp_path, note.libris_id)

    # Then it is found
    assert found is not None
    assert found.path == path


def test_a_superseded_id_resolves_to_the_survivor(tmp_path):
    # Given a note that absorbed another during a merge
    path = create_book_note(_candidate(), tmp_path)
    text = path.read_text(encoding="utf-8")
    path.write_text(
        text.replace("title:", "superseded_ids:\n- GONE\ntitle:", 1), encoding="utf-8"
    )

    # When an Intent names the identity that was merged away
    found = find_by_libris_id(tmp_path, "GONE")

    # Then it resolves to the surviving note rather than missing, so the Intent
    # applies instead of being rejected for a note Libris itself destroyed
    assert found is not None
    assert found.path == path


def test_an_unknown_libris_id_does_not_resolve(tmp_path):
    # Given a Shelf that never held the Book
    create_book_note(_candidate(), tmp_path)

    # When an unknown identity is looked up
    # Then a miss is a miss (ADR 0003)
    assert find_by_libris_id(tmp_path, "01NOPE") is None


def test_a_live_id_wins_over_a_superseded_one(tmp_path):
    # Given one note whose live id is what another note lists as superseded -
    # possible only through a bad merge, but it must resolve predictably
    live = create_book_note(_candidate(title="Live"), tmp_path)
    live_id = BookNote.read(live).libris_id

    other = create_book_note(_candidate(title="Other"), tmp_path)
    text = other.read_text(encoding="utf-8")
    other.write_text(
        text.replace("title:", f"superseded_ids:\n- {live_id}\ntitle:", 1),
        encoding="utf-8",
    )

    # When that id is resolved
    found = find_by_libris_id(tmp_path, live_id)

    # Then the note that actually holds the identity wins
    assert found.path == live


def test_a_blank_libris_id_does_not_resolve(tmp_path):
    # Given a Shelf with notes on it
    create_book_note(_candidate(), tmp_path)

    # When an empty or whitespace-only identity is resolved
    # Then it misses immediately rather than reading every note to find nothing
    assert find_by_libris_id(tmp_path, "") is None
    assert find_by_libris_id(tmp_path, "   ") is None


# --- applying an exported review (#72, ADR 0018) ---


def _pair(tmp_path):
    """Two notes for one Book, differing by a subtitle."""
    a = create_book_note(_candidate(title="The Brass Verdict"), tmp_path)
    b = create_book_note(_candidate(title="The Brass Verdict: A Novel"), tmp_path)
    return BookNote.read(a), BookNote.read(b)


def _decision(first, second, verdict="same"):
    return {
        "decision": verdict,
        "shorter": {"title": first.title, "libris_id": first.libris_id},
        "longer": {"title": second.title, "libris_id": second.libris_id},
    }


def test_a_pair_marked_one_book_is_merged(tmp_path):
    # Given two notes a person judged to be one Book
    first, second = _pair(tmp_path)

    # When the decision is applied
    outcomes = apply_decisions(tmp_path, [_decision(first, second)])

    # Then they become one note
    assert [o.status for o in outcomes] == [DecisionStatus.MERGED]
    assert len(list(tmp_path.glob("*.md"))) == 1


def test_a_pair_marked_two_books_is_left_alone(tmp_path):
    # Given a pair a person judged to be different books
    first, second = _pair(tmp_path)

    # When the decision is applied
    outcomes = apply_decisions(tmp_path, [_decision(first, second, "different")])

    # Then nothing is merged
    assert [o.status for o in outcomes] == [DecisionStatus.SKIPPED]
    assert len(list(tmp_path.glob("*.md"))) == 2


def test_a_decision_naming_a_vanished_note_is_reported(tmp_path):
    # Given a decision recorded against a Shelf that has since changed
    first, second = _pair(tmp_path)
    second.path.unlink()

    # When it is applied
    outcomes = apply_decisions(tmp_path, [_decision(first, second)])

    # Then it is reported rather than acted on: the file describes the Shelf as
    # it was, and the Shelf is what is true
    assert [o.status for o in outcomes] == [DecisionStatus.DRIFTED]
    assert first.path.exists()


def test_a_decision_still_applies_after_one_note_was_merged_away(tmp_path):
    # Given a note that has since absorbed another, so its id is superseded
    first, second = _pair(tmp_path)
    third = create_book_note(_candidate(title="The Brass Verdict: Deluxe"), tmp_path)
    third_note = BookNote.read(third)
    apply_decisions(tmp_path, [_decision(first, third_note)])

    # When a decision naming the merged-away id is applied
    outcomes = apply_decisions(tmp_path, [_decision(third_note, second)])

    # Then it resolves through superseded_ids rather than reporting drift
    # (ADR 0014)
    assert [o.status for o in outcomes] == [DecisionStatus.MERGED]


def test_a_conflicting_pair_is_reported_not_merged(tmp_path):
    # Given two notes that disagree about the reader's own value
    first, second = _pair(tmp_path)
    second.path.write_text(
        second.path.read_text(encoding="utf-8").replace("rating:", "rating: 3"),
        encoding="utf-8",
    )
    first.path.write_text(
        first.path.read_text(encoding="utf-8").replace("rating:", "rating: 5"),
        encoding="utf-8",
    )

    # When the decision is applied
    outcomes = apply_decisions(tmp_path, [_decision(first, second)])

    # Then it stops: the review answered "is this one Book", not "which rating is
    # yours"
    assert [o.status for o in outcomes] == [DecisionStatus.CONFLICTED]
    assert len(list(tmp_path.glob("*.md"))) == 2


# --- searching the Library ---


def _shelve(vault_path, title, authors, **overrides):
    """Put one Book Note on a Shelf and hand back the note."""
    path = create_book_note(
        _candidate(title=title, authors=authors),
        vault_path,
        overrides=overrides or None,
    )
    return BookNote.read(path)


def _titles(result):
    return [note.title for note in result.books]


def test_a_title_query_finds_the_note(tmp_path):
    # Given a Shelf holding one book
    _shelve(tmp_path, "Dune", ["Frank Herbert"])

    # When the Library is searched for its title
    result = search_library(tmp_path, query="dune")

    # Then it is found
    assert _titles(result) == ["Dune"]
    assert result.total == 1


def test_an_author_alone_finds_their_books(tmp_path):
    # Given two books by one author and one by another
    _shelve(tmp_path, "The Way of Kings", ["Brandon Sanderson"])
    _shelve(tmp_path, "Oathbringer", ["Brandon Sanderson"])
    _shelve(tmp_path, "Dune", ["Frank Herbert"])

    # When someone asks for "that Sanderson one" - a query with no title in it
    result = search_library(tmp_path, query="sanderson")

    # Then both of theirs come back. This is the case find_similar cannot serve:
    # it returns nothing without a title, and treats an author as an exact
    # equality filter rather than something to search on (ADR 0003).
    assert sorted(_titles(result)) == ["Oathbringer", "The Way of Kings"]


def test_the_tighter_title_outranks_the_one_that_merely_contains_it(tmp_path):
    # Given two different books that share a word
    _shelve(tmp_path, "Long Road to Mercy", ["David Baldacci"])
    _shelve(tmp_path, "Mercy", ["Jodi Picoult"])

    # When the shared word is searched for
    result = search_library(tmp_path, query="mercy")

    # Then both are offered, because deciding between them is not this layer's
    # job (ADR 0003) - but the note the query describes wholly comes first.
    assert _titles(result) == ["Mercy", "Long Road to Mercy"]


def test_more_matched_words_outrank_fewer(tmp_path):
    # Given a Shelf where one title answers more of the query than the other
    _shelve(tmp_path, "The Way of Kings", ["Brandon Sanderson"])
    _shelve(tmp_path, "Kings of the Wyld", ["Nicholas Eames"])

    # When several words are searched for
    result = search_library(tmp_path, query="way of kings")

    # Then the note matching more of them ranks first
    assert _titles(result)[0] == "The Way of Kings"


def test_the_query_is_normalized_before_matching(tmp_path):
    # Given a note whose title carries punctuation and capitals
    _shelve(tmp_path, "Mistborn: The Final Empire", ["Brandon Sanderson"])

    # When the query carries neither
    result = search_library(tmp_path, query="MISTBORN final empire")

    # Then it still matches, the same way every other comparison here normalizes
    assert _titles(result) == ["Mistborn: The Final Empire"]


def test_a_miss_is_a_miss(tmp_path):
    # Given a Shelf that holds nothing like the query
    _shelve(tmp_path, "Dune", ["Frank Herbert"])

    # When something absent is searched for
    result = search_library(tmp_path, query="neuromancer")

    # Then nothing is invented (ADR 0003)
    assert result.books == []
    assert result.total == 0


def test_a_status_narrows_the_search(tmp_path):
    # Given the same author held at two different points in the reading cycle
    _shelve(tmp_path, "Oathbringer", ["Brandon Sanderson"], status="Read")
    _shelve(tmp_path, "The Way of Kings", ["Brandon Sanderson"], status="To Read")

    # When the search is narrowed to what has been read
    result = search_library(tmp_path, query="sanderson", status="Read")

    # Then only that one comes back. Status is not fuzzy - it is a closed
    # vocabulary the Library defines (ADR 0022) - so it filters rather than ranks.
    assert _titles(result) == ["Oathbringer"]
    assert result.total == 1


def test_a_status_the_library_does_not_define_is_refused(tmp_path):
    # Given a status outside the four the Library allows
    # When it is used to narrow a search
    # Then it is refused rather than silently matching nothing
    with pytest.raises(InvalidFieldValue):
        search_library(tmp_path, status="Finished")


def test_omitting_the_query_lists_by_filter(tmp_path):
    # Given a Shelf where one book is being read
    _shelve(tmp_path, "Oathbringer", ["Brandon Sanderson"], status="Reading")
    _shelve(tmp_path, "Dune", ["Frank Herbert"], status="To Read")

    # When there is no query at all - "what am I reading?"
    result = search_library(tmp_path, status="Reading")

    # Then the filter alone answers it
    assert _titles(result) == ["Oathbringer"]


def test_omitting_everything_lists_the_whole_shelf(tmp_path):
    # Given a Shelf of three books
    for title in ("Dune", "Oathbringer", "Neuromancer"):
        _shelve(tmp_path, title, ["Someone"])

    # When nothing is asked for
    result = search_library(tmp_path)

    # Then the whole Shelf is counted, in a deterministic order
    assert result.total == 3
    assert _titles(result) == ["Dune", "Neuromancer", "Oathbringer"]


def test_the_total_counts_matches_the_limit_did_not_return(tmp_path):
    # Given more books than will be returned
    for index in range(5):
        _shelve(tmp_path, f"Dune {index}", ["Frank Herbert"])

    # When the search is limited
    result = search_library(tmp_path, query="dune", limit=2)

    # Then the caller is told how many there really were, so a Surface can say
    # "1,452 on the list, here are some" rather than implying it saw them all
    assert len(result.books) == 2
    assert result.total == 5


def test_the_limit_is_capped(tmp_path):
    # Given a Shelf and a caller asking for more than the ceiling
    for index in range(3):
        _shelve(tmp_path, f"Dune {index}", ["Frank Herbert"])

    # When an absurd limit is requested
    result = search_library(tmp_path, query="dune", limit=10_000)

    # Then it is clamped rather than honoured. Reading a whole Library into a
    # context window is the thing the cap exists to prevent.
    assert result.limit == MAX_SEARCH_LIMIT


def test_a_limit_of_zero_returns_nothing_but_still_counts(tmp_path):
    # Given a Shelf holding matches
    _shelve(tmp_path, "Dune", ["Frank Herbert"])

    # When nothing is asked to be returned
    result = search_library(tmp_path, query="dune", limit=0)

    # Then the count still answers "how many", which is a real question
    assert result.books == []
    assert result.total == 1


def test_a_note_without_a_title_is_skipped_rather_than_crashing(tmp_path):
    # Given a Shelf holding a file with frontmatter but no title
    (tmp_path / "broken.md").write_text(
        "---\nlibris_id: 01J0000000000000000000000A\ntitle:\nauthors: []\n---\n",
        encoding="utf-8",
    )
    _shelve(tmp_path, "Dune", ["Frank Herbert"])

    # When the Library is listed
    result = search_library(tmp_path)

    # Then the untitled note is passed over. Obsidian writes into this directory
    # too, so a note Libris did not create is ordinary rather than exceptional.
    assert _titles(result) == ["Dune"]


def test_a_common_word_alone_does_not_make_a_match(tmp_path):
    # Given a Shelf where one note shares only a common word with the query
    _shelve(tmp_path, "The Way of Kings", ["Brandon Sanderson"])
    _shelve(tmp_path, "The Silmarillion", ["J.R.R. Tolkien"])

    # When a title carrying that word is searched for
    result = search_library(tmp_path, query="the way of kings")

    # Then the note matching on "the" alone is not offered. Otherwise a title
    # with an article in it would report most of a 3,000-note Shelf as a match,
    # and the total would stop meaning anything.
    assert _titles(result) == ["The Way of Kings"]


def test_a_query_of_only_common_words_is_taken_at_face_value(tmp_path):
    # Given a note whose title really is a common word
    _shelve(tmp_path, "The Road", ["Cormac McCarthy"])

    # When that is all the person said
    result = search_library(tmp_path, query="the")

    # Then it still matches, rather than the guard swallowing the only query
    # the person gave
    assert _titles(result) == ["The Road"]


def test_a_distinctive_word_outweighs_a_common_one(tmp_path):
    # Given a Shelf where one word is everywhere, on notes short enough that
    # brevity alone would float them to the top
    for subject in ("Big", "New", "Old", "Best", "Grey"):
        _shelve(tmp_path, f"{subject} Book", ["Ann Bell"])
    _shelve(tmp_path, "Book", ["Ann Bell"])
    _shelve(tmp_path, "Mistborn: The Final Empire", ["Brandon Sanderson"])

    # When a query names both a common word and a rare one
    result = search_library(tmp_path, query="mistborn book")

    # Then the rare word decides. Counting matched words alone ties these at one
    # apiece, and the tie then goes to the shortest note - which is the wrong
    # book for a reason that has nothing to do with what was asked.
    assert _titles(result)[0] == "Mistborn: The Final Empire"


def test_conversational_filler_does_not_pull_in_unrelated_books(tmp_path):
    # Given the Shelf someone would actually be talking about
    _shelve(tmp_path, "The Final Empire: Mistborn Book 1", ["Brandon Sanderson"])
    _shelve(tmp_path, "The Hot One", ["Lauren Blakely"])
    _shelve(tmp_path, "Eat That Frog", ["Brian Tracy"])

    # When someone says it the way a person says it
    result = search_library(tmp_path, query="that mistborn one")

    # Then only the book they described comes back. Measured against the real
    # Shelf, "that" and "one" appear in 39 and 50 notes while "mistborn" appears
    # in 3, and weighting them alike returned 91 matches with no Mistborn among
    # the first six.
    assert _titles(result) == ["The Final Empire: Mistborn Book 1"]
    assert result.total == 1


# --- updating a Book Note ---


def _read_back(vault_path, libris_id):
    for path in vault_path.glob("*.md"):
        note = BookNote.read(path)
        if note and note.libris_id == libris_id:
            return note
    raise AssertionError(f"no note holds {libris_id}")


def test_a_named_field_is_set(tmp_path):
    # Given a book waiting to be read
    note = _shelve(tmp_path, "Dune", ["Frank Herbert"], status="To Read")

    # When its status is moved on
    update_book(tmp_path, note.libris_id, {"status": "Reading"})

    # Then the Shelf holds the new value
    assert _read_back(tmp_path, note.libris_id).frontmatter["status"] == "Reading"


def test_fields_that_were_not_named_are_left_alone(tmp_path):
    # Given a book carrying a rating nobody mentioned
    note = _shelve(tmp_path, "Dune", ["Frank Herbert"], status="To Read", rating=5)

    # When only the status is set
    update_book(tmp_path, note.libris_id, {"status": "Reading"})

    # Then the rating survives. An update names only the fields it changes, so
    # it can never overwrite a field it knows nothing about.
    assert _read_back(tmp_path, note.libris_id).frontmatter["rating"] == 5


def test_finishing_a_book_stamps_the_date_and_says_so(tmp_path):
    # Given a book being read, with no finish date
    note = _shelve(tmp_path, "Dune", ["Frank Herbert"], status="Reading")

    # When it is marked Read without a date
    result = update_book(tmp_path, note.libris_id, {"status": "Read"})

    # Then today is stamped, and reported as something the caller did not ask
    # for - so a person who meant last Tuesday can correct it (ADR 0024)
    today = date.today().isoformat()
    assert _read_back(tmp_path, note.libris_id).frontmatter["date_finished"] == today
    assert result.derived == {"date_finished": today}


def test_starting_a_book_stamps_the_started_date(tmp_path):
    # Given a book nobody has opened
    note = _shelve(tmp_path, "Dune", ["Frank Herbert"], status="To Read")

    # When it is marked Reading
    result = update_book(tmp_path, note.libris_id, {"status": "Reading"})

    # Then the start date is stamped and disclosed
    assert result.derived == {"date_started": date.today().isoformat()}


def test_an_explicit_date_is_never_overridden(tmp_path):
    # Given a book being read
    note = _shelve(tmp_path, "Dune", ["Frank Herbert"], status="Reading")

    # When it is marked Read with the date it was actually finished
    result = update_book(
        tmp_path, note.libris_id, {"status": "Read", "date_finished": "2026-08-25"}
    )

    # Then that date stands and nothing is derived. The stamp is a fallback,
    # never an override (ADR 0024).
    written = _read_back(tmp_path, note.libris_id).frontmatter["date_finished"]
    assert str(written) == "2026-08-25"
    assert result.derived == {}


def test_a_date_already_on_the_note_is_not_restamped(tmp_path):
    # Given a book finished years ago
    note = _shelve(
        tmp_path, "Dune", ["Frank Herbert"], status="Read", date_finished="2019-04-01"
    )

    # When its status is set to Read once more
    result = update_book(tmp_path, note.libris_id, {"status": "Read"})

    # Then the original date survives. A re-read is not something the Library
    # models, and inventing one here would be scope creep (ADR 0024).
    written = _read_back(tmp_path, note.libris_id).frontmatter["date_finished"]
    assert str(written) == "2019-04-01"
    assert result.derived == {}


def test_a_value_the_library_does_not_define_is_refused(tmp_path):
    # Given a status outside the four the Library allows
    note = _shelve(tmp_path, "Dune", ["Frank Herbert"])

    # When it is written
    # Then it is refused. `libris update` offers "Finished" to this day, which
    # is the drift ADR 0022 exists to stop.
    with pytest.raises(InvalidFieldValue):
        update_book(tmp_path, note.libris_id, {"status": "Finished"})


def test_a_field_that_is_not_the_readers_is_refused(tmp_path):
    # Given a field describing the edition rather than the reading of it
    note = _shelve(tmp_path, "Dune", ["Frank Herbert"])

    # When it is written
    # Then it is refused: Libris owns the title (ADR 0012), and bibliographic
    # fields come from enrichment rather than from someone talking.
    with pytest.raises(ValueError):
        update_book(tmp_path, note.libris_id, {"title": "Doon"})


def test_a_null_does_not_silently_clear_a_field(tmp_path):
    # Given a book carrying a rating
    note = _shelve(tmp_path, "Dune", ["Frank Herbert"], rating=5)

    # When a null arrives for it
    # Then it is refused rather than treated as "clear this". A model emitting
    # null for "unchanged" would otherwise erase a field nobody mentioned.
    with pytest.raises(ValueError):
        update_book(tmp_path, note.libris_id, {"rating": None})
    assert _read_back(tmp_path, note.libris_id).frontmatter["rating"] == 5


def test_an_unknown_identity_is_not_found(tmp_path):
    # Given a Shelf that holds no such book
    _shelve(tmp_path, "Dune", ["Frank Herbert"])

    # When an identity nothing answers for is updated
    # Then it fails rather than creating anything (ADR 0003)
    with pytest.raises(BookNotFound):
        update_book(tmp_path, "01J0000000000000000000000A", {"status": "Read"})
    assert len(list(tmp_path.glob("*.md"))) == 1


def test_a_multi_valued_field_takes_several_values(tmp_path):
    # Given a book owned on paper and listened to as an audiobook
    note = _shelve(tmp_path, "Dune", ["Frank Herbert"])

    # When both formats are recorded
    update_book(tmp_path, note.libris_id, {"format": ["Physical", "Audiobook"]})

    # Then both stand. Several at once is normal for a Format (ADR 0017).
    assert _read_back(tmp_path, note.libris_id).frontmatter["format"] == [
        "Physical",
        "Audiobook",
    ]


def test_a_superseded_identity_still_updates_the_survivor(tmp_path):
    # Given a note that absorbed another in a merge
    survivor = _shelve(tmp_path, "Dune", ["Frank Herbert"])
    dead_id = "01J0000000000000000000000A"
    raw = survivor.path.read_text(encoding="utf-8")
    marker = "superseded_ids:" + chr(10) + "- " + dead_id + chr(10) + "status:"
    survivor.path.write_text(raw.replace("status:", marker, 1), encoding="utf-8")

    # When the identity that was merged away is updated
    result = update_book(tmp_path, dead_id, {"status": "Read"})

    # Then it reaches the surviving note. An ID picked up before a merge still
    # applies rather than being rejected for a note Libris itself destroyed
    # (ADR 0014).
    assert result.note.libris_id == survivor.libris_id
    assert _read_back(tmp_path, survivor.libris_id).frontmatter["status"] == "Read"


def test_the_body_is_left_exactly_as_it_was(tmp_path):
    # Given a note whose body holds the reader's own writing, including a line
    # that looks like a frontmatter field
    note = _shelve(tmp_path, "Dune", ["Frank Herbert"])
    raw = note.path.read_text(encoding="utf-8")
    frontmatter = raw.split("---", 2)[1]
    body = (
        "# Dune"
        + chr(10) * 2
        + "## Notes"
        + chr(10) * 2
        + "Wrote this. status: unclear."
        + chr(10)
    )
    note.path.write_text("---" + frontmatter + "---" + chr(10) + body, encoding="utf-8")

    # When the status is updated
    update_book(tmp_path, note.libris_id, {"status": "Read"})

    # Then the body survives exactly, including that line. An MCP write reaches
    # frontmatter and nothing else (ADR 0023), and that line is the shape of the
    # bug in #92.
    assert note.path.read_text(encoding="utf-8").endswith(body)
