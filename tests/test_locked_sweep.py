"""A note that is there but cannot be opened, met partway through a sweep (#167).

#165 fixed this in the merge paths. Everywhere else that works through the Shelf
note by note caught `FileNotFoundError` - a note that went away - and nothing
else, so a locked, read-only or permission-denied note raised `PermissionError`
past the handler and ended the run partway, leaving every later note undone.

A note whose rewrite failed partway and could not be put back (`NoteWriteFailed`,
#166) is not an `OSError` at all, deliberately, and ended the same runs the same
way. Both are reported here and the run goes on.
"""

import errno
from pathlib import Path

import pytest
from typer.testing import CliRunner

from libris import markdown
from libris.api import BookCandidate
from libris.cli import app
from libris.markdown import (
    create_book_note,
    read_frontmatter,
    rename_book_file,
    update_wikilinks_in_vault,
)

runner = CliRunner()


def _legacy_note(vault: Path, name: str) -> Path:
    """A note the repair pass rewrites, because it lacks the current fields."""
    path = vault / name
    path.write_text(
        f"---\ntitle: {path.stem}\nStatus: Read\n---\n\n## Notes\n",
        encoding="utf-8",
    )
    return path


@pytest.fixture
def shelf(tmp_path, monkeypatch) -> Path:
    """An empty, configured Shelf."""
    vault = tmp_path / "shelf"
    vault.mkdir()
    monkeypatch.setattr("libris.cli.get_vault_path", lambda: vault)
    return vault


def _fail_partway(monkeypatch, *, restore_fails: bool = True, skip: int = 0) -> None:
    """Make the first rewrite fail halfway, and by default fail putting it back.

    Args:
        monkeypatch: The test's monkeypatch fixture.
        restore_fails: Fail putting the old bytes back as well, which raises
            `NoteWriteFailed` (#166). Otherwise the note is put back and the
            write's `OSError` - a full disk, not a lock - is raised.
        skip: Let this many rewrites succeed before the one that fails.
    """
    real = markdown._replace_bytes
    calls = 0
    failing = (skip + 1, skip + 2) if restore_fails else (skip + 1,)

    def _replace(fd, data):
        nonlocal calls
        calls += 1
        if calls in failing:
            real(fd, data[: len(data) // 2])
            raise OSError(errno.ENOSPC, "No space left on device")
        return real(fd, data)

    monkeypatch.setattr(markdown, "_replace_bytes", _replace)


# --- `libris cleanup` ---


@pytest.mark.parametrize("refused", ["reads", "writes"])
def test_cleanup_reports_a_locked_note_and_repairs_the_rest(shelf, lock_note, refused):
    # Given two notes due a repair, the first of them locked
    locked = _legacy_note(shelf, "A Locked.md")
    kept = _legacy_note(shelf, "B Kept.md")
    before = locked.read_bytes()
    lock_note(locked, **{refused: True})

    # When cleanup runs over the Shelf
    result = runner.invoke(app, ["cleanup"])

    # Then the locked note is named and left as it was, and the note after it is
    # still repaired. Caught as gone only, the lock ended the sweep there.
    assert result.exit_code == 0, result.output
    assert "A Locked.md could not be accessed" in result.output
    assert "1 note(s) could not be accessed" in result.output
    assert "tags: Book" in kept.read_text(encoding="utf-8")
    lock_note(locked)
    assert locked.read_bytes() == before


def test_cleanup_reports_a_note_left_damaged_and_repairs_the_rest(shelf, monkeypatch):
    # Given two notes due a repair, and the first write failing partway
    _legacy_note(shelf, "A Damaged.md")
    kept = _legacy_note(shelf, "B Kept.md")
    _fail_partway(monkeypatch)

    # When cleanup runs over the Shelf
    result = runner.invoke(app, ["cleanup"])

    # Then the damaged note is named as possibly damaged - not "skipped", which
    # would say it was left as it was - and the sweep goes on
    assert result.exit_code == 0, result.output
    assert "A Damaged.md failed partway" in result.output
    assert "1 note(s) may be damaged" in result.output
    assert "tags: Book" in kept.read_text(encoding="utf-8")


def test_clean_reports_a_locked_note_rather_than_a_traceback(
    shelf, monkeypatch, lock_note
):
    # Given the note picked for cleaning, read-only
    locked = _legacy_note(shelf, "Locked.md")
    monkeypatch.setattr(
        "questionary.autocomplete",
        lambda *a, **k: type("A", (), {"ask": lambda self: "Locked.md"})(),
    )
    lock_note(locked, writes=True)

    # When it is cleaned
    result = runner.invoke(app, ["clean"])

    # Then the command says so and exits 1, as it does for a note that is gone
    assert result.exit_code == 1
    assert "Locked.md could not be accessed" in result.output
    assert not isinstance(result.exception, PermissionError)


# --- `libris autoenrich` and `libris enrich` ---


def _enrichable(vault: Path, name: str) -> Path:
    path = vault / f"{name}.md"
    path.write_text(
        f"---\ntitle: {name}\nisbn: null\n---\n\n## Notes\n", encoding="utf-8"
    )
    return path


def _one_match_per_title(monkeypatch) -> None:
    monkeypatch.setattr(
        "libris.cli.GoogleBooksClient.search",
        lambda self, query: [
            BookCandidate(title=query.split(" ")[0].strip('"'), authors=["A"])
        ],
    )


@pytest.mark.parametrize("refused", ["reads", "writes"])
def test_autoenrich_reports_a_locked_note_and_enriches_the_rest(
    shelf, monkeypatch, lock_note, refused
):
    # Given two notes due enrichment, the first of them locked
    locked = _enrichable(shelf, "Alpha")
    kept = _enrichable(shelf, "Beta")
    before = locked.read_bytes()
    _one_match_per_title(monkeypatch)
    lock_note(locked, **{refused: True})

    # When autoenrich runs over the Shelf
    result = runner.invoke(app, ["autoenrich"])

    # Then the locked note is named and counted apart from a complete one, and
    # the note after it is still enriched
    assert result.exit_code == 0, result.output
    assert "Alpha.md could not be accessed" in result.output
    assert "Could not be accessed: 1" in result.output
    assert "Skipped (already complete): 0" in result.output
    assert read_frontmatter(kept)["authors"] == ["A"]
    lock_note(locked)
    assert locked.read_bytes() == before


def test_autoenrich_reports_a_note_left_damaged_and_enriches_the_rest(
    shelf, monkeypatch
):
    # Given two notes due enrichment, and the first write failing partway
    _enrichable(shelf, "Alpha")
    kept = _enrichable(shelf, "Beta")
    _one_match_per_title(monkeypatch)
    _fail_partway(monkeypatch)

    # When autoenrich runs over the Shelf
    result = runner.invoke(app, ["autoenrich"])

    # Then the damaged note is named, and the run goes on
    assert result.exit_code == 0, result.output
    assert "Alpha.md failed partway" in result.output
    assert "May be damaged: 1" in result.output
    assert read_frontmatter(kept)["authors"] == ["A"]


def test_enrich_reports_a_locked_note_rather_than_a_traceback(
    shelf, monkeypatch, lock_note
):
    # Given a note that can be read but not written
    locked = _enrichable(shelf, "Dune")
    _one_match_per_title(monkeypatch)
    monkeypatch.setattr(
        "questionary.select",
        lambda *a, choices=None, **k: type("A", (), {"ask": lambda self: choices[0]})(),
    )
    monkeypatch.setattr(
        "questionary.text",
        lambda *a, default="", **k: type("A", (), {"ask": lambda self: default})(),
    )
    lock_note(locked, writes=True)

    # When it is enriched
    result = runner.invoke(app, ["enrich", "Dune.md"])

    # Then the command says so and exits 1
    assert result.exit_code == 1, result.output
    assert "Dune.md could not be accessed" in result.output
    assert not isinstance(result.exception, PermissionError)


# --- `libris doctor` ---


def test_doctor_names_a_note_it_could_not_read(shelf, lock_note):
    # Given a Shelf of two notes, one of them locked
    create_book_note(BookCandidate(title="Dune", authors=["Frank Herbert"]), shelf)
    locked = create_book_note(
        BookCandidate(title="Emma", authors=["Jane Austen"]), shelf
    )
    lock_note(locked, reads=True)

    # When the doctor runs
    result = runner.invoke(app, ["doctor"])

    # Then the note is named, and the Shelf is not called clean. Skipped as a
    # vanished note is, a check that never read it reported nothing wrong with
    # it - and raised past that handler, it ended the check for every note.
    assert result.exit_code == 0, result.output
    assert "1 note(s) could not be read" in result.output
    assert locked.name in result.output
    assert "Nothing on the Shelf needs a decision" not in result.output


# --- `libris migrate` ---


def _confirm_yes(monkeypatch) -> None:
    monkeypatch.setattr(
        "questionary.confirm",
        lambda *a, **k: type("A", (), {"ask": lambda self: True})(),
    )


def test_migrate_plans_around_a_note_it_could_not_read(shelf, monkeypatch, lock_note):
    # Given two notes due a migration, the first of them locked
    locked = _legacy_note(shelf, "A Locked.md")
    kept = _legacy_note(shelf, "B Kept.md")
    before = locked.read_bytes()
    _confirm_yes(monkeypatch)
    lock_note(locked, reads=True)

    # When the migration is applied
    result = runner.invoke(app, ["migrate", "--apply"])

    # Then the locked note is flagged and left alone, and the other migrated.
    # Planning read every note unguarded, so it ended before anything was shown.
    assert result.exit_code == 0, result.output
    assert "A Locked.md: could not be read" in result.output
    assert "Migrated 1 notes." in result.output
    assert "libris_id" in kept.read_text(encoding="utf-8")
    lock_note(locked)
    assert locked.read_bytes() == before


def test_migrate_reports_a_note_it_could_not_write(shelf, monkeypatch, lock_note):
    # Given two notes due a migration, the first read-only
    locked = _legacy_note(shelf, "A Locked.md")
    kept = _legacy_note(shelf, "B Kept.md")
    before = locked.read_bytes()
    _confirm_yes(monkeypatch)
    lock_note(locked, writes=True)

    # When the migration is applied
    result = runner.invoke(app, ["migrate", "--apply"])

    # Then it is named, and the note after it is still written
    assert result.exit_code == 0, result.output
    assert "1 could not be accessed" in result.output
    assert "A Locked.md" in result.output
    assert "Migrated 1 notes." in result.output
    assert "libris_id" in kept.read_text(encoding="utf-8")
    lock_note(locked)
    assert locked.read_bytes() == before


def test_migrate_reports_a_note_left_damaged(shelf, monkeypatch):
    # Given two notes due a migration, and the first write failing partway
    _legacy_note(shelf, "A Damaged.md")
    kept = _legacy_note(shelf, "B Kept.md")
    _confirm_yes(monkeypatch)
    _fail_partway(monkeypatch)

    # When the migration is applied
    result = runner.invoke(app, ["migrate", "--apply"])

    # Then the damaged note is named, and the note after it is still written
    assert result.exit_code == 0, result.output
    assert "1 may be damaged" in result.output
    assert "A Damaged.md" in result.output
    assert "libris_id" in kept.read_text(encoding="utf-8")


# --- `libris repair` ---


def _damaged(vault: Path, name: str) -> Path:
    """A note whose author lost a letter (#78)."""
    note = vault / name
    note.write_text(
        '---\ntitle: "Either-Or"\nauthors:\n  - "S�ren Kierkegaard"\n'
        "google_books_id: vol1\n---\n\n## Notes\n\nMine.\n",
        encoding="utf-8",
    )
    return note


def _repair_answers(monkeypatch) -> None:
    monkeypatch.setattr(
        "libris.cli.GoogleBooksClient.get_volume", lambda self, gid: None
    )
    monkeypatch.setattr(
        "questionary.text",
        lambda *a, **k: type("A", (), {"ask": lambda self: "Søren Kierkegaard"})(),
    )


def test_repair_reports_a_locked_note_and_repairs_the_rest(
    shelf, monkeypatch, lock_note
):
    # Given two damaged notes, the first read-only
    locked = _damaged(shelf, "A Locked.md")
    kept = _damaged(shelf, "B Kept.md")
    before = locked.read_bytes()
    _repair_answers(monkeypatch)
    lock_note(locked, writes=True)

    # When they are repaired
    result = runner.invoke(app, ["repair"])

    # Then the locked note is named and left alone, and the other repaired
    assert result.exit_code == 0, result.output
    assert "A Locked.md could not be accessed" in result.output
    assert "Repaired 1 note(s); left 1 alone." in result.output
    assert "Søren" in kept.read_text(encoding="utf-8")
    lock_note(locked)
    assert locked.read_bytes() == before


def test_repair_reports_a_note_left_damaged_and_repairs_the_rest(shelf, monkeypatch):
    # Given two damaged notes, and the first write failing partway
    _damaged(shelf, "A Damaged.md")
    kept = _damaged(shelf, "B Kept.md")
    _repair_answers(monkeypatch)
    _fail_partway(monkeypatch)

    # When they are repaired
    result = runner.invoke(app, ["repair"])

    # Then the damaged note is named as possibly damaged, and the other repaired
    assert result.exit_code == 0, result.output
    assert "A Damaged.md failed partway" in result.output
    assert "Søren" in kept.read_text(encoding="utf-8")


def test_repair_names_a_damaged_note_it_could_not_read(shelf, lock_note):
    # Given one damaged note, locked against reading
    locked = _damaged(shelf, "Locked.md")
    lock_note(locked, reads=True)

    # When repair looks for damage
    result = runner.invoke(app, ["repair"])

    # Then it names the note it could not check, and does not say the Shelf has
    # no damage - it could not see this note's (#168 review)
    assert result.exit_code == 0, result.output
    assert "1 note(s) could not be read" in result.output
    assert "Locked.md" in result.output
    assert "No note has lost a character" not in result.output
    assert "No note it could read has lost a character" in result.output


def test_repair_rename_reports_a_locked_note_and_renames_the_rest(
    shelf, monkeypatch, lock_note
):
    # Given two notes whose filenames lost a letter, the first locked
    for stem in ("A S�ren", "B S�ren"):
        (shelf / f"{stem}.md").write_text(
            f'---\ntitle: "{stem[0]} Søren"\nauthors:\n  - "Kierkegaard"\n---\n\n'
            "## Notes\n",
            encoding="utf-8",
        )
    locked = shelf / "A S�ren.md"
    monkeypatch.setattr(
        "questionary.confirm",
        lambda *a, **k: type("A", (), {"ask": lambda self: True})(),
    )
    lock_note(locked, reads=True)

    # When the filenames are repaired
    result = runner.invoke(app, ["repair", "--rename"])

    # Then the locked note is named and the other is still renamed
    assert result.exit_code == 0, result.output
    assert "could not be accessed" in result.output
    assert (shelf / "B Søren - Kierkegaard.md").exists()


# --- the wikilink sweep after a rename ---


def test_a_locked_linking_note_does_not_stop_the_wikilink_sweep(tmp_path, lock_note):
    # Given three notes linking to one about to be renamed, the middle one locked
    for name in ("A.md", "B.md", "C.md"):
        (tmp_path / name).write_text("See [[Old Name]].\n", encoding="utf-8")
    lock_note(tmp_path / "B.md", writes=True)

    # When the links are updated
    updated = update_wikilinks_in_vault(tmp_path, "Old Name", "New Name")

    # Then the notes either side of it are updated. Raised, the lock ended the
    # sweep after the rename itself had happened, and the caller reported a
    # rename that happened as a note that could not be accessed.
    assert updated == 2
    assert "[[New Name]]" in (tmp_path / "C.md").read_text(encoding="utf-8")
    assert "[[Old Name]]" in (tmp_path / "B.md").read_text(encoding="utf-8")


def test_a_rename_names_the_notes_whose_links_it_could_not_update(tmp_path, lock_note):
    # Given a note due a rename, and a locked note linking to it
    book = tmp_path / "Dune.md"
    book.write_text(
        "---\ntitle: Dune\nauthors:\n  - Frank Herbert\n---\n\n## Notes\n",
        encoding="utf-8",
    )
    linking = tmp_path / "Links.md"
    linking.write_text("See [[Dune]].\n", encoding="utf-8")
    lock_note(linking, writes=True)

    # When it is renamed
    result = rename_book_file(book, tmp_path)

    # Then the rename is reported as done, and the note still linking to the old
    # name is named so the link can be fixed by hand
    assert result.status == "renamed"
    assert result.unlinked == (linking,)


def test_a_rename_does_not_say_a_note_it_could_not_read_links_to_it(
    tmp_path, lock_note
):
    # Given a note due a rename, and an unrelated note locked against reading
    book = tmp_path / "Dune.md"
    book.write_text(
        "---\ntitle: Dune\nauthors:\n  - Frank Herbert\n---\n\n## Notes\n",
        encoding="utf-8",
    )
    unrelated = tmp_path / "Unrelated.md"
    unrelated.write_text("Nothing about that book.\n", encoding="utf-8")
    lock_note(unrelated, reads=True)

    # When it is renamed
    result = rename_book_file(book, tmp_path)

    # Then the locked note is named as unchecked, not as still linking: the sweep
    # never saw what it holds (#168 review)
    assert result.status == "renamed"
    assert result.unchecked == (unrelated,)
    assert result.unlinked == ()


def test_a_note_that_is_not_utf8_does_not_stop_the_wikilink_sweep(tmp_path):
    # Given three notes linking to one about to be renamed, the middle one not
    # UTF-8 - a decoding error is not an OSError, and ended the sweep the same way
    for name in ("A.md", "C.md"):
        (tmp_path / name).write_text("See [[Old Name]].\n", encoding="utf-8")
    (tmp_path / "B.md").write_bytes("Søren: [[Old Name]].\n".encode("latin-1"))

    # When the links are updated
    sweep = markdown.sweep_wikilinks(tmp_path, "Old Name", "New Name")

    # Then the notes either side of it are updated, and it is named as unchecked
    assert sweep.updated == 2
    assert sweep.unchecked == [tmp_path / "B.md"]


def test_cleanup_says_it_could_not_check_a_note_it_could_not_read(shelf, lock_note):
    # Given a note due a rename, and an unrelated note locked against reading
    (shelf / "Dune.md").write_text(
        "---\ntitle: Dune\nauthors:\n  - Frank Herbert\n---\n\n## Notes\n",
        encoding="utf-8",
    )
    locked = shelf / "Zed.md"
    locked.write_text("Nothing about that book.\n", encoding="utf-8")
    lock_note(locked, reads=True)

    # When cleanup renames it
    result = runner.invoke(app, ["cleanup", "--rename"])

    # Then it says it does not know, rather than that the note still links
    assert result.exit_code == 0, result.output
    assert "Renamed: Dune.md" in result.output
    assert "Zed.md could not be read, so whether it links to Dune" in result.output
    assert "Zed.md still links" not in result.output


def test_cleanup_says_which_links_a_rename_left_pointing_at_the_old_name(
    shelf, lock_note
):
    # Given a note due a rename, and a locked note linking to it
    (shelf / "Dune.md").write_text(
        "---\ntitle: Dune\nauthors:\n  - Frank Herbert\n---\n\n## Notes\n",
        encoding="utf-8",
    )
    linking = shelf / "Links.md"
    linking.write_text(
        "---\ntitle: Links\nauthors:\n  - Links\n---\n\nSee [[Dune]].\n",
        encoding="utf-8",
    )
    lock_note(linking, writes=True)

    # When cleanup renames it
    result = runner.invoke(app, ["cleanup", "--rename"])

    # Then the rename is reported, and so is the link it could not update
    assert result.exit_code == 0, result.output
    assert "Renamed: Dune.md" in result.output
    assert "Links.md still links to Dune" in result.output


# --- `libris import` ---


def test_import_passes_over_a_locked_note_and_updates_the_rest(tmp_path, lock_note):
    # Given two notes an import would update, the first read-only
    from libris.importer import ImportBook, _apply_updates

    locked = create_book_note(
        BookCandidate(title="Dune", authors=["Frank Herbert"]), tmp_path
    )
    lock_note(locked, writes=True)
    book = ImportBook(
        candidate=BookCandidate(title="Dune", authors=["Frank Herbert"]),
        status="Read",
    )

    # When its update is applied
    # Then it is reported as not applied rather than raised, which ended the
    # import for every later note
    assert _apply_updates(locked, book, ["status"]) is False


def _import_two_finished(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    """Two notes marked To Read, and an Audible export saying both are finished."""
    import json

    vault = tmp_path / "vault"
    vault.mkdir()
    first = create_book_note(
        BookCandidate(title="Dune", authors=["Frank Herbert"]), vault
    )
    second = create_book_note(
        BookCandidate(title="Emma", authors=["Jane Austen"]), vault
    )
    export = tmp_path / "library.json"
    export.write_text(
        json.dumps(
            [
                {"title": "Dune", "author": "Frank Herbert", "finished": "Yes"},
                {"title": "Emma", "author": "Jane Austen", "finished": "Yes"},
            ]
        ),
        encoding="utf-8",
    )
    return vault, export, first, second


def test_an_import_reports_a_locked_note_apart_from_one_up_to_date(
    tmp_path, monkeypatch, lock_note
):
    # Given an import due to update two notes, the first read-only
    vault, export, locked, kept = _import_two_finished(tmp_path)
    monkeypatch.setattr("libris.cli.get_vault_path", lambda: vault)
    lock_note(locked, writes=True)

    # When it is applied
    result = runner.invoke(app, ["import", str(export), "--apply"])

    # Then the locked note is said not to have been updated - not counted among
    # the duplicates already up to date, which it is not - and the other is
    assert result.exit_code == 0, result.output
    assert "Could not be updated (1)" in result.output
    assert f"! {locked.name}" in result.output
    assert "0 duplicate(s) already up-to-date" in result.output
    assert "1 existing book(s) updated" in result.output
    assert read_frontmatter(kept)["status"] == "Read"


def test_an_import_reports_a_note_left_damaged_and_goes_on(tmp_path, monkeypatch):
    # Given an import due to update two notes, and the first write failing
    # partway with nothing put back
    from libris.importer import run_import

    vault, export, damaged, kept = _import_two_finished(tmp_path)
    _fail_partway(monkeypatch)

    # When it is applied
    result = run_import(export, vault, apply=True)

    # Then the damaged note is named as such, and the import went on. Not an
    # `OSError`, it escaped and ended the import (#168 review).
    assert [path for _, path in result.damaged_books] == [damaged]
    assert [path for _, path, _ in result.updated_books] == [kept]
    assert read_frontmatter(kept)["status"] == "Read"


def test_an_import_creates_nothing_while_a_note_it_could_not_read_may_match(
    tmp_path, monkeypatch, lock_note
):
    # Given a Shelf where Dune's note is locked against reading, and an export
    # naming Dune, Emma (whose note is readable) and a book new to the Shelf
    import json

    vault, export, locked, kept = _import_two_finished(tmp_path)
    export.write_text(
        json.dumps(
            [
                {"title": "Dune", "author": "Frank Herbert", "finished": "Yes"},
                {"title": "Emma", "author": "Jane Austen", "finished": "Yes"},
                {"title": "Middlemarch", "author": "George Eliot", "finished": "No"},
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("libris.cli.get_vault_path", lambda: vault)
    lock_note(locked, reads=True)

    # When it is applied
    result = runner.invoke(app, ["import", str(export), "--apply"])

    # Then the import runs rather than ending on the locked note, Emma is still
    # updated, and neither unmatched book is created: Dune's would be a second
    # note for a Book the Shelf already holds, and nothing can tell it from
    # Middlemarch until the locked note can be read
    assert result.exit_code == 0, result.output
    assert f"! {locked.name}" in result.output
    assert "2 book(s) matched no note and were not added" in result.output
    assert "? Dune by Frank Herbert" in result.output
    assert "0 new book(s) added" in result.output
    assert read_frontmatter(kept)["status"] == "Read"
    assert sorted(p.name for p in vault.glob("*.md")) == sorted(
        [locked.name, kept.name]
    )


# --- what a failure is said to be (#168 review) ---


def test_a_write_put_back_after_a_full_disk_is_not_called_a_lock(shelf, monkeypatch):
    # Given a note due a repair, and its write failing on a full disk - put
    # back, so an ordinary `OSError` rather than a lock or a permission
    _legacy_note(shelf, "A Full.md")
    _fail_partway(monkeypatch, restore_fails=False)

    # When cleanup runs
    result = runner.invoke(app, ["cleanup"])

    # Then the reason the OS gave is said, and no cause is invented for it
    assert result.exit_code == 0, result.output
    assert "A Full.md could not be accessed (No space left on device)" in result.output
    assert "locked" not in result.output


def test_a_link_update_left_damaged_is_named_apart_from_one_not_reached(
    tmp_path, monkeypatch
):
    # Given a note due a rename, a note linking to it, and that link's write
    # failing partway with nothing put back
    book = tmp_path / "Dune.md"
    book.write_text(
        "---\ntitle: Dune\nauthors:\n  - Frank Herbert\n---\n\n## Notes\n",
        encoding="utf-8",
    )
    linking = tmp_path / "Links.md"
    linking.write_text("See [[Dune]].\n", encoding="utf-8")
    _fail_partway(monkeypatch)

    # When it is renamed
    result = rename_book_file(book, tmp_path)

    # Then the linking note is named as possibly damaged - not merely as still
    # holding the old link, which hides that it may need restoring
    assert result.status == "renamed"
    assert result.damaged == (linking,)
    assert result.unlinked == ()


def test_cleanup_says_a_link_update_may_have_damaged_a_note(shelf, monkeypatch):
    # Given a note due a rename, and the write to a note linking to it failing
    # partway with nothing put back - the second rewrite, after cleanup's own
    # repair of the note being renamed
    (shelf / "Dune.md").write_text(
        "---\ntitle: Dune\nauthors:\n  - Frank Herbert\n---\n\n## Notes\n",
        encoding="utf-8",
    )
    (shelf / "Links.md").write_text(
        "---\ntitle: Links\nauthors:\n  - Links\n---\n\nSee [[Dune]].\n",
        encoding="utf-8",
    )
    _fail_partway(monkeypatch, skip=1)

    # When cleanup renames it
    result = runner.invoke(app, ["cleanup", "--rename"])

    # Then the rename is reported, and so is the damage
    assert result.exit_code == 0, result.output
    assert "Renamed: Dune.md" in result.output
    assert "Links.md may be damaged" in result.output


# --- `libris status` and the MCP update_book tool ---


def test_status_reports_a_locked_note_rather_than_a_traceback(shelf, lock_note):
    # Given a note that cannot be written
    locked = create_book_note(
        BookCandidate(title="Dune", authors=["Frank Herbert"]), shelf
    )
    lock_note(locked, writes=True)

    # When its status is set
    result = runner.invoke(app, ["status", locked.name, "--set", "Read"])

    # Then the command says so and exits 1
    assert result.exit_code == 1, result.output
    assert f"{locked.name} could not be accessed" in result.output
    assert not isinstance(result.exception, PermissionError)
