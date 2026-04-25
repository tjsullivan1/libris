from pathlib import Path
from typer.testing import CliRunner
from libris.cli import app
from libris.markdown import find_duplicates
from libris.config import set_config

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


def test_no_duplicates(tmp_path):
    _write_book(tmp_path, "A.md", title="Book A", isbn="111", google_books_id="a1")
    _write_book(tmp_path, "B.md", title="Book B", isbn="222", google_books_id="b2")
    assert find_duplicates(tmp_path) == []


def test_duplicate_by_title(tmp_path):
    _write_book(tmp_path, "A.md", title="Same Title", isbn="111", google_books_id="a1")
    _write_book(tmp_path, "B.md", title="same title", isbn="222", google_books_id="b2")
    groups = find_duplicates(tmp_path)
    assert len(groups) == 1
    names = {p.name for p in groups[0]}
    assert names == {"A.md", "B.md"}


def test_duplicate_by_isbn(tmp_path):
    _write_book(tmp_path, "A.md", title="First Title", isbn="111", google_books_id="a1")
    _write_book(tmp_path, "B.md", title="Second Title", isbn="111", google_books_id="b2")
    groups = find_duplicates(tmp_path)
    assert len(groups) == 1


def test_duplicate_by_google_books_id(tmp_path):
    _write_book(tmp_path, "A.md", title="First", isbn="111", google_books_id="same")
    _write_book(tmp_path, "B.md", title="Second", isbn="222", google_books_id="same")
    groups = find_duplicates(tmp_path)
    assert len(groups) == 1


def test_transitive_duplicates_merged(tmp_path):
    """A shares title with B, B shares ISBN with C => all three in one group."""
    _write_book(tmp_path, "A.md", title="Shared Title", isbn="111", google_books_id="a1")
    _write_book(tmp_path, "B.md", title="Shared Title", isbn="222", google_books_id="b2")
    _write_book(tmp_path, "C.md", title="Other Title", isbn="222", google_books_id="c3")
    groups = find_duplicates(tmp_path)
    assert len(groups) == 1
    assert len(groups[0]) == 3


def test_multiple_independent_groups(tmp_path):
    _write_book(tmp_path, "A.md", title="Group One", isbn="111")
    _write_book(tmp_path, "B.md", title="Group One", isbn="112")
    _write_book(tmp_path, "C.md", title="Group Two", isbn="333")
    _write_book(tmp_path, "D.md", title="Group Two", isbn="444")
    _write_book(tmp_path, "E.md", title="Unique", isbn="555")
    groups = find_duplicates(tmp_path)
    assert len(groups) == 2


def test_files_without_frontmatter_skipped(tmp_path):
    _write_book(tmp_path, "A.md", title="Same", isbn="111")
    _write_book(tmp_path, "B.md", title="Same", isbn="222")
    # File without frontmatter
    (tmp_path / "plain.md").write_text("# Just a heading\n")
    groups = find_duplicates(tmp_path)
    assert len(groups) == 1
    names = {p.name for p in groups[0]}
    assert "plain.md" not in names


def test_null_fields_not_matched(tmp_path):
    _write_book(tmp_path, "A.md", title="Book A", isbn=None, google_books_id=None)
    _write_book(tmp_path, "B.md", title="Book B", isbn=None, google_books_id=None)
    assert find_duplicates(tmp_path) == []


def test_cli_duplicates_command_reports(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    _write_book(vault, "A.md", title="Dup Book", isbn="111", google_books_id="g1")
    _write_book(vault, "B.md", title="Dup Book", isbn="222", google_books_id="g2")
    set_config("vault_path", str(vault))

    result = runner.invoke(app, ["duplicates"])
    assert result.exit_code == 0
    assert "Group 1:" in result.output
    assert "A.md" in result.output
    assert "B.md" in result.output


def test_cli_duplicates_command_no_duplicates(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    _write_book(vault, "A.md", title="Unique A", isbn="111")
    _write_book(vault, "B.md", title="Unique B", isbn="222")
    set_config("vault_path", str(vault))

    result = runner.invoke(app, ["duplicates"])
    assert result.exit_code == 0
    assert "No duplicates found." in result.output
