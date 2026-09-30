"""A note that is there but cannot be opened, met partway through a merge (#165).

A locked, read-only or permission-denied note raises `PermissionError`, which is
an `OSError` but not a `FileNotFoundError`. The merge paths caught only the
second, so one locked note ended a whole batch of decisions - and, met at the
delete, ended it after the merged note had been written, with nothing saying so.

The lock is simulated by refusing `Path.open` or `Path.unlink` for one path, which
is what a real lock refuses and works the same on every platform.
"""

import errno
from pathlib import Path

import pytest
from typer.testing import CliRunner

from libris import cli as cli_module
from libris import markdown, service
from libris.api import BookCandidate
from libris.cli import app
from libris.markdown import (
    BookNote,
    NoteWriteFailed,
    create_book_note,
    read_frontmatter,
    rewrite_note,
)
from libris.merge import get_primary_book
from libris.service import DecisionStatus, apply_decisions

runner = CliRunner()

_WRITE_MODES = set("wax+")


def _lock(
    monkeypatch,
    locked: Path,
    *,
    reads: bool = False,
    writes: bool = False,
    deletes: bool = False,
) -> None:
    """Refuse some kinds of access to one note, as a lock or a read-only file does.

    Args:
        monkeypatch: The test's monkeypatch fixture.
        locked: The note to refuse.
        reads: Refuse opening it to read.
        writes: Refuse opening it to write.
        deletes: Refuse removing it.
    """
    real_open = Path.open
    real_unlink = Path.unlink

    def _open(self, mode="r", *args, **kwargs):
        writing = bool(_WRITE_MODES & set(mode))
        if self == locked and ((writes and writing) or (reads and not writing)):
            raise PermissionError(13, "Permission denied", str(self))
        return real_open(self, mode, *args, **kwargs)

    def _unlink(self, *args, **kwargs):
        if self == locked and deletes:
            raise PermissionError(13, "Permission denied", str(self))
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", _open)
    monkeypatch.setattr(Path, "unlink", _unlink)


def _pair(vault: Path, title: str, subtitle: str) -> tuple[BookNote, BookNote]:
    """Two notes for one Book, differing by a subtitle."""
    a = create_book_note(BookCandidate(title=title, authors=["Frank Herbert"]), vault)
    b = create_book_note(
        BookCandidate(title=f"{title}: {subtitle}", authors=["Frank Herbert"]), vault
    )
    return BookNote.read(a), BookNote.read(b)


def _decision(first: BookNote, second: BookNote) -> dict:
    return {
        "decision": "same",
        "shorter": {"title": first.title, "libris_id": first.libris_id},
        "longer": {"title": second.title, "libris_id": second.libris_id},
    }


def _two_pairs(vault: Path):
    """A pair to lock, and a second pair whose decision comes after it."""
    locked_pair = _pair(vault, "Dune", "Deluxe Edition")
    later_pair = _pair(vault, "The Brass Verdict", "A Novel")
    return locked_pair, later_pair, [_decision(*locked_pair), _decision(*later_pair)]


def _snapshot(*notes: BookNote) -> list[bytes]:
    return [note.path.read_bytes() for note in notes]


# --- `libris merge --decisions` ---


def test_a_note_that_cannot_be_read_is_left_out_of_the_index(tmp_path, monkeypatch):
    # Given a Shelf holding one note that cannot be opened at all
    unreadable = create_book_note(
        BookCandidate(title="Children of Dune", authors=["Frank Herbert"]), tmp_path
    )
    first, second = _pair(tmp_path, "Dune", "Deluxe Edition")
    _lock(monkeypatch, unreadable, reads=True)

    # When a decision about another pair is applied
    outcomes = apply_decisions(tmp_path, [_decision(first, second)])

    # Then that pair is merged. Indexing read every note unguarded, so one locked
    # note anywhere on the Shelf stopped the run before any decision applied.
    assert [o.status for o in outcomes] == [DecisionStatus.MERGED]


def test_a_note_locked_before_the_merge_reads_it_drifts_and_the_batch_goes_on(
    tmp_path, monkeypatch
):
    # Given two decisions, the first pair's note locked after the Shelf was
    # indexed and before its merge reads it
    (first, second), _, decisions = _two_pairs(tmp_path)
    before = _snapshot(first, second)
    real_index = service.build_id_index

    def _locked_after_indexing(vault_path):
        index = real_index(vault_path)
        _lock(monkeypatch, second.path, reads=True)
        return index

    monkeypatch.setattr(service, "build_id_index", _locked_after_indexing)

    # When the decisions are applied
    outcomes = apply_decisions(tmp_path, decisions)

    # Then the first drifts, says the note could not be read rather than that it
    # is gone, and the second is still merged
    assert [o.status for o in outcomes] == [
        DecisionStatus.DRIFTED,
        DecisionStatus.MERGED,
    ]
    assert "could not be read" in outcomes[0].detail
    assert "gone" not in outcomes[0].detail
    monkeypatch.undo()
    assert _snapshot(first, second) == before


def test_a_secondary_locked_before_the_check_drifts_and_nothing_is_written(
    tmp_path, monkeypatch
):
    # Given two decisions, the first pair's secondary locked once the merge has
    # been worked out, before it is checked
    (first, second), _, decisions = _two_pairs(tmp_path)
    before = _snapshot(first, second)
    secondary = (
        second.path
        if get_primary_book(first.path, second.path) == first.path
        else first.path
    )
    real_merge = service.merge_two_books

    def _locked_after_merging(primary_path, secondary_path, **kwargs):
        result = real_merge(primary_path, secondary_path, **kwargs)
        if secondary_path == secondary:
            _lock(monkeypatch, secondary, reads=True)
        return result

    monkeypatch.setattr(service, "merge_two_books", _locked_after_merging)

    # When the decisions are applied
    outcomes = apply_decisions(tmp_path, decisions)

    # Then the first drifts with both notes as they were, and the second merges
    assert [o.status for o in outcomes] == [
        DecisionStatus.DRIFTED,
        DecisionStatus.MERGED,
    ]
    assert "could not be read" in outcomes[0].detail
    assert secondary.name in outcomes[0].detail
    monkeypatch.undo()
    assert _snapshot(first, second) == before


def test_a_primary_that_cannot_be_written_drifts_and_the_secondary_is_kept(
    tmp_path, monkeypatch
):
    # Given two decisions, the first pair's primary read-only
    (first, second), _, decisions = _two_pairs(tmp_path)
    before = _snapshot(first, second)
    primary = get_primary_book(first.path, second.path)
    _lock(monkeypatch, primary, writes=True)

    # When the decisions are applied
    outcomes = apply_decisions(tmp_path, decisions)

    # Then the first drifts, saying the primary could not be written, both of its
    # notes are untouched, and the second is merged
    assert [o.status for o in outcomes] == [
        DecisionStatus.DRIFTED,
        DecisionStatus.MERGED,
    ]
    assert "could not be written" in outcomes[0].detail
    assert primary.name in outcomes[0].detail
    monkeypatch.undo()
    assert _snapshot(first, second) == before


def test_a_secondary_that_cannot_be_deleted_is_still_reported_merged(
    tmp_path, monkeypatch
):
    # Given two decisions, the first pair's secondary impossible to delete
    (first, second), _, decisions = _two_pairs(tmp_path)
    primary = get_primary_book(first.path, second.path)
    secondary = second.path if primary == first.path else first.path
    _lock(monkeypatch, secondary, deletes=True)

    # When the decisions are applied
    outcomes = apply_decisions(tmp_path, decisions)

    # Then the first is merged - the merged note was written - and says the
    # secondary was kept. The batch used to end here, with the merge written and
    # reported nowhere.
    assert [o.status for o in outcomes] == [
        DecisionStatus.MERGED,
        DecisionStatus.MERGED,
    ]
    assert "could not be deleted" in outcomes[0].detail
    assert secondary.name in outcomes[0].detail
    assert secondary.exists()
    secondary_id = (first if secondary == first.path else second).libris_id
    assert secondary_id in read_frontmatter(primary)["superseded_ids"]


def test_a_merged_note_that_cannot_be_read_back_is_still_reported_merged(
    tmp_path, monkeypatch
):
    # Given two decisions, the first pair's merged note locked once it is written
    # and the secondary deleted, before it is read back for the index
    (first, second), _, decisions = _two_pairs(tmp_path)
    primary = get_primary_book(first.path, second.path)
    real_delete = service.delete_secondary_file

    def _locked_after_deleting(secondary_path, *args):
        real_delete(secondary_path, *args)
        if secondary_path in (first.path, second.path):
            _lock(monkeypatch, primary, reads=True)

    monkeypatch.setattr(service, "delete_secondary_file", _locked_after_deleting)

    # When the decisions are applied
    outcomes = apply_decisions(tmp_path, decisions)

    # Then the first is merged and says so, and the second is merged too
    assert [o.status for o in outcomes] == [
        DecisionStatus.MERGED,
        DecisionStatus.MERGED,
    ]
    assert "could not be read back" in outcomes[0].detail


# --- interactive `libris merge` ---


def _auto_group(vault: Path, stem: str) -> list[Path]:
    """Two copies of one Book that `merge --auto` merges without asking."""
    paths = []
    for suffix in ("A", "B"):
        path = vault / f"{stem} {suffix}.md"
        path.write_text(
            f"---\ntitle: {stem}\nauthors:\n  - Frank Herbert\n"
            f"isbn: '97804410135{len(stem):02d}'\ngoogle_books_id: gb-{stem}\n"
            "---\n\n## Notes\n\nMine.\n",
            encoding="utf-8",
        )
        paths.append(path)
    return paths


def _shelf(tmp_path, monkeypatch) -> Path:
    vault = tmp_path / "shelf"
    vault.mkdir()
    monkeypatch.setattr("libris.cli.get_vault_path", lambda: vault)
    return vault


def test_merge_skips_a_group_with_a_locked_note_and_goes_on(tmp_path, monkeypatch):
    # Given two groups of duplicates, a note of the first locked after the groups
    # were found and before they are shown
    vault = _shelf(tmp_path, monkeypatch)
    locked_group = _auto_group(vault, "Dune")
    _auto_group(vault, "Children of Dune")
    real_find = cli_module.find_duplicates

    def _locked_after_finding(vault_path):
        groups = sorted(real_find(vault_path), key=lambda g: g[0] not in locked_group)
        _lock(monkeypatch, locked_group[1], reads=True)
        return groups

    monkeypatch.setattr(cli_module, "find_duplicates", _locked_after_finding)

    # When the duplicates are auto-merged
    result = runner.invoke(app, ["merge", "--auto"])

    # Then the locked group is skipped with a reason, and the other is merged.
    # The group's handler caught only a missing note, so a locked one ended the
    # command before any group merged.
    assert result.exit_code == 0, result.output
    assert "could not be read" in result.output
    assert "1 duplicate(s) merged" in result.output


def test_merge_leaves_a_pair_whose_primary_cannot_be_written(tmp_path, monkeypatch):
    # Given one group of duplicates, its primary read-only
    vault = _shelf(tmp_path, monkeypatch)
    group = _auto_group(vault, "Dune")
    before = [path.read_bytes() for path in group]
    _lock(monkeypatch, get_primary_book(*group), writes=True)

    # When it is auto-merged
    result = runner.invoke(app, ["merge", "--auto"])

    # Then it says the primary could not be written, and nothing changed
    assert result.exit_code == 0, result.output
    assert "could not be written" in result.output
    assert "Error:" not in result.output
    assert "0 duplicate(s) merged" in result.output
    monkeypatch.undo()
    assert [path.read_bytes() for path in group] == before


def test_merge_counts_a_merge_whose_secondary_cannot_be_deleted(tmp_path, monkeypatch):
    # Given one group of duplicates, its secondary impossible to delete
    vault = _shelf(tmp_path, monkeypatch)
    group = _auto_group(vault, "Dune")
    primary = get_primary_book(*group)
    secondary = next(path for path in group if path != primary)
    _lock(monkeypatch, secondary, deletes=True)

    # When it is auto-merged
    result = runner.invoke(app, ["merge", "--auto"])

    # Then the merge is counted - the merged note was written - and the kept
    # secondary is named. It was a bare "Error:" and counted nothing.
    assert result.exit_code == 0, result.output
    assert "Error:" not in result.output
    assert "could not be deleted" in result.output
    assert "1 duplicate(s) merged" in result.output
    assert secondary.exists()


def test_merge_leaves_a_pair_whose_secondary_is_locked_before_the_check(
    tmp_path, monkeypatch
):
    # Given one group of duplicates, the secondary locked after the merge was
    # worked out and before it is checked
    vault = _shelf(tmp_path, monkeypatch)
    group = _auto_group(vault, "Dune")
    before = [path.read_bytes() for path in group]
    real_check = cli_module.check_auto_merge

    def _locked_after_checking(primary, secondary):
        result = real_check(primary, secondary)
        _lock(monkeypatch, secondary, reads=True)
        return result

    monkeypatch.setattr(cli_module, "check_auto_merge", _locked_after_checking)

    # When it is auto-merged
    result = runner.invoke(app, ["merge", "--auto"])

    # Then it says the secondary could not be read, and nothing changed
    assert result.exit_code == 0, result.output
    assert "could not be read" in result.output
    assert "Error:" not in result.output
    assert "0 duplicate(s) merged" in result.output
    monkeypatch.undo()
    assert [path.read_bytes() for path in group] == before


# --- a write that fails partway (#166 review) ---


def _fail_partway(
    monkeypatch, *, restore_fails: bool, whole: bool = False, skip: int = 0
) -> None:
    """Make a rewrite fail after writing, as a full disk or a lock landing does.

    Args:
        monkeypatch: The test's monkeypatch fixture.
        restore_fails: Fail putting the old bytes back as well.
        whole: Write all of the new text before failing, rather than half. The
            note is then damaged but still parses, which is the case that lets
            a later step act on it again.
        skip: Let this many rewrites succeed before the one that fails.
    """
    real = markdown._replace_bytes
    # A rewrite that succeeds makes one call; the failing one makes a second,
    # to put the old bytes back.
    failing = {skip + 1, skip + 2} if restore_fails else {skip + 1}
    calls = 0

    def _replace(fd, data):
        nonlocal calls
        calls += 1
        if calls in failing:
            real(fd, data if whole else data[: len(data) // 2])
            raise OSError(errno.ENOSPC, "No space left on device")
        return real(fd, data)

    monkeypatch.setattr(markdown, "_replace_bytes", _replace)


def test_a_rewrite_that_fails_partway_puts_the_note_back(tmp_path, monkeypatch):
    # Given a note, and a write that will fail halfway through
    (note, _) = _pair(tmp_path, "Dune", "Deluxe Edition")
    before = note.path.read_bytes()
    _fail_partway(monkeypatch, restore_fails=False)

    # When it is rewritten
    # Then the failure is raised as an OSError - which every caller reports as
    # a note left as it was - and that is true
    with pytest.raises(OSError, match="No space"):
        rewrite_note(note.path, "---\ntitle: Dune\n---\n\nReplaced.\n")
    assert note.path.read_bytes() == before


def test_a_rewrite_that_cannot_be_put_back_is_not_an_os_error(tmp_path, monkeypatch):
    # Given a note, and a write that fails halfway and cannot be undone
    (note, _) = _pair(tmp_path, "Dune", "Deluxe Edition")
    _fail_partway(monkeypatch, restore_fails=True)

    # When it is rewritten
    # Then it raises something no "nothing written" handler catches
    with pytest.raises(NoteWriteFailed, match="may be damaged") as raised:
        rewrite_note(note.path, "---\ntitle: Dune\n---\n\nReplaced.\n")
    assert not isinstance(raised.value, OSError)


def test_a_primary_write_put_back_after_failing_leaves_the_pair_as_it_was(
    tmp_path, monkeypatch
):
    # Given two decisions, the first pair's primary write failing partway and
    # being put back
    (first, second), _, decisions = _two_pairs(tmp_path)
    before = _snapshot(first, second)
    _fail_partway(monkeypatch, restore_fails=False)

    # When the decisions are applied
    outcomes = apply_decisions(tmp_path, decisions)

    # Then the first drifts with both notes byte for byte as they were, and the
    # second merges
    assert [o.status for o in outcomes] == [
        DecisionStatus.DRIFTED,
        DecisionStatus.MERGED,
    ]
    assert "could not be written" in outcomes[0].detail
    assert _snapshot(first, second) == before


def test_a_primary_left_damaged_is_reported_and_the_secondary_kept(
    tmp_path, monkeypatch
):
    # Given two decisions, the first pair's primary write failing partway and
    # not able to be put back
    (first, second), _, decisions = _two_pairs(tmp_path)
    primary = get_primary_book(first.path, second.path)
    secondary = second.path if primary == first.path else first.path
    secondary_before = secondary.read_bytes()
    _fail_partway(monkeypatch, restore_fails=True)

    # When the decisions are applied
    outcomes = apply_decisions(tmp_path, decisions)

    # Then the first says the primary may be damaged rather than that nothing
    # was merged, the secondary is kept untouched, and the second merges
    assert [o.status for o in outcomes] == [
        DecisionStatus.DRIFTED,
        DecisionStatus.MERGED,
    ]
    assert "may be damaged" in outcomes[0].detail
    assert "nothing merged" not in outcomes[0].detail
    assert secondary.read_bytes() == secondary_before


def test_a_two_books_record_left_damaged_is_not_reported_as_nothing_recorded(
    tmp_path, monkeypatch
):
    # Given a decision that a pair is two books, the first note's write failing
    # partway and not able to be put back
    first, second = _pair(tmp_path, "Dune", "Deluxe Edition")
    decision = {**_decision(first, second), "decision": "different"}
    _fail_partway(monkeypatch, restore_fails=True)

    # When it is applied
    outcomes = apply_decisions(tmp_path, [decision])

    # Then it says the note may be damaged
    assert [o.status for o in outcomes] == [DecisionStatus.DRIFTED]
    assert "may be damaged" in outcomes[0].detail
    assert "Nothing recorded" not in outcomes[0].detail


def test_merge_reports_a_primary_left_damaged(tmp_path, monkeypatch):
    # Given one group of duplicates, the primary's write failing partway and not
    # able to be put back
    vault = _shelf(tmp_path, monkeypatch)
    group = _auto_group(vault, "Dune")
    primary = get_primary_book(*group)
    secondary = next(path for path in group if path != primary)
    secondary_before = secondary.read_bytes()
    _fail_partway(monkeypatch, restore_fails=True)

    # When it is auto-merged
    result = runner.invoke(app, ["merge", "--auto"])

    # Then it says the primary may be damaged, the secondary is kept, and
    # nothing is counted as merged
    assert result.exit_code == 0, result.output
    # - said by the write's own handler, not the command's "Error:" catch-all,
    # whose text carries "may be damaged" too
    assert "may be damaged" in result.output
    assert f"{secondary.name} was kept" in result.output
    assert "Error:" not in result.output
    assert "Nothing merged" not in result.output
    assert "0 duplicate(s) merged" in result.output
    assert secondary.read_bytes() == secondary_before


def test_the_distinct_command_reports_a_note_left_damaged(tmp_path, monkeypatch):
    # Given a pair on the Shelf, the first note's write failing partway and not
    # able to be put back
    vault = _shelf(tmp_path, monkeypatch)
    first, second = _pair(vault, "Dune", "Deluxe Edition")
    _fail_partway(monkeypatch, restore_fails=True)

    # When the pair is recorded as two books
    result = runner.invoke(app, ["distinct", first.path.name, second.path.name])

    # Then it fails saying the note may be damaged - not "Nothing recorded", and
    # not a traceback
    assert result.exit_code == 1, result.output
    assert "may be damaged" in result.output
    assert "Nothing recorded" not in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)


def test_a_primary_left_damaged_is_not_acted_on_by_a_later_decision(
    tmp_path, monkeypatch
):
    # Given the same decision twice - repeated decisions are valid input - and
    # the first one's primary write failing and not able to be put back, leaving
    # a note that still parses
    first, second = _pair(tmp_path, "Dune", "Deluxe Edition")
    decision = _decision(first, second)
    _fail_partway(monkeypatch, restore_fails=True, whole=True)

    # When the decisions are applied
    outcomes = apply_decisions(tmp_path, [decision, decision])

    # Then the repeat drifts rather than merging into the damaged note: it was
    # left in the index, so the repeat read it and rewrote it
    assert [o.status for o in outcomes] == [
        DecisionStatus.DRIFTED,
        DecisionStatus.DRIFTED,
    ]
    assert "may be damaged" in outcomes[0].detail
    assert len(list(tmp_path.glob("*.md"))) == 2


def test_a_first_note_left_damaged_by_a_two_books_record_is_not_acted_on_again(
    tmp_path, monkeypatch
):
    # Given the same two-books decision twice, the first note's write failing
    # and not able to be put back
    first, second = _pair(tmp_path, "Dune", "Deluxe Edition")
    decision = {**_decision(first, second), "decision": "different"}
    _fail_partway(monkeypatch, restore_fails=True, whole=True)

    # When the decisions are applied
    outcomes = apply_decisions(tmp_path, [decision, decision])

    # Then the repeat drifts rather than writing the pair again
    assert [o.status for o in outcomes] == [
        DecisionStatus.DRIFTED,
        DecisionStatus.DRIFTED,
    ]


def test_a_second_note_left_damaged_by_a_two_books_record_is_not_acted_on_again(
    tmp_path, monkeypatch
):
    # Given the same two-books decision twice, the first note written and the
    # second's write failing and not able to be put back
    first, second = _pair(tmp_path, "Dune", "Deluxe Edition")
    decision = {**_decision(first, second), "decision": "different"}
    _fail_partway(monkeypatch, restore_fails=True, whole=True, skip=1)

    # When the decisions are applied
    outcomes = apply_decisions(tmp_path, [decision, decision])

    # Then the first is recorded - the first note settles the pair - and says
    # the second may be damaged, and the repeat drifts rather than touching it
    assert [o.status for o in outcomes] == [
        DecisionStatus.RECORDED,
        DecisionStatus.DRIFTED,
    ]
    assert "may be damaged" in outcomes[0].detail


def test_merge_leaves_the_rest_of_a_group_whose_primary_was_left_damaged(
    tmp_path, monkeypatch
):
    # Given three copies of one Book, the first merge's write failing and not
    # able to be put back, leaving a primary that still parses
    vault = _shelf(tmp_path, monkeypatch)
    group = _auto_group(vault, "Dune")
    third = vault / "Dune C.md"
    third.write_bytes(group[0].read_bytes())
    third_before = third.read_bytes()
    _fail_partway(monkeypatch, restore_fails=True, whole=True)

    # When they are auto-merged
    result = runner.invoke(app, ["merge", "--auto"])

    # Then nothing more is merged into the damaged primary: returned as a plain
    # refusal, the group went on to the next copy and merged it in
    assert result.exit_code == 0, result.output
    assert "rest of this group was left alone" in result.output
    assert "0 duplicate(s) merged" in result.output
    assert len(list(vault.glob("*.md"))) == 3
    assert third_before in {path.read_bytes() for path in vault.glob("*.md")}


def _second_write_changed(monkeypatch) -> None:
    """Make every second write find its note edited since it was read."""
    real = service.edit_note
    calls = 0

    def _edit(path, decide, *args, **kwargs):
        nonlocal calls
        calls += 1
        if calls % 2 == 0:
            raise markdown.NoteChanged(f"{path.name} has changed since it was read.")
        return real(path, decide, *args, **kwargs)

    monkeypatch.setattr(service, "edit_note", _edit)


@pytest.mark.parametrize("failure", ["damaged", "changed"])
def test_a_one_sided_record_reads_as_one_sentence(tmp_path, monkeypatch, failure):
    # Given two pairs, and each pair's second note failing to be written - left
    # damaged, or edited since it was read. Both reasons end in a full stop.
    vault = _shelf(tmp_path, monkeypatch)
    first, second = _pair(vault, "Dune", "Deluxe Edition")
    third, fourth = _pair(vault, "Children of Dune", "Deluxe Edition")
    if failure == "damaged":
        _fail_partway(monkeypatch, restore_fails=True, whole=True, skip=1)
    else:
        _second_write_changed(monkeypatch)

    # When one is recorded from the command and the other from a decision
    result = runner.invoke(app, ["distinct", first.path.name, second.path.name])
    if failure == "damaged":
        # Re-armed, so the decision's second write is the one that fails.
        monkeypatch.undo()
        _fail_partway(monkeypatch, restore_fails=True, whole=True, skip=1)
    outcomes = apply_decisions(
        vault, [{**_decision(third, fourth), "decision": "different"}]
    )

    # Then neither message doubles its punctuation around the reason
    assert "only that note says so" in result.output
    assert "the first note's record still holds" in outcomes[0].detail
    for text in (result.output, outcomes[0].detail):
        assert ".." not in text
        assert ".;" not in text
