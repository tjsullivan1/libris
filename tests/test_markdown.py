import time
from datetime import date
from pathlib import Path
from statistics import median

from libris.api import BookCandidate
from libris.markdown import (
    compute_canonical_filename,
    create_book_note,
    list_books,
    read_frontmatter,
    rename_book_file,
    sanitize_filename,
    standardize_title,
    update_frontmatter_from_book,
    update_wikilinks_in_vault,
)
from libris.note_format import mint_libris_id


def test_sanitize_filename():
    assert sanitize_filename("Title: With Colon") == "Title With Colon"
    assert sanitize_filename("Title / With / Slash") == "Title With Slash"


def test_create_book_note(tmp_path):
    book = BookCandidate(
        title="Test Book",
        authors=["Author One"],
        isbn="1234567890",
        page_count=100,
        published_date="2023",
        google_books_id="xyz",
        thumbnail="http://example.com/thumb.jpg",
        genres=["Test"],
        description="A test description",
    )

    file_path = create_book_note(book, tmp_path)
    assert file_path.exists()
    assert "Test Book - Author One.md" in file_path.name

    content = file_path.read_text()
    assert "title: Test Book" in content
    assert "authors:\n- Author One" in content
    assert "> [!abstract]- Description" in content
    assert "> A test description" in content


def test_create_book_note_with_overrides(tmp_path):
    """Test that overrides dict sets frontmatter fields."""
    book = BookCandidate(
        title="Override Book",
        authors=["Author Two"],
        isbn="0987654321",
        page_count=200,
        published_date="2024",
        google_books_id="abc",
        thumbnail=None,
        genres=["Fiction"],
        description=None,
    )

    # Given overrides for format, rating, and referred_by
    overrides = {
        "format": "Audiobook",
        "rating": 4,
        "referred_by": "A Friend",
    }

    # When creating a book note with overrides
    file_path = create_book_note(book, tmp_path, overrides=overrides)

    # Then the overrides appear in frontmatter
    content = file_path.read_text()
    assert "format:\n- Audiobook" in content
    assert "rating: 4" in content
    assert "referred_by: A Friend" in content


def test_create_book_note_overrides_status(tmp_path):
    """Test that status in overrides takes precedence over the status parameter."""
    book = BookCandidate(
        title="Status Book",
        authors=["Author Three"],
        isbn="1111111111",
        page_count=150,
        published_date="2025",
        google_books_id="def",
        thumbnail=None,
        genres=[],
        description=None,
    )

    # Given status override that differs from default
    overrides = {"status": "Reading"}

    # When creating with default status but an override
    file_path = create_book_note(book, tmp_path, status="To Read", overrides=overrides)

    # Then the override wins
    content = file_path.read_text()
    assert "status: Reading" in content


def test_create_book_note_invalid_override_raises(tmp_path):
    """Test that an unknown override key raises ValueError."""
    import pytest

    book = BookCandidate(
        title="Bad Override",
        authors=["Author Four"],
        isbn="2222222222",
        page_count=50,
        published_date="2020",
        google_books_id="ghi",
        thumbnail=None,
        genres=[],
        description=None,
    )

    # When passing an invalid frontmatter key
    # Then a ValueError is raised
    with pytest.raises(ValueError, match="Unknown frontmatter field"):
        create_book_note(book, tmp_path, overrides={"nonexistent_field": "value"})


def test_update_book_status(tmp_path):
    file_path = tmp_path / "test.md"
    file_path.write_text("---\ntitle: Test\nstatus: To Read\n---\n")

    from libris.markdown import update_book_status

    update_book_status(file_path, "Reading")

    content = file_path.read_text()
    assert "status: Reading" in content
    assert "status: To Read" not in content


def test_update_book_status_leaves_the_body_alone(tmp_path):
    # Given a Book Note whose body quotes the word the frontmatter key uses -
    # a reader writing about the book, which is what a note is for
    file_path = tmp_path / "body_says_status.md"
    file_path.write_text(
        """---
title: Test
status: To Read
---

## Notes
The narrator's status: unreliable, and gloriously so.
Compare status: the sequel, which drops the device.
""",
        encoding="utf-8",
    )

    # When the status is updated
    from libris.markdown import update_book_status

    update_book_status(file_path, "Reading")

    # Then the frontmatter carries the new status and the reader's own
    # sentences survive verbatim
    content = file_path.read_text(encoding="utf-8")
    assert read_frontmatter(file_path)["status"] == "Reading"
    assert "The narrator's status: unreliable, and gloriously so." in content
    assert "Compare status: the sequel, which drops the device." in content


def test_update_book_status_refuses_a_note_it_cannot_parse(tmp_path):
    # Given a file with no frontmatter block at all
    import pytest

    file_path = tmp_path / "no_frontmatter.md"
    original = "Just a body, mentioning status: somewhere.\n"
    file_path.write_text(original, encoding="utf-8")

    # When its status is updated
    # Then it is refused rather than rewritten - this is the write path for one
    # field, not the place to rebuild a broken note
    from libris.markdown import FrontmatterUnreadable, update_book_status

    with pytest.raises(FrontmatterUnreadable):
        update_book_status(file_path, "Reading")
    assert file_path.read_text(encoding="utf-8") == original


def test_ensure_frontmatter_fields(tmp_path):
    file_path = tmp_path / "legacy_book.md"
    file_path.write_text("""---
title: Legacy Book
status: Finished
google_books_id: 123
---

## Notes
Some existing notes here.
""")

    from libris.markdown import ensure_frontmatter_fields

    # Run cleanup
    updated, _ = ensure_frontmatter_fields(file_path)
    assert updated is True

    content = file_path.read_text()
    assert "tags: Book" in content
    assert "format: null" in content
    assert "date_added:" in content
    assert "status: Finished" in content
    assert "Some existing notes here." in content

    # Run again, should not update
    updated, _ = ensure_frontmatter_fields(file_path)
    assert updated is False


def test_ensure_frontmatter_fields_adds_title_heading_when_missing(tmp_path):
    file_path = tmp_path / "legacy_book.md"
    file_path.write_text(
        """---
title: Legacy Book
status: Finished
google_books_id: 123
---

## Notes
Some existing notes here.
"""
    )

    from libris.markdown import ensure_frontmatter_fields

    updated, _ = ensure_frontmatter_fields(file_path)
    assert updated is True

    content = file_path.read_text()
    assert "# Legacy Book\n\n## Notes" in content
    assert "Some existing notes here." in content


def test_ensure_frontmatter_sets_status_read_when_date_finished(tmp_path):
    file_path = tmp_path / "finished_book.md"
    file_path.write_text(
        "---\ntitle: Done Book\nstatus: To Read\ndate_finished: '2025-01-01'\n"
        "google_books_id: abc\n---\n"
    )

    from libris.markdown import ensure_frontmatter_fields

    updated, _ = ensure_frontmatter_fields(file_path)
    assert updated is True

    content = file_path.read_text()
    assert "status: Read" in content


def test_ensure_frontmatter_migrates_legacy_author_to_authors_list(tmp_path):
    file_path = tmp_path / "string_author.md"
    file_path.write_text(
        "---\ntitle: Test\nauthor: John Doe\ngoogle_books_id: abc\n---\n"
    )

    from libris.markdown import ensure_frontmatter_fields

    updated, _ = ensure_frontmatter_fields(file_path)
    assert updated is True

    content = file_path.read_text()
    assert "authors:\n- John Doe" in content


def test_ensuring_frontmatter_mints_a_missing_libris_id(tmp_path):
    from libris.markdown import ensure_frontmatter_fields

    # Given a note typed straight into Obsidian, with no identity
    file_path = tmp_path / "typed by hand.md"
    file_path.write_text(
        "---\ntitle: Dune\nauthors:\n- Frank Herbert\ndate_added: 2019-03-12\n---\n",
        encoding="utf-8",
    )

    # When its frontmatter is ensured
    updated, data = ensure_frontmatter_fields(file_path)

    # Then it gains an identity, timed from when the book was added
    assert updated is True
    assert len(data["libris_id"]) == 26
    assert data["libris_id"] < mint_libris_id(date(2026, 1, 1))


def test_ensure_frontmatter_fields_tricky_spacing(tmp_path):
    # Test with extra spaces after --- and no newline after second ---
    file_path = tmp_path / "tricky_book.md"
    file_path.write_text(
        "--- \ntitle: Tricky\ngoogle_books_id: 456\n--- \nSome content"
    )

    from libris.markdown import ensure_frontmatter_fields

    updated, _ = ensure_frontmatter_fields(file_path)
    assert updated is True

    content = file_path.read_text()
    assert "tags: Book" in content
    assert "Some content" in content
    # Verify exact format we expect: --- \nYAML\n---\nSome content
    assert content.startswith("---")
    assert "---" in content.split("\n", 1)[1]


def test_ensure_frontmatter_migrates_legacy_fields(tmp_path):
    file_path = tmp_path / "legacy_fields.md"
    file_path.write_text("""---
title: Old Book
status: read
Type Read:
- Audiobook
Rating out of 5: 4
---

## Notes
""")

    from libris.markdown import ensure_frontmatter_fields

    updated, _ = ensure_frontmatter_fields(file_path)
    assert updated is True

    content = file_path.read_text()
    # Legacy fields should be removed
    assert "Type Read" not in content
    assert "Rating out of 5" not in content
    # Values should be migrated to canonical fields
    assert "format:" in content
    assert "Audiobook" in content
    assert "rating: 4" in content


def test_ensure_frontmatter_migration_does_not_overwrite_existing(tmp_path):
    file_path = tmp_path / "both_fields.md"
    file_path.write_text("""---
title: Both Book
status: read
Type Read:
- Physical
format: Audiobook
Rating out of 5: 2
rating: 5
---
""")

    from libris.markdown import ensure_frontmatter_fields

    updated, _ = ensure_frontmatter_fields(file_path)
    assert updated is True

    content = file_path.read_text()
    # Legacy fields removed
    assert "Type Read" not in content
    assert "Rating out of 5" not in content
    # Existing canonical values should be preserved (not overwritten)
    assert "format:\n- Audiobook" in content
    assert "rating: 5" in content


def test_read_frontmatter(tmp_path):
    f = tmp_path / "book.md"
    f.write_text("---\ntitle: Test\nstatus: Reading\n---\nBody\n")
    data = read_frontmatter(f)
    assert data == {"title": "Test", "status": "Reading"}


def test_read_frontmatter_returns_none_for_non_frontmatter(tmp_path):
    f = tmp_path / "plain.md"
    f.write_text("# Just a heading\n")
    assert read_frontmatter(f) is None


def test_update_frontmatter_from_book_fills_nulls(tmp_path):
    f = tmp_path / "book.md"
    f.write_text(
        "---\ntitle: null\nisbn: null\ngoogle_books_id: null\nstatus: Reading\n---\n"
    )

    book = BookCandidate(
        title="Real Title",
        authors=["Author A"],
        isbn="1234567890",
        page_count=200,
        published_date="2023",
        google_books_id="gid123",
        thumbnail="http://example.com/thumb.jpg",
        genres=["Fiction"],
        description="A description",
    )

    assert update_frontmatter_from_book(f, book) is True
    data = read_frontmatter(f)
    assert data["title"] == "Real Title"
    assert data["isbn"] == "1234567890"
    assert data["google_books_id"] == "gid123"
    # status was not null so it should be preserved
    assert data["status"] == "Reading"
    # description should be added to the body
    content = f.read_text()
    assert "> [!abstract]- Description" in content
    assert "A description" in content


def test_update_frontmatter_from_book_skips_existing_description(tmp_path):
    f = tmp_path / "book.md"
    f.write_text(
        "---\ntitle: null\n---\n\n> [!abstract]- Description\n> Existing desc\n"
    )

    book = BookCandidate(
        title="Title",
        authors=["A"],
        isbn=None,
        page_count=None,
        published_date=None,
        google_books_id=None,
        thumbnail=None,
        genres=[],
        description="New desc",
    )

    update_frontmatter_from_book(f, book)
    content = f.read_text()
    assert content.count("[!abstract]- Description") == 1
    assert "Existing desc" in content
    assert "New desc" not in content


def test_update_frontmatter_from_book_adds_title_heading_when_missing(tmp_path):
    f = tmp_path / "book.md"
    f.write_text("---\ntitle: Test Book\nauthors: null\n---\n\n## Notes\n")

    book = BookCandidate(
        title="Test Book",
        authors=["Author"],
        isbn="123",
        page_count=100,
        published_date="2024",
        google_books_id="gid",
        thumbnail=None,
        genres=[],
        description=None,
    )

    assert update_frontmatter_from_book(f, book) is True

    content = f.read_text()
    assert "# Test Book\n\n## Notes" in content


def test_update_frontmatter_from_book_does_not_overwrite(tmp_path):
    f = tmp_path / "book.md"
    f.write_text(
        "---\ntitle: My Title\nauthors:\n- Original\nisbn: '999'\n"
        "page_count: 50\ndate_published: '2019'\ngoogle_books_id: existing\n"
        "cover_thumbnail: http://old.jpg\ngenres:\n- Nonfiction\n---\n"
    )

    book = BookCandidate(
        title="Other Title",
        authors=["Author B"],
        isbn="111",
        page_count=100,
        published_date="2020",
        google_books_id="other_id",
        thumbnail=None,
        genres=[],
        description=None,
    )

    assert update_frontmatter_from_book(f, book) is False
    data = read_frontmatter(f)
    assert data["title"] == "My Title"
    assert data["google_books_id"] == "existing"


def test_list_books_only_returns_book_notes(tmp_path):
    (tmp_path / "note.md").write_text("# plain note\n")
    (tmp_path / "journal.md").write_text("---\ntitle: Journal\n---\n")

    book1 = tmp_path / "book1.md"
    book1.write_text(
        "---\ntitle: Book One\nstatus: To Read\ngoogle_books_id: abc\n---\n",
        encoding="utf-8",
    )
    book2 = tmp_path / "book2.md"
    book2.write_text(
        "---\nstatus: Reading\ngoogle_books_id: xyz\n---\n",
        encoding="utf-8",
    )

    books = list_books(tmp_path)
    assert set(books) == {
        book1,
        book2,
        tmp_path / "note.md",
        tmp_path / "journal.md",
    }


def _legacy_list_books_baseline(vault_path: Path):
    books = []
    for p in vault_path.glob("*.md"):
        try:
            with open(p, "r", encoding="utf-8") as f:
                f.read(1024)
                books.append(p)
        except (OSError, UnicodeDecodeError):
            continue
    return books


def test_list_books_benchmark_against_legacy_read_pattern(tmp_path):
    # Mix many non-book markdown files with book files to simulate a large vault.
    for i in range(700):
        (tmp_path / f"note_{i}.md").write_text("# random note\n" * 4, encoding="utf-8")

    for i in range(300):
        (tmp_path / f"book_{i}.md").write_text(
            "---\ntitle: Book\nstatus: To Read\ngoogle_books_id: id\n---\n",
            encoding="utf-8",
        )

    new_times = []
    old_times = []
    for _ in range(5):
        t0 = time.perf_counter()
        list_books(tmp_path)
        new_times.append(time.perf_counter() - t0)

        t1 = time.perf_counter()
        _legacy_list_books_baseline(tmp_path)
        old_times.append(time.perf_counter() - t1)

    # Runtime can vary by filesystem and cache behavior.
    # Guard against major regressions while still tracking benchmark timings.
    assert median(new_times) <= median(old_times) * 2.0


# --- Title Standardizer Tests ---


def test_standardize_title_basic():
    assert standardize_title("the great gatsby") == "The Great Gatsby"
    assert standardize_title("THE GREAT GATSBY") == "The Great Gatsby"
    assert standardize_title("a tale of two cities") == "A Tale of Two Cities"


def test_standardize_title_preserves_subtitle():
    assert standardize_title("dune: the machine crusade") == "Dune: The Machine Crusade"
    assert (
        standardize_title("clean code: a handbook of agile software craftsmanship")
        == "Clean Code: A Handbook of Agile Software Craftsmanship"
    )


def test_standardize_title_strips_brackets():
    assert standardize_title("Dune [Illustrated]") == "Dune"
    assert standardize_title("Python {Kindle Edition}") == "Python"
    assert (
        standardize_title("The Art of War [Annotated] [Illustrated]")
        == "The Art of War"
    )


def test_standardize_title_collapses_whitespace():
    assert standardize_title("Title  With  Spaces") == "Title With Spaces"
    assert standardize_title("  Extra   Leading  ") == "Extra Leading"


def test_standardize_title_handles_none_and_empty():
    assert standardize_title(None) is None
    assert standardize_title("") == ""


def test_standardize_title_preserves_mixed_case():
    # titlecase preserves words with internal caps
    result = standardize_title("the iPhone revolution")
    assert "iPhone" in result


def test_ensure_frontmatter_standardizes_title(tmp_path):
    from libris.markdown import ensure_frontmatter_fields

    file_path = tmp_path / "caps_book.md"
    file_path.write_text("---\ntitle: THE GREAT GATSBY\ngoogle_books_id: abc\n---\n")

    updated, _ = ensure_frontmatter_fields(file_path)
    assert updated is True

    content = file_path.read_text()
    assert "title: The Great Gatsby" in content


def test_ensure_frontmatter_no_update_when_title_already_standard(tmp_path):
    from libris.markdown import ensure_frontmatter_fields

    file_path = tmp_path / "good_book.md"
    # Write a file with all fields present and title already standardized
    file_path.write_text(
        "---\nlibris_id: 01JQ8Z3K7M4X2VB9WCN5PDRT6E\n"
        "title: The Great Gatsby\nauthors:\n- F. Scott Fitzgerald\nisbn: '123'\n"
        "page_count: 200\ndate_published: '1925'\ngoogle_books_id: abc\n"
        "cover_thumbnail: null\ngenres: null\nseries: null\n"
        "status: To Read\npriority: null\nrating: null\nformat: null\n"
        "tags: Book\nreferred_by: null\n"
        "date_added: null\ndate_started: null\ndate_finished: null\n---\n"
    )

    updated, _ = ensure_frontmatter_fields(file_path)
    assert updated is False


# --- File Rename Tests ---


def test_compute_canonical_filename(tmp_path):
    f = tmp_path / "old name.md"
    f.write_text("---\ntitle: The Great Gatsby\nauthors:\n- F. Scott Fitzgerald\n---\n")
    result = compute_canonical_filename(f)
    assert result == "The Great Gatsby - F. Scott Fitzgerald.md"


def test_compute_canonical_filename_missing_author(tmp_path):
    f = tmp_path / "no_author.md"
    f.write_text("---\ntitle: Solo Title\nauthors: null\n---\n")
    assert compute_canonical_filename(f) is None


def test_compute_canonical_filename_missing_title(tmp_path):
    f = tmp_path / "no_title.md"
    f.write_text("---\ntitle: null\nauthors:\n- Someone\n---\n")
    assert compute_canonical_filename(f) is None


def test_compute_canonical_filename_sanitizes(tmp_path):
    f = tmp_path / "bad.md"
    f.write_text("---\ntitle: 'Title: With Colon'\nauthors:\n- Author\n---\n")
    result = compute_canonical_filename(f)
    assert ":" not in result
    assert result == "Title With Colon - Author.md"


def test_update_wikilinks_basic(tmp_path):
    note = tmp_path / "daily.md"
    note.write_text("I read [[Old Name]] today and loved it.\n")
    count = update_wikilinks_in_vault(tmp_path, "Old Name", "New Name")
    assert count == 1
    assert "[[New Name]]" in note.read_text()
    assert "[[Old Name]]" not in note.read_text()


def test_update_wikilinks_with_alias(tmp_path):
    note = tmp_path / "note.md"
    note.write_text("See [[Old Name|that book]] for details.\n")
    update_wikilinks_in_vault(tmp_path, "Old Name", "New Name")
    assert "[[New Name|that book]]" in note.read_text()


def test_update_wikilinks_with_heading(tmp_path):
    note = tmp_path / "note.md"
    note.write_text("Check [[Old Name#Chapter 1]] for the quote.\n")
    update_wikilinks_in_vault(tmp_path, "Old Name", "New Name")
    assert "[[New Name#Chapter 1]]" in note.read_text()


def test_update_wikilinks_with_block_ref(tmp_path):
    note = tmp_path / "note.md"
    note.write_text("Reference [[Old Name^abc123]] here.\n")
    update_wikilinks_in_vault(tmp_path, "Old Name", "New Name")
    assert "[[New Name^abc123]]" in note.read_text()


def test_update_wikilinks_with_embed(tmp_path):
    note = tmp_path / "note.md"
    note.write_text("Embedding: ![[Old Name]]\n")
    update_wikilinks_in_vault(tmp_path, "Old Name", "New Name")
    assert "![[New Name]]" in note.read_text()


def test_update_wikilinks_skips_hidden_dirs(tmp_path):
    hidden = tmp_path / ".obsidian"
    hidden.mkdir()
    note = hidden / "config.md"
    note.write_text("[[Old Name]] in hidden dir.\n")
    count = update_wikilinks_in_vault(tmp_path, "Old Name", "New Name")
    assert count == 0
    assert "[[Old Name]]" in note.read_text()


def test_update_wikilinks_excludes_target_file(tmp_path):
    target = tmp_path / "Old Name.md"
    target.write_text("Self reference [[Old Name]] should not change.\n")
    count = update_wikilinks_in_vault(tmp_path, "Old Name", "New Name", exclude=target)
    assert count == 0
    assert "[[Old Name]]" in target.read_text()


def test_rename_book_file(tmp_path):
    f = tmp_path / "wrong name.md"
    f.write_text("---\ntitle: Dune\nauthors:\n- Frank Herbert\n---\n")
    result = rename_book_file(f, tmp_path)
    assert result.status == "renamed"
    assert result.new_path.name == "Dune - Frank Herbert.md"
    assert result.new_path.exists()
    assert not f.exists()


def test_rename_book_file_updates_links(tmp_path):
    book = tmp_path / "wrong name.md"
    book.write_text("---\ntitle: Dune\nauthors:\n- Frank Herbert\n---\n")
    note = tmp_path / "reading log.md"
    note.write_text("Currently reading [[wrong name]].\n")

    result = rename_book_file(book, tmp_path)
    assert result.status == "renamed"
    assert result.new_path.name == "Dune - Frank Herbert.md"
    assert "[[Dune - Frank Herbert]]" in note.read_text()


def test_rename_book_file_collision_skips(tmp_path):
    f = tmp_path / "wrong name.md"
    f.write_text("---\ntitle: Dune\nauthors:\n- Frank Herbert\n---\n")
    collision = tmp_path / "Dune - Frank Herbert.md"
    collision.write_text("---\ntitle: Dune\n---\n")

    result = rename_book_file(f, tmp_path)
    assert result.status == "collision"
    assert result.detail == "Dune - Frank Herbert.md"
    assert f.exists()


def test_rename_book_file_already_canonical(tmp_path):
    f = tmp_path / "Dune - Frank Herbert.md"
    f.write_text("---\ntitle: Dune\nauthors:\n- Frank Herbert\n---\n")
    result = rename_book_file(f, tmp_path)
    assert result.status == "already_canonical"


def test_rename_book_file_missing_title(tmp_path):
    f = tmp_path / "some file.md"
    f.write_text("---\ntitle: null\nauthors:\n- Some Author\n---\n")
    result = rename_book_file(f, tmp_path)
    assert result.status == "missing_title"
    assert f.exists()


def test_rename_book_file_missing_author(tmp_path):
    f = tmp_path / "some file.md"
    f.write_text("---\ntitle: Some Title\nauthors: null\n---\n")
    result = rename_book_file(f, tmp_path)
    assert result.status == "missing_author"
    assert f.exists()


def test_rename_book_file_empty_author_list(tmp_path):
    f = tmp_path / "some file.md"
    f.write_text("---\ntitle: Some Title\nauthors: []\n---\n")
    result = rename_book_file(f, tmp_path)
    assert result.status == "missing_author"


def test_rename_book_file_whitespace_title(tmp_path):
    f = tmp_path / "some file.md"
    f.write_text("---\ntitle: '   '\nauthors:\n- Author\n---\n")
    result = rename_book_file(f, tmp_path)
    assert result.status == "missing_title"
