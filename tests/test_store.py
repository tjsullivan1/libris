"""Tests for the stores the service asks its exact questions of (ADR 0032, ADR 0033).

Every question is asked of the Shelf and of a replica built from the same Book
Notes, and must get the same answer from both. The replica stands in for the
remote one: it holds what sync would push, and answers from the stored keys,
words and per-Status word counts rather than from the notes themselves.
"""

from pathlib import Path

import pytest
from conftest import pushed_to_cosmos

from libris.service import search_library
from libris.shelf import index_for
from libris.store import ReplicaStore, ShelfStore, search_tokens


def _note(vault, name, title=None, authors=("An Author",), **fields):
    """Write a Book Note with the frontmatter given, and a Libris ID.

    The ID is made from the filename unless one is given. Every note on the real
    Shelf carries one, and the remote cannot hold a note without one (ADR 0033),
    so a note lacking one would test a Shelf sync refuses to mirror.
    """
    fields.setdefault("libris_id", "01" + Path(name).stem.upper())
    lines = []
    if title is not None:
        lines.append(f"title: {title}")
    if authors:
        lines.append("authors:")
        lines.extend(f"  - {author}" for author in authors)
    for key, value in fields.items():
        if isinstance(value, list):
            lines.append(f"{key}:")
            lines.extend(f"  - {item}" for item in value)
        else:
            lines.append(f"{key}: {value}")
    (vault / name).write_text(
        "---\n" + "\n".join(lines) + "\n---\n\nBody.\n", encoding="utf-8"
    )


def _titles(notes):
    return [note.title for note in notes]


# --- the exact lookups (ADR 0032) ---


def test_an_isbn_finds_every_note_carrying_it(tmp_path, open_store):
    # Given two notes sharing an ISBN and one that does not
    _note(tmp_path, "a.md", "Thinking in Systems", isbn='"9781603581486"')
    _note(tmp_path, "b.md", "Thinking in Systems A Primer", isbn='"9781603581486"')
    _note(tmp_path, "c.md", "Dune", isbn='"9780441013593"')

    # When the store is asked for the shared ISBN
    found = open_store(tmp_path).with_isbn("9781603581486")

    # Then both notes carrying it come back, and nothing else
    assert sorted(_titles(found)) == [
        "Thinking in Systems",
        "Thinking in Systems A Primer",
    ]


def test_an_isbn_stored_unquoted_is_still_found(tmp_path, open_store):
    # Given a note whose ISBN YAML reads as a number (#105)
    _note(tmp_path, "a.md", "Dune", isbn="786937521")

    # When it is asked for as text
    # Then the store compares what the note means, not how it was quoted
    assert _titles(open_store(tmp_path).with_isbn("786937521")) == ["Dune"]


def test_a_google_books_id_finds_its_note(tmp_path, open_store):
    # Given two notes with different volume ids
    _note(tmp_path, "a.md", "Dune", google_books_id="dune1")
    _note(tmp_path, "b.md", "Piranesi", google_books_id="pira1")

    # When one volume is asked for
    # Then only its note answers
    assert _titles(open_store(tmp_path).with_google_books_id("dune1")) == ["Dune"]


def test_a_title_and_author_are_compared_as_normalized_keys(tmp_path, open_store):
    # Given a note spelled with capitals and punctuation
    _note(tmp_path, "a.md", '"Dune: Messiah"', authors=["Frank Herbert"])
    _note(tmp_path, "b.md", '"Dune: Messiah"', authors=["Brian Herbert"])

    # When the store is asked for the normalized title and first author
    found = open_store(tmp_path).with_title_and_author("dune messiah", "frank herbert")

    # Then the note by that author answers, and the same title by another does not
    assert [n.first_author for n in found] == ["Frank Herbert"]


def test_a_first_author_returns_every_note_they_lead(tmp_path, open_store):
    # Given two notes led by one author, and one they only co-wrote
    _note(tmp_path, "a.md", "Mistborn", authors=["Brandon Sanderson"])
    _note(tmp_path, "b.md", "Elantris", authors=["Brandon Sanderson"])
    _note(tmp_path, "c.md", "The Wheel", authors=["Robert Jordan", "Brandon Sanderson"])

    # When the store is asked for that first author
    found = open_store(tmp_path).by_first_author("brandon sanderson")

    # Then only the notes they come first on answer
    assert sorted(_titles(found)) == ["Elantris", "Mistborn"]


def test_a_libris_id_finds_its_note_and_a_superseded_one_its_survivor(
    tmp_path, open_store
):
    # Given a note that absorbed another identity in a merge (ADR 0014)
    _note(tmp_path, "a.md", "Dune", libris_id="01LIVE", superseded_ids=["01GONE"])

    store = open_store(tmp_path)

    # When the store is asked for each identity
    # Then the live one is held by the note, and the absorbed one points at it
    assert _titles(store.with_libris_id("01LIVE")) == ["Dune"]
    assert store.with_libris_id("01GONE") == []
    assert _titles(store.superseding("01GONE")) == ["Dune"]
    assert store.superseding("01LIVE") == []


# --- the search questions (ADR 0033) ---


def test_words_are_looked_up_within_a_status(tmp_path, open_store):
    # Given notes carrying "kings" in two statuses, and one that carries no title
    _note(tmp_path, "a.md", "The Way of Kings", status="Read")
    _note(tmp_path, "b.md", "Kings of the Wyld", status="To Read")
    _note(tmp_path, "c.md", "Dune", status="Read")
    _note(tmp_path, "d.md", None, authors=["Kings"], status="Read")

    store = open_store(tmp_path)

    # When the store is asked which Read notes carry "kings" or "dune"
    found = store.carrying_words({"kings", "dune"}, status="Read")

    # Then only the titled Read notes carrying one of them answer
    assert sorted(_titles(found)) == ["Dune", "The Way of Kings"]
    # And with no status, every titled note carrying the word answers
    assert sorted(_titles(store.carrying_words({"kings"}, status=None))) == [
        "Kings of the Wyld",
        "The Way of Kings",
    ]


def test_words_are_counted_per_status_over_titled_notes(tmp_path, open_store):
    # Given two Read notes, one To Read note, and one with no title
    _note(
        tmp_path,
        "a.md",
        "The Way of Kings",
        authors=["Brandon Sanderson"],
        status="Read",
    )
    _note(
        tmp_path,
        "b.md",
        "The Final Empire",
        authors=["Brandon Sanderson"],
        status="Read",
    )
    _note(
        tmp_path,
        "c.md",
        "Kings of the Wyld",
        authors=["Nicholas Eames"],
        status="To Read",
    )
    _note(tmp_path, "d.md", None, authors=["Brandon Sanderson"], status="Read")

    # When the store is asked for the Read counts
    counts = open_store(tmp_path).word_counts("Read")

    # Then they cover only the titled Read notes, a word counted once per note
    assert counts.total == 2
    assert counts.counts["sanderson"] == 2
    assert counts.counts["kings"] == 1
    assert "wyld" not in counts.counts


def test_the_whole_library_counts_the_notes_with_no_status(tmp_path, open_store):
    # Given a Read note and a note with no status at all
    _note(tmp_path, "a.md", "The Way of Kings", status="Read")
    _note(tmp_path, "b.md", "Kings of the Wyld")

    # When the store is asked for counts with no status filter
    counts = open_store(tmp_path).word_counts(None)

    # Then the note with no status is counted too. A replica that kept buckets
    # only for the four Statuses would weigh words against a smaller Shelf than
    # the local search does (ADR 0033).
    assert counts.total == 2
    assert counts.counts["kings"] == 2


def test_a_listing_is_in_title_order_up_to_a_limit(tmp_path, open_store):
    # Given three titled To Read notes, one Read note, and an untitled one
    _note(tmp_path, "a.md", "Piranesi", status="To Read")
    _note(tmp_path, "b.md", "Dune", status="To Read")
    _note(tmp_path, "c.md", "The Road", status="To Read")
    _note(tmp_path, "d.md", "Mercy", status="Read")
    _note(tmp_path, "e.md", None, status="To Read")

    # When two To Read notes are listed
    listing = open_store(tmp_path).listing("To Read", limit=2)

    # Then the first two by normalized title come back, and the total counts
    # every titled To Read note the limit cut short
    assert _titles(listing.notes) == ["Dune", "Piranesi"]
    assert listing.total == 3


def test_a_listing_with_no_status_includes_notes_with_none(tmp_path, open_store):
    # Given a Read note and one with no status
    _note(tmp_path, "a.md", "Mercy", status="Read")
    _note(tmp_path, "b.md", "Dune")

    # When everything is listed
    listing = open_store(tmp_path).listing(None, limit=10)

    # Then both answer
    assert _titles(listing.notes) == ["Dune", "Mercy"]
    assert listing.total == 2


# --- how words are split ---


def test_words_are_split_after_normalization():
    # Given a title with capitals and punctuation
    # Then it splits into the lowercase words a search compares
    assert search_tokens("The Way of Kings: Book 1") == {
        "the",
        "way",
        "of",
        "kings",
        "book",
        "1",
    }
    assert search_tokens("") == set()


# --- the same query ranks the same way from both (ADR 0020, #157) ---
def _shelf(vault):
    """Write a Shelf on which every mistake a replica could make shows.

    - "Dune" is the only Read note carrying "dune", and two Read notes carry
      "messiah", so within Read "dune" is the rarer word and "Dune" ranks
      first. Across the Library, four notes with no Status also carry "dune",
      which makes "messiah" the rarer word and puts "Messiah" first. Counting
      Read over the whole Library, or leaving out the no-Status bucket, flips
      one of those answers.
    - Three To Read notes share the title "Poems", and the lowest Libris ID is
      in neither the first nor the last file, so no scan order puts them in
      Libris ID order by accident.
    - An untitled note carries the word "herbert", which only "Dune" should.
    - A titled note whose title and authors split into no words still counts
      towards its Status's total.
    - A note with a Status outside the Library's vocabulary counts in the
      whole Library, as it does locally.
    """
    _note(vault, "dune.md", "Dune", ("Frank Herbert",), status="Read", libris_id="01D")
    _note(vault, "messiah.md", "Messiah", status="Read", libris_id="01M1")
    _note(vault, "rising.md", "Messiah Rising", status="Read", libris_id="01M2")
    for n, word in enumerate(("Road", "Sea", "Song", "Wind")):
        _note(vault, f"dune-{word}.md", f"Dune {word}", libris_id=f"01N{n}")

    _note(vault, "poems-a.md", "Poems", ("Poet A",), status="To Read", libris_id="01P3")
    _note(vault, "poems-b.md", "Poems", ("Poet B",), status="To Read", libris_id="01P1")
    _note(vault, "poems-c.md", "Poems", ("Poet C",), status="To Read", libris_id="01P2")
    _note(vault, "road.md", "The Road", status="To Read", libris_id="01R")
    _note(vault, "hot.md", "The Hot One", status="To Read", libris_id="01H")
    _note(
        vault,
        "empire.md",
        '"The Final Empire: Mistborn Book 1"',
        status="Read",
        libris_id="01E",
    )

    _note(
        vault, "untitled.md", None, ("Frank Herbert",), status="Read", libris_id="01U"
    )
    _note(vault, "wordless.md", '"--"', (), status="Read", libris_id="01W")
    _note(
        vault,
        "history.md",
        '"Mistborn: Secret History"',
        status="Abandoned",
        libris_id="01S",
    )


def _stores(vault):
    """The Shelf; a replica built from its notes in the opposite order; and Cosmos.

    Reversed because the remote returns documents in its own order, not the
    Shelf's directory order. A replica built in the same order would agree with
    the Shelf on every tie whether or not anything broke the tie. The fake Cosmos
    container returns the newest push first for the same reason.
    """
    return (
        ShelfStore(vault),
        ReplicaStore.from_notes(reversed(index_for(vault).notes())),
        pushed_to_cosmos(vault),
    )


def _answer(store, query, status, limit):
    result = search_library(store, query=query, status=status, limit=limit)
    return result.total, [note.path.name for note in result.books]


@pytest.mark.parametrize(
    ("query", "status", "limit"),
    [
        pytest.param("that mistborn one", None, 20, id="stop-and-distinctive-words"),
        pytest.param("the", None, 20, id="only-filler"),
        pytest.param("the", "To Read", 20, id="only-filler-within-a-status"),
        pytest.param("dune messiah", None, 20, id="weighed-across-the-library"),
        pytest.param("dune messiah", "Read", 20, id="weighed-within-a-status"),
        pytest.param("herbert", None, 20, id="author-words-and-an-untitled-note"),
        pytest.param("mistborn", None, 20, id="status-outside-the-vocabulary"),
        pytest.param("poems", "To Read", 2, id="ranked-ties-cut-by-the-limit"),
        pytest.param(None, None, 50, id="listing-everything"),
        pytest.param(None, "Read", 50, id="listing-a-status"),
        pytest.param(None, "To Read", 2, id="listed-ties-cut-by-the-limit"),
    ],
)
def test_a_query_answers_the_same_from_the_shelf_and_the_remote(
    tmp_path, query, status, limit
):
    # Given a Shelf, the replica sync would push from it, and Cosmos after a push
    _shelf(tmp_path)
    shelf, replica, cosmos = _stores(tmp_path)

    # When the same search is asked of all three
    # Then all return the same books, in the same order, with the same total
    expected = _answer(shelf, query, status, limit)
    assert _answer(replica, query, status, limit) == expected
    assert _answer(cosmos, query, status, limit) == expected


# The parity above would hold if both stores went wrong the same way. These pin
# what the shared answer is, so it cannot.


def test_within_a_status_words_are_weighed_across_that_status_only(tmp_path):
    # Given a Shelf where "dune" is rare within Read and common across it
    _shelf(tmp_path)

    for store in _stores(tmp_path):
        # When Read is searched for "dune messiah"
        _, names = _answer(store, "dune messiah", "Read", 20)

        # Then "Dune" leads, carrying the rarer word within Read
        assert names[0] == "dune.md"


def test_across_the_library_the_notes_with_no_status_are_weighed_too(tmp_path):
    # Given the same Shelf, where four notes with no Status carry "dune"
    _shelf(tmp_path)

    for store in _stores(tmp_path):
        # When the whole Library is searched for "dune messiah"
        _, names = _answer(store, "dune messiah", None, 20)

        # Then "Messiah" leads, "dune" being the commoner word once those count
        assert names[0] == "messiah.md"


@pytest.mark.parametrize("query", ["poems", None])
def test_notes_sharing_a_title_come_back_in_libris_id_order(tmp_path, query):
    # Given three To Read notes titled "Poems"
    _shelf(tmp_path)

    for store in _stores(tmp_path):
        # When two are asked for, by search or by listing
        total, names = _answer(store, query, "To Read", 2)

        # Then the two with the lowest Libris IDs answer, lowest first
        assert names == ["poems-b.md", "poems-c.md"]


def test_a_listing_counts_a_titled_note_that_splits_into_no_words(tmp_path):
    # Given five titled Read notes, one of which splits into no words, and an
    # untitled Read note
    _shelf(tmp_path)

    for store in _stores(tmp_path):
        # When Read is listed
        total, names = _answer(store, None, "Read", 50)

        # Then the wordless note is listed and counted, and the untitled one is not
        assert "wordless.md" in names
        assert "untitled.md" not in names
        assert total == 5


def test_a_query_of_only_filler_is_taken_at_face_value(tmp_path):
    # Given a Shelf holding "The Road"
    _shelf(tmp_path)

    for store in _stores(tmp_path):
        # When the Library is searched for "the"
        _, names = _answer(store, "the", None, 20)

        # Then "The Road" is found rather than nothing
        assert "road.md" in names


def test_a_listing_orders_titles_past_u_ffff_by_code_point_everywhere(tmp_path):
    # Given two titles that code point order and UTF-16 order disagree on:
    # U+F900 comes first by code point, U+10428 first by UTF-16 code unit, whose
    # surrogate pair begins at U+D801
    _note(tmp_path, "cjk.md", "豈", status="Read", libris_id="01B")
    _note(tmp_path, "deseret.md", "\U00010428", status="Read", libris_id="01A")

    # When the first title in Read is listed from each store
    answers = [_answer(store, None, "Read", 1) for store in _stores(tmp_path)]

    # Then every store cuts the page at the same book, the one first by code point
    assert answers == [(2, ["cjk.md"])] * 3
