"""Tests for the service layer the adapters sit over.

ADR 0008 puts resolution, creation and querying below the adapters, so the REST
surface and the MCP tools cannot drift apart by reimplementing matching. These
tests exercise that layer directly, with no HTTP involved.
"""

import pytest

from libris.api import BookCandidate
from libris.markdown import (
    BookNote,  # noqa: F401
    create_book_note,
)
from libris.note_format import InvalidFieldValue
from libris.service import (
    Outcome,
    add_book,
    build_lookup_query,
    find_by_libris_id,
    find_existing,
    is_isbn10,
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
