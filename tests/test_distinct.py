"""Recording that a Duplicate Candidate pair is two books (#142).

`libris duplicates` recomputes from scratch on every run, so an answer of "two
books" has to live on the notes or it is asked for again. These tests cover the
record itself, what honours it, and what carries it through a merge.
"""

from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from libris.cli import app
from libris.config import set_config
from libris.markdown import (
    BookNote,
    NoteChanged,
    find_duplicate_candidates,
    find_pairs_recorded_distinct,
    read_frontmatter,
)
from libris.merge import merge_two_books
from libris.service import (
    DecisionStatus,
    NotTwoNotes,
    apply_decisions,
    record_two_books,
)

runner = CliRunner()


def _note(vault: Path, name: str, title: str, **fields) -> Path:
    """Write a Book Note by Will Guidara with the given title and fields."""
    frontmatter = {
        "libris_id": name.upper(),
        "title": title,
        "authors": ["Will Guidara"],
        "status": "To Read",
        **fields,
    }
    path = vault / f"{name}.md"
    dumped = yaml.dump(frontmatter, sort_keys=False, allow_unicode=True)
    path.write_text(
        f"---\n{dumped}---\n\n# {title}\n\n## Notes\n\nMine.\n", encoding="utf-8"
    )
    return path


def _field_guide_pair(vault: Path, **extra) -> tuple[Path, Path]:
    """The pair the real Shelf offers: a book and its companion field guide."""
    book = _note(vault, "book", "Unreasonable Hospitality", **extra.get("book", {}))
    guide = _note(
        vault,
        "guide",
        "Unreasonable Hospitality: The Field Guide",
        **extra.get("guide", {}),
    )
    return book, guide


def _decision(first: Path, second: Path, verdict: str) -> dict:
    a, b = BookNote.read(first), BookNote.read(second)
    return {
        "decision": verdict,
        "shorter": {"title": a.title, "libris_id": a.libris_id},
        "longer": {"title": b.title, "libris_id": b.libris_id},
    }


# --- what honours the record -------------------------------------------------


def test_the_field_guide_pair_is_offered_until_it_is_settled(tmp_path):
    # Given the pair as the real Shelf holds it, with nothing recorded
    _field_guide_pair(tmp_path)

    # When candidates are found
    # Then it is offered - which is the premise the rest of this file rests on
    assert len(find_duplicate_candidates(tmp_path)) == 1


@pytest.mark.parametrize("side", ["book", "guide"])
def test_a_record_on_either_note_settles_the_pair(tmp_path, side):
    # Given a pair where only one note records the other as a different book -
    # the state a record that half-landed leaves behind
    other = "GUIDE" if side == "book" else "BOOK"
    _field_guide_pair(tmp_path, **{side: {"distinct_from": [other]}})

    # When candidates are found
    # Then the pair is not offered, whichever note carries the record
    assert find_duplicate_candidates(tmp_path) == []


def test_a_record_naming_an_identity_the_other_note_absorbed_still_holds(tmp_path):
    # Given a record made against a note that has since been merged into another:
    # the book's distinct_from names OLDGUIDE, which the guide now answers for
    _field_guide_pair(
        tmp_path,
        book={"distinct_from": ["OLDGUIDE"]},
        guide={"superseded_ids": ["OLDGUIDE"]},
    )

    # When candidates are found
    # Then the pair stays settled (ADR 0014)
    assert find_duplicate_candidates(tmp_path) == []


def test_a_record_about_some_other_book_does_not_settle_this_pair(tmp_path):
    # Given a note settled against a third book, not against the guide
    _field_guide_pair(tmp_path, book={"distinct_from": ["SOMETHINGELSE"]})

    # When candidates are found
    # Then this pair is still offered
    assert len(find_duplicate_candidates(tmp_path)) == 1


def test_a_bare_string_record_is_read_as_one_identity(tmp_path):
    # Given a record hand-written as a bare string rather than a list. Iterated
    # as a string it would be read letter by letter.
    _field_guide_pair(tmp_path, book={"distinct_from": "GUIDE"})

    # When candidates are found
    # Then it settles the pair
    assert find_duplicate_candidates(tmp_path) == []


def test_settled_pairs_are_counted_once(tmp_path):
    # Given a pair recorded on both notes, and a record naming a book that is
    # no longer on the Shelf
    _field_guide_pair(
        tmp_path,
        book={"distinct_from": ["GUIDE", "GONE"]},
        guide={"distinct_from": ["BOOK"]},
    )
    notes = [BookNote.read(path) for path in sorted(tmp_path.glob("*.md"))]

    # When the settled pairs are listed
    pairs = find_pairs_recorded_distinct(notes)

    # Then the pair appears once, and the record about a missing book not at all
    assert len(pairs) == 1
    assert {note.path.name for note in pairs[0]} == {"book.md", "guide.md"}


# --- writing the record ------------------------------------------------------


def test_recording_writes_each_identity_onto_the_other_note(tmp_path):
    # Given the pair
    book, guide = _field_guide_pair(tmp_path)

    # When it is recorded as two books
    record = record_two_books(BookNote.read(book), BookNote.read(guide))

    # Then each note names the other, and the pair is no longer offered
    assert record.written == [book, guide]
    assert record.one_sided is None
    assert read_frontmatter(book)["distinct_from"] == ["GUIDE"]
    assert read_frontmatter(guide)["distinct_from"] == ["BOOK"]
    assert find_duplicate_candidates(tmp_path) == []


def test_recording_leaves_the_rest_of_the_note_alone(tmp_path):
    # Given the pair
    book, guide = _field_guide_pair(tmp_path)

    # When it is recorded
    record_two_books(BookNote.read(book), BookNote.read(guide))

    # Then the reader's writing is untouched
    assert book.read_text(encoding="utf-8").endswith("## Notes\n\nMine.\n")


def test_recording_twice_writes_nothing_the_second_time(tmp_path):
    # Given a pair already recorded
    book, guide = _field_guide_pair(tmp_path)
    record_two_books(BookNote.read(book), BookNote.read(guide))
    before = book.read_bytes(), guide.read_bytes()

    # When it is recorded again
    record = record_two_books(BookNote.read(book), BookNote.read(guide))

    # Then nothing is written and no identity is listed twice
    assert record.written == []
    assert (book.read_bytes(), guide.read_bytes()) == before


def test_recording_adds_to_an_existing_record(tmp_path):
    # Given a book already settled against another
    book, guide = _field_guide_pair(tmp_path, book={"distinct_from": ["EARLIER"]})

    # When it is settled against the guide too
    record_two_books(BookNote.read(book), BookNote.read(guide))

    # Then both answers stand
    assert read_frontmatter(book)["distinct_from"] == ["EARLIER", "GUIDE"]


def test_a_note_without_an_identity_cannot_be_recorded(tmp_path):
    # Given a note that has not been given a Libris ID
    book, guide = _field_guide_pair(tmp_path, guide={"libris_id": None})

    # When the pair is recorded
    # Then it is refused and nothing is written - there is nothing to key it on
    before = book.read_bytes()
    with pytest.raises(NotTwoNotes):
        record_two_books(BookNote.read(book), BookNote.read(guide))
    assert book.read_bytes() == before


def test_one_note_is_not_two_books(tmp_path):
    book, _ = _field_guide_pair(tmp_path)
    note = BookNote.read(book)

    with pytest.raises(NotTwoNotes):
        record_two_books(note, note)


def test_a_note_replaced_since_it_was_read_is_not_written(tmp_path):
    # Given the pair as read, then a different book written at the book's path
    book, guide = _field_guide_pair(tmp_path)
    book_as_read, guide_as_read = BookNote.read(book), BookNote.read(guide)
    book.unlink()
    _note(tmp_path, "book", "Setting the Table", libris_id="SOMEONEELSE")
    before = book.read_bytes(), guide.read_bytes()

    # When the pair is recorded
    # Then nothing is written to either: the replacement was never compared
    with pytest.raises(NoteChanged):
        record_two_books(book_as_read, guide_as_read)
    assert (book.read_bytes(), guide.read_bytes()) == before


def test_a_second_note_that_fails_leaves_a_record_that_still_holds(tmp_path):
    # Given the pair as read, then the guide removed before it is written
    book, guide = _field_guide_pair(tmp_path)
    book_as_read, guide_as_read = BookNote.read(book), BookNote.read(guide)
    moved = guide.with_name("moved.md")
    guide.rename(moved)

    # When the pair is recorded
    record = record_two_books(book_as_read, guide_as_read)

    # Then the book's record is written and said to be one-sided, and the pair
    # - with the guide back under any name - is still settled
    assert record.written == [book]
    assert record.one_sided is not None
    assert "guide.md" in record.one_sided
    assert find_duplicate_candidates(tmp_path) == []


# --- a review's "different" answer -------------------------------------------


def test_a_different_decision_is_recorded_on_the_notes(tmp_path):
    # Given the pair answered "different" in an exported review
    book, guide = _field_guide_pair(tmp_path)

    # When the decision is applied
    outcomes = apply_decisions(tmp_path, [_decision(book, guide, "different")])

    # Then it is recorded rather than skipped, and not offered again
    assert [o.status for o in outcomes] == [DecisionStatus.RECORDED]
    assert read_frontmatter(book)["distinct_from"] == ["GUIDE"]
    assert find_duplicate_candidates(tmp_path) == []


def test_a_different_decision_in_a_dry_run_writes_nothing(tmp_path):
    book, guide = _field_guide_pair(tmp_path)
    before = book.read_bytes(), guide.read_bytes()

    outcomes = apply_decisions(
        tmp_path, [_decision(book, guide, "different")], dry_run=True
    )

    assert [o.status for o in outcomes] == [DecisionStatus.WOULD_RECORD]
    assert (book.read_bytes(), guide.read_bytes()) == before


def test_a_different_decision_about_a_vanished_note_drifts(tmp_path):
    book, guide = _field_guide_pair(tmp_path)
    decision = _decision(book, guide, "different")
    guide.unlink()

    outcomes = apply_decisions(tmp_path, [decision])

    assert [o.status for o in outcomes] == [DecisionStatus.DRIFTED]
    assert "distinct_from" not in read_frontmatter(book)


def test_a_decision_with_no_answer_is_left_alone(tmp_path):
    book, guide = _field_guide_pair(tmp_path)

    outcomes = apply_decisions(tmp_path, [_decision(book, guide, "unsure")])

    assert [o.status for o in outcomes] == [DecisionStatus.SKIPPED]
    assert "distinct_from" not in read_frontmatter(book)


# --- carried through a merge -------------------------------------------------


def test_a_merge_keeps_the_secondarys_answers(tmp_path):
    # Given a guide settled against the book, and a copy of the guide that is
    # about to be merged into another copy that carries no record
    _note(tmp_path, "book", "Unreasonable Hospitality")
    keeper = _note(tmp_path, "guide", "Unreasonable Hospitality: The Field Guide")
    copy = _note(
        tmp_path,
        "guidecopy",
        "Unreasonable Hospitality: The Field Guide",
        distinct_from=["BOOK"],
    )

    # When the copy is merged into the keeper
    merged_fm, _, _ = merge_two_books(keeper, copy, allow_conflicts=True)

    # Then the keeper carries the copy's answer, so the pair stays settled
    assert merged_fm["distinct_from"] == ["BOOK"]


def test_a_merge_drops_answers_that_name_the_survivor_itself(tmp_path):
    # Given two notes recorded as two books, merged anyway - a person changed
    # their mind
    book, guide = _field_guide_pair(
        tmp_path,
        book={"distinct_from": ["GUIDE", "THIRD"]},
        guide={"distinct_from": ["BOOK"]},
    )

    # When one is merged into the other
    merged_fm, _, _ = merge_two_books(book, guide, allow_conflicts=True)

    # Then the survivor is not recorded as a different book from itself, and
    # its answer about a third book stands
    assert merged_fm["distinct_from"] == ["THIRD"]


def test_a_merge_of_notes_with_no_answers_adds_no_field(tmp_path):
    book, guide = _field_guide_pair(tmp_path)

    merged_fm, _, _ = merge_two_books(book, guide, allow_conflicts=True)

    assert "distinct_from" not in merged_fm


# --- the command line --------------------------------------------------------


def test_the_distinct_command_settles_a_pair_duplicates_offered(tmp_path):
    # Given the pair, offered by `libris duplicates`
    _field_guide_pair(tmp_path)
    set_config("vault_path", str(tmp_path))
    offered = runner.invoke(app, ["duplicates"])
    assert "need a person" in offered.output

    # When it is recorded as two books
    result = runner.invoke(app, ["distinct", "book.md", "guide.md"])

    # Then it is reported recorded, and the next report does not offer it but
    # says it passed it over
    assert result.exit_code == 0, result.output
    assert "Recorded" in result.output
    after = runner.invoke(app, ["duplicates"])
    assert "need a person" not in after.output
    assert "1 pair(s) recorded as two books" in after.output


def test_the_distinct_command_refuses_a_note_not_on_the_shelf(tmp_path):
    book, _ = _field_guide_pair(tmp_path)
    set_config("vault_path", str(tmp_path))
    before = book.read_bytes()

    result = runner.invoke(app, ["distinct", "book.md", "nope.md"])

    assert result.exit_code == 1
    assert book.read_bytes() == before


def test_the_distinct_command_refuses_a_note_without_an_identity(tmp_path):
    _field_guide_pair(tmp_path, guide={"libris_id": None})
    set_config("vault_path", str(tmp_path))

    result = runner.invoke(app, ["distinct", "book.md", "guide.md"])

    assert result.exit_code == 1
    assert "Nothing recorded" in result.output
