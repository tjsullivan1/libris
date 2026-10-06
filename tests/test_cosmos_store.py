"""Tests for what `libris sync` pushes to Cosmos, and what it refuses (#173).

What the store answers once a push is done is tested with the other stores, in
`test_store.py`. These cover the push itself: the whole note goes up, a note
the remote cannot hold is named rather than dropped in silence, two notes
sharing an identity stop the push, and the word counts are rebuilt every time
(ADR 0006, ADR 0033).
"""

import json

import pytest
from cosmos_fake import FakeContainer

from libris.cosmos_store import (
    CosmosStore,
    CountsMissing,
    CountsNotRebuilt,
    SyncRefused,
    push_shelf,
)
from libris.shelf import index_for


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


def test_a_note_that_cannot_be_reread_is_reported_not_raised(tmp_path, lock_note):
    # Given a Shelf the index has already read
    _write(tmp_path, "dune.md", "title: Dune\nlibris_id: 01D\n")
    _write(tmp_path, "emma.md", "title: Emma\nlibris_id: 01E\n")
    index_for(tmp_path).notes()

    # And one note is then locked, as a sync tool or an editor can hold it
    lock_note(tmp_path / "emma.md", reads=True)

    # When the Shelf is pushed
    report, books, _ = _push(tmp_path)

    # Then the rest go up, and the locked note is named rather than raised
    assert list(books.items) == ["01D"]
    assert [path.name for path in report.unreadable] == ["emma.md"]
    assert not report.complete


@pytest.mark.parametrize(
    ("frontmatter", "body", "reason"),
    [
        pytest.param(
            "title: Dune\nlibris_id: " + "X" * 1024 + "\n",
            "",
            "1023 bytes",
            id="an-id-longer-than-cosmos-allows",
        ),
        pytest.param(
            "title: Dune\nlibris_id: 01D\n",
            "x" * (2 * 1024 * 1024),
            "2 MB",
            id="a-note-larger-than-one-item",
        ),
    ],
)
def test_a_note_past_a_cosmos_limit_is_reported_before_any_write(
    tmp_path, frontmatter, body, reason
):
    # Given a note Cosmos would refuse to write, beside one it would accept
    _write(tmp_path, "big.md", frontmatter, body=body)
    _write(tmp_path, "emma.md", "title: Emma\nlibris_id: 01E\n")

    # When the Shelf is pushed
    report, books, _ = _push(tmp_path)

    # Then the push finishes, naming the note and why, instead of failing on it
    assert list(books.items) == ["01E"]
    ((path, why),) = report.not_storable
    assert path.name == "big.md"
    assert reason in why


def test_an_id_shared_only_by_the_time_of_the_reread_still_stops_the_push(
    tmp_path, monkeypatch
):
    # Given two notes sharing an ID, which the first check did not see - as when
    # a note is edited between that check and the push reading it again
    _write(tmp_path, "dune.md", "title: Dune\nlibris_id: 01SAME\n")
    _write(tmp_path, "emma.md", "title: Emma\nlibris_id: 01SAME\n")
    monkeypatch.setattr("libris.cosmos_store.find_id_collisions", lambda vault: [])
    books, counts = FakeContainer(), FakeContainer()

    # When the Shelf is pushed
    with pytest.raises(SyncRefused) as refused:
        push_shelf(tmp_path, books, counts)

    # Then it refuses on the notes it actually read, and nothing went up
    (collision,) = refused.value.collisions
    assert [n.path.name for n in collision.notes] == ["dune.md", "emma.md"]
    assert books.items == {}


def test_a_remote_no_sync_finished_on_says_so_rather_than_answering_empty(tmp_path):
    # Given books pushed by a sync whose counts rebuild failed
    _write(tmp_path, "dune.md", "title: Dune\nstatus: Read\nlibris_id: 01D\n")
    books, counts = FakeContainer(), FakeContainer()
    counts.fail_writes = OSError("throttled")
    with pytest.raises(CountsNotRebuilt):
        push_shelf(tmp_path, books, counts)
    store = CosmosStore(books, counts)

    # When it is asked for a listing or for the word counts
    # Then it says no sync has finished, rather than a total of none
    with pytest.raises(CountsMissing):
        store.listing("Read", limit=10)
    with pytest.raises(CountsMissing):
        store.word_counts(None)


def test_a_note_is_measured_as_cosmos_counts_it(tmp_path):
    # Given a note just under 2 MB as compact JSON, which the spaces a default
    # `json.dumps` puts after every comma in its 4,000-item list would push over
    items = "".join(f"  - t{n}\n" for n in range(4000))
    _write(tmp_path, "dune.md", f"title: Dune\nlibris_id: 01D\ntags:\n{items}")
    room = 2 * 1024 * 1024 - len(
        json.dumps(_push(tmp_path)[1].items["01D"], separators=(",", ":")).encode()
    )
    _write(
        tmp_path,
        "dune.md",
        f"title: Dune\nlibris_id: 01D\ntags:\n{items}",
        body="\nMy notes.\n" + "x" * (room - 2000),
    )

    # When the Shelf is pushed
    report, books, _ = _push(tmp_path)

    # Then it goes up, since the SDK sends it compact
    assert report.not_storable == []
    assert list(books.items) == ["01D"]
