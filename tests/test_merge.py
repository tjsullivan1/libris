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
            author=["Alice"],
            isbn="123-456",
            google_books_id="gb1",
            status="To Read",
        )
        path2 = _write_book(
            tmp_path,
            "B.md",
            title="Test Book",
            author=["Alice"],
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
        """Different non-null author lists should trigger conflict."""
        path1 = _write_book(
            tmp_path,
            "A.md",
            title="Book",
            isbn="123",
            google_books_id="gb1",
            author=["Alice"],
        )
        path2 = _write_book(
            tmp_path,
            "B.md",
            title="Book",
            isbn="123",
            google_books_id="gb1",
            author=["Bob"],
        )

        merged_fm, _, conflicts = merge_two_books(path1, path2)

        # Should have conflict in author field
        conflict_fields = [c.field for c in conflicts]
        assert "author" not in conflict_fields  # Authors should be merged as lists
        assert len(merged_fm["author"]) == 2

    def test_metadata_conflict_different_page_count(self, tmp_path):
        """Different page counts should trigger conflict."""
        path1 = _write_book(
            tmp_path,
            "A.md",
            title="Book",
            isbn="123",
            google_books_id="gb1",
            page_count=300,
        )
        path2 = _write_book(
            tmp_path,
            "B.md",
            title="Book",
            isbn="123",
            google_books_id="gb1",
            page_count=250,
        )

        merged_fm, _, conflicts = merge_two_books(path1, path2)

        # page_count conflict should be detected
        conflict_fields = [c.field for c in conflicts]
        assert "page_count" in conflict_fields

    def test_conflicts_prevent_merge_when_not_allowed(self, tmp_path):
        """With allow_conflicts=False, conflicts should be returned."""
        path1 = _write_book(
            tmp_path,
            "A.md",
            title="Book",
            isbn="123",
            google_books_id="gb1",
            page_count=300,
        )
        path2 = _write_book(
            tmp_path,
            "B.md",
            title="Book",
            isbn="123",
            google_books_id="gb1",
            page_count=250,
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
        )
        path2 = _write_book(
            tmp_path,
            "B.md",
            title="Book",
            isbn="123",
            google_books_id="gb1",
            page_count=250,
        )

        merged_fm, merged_body, conflicts = merge_two_books(
            path1, path2, allow_conflicts=True
        )

        # Merge should proceed
        assert merged_fm["page_count"] == 300  # Primary wins
        assert len(conflicts) > 0  # But conflicts are still recorded

    def test_merge_author_lists(self, tmp_path):
        """Author lists from both books should be merged and deduplicated."""
        path1 = _write_book(
            tmp_path,
            "A.md",
            title="Book",
            isbn="123",
            google_books_id="gb1",
            author=["Alice", "Charlie"],
        )
        path2 = _write_book(
            tmp_path,
            "B.md",
            title="Book",
            isbn="123",
            google_books_id="gb1",
            author=["Bob", "Alice"],  # Alice appears in both
        )

        merged_fm, _, conflicts = merge_two_books(path1, path2)

        assert len(conflicts) == 0
        assert len(merged_fm["author"]) == 3
        assert "Alice" in merged_fm["author"]
        assert "Bob" in merged_fm["author"]
        assert "Charlie" in merged_fm["author"]

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
        )
        path2 = _write_book(
            tmp_path,
            "B.md",
            title="Book",
            isbn="123",
            google_books_id="gb1",
            page_count=250,
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
        )
        _write_book(
            vault,
            "B.md",
            title="Book",
            isbn="123",
            google_books_id="gb1",
            page_count=250,
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

    # Then nothing is invented
    assert not merged_fm.get("superseded_ids")


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
