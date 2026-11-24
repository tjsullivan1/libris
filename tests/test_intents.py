"""Remote adds, queued as Intents and applied by sync (#176, ADR 0002, ADR 0013).

A Surface with no Shelf records an add-Intent after checking the replica for
the Book. The next sync applies it to the Shelf and settles it: applied when it
wrote a note, absorbed when the Library already held the Book. These drive the
service and the sync step directly; the tool answers are in
`test_mcp_server.py`, and the sync command's output in `test_cli.py`.
"""

from datetime import UTC, datetime

import pytest
from cosmos_fake import FakeContainer

from libris import service, shelf
from libris.api import BookCandidate
from libris.cosmos_store import CosmosStore, push_shelf
from libris.intents import IntentQueue, apply_pending
from libris.markdown import BookNote, create_book_note
from libris.store import ReplicaStore

MISTBORN = BookCandidate(
    title="Mistborn",
    authors=["Brandon Sanderson"],
    isbn="9780765311788",
    google_books_id="mist-1",
)


@pytest.fixture(autouse=True)
def fresh_indexes():
    """No index survives into another test's temporary Shelf."""
    shelf.forget_indexes()
    yield
    shelf.forget_indexes()


def _notes(vault):
    return sorted(p.name for p in vault.glob("*.md"))


def _queued(vault, candidate=MISTBORN, fields=None):
    """An add-Intent recorded as the hosted server records one; the container."""
    intents = FakeContainer()
    result = service.queue_add(
        ReplicaStore.from_shelf(vault), IntentQueue(intents), candidate, fields
    )
    assert result.outcome is service.Outcome.QUEUED
    return intents, result.intent_id


# --- recording ---


def test_a_remote_add_is_queued_and_leaves_the_shelf_untouched(tmp_path):
    # Given a Shelf without Mistborn, and its replica
    create_book_note(BookCandidate(title="Dune", authors=["Frank Herbert"]), tmp_path)
    before = _notes(tmp_path)

    # When Mistborn is added where there is no Shelf
    intents = FakeContainer()
    result = service.queue_add(
        ReplicaStore.from_shelf(tmp_path),
        IntentQueue(intents),
        MISTBORN,
        {"status": "Reading"},
    )

    # Then it is queued, on the replica's guarantee, with no Libris ID yet
    assert result.outcome is service.Outcome.QUEUED
    assert result.guarantee is service.Guarantee.REPLICA
    assert result.libris_id is None
    # And the Intent holds the candidate and only the fields it named
    document = intents.items[result.intent_id]
    assert document["kind"] == "add"
    assert document["pending"] is True
    assert document["candidate"]["google_books_id"] == "mist-1"
    assert document["fields"] == {"status": "Reading"}
    # And the Shelf has not changed
    assert _notes(tmp_path) == before


def test_a_remote_add_of_a_book_the_replica_holds_records_nothing(tmp_path):
    # Given a replica that already holds Mistborn
    create_book_note(MISTBORN, tmp_path)
    held = BookNote.read(next(tmp_path.glob("*.md")))

    # When it is added remotely
    intents = FakeContainer()
    result = service.queue_add(
        ReplicaStore.from_shelf(tmp_path), IntentQueue(intents), MISTBORN
    )

    # Then the answer names the Book held, on the replica's guarantee
    assert result.outcome is service.Outcome.ALREADY_PRESENT
    assert result.guarantee is service.Guarantee.REPLICA
    assert result.libris_id == held.libris_id
    assert intents.items == {}


def test_a_remote_add_stops_on_a_near_match_and_records_nothing(tmp_path):
    # Given a replica holding a book whose title contains this one's
    create_book_note(
        BookCandidate(
            title="Mistborn: The Final Empire", authors=["Brandon Sanderson"]
        ),
        tmp_path,
    )
    bare = BookCandidate(title="Mistborn", authors=["Brandon Sanderson"])

    # When it is added remotely by a Surface that asked to stop
    intents = FakeContainer()
    result = service.queue_add(
        ReplicaStore.from_shelf(tmp_path),
        IntentQueue(intents),
        bare,
        stop_on_near_match=True,
    )

    # Then it stops with the near match, says which guarantee, and queues nothing
    assert result.outcome is service.Outcome.NEEDS_CONFIRMATION
    assert result.guarantee is service.Guarantee.REPLICA
    assert [n.title for n in result.near_matches] == ["Mistborn: The Final Empire"]
    assert intents.items == {}


def test_a_remote_add_carrying_an_illegal_value_is_refused_not_queued(tmp_path):
    # Given an empty Shelf
    intents = FakeContainer()

    # When an add arrives with a status the Library does not define
    with pytest.raises(ValueError, match="status"):
        service.queue_add(
            ReplicaStore.from_shelf(tmp_path),
            IntentQueue(intents),
            MISTBORN,
            {"status": "Finished"},
        )

    # Then nothing is queued: it is refused while someone can still correct it
    assert intents.items == {}


# --- applying ---


def test_sync_writes_the_note_and_marks_the_intent_applied(tmp_path):
    # Given an add-Intent for a Book the Shelf does not hold
    intents, intent_id = _queued(tmp_path, fields={"status": "Reading"})

    # When sync applies the pending Intents
    report = apply_pending(tmp_path, intents)

    # Then the note is written, with the field the Intent named
    (path,) = tmp_path.glob("*.md")
    note = BookNote.read(path)
    assert note.title == "Mistborn"
    assert note.frontmatter["status"] == "Reading"
    # And the Intent is applied, and names the Libris ID it made
    settled = intents.items[intent_id]
    assert settled["pending"] is False
    assert settled["outcome"] == "applied"
    assert settled["libris_id"] == note.libris_id
    assert [s.libris_id for s in report.applied] == [note.libris_id]


def test_the_note_is_dated_the_day_the_add_was_asked_for(tmp_path):
    # Given an add-Intent recorded on an earlier day
    intents, intent_id = _queued(tmp_path)
    asked = datetime(2026, 3, 14, 12, 0, tzinfo=UTC)
    intents.items[intent_id]["recorded_at"] = asked.isoformat()

    # When sync applies it some days later
    apply_pending(tmp_path, intents)

    # Then the Book joined the Library on the day it was asked for
    (path,) = tmp_path.glob("*.md")
    assert BookNote.read(path).frontmatter["date_added"] == (
        asked.astimezone().date().isoformat()
    )


@pytest.mark.parametrize("held", [False, True], ids=["new note", "absorbed"])
def test_a_book_added_as_read_is_dated_the_day_it_was_asked_either_way(tmp_path, held):
    # Given an add-Intent for a Book added as Read with no finish date, asked
    # on an earlier day - and, for the absorbed case, Mistborn already on the
    # Shelf with no finish date by the time sync runs
    intents, intent_id = _queued(tmp_path, fields={"status": "Read"})
    asked = datetime(2026, 3, 14, 12, 0, tzinfo=UTC)
    intents.items[intent_id]["recorded_at"] = asked.isoformat()
    if held:
        create_book_note(MISTBORN, tmp_path)

    # When sync applies it some days later
    apply_pending(tmp_path, intents)

    # Then the finish date is the day it was asked, whichever path it took.
    # A new note was left with none, and an absorbed one got the day sync ran
    # (#214).
    (path,) = tmp_path.glob("*.md")
    frontmatter = BookNote.read(path).frontmatter
    assert frontmatter["status"] == "Read"
    assert frontmatter["date_finished"] == asked.astimezone().date().isoformat()
    assert intents.items[intent_id]["outcome"] == ("absorbed" if held else "applied")


def test_an_absorbed_read_add_keeps_the_finish_date_the_note_has(tmp_path):
    # Given an add-Intent for Mistborn as Read, and Mistborn already on the
    # Shelf, finished on a day of its own
    intents, intent_id = _queued(tmp_path, fields={"status": "Read"})
    create_book_note(
        MISTBORN, tmp_path, overrides={"status": "Read", "date_finished": "2019-07-04"}
    )

    # When sync absorbs it
    apply_pending(tmp_path, intents)

    # Then the date the note had is kept: a stamp only fills an empty field
    (path,) = tmp_path.glob("*.md")
    assert BookNote.read(path).frontmatter["date_finished"] == "2019-07-04"


@pytest.mark.parametrize("held", [False, True], ids=["new note", "absorbed"])
def test_a_finish_date_given_with_a_remote_add_reaches_the_note(tmp_path, held):
    # Given an add-Intent that names the day the Book was finished
    intents, intent_id = _queued(
        tmp_path, fields={"status": "Read", "date_finished": "2026-11-13"}
    )
    assert intents.items[intent_id]["fields"]["date_finished"] == "2026-11-13"
    if held:
        create_book_note(MISTBORN, tmp_path)

    # When sync applies it
    apply_pending(tmp_path, intents)

    # Then the note carries that day, not the day it was asked or applied
    (path,) = tmp_path.glob("*.md")
    assert BookNote.read(path).frontmatter["date_finished"] == "2026-11-13"


def test_a_remote_add_as_read_says_it_will_be_dated(tmp_path):
    # Given an empty Shelf
    intents = FakeContainer()

    # When a Book is added remotely as Read, with no finish date
    result = service.queue_add(
        ReplicaStore.from_shelf(tmp_path),
        IntentQueue(intents),
        MISTBORN,
        {"status": "Read"},
    )

    # Then the answer discloses the date sync will set (ADR 0024)
    assert list(result.derived) == ["date_finished"]
    # And the Intent records only what was asked: a date written into it would
    # overwrite the one a held note already has
    assert intents.items[result.intent_id]["fields"] == {"status": "Read"}


@pytest.mark.parametrize("given", ["last week", "13/11/2026", "2026-02-30"])
def test_a_remote_add_with_a_date_that_is_not_a_day_is_refused(tmp_path, given):
    # Given an empty Shelf
    intents = FakeContainer()

    # When an add arrives with a finish date that is not YYYY-MM-DD
    with pytest.raises(ValueError, match="date_finished"):
        service.queue_add(
            ReplicaStore.from_shelf(tmp_path),
            IntentQueue(intents),
            MISTBORN,
            {"status": "Read", "date_finished": given},
        )

    # Then nothing is queued, so it is never written into a note at sync
    assert intents.items == {}


def test_an_intent_for_a_book_already_held_is_absorbed_into_it(tmp_path):
    # Given an add-Intent queued before Mistborn reached the Shelf another way
    intents, intent_id = _queued(tmp_path, fields={"priority": "High"})
    create_book_note(MISTBORN, tmp_path)
    (path,) = tmp_path.glob("*.md")
    held = BookNote.read(path)

    # When sync applies it
    report = apply_pending(tmp_path, intents)

    # Then no second note is written
    assert list(tmp_path.glob("*.md")) == [path]
    # And the Intent is absorbed, carrying the Libris ID it turned out to mean
    settled = intents.items[intent_id]
    assert settled["outcome"] == "absorbed"
    assert settled["libris_id"] == held.libris_id
    assert [s.libris_id for s in report.absorbed] == [held.libris_id]
    # And the field it named is set on that note
    assert BookNote.read(path).frontmatter["priority"] == "High"


def test_an_absorbed_intent_waits_rather_than_edit_a_file_replaced_since_the_match(
    tmp_path, monkeypatch
):
    # Given an add-Intent for Mistborn, which the Shelf already holds
    intents, intent_id = _queued(tmp_path, fields={"priority": "High"})
    create_book_note(MISTBORN, tmp_path)
    (path,) = tmp_path.glob("*.md")

    # And a writer that replaces that file with another Book's note just after
    # the duplicate check matched it
    add_book = service.add_book

    def _matched_then_replaced(*args, **kwargs):
        result = add_book(*args, **kwargs)
        path.write_text(
            "---\ntitle: Elantris\nlibris_id: 01ELANTRIS\n---\n", encoding="utf-8"
        )
        return result

    monkeypatch.setattr(service, "add_book", _matched_then_replaced)

    # When sync applies the Intent
    report = apply_pending(tmp_path, intents)

    # Then the Book now in that file is not given Mistborn's fields
    assert "priority" not in BookNote.read(path).frontmatter
    # And the Intent waits for the next sync rather than settling
    assert intents.items[intent_id]["pending"] is True
    assert [s.intent_id for s in report.waiting] == [intent_id]


def test_no_intent_is_applied_while_two_notes_share_a_libris_id(tmp_path):
    # Given an add-Intent for Mistborn, which the Shelf already holds - and
    # another Book's note claiming Mistborn's Libris ID, sorting ahead of it
    intents, intent_id = _queued(tmp_path, fields={"priority": "High"})
    create_book_note(MISTBORN, tmp_path)
    (mistborn,) = tmp_path.glob("*.md")
    shared = BookNote.read(mistborn).libris_id
    elantris = tmp_path / "A Elantris.md"
    elantris.write_text(
        f"---\ntitle: Elantris\nlibris_id: {shared}\n---\n", encoding="utf-8"
    )
    before = {p.name: p.read_text(encoding="utf-8") for p in tmp_path.glob("*.md")}

    # When sync applies the pending Intents
    report = apply_pending(tmp_path, intents)

    # Then neither note changes, and the Intent waits, saying why
    after = {p.name: p.read_text(encoding="utf-8") for p in tmp_path.glob("*.md")}
    assert after == before
    assert intents.items[intent_id]["pending"] is True
    assert [s.intent_id for s in report.waiting] == [intent_id]
    assert shared in report.waiting[0].reason


def test_running_sync_again_does_not_apply_an_intent_twice(tmp_path):
    # Given an add-Intent sync has already applied
    intents, _ = _queued(tmp_path)
    apply_pending(tmp_path, intents)
    intents.writes.clear()

    # When sync runs again
    report = apply_pending(tmp_path, intents)

    # Then nothing is written to the Shelf or the container
    assert len(_notes(tmp_path)) == 1
    assert intents.writes == []
    assert report.applied == report.absorbed == []


def test_an_intent_whose_settling_was_lost_is_absorbed_not_written_twice(
    tmp_path, monkeypatch
):
    # Given a sync that wrote the note and then failed to settle the Intent
    intents, intent_id = _queued(tmp_path)
    upsert = intents.upsert_item

    def _lost(body):
        raise OSError("connection reset")

    monkeypatch.setattr(intents, "upsert_item", _lost)
    with pytest.raises(OSError):
        apply_pending(tmp_path, intents)
    monkeypatch.setattr(intents, "upsert_item", upsert)
    assert intents.items[intent_id]["pending"] is True
    shelf.forget_indexes()

    # When the next sync runs
    apply_pending(tmp_path, intents)

    # Then the note it wrote absorbs the Intent: there is still one note
    assert len(_notes(tmp_path)) == 1
    assert intents.items[intent_id]["outcome"] == "absorbed"


def test_of_two_intents_for_one_book_the_first_applies_and_the_second_is_absorbed(
    tmp_path,
):
    # Given two add-Intents for Mistborn, recorded a minute apart, both checked
    # against a replica that held neither
    intents = FakeContainer()
    queue = IntentQueue(intents)
    replica = ReplicaStore.from_shelf(tmp_path)
    second = service.queue_add(replica, queue, MISTBORN).intent_id
    first = service.queue_add(replica, queue, MISTBORN).intent_id
    intents.items[first]["recorded_at"] = "2026-11-23T10:00:00+00:00"
    intents.items[second]["recorded_at"] = "2026-11-23T10:01:00+00:00"

    # When sync applies them
    apply_pending(tmp_path, intents)

    # Then one note is written, the earlier is applied and the later absorbed
    assert len(_notes(tmp_path)) == 1
    assert intents.items[first]["outcome"] == "applied"
    assert intents.items[second]["outcome"] == "absorbed"
    assert intents.items[first]["libris_id"] == intents.items[second]["libris_id"]


def test_an_intent_carrying_a_value_the_library_no_longer_accepts_is_rejected(
    tmp_path,
):
    # Given an add-Intent whose status the Library does not define, as one
    # recorded before a vocabulary changed would be
    intents, intent_id = _queued(tmp_path)
    intents.items[intent_id]["fields"] = {"status": "Finished"}

    # When sync applies it
    report = apply_pending(tmp_path, intents)

    # Then it is rejected, with the reason, and nothing is written
    settled = intents.items[intent_id]
    assert settled["outcome"] == "rejected"
    assert "Finished" in settled["reason"]
    assert _notes(tmp_path) == []
    assert [s.title for s in report.rejected] == ["Mistborn"]


def test_an_intent_that_cannot_be_read_is_rejected_rather_than_retried_forever(
    tmp_path,
):
    # Given an add-Intent whose candidate this code cannot make sense of
    intents, intent_id = _queued(tmp_path)
    intents.items[intent_id]["candidate"]["shelf_colour"] = "teal"

    # When sync applies it
    apply_pending(tmp_path, intents)

    # Then it is rejected and nothing is written
    assert intents.items[intent_id]["outcome"] == "rejected"
    assert _notes(tmp_path) == []


def test_an_intent_the_shelf_cannot_take_yet_stays_pending(tmp_path, lock_note):
    # Given an add-Intent, and a note on the Shelf that cannot be read - it
    # may be this very Book
    create_book_note(BookCandidate(title="Dune", authors=["Frank Herbert"]), tmp_path)
    intents, intent_id = _queued(tmp_path)
    (dune,) = tmp_path.glob("*.md")
    lock_note(dune, reads=True)
    shelf.forget_indexes()

    # When sync applies it
    report = apply_pending(tmp_path, intents)

    # Then it is neither written nor settled, and the next sync tries again
    assert intents.items[intent_id]["pending"] is True
    assert "outcome" not in intents.items[intent_id]
    assert [s.intent_id for s in report.waiting] == [intent_id]
    assert len(_notes(tmp_path)) == 1


# --- end to end, without a network ---


def test_a_book_added_remotely_is_found_remotely_after_one_sync(tmp_path):
    # Given a Shelf pushed to the replica, and an add queued against it
    create_book_note(BookCandidate(title="Dune", authors=["Frank Herbert"]), tmp_path)
    books, counts, intents = FakeContainer(), FakeContainer(), FakeContainer()
    push_shelf(tmp_path, books, counts)
    replica = CosmosStore(books, counts)
    result = service.queue_add(replica, IntentQueue(intents), MISTBORN)
    assert service.search_library(replica, query="mistborn").total == 0

    # When sync applies the Intents and then pushes, as `libris sync` does
    apply_pending(tmp_path, intents)
    push_shelf(tmp_path, books, counts)

    # Then the replica holds the note, under the Libris ID the Intent records
    found = service.search_library(replica, query="mistborn")
    assert found.total == 1
    assert found.books[0].libris_id == intents.items[result.intent_id]["libris_id"]


# --- what a malformed Intent or an unfinished write cannot do (#211 review) ---


_MISTBORN = {"title": "Mistborn", "authors": ["Brandon Sanderson"]}


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("recorded_at", None),
        ("recorded_at", 1732400000),
        ("candidate", ["Mistborn"]),
        ("candidate", "Mistborn"),
        ("candidate", {"title": 7, "authors": ["Brandon Sanderson"]}),
        ("candidate", {"title": "Mistborn", "authors": "Brandon Sanderson"}),
        ("candidate", {**_MISTBORN, "google_books_id": {"id": "mist-1"}}),
        ("candidate", {**_MISTBORN, "description": 42}),
        ("candidate", {**_MISTBORN, "page_count": "541"}),
        ("candidate", {**_MISTBORN, "genres": ["Fantasy", None]}),
        ("fields", ["status", "Read"]),
    ],
    ids=[
        "time null",
        "time a number",
        "candidate a list",
        "candidate a string",
        "title not text",
        "authors not a list",
        "volume id a mapping",
        "description a number",
        "page count text",
        "a genre null",
        "fields a list",
    ],
)
def test_a_malformed_intent_is_rejected_without_holding_up_the_others(
    tmp_path, key, value
):
    # Given a well-formed add-Intent, and a malformed one beside it
    intents, good = _queued(tmp_path)
    elantris = BookCandidate(title="Elantris", authors=["Brandon Sanderson"])
    bad = IntentQueue(intents).record_add(elantris, {})
    intents.items[bad][key] = value

    # When sync applies them
    apply_pending(tmp_path, intents)

    # Then the malformed one is rejected and the other still applies
    assert intents.items[bad]["outcome"] == "rejected"
    assert intents.items[good]["outcome"] == "applied"
    assert _notes(tmp_path) == ["Mistborn - Brandon Sanderson.md"]


def test_a_note_written_but_not_read_back_leaves_its_intent_waiting(
    tmp_path, monkeypatch
):
    # Given an add-Intent, and a note that is locked the moment it is written,
    # so its Libris ID cannot be read back
    intents, intent_id = _queued(tmp_path)
    read = BookNote.read

    def _locked(path):
        raise PermissionError(13, "Permission denied", str(path))

    monkeypatch.setattr("libris.service.BookNote.read", _locked)

    # When sync applies it
    report = apply_pending(tmp_path, intents)

    # Then it is not settled without the Libris ID it made
    assert intents.items[intent_id]["pending"] is True
    assert [s.intent_id for s in report.waiting] == [intent_id]

    # And once the note can be read, the next sync absorbs it into that note
    monkeypatch.setattr("libris.service.BookNote.read", read)
    shelf.forget_indexes()
    apply_pending(tmp_path, intents)
    (path,) = tmp_path.glob("*.md")
    assert intents.items[intent_id]["outcome"] == "absorbed"
    assert intents.items[intent_id]["libris_id"] == BookNote.read(path).libris_id


def test_an_empty_field_is_not_recorded_so_absorbing_cannot_reject_it(tmp_path):
    # Given an add queued with an empty format beside a status
    intents, intent_id = _queued(tmp_path, fields={"format": [], "status": "Reading"})
    assert intents.items[intent_id]["fields"] == {"status": "Reading"}

    # When the Book reaches the Shelf before sync, which then absorbs the add
    create_book_note(MISTBORN, tmp_path)
    apply_pending(tmp_path, intents)

    # Then the status it did name is set, and nothing is rejected
    assert intents.items[intent_id]["outcome"] == "absorbed"
    (path,) = tmp_path.glob("*.md")
    assert BookNote.read(path).frontmatter["status"] == "Reading"


def test_a_remote_add_naming_a_field_absorbing_could_not_set_is_refused(tmp_path):
    # Given an empty Shelf
    intents = FakeContainer()

    # When an add arrives setting a field that is not the reader's
    with pytest.raises(ValueError, match="series"):
        service.queue_add(
            ReplicaStore.from_shelf(tmp_path),
            IntentQueue(intents),
            MISTBORN,
            {"series": "Mistborn"},
        )

    # Then nothing is queued
    assert intents.items == {}
