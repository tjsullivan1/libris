"""Tests for what `libris sync` pushes to Cosmos, and what it refuses (#173).

What the store answers once a push is done is tested with the other stores, in
`test_store.py`. These cover the push itself: the whole note goes up, a note
the remote cannot hold is named rather than dropped in silence, two notes
sharing an identity stop the push, and the word counts are rebuilt every time
(ADR 0006, ADR 0033).
"""

import pytest
from cosmos_fake import FakeContainer

from libris.cosmos_store import (
    CosmosStore,
    CountsNotRebuilt,
    SyncRefused,
    push_shelf,
)


def _write(vault, name, frontmatter, body="\nMy notes.\n"):
    (vault / name).write_text(f"---\n{frontmatter}---\n{body}", encoding="utf-8")


def _push(vault):
    books, counts = FakeContainer(), FakeContainer()
    report = push_shelf(vault, books, counts)
    return report, books, counts


def test_the_whole_note_goes_up_under_its_libris_id(tmp_path):
    # Given a note with a key Libris does not model, and a body
    _write(
        tmp_path,
        "dune.md",
        "title: Dune\nauthors:\n  - Frank Herbert\nlibris_id: 01D\n"
        "date_added: 2024-03-01\nshelf_mood: cosy\n",
        body="\n## My notes\n\nSpice.\n",
    )

    # When the Shelf is pushed
    report, books, _ = _push(tmp_path)

    # Then the note is stored under its Libris ID, every key and the body intact
    assert report.pushed == 1
    assert report.complete
    document = books.items["01D"]
    assert document["filename"] == "dune.md"
    assert document["frontmatter"] == {
        "title": "Dune",
        "authors": ["Frank Herbert"],
        "libris_id": "01D",
        "date_added": "2024-03-01",
        "shelf_mood": "cosy",
    }
    assert document["body"] == "\n## My notes\n\nSpice.\n"
    # And the store hands the same note back
    (note,) = CosmosStore(books, FakeContainer()).with_libris_id("01D")
    assert note.frontmatter == document["frontmatter"]
    assert note.body == "\n## My notes\n\nSpice.\n"


def test_two_notes_sharing_a_libris_id_stop_the_push_and_are_named(tmp_path):
    # Given two notes claiming one Libris ID, and one that is fine
    _write(tmp_path, "dune.md", "title: Dune\nlibris_id: 01SAME\n")
    _write(tmp_path, "dune-copy.md", "title: Dune\nlibris_id: 01SAME\n")
    _write(tmp_path, "emma.md", "title: Emma\nlibris_id: 01E\n")
    books, counts = FakeContainer(), FakeContainer()

    # When the Shelf is pushed
    with pytest.raises(SyncRefused) as refused:
        push_shelf(tmp_path, books, counts)

    # Then it refuses, naming the contested ID and both notes
    (collision,) = refused.value.collisions
    assert collision.libris_id == "01SAME"
    assert [n.path.name for n in collision.notes] == ["dune-copy.md", "dune.md"]
    # And nothing at all went up, not even the note that was fine
    assert books.items == {}
    assert counts.items == {}


def test_a_note_without_a_libris_id_is_reported_not_skipped(tmp_path):
    # Given a titled note with no Libris ID beside one with
    _write(tmp_path, "dune.md", "title: Dune\nlibris_id: 01D\n")
    _write(tmp_path, "nameless.md", "title: Emma\n")

    # When the Shelf is pushed
    report, books, _ = _push(tmp_path)

    # Then the rest go up, and the push says which one did not
    assert list(books.items) == ["01D"]
    assert [path.name for path in report.without_id] == ["nameless.md"]
    assert not report.complete


def test_a_note_json_would_change_is_reported_not_altered(tmp_path):
    # Given a note whose passthrough key is a number, which JSON makes text
    _write(tmp_path, "dune.md", "title: Dune\nlibris_id: 01D\n1984: yes\n")

    # When the Shelf is pushed
    report, books, _ = _push(tmp_path)

    # Then it is not pushed changed, and the push says so
    assert books.items == {}
    ((path, reason),) = report.not_storable
    assert path.name == "dune.md"
    assert "JSON" in reason


def test_the_word_counts_describe_what_was_pushed(tmp_path):
    # Given two Read notes, one of which cannot go up
    _write(tmp_path, "dune.md", "title: Dune\nstatus: Read\nlibris_id: 01D\n")
    _write(tmp_path, "nameless.md", "title: Dune Messiah\nstatus: Read\n")

    # When the Shelf is pushed
    _, books, counts = _push(tmp_path)

    # Then the counts weigh only the note the remote holds
    store = CosmosStore(books, counts)
    assert store.word_counts("Read").total == 1
    assert "messiah" not in store.word_counts(None).counts


def test_the_word_counts_are_rebuilt_even_when_nothing_is_pushed(tmp_path):
    # Given counts left by an earlier push, and a Shelf now empty
    counts = FakeContainer()
    counts.upsert_item({"id": "word-counts", "buckets": [{"status": "Read"}]})

    # When the Shelf is pushed
    push_shelf(tmp_path, FakeContainer(), counts)

    # Then the counts are replaced, describing an empty Library
    assert counts.items["word-counts"]["buckets"] == []


def test_a_failed_rebuild_fails_the_push(tmp_path):
    # Given a Shelf, and a word-counts container that refuses writes
    _write(tmp_path, "dune.md", "title: Dune\nlibris_id: 01D\n")
    books, counts = FakeContainer(), FakeContainer()
    counts.fail_writes = OSError("Cosmos is unreachable")

    # When the Shelf is pushed
    # Then the push fails, saying why, though the notes went up
    with pytest.raises(CountsNotRebuilt, match="unreachable"):
        push_shelf(tmp_path, books, counts)
    assert list(books.items) == ["01D"]
