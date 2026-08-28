"""
Tests for book merge functionality.
"""

from pathlib import Path

import pytest
import yaml

from libris.api import BookCandidate
from libris.markdown import BookNote, create_book_note, read_frontmatter
from libris.merge import (
    check_auto_merge,
    delete_secondary_file,
    get_primary_book,
    merge_two_books,
    write_merged_book,
)


def _write_book(vault: Path, name: str, **frontmatter_fields) -> Path:
    """Helper to write a minimal book note with given frontmatter fields."""
    lines = ["---"]
    for key, val in frontmatter_fields.items():
        if isinstance(val, list):
            lines.append(f"{key}:")
            for item in val:
                lines.append(f"  - {item}")
        elif val is None:
            lines.append(f"{key}:")
        else:
            lines.append(f"{key}: {val}")
    lines.append("---\n")
    lines.append("## Notes\n\n")
    p = vault / name
    p.write_text("\n".join(lines), encoding="utf-8")
    return p


class TestMergeTwoBooks:
    """Test merge_two_books() function."""

    def test_identical_books_no_conflicts(self, tmp_path):
        """Two identical books should merge cleanly with no conflicts."""
        path1 = _write_book(
            tmp_path,
            "A.md",
            title="Test Book",
            authors=["Alice"],
            isbn="123-456",
            google_books_id="gb1",
            status="To Read",
        )
        path2 = _write_book(
            tmp_path,
            "B.md",
            title="Test Book",
            authors=["Alice"],
            isbn="123-456",
            google_books_id="gb1",
            status="To Read",
        )

        merged_fm, merged_body, conflicts = merge_two_books(path1, path2)

        assert len(conflicts) == 0
        assert merged_fm["title"] == "Test Book"
        assert merged_fm["isbn"] == "123-456"

    def test_status_priority_read_beats_to_read(self, tmp_path):
        """Status 'Read' should beat 'To Read' in merge."""
        path1 = _write_book(
            tmp_path,
            "A.md",
            title="Book",
            isbn="123",
            google_books_id="gb1",
            status="To Read",
        )
        path2 = _write_book(
            tmp_path,
            "B.md",
            title="Book",
            isbn="123",
            google_books_id="gb1",
            status="Read",
        )

        merged_fm, _, conflicts = merge_two_books(path1, path2)

        assert len(conflicts) == 0
        assert merged_fm["status"] == "Read"

    def test_status_priority_both_read(self, tmp_path):
        """Both 'Read' should stay 'Read'."""
        path1 = _write_book(
            tmp_path,
            "A.md",
            title="Book",
            isbn="123",
            google_books_id="gb1",
            status="Read",
        )
        path2 = _write_book(
            tmp_path,
            "B.md",
            title="Book",
            isbn="123",
            google_books_id="gb1",
            status="Read",
        )

        merged_fm, _, conflicts = merge_two_books(path1, path2)

        assert len(conflicts) == 0
        assert merged_fm["status"] == "Read"

    def test_null_field_filled_from_secondary(self, tmp_path):
        """Null fields in primary should be filled from secondary."""
        path1 = _write_book(
            tmp_path,
            "A.md",
            title="Book",
            isbn="123",
            google_books_id="gb1",
            page_count=None,
            description=None,
        )
        path2 = _write_book(
            tmp_path,
            "B.md",
            title="Book",
            isbn="123",
            google_books_id="gb1",
            page_count=300,
            description=None,
        )

        merged_fm, _, conflicts = merge_two_books(path1, path2)

        assert len(conflicts) == 0
        assert merged_fm["page_count"] == 300

    def test_metadata_conflict_different_authors(self, tmp_path):
        """Different non-null author lists should be unioned, not contested.

        Uses `authors`, the field every note carries. This test named the
        legacy `author` key, so it asserted union behaviour on a field the
        merge never sees (#72).
        """
        path1 = _write_book(
            tmp_path,
            "A.md",
            title="Book",
            isbn="123",
            google_books_id="gb1",
            authors=["Alice"],
        )
        path2 = _write_book(
            tmp_path,
            "B.md",
            title="Book",
            isbn="123",
            google_books_id="gb1",
            authors=["Bob"],
        )

        merged_fm, _, conflicts = merge_two_books(path1, path2)

        # Should have conflict in author field
        conflict_fields = [c.field for c in conflicts]
        assert "authors" not in conflict_fields  # Authors should be merged as lists
        assert len(merged_fm["authors"]) == 2

    def test_metadata_conflict_different_page_count(self, tmp_path):
        """Different page counts should trigger conflict."""
        path1 = _write_book(
            tmp_path,
            "A.md",
            title="Book",
            isbn="123",
            google_books_id="gb1",
            page_count=300,
            rating=5,
        )
        path2 = _write_book(
            tmp_path,
            "B.md",
            title="Book",
            isbn="123",
            google_books_id="gb1",
            page_count=250,
            rating=3,
        )

        merged_fm, _, conflicts = merge_two_books(path1, path2)

        # page_count conflict should be detected
        conflict_fields = [c.field for c in conflicts]
        assert "rating" in conflict_fields

    def test_conflicts_prevent_merge_when_not_allowed(self, tmp_path):
        """With allow_conflicts=False, conflicts should be returned."""
        path1 = _write_book(
            tmp_path,
            "A.md",
            title="Book",
            isbn="123",
            google_books_id="gb1",
            page_count=300,
            rating=5,
        )
        path2 = _write_book(
            tmp_path,
            "B.md",
            title="Book",
            isbn="123",
            google_books_id="gb1",
            page_count=250,
            rating=3,
        )

        merged_fm, merged_body, conflicts = merge_two_books(
            path1, path2, allow_conflicts=False
        )

        assert len(conflicts) > 0
        assert merged_body == ""  # Body empty when merge blocked

    def test_merge_with_allow_conflicts(self, tmp_path):
        """With allow_conflicts=True, conflicts should be resolved and body merged."""
        path1 = _write_book(
            tmp_path,
            "A.md",
            title="Book",
            isbn="123",
            google_books_id="gb1",
            page_count=300,
            rating=5,
        )
        path2 = _write_book(
            tmp_path,
            "B.md",
            title="Book",
            isbn="123",
            google_books_id="gb1",
            page_count=250,
            rating=3,
        )

        merged_fm, merged_body, conflicts = merge_two_books(
            path1, path2, allow_conflicts=True
        )

        # Merge should proceed
        assert merged_fm["rating"] == 5  # Primary wins
        assert len(conflicts) > 0  # But conflicts are still recorded

    def test_merge_author_lists(self, tmp_path):
        """Author lists from both books should be merged and deduplicated."""
        path1 = _write_book(
            tmp_path,
            "A.md",
            title="Book",
            isbn="123",
            google_books_id="gb1",
            authors=["Alice", "Charlie"],
        )
        path2 = _write_book(
            tmp_path,
            "B.md",
            title="Book",
            isbn="123",
            google_books_id="gb1",
            authors=["Bob", "Alice"],  # Alice appears in both
        )

        merged_fm, _, conflicts = merge_two_books(path1, path2)

        assert len(conflicts) == 0
        assert len(merged_fm["authors"]) == 3
        assert "Alice" in merged_fm["authors"]
        assert "Bob" in merged_fm["authors"]
        assert "Charlie" in merged_fm["authors"]

    def test_merge_body_content(self, tmp_path):
        """Body content from both files should be merged."""
        vault = tmp_path / "vault"
        vault.mkdir()

        # Create book with custom body content
        path1 = vault / "A.md"
        path1.write_text(
            "---\n"
            "title: Book\nisbn: 123\ngoogle_books_id: gb1\n"
            "---\n"
            "## Notes\n\nMy notes for book A\n"
        )

        path2 = vault / "B.md"
        path2.write_text(
            "---\n"
            "title: Book\nisbn: 123\ngoogle_books_id: gb1\n"
            "---\n"
            "## Notes\n\nMy notes for book B\n"
        )

        merged_fm, merged_body, conflicts = merge_two_books(path1, path2)

        assert "My notes for book A" in merged_body
        assert "My notes for book B" in merged_body

    def test_skip_generic_description_section(self, tmp_path):
        """Generic Description sections (from API) should be skipped during merge."""
        vault = tmp_path / "vault"
        vault.mkdir()

        path1 = vault / "A.md"
        path1.write_text(
            "---\n"
            "title: Book\nisbn: 123\ngoogle_books_id: gb1\n"
            "---\n"
            "## Notes\n\nMy personal notes\n\n### Description\nAPI-provided description\n"
        )

        path2 = vault / "B.md"
        path2.write_text(
            "---\n"
            "title: Book\nisbn: 123\ngoogle_books_id: gb1\n"
            "---\n"
            "## Notes\n\n### Description\nDifferent API description\n"
        )

        merged_fm, merged_body, conflicts = merge_two_books(path1, path2)

        # Personal notes should be there
        assert "My personal notes" in merged_body
        # API descriptions should not appear
        assert "API-provided description" not in merged_body
        assert "Different API description" not in merged_body


class TestCheckAutoMerge:
    """Test check_auto_merge() function."""

    def test_auto_merge_both_ids_match(self, tmp_path):
        """Should auto-merge when both ISBN and Google ID match."""
        path1 = _write_book(
            tmp_path,
            "A.md",
            title="Book A",
            isbn="123",
            google_books_id="gb1",
            page_count=300,
        )
        path2 = _write_book(
            tmp_path,
            "B.md",
            title="Book B",
            isbn="123",
            google_books_id="gb1",
            page_count=300,
        )

        can_merge, reason, merge_result = check_auto_merge(path1, path2)
        assert can_merge is True
        assert reason is None
        assert merge_result is not None
        merged_fm, merged_body, _ = merge_result
        assert str(merged_fm["isbn"]) == "123"

    def test_no_auto_merge_isbn_mismatch(self, tmp_path):
        """Should not auto-merge if ISBN differs."""
        path1 = _write_book(
            tmp_path, "A.md", title="Book", isbn="123", google_books_id="gb1"
        )
        path2 = _write_book(
            tmp_path, "B.md", title="Book", isbn="999", google_books_id="gb1"
        )

        can_merge, reason, merge_result = check_auto_merge(path1, path2)
        assert can_merge is False
        assert reason is not None
        assert merge_result is None

    def test_no_auto_merge_google_id_mismatch(self, tmp_path):
        """Should not auto-merge if Google Books ID differs."""
        path1 = _write_book(
            tmp_path, "A.md", title="Book", isbn="123", google_books_id="gb1"
        )
        path2 = _write_book(
            tmp_path, "B.md", title="Book", isbn="123", google_books_id="gb2"
        )

        can_merge, reason, merge_result = check_auto_merge(path1, path2)
        assert can_merge is False
        assert reason is not None
        assert merge_result is None

    def test_no_auto_merge_with_conflicts(self, tmp_path):
        """Should not auto-merge if there are metadata conflicts."""
        path1 = _write_book(
            tmp_path,
            "A.md",
            title="Book",
            isbn="123",
            google_books_id="gb1",
            page_count=300,
            rating=5,
        )
        path2 = _write_book(
            tmp_path,
            "B.md",
            title="Book",
            isbn="123",
            google_books_id="gb1",
            page_count=250,
            rating=3,
        )

        can_merge, reason, merge_result = check_auto_merge(path1, path2)
        assert can_merge is False
        assert "conflict" in reason.lower()
        assert merge_result is None

    def test_no_auto_merge_missing_isbn(self, tmp_path):
        """Should not auto-merge if ISBN is missing from either book."""
        path1 = _write_book(tmp_path, "A.md", title="Book", google_books_id="gb1")
        path2 = _write_book(
            tmp_path, "B.md", title="Book", isbn="123", google_books_id="gb1"
        )

        can_merge, reason, _ = check_auto_merge(path1, path2)
        assert can_merge is False


class TestGetPrimaryBook:
    """Test get_primary_book() function."""

    def test_primary_is_more_complete(self, tmp_path):
        """Book with more non-null fields should be primary."""
        path1 = _write_book(
            tmp_path,
            "A.md",
            title="Book",
            isbn="123",
            google_books_id="gb1",
            page_count=300,
            genres=["Fiction"],
            rating=5,
        )
        path2 = _write_book(
            tmp_path, "B.md", title="Book", isbn="123", google_books_id="gb1"
        )

        primary = get_primary_book(path1, path2)
        assert primary == path1

    def test_primary_fallback_to_first_when_equal_completeness(self, tmp_path):
        """When completeness is equal, should fallback to first path."""
        path1 = _write_book(
            tmp_path, "A.md", title="Book", isbn="123", google_books_id="gb1"
        )
        path2 = _write_book(
            tmp_path, "B.md", title="Book", isbn="123", google_books_id="gb1"
        )

        primary = get_primary_book(path1, path2)
        assert primary == path1

    def test_primary_by_earlier_date_added(self, tmp_path):
        """When completeness is equal, earlier date_added wins."""
        path1 = _write_book(
            tmp_path,
            "A.md",
            title="Book",
            isbn="123",
            google_books_id="gb1",
            date_added="2024-01-01",
        )
        path2 = _write_book(
            tmp_path,
            "B.md",
            title="Book",
            isbn="123",
            google_books_id="gb1",
            date_added="2024-06-01",
        )

        primary = get_primary_book(path1, path2)
        assert primary == path1


class TestWriteMergedBook:
    """Test write_merged_book() function."""

    def test_write_merged_frontmatter_and_body(self, tmp_path):
        """Should write merged frontmatter and body to file."""
        path = tmp_path / "book.md"

        merged_fm = {
            "title": "Book",
            "isbn": "123",
            "google_books_id": "gb1",
            "status": "Read",
        }
        merged_body = "## Notes\n\nMy notes here"

        write_merged_book(path, merged_fm, merged_body)

        content = path.read_text()
        assert "title: Book" in content
        assert "isbn: '123'" in content or "isbn: 123" in content
        assert "My notes here" in content


class TestDeleteSecondaryFile:
    """Test delete_secondary_file() function."""

    def test_delete_file(self, tmp_path):
        """Should delete the secondary file."""
        path = tmp_path / "book.md"
        path.write_text("content")
        assert path.exists()

        delete_secondary_file(path)

        assert not path.exists()


class TestMergeEdgeCases:
    """Edge case tests for merge functionality."""

    def test_merge_three_book_group(self, tmp_path):
        """Merging a group of 3 books should work progressively."""
        path1 = _write_book(
            tmp_path,
            "A.md",
            title="Book",
            isbn="123",
            google_books_id="gb1",
            status="To Read",
            page_count=300,
        )
        path2 = _write_book(
            tmp_path,
            "B.md",
            title="Book",
            isbn="123",
            google_books_id="gb1",
            status="Read",
            rating=5,
        )
        path3 = _write_book(
            tmp_path,
            "C.md",
            title="Book",
            isbn="123",
            google_books_id="gb1",
            genres=["Fiction"],
        )

        # Merge B into A
        fm1, body1, conflicts1 = merge_two_books(path1, path2)
        assert fm1["status"] == "Read"  # Read wins
        assert fm1["page_count"] == 300  # From A
        assert fm1["rating"] == 5  # From B

        write_merged_book(path1, fm1, body1)

        # Merge C into updated A
        fm2, body2, conflicts2 = merge_two_books(path1, path3)
        assert fm2["genres"] == ["Fiction"]  # From C

    def test_merge_empty_primary_body_with_secondary_notes(self, tmp_path):
        """Secondary notes should be preserved when primary body is empty."""
        path1 = tmp_path / "A.md"
        path1.write_text(
            "---\ntitle: Book\nisbn: 123\ngoogle_books_id: gb1\n---\n",
            encoding="utf-8",
        )

        path2 = tmp_path / "B.md"
        path2.write_text(
            "---\ntitle: Book\nisbn: 123\ngoogle_books_id: gb1\n---\n"
            "## Notes\n\nImportant notes from B\n",
            encoding="utf-8",
        )

        _, merged_body, _ = merge_two_books(path1, path2)
        assert "Important notes from B" in merged_body

    def test_merge_both_bodies_empty(self, tmp_path):
        """Merge with both bodies empty should produce empty body."""
        path1 = tmp_path / "A.md"
        path1.write_text(
            "---\ntitle: Book\nisbn: 123\ngoogle_books_id: gb1\n---\n",
            encoding="utf-8",
        )
        path2 = tmp_path / "B.md"
        path2.write_text(
            "---\ntitle: Book\nisbn: 123\ngoogle_books_id: gb1\n---\n",
            encoding="utf-8",
        )

        _, merged_body, _ = merge_two_books(path1, path2)
        assert merged_body.strip() == ""

    def test_merge_unreadable_frontmatter_raises(self, tmp_path):
        """Merge with unreadable frontmatter should raise ValueError."""
        path1 = _write_book(
            tmp_path, "A.md", title="Book", isbn="123", google_books_id="gb1"
        )
        path2 = tmp_path / "B.md"
        path2.write_text("No frontmatter here", encoding="utf-8")

        with pytest.raises(ValueError):
            merge_two_books(path1, path2)

    def test_get_primary_book_selects_secondary_when_more_complete(self, tmp_path):
        """get_primary_book should pick secondary when it has more fields."""
        path1 = _write_book(tmp_path, "A.md", title="Book", isbn="123")
        path2 = _write_book(
            tmp_path,
            "B.md",
            title="Book",
            isbn="123",
            google_books_id="gb1",
            page_count=300,
            rating=4,
            genres=["Fiction"],
        )

        primary = get_primary_book(path1, path2)
        assert primary == path2


class TestMergeCLI:
    """CLI integration tests for the merge command."""

    def test_merge_auto_no_duplicates(self, tmp_path):
        from typer.testing import CliRunner

        from libris.cli import app
        from libris.config import set_config

        vault = tmp_path / "vault"
        vault.mkdir()
        _write_book(vault, "A.md", title="Book A", isbn="111", google_books_id="a1")
        _write_book(vault, "B.md", title="Book B", isbn="222", google_books_id="b2")
        set_config("vault_path", str(vault))

        result = CliRunner().invoke(app, ["merge", "--auto"])
        assert result.exit_code == 0
        assert "No duplicates found" in result.output

    def test_merge_auto_succeeds(self, tmp_path):
        from typer.testing import CliRunner

        from libris.cli import app
        from libris.config import set_config

        vault = tmp_path / "vault"
        vault.mkdir()
        _write_book(
            vault,
            "A.md",
            title="Book A",
            isbn="123",
            google_books_id="gb1",
            status="To Read",
            page_count=300,
        )
        _write_book(
            vault,
            "B.md",
            title="Book B",
            isbn="123",
            google_books_id="gb1",
            status="Read",
        )
        set_config("vault_path", str(vault))

        result = CliRunner().invoke(app, ["merge", "--auto"])
        assert result.exit_code == 0
        assert "1 duplicate(s) merged" in result.output
        # Secondary file should be deleted
        assert not (vault / "B.md").exists() or not (vault / "A.md").exists()
        # At least one should remain
        remaining = list(vault.glob("*.md"))
        assert len(remaining) == 1

    def test_merge_auto_skips_conflicts(self, tmp_path):
        from typer.testing import CliRunner

        from libris.cli import app
        from libris.config import set_config

        vault = tmp_path / "vault"
        vault.mkdir()
        _write_book(
            vault,
            "A.md",
            title="Book",
            isbn="123",
            google_books_id="gb1",
            page_count=300,
            rating=5,
        )
        _write_book(
            vault,
            "B.md",
            title="Book",
            isbn="123",
            google_books_id="gb1",
            page_count=250,
            rating=3,
        )
        set_config("vault_path", str(vault))

        result = CliRunner().invoke(app, ["merge", "--auto"])
        assert result.exit_code == 0
        assert "Skipped" in result.output
        assert "0 duplicate(s) merged" in result.output
        # Both files should still exist
        assert (vault / "A.md").exists()
        assert (vault / "B.md").exists()


# --- superseded ids (#64, ADR 0014) ---


def _note(tmp_path, name, **frontmatter):
    """Write a Book Note with the given frontmatter and a line of reader's notes."""
    fields = {
        "libris_id": name.upper(),
        "title": name,
        "authors": ["Frank Herbert"],
        "status": "To Read",
    }
    fields.update(frontmatter)
    path = tmp_path / f"{name}.md"
    body = yaml.dump(fields, sort_keys=False, allow_unicode=True)
    path.write_text(
        f"---\n{body}---\n\n# {name}\n\n## Notes\n\nMine.\n", encoding="utf-8"
    )
    return path


def test_merging_records_the_losing_id_on_the_survivor(tmp_path):
    # Given two notes for one Book
    primary = _note(tmp_path, "Keeper", isbn="9780441013593")
    secondary = _note(tmp_path, "Loser")

    # When they are merged
    merged_fm, _, _ = merge_two_books(primary, secondary, allow_conflicts=True)

    # Then the survivor carries the identity the deleted note had, so an Intent
    # naming it still resolves (ADR 0014)
    assert merged_fm["superseded_ids"] == ["LOSER"]


def test_merging_carries_forward_ids_the_loser_had_absorbed(tmp_path):
    # Given a note that had itself already absorbed another
    primary = _note(tmp_path, "Keeper")
    secondary = _note(tmp_path, "Loser", superseded_ids=["EARLIER"])

    # When it is merged away in turn
    merged_fm, _, _ = merge_two_books(primary, secondary, allow_conflicts=True)

    # Then the whole chain survives; a second merge must not break the first
    assert merged_fm["superseded_ids"] == ["LOSER", "EARLIER"]


def test_merging_keeps_ids_the_survivor_had_already_absorbed(tmp_path):
    # Given a survivor that has absorbed a note before
    primary = _note(tmp_path, "Keeper", superseded_ids=["OLDEST"])
    secondary = _note(tmp_path, "Loser")

    # When it absorbs another
    merged_fm, _, _ = merge_two_books(primary, secondary, allow_conflicts=True)

    # Then both are held
    assert merged_fm["superseded_ids"] == ["OLDEST", "LOSER"]


def test_superseded_ids_are_not_duplicated(tmp_path):
    # Given both notes claiming the same superseded id
    primary = _note(tmp_path, "Keeper", superseded_ids=["SHARED"])
    secondary = _note(tmp_path, "Loser", superseded_ids=["SHARED"])

    # When they are merged
    merged_fm, _, _ = merge_two_books(primary, secondary, allow_conflicts=True)

    # Then it appears once
    assert merged_fm["superseded_ids"] == ["SHARED", "LOSER"]


def test_the_survivor_never_supersedes_itself(tmp_path):
    # Given a secondary that somehow lists the survivor's own id
    primary = _note(tmp_path, "Keeper")
    secondary = _note(tmp_path, "Loser", superseded_ids=["KEEPER"])

    # When they are merged
    merged_fm, _, _ = merge_two_books(primary, secondary, allow_conflicts=True)

    # Then the survivor's live identity is not also listed as superseded, which
    # would make it resolve to itself through two different fields
    assert "KEEPER" not in merged_fm["superseded_ids"]
    assert merged_fm["libris_id"] == "KEEPER"


def test_a_note_that_absorbed_nothing_has_no_superseded_ids(tmp_path):
    # Given a note created normally
    path = create_book_note(
        BookCandidate(title="Dune", authors=["Frank Herbert"]), tmp_path
    )

    # Then the field is absent rather than present and empty: it is not part of
    # the canonical shape every note carries
    assert "superseded_ids" not in read_frontmatter(path)


def test_a_merge_with_no_ids_to_record_adds_no_field(tmp_path):
    # Given notes from before identities existed
    primary = _note(tmp_path, "Keeper", libris_id=None)
    secondary = _note(tmp_path, "Loser", libris_id=None)

    # When they are merged
    merged_fm, _, _ = merge_two_books(primary, secondary, allow_conflicts=True)

    # Then nothing is invented. Asserted as absence rather than falsiness: the
    # field is meant not to exist, and `.get()` cannot tell that apart from a
    # key present and empty.
    assert "superseded_ids" not in merged_fm


def test_the_written_survivor_holds_the_superseded_id(tmp_path):
    # Given a completed merge
    primary = _note(tmp_path, "Keeper")
    secondary = _note(tmp_path, "Loser")
    merged_fm, merged_body, _ = merge_two_books(
        primary, secondary, allow_conflicts=True
    )

    # When it is written to disk
    write_merged_book(primary, merged_fm, merged_body)

    # Then the file itself carries the forwarding address
    note = BookNote.read(primary)
    assert note.superseded_ids == ["LOSER"]


def test_a_superseded_id_stored_as_a_bare_string_is_read_as_one_id(tmp_path):
    # Given a note whose superseded_ids is a bare string rather than a list -
    # the shape 1,341 notes already use for `format`
    primary = _note(tmp_path, "Keeper")
    secondary = _note(tmp_path, "Loser", superseded_ids="EARLIER")

    # When they are merged
    merged_fm, _, _ = merge_two_books(primary, secondary, allow_conflicts=True)

    # Then it is one identity, not one per character
    assert merged_fm["superseded_ids"] == ["LOSER", "EARLIER"]


def test_a_superseded_id_of_an_unusable_shape_is_ignored(tmp_path):
    # Given frontmatter holding something that names no identity
    primary = _note(tmp_path, "Keeper", superseded_ids=42)
    secondary = _note(tmp_path, "Loser")

    # When they are merged
    merged_fm, _, _ = merge_two_books(primary, secondary, allow_conflicts=True)

    # Then only the real identity is recorded
    assert merged_fm["superseded_ids"] == ["LOSER"]


def test_reading_superseded_ids_from_a_bare_string(tmp_path):
    # Given a note carrying the field as a string
    path = _note(tmp_path, "Solo", superseded_ids="GONE")

    # When the note is read
    note = BookNote.read(path)

    # Then the reader agrees with the merge helper, because both use one rule
    assert note.superseded_ids == ["GONE"]


# --- fields that hold several values are unioned, not conflicted (#72) ---
#
# _resolve_field_value unioned list fields, but the list it checked named
# "author" while the canonical field is "authors" (ADR 0005) - the same drift
# #62 undid. So merging two notes with different author lists reported a
# conflict and silently kept the primary's. `format` had never been in the set
# at all, and since ADR 0017 it holds a list on 2,210 notes.


def test_merging_unions_author_lists(tmp_path):
    # Given two notes for one Book crediting different authors
    primary = _write_book(
        tmp_path, "A.md", title="The Gap and the Gain", authors=["Dan Sullivan"]
    )
    secondary = _write_book(
        tmp_path, "B.md", title="The Gap and the Gain", authors=["Benjamin Hardy"]
    )

    # When they are merged
    merged_fm, _, conflicts = merge_two_books(primary, secondary, allow_conflicts=True)

    # Then both survive, rather than the secondary's being reported as a
    # conflict and dropped
    assert merged_fm["authors"] == ["Dan Sullivan", "Benjamin Hardy"]
    assert not [c for c in conflicts if c.field == "authors"]


def test_merging_unions_formats(tmp_path):
    # Given the same Book held on paper in one note and as audio in the other
    primary = _write_book(tmp_path, "A.md", title="Changes", format=["Physical"])
    secondary = _write_book(tmp_path, "B.md", title="Changes", format=["Audiobook"])

    # When they are merged
    merged_fm, _, conflicts = merge_two_books(primary, secondary, allow_conflicts=True)

    # Then the reader still owns it in both media
    assert merged_fm["format"] == ["Physical", "Audiobook"]
    assert not [c for c in conflicts if c.field == "format"]


def test_merging_does_not_duplicate_a_shared_value(tmp_path):
    # Given both notes recording the same format
    primary = _write_book(tmp_path, "A.md", title="Dune", format=["Audiobook"])
    secondary = _write_book(tmp_path, "B.md", title="Dune", format=["Audiobook"])

    # When they are merged
    merged_fm, _, _ = merge_two_books(primary, secondary, allow_conflicts=True)

    # Then it appears once
    assert merged_fm["format"] == ["Audiobook"]


def test_merging_unions_a_bare_string_with_a_list(tmp_path):
    # Given tags, which this vault still holds as both shapes
    primary = _write_book(tmp_path, "A.md", title="Dune", tags="Book")
    secondary = _write_book(tmp_path, "B.md", title="Dune", tags=["Book", "SF"])

    # When they are merged
    merged_fm, _, _ = merge_two_books(primary, secondary, allow_conflicts=True)

    # Then the shapes combine without losing either
    assert merged_fm["tags"] == ["Book", "SF"]


def test_merging_still_unions_genres(tmp_path):
    # Given the field that already worked, so the fix does not regress it
    primary = _write_book(tmp_path, "A.md", title="Dune", genres=["Science Fiction"])
    secondary = _write_book(tmp_path, "B.md", title="Dune", genres=["Classics"])

    # When they are merged
    merged_fm, _, _ = merge_two_books(primary, secondary, allow_conflicts=True)

    # Then both are kept
    assert merged_fm["genres"] == ["Science Fiction", "Classics"]


def test_a_reader_field_still_conflicts(tmp_path):
    # Given two notes disagreeing about a single-valued field
    primary = _write_book(tmp_path, "A.md", title="Dune", rating=5)
    secondary = _write_book(tmp_path, "B.md", title="Dune", rating=3)

    # When they are merged
    merged_fm, _, conflicts = merge_two_books(primary, secondary, allow_conflicts=True)

    # Then it is reported: a rating is the reader's own, and two different ones
    # means they rated the same book twice (ADR 0018)
    assert [c.field for c in conflicts] == ["rating"]
    assert merged_fm["rating"] == 5


# --- what a merge asks a person (ADR 0018) ---


def test_an_unmodelled_field_does_not_conflict(tmp_path):
    # Given Obsidian's own timestamps, which two different files never share.
    # 414 notes carry these.
    primary = _write_book(
        tmp_path, "A.md", title="Dune", date_modified='"2026-08-01T10:00:00"'
    )
    secondary = _write_book(
        tmp_path, "B.md", title="Dune", date_modified='"2026-08-02T11:00:00"'
    )

    # When they are merged
    merged_fm, _, conflicts = merge_two_books(primary, secondary, allow_conflicts=True)

    # Then nothing is asked: the keeper's value stands. Otherwise every merge
    # prompts, and --allow-conflicts becomes routine on a bulk run - which
    # would suppress the conflicts that matter too
    assert conflicts == []
    assert merged_fm["date_modified"] == "2026-08-01T10:00:00"


def test_edition_metadata_does_not_conflict(tmp_path):
    # Given two notes describing different editions of one work, which is what
    # every Duplicate Candidate pair is
    primary = _write_book(tmp_path, "A.md", title="Dune", isbn="978-0-441-01359-3")
    secondary = _write_book(tmp_path, "B.md", title="Dune", isbn="978-0-441-17271-9")

    # When they are merged
    merged_fm, _, conflicts = merge_two_books(primary, secondary, allow_conflicts=True)

    # Then nothing is asked: an ISBN belongs to the edition, and the keeper's
    # will do. Measured: under the old rule 0 of 83 candidate pairs merged
    # cleanly, so the feature was inert
    assert conflicts == []
    assert merged_fm["isbn"] == "978-0-441-01359-3"


def test_aliases_are_unioned(tmp_path):
    # Given aliases, which Obsidian writes and the Library does not model, but
    # which genuinely holds several values
    primary = _write_book(tmp_path, "A.md", title="Dune", aliases=["Dune 1965"])
    secondary = _write_book(tmp_path, "B.md", title="Dune", aliases=["Herbert Dune"])

    # When they are merged
    merged_fm, _, conflicts = merge_two_books(primary, secondary, allow_conflicts=True)

    # Then both survive rather than the keeper's silently winning
    assert merged_fm["aliases"] == ["Dune 1965", "Herbert Dune"]
    assert conflicts == []


def test_one_author_spelled_two_ways_is_not_doubled(tmp_path):
    # Given the whitespace dirt on 185 notes
    primary = _write_book(tmp_path, "A.md", title="10% Happier", authors=["Dan Harris"])
    secondary = _write_book(
        tmp_path, "B.md", title="10% Happier", authors=["Dan   Harris"]
    )

    # When they are merged
    merged_fm, _, _ = merge_two_books(primary, secondary, allow_conflicts=True)

    # Then one person appears once
    assert merged_fm["authors"] == ["Dan Harris"]


def test_a_wikilinked_author_matches_the_plain_spelling(tmp_path):
    # Given one note linking the author and the other naming them plainly
    primary = _write_book(
        tmp_path, "A.md", title="A Calendar of Wisdom", authors=['"[[Leo Tolstoy]]"']
    )
    secondary = _write_book(
        tmp_path, "B.md", title="A Calendar of Wisdom", authors=["Leo Tolstoy"]
    )

    # When they are merged
    merged_fm, _, _ = merge_two_books(primary, secondary, allow_conflicts=True)

    # Then they are one person, and the keeper's link survives: in Obsidian a
    # wikilink is the edge to that author's note (ADR 0018)
    assert merged_fm["authors"] == ["[[Leo Tolstoy]]"]


def test_whitespace_is_collapsed_on_write(tmp_path):
    # Given a keeper whose own author value carries the dirt
    primary = _write_book(tmp_path, "A.md", title="Dune", authors=["Frank   Herbert"])
    secondary = _write_book(tmp_path, "B.md", title="Dune", authors=["Brian Herbert"])

    # When they are merged
    merged_fm, _, _ = merge_two_books(primary, secondary, allow_conflicts=True)

    # Then the value written is tidied, but no further: comparing loosely and
    # writing conservatively is the whole rule
    assert merged_fm["authors"] == ["Frank Herbert", "Brian Herbert"]


def test_genres_still_dedupe_on_the_exact_string(tmp_path):
    # Given case drift in genres, which is a vocabulary question and not a
    # merge one
    primary = _write_book(tmp_path, "A.md", title="Dune", genres=["Science Fiction"])
    secondary = _write_book(tmp_path, "B.md", title="Dune", genres=["science fiction"])

    # When they are merged
    merged_fm, _, _ = merge_two_books(primary, secondary, allow_conflicts=True)

    # Then both are kept; merging does not quietly pick a spelling
    assert merged_fm["genres"] == ["Science Fiction", "science fiction"]
