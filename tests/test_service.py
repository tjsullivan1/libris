"""Tests for the service layer the adapters sit over.

ADR 0008 puts resolution, creation and querying below the adapters, so the REST
surface and the MCP tools cannot drift apart by reimplementing matching. These
tests exercise that layer directly, with no HTTP involved.
"""

import sys
from datetime import date

import pytest

from libris import service
from libris.api import BookCandidate
from libris.markdown import (
    BookNote,
    FrontmatterUnreadable,
    create_book_note,
)
from libris.note_format import InvalidFieldValue
from libris.service import (
    MAX_SEARCH_LIMIT,
    BookNotFound,
    DecisionStatus,
    IsbnAgreement,
    Outcome,
    add_book,
    apply_decisions,
    build_lookup_query,
    find_by_libris_id,
    find_encoding_damage,
    find_existing,
    find_id_collisions,
    is_isbn10,
    search_library,
    update_book,
    update_note,
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


def test_a_primary_removed_before_its_merge_is_written_keeps_the_secondary(
    tmp_path, monkeypatch
):
    # Given a pair judged one Book, whose primary is removed between the merge
    # being worked out and written
    first, second = _pair(tmp_path)
    removed = []
    real = service.write_merged_book

    def _removed_first(primary_path, merged_frontmatter, merged_body, *args):
        removed.append(primary_path)
        primary_path.unlink()
        return real(primary_path, merged_frontmatter, merged_body, *args)

    monkeypatch.setattr(service, "write_merged_book", _removed_first)

    # When the decision is applied
    outcomes = apply_decisions(tmp_path, [_decision(first, second)])

    # Then the primary is not recreated, the secondary is not deleted, and the
    # decision is reported as drifted. Written with `write_note`, the primary
    # came back from memory and the secondary was deleted after it (#128).
    assert [o.status for o in outcomes] == [DecisionStatus.DRIFTED]
    assert not removed[0].exists()
    survivors = {first.path, second.path} - {removed[0]}
    assert all(path.exists() for path in survivors)


def test_a_note_removed_before_its_merge_is_read_drifts_rather_than_stopping(
    tmp_path, monkeypatch
):
    # Given a pair found in the index, one of them removed before the merge reads
    # it - after the index was built, so the lookup still finds it
    first, second = _pair(tmp_path)
    real = service.merge_two_books

    def _removed_first(primary_path, secondary_path, **kwargs):
        secondary_path.unlink()
        return real(primary_path, secondary_path, **kwargs)

    monkeypatch.setattr(service, "merge_two_books", _removed_first)

    # When the decision is applied
    outcomes = apply_decisions(tmp_path, [_decision(first, second)])

    # Then it is reported as drifted, rather than the read ending the batch. The
    # handler covered only the write, so a note vanishing before the merge read
    # it stopped every later decision (#131 review).
    assert [o.status for o in outcomes] == [DecisionStatus.DRIFTED]


def test_a_later_decision_naming_a_drifted_note_drifts_too(tmp_path, monkeypatch):
    # Given the same pair decided twice, and its primary removed during the first
    # merge's write - leaving the index still naming the vanished note
    first, second = _pair(tmp_path)
    real = service.write_merged_book
    calls = []

    def _removed_once(primary_path, merged_frontmatter, merged_body, *args):
        if not calls:
            primary_path.unlink()
        calls.append(primary_path)
        return real(primary_path, merged_frontmatter, merged_body, *args)

    monkeypatch.setattr(service, "write_merged_book", _removed_once)

    # When both decisions are applied
    outcomes = apply_decisions(
        tmp_path, [_decision(first, second), _decision(first, second)]
    )

    # Then both are drifted. The index still maps the vanished note's identity to
    # its old path, so the second decision's lookup succeeded and its merge read
    # a file that was gone, ending the batch (#131 review).
    assert [o.status for o in outcomes] == [
        DecisionStatus.DRIFTED,
        DecisionStatus.DRIFTED,
    ]


def test_a_merge_whose_secondary_vanished_before_deletion_is_still_merged(
    tmp_path, monkeypatch
):
    # Given a pair whose merged note is written, and whose secondary is removed
    # before it can be deleted
    first, second = _pair(tmp_path)
    real = service.delete_secondary_file

    def _already_gone(secondary_path, *args):
        secondary_path.unlink()
        return real(secondary_path, *args)

    monkeypatch.setattr(service, "delete_secondary_file", _already_gone)

    # When the decision is applied
    outcomes = apply_decisions(tmp_path, [_decision(first, second)])

    # Then it is reported as merged: the merged note was written, and the note it
    # would have deleted is already gone. The delete was outside the handler, so
    # this ended the batch with no outcome for the pair (#131 review).
    assert [o.status for o in outcomes] == [DecisionStatus.MERGED]
    assert len(list(tmp_path.glob("*.md"))) == 1


def test_a_merge_whose_secondary_was_moved_says_it_was_not_deleted(
    tmp_path, monkeypatch
):
    # Given a pair whose merged note is written, and whose secondary is moved -
    # not removed - before it can be deleted
    first, second = _pair(tmp_path)
    real = service.delete_secondary_file

    def _moved(secondary_path, *args):
        secondary_path.rename(
            secondary_path.with_name(f"{secondary_path.stem} moved.md")
        )
        return real(secondary_path, *args)

    monkeypatch.setattr(service, "delete_secondary_file", _moved)

    # When the decision is applied
    outcomes = apply_decisions(tmp_path, [_decision(first, second)])

    # Then it is merged - the merged note was written - but the outcome does not
    # imply the secondary is gone. A missing path cannot tell a deletion from a
    # move, and a moved copy is still on the Shelf carrying the merged note's
    # identity (#131 second review).
    assert [o.status for o in outcomes] == [DecisionStatus.MERGED]
    assert "not deleted" in outcomes[0].detail


def test_a_merged_note_removed_before_the_index_is_refreshed_records_the_merge(
    tmp_path, monkeypatch
):
    # Given a pair decided twice, whose merged note is removed after the merge is
    # written and the secondary deleted - before the index is refreshed
    first, second = _pair(tmp_path)
    real = service.delete_secondary_file

    def _then_merged_note_gone(secondary_path, *args):
        real(secondary_path, *args)
        for remaining in tmp_path.glob("*.md"):
            remaining.unlink()

    monkeypatch.setattr(service, "delete_secondary_file", _then_merged_note_gone)

    # When both decisions are applied
    outcomes = apply_decisions(
        tmp_path, [_decision(first, second), _decision(first, second)]
    )

    # Then the first is recorded as merged and the second as drifted. Reading the
    # merged note back to refresh the index ran outside any handler, so the batch
    # ended with no outcome for either; and its stale index entries would have
    # sent the second decision to a note that was gone (#131 second review).
    assert [o.status for o in outcomes] == [
        DecisionStatus.MERGED,
        DecisionStatus.DRIFTED,
    ]


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


@pytest.fixture(params=["by identity", "by path"])
def update(request, tmp_path):
    """Update a note through each entry point in turn (#125).

    `update_book` and `update_note` differ only in how they find the note, so
    every rule about what an update means is checked through both. A rule that
    held for one and not the other would be #97 again, one layer down.
    """

    def _update(note, fields):
        if request.param == "by identity":
            return update_book(tmp_path, note.libris_id, fields)
        return update_note(note.path, fields)

    return _update


def test_a_named_field_is_set(tmp_path, update):
    # Given a book waiting to be read
    note = _shelve(tmp_path, "Dune", ["Frank Herbert"], status="To Read")

    # When its status is moved on
    update(note, {"status": "Reading"})

    # Then the Shelf holds the new value
    assert _read_back(tmp_path, note.libris_id).frontmatter["status"] == "Reading"


def test_fields_that_were_not_named_are_left_alone(tmp_path, update):
    # Given a book carrying a rating nobody mentioned
    note = _shelve(tmp_path, "Dune", ["Frank Herbert"], status="To Read", rating=5)

    # When only the status is set
    update(note, {"status": "Reading"})

    # Then the rating survives. An update names only the fields it changes, so
    # it can never overwrite a field it knows nothing about.
    assert _read_back(tmp_path, note.libris_id).frontmatter["rating"] == 5


def test_finishing_a_book_stamps_the_date_and_says_so(tmp_path, update):
    # Given a book being read, with no finish date
    note = _shelve(tmp_path, "Dune", ["Frank Herbert"], status="Reading")

    # When it is marked Read without a date
    result = update(note, {"status": "Read"})

    # Then today is stamped, and reported as something the caller did not ask
    # for - so a person who meant last Tuesday can correct it (ADR 0024)
    today = date.today().isoformat()
    assert _read_back(tmp_path, note.libris_id).frontmatter["date_finished"] == today
    assert result.derived == {"date_finished": today}


def test_starting_a_book_stamps_the_started_date(tmp_path, update):
    # Given a book nobody has opened
    note = _shelve(tmp_path, "Dune", ["Frank Herbert"], status="To Read")

    # When it is marked Reading
    result = update(note, {"status": "Reading"})

    # Then the start date is stamped and disclosed
    assert result.derived == {"date_started": date.today().isoformat()}


def test_an_explicit_date_is_never_overridden(tmp_path, update):
    # Given a book being read
    note = _shelve(tmp_path, "Dune", ["Frank Herbert"], status="Reading")

    # When it is marked Read with the date it was actually finished
    result = update(note, {"status": "Read", "date_finished": "2026-08-25"})

    # Then that date stands and nothing is derived. The stamp is a fallback,
    # never an override (ADR 0024).
    written = _read_back(tmp_path, note.libris_id).frontmatter["date_finished"]
    assert str(written) == "2026-08-25"
    assert result.derived == {}


def test_a_date_already_on_the_note_is_not_restamped(tmp_path, update):
    # Given a book finished years ago
    note = _shelve(
        tmp_path, "Dune", ["Frank Herbert"], status="Read", date_finished="2019-04-01"
    )

    # When its status is set to Read once more
    result = update(note, {"status": "Read"})

    # Then the original date survives. A re-read is not something the Library
    # models, and inventing one here would be scope creep (ADR 0024).
    written = _read_back(tmp_path, note.libris_id).frontmatter["date_finished"]
    assert str(written) == "2019-04-01"
    assert result.derived == {}


def test_a_value_the_library_does_not_define_is_refused(tmp_path, update):
    # Given a status outside the four the Library allows
    note = _shelve(tmp_path, "Dune", ["Frank Herbert"])

    # When it is written
    # Then it is refused. `libris update` offers "Finished" to this day, which
    # is the drift ADR 0022 exists to stop.
    with pytest.raises(InvalidFieldValue):
        update(note, {"status": "Finished"})


def test_a_field_that_is_not_the_readers_is_refused(tmp_path, update):
    # Given a field describing the edition rather than the reading of it
    note = _shelve(tmp_path, "Dune", ["Frank Herbert"])

    # When it is written
    # Then it is refused: Libris owns the title (ADR 0012), and bibliographic
    # fields come from enrichment rather than from someone talking.
    with pytest.raises(ValueError):
        update(note, {"title": "Doon"})


def test_a_null_does_not_silently_clear_a_field(tmp_path, update):
    # Given a book carrying a rating
    note = _shelve(tmp_path, "Dune", ["Frank Herbert"], rating=5)

    # When a null arrives for it
    # Then it is refused rather than treated as "clear this". A model emitting
    # null for "unchanged" would otherwise erase a field nobody mentioned.
    with pytest.raises(ValueError):
        update(note, {"rating": None})
    assert _read_back(tmp_path, note.libris_id).frontmatter["rating"] == 5


def test_an_unknown_identity_is_not_found(tmp_path):
    # Given a Shelf that holds no such book
    _shelve(tmp_path, "Dune", ["Frank Herbert"])

    # When an identity nothing answers for is updated
    # Then it fails rather than creating anything (ADR 0003)
    with pytest.raises(BookNotFound):
        update_book(tmp_path, "01J0000000000000000000000A", {"status": "Read"})
    assert len(list(tmp_path.glob("*.md"))) == 1


def test_a_multi_valued_field_takes_several_values(tmp_path, update):
    # Given a book owned on paper and listened to as an audiobook
    note = _shelve(tmp_path, "Dune", ["Frank Herbert"])

    # When both formats are recorded
    update(note, {"format": ["Physical", "Audiobook"]})

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


def test_the_body_is_left_exactly_as_it_was(tmp_path, update):
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
    update(note, {"status": "Read"})

    # Then the body survives exactly, including that line. An MCP write reaches
    # frontmatter and nothing else (ADR 0023), and that line is the shape of the
    # bug in #92.
    assert note.path.read_text(encoding="utf-8").endswith(body)


def test_an_update_by_path_writes_the_note_it_was_handed(tmp_path):
    # Given two notes claiming one Libris ID, as the real Shelf has held (#75)
    for name in ("Aaa First.md", "Zzz Second.md"):
        (tmp_path / name).write_text(
            "---\nlibris_id: 01AAAAAAAAAAAAAAAAAAAAAAAA\n"
            f"title: {name[:-3]}\nauthors:\n  - Someone\nstatus: To Read\n---\n\nMine.\n",
            encoding="utf-8",
        )

    # When the second is updated by its path
    result = update_note(tmp_path / "Zzz Second.md", {"status": "Read"})

    # Then the second is written and the first is not. By identity, this reached
    # whichever claimant the scan found first (#125).
    assert result.note.path == tmp_path / "Zzz Second.md"
    assert BookNote.read(tmp_path / "Zzz Second.md").frontmatter["status"] == "Read"
    assert BookNote.read(tmp_path / "Aaa First.md").frontmatter["status"] == "To Read"


def test_an_update_by_path_to_a_file_that_is_gone_is_not_found(tmp_path):
    # Given a path where a note was picked and has since gone - renamed in
    # Obsidian, or moved by a sync client
    gone = tmp_path / "Dune - Frank Herbert.md"

    # When it is updated
    # Then it is a miss, the same as an identity nothing holds, and nothing is
    # created in its place (ADR 0003)
    with pytest.raises(BookNotFound):
        update_note(gone, {"status": "Read"})
    assert list(tmp_path.glob("*.md")) == []


def test_a_note_gone_before_the_write_is_not_found(tmp_path, monkeypatch, update):
    # Given a note that is found, then removed before the write reaches it - the
    # index answering for a file that has since moved, or a picked file renamed
    note = _shelve(tmp_path, "Dune", ["Frank Herbert"], status="To Read")
    real = service.set_frontmatter_fields

    def _removed_first(path, updates):
        path.unlink()
        real(path, updates)

    monkeypatch.setattr(service, "set_frontmatter_fields", _removed_first)

    # When it is updated
    # Then it is a miss rather than a raw FileNotFoundError, which neither
    # `libris status` nor the MCP tool catches (#127 review), and nothing is
    # created in its place (ADR 0003)
    with pytest.raises(BookNotFound):
        update(note, {"status": "Reading"})
    assert list(tmp_path.glob("*.md")) == []


def _during_the_write(monkeypatch, act):
    """Run `act` after an update has read its note and before it writes back.

    The YAML is rendered in exactly that gap, with the note held open. Windows
    refuses to rename or remove a file held open, so what these tests do to the
    note cannot happen there - which is the protection, not a gap in the tests.
    """
    import yaml

    real_dump = yaml.dump

    def _dump(*args, **kwargs):
        act()
        return real_dump(*args, **kwargs)

    monkeypatch.setattr(yaml, "dump", _dump)


_POSIX_ONLY = pytest.mark.skipif(
    sys.platform == "win32",
    reason="Windows refuses to rename or remove a file another handle holds open",
)


@_POSIX_ONLY
def test_a_note_removed_during_its_write_is_not_recreated(
    tmp_path, monkeypatch, update
):
    # Given a note removed after the update has read it
    note = _shelve(tmp_path, "Dune", ["Frank Herbert"], status="To Read")
    _during_the_write(monkeypatch, note.path.unlink)

    # When it is updated
    # Then it is a miss, and the note is not put back. Opening the path for
    # writing created it again under its old name and reported success (#127
    # review).
    with pytest.raises(BookNotFound):
        update(note, {"status": "Reading"})
    assert list(tmp_path.glob("*.md")) == []


@_POSIX_ONLY
def test_a_note_renamed_during_its_write_is_not_written(tmp_path, monkeypatch, update):
    # Given a note renamed away after the update has read it - Obsidian renaming
    note = _shelve(tmp_path, "Dune", ["Frank Herbert"], status="To Read")
    renamed = tmp_path / "Dune (renamed).md"
    _during_the_write(monkeypatch, lambda: note.path.rename(renamed))

    # When it is updated
    # Then it is a miss with nothing written, rather than a success reported for
    # a filename that no longer exists (#127 review)
    with pytest.raises(BookNotFound):
        update(note, {"status": "Reading"})
    assert BookNote.read(renamed).frontmatter["status"] == "To Read"
    assert not note.path.exists()


@_POSIX_ONLY
def test_a_note_replaced_during_its_write_leaves_the_replacement_alone(
    tmp_path, monkeypatch, update
):
    # Given a note renamed away after the update has read it, and a different
    # note saved at the same path
    note = _shelve(tmp_path, "Dune", ["Frank Herbert"], status="To Read")
    replacement = (
        "---\nlibris_id: 01BBBBBBBBBBBBBBBBBBBBBBBB\ntitle: Other\n"
        "status: To Read\n---\n\nSomeone else's.\n"
    )

    def _replace():
        note.path.rename(tmp_path / "Dune (renamed).md")
        note.path.write_text(replacement, encoding="utf-8")

    _during_the_write(monkeypatch, _replace)

    # When it is updated
    # Then neither note is written. What the update decided came from the note
    # it read; writing it into a file it never read would put one book's status
    # on another (#127 review).
    with pytest.raises(BookNotFound):
        update(note, {"status": "Reading"})
    assert note.path.read_text(encoding="utf-8") == replacement
    renamed = BookNote.read(tmp_path / "Dune (renamed).md")
    assert renamed.frontmatter["status"] == "To Read"


def test_an_update_by_identity_refuses_a_file_that_no_longer_holds_it(
    tmp_path, monkeypatch
):
    # Given a note the index resolves, whose file then holds a different note by
    # the time the update opens it - one renamed away, another saved in its place
    note = _shelve(tmp_path, "Dune", ["Frank Herbert"], status="To Read")
    replacement = (
        "---\nlibris_id: 01BBBBBBBBBBBBBBBBBBBBBBBB\ntitle: Other\n"
        "status: To Read\n---\n\nSomeone else's.\n"
    )
    real = service.set_frontmatter_fields

    def _replaced_first(path, updates):
        path.write_text(replacement, encoding="utf-8")
        return real(path, updates)

    monkeypatch.setattr(service, "set_frontmatter_fields", _replaced_first)

    # When the identity is updated
    # Then the other note is not written. The path was only ever where that
    # identity lived; the identity is what was asked for.
    with pytest.raises(BookNotFound):
        update_book(tmp_path, note.libris_id, {"status": "Reading"})
    assert note.path.read_text(encoding="utf-8") == replacement


def test_an_update_by_path_refuses_a_note_that_is_not_utf8(tmp_path):
    # Given a note saved in another encoding
    path = tmp_path / "Latin1.md"
    path.write_bytes("---\ntitle: Søren\nstatus: To Read\n---\n".encode("latin-1"))
    before = path.read_bytes()

    # When it is updated
    # Then it is refused as unreadable, the answer `status` knows how to give,
    # rather than a UnicodeDecodeError (#127 review) - and left exactly as it was
    with pytest.raises(FrontmatterUnreadable):
        update_note(path, {"status": "Read"})
    assert path.read_bytes() == before


def test_a_note_that_is_not_utf8_does_not_stop_the_library_answering(tmp_path):
    # Given a Shelf holding a book, and one note in another encoding
    _shelve(tmp_path, "Dune", ["Frank Herbert"])
    (tmp_path / "Latin1.md").write_bytes("---\ntitle: Søren\n---\n".encode("latin-1"))

    # When the Library is searched
    result = search_library(tmp_path, query="dune")

    # Then the book is found. The decode error escaped the index, so this one
    # file made every search, lookup and update by identity fail (#127 review).
    assert _titles(result) == ["Dune"]


def test_a_note_gone_after_the_write_still_reports_the_write(
    tmp_path, monkeypatch, update
):
    # Given a note removed the moment after it is written
    note = _shelve(tmp_path, "Dune", ["Frank Herbert"], status="To Read")
    real = service.set_frontmatter_fields

    def _removed_after(path, updates):
        written = real(path, updates)
        path.unlink()
        return written

    monkeypatch.setattr(service, "set_frontmatter_fields", _removed_after)

    # When it is updated
    result = update(note, {"status": "Reading"})

    # Then the answer is the write that happened, not a miss. Calling it not
    # found would tell a person their book was never marked, when it was.
    assert result.note.frontmatter["status"] == "Reading"
    assert result.note.frontmatter["date_started"] == date.today().isoformat()
    assert result.note.libris_id == note.libris_id


def test_an_update_by_path_refuses_a_note_it_cannot_parse(tmp_path):
    # Given a note whose frontmatter is broken
    path = tmp_path / "Broken.md"
    path.write_text("---\ntitle: [unclosed\n---\n\nMine.\n", encoding="utf-8")
    before = path.read_bytes()

    # When it is updated
    # Then it is refused and left exactly as it was. By identity such a note is
    # simply not found, because the index skips it; by path it is in hand, so
    # the reason is the one worth giving.
    with pytest.raises(FrontmatterUnreadable):
        update_note(path, {"status": "Read"})
    assert path.read_bytes() == before


def test_an_update_by_path_does_not_consult_the_shelf(tmp_path, monkeypatch):
    # Given a note, and a Shelf index that fails if anything asks it
    note = _shelve(tmp_path, "Dune", ["Frank Herbert"], status="To Read")

    def _refuse(_vault_path):
        raise AssertionError("an update by path built the Shelf index")

    monkeypatch.setattr(service, "index_for", _refuse)

    # When it is updated by path
    update_note(note.path, {"status": "Reading"})

    # Then it is written without the whole Shelf being parsed to find a note the
    # caller already had - 6 to 14 seconds on the real Shelf (#125)
    assert BookNote.read(note.path).frontmatter["status"] == "Reading"


# --- adding over a Near Match (ADR 0026) ---


def test_a_near_match_stops_the_write_when_asked_to(tmp_path):
    # Given a Library already holding something that may be this Book
    create_book_note(_candidate(title="The Brass Verdict: A Novel"), tmp_path)

    # When a Surface that wants to ask first tries to add it
    result = add_book(
        tmp_path, _candidate(title="The Brass Verdict"), stop_on_near_match=True
    )

    # Then nothing is written and the near matches come back for a person to
    # settle. Over MCP the caller is a model, so a near match reported after the
    # write arrives too late to be a question (ADR 0026).
    assert result.outcome is Outcome.NEEDS_CONFIRMATION
    assert [n.title for n in result.near_matches] == ["The Brass Verdict: A Novel"]
    assert result.libris_id is None
    assert len(list(tmp_path.glob("*.md"))) == 1


def test_a_near_match_does_not_stop_the_write_by_default(tmp_path):
    # Given the same Library
    create_book_note(_candidate(title="The Brass Verdict: A Novel"), tmp_path)

    # When a Surface with a person already looking at the near matches adds it -
    # which is what the extension's popup does
    result = add_book(tmp_path, _candidate(title="The Brass Verdict"))

    # Then it writes. The two adapters differ deliberately, and this is the REST
    # half of ADR 0026.
    assert result.outcome is Outcome.CREATED
    assert len(list(tmp_path.glob("*.md"))) == 2


def test_stopping_is_not_triggered_when_nothing_is_near(tmp_path):
    # Given a Library holding something else entirely
    create_book_note(_candidate(title="Dune"), tmp_path)

    # When an unrelated Book is added by a Surface that would have asked
    result = add_book(
        tmp_path, _candidate(title="Neuromancer"), stop_on_near_match=True
    )

    # Then it writes without a question. The confirmation is the price of an
    # ambiguity, not of every add.
    assert result.outcome is Outcome.CREATED
    assert result.near_matches == []


def test_a_book_already_held_is_reported_as_held_not_as_a_question(tmp_path):
    # Given a Library that exactly holds this Book
    create_book_note(_candidate(isbn="9780441013593"), tmp_path)

    # When it is added again by a Surface that would have asked
    result = add_book(
        tmp_path, _candidate(isbn="9780441013593"), stop_on_near_match=True
    )

    # Then the exact check answers first. Asking "did you mean this one?" about a
    # Book the Library provably already holds is a question with no useful answer.
    assert result.outcome is Outcome.ALREADY_PRESENT
    assert result.libris_id is not None


def test_confirming_writes_even_over_a_near_match(tmp_path):
    # Given a near match that stopped a first attempt
    create_book_note(_candidate(title="The Brass Verdict: A Novel"), tmp_path)
    stopped = add_book(
        tmp_path, _candidate(title="The Brass Verdict"), stop_on_near_match=True
    )
    assert stopped.outcome is Outcome.NEEDS_CONFIRMATION

    # When the person says it is a different Book
    result = add_book(
        tmp_path, _candidate(title="The Brass Verdict"), stop_on_near_match=False
    )

    # Then it writes. Confirmation is the second call, not a flag the first one
    # carried.
    assert result.outcome is Outcome.CREATED
    assert len(list(tmp_path.glob("*.md"))) == 2


def test_an_empty_string_does_not_clear_a_field(tmp_path, update):
    # Given a book carrying a start date
    note = _shelve(tmp_path, "Dune", ["Frank Herbert"], date_started="2019-04-01")

    # When an empty string arrives for it
    # Then it is refused. Null is the obvious way to erase a field by accident;
    # the empty string is the quiet one, and it passes every vocabulary check
    # because the field has no vocabulary.
    with pytest.raises(ValueError):
        update(note, {"date_started": ""})
    written = _read_back(tmp_path, note.libris_id).frontmatter["date_started"]
    assert str(written) == "2019-04-01"


def test_an_empty_list_does_not_clear_a_multi_valued_field(tmp_path, update):
    # Given a book recorded in two formats
    note = _shelve(tmp_path, "Dune", ["Frank Herbert"], format=["Physical", "Ebook"])

    # When an empty list arrives for it
    # Then it is refused. This one is quieter still: an empty list passes
    # validation entry by entry because there are no entries, and then
    # normalize_field_value turns it into None - a clear nobody asked for.
    with pytest.raises(ValueError):
        update(note, {"format": []})
    assert _read_back(tmp_path, note.libris_id).frontmatter["format"] == [
        "Physical",
        "Ebook",
    ]


def test_a_rating_of_zero_is_a_value_not_an_absence(tmp_path, update):
    # Given a book with no rating
    note = _shelve(tmp_path, "Dune", ["Frank Herbert"])

    # When it is rated zero
    update(note, {"rating": 0})

    # Then it is written. Emptiness is tested by type rather than truthiness,
    # because a zero and a False are things a reader can mean.
    assert _read_back(tmp_path, note.libris_id).frontmatter["rating"] == 0


def test_listing_by_filter_does_not_tokenize_every_note(tmp_path, monkeypatch):
    # Given a Shelf and a count of how often a note's words are split
    for title in ("Dune", "Piranesi", "Mercy"):
        _shelve(tmp_path, title, ["Someone"], status="To Read")

    calls = []
    real = service._search_tokens
    monkeypatch.setattr(
        service, "_search_tokens", lambda text: (calls.append(text), real(text))[1]
    )

    # When the Library is listed by status alone
    result = service.search_library(tmp_path, status="To Read")

    # Then nothing was tokenized. Listing "To Read" walks 1,452 notes on the
    # real Shelf, and splitting every title and author to then sort them
    # alphabetically is work with no reader.
    assert len(result.books) == 3
    assert calls == []


def test_a_format_written_as_a_bare_string_is_repaired_not_refused(tmp_path, update):
    # Given a note whose format is a bare string, which two notes on the real
    # Shelf hold and which Obsidian can write at any time (ADR 0017)
    note = _shelve(tmp_path, "Dune", ["Frank Herbert"])
    raw = note.path.read_text(encoding="utf-8")
    note.path.write_text(
        raw.replace("format: null", "format: Physical", 1), encoding="utf-8"
    )

    # When a second format is recorded
    update(note, {"format": ["Physical", "Audiobook"]})

    # Then the write lands
    assert _read_back(tmp_path, note.libris_id).frontmatter["format"] == [
        "Physical",
        "Audiobook",
    ]


def test_a_format_is_repaired_before_it_is_judged(tmp_path, update):
    # Given a format in a shape and case the Library repairs elsewhere
    note = _shelve(tmp_path, "Dune", ["Frank Herbert"])

    # When it arrives that way
    update(note, {"format": ["physical", "EBOOK"]})

    # Then it is repaired and written, rather than refused for a spelling the
    # Library corrects on every other pass. Validating before normalizing judged
    # one value and wrote a different one.
    assert _read_back(tmp_path, note.libris_id).frontmatter["format"] == [
        "Physical",
        "Ebook",
    ]


def test_a_format_of_nothing_recognisable_is_still_refused(tmp_path, update):
    # Given a format holding no value the Library defines
    note = _shelve(tmp_path, "Dune", ["Frank Herbert"], format=["Physical"])

    # When it arrives
    # Then it is refused rather than normalized away to nothing. Repairing before
    # judging must not become a second route to clearing a field.
    with pytest.raises(ValueError):
        update(note, {"format": ["papyrus"]})
    assert _read_back(tmp_path, note.libris_id).frontmatter["format"] == ["Physical"]


# --- an ISBN is an identifier, not a number (#105) --------------------------


def _note_with_isbn(vault, name, isbn_line):
    """Write a Book Note whose isbn line is spelled exactly as given."""
    path = vault / name
    path.write_text(
        "---\n"
        f"libris_id: lb-{name[:4]}\n"
        "title: A Book\n"
        "authors:\n"
        "  - An Author\n"
        f"{isbn_line}\n"
        "status: To Read\n"
        "---\n\nBody.\n",
        encoding="utf-8",
    )
    return path


@pytest.mark.parametrize(
    "isbn_line",
    [
        "isbn: 786937521",  # unquoted: YAML hands this back as an int
        'isbn: "786937521"',
        "isbn: 786-937-521",  # hyphens are group separators, not information
        'isbn: "  786937521  "',
    ],
    ids=["bare-int", "quoted", "hyphenated", "padded"],
)
def test_a_book_is_found_by_isbn_however_the_note_spells_it(tmp_path, isbn_line):
    # Given a Shelf holding one note, whose ISBN is written in one of the shapes
    # the real Shelf actually uses
    vault = tmp_path / "shelf"
    vault.mkdir()
    _note_with_isbn(vault, "book.md", isbn_line)

    # When the Library is asked for that ISBN as a string, which is what every
    # Surface passes
    found = find_existing(vault, isbn="786937521")

    # Then the note is found. It was not: `786937521 == "786937521"` is False,
    # so 31 notes on the real Shelf were invisible to this lookup and add_book
    # would have written a second note for a book already held.
    assert found is not None
    assert found.title == "A Book"


def test_an_isbn_lookup_still_misses_a_book_the_shelf_does_not_hold(tmp_path):
    # Given a Shelf holding a different book
    vault = tmp_path / "shelf"
    vault.mkdir()
    _note_with_isbn(vault, "other.md", "isbn: 9780000000001")

    # When a different ISBN is looked up
    # Then it is a miss, rather than the normalisation making everything match
    assert find_existing(vault, isbn="786937521") is None


# --- reporting characters lost to a bad decode (#78) ------------------------


def _damaged_note(vault, name, frontmatter, body="## Notes\n\nMine.\n"):
    """Write a Book Note verbatim, damage and all."""
    path = vault / name
    path.write_text(f"---\n{frontmatter}\n---\n\n{body}", encoding="utf-8")
    return path


# --- proposing a repair for what was lost (#78) -----------------------------


class _StubClient:
    """A Google Books client that answers with one volume, and counts asks."""

    def __init__(self, volume=None):
        self.volume = volume
        self.asked = []

    def get_volume(self, google_books_id):
        self.asked.append(google_books_id)
        return self.volume

    def search(self, query):
        self.asked.append(query)
        return [self.volume] if self.volume else []


def test_a_candidate_that_restores_the_lost_letters_fits():
    # Given a name that lost its ø, and the spelling the API holds
    # Then it fits: the two agree everywhere the file still knows what it held
    assert service.fits_lost_characters("S�ren Kierkegaard", "Søren Kierkegaard")


def test_a_candidate_that_differs_elsewhere_does_not_fit():
    # Given a volume whose author differs beyond the lost character
    # Then it does not fit. A plausible-looking volume is not evidence about a
    # letter nobody can read (ADR 0003).
    assert not service.fits_lost_characters("S�ren Kierkegaard", "Anne Kierkegaard")
    assert not service.fits_lost_characters("S�ren", "Søren Kierkegaard")


def test_a_lost_character_stands_for_one_character_not_for_any_amount_of_text():
    # Given a volume whose author agrees at both ends but carries a whole extra
    # name where the note lost a single letter
    # Then it does not fit. A replacement character is one character that could
    # not be decoded - written as one or two - and letting it stand for any run
    # of text would propose "Sebastian Loren Kierkegaard" for Søren.
    assert not service.fits_lost_characters(
        "S�ren Kierkegaard", "Sebastian Loren Kierkegaard"
    )

    # And the two-character case still fits, because a lost letter read as
    # Latin-1 arrives as two characters
    assert service.fits_lost_characters("S�ren", "Søren")
    assert service.fits_lost_characters("Gr�gory", "Grégory")


def test_a_candidate_still_carrying_the_damage_does_not_fit():
    # Given an API answer that is itself damaged, which would repair nothing
    assert not service.fits_lost_characters("S�ren", "S�ren")


def test_an_undamaged_string_is_never_proposed_for():
    # Given a string that lost nothing, there is nothing to restore
    assert not service.fits_lost_characters("Søren", "Søren")


def test_a_typed_correction_is_taken_as_the_reader_typed_it():
    # Given a reader who typed the letter no source holds. Measured against the
    # real Shelf, the API accounts for 3 of 57 damaged strings, so this is the
    # ordinary case rather than the fallback (#78).
    assert service.accept_correction("Po Ch�-i", "Po Chü-i") == "Po Chü-i"

    # And surrounding whitespace is not part of what they meant
    assert service.accept_correction("Po Ch�-i", "  Po Chü-i  ") == "Po Chü-i"


def test_an_empty_answer_leaves_the_string_alone():
    # Given a reader who does not know this one either
    # Then nothing is written for it, rather than an empty field
    assert service.accept_correction("Po Ch�-i", "") is None
    assert service.accept_correction("Po Ch�-i", "   ") is None


def test_an_answer_that_changes_nothing_writes_nothing():
    # Given an answer identical to the damage, which repairs nothing
    assert service.accept_correction("Po Ch�-i", "Po Ch�-i") is None


def test_an_answer_still_carrying_the_damage_is_refused():
    # Given an answer that fixed one lost character and left another - easy to
    # do when a long description carries eight of them
    # Then it is refused rather than written back as a repair
    assert service.accept_correction("m�t�ores", "mét�ores") is None


def test_a_repair_is_proposed_for_the_title_and_the_author(tmp_path):
    # Given a note whose title and author each lost a character
    vault = tmp_path / "shelf"
    vault.mkdir()
    _damaged_note(
        vault,
        "either-or.md",
        'title: "Either-Or. Stages on Life\'s Way"\n'
        'authors:\n  - "S�ren Kierkegaard"\ngoogle_books_id: vol1',
    )
    damage = find_encoding_damage(vault)[0]
    client = _StubClient(
        BookCandidate(
            title="Either-Or. Stages on Life's Way",
            authors=["Søren Kierkegaard"],
            google_books_id="vol1",
        )
    )

    # When the volume it names is asked
    proposal = service.propose_encoding_repair(damage, client)

    # Then the author is offered, fetched by the id the note carries
    assert client.asked == ["vol1"]
    assert [(r.field, r.proposed) for r in proposal.fields] == [
        ("authors", "Søren Kierkegaard")
    ]

    # And nothing is written by proposing it
    assert "�" in (vault / "either-or.md").read_text(encoding="utf-8")


def test_a_repair_is_proposed_for_a_rendered_heading_and_a_callout(tmp_path):
    # Given a note whose body carries the damage in machine-written lines: the
    # H1 rendered from the title, and the description callout from the API
    vault = tmp_path / "shelf"
    vault.mkdir()
    _damaged_note(
        vault,
        "discourse.md",
        'title: "Helkavirsi� (Whitsongs)"\ngoogle_books_id: vol1',
        body=(
            "# Helkavirsi� (Whitsongs)\n\n## Notes\n\nMine, and m�thode is my own.\n\n"
            "> [!abstract]- Description\n> ...(French: Discours de la m�thode)...\n"
        ),
    )
    damage = find_encoding_damage(vault)[0]
    client = _StubClient(
        BookCandidate(
            title="Helkavirsiä (Whitsongs)",
            authors=["René Descartes"],
            google_books_id="vol1",
            description="...(French: Discours de la méthode)...",
        )
    )

    # When the volume is asked
    proposal = service.propose_encoding_repair(damage, client)

    # Then both machine-written lines are offered
    assert [r.proposed for r in proposal.body] == [
        "# Helkavirsiä (Whitsongs)",
        "> ...(French: Discours de la méthode)...",
    ]

    # And the reader's own sentence is not - nothing here can speak for it, so
    # it is reported as left behind rather than guessed at
    assert proposal.unrepaired == ["Mine, and m�thode is my own."]


def test_a_note_naming_no_identifier_is_not_proposed_for(tmp_path):
    # Given a damaged note carrying neither a volume id nor an ISBN
    vault = tmp_path / "shelf"
    vault.mkdir()
    _damaged_note(vault, "orphan.md", 'title: "A�B"')
    damage = find_encoding_damage(vault)[0]
    client = _StubClient(BookCandidate(title="AxB", authors=[]))

    # When a repair is sought
    # Then the API is not asked at all: there is nothing to ask it about
    assert service.propose_encoding_repair(damage, client) is None
    assert client.asked == []


def test_a_sentinel_volume_id_is_not_something_to_ask_about(tmp_path):
    # Given a note recording that Google Books has no such book. 41 notes on the
    # real Shelf carry this, and 47 carry `_not_a_book`; both are answers
    # somebody wrote down, not missing ids.
    vault = tmp_path / "shelf"
    vault.mkdir()
    _damaged_note(
        vault,
        "absent.md",
        'title: "S�ren"\ngoogle_books_id: _not_found_in_google_books_api',
    )
    damage = find_encoding_damage(vault)[0]
    client = _StubClient(BookCandidate(title="Søren", authors=[]))

    # Then it is not an identifier, and the API is never asked. Read literally
    # it is a malformed id, which Google answers with 503 rather than 404 - so
    # asking costs three retries to be told what the note already said.
    assert damage.identifier is None
    assert service.propose_encoding_repair(damage, client) is None
    assert client.asked == []


def test_a_sentinel_volume_id_still_leaves_an_isbn_worth_asking(tmp_path):
    # Given a note whose volume id is a sentinel but which names an ISBN
    vault = tmp_path / "shelf"
    vault.mkdir()
    _damaged_note(
        vault,
        "by-isbn.md",
        'title: "S�ren"\ngoogle_books_id: _not_a_book\nisbn: "9780000000001"',
    )
    damage = find_encoding_damage(vault)[0]
    client = _StubClient(BookCandidate(title="Søren", authors=[]))

    # When a repair is sought
    proposal = service.propose_encoding_repair(damage, client)

    # Then the ISBN is what gets asked, rather than the sentinel. A book Google
    # Books holds no volume for may still be findable by its ISBN.
    assert damage.identifier == "9780000000001"
    assert client.asked == ["isbn:9780000000001"]
    assert [item.proposed for item in proposal.fields] == ["Søren"]


def test_a_volume_that_agrees_with_nothing_offers_nothing(tmp_path):
    # Given a note whose recorded volume describes a different book
    vault = tmp_path / "shelf"
    vault.mkdir()
    _damaged_note(vault, "wrong.md", 'title: "S�ren"\ngoogle_books_id: vol1')
    damage = find_encoding_damage(vault)[0]
    client = _StubClient(BookCandidate(title="Dune", authors=["Frank Herbert"]))

    # When it is asked
    proposal = service.propose_encoding_repair(damage, client)

    # Then the proposal is empty rather than a guess at the title
    assert proposal.is_empty
    assert proposal.unrepaired == ["S�ren"]


def test_applying_a_repair_writes_frontmatter_and_body_in_one_write(
    tmp_path, monkeypatch
):
    # Given a confirmed proposal touching the title and the heading rendered
    # from it, and a count of every write that reaches a note
    from libris import markdown

    vault = tmp_path / "shelf"
    vault.mkdir()
    path = _damaged_note(
        vault,
        "discourse.md",
        'title: "Discours de la m�thode"\ngoogle_books_id: vol1',
        body="# Discours de la m�thode\n\n## Notes\n\nMine.\n",
    )
    damage = find_encoding_damage(vault)[0]
    client = _StubClient(
        BookCandidate(
            title="Discours de la méthode", authors=[], google_books_id="vol1"
        )
    )
    proposal = service.propose_encoding_repair(damage, client)

    writes = []
    real = markdown._write_through
    monkeypatch.setattr(
        markdown,
        "_write_through",
        lambda *args: (writes.append(args[1]), real(*args))[1],
    )

    # When it is applied
    assert service.apply_encoding_repair(proposal) == 2

    # Then both land in one write. Two writes would mean a note that moved in
    # between kept a repaired title under a damaged heading (#127 review).
    assert len(writes) == 1
    text = path.read_text(encoding="utf-8")
    assert "title: Discours de la méthode" in text
    assert "# Discours de la méthode" in text
    assert "�" not in text


def test_applying_a_repair_leaves_the_readers_prose_and_the_filename_alone(tmp_path):
    # Given a damaged note whose body also holds the reader's own writing
    vault = tmp_path / "shelf"
    vault.mkdir()
    path = _damaged_note(
        vault,
        "S�ren.md",
        'title: "S�ren"\ngoogle_books_id: vol1',
        body="## Notes\n\n    an indented block I wrote about S�ren\n",
    )
    damage = find_encoding_damage(vault)[0]
    client = _StubClient(
        BookCandidate(title="Søren", authors=[], google_books_id="vol1")
    )
    proposal = service.propose_encoding_repair(damage, client)

    # When it is applied
    service.apply_encoding_repair(proposal)

    # Then the title is repaired, the reader's line is untouched, and the file
    # keeps its name - renaming rewrites wikilinks and is its own work (#78)
    text = path.read_text(encoding="utf-8")
    assert "title: Søren" in text
    assert "    an indented block I wrote about S�ren\n" in text
    assert path.name == "S�ren.md"


def test_applying_an_empty_proposal_writes_nothing(tmp_path):
    # Given a proposal the volume could not fill
    vault = tmp_path / "shelf"
    vault.mkdir()
    path = _damaged_note(vault, "wrong.md", 'title: "S�ren"\ngoogle_books_id: vol1')
    before = path.read_bytes()
    damage = find_encoding_damage(vault)[0]
    proposal = service.propose_encoding_repair(
        damage, _StubClient(BookCandidate(title="Dune", authors=[]))
    )

    # When it is applied
    # Then it says it wrote nothing, and the note is untouched byte for byte
    assert service.apply_encoding_repair(proposal) == 0
    assert path.read_bytes() == before


def test_applying_a_repair_to_a_note_that_has_gone_is_not_found(tmp_path):
    # Given a proposal for a note removed since the report was built
    vault = tmp_path / "shelf"
    vault.mkdir()
    path = _damaged_note(vault, "gone.md", 'title: "S�ren"\ngoogle_books_id: vol1')
    damage = find_encoding_damage(vault)[0]
    proposal = service.propose_encoding_repair(
        damage, _StubClient(BookCandidate(title="Søren", authors=[]))
    )
    path.unlink()

    # When it is applied
    # Then it is a miss, and nothing is created in its place (ADR 0003)
    with pytest.raises(BookNotFound):
        service.apply_encoding_repair(proposal)
    assert list(vault.glob("*.md")) == []


# --- #129 review ----------------------------------------------------------


def _repair_of(vault, fields=(), body=()):
    """Build a repair for the one damaged note on a Shelf, from given answers."""
    damage = find_encoding_damage(vault)[0]
    return service.EncodingRepair(
        damage=damage, fields=_numbered(fields), body=_numbered(body)
    )


def _numbered(answers):
    """Repairs for answers given in order, each identical damaged string its own.

    Numbered the way `libris repair` numbers them when nothing is skipped: the
    second answer for the same damaged text is occurrence 1, not a second
    occurrence 0 that overwrites the first.
    """
    seen = {}
    repairs = []
    for field_name, damaged, proposed in answers:
        occurrence = seen.get((field_name, damaged), 0)
        seen[(field_name, damaged)] = occurrence + 1
        repairs.append(service.FieldRepair(field_name, damaged, proposed, occurrence))
    return repairs


def test_every_damaged_author_in_a_note_is_repaired(tmp_path):
    # Given a note two of whose authors each lost a character
    vault = tmp_path / "shelf"
    vault.mkdir()
    path = _damaged_note(
        vault,
        "two.md",
        'title: Poems\nauthors:\n  - "Po Ch�-i"\n  - "S�ren Kierkegaard"',
    )
    repair = _repair_of(
        vault,
        fields=[
            ("authors", "Po Ch�-i", "Po Chü-i"),
            ("authors", "S�ren Kierkegaard", "Søren Kierkegaard"),
        ],
    )

    # When both corrections are applied
    service.apply_encoding_repair(repair)

    # Then both land. Each correction rebuilt the list from the note as read,
    # so the second overwrote the first and only the last author was repaired
    # while `repair` reported two (#129 review).
    assert BookNote.read(path).frontmatter["authors"] == [
        "Po Chü-i",
        "Søren Kierkegaard",
    ]


def test_identical_damaged_lines_each_take_their_own_answer(tmp_path):
    # Given a note in which the same damaged line appears twice, and a reader
    # who knows the two meant different things
    vault = tmp_path / "shelf"
    vault.mkdir()
    path = _damaged_note(
        vault,
        "twice.md",
        'title: "Notes"',
        body="## Notes\n\nV�lez\n\nand later\n\nV�lez\n",
    )
    repair = _repair_of(
        vault,
        body=[("body", "V�lez", "Vélez"), ("body", "V�lez", "Vález")],
    )

    # When both are applied
    service.apply_encoding_repair(repair)

    # Then each occurrence takes the answer given for it, in order. Keyed by the
    # damaged text alone, the last answer landed on both (#129 review).
    lines = [
        line for line in path.read_text(encoding="utf-8").splitlines() if "lez" in line
    ]
    assert lines == ["Vélez", "Vález"]


def test_a_repaired_body_line_keeps_its_indentation(tmp_path):
    # Given a damaged line inside an indented block the reader wrote
    vault = tmp_path / "shelf"
    vault.mkdir()
    path = _damaged_note(
        vault,
        "indented.md",
        'title: "Notes"',
        body="## Notes\n\n    Discours de la m�thode\n",
    )
    repair = _repair_of(
        vault,
        body=[("body", "Discours de la m�thode", "Discours de la méthode")],
    )

    # When it is repaired
    service.apply_encoding_repair(repair)

    # Then only the letter changes. The indent made it a code block, and losing
    # it changes what the note renders - the same damage #99 fixed once.
    assert "\n    Discours de la méthode\n" in path.read_text(encoding="utf-8")


def test_a_repair_that_finds_nothing_to_change_says_it_wrote_nothing(tmp_path):
    # Given a report built before the reader fixed the title by hand
    vault = tmp_path / "shelf"
    vault.mkdir()
    path = _damaged_note(vault, "stale.md", 'title: "A�B"')
    repair = _repair_of(vault, fields=[("title", "A�B", "AxB")])
    path.write_text('---\ntitle: "edited since"\n---\n\n## Notes\n', encoding="utf-8")
    before = path.read_bytes()

    # When the stale repair is applied
    wrote = service.apply_encoding_repair(repair)

    # Then it reports that nothing was written, rather than a repair that did
    # not happen (#129 review) - and the reader's edit stands
    assert wrote == 0
    assert path.read_bytes() == before


def test_a_readers_own_heading_is_not_offered_the_volume_title(tmp_path):
    # Given a note whose rendered title heading is intact, and a later heading
    # the reader wrote that lost a character
    vault = tmp_path / "shelf"
    vault.mkdir()
    _damaged_note(
        vault,
        "own-heading.md",
        'title: "Either-Or"\ngoogle_books_id: vol1',
        body="# Either-Or\n\n## Notes\n\n# S�ren, as I read him\n",
    )
    damage = find_encoding_damage(vault)[0]
    client = _StubClient(
        BookCandidate(title="Søren, as I read him", authors=[], google_books_id="vol1")
    )

    # When the volume is asked
    proposal = service.propose_encoding_repair(damage, client)

    # Then the reader's heading is not offered the volume's title, however well
    # it fits. Only the heading rendered from the title is the Library's to
    # repair; prefilling this one would replace the reader's words at a keystroke
    # (#129 review).
    assert proposal.body == []


def test_a_quoted_line_outside_the_description_is_not_offered_the_blurb(tmp_path):
    # Given a quotation the reader wrote in their notes, and a volume whose
    # description happens to contain the same sentence
    vault = tmp_path / "shelf"
    vault.mkdir()
    _damaged_note(
        vault,
        "own-quote.md",
        'title: "Discourse"\ngoogle_books_id: vol1',
        body="## Notes\n\n> Discours de la m�thode\n",
    )
    damage = find_encoding_damage(vault)[0]
    client = _StubClient(
        BookCandidate(
            title="Discourse",
            authors=[],
            google_books_id="vol1",
            description="Discours de la méthode",
        )
    )

    # When the volume is asked
    proposal = service.propose_encoding_repair(damage, client)

    # Then nothing is offered for it. Only lines inside the description callout
    # came from the API (#129 review).
    assert proposal.body == []


# --- #129 second review -----------------------------------------------------


def test_a_skipped_author_keeps_its_place_when_a_later_twin_is_answered(tmp_path):
    # Given two authors that lost the same character in the same place, and a
    # reader who skipped the first and corrected only the second
    vault = tmp_path / "shelf"
    vault.mkdir()
    path = _damaged_note(
        vault,
        "twins.md",
        'title: Poems\nauthors:\n  - "V�lez"\n  - "V�lez"',
    )
    damage = find_encoding_damage(vault)[0]
    repair = service.EncodingRepair(
        damage=damage,
        fields=[service.FieldRepair("authors", "V�lez", "Vález", occurrence=1)],
    )

    # When the one answer is applied
    service.apply_encoding_repair(repair)

    # Then it lands on the second author, the one it was given for. Matched by
    # text alone it went to the first, which the reader had left alone (#129
    # second review).
    assert BookNote.read(path).frontmatter["authors"] == ["V�lez", "Vález"]


def test_a_skipped_body_line_keeps_its_place_when_a_later_twin_is_answered(tmp_path):
    # Given two identical damaged lines, the first skipped and the second answered
    vault = tmp_path / "shelf"
    vault.mkdir()
    path = _damaged_note(
        vault,
        "twin-lines.md",
        'title: "Notes"',
        body="## Notes\n\nV�lez\n\nand later\n\nV�lez\n",
    )
    damage = find_encoding_damage(vault)[0]
    repair = service.EncodingRepair(
        damage=damage,
        body=[service.FieldRepair("body", "V�lez", "Vález", occurrence=1)],
    )

    # When the one answer is applied
    service.apply_encoding_repair(repair)

    # Then the second line takes it and the first stays as the reader left it
    lines = [
        line for line in path.read_text(encoding="utf-8").splitlines() if "lez" in line
    ]
    assert lines == ["V�lez", "Vález"]


def test_a_damaged_genre_is_repaired(tmp_path):
    # Given a genre that lost a character. Genres is a list, and only authors
    # was handled as one, so a genre answer matched nothing and was dropped
    # while the command said the note had changed since (#129 second review).
    vault = tmp_path / "shelf"
    vault.mkdir()
    path = _damaged_note(
        vault,
        "genre.md",
        'title: Poems\ngenres:\n  - "Po�sie"\n  - Fiction',
    )
    damage = find_encoding_damage(vault)[0]
    repair = service.EncodingRepair(
        damage=damage, fields=[service.FieldRepair("genres", "Po�sie", "Poésie")]
    )

    # When it is applied
    applied = service.apply_encoding_repair(repair)

    # Then it lands, and is counted
    assert BookNote.read(path).frontmatter["genres"] == ["Poésie", "Fiction"]
    assert applied == 1


def test_a_repair_counts_only_what_it_actually_changed(tmp_path):
    # Given two answers for an unchanged note, one of them for an author the
    # note does not hold. Set up without editing the note: a note changed since
    # it was reported now refuses every answer (#129 fifth review), so a hand
    # edit no longer produces a partial repair.
    vault = tmp_path / "shelf"
    vault.mkdir()
    path = _damaged_note(
        vault,
        "partial.md",
        'title: "A�B"\nauthors:\n  - "C�D"',
    )
    damage = find_encoding_damage(vault)[0]
    repair = service.EncodingRepair(
        damage=damage,
        fields=[
            service.FieldRepair("title", "A�B", "AxB"),
            service.FieldRepair("authors", "Z�Z", "ZyZ"),
        ],
    )

    # When both are applied
    applied = service.apply_encoding_repair(repair)

    # Then one change is reported, not the two answers submitted (#129 second
    # review)
    assert applied == 1
    frontmatter = BookNote.read(path).frontmatter
    assert frontmatter["title"] == "AxB"
    assert frontmatter["authors"] == ["C�D"]


def test_unrepaired_damage_is_told_apart_by_field(tmp_path):
    # Given a title and an author holding the same damaged text, and a volume
    # that can only speak for the title
    vault = tmp_path / "shelf"
    vault.mkdir()
    _damaged_note(
        vault,
        "same.md",
        'title: "S�ren"\nauthors:\n  - "S�ren"\ngoogle_books_id: vol1',
    )
    damage = find_encoding_damage(vault)[0]
    client = _StubClient(
        BookCandidate(title="Søren", authors=["Anonymous"], google_books_id="vol1")
    )

    # When the volume is asked
    proposal = service.propose_encoding_repair(damage, client)

    # Then the author is still reported as unrepaired. Keyed by text alone, the
    # title's repair hid it (#129 second review).
    assert proposal.unrepaired == ["S�ren"]
    assert [item.field for item in proposal.fields] == ["title"]


def test_only_the_heading_a_note_opens_with_counts_as_its_title(tmp_path):
    # Given a note with no generated title heading at all, and a heading further
    # down that the reader wrote
    vault = tmp_path / "shelf"
    vault.mkdir()
    _damaged_note(
        vault,
        "no-title-heading.md",
        'title: "Either-Or"\ngoogle_books_id: vol1',
        body="## Notes\n\nSome notes.\n\n# S�ren, as I read him\n",
    )
    damage = find_encoding_damage(vault)[0]
    client = _StubClient(
        BookCandidate(title="Søren, as I read him", authors=[], google_books_id="vol1")
    )

    # When the volume is asked
    proposal = service.propose_encoding_repair(damage, client)

    # Then the reader's heading is not taken for the title heading. The first H1
    # anywhere is not the heading the note opens with (#129 second review).
    assert proposal.body == []


# --- #129 third review ------------------------------------------------------


def test_a_note_without_frontmatter_is_reported_as_unwritable(tmp_path):
    # Given a file on the Shelf with no frontmatter at all, damaged in its text,
    # beside an ordinary damaged note
    vault = tmp_path / "shelf"
    vault.mkdir()
    (vault / "loose.md").write_text("Just text about m�thode.\n", encoding="utf-8")
    _damaged_note(vault, "fine.md", 'title: "A�B"')

    # When the Shelf is inspected
    found = {damage.path.name: damage for damage in find_encoding_damage(vault)}

    # Then the loose file says it cannot be written. Its damage is reported
    # under the body alone, so nothing marked it as beyond a repair and the
    # reader was prompted for answers that were then refused (#129 third review).
    assert found["loose.md"].writable is False
    assert found["fine.md"].writable is True


def test_an_opening_heading_that_is_not_the_title_is_not_offered_the_volume_title(
    tmp_path,
):
    # Given a note that opens with a heading the reader wrote, which is not its
    # title
    vault = tmp_path / "shelf"
    vault.mkdir()
    _damaged_note(
        vault,
        "opens-with-own.md",
        'title: "Either-Or"\ngoogle_books_id: vol1',
        body="# S�ren, as I read him\n\n## Notes\n\nMine.\n",
    )
    damage = find_encoding_damage(vault)[0]
    client = _StubClient(
        BookCandidate(title="Søren, as I read him", authors=[], google_books_id="vol1")
    )

    # When the volume is asked
    proposal = service.propose_encoding_repair(damage, client)

    # Then it is not offered the volume's title. A generated heading is the
    # title written as a heading; opening the note does not make a heading one
    # (#129 third review).
    assert proposal.body == []


def test_only_the_callout_copy_of_a_twin_line_is_offered_the_description(tmp_path):
    # Given a quotation in the reader's notes identical to a damaged line of the
    # description callout below it
    vault = tmp_path / "shelf"
    vault.mkdir()
    _damaged_note(
        vault,
        "twin-quote.md",
        'title: "Discourse"\ngoogle_books_id: vol1',
        body=(
            "## Notes\n\n> Discours de la m�thode\n\n"
            "> [!abstract]- Description\n> Discours de la m�thode\n"
        ),
    )
    damage = find_encoding_damage(vault)[0]
    client = _StubClient(
        BookCandidate(
            title="Discourse",
            authors=[],
            google_books_id="vol1",
            description="Discours de la méthode",
        )
    )

    # When the volume is asked
    proposal = service.propose_encoding_repair(damage, client)

    # Then only the second occurrence - the one inside the callout - is offered
    # the blurb. By text alone both matched (#129 third review).
    assert [(item.damaged, item.occurrence) for item in proposal.body] == [
        ("> Discours de la m�thode", 1)
    ]


def test_a_proposal_numbers_twin_authors_so_applying_it_repairs_both(tmp_path):
    # Given two authors that lost the same letter, and a volume that holds it
    vault = tmp_path / "shelf"
    vault.mkdir()
    path = _damaged_note(
        vault,
        "twin-authors.md",
        'title: Poems\nauthors:\n  - "V�lez"\n  - "V�lez"\ngoogle_books_id: vol1',
    )
    damage = find_encoding_damage(vault)[0]
    client = _StubClient(
        BookCandidate(title="Poems", authors=["Vélez"], google_books_id="vol1")
    )

    # When the proposal is built and applied as it stands
    proposal = service.propose_encoding_repair(damage, client)
    applied = service.apply_encoding_repair(proposal)

    # Then each twin is its own occurrence and both are repaired. Numbered 0 and
    # 0, the first was repaired twice, two were reported, and the second stayed
    # damaged (#129 third review).
    assert [item.occurrence for item in proposal.fields] == [0, 1]
    assert applied == 2
    assert BookNote.read(path).frontmatter["authors"] == ["Vélez", "Vélez"]


def test_a_twin_line_added_since_the_report_refuses_the_repair(tmp_path):
    # Given two identical damaged lines and an answer for the second, then a
    # third identical line added above them before the answer is applied
    vault = tmp_path / "shelf"
    vault.mkdir()
    path = _damaged_note(
        vault,
        "stale-twins.md",
        'title: "Notes"',
        body="## Notes\n\nV�lez\n\nV�lez\n",
    )
    damage = find_encoding_damage(vault)[0]
    repair = service.EncodingRepair(
        damage=damage,
        body=[service.FieldRepair("body", "V�lez", "Vález", occurrence=1)],
    )
    text = path.read_text(encoding="utf-8").replace(
        "## Notes\n", "## Notes\n\nV�lez\n", 1
    )
    path.write_text(text, encoding="utf-8")
    before = path.read_bytes()

    # When the answer is applied
    applied = service.apply_encoding_repair(repair)

    # Then nothing is written. Occurrence 1 now names a different line - the one
    # the reader skipped - and the numbering can only be trusted while the
    # damaged lines are as they were reported (#129 third review).
    assert applied == 0
    assert path.read_bytes() == before


def test_a_twin_author_added_since_the_report_refuses_the_repair(tmp_path):
    # Given two identical damaged authors and an answer for the second, then a
    # third identical author added at the front before it is applied
    vault = tmp_path / "shelf"
    vault.mkdir()
    path = _damaged_note(
        vault,
        "stale-authors.md",
        'title: Poems\nauthors:\n  - "V�lez"\n  - "V�lez"',
    )
    damage = find_encoding_damage(vault)[0]
    repair = service.EncodingRepair(
        damage=damage,
        fields=[service.FieldRepair("authors", "V�lez", "Vález", occurrence=1)],
    )
    text = path.read_text(encoding="utf-8").replace(
        "authors:\n", 'authors:\n  - "V�lez"\n', 1
    )
    path.write_text(text, encoding="utf-8")
    before = path.read_bytes()

    # When the answer is applied
    applied = service.apply_encoding_repair(repair)

    # Then nothing is written, for the same reason as a line added above its twin
    assert applied == 0
    assert path.read_bytes() == before


def test_two_answers_for_one_occurrence_apply_once(tmp_path):
    # Given a repair built by a caller that named the same author twice - both
    # answers for occurrence 0
    vault = tmp_path / "shelf"
    vault.mkdir()
    path = _damaged_note(
        vault,
        "doubled.md",
        'title: Poems\nauthors:\n  - "V�lez"\n  - Other',
    )
    damage = find_encoding_damage(vault)[0]
    repair = service.EncodingRepair(
        damage=damage,
        fields=[
            service.FieldRepair("authors", "V�lez", "Vélez", occurrence=0),
            service.FieldRepair("authors", "V�lez", "Vález", occurrence=0),
        ],
    )

    # When it is applied
    applied = service.apply_encoding_repair(repair)

    # Then the first answer lands and is counted once. The second found the
    # entry already repaired; letting it apply overwrote the first answer and
    # counted two repairs of one string (#129 third review).
    assert applied == 1
    assert BookNote.read(path).frontmatter["authors"] == ["Vélez", "Other"]


# --- #129 fourth review -----------------------------------------------------


def test_unrepaired_names_a_twin_left_behind(tmp_path):
    # Given two identical damaged authors and a repair offered for the second
    vault = tmp_path / "shelf"
    vault.mkdir()
    _damaged_note(
        vault,
        "twins-left.md",
        'title: Poems\nauthors:\n  - "V�lez"\n  - "V�lez"',
    )
    damage = find_encoding_damage(vault)[0]
    repair = service.EncodingRepair(
        damage=damage,
        fields=[service.FieldRepair("authors", "V�lez", "Vélez", occurrence=1)],
    )

    # When what it leaves behind is asked
    # Then the first twin is named. Keyed by field and text, the second's repair
    # hid it (#129 fourth review).
    assert repair.unrepaired == ["V�lez"]


def test_a_twin_author_swapped_since_the_report_refuses_the_repair(tmp_path):
    # Given two identical damaged authors and an answer for the second - then one
    # twin changed to another damaged name and a new twin added, which leaves the
    # count of the answered text unchanged while moving where occurrence 1 is
    vault = tmp_path / "shelf"
    vault.mkdir()
    path = _damaged_note(
        vault,
        "swapped-authors.md",
        'title: Poems\nauthors:\n  - "V�lez"\n  - "V�lez"',
    )
    damage = find_encoding_damage(vault)[0]
    repair = service.EncodingRepair(
        damage=damage,
        fields=[service.FieldRepair("authors", "V�lez", "Vález", occurrence=1)],
    )
    path.write_text(
        '---\ntitle: Poems\nauthors:\n  - "Gr�gory"\n  - "V�lez"\n'
        '  - "V�lez"\n---\n\n## Notes\n\nMine.\n',
        encoding="utf-8",
    )
    before = path.read_bytes()

    # When the answer is applied
    applied = service.apply_encoding_repair(repair)

    # Then nothing is written. Counting the answered text could not see the
    # change: only the whole sequence of damaged entries, as reported, proves an
    # occurrence still names the entry the reader answered (#129 fourth review).
    assert applied == 0
    assert path.read_bytes() == before


def test_a_twin_line_swapped_since_the_report_refuses_the_repair(tmp_path):
    # Given the same change made to damaged body lines
    vault = tmp_path / "shelf"
    vault.mkdir()
    path = _damaged_note(
        vault,
        "swapped-lines.md",
        'title: "Notes"',
        body="## Notes\n\nV�lez\n\nV�lez\n",
    )
    damage = find_encoding_damage(vault)[0]
    repair = service.EncodingRepair(
        damage=damage,
        body=[service.FieldRepair("body", "V�lez", "Vález", occurrence=1)],
    )
    path.write_text(
        '---\ntitle: "Notes"\n---\n\n## Notes\n\nGr�gory\n\nV�lez\n\nV�lez\n',
        encoding="utf-8",
    )
    before = path.read_bytes()

    # When the answer is applied
    applied = service.apply_encoding_repair(repair)

    # Then nothing is written, for the same reason
    assert applied == 0
    assert path.read_bytes() == before


def test_a_note_with_an_empty_frontmatter_mapping_is_writable(tmp_path):
    # Given a note whose frontmatter is present but empty, damaged in its body
    vault = tmp_path / "shelf"
    vault.mkdir()
    _damaged_note(vault, "empty.md", "{}", body="Discours de la m�thode\n")

    # When the Shelf is inspected
    damage = find_encoding_damage(vault)[0]

    # Then it is writable. An empty mapping is falsy, so asking whether the
    # mapping held anything called it unwritable - but a repair writes to it
    # without trouble (#129 fourth review).
    assert damage.writable is True


# --- #129 fifth review ------------------------------------------------------


def test_an_author_fixed_by_hand_then_re_added_refuses_the_repair(tmp_path):
    # Given a damaged author and an answer for it - then, before the answer is
    # applied, the author fixed by hand and a new damaged author of the same
    # text added after it. The damaged entries read exactly as reported.
    vault = tmp_path / "shelf"
    vault.mkdir()
    path = _damaged_note(vault, "re-added.md", 'title: Poems\nauthors:\n  - "V�lez"')
    damage = find_encoding_damage(vault)[0]
    repair = service.EncodingRepair(
        damage=damage,
        fields=[service.FieldRepair("authors", "V�lez", "Vélez")],
    )
    path.write_text(
        '---\ntitle: Poems\nauthors:\n  - "Vález"\n  - "V�lez"\n'
        "---\n\n## Notes\n\nMine.\n",
        encoding="utf-8",
    )
    before = path.read_bytes()

    # When the answer is applied
    applied = service.apply_encoding_repair(repair)

    # Then nothing is written. Every check that compared some part of the note -
    # counts, then the damaged sequence - had an edit it could not see; only the
    # note being exactly as it was when reported proves the answer still names
    # what the reader answered (#129 fifth review).
    assert applied == 0
    assert path.read_bytes() == before


def test_a_damaged_line_moved_out_of_the_callout_refuses_the_repair(tmp_path):
    # Given the volume's text proposed for a damaged line of the description
    # callout - then that line moved up into the reader's notes, and the callout
    # given clean text. The damaged lines read exactly as reported.
    vault = tmp_path / "shelf"
    vault.mkdir()
    path = _damaged_note(
        vault,
        "moved.md",
        'title: "Discourse"',
        body=(
            "## Notes\n\nMine.\n\n"
            "> [!abstract]- Description\n> Discours de la m�thode\n"
        ),
    )
    damage = find_encoding_damage(vault)[0]
    repair = service.EncodingRepair(
        damage=damage,
        body=[
            service.FieldRepair(
                "body", "> Discours de la m�thode", "> Discours de la méthode"
            )
        ],
    )
    path.write_text(
        '---\ntitle: "Discourse"\n---\n\n## Notes\n\n> Discours de la m�thode\n\n'
        "> [!abstract]- Description\n> A clean blurb.\n",
        encoding="utf-8",
    )
    before = path.read_bytes()

    # When the proposal is applied
    applied = service.apply_encoding_repair(repair)

    # Then the reader's quotation is not given the volume's text (#129 fifth
    # review)
    assert applied == 0
    assert path.read_bytes() == before


def test_a_legacy_integer_volume_id_is_read_as_text(tmp_path):
    # Given an older note whose volume id YAML reads as a number
    vault = tmp_path / "shelf"
    vault.mkdir()
    _damaged_note(vault, "legacy.md", 'title: "A�B"\ngoogle_books_id: 123')
    damage = find_encoding_damage(vault)[0]
    client = _StubClient(BookCandidate(title="AxB", authors=[]))

    # When a repair is sought
    service.propose_encoding_repair(damage, client)

    # Then the id is text, as the client needs. Passed as an int it reached URL
    # quoting and raised TypeError, which `repair` does not catch (#129 fifth
    # review).
    assert damage.google_books_id == "123"
    assert client.asked == ["123"]


def test_a_note_that_lost_nothing_is_not_reported(tmp_path):
    # Given a Shelf whose notes are intact
    vault = tmp_path / "shelf"
    vault.mkdir()
    _damaged_note(
        vault, "fine.md", 'title: Either-Or\nauthors:\n  - "Søren Kierkegaard"'
    )

    # When the Shelf is inspected
    # Then nothing is reported. An accented character is not damage.
    assert find_encoding_damage(vault) == []


def test_a_lost_character_is_found_wherever_the_note_keeps_it(tmp_path):
    # Given notes that lost a character in each of the places one can hide
    vault = tmp_path / "shelf"
    vault.mkdir()
    _damaged_note(
        vault,
        "S�ren.md",
        'title: Either-Or\nauthors:\n  - "S�ren Kierkegaard"\ngoogle_books_id: vol1',
    )
    _damaged_note(
        vault,
        "title.md",
        'title: "La Com�die Humaine"\nauthors:\n  - Balzac\ngoogle_books_id: vol2',
    )
    _damaged_note(
        vault,
        "body.md",
        "title: Discourse\nauthors:\n  - Descartes\ngoogle_books_id: vol3",
        body="# Discourse\n\n> Discours de la m�thode.\n",
    )

    # When the Shelf is inspected
    found = {damage.path.name: damage for damage in find_encoding_damage(vault)}

    # Then each note is reported against the field that actually holds it
    assert set(found) == {"S�ren.md", "title.md", "body.md"}
    assert set(found["S�ren.md"].fields) == {"filename", "authors"}
    assert set(found["title.md"].fields) == {"title"}
    assert set(found["body.md"].fields) == {"body"}

    # And the body is reported by line, not whole
    assert found["body.md"].fields["body"] == ["> Discours de la m�thode."]


def test_the_report_says_what_could_repair_each_note(tmp_path):
    # Given three notes with different means of identification
    vault = tmp_path / "shelf"
    vault.mkdir()
    _damaged_note(
        vault, "gid.md", 'title: "A�B"\ngoogle_books_id: vol1\nisbn: "9780000000001"'
    )
    _damaged_note(vault, "isbn.md", 'title: "A�B"\nisbn: "9780000000001"')
    _damaged_note(vault, "neither.md", 'title: "A�B"')

    found = {damage.path.name: damage for damage in find_encoding_damage(vault)}

    # Then the volume id is preferred, an ISBN will do, and a note with neither
    # says so rather than being reported as repairable
    assert found["gid.md"].identifier == "vol1"
    assert found["isbn.md"].identifier == "9780000000001"
    assert found["neither.md"].identifier is None


def test_the_report_separates_what_needs_a_rename(tmp_path):
    # Given one note damaged only in frontmatter and one damaged in its filename
    vault = tmp_path / "shelf"
    vault.mkdir()
    _damaged_note(vault, "clean-name.md", 'title: "A�B"\ngoogle_books_id: vol1')
    _damaged_note(vault, "A�B.md", 'title: "A�B"\ngoogle_books_id: vol2')

    found = {damage.path.name: damage for damage in find_encoding_damage(vault)}

    # Then only the second needs a rename, which rewrites wikilinks and is its
    # own piece of work
    assert not found["clean-name.md"].touches_filename
    assert found["clean-name.md"].repairable_in_place == ["title"]
    assert found["A�B.md"].touches_filename


def test_the_report_writes_nothing(tmp_path):
    # Given a damaged note
    vault = tmp_path / "shelf"
    vault.mkdir()
    path = _damaged_note(vault, "d.md", 'title: "A�B"\ngoogle_books_id: vol1')
    before = path.read_bytes()

    # When the Shelf is inspected
    find_encoding_damage(vault)

    # Then the note is untouched, byte for byte. A report that repairs something
    # on the way past is the silent wrongness ADR 0003 refuses.
    assert path.read_bytes() == before


def test_damage_is_found_in_a_file_with_no_frontmatter_at_all(tmp_path):
    # Given a file in the Shelf directory with no frontmatter block. Obsidian
    # writes into this directory too, so not every .md here is a Book Note.
    vault = tmp_path / "shelf"
    vault.mkdir()
    loose = vault / "loose.md"
    loose.write_text("Just a body, mentioning Ren� Descartes.\n", encoding="utf-8")

    # When the Shelf is inspected
    found = {damage.path.name: damage for damage in find_encoding_damage(vault)}

    # Then the body is still searched. Treating an unparseable file as having no
    # body made the report depend on a note being parseable, which is backwards:
    # a file nothing can parse is more likely to hold damage, not less.
    assert "loose.md" in found
    assert found["loose.md"].fields["body"] == [
        "Just a body, mentioning Ren� Descartes."
    ]


def test_damage_is_found_when_the_frontmatter_will_not_parse(tmp_path):
    # Given a note whose frontmatter is not valid YAML, damaged in the body
    vault = tmp_path / "shelf"
    vault.mkdir()
    broken = vault / "broken.md"
    broken.write_text(
        "---\ntitle: [unclosed\n---\n\n# La Com�die Humaine\n", encoding="utf-8"
    )

    # When the Shelf is inspected
    found = {damage.path.name: damage for damage in find_encoding_damage(vault)}

    # Then it is reported through what can still be read, rather than skipped
    assert "broken.md" in found
    assert found["broken.md"].fields["body"] == ["# La Com�die Humaine"]
    assert found["broken.md"].identifier is None


def test_damage_inside_unparseable_frontmatter_is_reported(tmp_path):
    # Given a note whose frontmatter will not parse and whose damage is inside
    # that frontmatter. The filename and body are clean, so nothing else can
    # report this note.
    vault = tmp_path / "shelf"
    vault.mkdir()
    (vault / "clean-name.md").write_text(
        '---\ntitle: [unclosed\nauthors:\n  - "S�ren Kierkegaard"\n---\n\nA clean body.\n',
        encoding="utf-8",
    )

    # When the Shelf is inspected
    found = {damage.path.name: damage for damage in find_encoding_damage(vault)}

    # Then it is reported from the raw text. Reading the parsed fields was the
    # only way frontmatter damage was ever found, so a note whose YAML will not
    # parse went unreported - which is exactly the note most likely to hold some.
    assert "clean-name.md" in found
    assert found["clean-name.md"].fields["frontmatter (unparseable)"] == [
        '- "S�ren Kierkegaard"'
    ]


def test_an_identifier_is_read_from_frontmatter_that_will_not_parse(tmp_path):
    # Given a note whose YAML will not parse but which plainly states its
    # volume id and ISBN, on their own lines, right beside the damage
    vault = tmp_path / "shelf"
    vault.mkdir()
    (vault / "clean-name.md").write_text(
        '---\ntitle: [unclosed\nauthors:\n  - "S�ren Kierkegaard"\n'
        'google_books_id: vol1\nisbn: "9780000000001"\n---\n\nA clean body.\n',
        encoding="utf-8",
    )

    # When the Shelf is inspected
    found = {damage.path.name: damage for damage in find_encoding_damage(vault)}
    damage = found["clean-name.md"]

    # Then the report knows the API could answer for it. Reporting the note as
    # needing a person, because the block as a whole would not parse, would have
    # sent someone to look up a book whose id is written above the damage.
    assert damage.google_books_id == "vol1"
    assert damage.isbn == "9780000000001"
    assert damage.identifier == "vol1"


def test_a_nested_key_is_not_mistaken_for_the_notes_own(tmp_path):
    # Given unparseable frontmatter where `isbn:` appears only as a nested key
    vault = tmp_path / "shelf"
    vault.mkdir()
    (vault / "nested.md").write_text(
        '---\ntitle: [unclosed\nother:\n  isbn: "9780000000001"\n'
        'authors:\n  - "S�ren Kierkegaard"\n---\n\nBody.\n',
        encoding="utf-8",
    )

    # When the Shelf is inspected
    found = {damage.path.name: damage for damage in find_encoding_damage(vault)}

    # Then the indented one is not read as the note's. The raw reader is naive
    # by design, so this pins the one thing naivety must not cost.
    assert found["nested.md"].isbn is None
    assert found["nested.md"].identifier is None


def test_a_parseable_note_still_reads_its_identifiers_normally(tmp_path):
    # Given an ordinary damaged note
    vault = tmp_path / "shelf"
    vault.mkdir()
    _damaged_note(
        vault, "ok.md", 'title: "A�B"\ngoogle_books_id: vol9\nisbn: "9780000000001"'
    )

    # When the Shelf is inspected
    found = {damage.path.name: damage for damage in find_encoding_damage(vault)}

    # Then nothing about the raw-text fallback changed the normal path
    assert found["ok.md"].google_books_id == "vol9"
    assert found["ok.md"].identifier == "vol9"


# --- Book Notes contesting one identity (#75) -------------------------------


def _note_with_id(vault, name, libris_id, title="A Book", isbn=None):
    """Write a Book Note claiming a given Libris ID."""
    lines = [f"libris_id: {libris_id}", f"title: {title}", "authors:", "  - An Author"]
    if isbn:
        lines.append(f'isbn: "{isbn}"')
    (vault / name).write_text(
        "---\n" + "\n".join(lines) + "\n---\n\nBody.\n", encoding="utf-8"
    )
    return vault / name


def test_two_notes_claiming_one_identity_are_reported(tmp_path):
    # Given two Book Notes carrying the same Libris ID, as the real Shelf holds
    vault = tmp_path / "shelf"
    vault.mkdir()
    _note_with_id(vault, "b.md", "01M0WQEHRZ6KZK0D3BM7C2YXEM")
    _note_with_id(vault, "a.md", "01M0WQEHRZ6KZK0D3BM7C2YXEM")

    # When the Shelf is inspected
    collisions = find_id_collisions(vault)

    # Then the contested identity is reported, with its notes in filename order
    # so two runs report it the same way
    assert len(collisions) == 1
    assert collisions[0].libris_id == "01M0WQEHRZ6KZK0D3BM7C2YXEM"
    assert [note.path.name for note in collisions[0].notes] == ["a.md", "b.md"]


def test_distinct_identities_are_not_reported(tmp_path):
    # Given notes that each hold their own identity
    vault = tmp_path / "shelf"
    vault.mkdir()
    _note_with_id(vault, "a.md", "01AAAAAAAAAAAAAAAAAAAAAAAA")
    _note_with_id(vault, "b.md", "01BBBBBBBBBBBBBBBBBBBBBBBB")

    # When the Shelf is inspected
    # Then nothing is reported
    assert find_id_collisions(vault) == []


def test_a_note_that_is_not_utf8_is_reported_as_such_not_as_lost_letters(tmp_path):
    # Given a Shelf holding one note saved as Latin-1, its letters intact on disk
    vault = tmp_path / "shelf"
    vault.mkdir()
    _note_with_id(vault, "a.md", "01AAAAAAAAAAAAAAAAAAAAAAAA")
    latin1 = vault / "Kierkegaard.md"
    latin1.write_bytes(
        "---\ntitle: Either-Or\nauthors:\n  - Søren Kierkegaard\n---\n\nBody.\n".encode(
            "latin-1"
        )
    )
    before = latin1.read_bytes()

    # When the Shelf is inspected
    report = service.inspect_shelf(vault)

    # Then it is named as not UTF-8 rather than stopping the inspection - the
    # decode error ended `doctor` for the whole Shelf (#127 review)
    assert report.not_utf8 == [latin1]
    assert not report.is_clean

    # And it is not reported as having lost a character. Reading it put the
    # replacement characters there; the ø is still in the file, so sending a
    # person to the API for the spelling would be the wrong repair.
    assert report.encoding_damage == []
    assert service.find_encoding_damage(vault) == []
    assert latin1.read_bytes() == before


def test_a_note_that_is_not_utf8_still_counts_toward_a_contested_identity(tmp_path):
    # Given two notes claiming one Libris ID, one of them saved as Latin-1
    vault = tmp_path / "shelf"
    vault.mkdir()
    _note_with_id(vault, "a.md", "01M0WQEHRZ6KZK0D3BM7C2YXEM")
    (vault / "b.md").write_bytes(
        "---\nlibris_id: 01M0WQEHRZ6KZK0D3BM7C2YXEM\ntitle: Søren\n---\n".encode(
            "latin-1"
        )
    )

    # When the Shelf is inspected
    collisions = find_id_collisions(vault)

    # Then the collision is found. Its identity is plain ASCII and survives the
    # decode, so dropping the note would hide a collision it is plainly part of.
    assert [note.path.name for note in collisions[0].notes] == ["a.md", "b.md"]


def test_a_note_that_is_not_utf8_still_reports_a_damaged_filename(tmp_path):
    # Given a note saved as Latin-1 whose filename has also lost a character
    vault = tmp_path / "shelf"
    vault.mkdir()
    damaged = vault / "S�ren.md"
    damaged.write_bytes("---\ntitle: Søren\n---\n".encode("latin-1"))

    # When the Shelf is inspected
    report = service.inspect_shelf(vault)

    # Then both are reported. The filename is not decoded by the reader, so its
    # lost character is real and wants a rename, however the contents are
    # encoded (#127 review) - but the contents' replacement characters are still
    # not counted, because the reader put them there.
    assert report.not_utf8 == [damaged]
    assert [note.fields for note in report.encoding_damage] == [
        {"filename": ["S�ren.md"]}
    ]


def test_a_note_gone_between_listing_and_reading_does_not_stop_the_inspection(
    tmp_path, monkeypatch
):
    # Given a Shelf listed with a note that is gone by the time it is read -
    # Obsidian renaming it, or a sync client moving it
    vault = tmp_path / "shelf"
    vault.mkdir()
    _note_with_id(vault, "a.md", "01AAAAAAAAAAAAAAAAAAAAAAAA")
    real_list = service.list_books
    monkeypatch.setattr(
        service, "list_books", lambda path: [*real_list(path), path / "gone.md"]
    )

    # When the Shelf is inspected
    report = service.inspect_shelf(vault)

    # Then the rest is inspected, rather than one moved note ending `doctor` in
    # a FileNotFoundError (#127 review)
    assert report.is_clean


def test_notes_without_an_identity_are_not_a_collision(tmp_path):
    # Given two notes that carry no Libris ID at all
    vault = tmp_path / "shelf"
    vault.mkdir()
    for name in ("a.md", "b.md"):
        (vault / name).write_text(
            "---\ntitle: A Book\nauthors:\n  - An Author\n---\n\nBody.\n",
            encoding="utf-8",
        )

    # When the Shelf is inspected
    # Then they are not reported as contesting one identity. Sharing "no id" is
    # a different problem, and ensure_frontmatter_fields already mints one.
    assert find_id_collisions(vault) == []


def test_a_collision_says_whether_the_notes_name_one_book(tmp_path):
    # Given one identity held by two notes naming the same ISBN, and another
    # held by two notes naming different ones
    vault = tmp_path / "shelf"
    vault.mkdir()
    _note_with_id(vault, "same1.md", "01AAAAAAAAAAAAAAAAAAAAAAAA", isbn="9780000000001")
    _note_with_id(vault, "same2.md", "01AAAAAAAAAAAAAAAAAAAAAAAA", isbn="9780000000001")
    _note_with_id(vault, "diff1.md", "01BBBBBBBBBBBBBBBBBBBBBBBB", isbn="9780000000001")
    _note_with_id(vault, "diff2.md", "01BBBBBBBBBBBBBBBBBBBBBBBB", isbn="9780000000002")

    # When the Shelf is inspected
    found = {c.libris_id: c for c in find_id_collisions(vault)}

    # Then the fact that most often decides merge-versus-remint travels with the
    # collision - without the report making that decision
    assert found["01AAAAAAAAAAAAAAAAAAAAAAAA"].isbn_agreement is IsbnAgreement.SAME
    assert found["01BBBBBBBBBBBBBBBBBBBBBBBB"].isbn_agreement is IsbnAgreement.DIFFERENT


def test_reporting_a_collision_writes_nothing(tmp_path):
    # Given two notes contesting an identity
    vault = tmp_path / "shelf"
    vault.mkdir()
    first = _note_with_id(vault, "a.md", "01AAAAAAAAAAAAAAAAAAAAAAAA")
    second = _note_with_id(vault, "b.md", "01AAAAAAAAAAAAAAAAAAAAAAAA")
    before = (first.read_bytes(), second.read_bytes())

    # When the Shelf is inspected
    find_id_collisions(vault)

    # Then neither note is touched. Merging or re-minting without being asked is
    # exactly what this must not do.
    assert (first.read_bytes(), second.read_bytes()) == before


def test_a_collision_is_found_even_when_a_notes_frontmatter_will_not_parse(tmp_path):
    # Given two notes contesting one identity, one of which has frontmatter that
    # will not parse
    vault = tmp_path / "shelf"
    vault.mkdir()
    (vault / "good.md").write_text(
        "---\nlibris_id: 01AAAAAAAAAAAAAAAAAAAAAAAA\ntitle: A Book\n---\n\nBody.\n",
        encoding="utf-8",
    )
    (vault / "broken.md").write_text(
        "---\nlibris_id: 01AAAAAAAAAAAAAAAAAAAAAAAA\ntitle: [unclosed\n---\n\nBody.\n",
        encoding="utf-8",
    )

    # When the Shelf is inspected
    collisions = find_id_collisions(vault)

    # Then both notes are counted. Reading through the index dropped the
    # unparseable one - BookNote.read returns None - so the collision went
    # unreported exactly when a note was damaged, which is what this check is
    # for (#75).
    assert len(collisions) == 1
    assert [note.path.name for note in collisions[0].notes] == ["broken.md", "good.md"]


def test_every_check_reads_a_damaged_note_the_same_way(tmp_path):
    # Given one note whose frontmatter will not parse, holding both an identity
    # and a lost character
    vault = tmp_path / "shelf"
    vault.mkdir()
    (vault / "a.md").write_text(
        "---\nlibris_id: 01AAAAAAAAAAAAAAAAAAAAAAAA\ntitle: [unclosed\n"
        'authors:\n  - "S�ren Kierkegaard"\n---\n\nBody.\n',
        encoding="utf-8",
    )
    (vault / "b.md").write_text(
        "---\nlibris_id: 01AAAAAAAAAAAAAAAAAAAAAAAA\ntitle: Another\n---\n\nBody.\n",
        encoding="utf-8",
    )

    # When both checks run
    collisions = find_id_collisions(vault)
    damaged = find_encoding_damage(vault)

    # Then neither check pretends the damaged note is absent. Two checks with
    # two ideas of how to read a note is what let a collision hide.
    assert [note.path.name for note in collisions[0].notes] == ["a.md", "b.md"]
    assert any(entry.path.name == "a.md" for entry in damaged)


def test_a_collision_reads_a_damaged_notes_isbn_and_title(tmp_path):
    # Given two notes claiming one identity and plainly naming one ISBN, where
    # one note's frontmatter will not parse
    vault = tmp_path / "shelf"
    vault.mkdir()
    (vault / "good.md").write_text(
        "---\nlibris_id: 01AAAAAAAAAAAAAAAAAAAAAAAA\ntitle: A Calendar of Wisdom\n"
        'isbn: "9781847495631"\n---\n\nBody.\n',
        encoding="utf-8",
    )
    (vault / "broken.md").write_text(
        "---\nlibris_id: 01AAAAAAAAAAAAAAAAAAAAAAAA\ntitle: A Calendar of Wisdom\n"
        'authors: [unclosed\nisbn: "9781847495631"\n---\n\nBody.\n',
        encoding="utf-8",
    )

    # When the Shelf is inspected
    collision = find_id_collisions(vault)[0]

    # Then the damaged note's scalars are read from its raw text. Reading it as
    # holding nothing did not merely lose detail - it inverted the answer, and
    # doctor advised re-minting an id where merging was right.
    assert collision.titles == ["A Calendar of Wisdom", "A Calendar of Wisdom"]
    assert collision.isbn_agreement is IsbnAgreement.SAME


def test_a_damaged_note_reports_no_authors_rather_than_guessing(tmp_path):
    # Given a note whose frontmatter will not parse and whose authors are a list
    vault = tmp_path / "shelf"
    vault.mkdir()
    for name in ("a.md", "b.md"):
        (vault / name).write_text(
            "---\nlibris_id: 01AAAAAAAAAAAAAAAAAAAAAAAA\ntitle: A Book\n"
            'authors: [unclosed\n  - "An Author"\n---\n\nBody.\n',
            encoding="utf-8",
        )

    # When the Shelf is inspected
    collision = find_id_collisions(vault)[0]

    # Then no authors are claimed. A list cannot be read from one raw line, and
    # reporting none is honest where guessing is not.
    assert all(note.authors == [] for note in collision.notes)


def test_a_missing_isbn_is_unknown_rather_than_disagreement(tmp_path):
    # Given one identity held by two notes where only one names an ISBN
    vault = tmp_path / "shelf"
    vault.mkdir()
    _note_with_id(vault, "has.md", "01AAAAAAAAAAAAAAAAAAAAAAAA", isbn="9780000000001")
    _note_with_id(vault, "none.md", "01AAAAAAAAAAAAAAAAAAAAAAAA")

    # When the Shelf is inspected
    collision = find_id_collisions(vault)[0]

    # Then the answer is UNKNOWN, not DIFFERENT. A note carrying no ISBN says
    # nothing about which book it is, and a boolean reported that silence as
    # disagreement - which read as "these are different books" and would have
    # sent someone to re-mint an id for two copies of one.
    assert collision.isbn_agreement is IsbnAgreement.UNKNOWN


def test_no_colliding_note_naming_an_isbn_is_also_unknown(tmp_path):
    # Given two notes contesting an identity, neither naming an ISBN
    vault = tmp_path / "shelf"
    vault.mkdir()
    _note_with_id(vault, "a.md", "01AAAAAAAAAAAAAAAAAAAAAAAA")
    _note_with_id(vault, "b.md", "01AAAAAAAAAAAAAAAAAAAAAAAA")

    # When the Shelf is inspected
    # Then two silences do not agree with each other
    assert find_id_collisions(vault)[0].isbn_agreement is IsbnAgreement.UNKNOWN


def test_a_collision_is_found_when_a_note_never_closes_its_frontmatter(tmp_path):
    # Given two notes contesting one identity, one of which opens a frontmatter
    # block and never closes it
    vault = tmp_path / "shelf"
    vault.mkdir()
    (vault / "good.md").write_text(
        "---\nlibris_id: 01AAAAAAAAAAAAAAAAAAAAAAAA\ntitle: A Book\n---\n\nBody.\n",
        encoding="utf-8",
    )
    (vault / "unclosed.md").write_text(
        "---\nlibris_id: 01AAAAAAAAAAAAAAAAAAAAAAAA\ntitle: S�ren\n",
        encoding="utf-8",
    )

    # When the Shelf is inspected
    collisions = find_id_collisions(vault)
    damaged = {entry.path.name: entry for entry in find_encoding_damage(vault)}

    # Then the unclosed note is counted. `split_frontmatter` returns None for it,
    # and treating that as "all body" lost the identity stated on its second line.
    assert len(collisions) == 1
    assert [note.path.name for note in collisions[0].notes] == [
        "good.md",
        "unclosed.md",
    ]

    # And its damage is filed as frontmatter rather than as the reader's prose,
    # which is what an unterminated block actually holds
    assert list(damaged["unclosed.md"].fields) == ["frontmatter (unparseable)"]


def test_a_file_with_no_fence_at_all_is_still_all_body(tmp_path):
    # Given a file that never opens a frontmatter block
    vault = tmp_path / "shelf"
    vault.mkdir()
    (vault / "loose.md").write_text(
        "Just prose, mentioning Ren� Descartes.\n", encoding="utf-8"
    )

    # When the Shelf is inspected
    damaged = {entry.path.name: entry for entry in find_encoding_damage(vault)}

    # Then it is body, not frontmatter. Only a file that opens a fence gets the
    # benefit of the doubt about what its author meant.
    assert list(damaged["loose.md"].fields) == ["body"]


def test_a_decision_drifts_when_a_path_carries_the_other_named_id(
    tmp_path, monkeypatch
):
    # Given a pair whose second note is replaced at its path by a note carrying
    # the first note's Libris ID. Both ids are ones the decision names, so
    # asking only whether a path holds one of the two is satisfied - and the
    # note that actually answers for the second id is no longer in the pair.
    first, second = _pair(tmp_path)
    real = service.get_primary_book

    def _replaced_after_the_index(path1, path2):
        primary = real(path1, path2)
        # The title is quoted: these titles carry a colon, and written bare the
        # note is not valid YAML. Unquoted, it was unreadable rather than a
        # different book, so the decision drifted because nothing could be read
        # from it and the test passed without ever exercising the ids.
        second.path.write_text(
            f'---\ntitle: "{BookNote.read(primary).title}"\n'
            f"libris_id: {first.libris_id}\n---\n\n## Notes\n\nA different book.\n",
            encoding="utf-8",
        )
        return primary

    monkeypatch.setattr(service, "get_primary_book", _replaced_after_the_index)

    # When the decision is applied
    outcomes = apply_decisions(tmp_path, [_decision(first, second)])

    # Then it drifts and both notes are still there. Each path has to carry the
    # id it resolved for: checked against the two ids as a set, this pair passed
    # and deleted a book the decision never named (#133 fourth review).
    assert [o.status for o in outcomes] == [DecisionStatus.DRIFTED]
    assert len(list(tmp_path.glob("*.md"))) == 2
    assert any(
        "A different book." in path.read_text(encoding="utf-8")
        for path in tmp_path.glob("*.md")
    )


def test_a_decision_whose_note_was_replaced_by_another_book_drifts(
    tmp_path, monkeypatch
):
    # Given a pair, one of whose notes is replaced at the same path by a
    # different book - after the index resolved the decision to that path, and
    # before anything about it is read. Its title matches the note it replaced,
    # so nothing downstream reports a conflict and the merge simply proceeds.
    first, second = _pair(tmp_path)
    real = service.get_primary_book

    def _replaced_after_the_index(path1, path2):
        primary = real(path1, path2)
        secondary = path2 if primary == path1 else path1
        # Quoted, so this is a readable note that is a different book - not an
        # unreadable one, which drifts for its own reason and would let this
        # test pass without the identity ever being compared.
        secondary.write_text(
            f'---\ntitle: "{BookNote.read(primary).title}"\n'
            "libris_id: lb-2099-9999\n---\n\n## Notes\n\nAnother book entirely.\n",
            encoding="utf-8",
        )
        return primary

    monkeypatch.setattr(service, "get_primary_book", _replaced_after_the_index)

    # When the decision is applied
    outcomes = apply_decisions(tmp_path, [_decision(first, second)])

    # Then it drifts, and the book that was written there is still on the Shelf.
    # Checked only by fingerprint, the hash described the replacement, so the
    # decision merged a book it never named into another and then deleted it
    # (#133 third review).
    assert [o.status for o in outcomes] == [DecisionStatus.DRIFTED]
    assert len(list(tmp_path.glob("*.md"))) == 2
    assert any(
        "Another book entirely." in path.read_text(encoding="utf-8")
        for path in tmp_path.glob("*.md")
    )


def test_a_kept_secondary_is_forgotten_rather_than_answered_for_by_the_primary(
    tmp_path, monkeypatch
):
    # Given a pair judged one Book whose secondary is edited between the check
    # and the deletion, so it is kept - and a later decision in the same batch
    # naming that kept note and a third book
    from libris.merge import get_primary_book

    first, second = _pair(tmp_path)
    third = BookNote.read(create_book_note(_candidate(title="The Reversal"), tmp_path))
    primary_path = get_primary_book(first.path, second.path)
    kept = second if primary_path == first.path else first
    real = service.write_merged_book

    def _edit_secondary_after_writing(path, *args, **kwargs):
        result = real(path, *args, **kwargs)
        kept.path.write_text(
            "---\ntitle: The Brass Verdict\n---\n\n## Notes\n\nTyped in Obsidian.\n",
            encoding="utf-8",
        )
        return result

    monkeypatch.setattr(service, "write_merged_book", _edit_secondary_after_writing)

    # When both decisions are applied in one batch
    outcomes = apply_decisions(
        tmp_path, [_decision(first, second), _decision(kept, third)]
    )

    # Then the second drifts. The index went on mapping the kept note's identity
    # to the primary, so a decision naming the note that was deliberately kept
    # resolved to the wrong file and merged it (#133 review).
    assert [o.status for o in outcomes] == [
        DecisionStatus.MERGED,
        DecisionStatus.DRIFTED,
    ]
    assert third.path.exists()


def test_a_secondary_that_only_changed_is_still_the_note_it_was(tmp_path, monkeypatch):
    # Given a pair whose secondary changes before the merge is written - keeping
    # its Libris ID, so it is the same note carrying newer writing - and a later
    # decision in the same batch naming that note and a third book
    from libris.merge import get_primary_book

    first, second = _pair(tmp_path)
    third = BookNote.read(create_book_note(_candidate(title="The Reversal"), tmp_path))
    primary_path = get_primary_book(first.path, second.path)
    changed = second if primary_path == first.path else first
    real = service.merge_two_books

    def _edited_after_reading(path, secondary_path, **kwargs):
        merged = real(path, secondary_path, **kwargs)
        note = BookNote.read(secondary_path)
        secondary_path.write_text(
            f"---\ntitle: {note.title}\nlibris_id: {note.libris_id}\n---\n"
            "\n## Notes\n\nTyped in Obsidian.\n",
            encoding="utf-8",
        )
        return merged

    monkeypatch.setattr(service, "merge_two_books", _edited_after_reading)

    # When both decisions are applied in one batch
    outcomes = apply_decisions(
        tmp_path, [_decision(first, second), _decision(changed, third)]
    )

    # Then the first drifts - nothing was merged - and the second still acts on
    # the changed note. It kept the identity the decision names, so it is the
    # note to act on; forgetting it because its text moved drifted a decision
    # about a note that was there and was right (#133 third review).
    assert [o.status for o in outcomes] == [
        DecisionStatus.DRIFTED,
        DecisionStatus.MERGED,
    ]


def test_a_secondary_edited_after_the_check_and_before_the_delete_is_kept(
    tmp_path, monkeypatch
):
    # Given a pair judged one Book, whose secondary is edited after it was
    # checked and after the merged note was written - the last instant before
    # the deletion, which no check can close
    first, second = _pair(tmp_path)
    real = service.write_merged_book

    def _edit_secondary_after_writing(primary_path, *args, **kwargs):
        result = real(primary_path, *args, **kwargs)
        secondary_path = next(
            path for path in (first.path, second.path) if path != primary_path
        )
        secondary_path.write_text(
            "---\ntitle: The Brass Verdict\n---\n\n## Notes\n\nTyped in Obsidian.\n",
            encoding="utf-8",
        )
        return result

    monkeypatch.setattr(service, "write_merged_book", _edit_secondary_after_writing)

    # When the decision is applied
    outcomes = apply_decisions(tmp_path, [_decision(first, second)])

    # Then the merge counts as done - it was written - but the secondary is kept
    # rather than deleted, because it now holds writing the merged note never
    # saw. The deletion is the last irreversible act, so it is checked again
    # here and not only before the write (#133 review).
    assert [o.status for o in outcomes] == [DecisionStatus.MERGED]
    assert "kept" in outcomes[0].detail
    assert len(list(tmp_path.glob("*.md"))) == 2
    assert any(
        "Typed in Obsidian." in path.read_text(encoding="utf-8")
        for path in tmp_path.glob("*.md")
    )


def test_a_secondary_edited_before_its_merge_is_written_is_not_deleted(
    tmp_path, monkeypatch
):
    # Given a pair judged one Book, whose secondary - the note the merge deletes
    # - is edited between the merge being worked out and written
    first, second = _pair(tmp_path)
    real = service.merge_two_books

    def _edited_after_reading(primary_path, secondary_path, **kwargs):
        merged = real(primary_path, secondary_path, **kwargs)
        secondary_path.write_text(
            "---\ntitle: The Brass Verdict\n---\n\n## Notes\n\nTyped in Obsidian.\n",
            encoding="utf-8",
        )
        return merged

    monkeypatch.setattr(service, "merge_two_books", _edited_after_reading)

    # When the decision is applied
    outcomes = apply_decisions(tmp_path, [_decision(first, second)])

    # Then nothing is merged and the secondary is not deleted. Its edit is not in
    # the merged text, so deleting it would have destroyed the only copy of it
    # (#133 review).
    assert [o.status for o in outcomes] == [DecisionStatus.DRIFTED]
    assert len(list(tmp_path.glob("*.md"))) == 2
    assert any(
        "Typed in Obsidian." in path.read_text(encoding="utf-8")
        for path in tmp_path.glob("*.md")
    )


def test_a_primary_edited_before_its_merge_is_written_keeps_both_notes(
    tmp_path, monkeypatch
):
    # Given a pair judged one Book, whose primary is edited between the merge
    # being worked out and written
    first, second = _pair(tmp_path)
    real = service.merge_two_books

    def _edited_after_reading(primary_path, secondary_path, **kwargs):
        merged = real(primary_path, secondary_path, **kwargs)
        primary_path.write_text(
            "---\ntitle: The Brass Verdict\n---\n\n## Notes\n\nTyped in Obsidian.\n",
            encoding="utf-8",
        )
        return merged

    monkeypatch.setattr(service, "merge_two_books", _edited_after_reading)

    # When the decision is applied
    outcomes = apply_decisions(tmp_path, [_decision(first, second)])

    # Then nothing is merged and nothing is deleted: the merged content was
    # worked out from a note that no longer says that, and writing it would have
    # traded the reader's edit for a deletion of the secondary (#132)
    assert [o.status for o in outcomes] == [DecisionStatus.DRIFTED]
    assert len(list(tmp_path.glob("*.md"))) == 2
    assert any(
        "Typed in Obsidian." in path.read_text(encoding="utf-8")
        for path in tmp_path.glob("*.md")
    )
