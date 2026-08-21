"""Tests asserting the canonical frontmatter vocabulary from ADR 0005.

Every note in the vault uses `authors`, `date_published` and `cover_thumbnail`. The code
reads `author`, `published_date` and `thumbnail`, so every read returns None silently and
six behaviours are broken in production while the rest of the suite stays green — because
its fixtures use the code's schema rather than the vault's.

These tests build notes shaped like real ones. See #61.
"""

import pytest

from libris.api import BookCandidate
from libris.cli import _build_query_from_frontmatter, _needs_enrichment
from libris.importer import _build_vault_index
from libris.markdown import (
    compute_canonical_filename,
    create_book_note,
    find_duplicates,
    rename_book_file,
)

# The exact key set present on all 3,137 notes in the vault.
CANONICAL_FRONTMATTER = {
    "title": None,
    "authors": None,
    "isbn": None,
    "page_count": None,
    "date_published": None,
    "google_books_id": "",
    "cover_thumbnail": None,
    "genres": [],
    "tags": "Book",
    "format": None,
    "status": "To Read",
    "rating": None,
    "referred_by": None,
    "date_added": None,
    "date_started": None,
    "date_finished": None,
}


def write_note(vault, filename, **overrides):
    """Write a Book Note shaped exactly like the ones in the vault.

    Args:
        vault: Directory to write into.
        filename: Note filename, including the .md suffix.
        **overrides: Frontmatter values to set, using canonical field names.

    Returns:
        Path to the written note.
    """
    fm = {**CANONICAL_FRONTMATTER, **overrides}
    lines = []
    for key, value in fm.items():
        if value is None:
            lines.append(f"{key}:")
        elif isinstance(value, list):
            if not value:
                lines.append(f"{key}: []")
            else:
                lines.append(f"{key}:")
                lines.extend(f"  - {item}" for item in value)
        elif isinstance(value, str):
            lines.append(f'{key}: "{value}"')
        else:
            lines.append(f"{key}: {value}")

    path = vault / filename
    path.write_text(
        "---\n" + "\n".join(lines) + "\n---\n\n## Notes\n", encoding="utf-8"
    )
    return path


@pytest.fixture
def vault(tmp_path):
    """A Shelf directory to write Book Notes into."""
    shelf = tmp_path / "Book List"
    shelf.mkdir()
    return shelf


def test_vault_index_finds_notes_written_with_canonical_fields(vault):
    # Given two Book Notes shaped like the ones in the vault
    write_note(
        vault, "Dune - Frank Herbert.md", title="Dune", authors=["Frank Herbert"]
    )
    write_note(
        vault,
        "Oathbringer - Brandon Sanderson.md",
        title="Oathbringer",
        authors=["Brandon Sanderson"],
    )

    # When the importer builds its duplicate-detection index
    index = _build_vault_index(vault)

    # Then both notes are indexed
    # Currently 0: every note is skipped at importer.py:134 because fm.get("author")
    # is None, so import duplicate detection has never matched anything.
    assert len(index) == 2


def test_duplicates_does_not_group_books_by_different_authors(vault):
    # Given two unrelated books that happen to share a title
    write_note(vault, "Poems - Catullus.md", title="Poems", authors=["Catullus"])
    write_note(
        vault,
        "Poems - Gerard Manley Hopkins.md",
        title="Poems",
        authors=["Gerard Manley Hopkins"],
    )

    # When duplicates are detected
    groups = find_duplicates(vault)

    # Then they are not duplicates of each other
    # Currently one group of 2: _author_key returns () for every note, so a missing
    # author is treated as a wildcard and grouping falls back to title alone.
    assert groups == []


def test_canonical_filename_resolves_from_the_authors_field(vault):
    # Given a Book Note with a misnamed file
    note = write_note(vault, "wrong-name.md", title="Dune", authors=["Frank Herbert"])

    # When the canonical filename is computed
    result = compute_canonical_filename(note)

    # Then it comes from the title and first author
    # Currently None for every note in the vault, which makes clean --rename a no-op.
    assert result == "Dune - Frank Herbert.md"


def test_rename_reads_the_authors_field(vault):
    # Given a Book Note with a misnamed file
    note = write_note(vault, "wrong-name.md", title="Dune", authors=["Frank Herbert"])

    # When it is renamed
    result = rename_book_file(note)

    # Then the author was found
    # Currently "missing_author" for every note in the vault.
    assert result.status != "missing_author"


def test_enrichment_query_searches_by_author(vault):
    # Given frontmatter from a Book Note in the vault
    fm = {**CANONICAL_FRONTMATTER, "title": "Poems", "authors": ["Catullus"]}
    note = vault / "Poems - Catullus.md"

    # When an enrichment query is built from it
    query = _build_query_from_frontmatter(fm, note)

    # Then it constrains the search by author
    # Currently title-only, because fm.get("author") is None. Nineteen notes in the
    # vault are titled "Poems", so a title-only query cannot resolve them.
    assert "inauthor:Catullus" in query


def test_already_enriched_note_is_not_re_enriched():
    # Given a note carrying metadata that only Google Books could have supplied
    fm = {
        **CANONICAL_FRONTMATTER,
        "title": "Dune",
        "authors": ["Frank Herbert"],
        "cover_thumbnail": "http://books.google.com/books/content?id=abc",
        "date_published": "1965-08-01",
    }

    # When it is checked for enrichment
    # Then it is recognised as already enriched
    # Currently True: _API_SOURCED_FIELDS names "thumbnail" and "published_date", neither
    # of which exists, so it tests two fields instead of four. Measured against the
    # vault it reports 152 unenriched notes when only 5 are.
    assert _needs_enrichment(fm) is False


def test_created_notes_use_the_canonical_field_names(vault):
    # Given a book resolved from Google Books
    book = BookCandidate(
        title="Dune",
        authors=["Frank Herbert"],
        isbn="9780441013593",
        page_count=412,
        published_date="1965-08-01",
        google_books_id="dune1",
        thumbnail="http://books.google.com/books/content?id=dune1",
        genres=["Science Fiction"],
        description="A novel about a desert planet.",
    )

    # When a note is created for it
    note = create_book_note(book, vault)
    content = note.read_text(encoding="utf-8")

    # Then the frontmatter matches every other note in the vault
    # Currently writes author/published_date/thumbnail, so newly created notes disagree
    # with all 3,137 existing ones. This is the root cause of the five failures above.
    assert "authors:" in content
    assert "date_published:" in content
    assert "cover_thumbnail:" in content
