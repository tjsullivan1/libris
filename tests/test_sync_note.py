"""The note sync leaves beside the Shelf, listing what did not apply (#178, ADR 0013).

Sync runs unattended, so a rejected Intent reaches a person through this note.
It lists rejections until they are ticked, and waiting Intents until they
apply. These drive `update_note` after `apply_pending`, as sync does; the sync
command's part is in `test_cli.py`.
"""

import pytest
from cosmos_fake import FakeContainer

from libris import shelf
from libris.api import BookCandidate
from libris.intents import IntentQueue, apply_pending
from libris.markdown import BookNote, create_book_note, list_books
from libris.sync_note import _MARKER, NOTE_NAME, NoteRefused, update_note


@pytest.fixture(autouse=True)
def fresh_indexes():
    shelf.forget_indexes()
    yield
    shelf.forget_indexes()


@pytest.fixture
def vault(tmp_path):
    """A Vault holding a Shelf, so the note has a folder of its own to land in."""
    books = tmp_path / "Books"
    books.mkdir()
    return books


def _sync(books, intents):
    """Apply what is queued and rewrite the note, as one sync run does."""
    shelf.forget_indexes()
    return update_note(books, intents, apply_pending(books, intents))


def _note(books):
    return (books.parent / NOTE_NAME).read_text(encoding="utf-8")


def _rejected_update(intents, libris_id="01X", title="Emma"):
    return IntentQueue(intents).record_update(libris_id, {"status": "Read"}, title)


def _tick(books, intent_id):
    path = books.parent / NOTE_NAME
    lines = path.read_text(encoding="utf-8").splitlines()
    ticked = [
        line.replace("- [ ]", "- [x]", 1) if f"intent:{intent_id}" in line else line
        for line in lines
    ]
    assert ticked != lines, "no line names that Intent"
    path.write_text("\n".join(ticked) + "\n", encoding="utf-8")


def _rejected_lines(books):
    return [line for line in _note(books).splitlines() if line.startswith("- [")]


def test_a_rejected_intent_is_listed_with_its_surface_request_and_reason(vault):
    # Given an update queued remotely for a Libris ID the Shelf does not hold
    intents = FakeContainer()
    intent_id = _rejected_update(intents)

    # When sync runs
    _sync(vault, intents)

    # Then the note lists it as a task: the Book, the Surface, what it asked
    # for and why it was rejected
    (line,) = _rejected_lines(vault)
    assert line.startswith("- [ ] **Emma**:")
    assert "set status to Read on the note with Libris ID 01X" in line
    assert "from the hosted MCP server on " in line
    assert "Rejected because no note on the Shelf holds the Libris ID 01X." in line
    assert line.endswith(f"%%intent:{intent_id}%%")


def test_the_note_sits_beside_the_shelf_where_nothing_takes_it_for_a_book(vault):
    # Given a Shelf holding one Book, and a rejected Intent
    create_book_note(BookCandidate(title="Dune", authors=["Frank Herbert"]), vault)
    intents = FakeContainer()
    _rejected_update(intents)
    before = list_books(vault)

    # When sync runs
    report = _sync(vault, intents)

    # Then the note is in the folder holding the Shelf, not on it
    assert report.path == vault.parent / NOTE_NAME
    assert report.path.exists()
    # And the Shelf still lists only its Book Note
    assert list_books(vault) == before
    assert len(before) == 1


def test_a_rejection_stays_listed_until_it_is_ticked_then_drops(vault):
    # Given two rejected Intents, listed after one sync
    intents = FakeContainer()
    emma = _rejected_update(intents, "01E", "Emma")
    persuasion = _rejected_update(intents, "01P", "Persuasion")
    _sync(vault, intents)

    # When a later sync runs without a tick, both stay
    _sync(vault, intents)
    assert len(_rejected_lines(vault)) == 2

    # And when one is ticked, the next sync acknowledges it on the remote
    _tick(vault, emma)
    report = _sync(vault, intents)
    assert report.acknowledged == [emma]
    assert intents.items[emma]["acknowledged_at"]
    assert "acknowledged_at" not in intents.items[persuasion]

    # And lists only the other one, then and on every run after
    for _ in range(2):
        (line,) = _rejected_lines(vault)
        assert "**Persuasion**" in line
        _sync(vault, intents)


def test_a_run_with_nothing_outstanding_says_there_is_nothing_to_act_on(vault):
    # Given a sync with nothing queued
    # When it runs
    _sync(vault, FakeContainer())

    # Then the note says there is nothing to act on, and lists nothing
    assert "Nothing to act on" in _note(vault)
    assert _rejected_lines(vault) == []


def test_once_the_last_rejection_is_ticked_the_note_says_nothing_to_act_on(vault):
    # Given a rejection, listed and then ticked
    intents = FakeContainer()
    intent_id = _rejected_update(intents)
    _sync(vault, intents)
    _tick(vault, intent_id)

    # When sync runs
    _sync(vault, intents)

    # Then the note has nothing left on it, and does not claim the change it
    # never made was applied
    assert "Nothing to act on" in _note(vault)
    assert "applied" not in _note(vault)


def test_a_waiting_intent_is_listed_untickable_until_the_shelf_lets_it_apply(
    vault, lock_note
):
    # Given an update queued for a Book whose note is locked
    create_book_note(BookCandidate(title="Dune", authors=["Frank Herbert"]), vault)
    held = BookNote.read(next(vault.glob("*.md")))
    intents = FakeContainer()
    IntentQueue(intents).record_update(held.libris_id, {"status": "Read"}, "Dune")
    lock_note(held.path, reads=True)

    # When sync runs
    _sync(vault, intents)

    # Then the note lists it as waiting, with the Shelf's reason, and no box
    note = _note(vault)
    assert "## Waiting" in note
    assert "- **Dune** (update): " in note
    assert "None: nothing needs deciding." in note
    assert _rejected_lines(vault) == []

    # And once the note is unlocked, the next sync applies it and drops it
    lock_note(held.path)
    _sync(vault, intents)
    assert "Nothing to act on" in _note(vault)


def test_a_tick_on_anything_but_a_rejection_acknowledges_nothing(vault):
    # Given an Intent applied cleanly, and a note claiming it ticked
    create_book_note(BookCandidate(title="Dune", authors=["Frank Herbert"]), vault)
    held = BookNote.read(next(vault.glob("*.md")))
    intents = FakeContainer()
    applied = IntentQueue(intents).record_update(
        held.libris_id, {"status": "Read"}, "Dune"
    )
    (vault.parent / NOTE_NAME).write_text(
        f"{_MARKER}\n- [x] **Dune** %%intent:{applied}%%\n", encoding="utf-8"
    )

    # When sync runs
    report = _sync(vault, intents)

    # Then nothing is acknowledged
    assert report.acknowledged == []
    assert "acknowledged_at" not in intents.items[applied]


@pytest.mark.parametrize("mark", ["/", "-", " "])
def test_only_a_tick_acknowledges_a_rejection(vault, mark):
    # Given a listed rejection marked with something other than a tick, as some
    # Obsidian themes offer for "in progress" or "cancelled"
    intents = FakeContainer()
    intent_id = _rejected_update(intents)
    _sync(vault, intents)
    path = vault.parent / NOTE_NAME
    path.write_text(
        path.read_text(encoding="utf-8").replace("- [ ]", f"- [{mark}]"),
        encoding="utf-8",
    )

    # When sync runs
    report = _sync(vault, intents)

    # Then it is still outstanding
    assert report.acknowledged == []
    assert "acknowledged_at" not in intents.items[intent_id]
    assert len(_rejected_lines(vault)) == 1


def test_an_unchanged_note_is_not_written_again(vault):
    # Given a note written by one sync
    intents = FakeContainer()
    _rejected_update(intents)
    assert _sync(vault, intents).written

    # When the next sync has nothing new to say
    report = _sync(vault, intents)

    # Then it leaves the file alone, for Obsidian Sync not to carry it again
    assert not report.written


def _settled_rejection(**overrides):
    """A rejected update as the remote holds it once sync has settled it."""
    return {
        "id": "0a1b2c",
        "kind": "update",
        "pending": False,
        "outcome": "rejected",
        "surface": "hosted_mcp",
        "recorded_at": "2026-11-20T15:00:00+00:00",
        "target": "01X",
        "title": "Emma",
        "fields": {"status": "Read"},
        "reason": "no note on the Shelf holds the Libris ID 01X",
        **overrides,
    }


@pytest.mark.parametrize(
    "overrides",
    [
        {"title": "Pride\nand Prejudice"},
        {"target": "01X\n"},
        {"surface": "a\nnewer server"},
        {"fields": {"status\nnote": "Read"}},
        {"fields": {"status": ["Read", "\n"]}},
        {"reason": "it cannot be read:\nfields are str"},
        {"kind": "rename\nbook"},
    ],
    ids=["title", "target", "surface", "field name", "field value", "reason", "kind"],
)
def test_a_line_break_anywhere_in_an_intent_keeps_its_task_on_one_line(
    vault, overrides
):
    # Given a rejected Intent holding a line break in one of its texts
    intents = FakeContainer()
    intents.upsert_item(_settled_rejection(**overrides))

    # When sync lists it
    _sync(vault, intents)

    # Then the box and the Intent's marker share one line, so ticking it
    # acknowledges it
    (line,) = _rejected_lines(vault)
    assert line.startswith("- [ ] ") and line.endswith("%%intent:0a1b2c%%")
    _tick(vault, "0a1b2c")
    assert _sync(vault, intents).acknowledged == ["0a1b2c"]


@pytest.mark.parametrize(
    "held",
    [
        "# Libris sync\n\nMy own notes on syncing.\n- [x] done %%intent:{id}%%\n",
        b"- [x] \xff\xfe not text %%intent:{id}%%",
    ],
    ids=["a note of the person's own", "bytes that are not UTF-8"],
)
def test_a_file_libris_did_not_write_is_left_alone_and_its_ticks_ignored(vault, held):
    # Given a rejection, and a file with the note's name that libris did not
    # write, or that cannot be read to tell
    intents = FakeContainer()
    intent_id = _rejected_update(intents)
    apply_pending(vault, intents)
    path = vault.parent / NOTE_NAME
    if isinstance(held, str):
        path.write_text(held.format(id=intent_id), encoding="utf-8")
    else:
        path.write_bytes(held.replace(b"{id}", intent_id.encode()))
    before = path.read_bytes()

    # When sync tries to update the note
    with pytest.raises(NoteRefused, match="not written by libris"):
        _sync(vault, intents)

    # Then the file is as it was, and nothing is acknowledged from it
    assert path.read_bytes() == before
    assert "acknowledged_at" not in intents.items[intent_id]


def test_a_shelf_at_its_vaults_root_gets_no_note_outside_the_vault(tmp_path):
    # Given a Shelf that is its Vault's root, and a rejection. The Vault is a
    # folder of this test's, so "beside it" is this test's directory too.
    root = tmp_path / "Vault"
    (root / ".obsidian").mkdir(parents=True)
    intents = FakeContainer()
    _rejected_update(intents)

    # When sync tries to update the note
    with pytest.raises(NoteRefused, match="root of its Vault"):
        _sync(root, intents)

    # Then nothing is written beside the Shelf, outside the Vault, or on it
    assert not (tmp_path / NOTE_NAME).exists()
    assert not (root / NOTE_NAME).exists()
