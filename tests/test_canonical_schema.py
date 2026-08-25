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
    update_book_status,
)
from libris.note_format import InvalidFieldValue

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
                # Quote as the vault does, so a wikilink is a string and not a
                # nested YAML sequence.
                lines.extend(f'  - "{item}"' for item in value)
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


def test_an_author_written_as_a_wikilink_reads_as_a_plain_name(vault):
    # Given a note that links its author to their own note
    note = write_note(
        vault,
        "Atlas Shrugged - Ayn Rand.md",
        title="Atlas Shrugged",
        authors=["[[Ayn Rand]]"],
    )

    # Then the name is used, not the link markup
    # Seven notes in the vault store authors this way; the brackets would
    # otherwise end up in a canonical filename.
    from libris.markdown import BookNote

    assert BookNote.read(note).authors == ["Ayn Rand"]
    assert BookNote.read(note).canonical_filename == "Atlas Shrugged - Ayn Rand.md"


def test_stray_whitespace_in_an_author_name_is_collapsed(vault):
    # Given an author name carrying a doubled space
    note = write_note(
        vault,
        "10% Happier - Dan Harris.md",
        title="10% Happier",
        authors=["Dan   Harris"],
    )

    # Then it matches the same name written normally
    # This is why the migration first missed one polluted title: the check
    # compared "Dan   Harris" against "Dan Harris" literally.
    from libris.markdown import BookNote

    assert BookNote.read(note).first_author == "Dan Harris"


# --- field value vocabularies (#65) ---
#
# Measured against the vault before writing these: all 3,136 notes already hold
# a legal status (1,667 Read, 1,467 To Read, 1 Reading, 1 Not To Read) and a
# legal priority (2,251 unset, then Low/Medium/High). Nothing needs migrating,
# so this is pure enforcement. `format` is deliberately not validated here: it
# holds eleven shapes across two types and needs a migration of its own.


def _candidate() -> BookCandidate:
    return BookCandidate(title="Dune", authors=["Frank Herbert"])


@pytest.mark.parametrize("status", ["To Read", "Reading", "Read", "Not To Read"])
def test_every_status_the_vault_uses_is_accepted(tmp_path, status):
    # Given a status that exists in the vault today
    # When a note is created with it
    path = create_book_note(_candidate(), tmp_path, status=status)

    # Then it is written
    assert path.exists()


def test_an_unknown_status_is_refused(tmp_path):
    # Given a status outside the four the Library defines
    # When a note is created with it
    # Then it is refused rather than silently written into the Shelf
    with pytest.raises(InvalidFieldValue):
        create_book_note(_candidate(), tmp_path, status="finished")


def test_a_status_differing_only_in_case_is_refused(tmp_path):
    # Given the right word in the wrong case
    # When a note is created with it
    # Then it is refused; the vault's Bases views group on the exact string
    with pytest.raises(InvalidFieldValue):
        create_book_note(_candidate(), tmp_path, status="read")


def test_the_refusal_names_the_field_and_the_legal_values(tmp_path):
    # Given an illegal status
    # When a note is created with it
    with pytest.raises(InvalidFieldValue) as excinfo:
        create_book_note(_candidate(), tmp_path, status="finished")

    # Then the message is usable without reading the source
    message = str(excinfo.value)
    assert "status" in message
    assert "finished" in message
    assert "To Read" in message


@pytest.mark.parametrize("priority", ["Low", "Medium", "High"])
def test_every_priority_the_vault_uses_is_accepted(tmp_path, priority):
    # Given a priority that exists in the vault today
    # When a note is created with it
    path = create_book_note(_candidate(), tmp_path, overrides={"priority": priority})

    # Then it is written
    assert path.exists()


def test_priority_may_be_unset(tmp_path):
    # Given a book never triaged - 2,251 notes in the vault
    # When a note is created with no priority
    path = create_book_note(_candidate(), tmp_path, overrides={"priority": None})

    # Then that is legal; absent is not the same as invalid
    assert path.exists()


def test_an_unknown_priority_is_refused(tmp_path):
    # Given a priority outside Low, Medium, High
    # When a note is created with it
    # Then it is refused
    with pytest.raises(InvalidFieldValue):
        create_book_note(_candidate(), tmp_path, overrides={"priority": "Urgent"})


def test_status_override_is_validated_too(tmp_path):
    # Given an illegal status arriving as an override rather than the argument
    # When a note is created
    # Then the same rule applies; there is one gate, not two
    with pytest.raises(InvalidFieldValue):
        create_book_note(_candidate(), tmp_path, overrides={"status": "finished"})


def test_updating_to_an_unknown_status_is_refused(tmp_path):
    # Given an existing note
    path = create_book_note(_candidate(), tmp_path, status="To Read")

    # When its status is updated to something the Library does not define
    # Then it is refused and the note is left alone
    with pytest.raises(InvalidFieldValue):
        update_book_status(path, "finished")
    assert "status: To Read" in path.read_text(encoding="utf-8")


def test_updating_to_a_known_status_is_allowed(tmp_path):
    # Given an existing note
    path = create_book_note(_candidate(), tmp_path, status="To Read")

    # When its status is updated to a legal value
    update_book_status(path, "Read")

    # Then the note carries it
    assert "status: Read" in path.read_text(encoding="utf-8")


def test_format_is_not_validated_yet(tmp_path):
    # Given the vault holds format as both string and list, in mixed case
    # When a note is created with a bare string, as `libris add -f` writes today
    path = create_book_note(_candidate(), tmp_path, overrides={"format": "Audiobook"})

    # Then it is accepted. Validating this needs the field's type settled and
    # 1,341 notes migrated first, which is its own piece of work.
    assert path.exists()
