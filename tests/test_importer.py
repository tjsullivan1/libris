import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from libris.cli import app
from libris.config import set_config
from libris.importer import (
    normalize_for_match,
    parse_audible_json,
    run_import,
)

runner = CliRunner()


def _write_book(vault: Path, name: str, **frontmatter_fields) -> Path:
    """Helper to write a minimal book note with given frontmatter fields."""
    lines = ["---"]
    for key, val in frontmatter_fields.items():
        if isinstance(val, list):
            lines.append(f"{key}:")
            for item in val:
                lines.append(f"- {item}")
        elif val is None:
            lines.append(f"{key}: null")
        else:
            lines.append(f"{key}: {val}")
    lines.append("---\n")
    p = vault / name
    p.write_text("\n".join(lines), encoding="utf-8")
    return p


def _write_audible_json(path: Path, entries: list[dict]) -> Path:
    """Helper to write a JSON file with Audible-format entries."""
    path.write_text(json.dumps(entries), encoding="utf-8")
    return path


# --- normalize_for_match tests ---


def test_normalize_for_match_basic():
    assert normalize_for_match("Hello, World!") == "hello world"


def test_normalize_for_match_extra_whitespace():
    assert normalize_for_match("  foo   bar  ") == "foo bar"


def test_normalize_for_match_punctuation():
    assert normalize_for_match("It's a Test: Book #1") == "it s a test book 1"


# --- parse_audible_json tests ---


def test_parse_audible_json_basic(tmp_path):
    data = [
        {
            "title": "The Great Book",
            "author": "Jane Smith",
            "narrator": "John Doe",
            "series": "",
            "length": "8h 30m",
            "finished": "Yes",
            "progress": "Finished",
        },
        {
            "title": "Another Book",
            "author": "Bob Jones, Alice Lee",
            "narrator": "Someone",
            "series": "Series A , Book 1",
            "length": "4h",
            "finished": "No",
            "progress": "",
        },
    ]
    json_file = _write_audible_json(tmp_path / "library.json", data)
    books = parse_audible_json(json_file)

    assert len(books) == 2

    assert books[0].title == "The Great Book"
    assert books[0].authors == ["Jane Smith"]
    assert books[0].status == "Read"
    assert books[0].format == "Audiobook"
    assert books[0].source_format == "audible-json"

    assert books[1].title == "Another Book"
    assert books[1].authors == ["Bob Jones", "Alice Lee"]
    assert books[1].status == "To Read"


def test_parse_audible_json_skips_empty_title(tmp_path):
    data = [
        {"title": "", "author": "Someone", "finished": "No"},
        {"title": "Valid Book", "author": "Author", "finished": "No"},
    ]
    json_file = _write_audible_json(tmp_path / "library.json", data)
    books = parse_audible_json(json_file)
    assert len(books) == 1
    assert books[0].title == "Valid Book"


def test_parse_audible_json_empty_author(tmp_path):
    data = [{"title": "No Author Book", "author": "", "finished": "No"}]
    json_file = _write_audible_json(tmp_path / "library.json", data)
    books = parse_audible_json(json_file)
    assert len(books) == 1
    assert books[0].authors == ["Unknown Author"]


def test_parse_audible_json_invalid_format(tmp_path):
    json_file = tmp_path / "bad.json"
    json_file.write_text('{"not": "an array"}', encoding="utf-8")
    with pytest.raises(ValueError, match="Expected a JSON array"):
        parse_audible_json(json_file)


def test_parse_audible_json_empty_array(tmp_path):
    json_file = _write_audible_json(tmp_path / "library.json", [])
    books = parse_audible_json(json_file)
    assert books == []


# --- run_import / duplicate detection tests ---


def test_import_new_books_dry_run(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    set_config("book_vault", str(vault))

    data = [
        {"title": "Book One", "author": "Author A", "finished": "No"},
        {"title": "Book Two", "author": "Author B", "finished": "Yes"},
    ]
    json_file = _write_audible_json(tmp_path / "library.json", data)

    result = run_import(json_file, vault, apply=False)
    assert len(result.new_books) == 2
    assert len(result.updated_books) == 0
    assert len(result.skipped_books) == 0
    # Dry run: no files created
    assert list(vault.glob("*.md")) == []


def test_import_new_books_apply(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    set_config("book_vault", str(vault))

    data = [
        {"title": "Book One", "author": "Author A", "finished": "No"},
        {"title": "Book Two", "author": "Author B", "finished": "Yes"},
    ]
    json_file = _write_audible_json(tmp_path / "library.json", data)

    result = run_import(json_file, vault, apply=True)
    assert len(result.new_books) == 2

    md_files = list(vault.glob("*.md"))
    assert len(md_files) == 2

    # Check that the status is set correctly
    for f in md_files:
        content = f.read_text(encoding="utf-8")
        if "Book One" in content:
            assert "status: To Read" in content
            assert "format: Audiobook" in content
        elif "Book Two" in content:
            assert "status: Read" in content
            assert "format: Audiobook" in content


def test_import_detects_duplicates(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    set_config("book_vault", str(vault))

    # Pre-existing book in vault
    _write_book(
        vault,
        "Existing Book - Author A.md",
        title="Existing Book",
        author=["Author A"],
        status="Read",
        format="Audiobook",
    )

    data = [
        {"title": "Existing Book", "author": "Author A", "finished": "Yes"},
        {"title": "New Book", "author": "Author B", "finished": "No"},
    ]
    json_file = _write_audible_json(tmp_path / "library.json", data)

    result = run_import(json_file, vault, apply=False)
    assert len(result.new_books) == 1
    assert len(result.skipped_books) == 1
    assert result.skipped_books[0].title == "Existing Book"


def test_import_updates_status_on_duplicate(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    set_config("book_vault", str(vault))

    # Existing book with "To Read" status
    existing = _write_book(
        vault,
        "My Book - Author A.md",
        title="My Book",
        author=["Author A"],
        status="To Read",
        format=None,
    )

    data = [{"title": "My Book", "author": "Author A", "finished": "Yes"}]
    json_file = _write_audible_json(tmp_path / "library.json", data)

    result = run_import(json_file, vault, apply=True)
    assert len(result.updated_books) == 1
    assert "status" in result.updated_books[0][2]

    content = existing.read_text(encoding="utf-8")
    assert "status: Read" in content


def test_import_updates_format_on_duplicate(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    set_config("book_vault", str(vault))

    # Existing book with no format
    existing = _write_book(
        vault,
        "My Book - Author A.md",
        title="My Book",
        author=["Author A"],
        status="Read",
        format=None,
    )

    data = [{"title": "My Book", "author": "Author A", "finished": "Yes"}]
    json_file = _write_audible_json(tmp_path / "library.json", data)

    result = run_import(json_file, vault, apply=True)
    assert len(result.updated_books) == 1
    assert "format" in result.updated_books[0][2]

    content = existing.read_text(encoding="utf-8")
    assert "format: Audiobook" in content


def test_import_updates_format_when_field_missing(tmp_path):
    """Test that format update works when the format field is completely missing from frontmatter."""
    vault = tmp_path / "vault"
    vault.mkdir()
    set_config("book_vault", str(vault))

    # Create a book note without a format field at all
    existing = vault / "My Book - Author A.md"
    existing.write_text(
        "---\ntitle: My Book\nauthor:\n- Author A\nstatus: Read\n---\n",
        encoding="utf-8",
    )

    data = [{"title": "My Book", "author": "Author A", "finished": "Yes"}]
    json_file = _write_audible_json(tmp_path / "library.json", data)

    result = run_import(json_file, vault, apply=True)
    assert len(result.updated_books) == 1
    assert "format" in result.updated_books[0][2]

    content = existing.read_text(encoding="utf-8")
    assert "format: Audiobook" in content


def test_import_updates_both_status_and_format(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    set_config("book_vault", str(vault))

    existing = _write_book(
        vault,
        "My Book - Author A.md",
        title="My Book",
        author=["Author A"],
        status="To Read",
        format=None,
    )

    data = [{"title": "My Book", "author": "Author A", "finished": "Yes"}]
    json_file = _write_audible_json(tmp_path / "library.json", data)

    result = run_import(json_file, vault, apply=True)
    assert len(result.updated_books) == 1
    updates = result.updated_books[0][2]
    assert "status" in updates
    assert "format" in updates

    content = existing.read_text(encoding="utf-8")
    assert "status: Read" in content
    assert "format: Audiobook" in content


def test_import_skips_up_to_date_duplicate(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    set_config("book_vault", str(vault))

    _write_book(
        vault,
        "My Book - Author A.md",
        title="My Book",
        author=["Author A"],
        status="Read",
        format="Audiobook",
    )

    data = [{"title": "My Book", "author": "Author A", "finished": "Yes"}]
    json_file = _write_audible_json(tmp_path / "library.json", data)

    result = run_import(json_file, vault, apply=False)
    assert len(result.skipped_books) == 1
    assert len(result.updated_books) == 0
    assert len(result.new_books) == 0


def test_import_limit(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    set_config("book_vault", str(vault))

    data = [
        {"title": f"Book {i}", "author": f"Author {i}", "finished": "No"}
        for i in range(10)
    ]
    json_file = _write_audible_json(tmp_path / "library.json", data)

    result = run_import(json_file, vault, apply=False, limit=3)
    assert len(result.new_books) == 3


def test_import_case_insensitive_duplicate(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    set_config("book_vault", str(vault))

    _write_book(
        vault,
        "The Great Book - Jane Smith.md",
        title="The Great Book",
        author=["Jane Smith"],
        status="Read",
        format="Audiobook",
    )

    # Same book with different casing
    data = [{"title": "the great book", "author": "jane smith", "finished": "Yes"}]
    json_file = _write_audible_json(tmp_path / "library.json", data)

    result = run_import(json_file, vault, apply=False)
    assert len(result.skipped_books) == 1
    assert len(result.new_books) == 0


# --- CLI tests ---


def test_cli_import_dry_run(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    set_config("book_vault", str(vault))

    data = [
        {"title": "CLI Book", "author": "CLI Author", "finished": "No"},
    ]
    json_file = _write_audible_json(tmp_path / "library.json", data)

    result = runner.invoke(app, ["import", str(json_file)])
    assert result.exit_code == 0
    assert "Dry run complete" in result.output
    assert "1 new book(s) would be added" in result.output
    assert "Run with --apply" in result.output
    # No files created
    assert list(vault.glob("*.md")) == []


def test_cli_import_apply(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    set_config("book_vault", str(vault))

    data = [
        {"title": "CLI Book", "author": "CLI Author", "finished": "Yes"},
    ]
    json_file = _write_audible_json(tmp_path / "library.json", data)

    result = runner.invoke(app, ["import", str(json_file), "--apply"])
    assert result.exit_code == 0
    assert "Import complete" in result.output
    assert "1 new book(s) added" in result.output
    assert len(list(vault.glob("*.md"))) == 1


def test_cli_import_file_not_found(tmp_path):
    result = runner.invoke(app, ["import", str(tmp_path / "nonexistent.json")])
    assert result.exit_code == 1
    assert "File not found" in result.output


def test_cli_import_with_limit(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    set_config("book_vault", str(vault))

    data = [
        {"title": f"Book {i}", "author": f"Author {i}", "finished": "No"}
        for i in range(10)
    ]
    json_file = _write_audible_json(tmp_path / "library.json", data)

    result = runner.invoke(app, ["import", str(json_file), "--limit", "3"])
    assert result.exit_code == 0
    assert "3 new book(s) would be added" in result.output


def test_cli_import_unknown_format(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    set_config("book_vault", str(vault))

    csv_file = tmp_path / "data.csv"
    csv_file.write_text("title,author\nFoo,Bar\n", encoding="utf-8")

    result = runner.invoke(app, ["import", str(csv_file)])
    assert result.exit_code == 1
    assert "Cannot detect format" in result.output
