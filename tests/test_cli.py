import sys

import yaml
from typer.testing import CliRunner

import libris
from libris import config
from libris.cli import app

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
    legacy_file.write_text(
        "---\ntitle: Legacy\nstatus: To Read\ngoogle_books_id: 123\n---\n"
    )

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
    from libris.api import BookCandidate

    mock_books = [
        BookCandidate(
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
    monkeypatch.setattr(
        "libris.cli.GoogleBooksClient.search", lambda self, q: mock_books
    )

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
    from libris.api import BookCandidate

    captured = {}

    def fake_search(self, q):
        captured["query"] = q
        return [
            BookCandidate(
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
    from libris.api import BookCandidate

    captured = {}

    def fake_search(self, q):
        captured["query"] = q
        return [
            BookCandidate(
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
    from libris.api import BookCandidate

    captured = {}

    def fake_search(self, q):
        captured["query"] = q
        return [
            BookCandidate(
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


def test_add_command_passes_cli_overrides(monkeypatch, tmp_path):
    from libris.api import BookCandidate

    mock_books = [
        BookCandidate(
            title="Dune",
            authors=["Frank Herbert"],
            isbn="9780441013593",
            page_count=412,
            published_date="1965",
            google_books_id="dune1",
            thumbnail=None,
            genres=["Science Fiction"],
            description=None,
        )
    ]
    monkeypatch.setattr(
        "libris.cli.GoogleBooksClient.search", lambda self, q: mock_books
    )

    choice = "Dune by Frank Herbert"

    class _Selection:
        def ask(self):
            return choice

    monkeypatch.setattr(
        "libris.cli.questionary.select", lambda *args, **kwargs: _Selection()
    )
    monkeypatch.setattr("libris.cli.get_vault_path", lambda: tmp_path)

    captured = {}

    def fake_create_book_note(book, vault_path, status, overrides):
        captured["book"] = book
        captured["vault_path"] = vault_path
        captured["status"] = status
        captured["overrides"] = overrides
        return vault_path / "Dune.md"

    monkeypatch.setattr("libris.cli.create_book_note", fake_create_book_note)

    result = runner.invoke(
        app,
        [
            "add",
            "dune",
            "--status",
            "Finished",
            "--format",
            "kindle",
            "--rating",
            "5",
            "--referred-by",
            "Alice",
            "--tags",
            "Sci-Fi,Classic",
            "--date-started",
            "2026-01-01",
            "--date-finished",
            "2026-01-10",
        ],
    )

    assert result.exit_code == 0
    assert captured["book"] == mock_books[0]
    assert captured["vault_path"] == tmp_path
    assert captured["status"] == "Finished"
    assert captured["overrides"] == {
        "status": "Finished",
        "format": "kindle",
        "rating": 5,
        "referred_by": "Alice",
        "tags": "Sci-Fi,Classic",
        "date_started": "2026-01-01",
        "date_finished": "2026-01-10",
    }
    assert "Added:" in result.output


def test_list_command_timing_flag(tmp_path):
    vault_path = tmp_path / "my_vault"
    vault_path.mkdir()

    # Valid book note (should be listed)
    book_file = vault_path / "Book.md"
    book_file.write_text(
        "---\nstatus: To Read\ngoogle_books_id: 123\n---\n", encoding="utf-8"
    )

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

        result = _build_search_query(
            "The First Rule of Mastery Stop Worrying about What People Think of You - Michael Gervais"
        )
        assert (
            result
            == "intitle:The First Rule of Mastery Stop Worrying about What People Think of You inauthor:Michael Gervais"
        )

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


# --- serve ---


def test_serve_reports_a_missing_server_extra_clearly(monkeypatch):
    # Given libris installed without the server extra. Both the module cache and
    # the attribute on the package have to go: `from . import server` resolves
    # via the parent attribute when a previous test has already imported it.
    monkeypatch.setitem(sys.modules, "fastapi", None)
    monkeypatch.delitem(sys.modules, "libris.server", raising=False)
    monkeypatch.delattr(libris, "server", raising=False)

    # When the daemon is started
    result = runner.invoke(app, ["serve"])

    # Then it says what to install rather than raising an ImportError at the user
    assert result.exit_code != 0
    assert "libris[server]" in result.output
    assert "Traceback" not in result.output


def test_serve_show_token_prints_the_token():
    # Given no token yet configured
    # When the token is asked for
    result = runner.invoke(app, ["serve", "--show-token"])

    # Then it is generated, printed, and the daemon does not start
    assert result.exit_code == 0
    assert config.get_server_token() in result.output
