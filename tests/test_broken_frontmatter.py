"""A note whose frontmatter will not parse is not proof a Book is absent (#212).

An edit can break a Book Note's YAML. The note is still on the Shelf and still
someone's Book, but no lookup can say what it holds. Every write that acts on
"no note holds this" - an add, a queued add, an update by identity, an import -
waits for it to be fixed rather than writing a second note or calling the Book
missing, as each already does for a note that could not be read (#168 review).
"""

import json
from pathlib import Path

import pytest
from cosmos_fake import FakeContainer
from typer.testing import CliRunner

from libris import service, shelf
from libris.api import BookCandidate
from libris.cli import app
from libris.intents import IntentQueue, apply_pending
from libris.markdown import BookNote, create_book_note
from libris.store import ReplicaStore

runner = CliRunner()

DUNE = BookCandidate(
    title="Dune",
    authors=["Frank Herbert"],
    isbn="9780441013593",
    google_books_id="dune-1",
)


@pytest.fixture(autouse=True)
def fresh_indexes():
    """No index survives into another test's temporary Shelf."""
    shelf.forget_indexes()
    yield
    shelf.forget_indexes()


def _break(path):
    """Break a note's frontmatter as a stray edit would, keeping its text."""
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace("title:", "title: [unclosed", 1), encoding="utf-8")
    assert BookNote.read(path) is None


def _notes(vault):
    return sorted(p.name for p in vault.glob("*.md"))


def test_a_queued_add_waits_while_the_book_it_duplicates_will_not_parse(tmp_path):
    # Given Dune on the Shelf, an add-Intent for Dune recorded while the
    # replica could not see it, and then Dune's frontmatter broken by an edit
    intents = FakeContainer()
    queued = service.queue_add(
        ReplicaStore.from_shelf(tmp_path), IntentQueue(intents), DUNE
    )
    held = create_book_note(DUNE, tmp_path)
    _break(held)

    # When sync applies the pending Intents
    report = apply_pending(tmp_path, intents)

    # Then no second Dune is written, the Intent stays pending, and what it
    # waits on names the broken note
    assert _notes(tmp_path) == [held.name]
    assert intents.items[queued.intent_id]["pending"] is True
    assert "outcome" not in intents.items[queued.intent_id]
    (waiting,) = report.waiting
    assert waiting.intent_id == queued.intent_id
    assert held.name in waiting.reason
    assert "will not parse" in waiting.reason


def test_a_queued_add_is_absorbed_once_the_broken_note_is_fixed(tmp_path):
    # Given an add-Intent for Dune left waiting on Dune's broken note
    intents = FakeContainer()
    queued = service.queue_add(
        ReplicaStore.from_shelf(tmp_path), IntentQueue(intents), DUNE
    )
    held = create_book_note(DUNE, tmp_path)
    good = held.read_text(encoding="utf-8")
    _break(held)
    apply_pending(tmp_path, intents)

    # When the note is fixed and sync runs again
    held.write_text(good, encoding="utf-8")
    report = apply_pending(tmp_path, intents)

    # Then the Intent is absorbed into the note it meant, still with no second
    assert [s.intent_id for s in report.absorbed] == [queued.intent_id]
    assert intents.items[queued.intent_id]["outcome"] == "absorbed"
    assert _notes(tmp_path) == [held.name]


def test_a_local_add_writes_nothing_while_a_note_will_not_parse(tmp_path):
    # Given Dune on the Shelf, its frontmatter broken
    held = create_book_note(DUNE, tmp_path)
    _break(held)

    # When Dune is added again
    # Then the add is refused, naming the broken file, and nothing is written
    with pytest.raises(service.ShelfUnreadable, match=held.name) as raised:
        service.add_book(tmp_path, DUNE)
    assert raised.value.unparseable == [held]
    assert raised.value.paths == []
    assert "will not parse" in str(raised.value)
    assert "could not be read" not in str(raised.value)
    assert _notes(tmp_path) == [held.name]


def test_a_book_provably_held_is_still_reported_held_beside_a_broken_note(tmp_path):
    # Given Dune on the Shelf, and some other file whose frontmatter is broken
    dune = create_book_note(DUNE, tmp_path)
    _break(
        create_book_note(BookCandidate(title="Emma", authors=["Jane Austen"]), tmp_path)
    )

    # When Dune is added again
    result = service.add_book(tmp_path, DUNE)

    # Then the exact match answers: the broken file cannot make Dune less held
    assert result.outcome is service.Outcome.ALREADY_PRESENT
    assert result.path == dune


def test_a_match_whose_note_breaks_after_the_lookup_is_not_reported_held(
    tmp_path, monkeypatch
):
    # Given Dune on the Shelf, whose frontmatter breaks just after the exact
    # check finds it - an edit landing mid-add
    held = create_book_note(DUNE, tmp_path)
    real_find_existing = service.find_existing

    def _breaks_after(store, **kwargs):
        found = real_find_existing(store, **kwargs)
        _break(held)
        return found

    monkeypatch.setattr(service, "find_existing", _breaks_after)

    # When Dune is added again
    # Then that parse is not taken as proof: the note no longer says anything
    # that can be read, so the add is refused rather than answered "held"
    with pytest.raises(service.ShelfUnreadable, match=held.name):
        service.add_book(tmp_path, DUNE)
    assert _notes(tmp_path) == [held.name]


def test_the_shelf_is_judged_complete_on_everything_its_check_saw(
    tmp_path, monkeypatch
):
    # Given a Shelf where each scan finds another note locked and another
    # broken - edits and a sync client landing while the check runs
    from libris import store as store_module

    handed_out: list[Path] = []

    class _Busy:
        scans = 0

        def notes(self):
            _Busy.scans += 1
            return []

        @property
        def unreadable(self):
            path = tmp_path / f"locked-{_Busy.scans}.md"
            handed_out.append(path)
            return [path]

        @property
        def unparseable(self):
            path = tmp_path / f"broken-{_Busy.scans}.md"
            handed_out.append(path)
            return [path]

    monkeypatch.setattr(store_module, "index_for", lambda _vault: _Busy())
    shelf_store = store_module.ShelfStore(tmp_path)

    # When the store is asked what it could not answer for
    unanswered = shelf_store.unanswered()

    # Then every failure any scan in that check saw is in the answer: one
    # seen after an earlier list was copied would let a write go ahead that
    # the store itself knew was unchecked (#216 review)
    assert unanswered.paths == set(handed_out)


def test_an_update_by_id_is_not_called_a_miss_while_a_note_will_not_parse(tmp_path):
    # Given Dune on the Shelf, whose frontmatter is then broken
    held = create_book_note(DUNE, tmp_path)
    libris_id = BookNote.read(held).libris_id
    _break(held)

    # When its status is set by that identity
    # Then the update says it cannot tell, naming the file, rather than that
    # no note holds the id - which would reject a queued update for good
    with pytest.raises(service.ShelfUnreadable, match=held.name):
        service.update_book(tmp_path, libris_id, {"status": "Read"})


def test_an_import_creates_nothing_while_a_note_will_not_parse(tmp_path, monkeypatch):
    # Given a Shelf where Dune's frontmatter is broken, and an export naming
    # Dune and a book new to the Shelf
    vault = tmp_path / "vault"
    vault.mkdir()
    held = create_book_note(
        BookCandidate(title="Dune", authors=["Frank Herbert"]), vault
    )
    _break(held)
    export = tmp_path / "audible.json"
    export.write_text(
        json.dumps(
            [
                {"title": "Dune", "author": "Frank Herbert", "finished": "Yes"},
                {"title": "Middlemarch", "author": "George Eliot", "finished": "No"},
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("libris.cli.get_vault_path", lambda: vault)

    # When it is applied
    result = runner.invoke(app, ["import", str(export), "--apply"])

    # Then neither unmatched book is created, and the report names the file
    assert result.exit_code == 0, result.output
    assert "frontmatter that will not parse" in result.output
    assert f"! {held.name}" in result.output
    assert "2 book(s) matched no note and were not added" in result.output
    assert "0 new book(s) added" in result.output
    assert _notes(vault) == [held.name]
