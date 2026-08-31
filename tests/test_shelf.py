"""The Shelf index, and the promise that it is never stale.

The measurement that prompted it: against a 3,061-note Shelf, one exact-match
query took 8 seconds and one Near Match query 13, because both read and parsed
every note. Nothing caught it because every other test uses a vault holding one
or two notes - so these tests count parses rather than trusting a stopwatch.
"""

from pathlib import Path

import pytest

from libris import service, shelf
from libris.api import BookCandidate
from libris.markdown import BookNote, create_book_note


@pytest.fixture(autouse=True)
def fresh_indexes():
    """Start every test with no index, and leave none behind.

    The index lives for the life of the process, which is the point of it, and
    a test run is one process holding many Shelves.
    """
    shelf.forget_indexes()
    yield
    shelf.forget_indexes()


@pytest.fixture
def counted_reads(monkeypatch):
    """Count how many notes get parsed, which is the cost being removed."""
    reads = []
    original = BookNote.read

    def _counted(path):
        reads.append(Path(path).name)
        return original(path)

    monkeypatch.setattr(BookNote, "read", staticmethod(_counted))
    return reads


def _shelve(vault_path, title, author="Frank Herbert", **fields):
    """Put a Book Note on a Shelf."""
    return create_book_note(
        BookCandidate(title=title, authors=[author], **fields), vault_path
    )


def test_a_note_is_parsed_once_however_often_it_is_asked_about(tmp_path, counted_reads):
    # Given a Shelf of three books
    for title in ("Dune", "Piranesi", "Mercy"):
        _shelve(tmp_path, title)
    index = shelf.index_for(tmp_path)

    # When the Shelf is read four times
    for _ in range(4):
        index.notes()

    # Then each note was parsed once, not four times. This is the whole saving:
    # the daemon asks twice per popup flow and the files have not changed.
    assert sorted(counted_reads) == sorted([p.name for p in tmp_path.glob("*.md")]), (
        counted_reads
    )


def test_a_book_added_outside_libris_is_seen_immediately(tmp_path):
    # Given a Shelf that has already been read
    _shelve(tmp_path, "Dune")
    index = shelf.index_for(tmp_path)
    assert len(index.notes()) == 1

    # When something else writes a note - Obsidian, or another Libris process
    _shelve(tmp_path, "Piranesi", author="Susanna Clarke")

    # Then the next question sees it. The index is revalidated per call, so the
    # daemon's live-Shelf guarantee survives it (ADR 0010).
    assert {n.title for n in index.notes()} == {"Dune", "Piranesi"}


def test_a_book_edited_outside_libris_is_re_read(tmp_path):
    # Given a Shelf that has already been read
    path = _shelve(tmp_path, "Dune")
    index = shelf.index_for(tmp_path)
    assert index.notes()[0].frontmatter.get("status") == "To Read"

    # When the note is edited in Obsidian
    path.write_text(
        path.read_text(encoding="utf-8").replace("status: To Read", "status: Read"),
        encoding="utf-8",
    )

    # Then the change is visible, not masked by what was parsed before
    assert index.notes()[0].frontmatter.get("status") == "Read"


def test_a_book_deleted_outside_libris_stops_being_reported(tmp_path):
    # Given a Shelf of two books, already read
    _shelve(tmp_path, "Dune")
    doomed = _shelve(tmp_path, "Piranesi", author="Susanna Clarke")
    index = shelf.index_for(tmp_path)
    assert len(index.notes()) == 2

    # When one is deleted
    doomed.unlink()

    # Then it is gone from the answer rather than lingering as a duplicate that
    # cannot be found on disk
    assert {n.title for n in index.notes()} == {"Dune"}


def test_only_the_changed_note_is_re_read(tmp_path, counted_reads):
    # Given a Shelf of three books, already read
    for title in ("Dune", "Piranesi", "Mercy"):
        _shelve(tmp_path, title)
    index = shelf.index_for(tmp_path)
    index.notes()
    counted_reads.clear()

    # When one of them is edited
    edited = tmp_path / "Dune - Frank Herbert.md"
    edited.write_text(
        edited.read_text(encoding="utf-8").replace("status: To Read", "status: Read"),
        encoding="utf-8",
    )
    index.notes()

    # Then only that one is parsed again
    assert counted_reads == [edited.name]


def test_a_note_that_will_not_parse_is_skipped_and_not_re_read(tmp_path, counted_reads):
    # Given a file that is not a Book Note at all
    (tmp_path / "Broken.md").write_text("no frontmatter here", encoding="utf-8")
    _shelve(tmp_path, "Dune")
    index = shelf.index_for(tmp_path)

    # When the Shelf is read twice
    assert [n.title for n in index.notes()] == ["Dune"]
    counted_reads.clear()
    index.notes()

    # Then the broken file is left out both times, and is not re-parsed on the
    # second - a Shelf with a damaged note should not pay for it every call
    assert counted_reads == []


def test_a_repaired_note_is_picked_up(tmp_path):
    # Given a file that would not parse
    broken = tmp_path / "Broken.md"
    broken.write_text("no frontmatter here", encoding="utf-8")
    index = shelf.index_for(tmp_path)
    assert index.notes() == []

    # When it is repaired
    broken.write_text(
        "---\ntitle: Dune\nauthors:\n- Frank Herbert\n---\n", encoding="utf-8"
    )

    # Then it joins the Shelf, rather than being remembered as broken forever
    assert [n.title for n in index.notes()] == ["Dune"]


def test_a_shelf_that_is_not_there_holds_no_books(tmp_path):
    # Given a configured Shelf that does not exist
    missing = tmp_path / "gone"

    # When it is read
    # Then it answers empty rather than raising, so a Surface reports a miss
    assert shelf.index_for(missing).notes() == []


def test_two_shelves_do_not_see_each_other(tmp_path):
    # Given two Shelves
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    _shelve(first, "Dune")
    _shelve(second, "Piranesi", author="Susanna Clarke")

    # Then each answers for itself
    assert [n.title for n in shelf.index_for(first).notes()] == ["Dune"]
    assert [n.title for n in shelf.index_for(second).notes()] == ["Piranesi"]


def test_a_miss_reads_the_shelf_once_not_twice(tmp_path, counted_reads):
    # Given a Shelf whose titles resemble what is being looked up
    for title in ("The Brass Verdict: A Novel", "Dune", "Mercy"):
        _shelve(tmp_path, title, author="Michael Connelly")
    shelf.index_for(tmp_path).notes()
    counted_reads.clear()

    # When a Surface asks whether a Book is held and what nearly matches it -
    # which is what GET /api/v1/books does on every miss
    assert (
        service.find_existing(
            tmp_path, title="The Brass Verdict", authors=["Michael Connelly"]
        )
        is None
    )
    near = service.find_similar(
        tmp_path, title="The Brass Verdict", authors=["Michael Connelly"]
    )

    # Then the Near Match is offered, and neither question re-read a thing.
    # Before the index these were two full passes over the Shelf.
    assert [n.title for n in near] == ["The Brass Verdict: A Novel"]
    assert counted_reads == []


def test_a_large_shelf_is_read_once_and_then_left_alone(tmp_path, counted_reads):
    # Given a Shelf big enough for the cost to matter. The real one holds 3,061
    # notes and took 8 seconds a query; a two-note fixture is what hid that.
    for n in range(300):
        _shelve(tmp_path, f"Book {n:03d}")

    # When it is queried the way a popup queries it - twice per flow
    for _ in range(2):
        service.find_existing(tmp_path, isbn="9780441013593")
        service.find_similar(tmp_path, title="Book 001", authors=["Frank Herbert"])

    # Then the Shelf was parsed once, not four times over
    assert len(counted_reads) == 300
