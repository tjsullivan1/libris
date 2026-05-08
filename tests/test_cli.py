from typer.testing import CliRunner
from libris.cli import app
import yaml

runner = CliRunner()

def test_config_vault_path(tmp_path):
    # Test getting current vault path (should not fail)
    result = runner.invoke(app, ["config"])
    assert result.exit_code == 0
    assert "Current vault path:" in result.output

    # Test setting vault path
    vault_path = tmp_path / "my_vault"
    result = runner.invoke(app, ["config", "--vault", str(vault_path)])
    assert result.exit_code == 0
    assert f"Vault path set to: {vault_path.resolve()}" in result.output
    assert vault_path.exists()

    # Test setting API key
    result = runner.invoke(app, ["config", "--api-key", "my-secret-key"])
    assert result.exit_code == 0
    assert "API key set successfully." in result.output
    
    # Test getting current config
    result = runner.invoke(app, ["config"])
    assert result.exit_code == 0
    assert "API key: *********-key" in result.output

def test_config_vault_path_writes_legacy_and_new_keys(tmp_path):
    from libris.config import get_config_file

    vault_path = tmp_path / "my_vault"
    result = runner.invoke(app, ["config", "--vault", str(vault_path)])

    assert result.exit_code == 0
    config_data = yaml.safe_load(get_config_file().read_text(encoding="utf-8"))
    assert config_data["book_vault"] == str(vault_path.resolve())
    assert config_data["vault_path"] == str(vault_path.resolve())


def test_config_command_reads_legacy_vault_key(tmp_path):
    from libris.config import get_config_file

    legacy_vault = tmp_path / "legacy_vault"
    legacy_vault.mkdir()
    get_config_file().write_text(
        yaml.safe_dump({"vault_path": str(legacy_vault.resolve())}),
        encoding="utf-8",
    )

    result = runner.invoke(app, ["config"])

    assert result.exit_code == 0
    assert f"Current vault path: {legacy_vault.resolve()}" in result.output

def test_cleanup_command(tmp_path):
    # Mock vault path
    vault_path = tmp_path / "my_vault"
    vault_path.mkdir()
    
    # Create a legacy file
    legacy_file = vault_path / "Legacy.md"
    legacy_file.write_text("---\ntitle: Legacy\nstatus: To Read\ngoogle_books_id: 123\n---\n")
    
    # Run cleanup via CLI
    # We need to ensure the config uses this vault path
    from libris.config import set_config
    set_config("vault_path", str(vault_path))
    
    result = runner.invoke(app, ["cleanup"])
    assert result.exit_code == 0
    assert "Updated: Legacy.md" in result.output
    assert "Finished. Updated 1 books." in result.output
    
    # Verify file content
    content = legacy_file.read_text()
    assert "tags: Book" in content
    
    # Run again
    result = runner.invoke(app, ["cleanup"])
    assert result.exit_code == 0
    assert "All books are already up to date." in result.output


def test_search_command_generic(monkeypatch):
    """Search with no flags performs a generic query."""
    from libris.api import Book
    mock_books = [
        Book(
            title="The Great Gatsby",
            authors=["F. Scott Fitzgerald"],
            isbn="1234567890123",
            page_count=180,
            published_date="1925",
            google_books_id="abc123",
            thumbnail=None,
            genres=["Classic"],
            description="A novel about Jay Gatsby",
        )
    ]
    monkeypatch.setattr("libris.cli.GoogleBooksClient.search", lambda self, q: mock_books)

    result = runner.invoke(app, ["search", "gatsby"])
    assert result.exit_code == 0
    assert "Found 1 result(s):" in result.output
    assert "The Great Gatsby" in result.output
    assert "F. Scott Fitzgerald" in result.output
    assert "1234567890123" in result.output
    assert "1925" in result.output
    assert "180" in result.output


def test_search_command_by_author(monkeypatch):
    """Search with --author prepends inauthor: prefix."""
    from libris.api import Book
    captured = {}

    def fake_search(self, q):
        captured["query"] = q
        return [
            Book(
                title="Dune",
                authors=["Frank Herbert"],
                isbn=None,
                page_count=412,
                published_date="1965",
                google_books_id="dune1",
                thumbnail=None,
                genres=["Science Fiction"],
                description=None,
            )
        ]

    monkeypatch.setattr("libris.cli.GoogleBooksClient.search", fake_search)

    result = runner.invoke(app, ["search", "--author", "Frank Herbert"])
    assert result.exit_code == 0
    assert captured["query"] == "inauthor:Frank Herbert"
    assert "Dune" in result.output
    assert "Frank Herbert" in result.output


def test_search_command_by_title(monkeypatch):
    """Search with --title prepends intitle: prefix."""
    from libris.api import Book
    captured = {}

    def fake_search(self, q):
        captured["query"] = q
        return [
            Book(
                title="Dune",
                authors=["Frank Herbert"],
                isbn=None,
                page_count=None,
                published_date=None,
                google_books_id="dune1",
                thumbnail=None,
                genres=[],
                description=None,
            )
        ]

    monkeypatch.setattr("libris.cli.GoogleBooksClient.search", fake_search)

    result = runner.invoke(app, ["search", "--title", "Dune"])
    assert result.exit_code == 0
    assert captured["query"] == "intitle:Dune"
    assert "Dune" in result.output


def test_search_command_by_isbn(monkeypatch):
    """Search with --isbn prepends isbn: prefix."""
    from libris.api import Book
    captured = {}

    def fake_search(self, q):
        captured["query"] = q
        return [
            Book(
                title="Dune",
                authors=["Frank Herbert"],
                isbn="9780441013593",
                page_count=None,
                published_date=None,
                google_books_id="dune1",
                thumbnail=None,
                genres=[],
                description=None,
            )
        ]

    monkeypatch.setattr("libris.cli.GoogleBooksClient.search", fake_search)

    result = runner.invoke(app, ["search", "--isbn", "9780441013593"])
    assert result.exit_code == 0
    assert captured["query"] == "isbn:9780441013593"
    assert "9780441013593" in result.output


def test_search_command_no_results(monkeypatch):
    """Search returns a helpful message when no books are found."""
    monkeypatch.setattr("libris.cli.GoogleBooksClient.search", lambda self, q: [])

    result = runner.invoke(app, ["search", "xyzzy_no_such_book"])
    assert result.exit_code == 0
    assert "No books found." in result.output


def test_list_command_timing_flag(tmp_path):
    vault_path = tmp_path / "my_vault"
    vault_path.mkdir()

    # Valid book note (should be listed)
    book_file = vault_path / "Book.md"
    book_file.write_text("---\nstatus: To Read\ngoogle_books_id: 123\n---\n", encoding="utf-8")

    from libris.config import set_config
    set_config("vault_path", str(vault_path))

    result = runner.invoke(app, ["list", "--timing"])
    assert result.exit_code == 0
    assert "- Book.md [To Read]" in result.output
    assert "Scan time:" in result.output


class TestBuildSearchQuery:
    """Tests for _build_search_query helper."""

    def test_title_with_author_separator(self):
        from libris.cli import _build_search_query
        result = _build_search_query("The First Rule of Mastery Stop Worrying about What People Think of You - Michael Gervais")
        assert result == "intitle:The First Rule of Mastery Stop Worrying about What People Think of You inauthor:Michael Gervais"

    def test_plain_title_no_separator(self):
        from libris.cli import _build_search_query
        result = _build_search_query("Atomic Habits")
        assert result == "Atomic Habits"

    def test_multiple_separators_splits_on_first(self):
        from libris.cli import _build_search_query
        result = _build_search_query("Title - Subtitle - Author")
        assert result == "intitle:Title inauthor:Subtitle - Author"

    def test_empty_string(self):
        from libris.cli import _build_search_query
        result = _build_search_query("")
        assert result == ""
