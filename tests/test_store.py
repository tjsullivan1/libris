"""Tests for the stores the service asks its exact questions of (ADR 0032, ADR 0033).

Every question is asked of the Shelf and of a replica built from the same Book
Notes, and must get the same answer from both. The replica stands in for the
remote one: it holds what sync would push, and answers from the stored keys,
words and per-Status word counts rather than from the notes themselves.
"""

from libris.store import search_tokens


def _note(vault, name, title=None, authors=("An Author",), **fields):
    """Write a Book Note with exactly the frontmatter given."""
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
