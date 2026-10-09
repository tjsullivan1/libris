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
    DeletionsRefused,
    SyncInProgress,
    SyncRefused,
    _sync_lock,
    push_shelf,
)
from libris.shelf import index_for
from libris.store import SPLIT_VERSION


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


def test_a_note_of_non_ascii_text_is_measured_as_the_sdk_escapes_it(tmp_path):
    # Given a note of 400,000 accented letters: 0.8 MB as UTF-8, but 2.4 MB as
    # the escaped JSON the SDK sends unless compact UTF-8 writes are enabled
    _write(tmp_path, "dune.md", "title: Dune\nlibris_id: 01D\n", body="é" * 400_000)
    _write(tmp_path, "emma.md", "title: Emma\nlibris_id: 01E\n")

    # When the Shelf is pushed
    report, books, _ = _push(tmp_path)

    # Then it is reported, rather than sent to fail and stop the push
    assert list(books.items) == ["01E"]
    ((path, why),) = report.not_storable
    assert path.name == "dune.md"
    assert "2 MB" in why


# --- pushing only what changed (#174, ADR 0015) ---


def _shelf_of(vault, count):
    for n in range(count):
        _write(vault, f"book-{n:02}.md", f"title: Book {n}\nlibris_id: 01B{n:02}\n")


def _pushed(vault, count):
    """A Shelf of `count` notes, pushed once, with the writes that took forgotten."""
    _shelf_of(vault, count)
    books, counts = FakeContainer(), FakeContainer()
    push_shelf(vault, books, counts)
    books.writes.clear()
    counts.writes.clear()
    return books, counts


def test_a_second_push_with_nothing_changed_writes_nothing(tmp_path):
    # Given a Shelf already pushed
    books, counts = _pushed(tmp_path, 3)

    # When it is pushed again, unchanged
    report = push_shelf(tmp_path, books, counts)

    # Then nothing is written to Cosmos at all - not a note, not the counts
    assert books.writes == []
    assert counts.writes == []
    assert report.pushed == 0
    assert report.unchanged == 3


def test_editing_one_note_pushes_exactly_that_note(tmp_path):
    # Given a Shelf already pushed
    books, counts = _pushed(tmp_path, 3)

    # When one note is edited and the Shelf pushed again
    _write(tmp_path, "book-01.md", "title: Book 1\nlibris_id: 01B01\nstatus: Read\n")
    report = push_shelf(tmp_path, books, counts)

    # Then that note alone goes up, and the counts weigh its new Status
    assert books.writes == [("upsert", "01B01")]
    assert report.pushed == 1
    assert CosmosStore(books, counts).word_counts("Read").total == 1


def test_renaming_a_note_pushes_it_under_its_new_filename(tmp_path):
    # Given a Shelf already pushed
    books, counts = _pushed(tmp_path, 3)

    # When one note is renamed, its content untouched, and the Shelf pushed again
    (tmp_path / "book-01.md").rename(tmp_path / "Book 1 - Someone.md")
    push_shelf(tmp_path, books, counts)

    # Then that note goes up again, carrying the name it now has
    assert books.writes == [("upsert", "01B01")]
    assert books.items["01B01"]["filename"] == "Book 1 - Someone.md"


def test_a_note_that_left_the_shelf_has_its_remote_document_deleted(tmp_path):
    # Given a Shelf already pushed
    books, counts = _pushed(tmp_path, 3)

    # When one note is deleted and the Shelf pushed again
    (tmp_path / "book-01.md").unlink()
    report = push_shelf(tmp_path, books, counts)

    # Then its document goes too, and the counts no longer weigh it
    assert books.writes == [("delete", "01B01")]
    assert sorted(books.items) == ["01B00", "01B02"]
    assert report.deleted == 1
    assert CosmosStore(books, counts).word_counts(None).total == 2


def test_a_document_already_gone_from_the_remote_is_not_an_error(tmp_path):
    # Given a pushed note whose document someone has since removed by hand
    books, counts = _pushed(tmp_path, 2)
    del books.items["01B01"]

    # When the note leaves the Shelf too, and the Shelf is pushed
    (tmp_path / "book-01.md").unlink()
    report = push_shelf(tmp_path, books, counts)

    # Then the sync finishes, and does not try to delete it again next time
    assert report.deleted == 1
    books.writes.clear()
    push_shelf(tmp_path, books, counts)
    assert books.writes == []


def test_implausibly_many_deletions_stop_the_sync_before_any_write(tmp_path):
    # Given a Shelf of 30 already pushed
    books, counts = _pushed(tmp_path, 30)

    # When 21 of them vanish - more than a tidy-up removes - beside one edit
    for n in range(21):
        (tmp_path / f"book-{n:02}.md").unlink()
    _write(tmp_path, "book-29.md", "title: Book 29\nlibris_id: 01B29\nstatus: Read\n")

    # Then the sync refuses, saying how many, and writes nothing at all
    with pytest.raises(DeletionsRefused) as refused:
        push_shelf(tmp_path, books, counts)
    assert refused.value.deleting == 21
    assert refused.value.held == 30
    assert books.writes == []
    assert counts.writes == []

    # And being told they are meant lets them through
    report = push_shelf(tmp_path, books, counts, allow_mass_deletion=True)
    assert report.deleted == 21
    assert len(books.items) == 9


def test_as_many_deletions_as_a_tidy_up_makes_go_through(tmp_path):
    # Given a Shelf of 30 already pushed
    books, counts = _pushed(tmp_path, 30)

    # When 20 of them are removed - the most the guard lets by unasked
    for n in range(20):
        (tmp_path / f"book-{n:02}.md").unlink()
    report = push_shelf(tmp_path, books, counts)

    # Then they are deleted without being confirmed
    assert report.deleted == 20


def test_a_shelf_that_scans_empty_deletes_nothing(tmp_path):
    # Given a Shelf of two already pushed
    books, counts = _pushed(tmp_path, 2)

    # When it scans empty, as an unmounted drive or a half-finished sync would
    for path in tmp_path.glob("*.md"):
        path.unlink()

    # Then the sync refuses, few as the deletions are, and the remote is kept
    with pytest.raises(DeletionsRefused) as refused:
        push_shelf(tmp_path, books, counts)
    assert refused.value.scanned_empty
    assert len(books.items) == 2
    assert counts.writes == []


def test_an_unreadable_note_holds_back_every_deletion(tmp_path, lock_note):
    # Given a Shelf already pushed
    books, counts = _pushed(tmp_path, 3)
    index_for(tmp_path).notes()

    # When one note leaves, and another cannot be read - so whether it is still
    # the note the remote holds, or has gone too, cannot be told
    (tmp_path / "book-00.md").unlink()
    lock_note(tmp_path / "book-02.md", reads=True)
    report = push_shelf(tmp_path, books, counts)

    # Then nothing is deleted this time, and the report says deletions waited
    assert books.writes == []
    assert report.deletions_held == 1
    assert not report.complete


def test_a_lost_state_file_means_the_next_push_sends_everything(
    tmp_path, mock_config_dir
):
    # Given a Shelf already pushed
    books, counts = _pushed(tmp_path, 3)

    # When the state file is lost, and the Shelf pushed again
    (mock_config_dir / "sync-state.json").unlink()
    report = push_shelf(tmp_path, books, counts)

    # Then every note goes up, and the state is rebuilt so the next sends none
    assert report.pushed == 3
    assert len(books.writes) == 3
    books.writes.clear()
    push_shelf(tmp_path, books, counts)
    assert books.writes == []


def test_a_note_that_left_while_the_state_was_lost_is_still_deleted(
    tmp_path, mock_config_dir
):
    # Given a Shelf already pushed, whose state file is then lost
    books, counts = _pushed(tmp_path, 3)
    (mock_config_dir / "sync-state.json").unlink()

    # When a note leaves the Shelf and it is pushed again
    (tmp_path / "book-01.md").unlink()
    report = push_shelf(tmp_path, books, counts)

    # Then the remote is asked what it holds, and the note's document goes
    assert "01B01" not in books.items
    assert report.deleted == 1


def test_a_note_that_left_while_syncing_elsewhere_is_deleted_on_return(tmp_path):
    # Given a Shelf pushed to one account, then to another
    books, counts = FakeContainer(), FakeContainer()
    _shelf_of(tmp_path, 3)
    push_shelf(tmp_path, books, counts, target="https://a#libris")
    push_shelf(tmp_path, FakeContainer(), FakeContainer(), target="https://b#libris")

    # When a note leaves, and the Shelf is pushed to the first account again
    (tmp_path / "book-01.md").unlink()
    push_shelf(tmp_path, books, counts, target="https://a#libris")

    # Then the first account loses the note too, though no state remembered it
    assert sorted(books.items) == ["01B00", "01B02"]


def test_a_shelf_that_scans_empty_is_refused_even_with_no_state(
    tmp_path, mock_config_dir
):
    # Given a Shelf already pushed, whose state file is then lost
    books, counts = _pushed(tmp_path, 2)
    (mock_config_dir / "sync-state.json").unlink()

    # When the Shelf scans empty
    for path in tmp_path.glob("*.md"):
        path.unlink()

    # Then the sync refuses, rather than rebuilding the counts as empty
    with pytest.raises(DeletionsRefused) as refused:
        push_shelf(tmp_path, books, counts)
    assert refused.value.scanned_empty
    assert counts.writes == []
    assert len(books.items) == 2


def test_an_empty_shelf_pushed_to_an_empty_remote_is_not_refused(tmp_path):
    # Given a Shelf with no notes, and a remote with none either
    books, counts = FakeContainer(), FakeContainer()

    # When it is pushed
    report = push_shelf(tmp_path, books, counts)

    # Then there is nothing to lose, so it goes through
    assert report.pushed == 0
    assert "word-counts" in counts.items


def test_an_ordinary_sync_does_not_list_the_remote(tmp_path):
    # Given a Shelf already pushed
    books, counts = _pushed(tmp_path, 3)
    books.queries.clear()

    # When it is pushed again
    push_shelf(tmp_path, books, counts)

    # Then the remote's inventory is not fetched: the state is trusted
    assert books.queries == []


def test_a_note_whose_frontmatter_breaks_is_not_deleted_for_it(tmp_path):
    # Given a Shelf already pushed
    books, counts = _pushed(tmp_path, 3)

    # When one note's frontmatter is broken by an edit, and the Shelf pushed
    (tmp_path / "book-01.md").write_text(
        "---\ntitle: [Book 1\nlibris_id: 01B01\n---\n", encoding="utf-8"
    )
    report = push_shelf(tmp_path, books, counts)

    # Then its document stays, the file is named, and the sync is not complete
    assert "01B01" in books.items
    assert [path.name for path in report.unparseable] == ["book-01.md"]
    assert report.deletions_held == 1
    assert not report.complete


def test_a_shelf_of_only_broken_notes_is_named_not_called_empty(tmp_path):
    # Given a Shelf of two already pushed
    books, counts = _pushed(tmp_path, 2)

    # When every note's frontmatter is broken, so the scan finds no note
    for n in range(2):
        (tmp_path / f"book-{n:02}.md").write_text(
            f"---\ntitle: [Book {n}\n---\n", encoding="utf-8"
        )
    report = push_shelf(tmp_path, books, counts)

    # Then it is not refused as empty: the files are named, nothing is deleted
    assert sorted(p.name for p in report.unparseable) == ["book-00.md", "book-01.md"]
    assert report.deletions_held == 2
    assert len(books.items) == 2


def test_a_sync_while_another_runs_is_refused_before_any_write(tmp_path):
    # Given a Shelf, and a sync already holding the lock
    _shelf_of(tmp_path, 2)
    books, counts = FakeContainer(), FakeContainer()

    # When a second sync starts while it does
    with _sync_lock():
        with pytest.raises(SyncInProgress):
            push_shelf(tmp_path, books, counts)

    # Then the second wrote nothing, and once the first is done a sync runs
    assert books.writes == [] and counts.writes == []
    assert push_shelf(tmp_path, books, counts).pushed == 2


def test_a_kept_document_is_counted_by_the_words_it_was_stored_with(
    tmp_path, monkeypatch, lock_note
):
    # Given a Shelf pushed when words were split one way
    books, counts = _pushed(tmp_path, 3)
    index_for(tmp_path).notes()
    import libris.store

    # When the splitting changes, while one note cannot be read so its
    # document is kept with the words it was stored with
    split = libris.store.note_words
    monkeypatch.setattr(
        "libris.store.note_words",
        lambda note: frozenset(word + "x" for word in split(note)),
    )
    monkeypatch.setattr("libris.cosmos_store.SPLIT_VERSION", SPLIT_VERSION + 1)
    lock_note(tmp_path / "book-02.md", reads=True)
    push_shelf(tmp_path, books, counts)

    # Then the counts weigh the kept document by its stored, old-split words -
    # the ones a query will match it on - and the others by their new ones
    weights = CosmosStore(books, counts).word_counts(None).counts
    assert books.items["01B02"]["words"] == ["2", "book"]
    assert weights["book"] == 1
    assert weights["bookx"] == 2


def test_a_recreated_remote_is_not_held_to_what_the_old_one_held(tmp_path):
    # Given a Shelf of 30 pushed to an account
    _pushed(tmp_path, 30)

    # When 21 notes leave, and the account's containers are recreated empty
    for n in range(21):
        (tmp_path / f"book-{n:02}.md").unlink()
    books, counts = FakeContainer(), FakeContainer()
    report = push_shelf(tmp_path, books, counts)

    # Then nothing is refused, since the new remote holds none of the 21, and
    # the nine still on the Shelf go up
    assert report.pushed == 9
    assert report.deleted == 0
    assert len(books.items) == 9


def test_an_id_given_up_between_the_two_reads_is_deleted(tmp_path, monkeypatch):
    # Given a pushed note, which the index read while it held its first ID
    books, counts = _pushed(tmp_path, 2)
    listed = index_for(tmp_path).notes()

    # When its ID changes before sync reads it again
    _write(tmp_path, "book-01.md", "title: Book 1\nlibris_id: 01NEW\n")

    class _Stale:
        unreadable: list = []
        unparseable: list = []

        def notes(self):
            return listed

    monkeypatch.setattr("libris.cosmos_store.index_for", lambda vault: _Stale())
    push_shelf(tmp_path, books, counts)

    # Then the note goes up under the ID it now has, and the old one goes
    assert sorted(books.items) == ["01B00", "01NEW"]


def test_a_document_kept_for_a_note_cosmos_cannot_hold_is_still_counted(tmp_path):
    # Given two pushed notes
    books, counts = _pushed(tmp_path, 2)

    # When one gains a key JSON cannot carry, so it can no longer go up
    _write(tmp_path, "book-01.md", "title: Book 1\nlibris_id: 01B01\n1984: yes\n")
    report = push_shelf(tmp_path, books, counts)

    # Then its old document stays, and the counts still weigh both notes
    assert report.not_storable
    assert sorted(books.items) == ["01B00", "01B01"]
    assert CosmosStore(books, counts).word_counts(None).total == 2


def test_a_repush_that_leaves_a_document_unreplaced_keeps_the_old_version(
    tmp_path, monkeypatch, lock_note
):
    # Given a Shelf pushed when words were split one way
    books, counts = _pushed(tmp_path, 3)
    index_for(tmp_path).notes()

    # When the splitting changes while one note cannot be read
    monkeypatch.setattr("libris.cosmos_store.SPLIT_VERSION", SPLIT_VERSION + 1)
    lock_note(tmp_path / "book-02.md", reads=True)
    report = push_shelf(tmp_path, books, counts)

    # Then the old version stands, since that note's document is split the old
    # way, and the counts still weigh it
    assert not report.complete
    assert counts.items["word-counts"]["split_version"] == SPLIT_VERSION
    assert CosmosStore(books, counts).word_counts(None).total == 3

    # And the next sync that can read it re-pushes everything and records it
    lock_note(tmp_path / "book-02.md", reads=False)
    books.writes.clear()
    report = push_shelf(tmp_path, books, counts)
    assert report.repushed_all
    assert len(books.writes) == 3
    assert counts.items["word-counts"]["split_version"] == SPLIT_VERSION + 1


def test_the_state_file_is_kept_outside_the_shelf(tmp_path, mock_config_dir):
    # Given a Shelf
    _shelf_of(tmp_path, 1)

    # When it is pushed
    push_shelf(tmp_path, FakeContainer(), FakeContainer())

    # Then the state lands in Libris's config directory, keyed by Libris ID
    state = json.loads((mock_config_dir / "sync-state.json").read_text("utf-8"))
    assert list(state["hashes"]) == ["01B00"]
    assert sorted(p.name for p in tmp_path.iterdir() if p.is_file()) == ["book-00.md"]


def test_a_push_to_another_account_sends_everything(tmp_path):
    # Given a Shelf pushed to one account, whose counts are then copied to a
    # second - so only the state could tell the second it holds no notes
    _shelf_of(tmp_path, 2)
    counts = FakeContainer()
    push_shelf(tmp_path, FakeContainer(), counts, target="https://a#libris")

    # When it is pushed to the second
    books = FakeContainer()
    report = push_shelf(tmp_path, books, counts, target="https://b#libris")

    # Then the state of the first is not trusted for the second
    assert report.pushed == 2
    assert sorted(books.items) == ["01B00", "01B01"]


def test_an_empty_remote_is_sent_everything_whatever_the_state_says(tmp_path):
    # Given a Shelf already pushed
    _pushed(tmp_path, 2)

    # When it is pushed to a remote that holds nothing - recreated, say
    books = FakeContainer()
    report = push_shelf(tmp_path, books, FakeContainer())

    # Then every note goes up: no counts means no sync finished there
    assert report.pushed == 2
    assert sorted(books.items) == ["01B00", "01B01"]


def test_a_change_to_how_words_split_repushes_everything(tmp_path, monkeypatch):
    # Given a Shelf pushed when words were split one way
    books, counts = _pushed(tmp_path, 3)

    # When the splitting changes, and the Shelf is pushed
    monkeypatch.setattr("libris.cosmos_store.SPLIT_VERSION", SPLIT_VERSION + 1)
    report = push_shelf(tmp_path, books, counts)

    # Then every note goes up again, and the new version is recorded
    assert sorted(books.writes) == [("upsert", f"01B0{n}") for n in range(3)]
    assert report.repushed_all
    assert counts.items["word-counts"]["split_version"] == SPLIT_VERSION + 1


def _fail_third_write(books, monkeypatch):
    """Make `books` refuse its third write from now; return what undoes that."""
    upsert = books.upsert_item
    start = len(books.writes)

    def _fails_third(body):
        if len(books.writes) - start == 2:
            raise OSError("throttled")
        return upsert(body)

    monkeypatch.setattr(books, "upsert_item", _fails_third)
    return lambda: monkeypatch.setattr(books, "upsert_item", upsert)


def test_a_repush_that_fails_partway_is_retried_in_full(tmp_path, monkeypatch):
    # Given a Shelf pushed when words were split one way
    books, counts = _pushed(tmp_path, 3)

    # When the splitting changes and the re-push fails after two notes went up
    monkeypatch.setattr("libris.cosmos_store.SPLIT_VERSION", SPLIT_VERSION + 1)
    restore = _fail_third_write(books, monkeypatch)
    with pytest.raises(OSError, match="throttled"):
        push_shelf(tmp_path, books, counts)
    assert len(books.writes) == 2

    # Then the old version stands, so the next sync re-pushes all three - not
    # only the one the failed run did not reach
    assert counts.items["word-counts"]["split_version"] == SPLIT_VERSION
    restore()
    books.writes.clear()
    report = push_shelf(tmp_path, books, counts)
    assert report.repushed_all
    assert len(books.writes) == 3
    assert counts.items["word-counts"]["split_version"] == SPLIT_VERSION + 1


def test_a_push_that_fails_partway_keeps_what_did_go_up(tmp_path, monkeypatch):
    # Given a Shelf, and a remote that fails on the third write
    _shelf_of(tmp_path, 4)
    books, counts = FakeContainer(), FakeContainer()
    restore = _fail_third_write(books, monkeypatch)
    with pytest.raises(OSError):
        push_shelf(tmp_path, books, counts)

    # When the counts are in place and the Shelf is pushed again
    restore()
    counts.upsert_item(
        {"id": "word-counts", "buckets": [], "split_version": SPLIT_VERSION}
    )
    books.writes.clear()
    report = push_shelf(tmp_path, books, counts)

    # Then only the two that never went up are sent
    assert report.pushed == 2
    assert len(books.writes) == 2
