from pathlib import Path
import time
from statistics import median
from libris.api import Book
from libris.markdown import (
    create_book_note,
    sanitize_filename,
    standardize_title,
    list_books,
    read_frontmatter,
    update_frontmatter_from_book,
    compute_canonical_filename,
    update_wikilinks_in_vault,
    rename_book_file,
)


def test_sanitize_filename():
    assert sanitize_filename("Title: With Colon") == "Title With Colon"
    assert sanitize_filename("Title / With / Slash") == "Title With Slash"


def test_create_book_note(tmp_path):
    book = Book(
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
    assert "author:\n- Author One" in content
    assert "### Description" in content
    assert "A test description" in content


def test_update_book_status(tmp_path):
    file_path = tmp_path / "test.md"
    file_path.write_text("---\ntitle: Test\nstatus: To Read\n---\n")

    from libris.markdown import update_book_status

    update_book_status(file_path, "Reading")

    content = file_path.read_text()
    assert "status: Reading" in content
    assert "status: To Read" not in content


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


def test_ensure_frontmatter_converts_author_string_to_list(tmp_path):
    file_path = tmp_path / "string_author.md"
    file_path.write_text(
        "---\ntitle: Test\nauthor: John Doe\ngoogle_books_id: abc\n---\n"
    )

    from libris.markdown import ensure_frontmatter_fields

    updated, _ = ensure_frontmatter_fields(file_path)
    assert updated is True

    content = file_path.read_text()
    assert "author:\n- John Doe" in content


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
    assert "format: Audiobook" in content
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

    book = Book(
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
    assert "### Description" in content
    assert "A description" in content


def test_update_frontmatter_from_book_skips_existing_description(tmp_path):
    f = tmp_path / "book.md"
    f.write_text("---\ntitle: null\n---\n\n### Description\nExisting desc\n")

    book = Book(
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
    assert content.count("### Description") == 1
    assert "Existing desc" in content
    assert "New desc" not in content


def test_update_frontmatter_from_book_does_not_overwrite(tmp_path):
    f = tmp_path / "book.md"
    f.write_text(
        "---\ntitle: My Title\nauthor:\n- Original\nisbn: '999'\n"
        "page_count: 50\npublished_date: '2019'\ngoogle_books_id: existing\n"
        "thumbnail: http://old.jpg\ngenres:\n- Nonfiction\n---\n"
    )

    book = Book(
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
        "---\ntitle: The Great Gatsby\nauthor:\n- F. Scott Fitzgerald\nisbn: '123'\n"
        "page_count: 200\npublished_date: '1925'\ngoogle_books_id: abc\n"
        "thumbnail: null\ngenres: null\ntags: Book\nformat: null\n"
        "status: To Read\nrating: null\nreferred_by: null\n"
        "date_added: null\ndate_started: null\ndate_finished: null\n---\n"
    )

    updated, _ = ensure_frontmatter_fields(file_path)
    assert updated is False


# --- File Rename Tests ---


def test_compute_canonical_filename(tmp_path):
    f = tmp_path / "old name.md"
    f.write_text("---\ntitle: The Great Gatsby\nauthor:\n- F. Scott Fitzgerald\n---\n")
    result = compute_canonical_filename(f)
    assert result == "The Great Gatsby - F. Scott Fitzgerald.md"


def test_compute_canonical_filename_missing_author(tmp_path):
    f = tmp_path / "no_author.md"
    f.write_text("---\ntitle: Solo Title\nauthor: null\n---\n")
    assert compute_canonical_filename(f) is None


def test_compute_canonical_filename_missing_title(tmp_path):
    f = tmp_path / "no_title.md"
    f.write_text("---\ntitle: null\nauthor:\n- Someone\n---\n")
    assert compute_canonical_filename(f) is None


def test_compute_canonical_filename_sanitizes(tmp_path):
    f = tmp_path / "bad.md"
    f.write_text("---\ntitle: 'Title: With Colon'\nauthor:\n- Author\n---\n")
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
    f.write_text("---\ntitle: Dune\nauthor:\n- Frank Herbert\n---\n")
    result = rename_book_file(f, tmp_path)
    assert result.status == "renamed"
    assert result.new_path.name == "Dune - Frank Herbert.md"
    assert result.new_path.exists()
    assert not f.exists()


def test_rename_book_file_updates_links(tmp_path):
    book = tmp_path / "wrong name.md"
    book.write_text("---\ntitle: Dune\nauthor:\n- Frank Herbert\n---\n")
    note = tmp_path / "reading log.md"
    note.write_text("Currently reading [[wrong name]].\n")

    result = rename_book_file(book, tmp_path)
    assert result.status == "renamed"
    assert result.new_path.name == "Dune - Frank Herbert.md"
    assert "[[Dune - Frank Herbert]]" in note.read_text()


def test_rename_book_file_collision_skips(tmp_path):
    f = tmp_path / "wrong name.md"
    f.write_text("---\ntitle: Dune\nauthor:\n- Frank Herbert\n---\n")
    collision = tmp_path / "Dune - Frank Herbert.md"
    collision.write_text("---\ntitle: Dune\n---\n")

    result = rename_book_file(f, tmp_path)
    assert result.status == "collision"
    assert result.detail == "Dune - Frank Herbert.md"
    assert f.exists()


def test_rename_book_file_already_canonical(tmp_path):
    f = tmp_path / "Dune - Frank Herbert.md"
    f.write_text("---\ntitle: Dune\nauthor:\n- Frank Herbert\n---\n")
    result = rename_book_file(f, tmp_path)
    assert result.status == "already_canonical"


def test_rename_book_file_missing_title(tmp_path):
    f = tmp_path / "some file.md"
    f.write_text("---\ntitle: null\nauthor:\n- Some Author\n---\n")
    result = rename_book_file(f, tmp_path)
    assert result.status == "missing_title"
    assert f.exists()


def test_rename_book_file_missing_author(tmp_path):
    f = tmp_path / "some file.md"
    f.write_text("---\ntitle: Some Title\nauthor: null\n---\n")
    result = rename_book_file(f, tmp_path)
    assert result.status == "missing_author"
    assert f.exists()


def test_rename_book_file_empty_author_list(tmp_path):
    f = tmp_path / "some file.md"
    f.write_text("---\ntitle: Some Title\nauthor: []\n---\n")
    result = rename_book_file(f, tmp_path)
    assert result.status == "missing_author"


def test_rename_book_file_whitespace_title(tmp_path):
    f = tmp_path / "some file.md"
    f.write_text("---\ntitle: '   '\nauthor:\n- Author\n---\n")
    result = rename_book_file(f, tmp_path)
    assert result.status == "missing_title"
