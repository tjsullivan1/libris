import sys
from datetime import date
from pathlib import Path

import pytest
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


def test_config_reports_an_unset_vault_rather_than_guessing(tmp_path, monkeypatch):
    # Given no configured Shelf, and a working directory that is not one
    monkeypatch.chdir(tmp_path)

    # When a person runs `libris config` to find out what is set
    result = runner.invoke(app, ["config"])

    # Then it says nothing is, instead of naming the directory it was run from
    assert result.exit_code == 0
    assert "Current vault path: Not set" in result.output
    assert str(tmp_path) not in result.output


def test_a_command_refuses_to_treat_the_working_directory_as_the_shelf(
    tmp_path, monkeypatch
):
    # Given no configured Shelf, and a working directory holding a stray note
    monkeypatch.chdir(tmp_path)
    (tmp_path / "Not A Book Note.md").write_text("# Not a Book Note", encoding="utf-8")

    # When a command that reads the Shelf runs
    result = runner.invoke(app, ["list"])

    # Then it stops and says how to configure one, rather than reporting
    # whatever happened to be in the directory it was launched from (#82)
    assert result.exit_code == 1
    assert "No Shelf is configured" in result.output
    assert "libris config --vault" in result.output


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
    # Status is "Read" rather than the "Finished" this test used to pass: the
    # Library defines four statuses and Finished is not one of them (#65). The
    # assertion here is that overrides reach create_book_note, not that any
    # string may be written into a Book Note.
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

    monkeypatch.setattr("questionary.select", lambda *args, **kwargs: _Selection())
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
            "Read",
            "--format",
            "Audiobook",
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
    assert captured["status"] == "Read"
    assert captured["overrides"] == {
        "status": "Read",
        "format": ["Audiobook"],
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


def test_add_refuses_an_unknown_status_before_searching(monkeypatch):
    # Given a search that would fail loudly if it ran
    def _never(*args, **kwargs):
        raise AssertionError("the API was called despite an invalid status")

    monkeypatch.setattr("libris.cli.GoogleBooksClient.search", _never)

    # When a book is added with a status the Library does not define
    result = runner.invoke(app, ["add", "Dune", "--status", "finished"])

    # Then it is refused up front, before the network call and before the user
    # is made to pick a book, and the message says what is allowed
    assert result.exit_code != 0
    assert "finished" in result.output
    assert "To Read" in result.output
    assert "Traceback" not in result.output


def test_add_accepts_more_than_one_format(monkeypatch, tmp_path):
    # Given a book owned on paper and listened to
    from libris.api import BookCandidate

    monkeypatch.setattr(
        "libris.cli.GoogleBooksClient.search",
        lambda self, q: [BookCandidate(title="Changes", authors=["Jim Butcher"])],
    )

    class _Selection:
        def ask(self):
            return "Changes by Jim Butcher"

    monkeypatch.setattr("questionary.select", lambda *a, **k: _Selection())
    monkeypatch.setattr("libris.cli.get_vault_path", lambda: tmp_path)

    captured = {}

    def fake_create(book, vault_path, status, overrides):
        captured["overrides"] = overrides
        return vault_path / "Changes.md"

    monkeypatch.setattr("libris.cli.create_book_note", fake_create)

    # When both are given
    result = runner.invoke(app, ["add", "Changes", "-f", "Physical", "-f", "Audiobook"])

    # Then the CLI can express everything the field holds (ADR 0017)
    assert result.exit_code == 0
    assert captured["overrides"]["format"] == ["Physical", "Audiobook"]


def test_add_refuses_an_unknown_format_before_searching(monkeypatch):
    # Given a search that would fail loudly if it ran
    def _never(*args, **kwargs):
        raise AssertionError("the API was called despite an invalid format")

    monkeypatch.setattr("libris.cli.GoogleBooksClient.search", _never)

    # When a book is added with a format the Library does not define
    result = runner.invoke(app, ["add", "Dune", "-f", "kindle"])

    # Then it is refused up front, and the message says what is allowed
    assert result.exit_code != 0
    assert "Physical" in result.output
    assert "Traceback" not in result.output


def test_cleanup_dry_run_refuses_to_combine_with_rename(tmp_path, monkeypatch):
    # Given a dry run asked to also rename files
    monkeypatch.setattr("libris.cli.get_vault_path", lambda: tmp_path)
    (tmp_path / "Dune - Frank Herbert.md").write_text(
        "---\ntitle: Dune\nauthors:\n  - Frank Herbert\n---\n\n# Dune\n",
        encoding="utf-8",
    )

    # When cleanup runs
    result = runner.invoke(app, ["cleanup", "--dry-run", "--rename"])

    # Then it refuses, because renaming rewrites wikilinks and has no preview:
    # a flag that says it writes nothing has to mean it
    assert result.exit_code != 0
    assert "--rename" in result.output


def test_version_flag_reports_the_installed_version():
    # Given a build whose version is the only way to tell it from another
    from libris import installed_version

    # When the version is asked for
    result = runner.invoke(app, ["--version"])

    # Then it is printed and nothing else runs
    assert result.exit_code == 0
    assert result.output.strip() == installed_version()


def test_version_is_not_unknown_in_a_normal_install():
    # Given libris installed as a distribution, as it is under test
    from libris import installed_version

    # Then the version is readable. "unknown" means the metadata is missing,
    # which is exactly when a stale install goes unnoticed
    assert installed_version() != "unknown"


def test_the_server_reports_the_same_version():
    # Given the daemon's /health, which also reports a version
    pytest.importorskip("fastapi")
    from libris import installed_version
    from libris.server import libris_version

    # Then it is the same one: two definitions of a version is how two builds
    # come to disagree about which they are
    assert libris_version() == installed_version()


def test_the_status_prompt_offers_what_the_library_defines(monkeypatch, tmp_path):
    # Given a Shelf holding one book
    from libris.api import BookCandidate
    from libris.markdown import create_book_note
    from libris.note_format import STATUS_VALUES

    create_book_note(BookCandidate(title="Dune", authors=["Frank Herbert"]), tmp_path)
    monkeypatch.setattr("libris.cli.get_vault_path", lambda: tmp_path)

    offered = []

    class _Selection:
        def __init__(self, choices):
            self._choices = choices

        def ask(self):
            offered.append(self._choices)
            return self._choices[0]

    monkeypatch.setattr(
        "questionary.select",
        lambda _message, choices, **kwargs: _Selection(choices),
    )
    # The book is picked with autocomplete now, not a list to scroll (#43), so
    # both prompts have to be stood in for.
    monkeypatch.setattr(
        "questionary.autocomplete",
        lambda _message, choices, **kwargs: _Selection(choices),
    )

    # When someone updates a book's status
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0, result.output

    # Then the prompt offers exactly the four values a status may hold. It used
    # to offer "Finished", which no note has ever held and which validation now
    # rejects, and to omit "Not To Read" entirely.
    assert offered[-1] == list(STATUS_VALUES)
    assert "Finished" not in offered[-1]


# --- what importing the CLI is allowed to cost (#106) -----------------------

# Every command pays for whatever `libris.cli` imports, before Typer has even
# chosen one. Four imports were top-level and cost ~700ms between them, so
# `libris --version` took the best part of a second to print one string. Each is
# now imported at the point of use. Asserted against a subprocess rather than
# `sys.modules` in this process, because the test suite has already imported
# every one of them itself.
#
# `prompt_toolkit` is here because it is what makes `questionary` expensive, and
# `importlib.metadata` because `ulid` pulls it in - deferring the package's own
# use of it saved nothing until `ulid` moved too.
_DEFERRED = (
    "questionary",
    "prompt_toolkit",
    "httpx",
    "ulid",
    "importlib.metadata",
)


def test_importing_the_cli_does_not_drag_in_the_heavy_optional_stack():
    # Given a fresh interpreter that has imported nothing but the CLI
    import json
    import subprocess

    code = (
        "import sys, json; import libris.cli; "
        f"print(json.dumps([m for m in {_DEFERRED!r} if m in sys.modules]))"
    )

    # When it reports which of the deferred modules got pulled in
    # noqa justified: the command is this interpreter and a literal string built
    # above from a module-level tuple. Nothing here comes from outside the test.
    proc = subprocess.run(  # noqa: S603
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    loaded = json.loads(proc.stdout.strip().splitlines()[-1])

    # Then none of them did. A top-level import of any of these puts ~700ms back
    # on every command, including `--help`, so this fails loudly rather than
    # letting the cost creep back in unnoticed.
    assert loaded == []


def test_the_version_still_reads_from_package_metadata():
    # Given importlib.metadata is no longer imported at package scope
    # When the version is asked for
    version = libris.installed_version()

    # Then it still answers, so deferring the import did not break the thing it
    # was deferred for.
    assert isinstance(version, str) and version


# --- the doctor report (#78) ------------------------------------------------


def test_doctor_reports_lost_characters_without_touching_anything(
    tmp_path, monkeypatch
):
    # Given a Shelf with one damaged note
    vault = tmp_path / "shelf"
    vault.mkdir()
    note = vault / "S�ren.md"
    note.write_text(
        '---\ntitle: Either-Or\nauthors:\n  - "S�ren Kierkegaard"\n'
        "google_books_id: vol1\n---\n\n## Notes\n\nMine.\n",
        encoding="utf-8",
    )
    before = note.read_bytes()
    monkeypatch.setattr("libris.cli.get_vault_path", lambda: vault)

    # When the doctor runs
    result = CliRunner().invoke(app, ["doctor"])

    # Then it reports the damage and says a rename would be needed
    assert result.exit_code == 0
    assert "1 note(s) have lost a character" in result.output
    assert "filename" in result.output
    assert "authors" in result.output
    assert "1 would need a rename" in result.output

    # And the note is untouched
    assert note.read_bytes() == before


def test_doctor_says_so_when_the_shelf_is_clean(tmp_path, monkeypatch):
    # Given a Shelf whose notes are intact, accents and all
    vault = tmp_path / "shelf"
    vault.mkdir()
    (vault / "fine.md").write_text(
        '---\ntitle: Either-Or\nauthors:\n  - "Søren Kierkegaard"\n---\n\nBody.\n',
        encoding="utf-8",
    )
    monkeypatch.setattr("libris.cli.get_vault_path", lambda: vault)

    # When the doctor runs
    result = CliRunner().invoke(app, ["doctor"])

    # Then it says nothing is wrong rather than printing an empty report
    assert result.exit_code == 0
    assert "Nothing on the Shelf needs a decision" in result.output


def test_doctor_reports_a_note_that_is_not_utf8(tmp_path, monkeypatch):
    # Given a Shelf holding one note saved as Latin-1
    vault = tmp_path / "shelf"
    vault.mkdir()
    note = vault / "Kierkegaard.md"
    note.write_bytes("---\ntitle: Søren\n---\n\nMine.\n".encode("latin-1"))
    before = note.read_bytes()
    monkeypatch.setattr("libris.cli.get_vault_path", lambda: vault)

    # When the doctor runs
    result = CliRunner().invoke(app, ["doctor"])

    # Then it names the note and the repair, rather than dying on the decode
    # (#127 review) or calling the Shelf clean
    assert result.exit_code == 0, result.output
    assert "1 note(s) are not UTF-8 text" in result.output
    assert "Kierkegaard.md" in result.output
    assert "Nothing on the Shelf needs a decision" not in result.output

    # And it does not call the intact letters lost, which would send a person to
    # the API for a spelling the file still holds
    assert "lost a character" not in result.output
    assert note.read_bytes() == before


# --- repairing what was lost (#78) ------------------------------------------


def _damaged_shelf(tmp_path):
    """A Shelf holding one note whose author and rendered heading lost a letter."""
    vault = tmp_path / "shelf"
    vault.mkdir()
    note = vault / "Either-Or.md"
    note.write_text(
        '---\ntitle: "Either-Or"\nauthors:\n  - "S�ren Kierkegaard"\n'
        "google_books_id: vol1\n---\n\n# Either-Or\n\n## Notes\n\nMine.\n",
        encoding="utf-8",
    )
    return vault, note


def _answer_text(monkeypatch, answer):
    """Stand in for the prompt `libris repair` asks per damaged string.

    Records the default it was offered, which is where the API's suggestion
    reaches the reader - asserting on the printed output alone would not say
    whether the suggestion was actually the answer they could accept.
    """
    offered = []

    def _text(message, default="", **_kwargs):
        offered.append(default)

        class _Answer:
            def ask(self):
                return answer(default) if callable(answer) else answer

        return _Answer()

    monkeypatch.setattr("questionary.text", _text)
    return offered


def _volume(monkeypatch, candidate):
    """Answer every volume lookup with one candidate, or None for no volume."""
    monkeypatch.setattr(
        "libris.cli.GoogleBooksClient.get_volume", lambda self, gid: candidate
    )


def test_repair_writes_what_the_reader_typed(monkeypatch, tmp_path):
    # Given a damaged note whose volume says nothing that fits - 50 of the real
    # Shelf's 57 damaged strings are like this (#78)
    vault, note = _damaged_shelf(tmp_path)
    monkeypatch.setattr("libris.cli.get_vault_path", lambda: vault)
    _volume(monkeypatch, None)
    _answer_text(monkeypatch, "Søren Kierkegaard")

    # When the reader types the letter themselves
    result = runner.invoke(app, ["repair"])
    assert result.exit_code == 0, result.output

    # Then it is written, and the note carries no replacement character
    text = note.read_text(encoding="utf-8")
    assert "Søren Kierkegaard" in text
    assert "�" not in text
    assert "Repaired 1 note(s)" in result.output


def test_repair_offers_the_volumes_spelling_as_the_default(monkeypatch, tmp_path):
    # Given a damaged note whose volume does hold the spelling
    from libris.api import BookCandidate

    vault, note = _damaged_shelf(tmp_path)
    monkeypatch.setattr("libris.cli.get_vault_path", lambda: vault)
    _volume(
        monkeypatch,
        BookCandidate(
            title="Either-Or", authors=["Søren Kierkegaard"], google_books_id="vol1"
        ),
    )
    # The reader presses Enter, which questionary answers with the default
    offered = _answer_text(monkeypatch, lambda default: default)

    # When the repair runs
    result = runner.invoke(app, ["repair"])
    assert result.exit_code == 0, result.output

    # Then the volume's spelling was what they were offered, so accepting it is
    # one keystroke - and it is what got written
    assert "Søren Kierkegaard" in offered
    assert "Søren Kierkegaard" in note.read_text(encoding="utf-8")
    assert "the volume says" in result.output


def test_repair_leaves_a_string_alone_on_an_empty_answer(monkeypatch, tmp_path):
    # Given a damaged note and a reader who does not know this one either
    vault, note = _damaged_shelf(tmp_path)
    before = note.read_bytes()
    monkeypatch.setattr("libris.cli.get_vault_path", lambda: vault)
    _volume(monkeypatch, None)
    _answer_text(monkeypatch, "")

    # When they answer with nothing
    result = runner.invoke(app, ["repair"])
    assert result.exit_code == 0, result.output

    # Then the note is untouched byte for byte. A letter nobody supplied cannot
    # be invented, and the file no longer says what it was (ADR 0003).
    assert note.read_bytes() == before
    assert "left 1 alone" in result.output


def test_repair_refuses_an_answer_that_still_carries_the_damage(monkeypatch, tmp_path):
    # Given a reader who fixed one lost character and left another, which is
    # easy to do in a description carrying eight of them
    vault, note = _damaged_shelf(tmp_path)
    before = note.read_bytes()
    monkeypatch.setattr("libris.cli.get_vault_path", lambda: vault)
    _volume(monkeypatch, None)
    _answer_text(monkeypatch, "S�ren Kierkegård")

    # When it is answered
    result = runner.invoke(app, ["repair"])
    assert result.exit_code == 0, result.output

    # Then nothing is written: an answer still carrying the replacement
    # character writes the damage back
    assert note.read_bytes() == before
    assert "left 1 alone" in result.output


def test_repair_says_when_a_note_records_that_google_books_lacks_the_book(
    monkeypatch, tmp_path
):
    # Given a damaged note carrying the sentinel someone wrote after looking the
    # book up and finding Google Books has not got it
    vault = tmp_path / "shelf"
    vault.mkdir()
    note = vault / "Absent.md"
    note.write_text(
        '---\ntitle: "S�ren"\ngoogle_books_id: _not_found_in_google_books_api\n'
        "---\n\n## Notes\n\nMine.\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("libris.cli.get_vault_path", lambda: vault)

    def _never(self, gid):
        raise AssertionError(f"asked Google Books about the sentinel id {gid!r}")

    monkeypatch.setattr("libris.cli.GoogleBooksClient.get_volume", _never)
    _answer_text(monkeypatch, "Søren")

    # When the repair runs
    result = runner.invoke(app, ["repair"])

    # Then it reports what the note records rather than spending three retries
    # and a backoff on a malformed lookup, and the reader still supplies the
    # letter
    assert result.exit_code == 0, result.output
    assert "records that Google Books has no such book" in result.output
    assert "title: Søren" in note.read_text(encoding="utf-8")


def test_repair_offers_a_note_with_no_identifier_without_asking_the_api(
    monkeypatch, tmp_path
):
    # Given a damaged note naming neither a volume nor an ISBN
    vault = tmp_path / "shelf"
    vault.mkdir()
    note = vault / "Orphan.md"
    note.write_text('---\ntitle: "S�ren"\n---\n\n## Notes\n\nMine.\n', encoding="utf-8")
    monkeypatch.setattr("libris.cli.get_vault_path", lambda: vault)

    def _never(self, gid):
        raise AssertionError("asked the API about a note naming no volume")

    monkeypatch.setattr("libris.cli.GoogleBooksClient.get_volume", _never)
    _answer_text(monkeypatch, "Søren")

    # When the repair runs
    result = runner.invoke(app, ["repair"])

    # Then it is still offered - the reader is the source, not the API - and the
    # API is not asked about a note that names no volume
    assert result.exit_code == 0, result.output
    assert "names no volume" in result.output
    assert "title: Søren" in note.read_text(encoding="utf-8")


def test_a_long_damaged_string_is_excerpted_around_what_was_lost():
    # Given a description callout of the length the real Shelf holds, damaged in
    # two places far apart
    from libris.cli import _excerpt_damage

    value = "A" * 200 + "�" + "B" * 200 + "�" + "C" * 200

    # When it is rendered for the report
    excerpt = _excerpt_damage(value)

    # Then both losses are shown in context and the filler between them is not
    assert excerpt.count("�") == 2
    assert len(excerpt) < len(value) / 2
    assert "..." in excerpt


def test_a_short_damaged_string_is_shown_whole():
    # Given a damaged author name
    from libris.cli import _excerpt_damage

    # When it is rendered
    # Then it is printed outright - excerpting it would only hide it
    assert _excerpt_damage("S�ren Kierkegaard") == "S�ren Kierkegaard"


def test_doctor_reports_a_contested_identity(tmp_path, monkeypatch):
    # Given two Book Notes claiming one Libris ID and naming one ISBN
    vault = tmp_path / "shelf"
    vault.mkdir()
    for name in ("a.md", "b.md"):
        (vault / name).write_text(
            "---\nlibris_id: 01AAAAAAAAAAAAAAAAAAAAAAAA\ntitle: A Book\n"
            'authors:\n  - An Author\nisbn: "9780000000001"\n---\n\nBody.\n',
            encoding="utf-8",
        )
    before = {p.name: p.read_bytes() for p in vault.glob("*.md")}
    monkeypatch.setattr("libris.cli.get_vault_path", lambda: vault)

    # When the doctor runs
    result = CliRunner().invoke(app, ["doctor"])

    # Then the contested identity is reported, both notes named, and the fact
    # that points at merging travels with it
    assert result.exit_code == 0
    assert "1 Libris ID(s) are claimed by more than one note" in result.output
    assert "01AAAAAAAAAAAAAAAAAAAAAAAA" in result.output
    assert "a.md" in result.output and "b.md" in result.output
    assert "Both name ISBN 9780000000001" in result.output
    assert "Nothing is merged or re-minted here" in result.output

    # And nothing was written
    assert {p.name: p.read_bytes() for p in vault.glob("*.md")} == before


def test_doctor_reports_both_kinds_of_damage_together(tmp_path, monkeypatch):
    # Given a Shelf with a contested identity and, separately, a lost character
    vault = tmp_path / "shelf"
    vault.mkdir()
    for name in ("a.md", "b.md"):
        (vault / name).write_text(
            "---\nlibris_id: 01AAAAAAAAAAAAAAAAAAAAAAAA\ntitle: A Book\n"
            "authors:\n  - An Author\n---\n\nBody.\n",
            encoding="utf-8",
        )
    (vault / "lost.md").write_text(
        "---\nlibris_id: 01BBBBBBBBBBBBBBBBBBBBBBBB\ntitle: Either-Or\n"
        'authors:\n  - "S�ren Kierkegaard"\ngoogle_books_id: vol1\n---\n\nBody.\n',
        encoding="utf-8",
    )
    monkeypatch.setattr("libris.cli.get_vault_path", lambda: vault)

    # When the doctor runs
    result = CliRunner().invoke(app, ["doctor"])

    # Then both are reported. One check finding nothing must not hide the other.
    assert result.exit_code == 0
    assert "claimed by more than one note" in result.output
    assert "have lost a character" in result.output


def test_doctor_does_not_claim_different_books_when_an_isbn_is_missing(
    tmp_path, monkeypatch
):
    # Given two notes contesting an identity where only one names an ISBN
    vault = tmp_path / "shelf"
    vault.mkdir()
    (vault / "has.md").write_text(
        "---\nlibris_id: 01AAAAAAAAAAAAAAAAAAAAAAAA\ntitle: A Book\n"
        'isbn: "9780000000001"\n---\n\nBody.\n',
        encoding="utf-8",
    )
    (vault / "none.md").write_text(
        "---\nlibris_id: 01AAAAAAAAAAAAAAAAAAAAAAAA\ntitle: A Book\n---\n\nBody.\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("libris.cli.get_vault_path", lambda: vault)

    # When the doctor runs
    result = CliRunner().invoke(app, ["doctor"])

    # Then it says it does not know, rather than asserting they differ
    assert result.exit_code == 0
    assert "names no ISBN" in result.output
    # The specific claim, not the bare word: asserting "different" is absent
    # would start failing the day any unrelated line of doctor output used it.
    assert "different ISBNs" not in result.output


# --- `libris status` writes what the MCP tools write (#97, #43) -------------


def _answer_prompts(monkeypatch, filename, new_status):
    """Stand in for the two prompts `libris status` asks.

    Each prompt is patched with its own answer rather than one stand-in that
    decides by looking at `choices`. The first version did that, and it made
    the tests below meaningless: a filename deliberately absent from `choices` -
    which is the whole point of typing something the Shelf does not hold - fell
    through and answered the *status* prompt instead, so the command never saw
    the name under test and the assertion passed for the wrong reason.
    """

    def _answering(value):
        def _ask(_message, choices=None, **kwargs):
            class _Answer:
                def ask(self):
                    return value

            return _Answer()

        return _ask

    monkeypatch.setattr("questionary.autocomplete", _answering(filename))
    monkeypatch.setattr("questionary.select", _answering(new_status))


def test_marking_a_book_reading_stamps_the_date_it_was_started(monkeypatch, tmp_path):
    # Given a book on the Shelf that has not been started
    from libris.api import BookCandidate
    from libris.markdown import create_book_note, read_frontmatter

    path = create_book_note(
        BookCandidate(title="Dune", authors=["Frank Herbert"]), tmp_path
    )
    monkeypatch.setattr("libris.cli.get_vault_path", lambda: tmp_path)
    _answer_prompts(monkeypatch, path.name, "Reading")

    # When it is marked Reading from the CLI
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0, result.output

    # Then the date is stamped, as it already was through MCP. The same act left
    # two different notes depending on which Surface did it (#97).
    frontmatter = read_frontmatter(path)
    assert frontmatter["status"] == "Reading"
    assert frontmatter["date_started"] == date.today().isoformat()

    # And the reader is told, because a stamped date is indistinguishable from a
    # stated one afterwards (ADR 0024).
    assert "date_started" in result.output


def test_marking_a_book_read_stamps_the_date_it_was_finished(monkeypatch, tmp_path):
    # Given a book on the Shelf
    from libris.api import BookCandidate
    from libris.markdown import create_book_note, read_frontmatter

    path = create_book_note(
        BookCandidate(title="Dune", authors=["Frank Herbert"]), tmp_path
    )
    monkeypatch.setattr("libris.cli.get_vault_path", lambda: tmp_path)
    _answer_prompts(monkeypatch, path.name, "Read")

    # When it is marked Read
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0, result.output

    # Then the finish date is stamped
    assert read_frontmatter(path)["date_finished"] == date.today().isoformat()


def test_a_date_already_set_is_not_overwritten(monkeypatch, tmp_path):
    # Given a book already marked as started on a day the reader stated
    from libris.api import BookCandidate
    from libris.markdown import (
        create_book_note,
        read_frontmatter,
        set_frontmatter_fields,
    )

    path = create_book_note(
        BookCandidate(title="Dune", authors=["Frank Herbert"]), tmp_path
    )
    set_frontmatter_fields(path, {"date_started": "2019-03-12"})
    monkeypatch.setattr("libris.cli.get_vault_path", lambda: tmp_path)
    _answer_prompts(monkeypatch, path.name, "Reading")

    # When it is marked Reading again
    assert runner.invoke(app, ["status"]).exit_code == 0

    # Then the reader's own date stands. Re-marking a dated book must not
    # rewrite it - a re-read is not something the Library models.
    assert read_frontmatter(path)["date_started"] == "2019-03-12"


def test_a_note_without_an_identity_is_updated_rather_than_refused(
    monkeypatch, tmp_path
):
    # Given a note written by hand outside Libris, carrying no libris_id
    from libris.markdown import read_frontmatter

    path = tmp_path / "Handwritten.md"
    path.write_text(
        "---\ntitle: Handwritten\nauthors:\n  - Someone\nstatus: To Read\n"
        "date_added: 2019-03-12\n---\n\n## Notes\n\nMine.\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("libris.cli.get_vault_path", lambda: tmp_path)
    _answer_prompts(monkeypatch, path.name, "Reading")

    # When its status is updated
    result = runner.invoke(app, ["status"])

    # Then the update goes through. Refusing to set a status would be a strange
    # place for a reader to learn their note lacked an identity - and a write by
    # path does not need one (#125).
    assert result.exit_code == 0, result.output
    frontmatter = read_frontmatter(path)
    assert frontmatter["status"] == "Reading"

    # And none is minted. Setting a status is not the place to decide a note's
    # identity, and minting existed only to feed a lookup this command no longer
    # makes.
    assert "libris_id" not in frontmatter


def test_status_reports_a_note_gone_before_the_write(monkeypatch, tmp_path):
    # Given a picked note that is removed before the write reaches it
    from libris import service
    from libris.api import BookCandidate
    from libris.markdown import create_book_note

    path = create_book_note(
        BookCandidate(title="Dune", authors=["Frank Herbert"]), tmp_path
    )
    real = service.set_frontmatter_fields

    def _removed_first(target, updates):
        target.unlink()
        real(target, updates)

    monkeypatch.setattr(service, "set_frontmatter_fields", _removed_first)
    monkeypatch.setattr("libris.cli.get_vault_path", lambda: tmp_path)
    _answer_prompts(monkeypatch, path.name, "Reading")

    # When its status is set
    result = runner.invoke(app, ["status"])

    # Then the command says so and exits, rather than ending in a traceback
    # (#127 review). An exception escaping into CliRunner leaves exit code 1
    # too, so the output is what tells the two apart.
    assert result.exit_code == 1
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert f"No Book Note is at {path.name}" in result.output
    assert not path.exists()


def test_status_reads_only_the_note_it_was_asked_about(monkeypatch, tmp_path):
    # Given a Shelf of several books, and a record of every note parsed
    from libris import markdown
    from libris.api import BookCandidate

    vault = tmp_path / "shelf"
    vault.mkdir()
    paths = [
        markdown.create_book_note(
            BookCandidate(title=title, authors=["Someone"]), vault
        )
        for title in ("Dune", "Piranesi", "Mercy", "Hild")
    ]
    chosen = paths[1]

    parsed = []
    real = markdown.read_frontmatter
    monkeypatch.setattr(
        markdown, "read_frontmatter", lambda path: (parsed.append(path), real(path))[1]
    )
    monkeypatch.setattr("libris.cli.get_vault_path", lambda: vault)
    _answer_prompts(monkeypatch, chosen.name, "Reading")

    # When one of them has its status set
    result = runner.invoke(app, ["status"])
    assert result.exit_code == 0, result.output

    # Then no other note was parsed, and the chosen one was written. The command
    # is handed the note, and resolving it by identity parsed the whole Shelf to
    # find it again - 3,065 parses and 6 to 14 seconds against the real one
    # (#125).
    assert {Path(p) for p in parsed} <= {chosen}
    assert real(chosen)["status"] == "Reading"


def test_the_readers_own_writing_survives_a_status_change(monkeypatch, tmp_path):
    # Given a note whose body is the reader's own
    from libris.api import BookCandidate
    from libris.markdown import create_book_note

    path = create_book_note(
        BookCandidate(title="Dune", authors=["Frank Herbert"]), tmp_path
    )
    body = (
        "\n## Notes\n\n    an indented block\n\nAnd a line saying status: unreliable.\n"
    )
    path.write_text(
        path.read_text(encoding="utf-8").split("\n---\n")[0] + "\n---\n" + body,
        encoding="utf-8",
    )
    monkeypatch.setattr("libris.cli.get_vault_path", lambda: tmp_path)
    _answer_prompts(monkeypatch, path.name, "Read")

    # When the status changes
    assert runner.invoke(app, ["status"]).exit_code == 0

    # Then the body is untouched - indentation, stray "status:" and all. This is
    # #92 and #99 in the one command that now writes through a different path.
    assert path.read_text(encoding="utf-8").endswith(body)


# --- a typed answer is not a path (#124 review) -----------------------------


@pytest.mark.parametrize(
    "typed",
    [
        "../../escaped.md",
        "nonexistent.md",
        "Dune - Frank Herbert.md.bak",
    ],
    ids=["walks-out", "not-on-the-shelf", "near-miss"],
)
def test_status_refuses_a_name_that_is_not_on_the_shelf(monkeypatch, tmp_path, typed):
    # Given a Shelf with one book, and a file above it that must not be touched
    from libris.api import BookCandidate
    from libris.markdown import create_book_note

    vault = tmp_path / "shelf"
    vault.mkdir()
    create_book_note(BookCandidate(title="Dune", authors=["Frank Herbert"]), vault)
    outside = tmp_path / "escaped.md"
    outside.write_text(
        "---\ntitle: Not a Book Note\n---\n\nLeave me alone.\n", encoding="utf-8"
    )
    before = outside.read_bytes()

    monkeypatch.setattr("libris.cli.get_vault_path", lambda: vault)
    _answer_prompts(monkeypatch, typed, "Reading")

    # When someone types it at the prompt, which autocomplete allows because it
    # offers completions but returns free text
    result = runner.invoke(app, ["status"])

    # Then nothing is written and it says why. `vault / "../../escaped.md"`
    # walks out of the Shelf, and an absolute name replaces it outright, so
    # the status could have been written into a file that is not a note.
    assert result.exit_code == 1
    assert "is on the Shelf" in result.output
    assert outside.read_bytes() == before


def test_status_refuses_an_absolute_path(monkeypatch, tmp_path):
    # Given a Shelf, and a file elsewhere entirely
    from libris.api import BookCandidate
    from libris.markdown import create_book_note

    vault = tmp_path / "shelf"
    vault.mkdir()
    create_book_note(BookCandidate(title="Dune", authors=["Frank Herbert"]), vault)
    outside = tmp_path / "elsewhere.md"
    outside.write_text("---\ntitle: Elsewhere\n---\n\nMine.\n", encoding="utf-8")
    before = outside.read_bytes()

    monkeypatch.setattr("libris.cli.get_vault_path", lambda: vault)
    _answer_prompts(monkeypatch, str(outside), "Reading")

    # When an absolute path is typed
    result = runner.invoke(app, ["status"])

    # Then it is refused. `Path(shelf) / "/abs/path"` is the absolute path, so
    # joining discards the Shelf entirely rather than nesting under it.
    assert result.exit_code == 1
    assert outside.read_bytes() == before


def test_enrich_refuses_a_filename_that_is_not_on_the_shelf(monkeypatch, tmp_path):
    # Given a Shelf and a file outside it
    vault = tmp_path / "shelf"
    vault.mkdir()
    (vault / "Dune - Frank Herbert.md").write_text(
        "---\ntitle: Dune\n---\n\nBody.\n", encoding="utf-8"
    )
    outside = tmp_path / "elsewhere.md"
    outside.write_text("---\ntitle: Elsewhere\n---\n\nMine.\n", encoding="utf-8")
    monkeypatch.setattr("libris.cli.get_vault_path", lambda: vault)

    # When it is passed as the argument, which takes a filename and not a path
    result = runner.invoke(app, ["enrich", str(outside)])

    # Then it is refused rather than enriched
    assert result.exit_code == 1
    assert "is on the Shelf" in result.output


def test_status_writes_the_note_picked_when_another_claims_its_identity(
    monkeypatch, tmp_path
):
    # Given two notes claiming one Libris ID, as the real Shelf holds (#75)
    from libris.markdown import read_frontmatter

    vault = tmp_path / "shelf"
    vault.mkdir()
    for name in ("Aaa First.md", "Zzz Second.md"):
        (vault / name).write_text(
            "---\nlibris_id: 01AAAAAAAAAAAAAAAAAAAAAAAA\n"
            f"title: {name[:-3]}\nauthors:\n  - Someone\nstatus: To Read\n---\n\nMine.\n",
            encoding="utf-8",
        )
    monkeypatch.setattr("libris.cli.get_vault_path", lambda: vault)
    _answer_prompts(monkeypatch, "Zzz Second.md", "Read")

    # When the second of them is picked
    result = runner.invoke(app, ["status"])

    # Then the note picked is the note written. Resolved by identity, this wrote
    # to the first claimant while reporting the one that was picked - a write to
    # the wrong book announced as a write to the right one (ADR 0003). #124
    # refused the write to stop that; writing by path cannot land elsewhere, so
    # there is nothing left to refuse (#125).
    assert result.exit_code == 0, result.output
    assert "Zzz Second.md" in result.output
    assert read_frontmatter(vault / "Zzz Second.md")["status"] == "Read"
    assert read_frontmatter(vault / "Aaa First.md")["status"] == "To Read"


def test_status_refuses_a_note_that_is_a_link_out_of_the_shelf(monkeypatch, tmp_path):
    # Given a file outside the Shelf, and an in-vault note that is a link to it
    vault = tmp_path / "shelf"
    vault.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text(
        "---\ntitle: Outside\nauthors:\n  - Someone\nstatus: To Read\n---\n\nMine.\n",
        encoding="utf-8",
    )
    link = vault / "Linked.md"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("this platform does not permit creating a symlink unprivileged")

    before = outside.read_bytes()
    monkeypatch.setattr("libris.cli.get_vault_path", lambda: vault)
    _answer_prompts(monkeypatch, "Linked.md", "Reading")

    # When it is picked
    result = runner.invoke(app, ["status"])

    # Then nothing outside the Shelf is written. Checking the name against the
    # Shelf says the name is there; it says nothing about where the file leads,
    # and `list_books` follows a symlink when it decides what is a file.
    assert result.exit_code == 1
    assert "outside the Shelf" in result.output
    assert outside.read_bytes() == before


def test_the_book_picker_matches_anywhere_in_the_name(monkeypatch, tmp_path):
    # Given a Shelf
    from libris.api import BookCandidate
    from libris.markdown import create_book_note

    vault = tmp_path / "shelf"
    vault.mkdir()
    path = create_book_note(
        BookCandidate(title="Dune", authors=["Frank Herbert"]), vault
    )
    monkeypatch.setattr("libris.cli.get_vault_path", lambda: vault)

    asked = {}

    def _autocomplete(_message, choices=None, **kwargs):
        asked.update(kwargs)

        class _Answer:
            def ask(self):
                return path.name

        return _Answer()

    monkeypatch.setattr("questionary.autocomplete", _autocomplete)
    monkeypatch.setattr(
        "questionary.select",
        lambda _m, choices=None, **k: type("A", (), {"ask": lambda s: "Reading"})(),
    )

    # When a book is picked
    assert runner.invoke(app, ["status"]).exit_code == 0

    # Then the prompt matches anywhere in the filename, not only at the start.
    # Every other test here stubs the prompt and ignores its arguments, so
    # deleting this option would have left the suite green while #43 quietly
    # regressed to prefix-only matching - and a Shelf of "The ..." titles is
    # exactly where that is useless.
    assert asked.get("match_middle") is True


def test_a_shelf_that_is_not_there_is_reported_rather_than_raised(
    monkeypatch, tmp_path
):
    # Given a configured Shelf that has since been deleted
    gone = tmp_path / "deleted-shelf"
    monkeypatch.setattr("libris.cli.get_vault_path", lambda: gone)

    # When a command that scans it runs
    result = runner.invoke(app, ["status"])

    # Then it says so. Configured is not the same as present, and every command
    # otherwise reached its first scan and ended in a traceback naming
    # os.scandir. `enrich` used to report this and stopped when its own
    # File-not-found check was replaced by the Shelf membership check.
    assert result.exit_code == 1
    assert "The Shelf is not there" in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)
