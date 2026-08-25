import yaml
from typer.testing import CliRunner

from libris.api import BookCandidate
from libris.cli import app

runner = CliRunner()


def _make_book(**overrides):
    defaults = dict(
        title="The Great Gatsby",
        authors=["F. Scott Fitzgerald"],
        isbn="9780743273565",
        page_count=180,
        published_date="1925-04-10",
        google_books_id="gatsby1",
        thumbnail="http://img.example.com/gatsby.jpg",
        genres=["Classic"],
        description="A novel about the American dream.",
    )
    defaults.update(overrides)
    return BookCandidate(**defaults)


def _write_book(vault, name, fm_overrides=None):
    fm = {
        "title": "The Great Gatsby",
        "authors": ["F. Scott Fitzgerald"],
        "isbn": None,
        "page_count": None,
        "date_published": None,
        "google_books_id": None,
        "cover_thumbnail": None,
        "genres": None,
        "tags": "Book",
        "format": None,
        "status": "To Read",
        "rating": None,
        "referred_by": None,
        "date_added": None,
        "date_started": None,
        "date_finished": None,
    }
    if fm_overrides:
        fm.update(fm_overrides)
    content = (
        f"---\n{yaml.dump(fm, sort_keys=False, allow_unicode=True)}---\n\n## Notes\n"
    )
    path = vault / name
    path.write_text(content, encoding="utf-8")
    return path


def _setup_vault(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    from libris.config import set_config

    set_config("vault_path", str(vault))
    return vault


# ── Single match → auto-enriched ────────────────────────────────────


def test_autoenrich_single_match(tmp_path, monkeypatch):
    vault = _setup_vault(tmp_path)
    _write_book(vault, "The Great Gatsby - F. Scott Fitzgerald.md")

    monkeypatch.setattr(
        "libris.cli.GoogleBooksClient.search",
        lambda self, q: [_make_book()],
    )

    result = runner.invoke(app, ["autoenrich"])
    assert result.exit_code == 0
    assert "auto-enriched" in result.output
    assert "Enriched (auto): 1" in result.output

    fm = yaml.safe_load(
        (vault / "The Great Gatsby - F. Scott Fitzgerald.md")
        .read_text(encoding="utf-8")
        .split("---")[1]
    )
    assert fm["isbn"] == "9780743273565"
    assert fm["google_books_id"] == "gatsby1"


# ── Multiple results, one confident title match → auto-enriched ─────


def test_autoenrich_multiple_results_one_confident(tmp_path, monkeypatch):
    vault = _setup_vault(tmp_path)
    _write_book(
        vault,
        "Dune - Frank Herbert.md",
        {"title": "Dune", "authors": ["Frank Herbert"]},
    )

    dune = _make_book(
        title="Dune",
        authors=["Frank Herbert"],
        google_books_id="dune1",
        isbn="9780441013593",
    )
    other = _make_book(
        title="Dune Messiah",
        authors=["Frank Herbert"],
        google_books_id="dune2",
        isbn="9780441172696",
    )

    monkeypatch.setattr(
        "libris.cli.GoogleBooksClient.search",
        lambda self, q: [dune, other],
    )

    result = runner.invoke(app, ["autoenrich"])
    assert result.exit_code == 0
    assert "auto-enriched" in result.output
    assert "Enriched (auto): 1" in result.output


# ── Multiple title matches → picks most complete metadata ───────────


def test_autoenrich_prefers_most_complete_edition(tmp_path, monkeypatch):
    vault = _setup_vault(tmp_path)
    _write_book(
        vault,
        "Dune - Frank Herbert.md",
        {"title": "Dune", "authors": ["Frank Herbert"]},
    )

    sparse = _make_book(
        title="Dune",
        authors=["Frank Herbert"],
        google_books_id="dune_sparse",
        isbn=None,
        page_count=None,
        thumbnail=None,
        genres=[],
        description=None,
    )
    complete = _make_book(
        title="Dune",
        authors=["Frank Herbert"],
        google_books_id="dune_complete",
        isbn="9780441013593",
        page_count=412,
        thumbnail="http://img.example.com/dune.jpg",
        genres=["Science Fiction"],
        description="A science fiction masterpiece.",
    )

    monkeypatch.setattr(
        "libris.cli.GoogleBooksClient.search",
        lambda self, q: [sparse, complete],  # sparse is first, but complete should win
    )

    result = runner.invoke(app, ["autoenrich"])
    assert result.exit_code == 0
    assert "auto-enriched" in result.output

    fm = yaml.safe_load(
        (vault / "Dune - Frank Herbert.md").read_text(encoding="utf-8").split("---")[1]
    )
    assert fm["google_books_id"] == "dune_complete"
    assert fm["isbn"] == "9780441013593"


# ── Multiple plausible results without --interactive → logged ────────


def test_autoenrich_multiple_no_interactive_logs(tmp_path, monkeypatch):
    vault = _setup_vault(tmp_path)
    _write_book(
        vault,
        "Ambiguous Book.md",
        {"title": "Ambiguous Book", "authors": ["Some Author"]},
    )

    book_a = _make_book(title="Ambiguous Book Vol 1", google_books_id="a1")
    book_b = _make_book(title="Ambiguous Book Vol 2", google_books_id="a2")

    monkeypatch.setattr(
        "libris.cli.GoogleBooksClient.search",
        lambda self, q: [book_a, book_b],
    )

    result = runner.invoke(app, ["autoenrich"])
    assert result.exit_code == 0
    assert "needs interactive selection" in result.output
    assert "rerun with --interactive" in result.output
    assert "Ambiguous Book.md" in result.output


# ── Multiple plausible results with --interactive → user selects ─────


def test_autoenrich_multiple_with_interactive(tmp_path, monkeypatch):
    vault = _setup_vault(tmp_path)
    _write_book(
        vault,
        "Ambiguous Book.md",
        {"title": "Ambiguous Book", "authors": ["Some Author"]},
    )

    book_a = _make_book(title="Ambiguous Book Vol 1", google_books_id="a1", isbn="111")
    book_b = _make_book(title="Ambiguous Book Vol 2", google_books_id="a2", isbn="222")

    monkeypatch.setattr(
        "libris.cli.GoogleBooksClient.search",
        lambda self, q: [book_a, book_b],
    )

    # Simulate questionary.select choosing the second option
    monkeypatch.setattr(
        "libris.cli.questionary.select",
        lambda *a, **kw: type(
            "Ask",
            (),
            {"ask": lambda self: "Ambiguous Book Vol 2 by F. Scott Fitzgerald"},
        )(),
    )

    result = runner.invoke(app, ["autoenrich", "--interactive"])
    assert result.exit_code == 0
    assert "Enriched (interactive): 1" in result.output

    fm = yaml.safe_load(
        (vault / "Ambiguous Book.md").read_text(encoding="utf-8").split("---")[1]
    )
    assert fm["isbn"] == "222"


# ── Interactive skip → book left unchanged ──────────────────────────


def test_autoenrich_interactive_skip(tmp_path, monkeypatch):
    vault = _setup_vault(tmp_path)
    book_path = _write_book(
        vault, "Skip Me.md", {"title": "Skip Me", "authors": ["Author"]}
    )
    original_content = book_path.read_text(encoding="utf-8")

    book_a = _make_book(title="Skip Me Vol 1", google_books_id="s1", isbn="111")
    book_b = _make_book(title="Skip Me Vol 2", google_books_id="s2", isbn="222")

    monkeypatch.setattr(
        "libris.cli.GoogleBooksClient.search",
        lambda self, q: [book_a, book_b],
    )

    # Simulate choosing the skip option
    monkeypatch.setattr(
        "libris.cli.questionary.select",
        lambda *a, **kw: type("Ask", (), {"ask": lambda self: "[ Skip this book ]"})(),
    )

    result = runner.invoke(app, ["autoenrich", "--interactive"])
    assert result.exit_code == 0
    assert (
        "Enriched (interactive): 0" not in result.output
        or "Enriched (auto): 0" in result.output
    )
    assert book_path.read_text(encoding="utf-8") == original_content


# ── Zero results → unmatched ────────────────────────────────────────


def test_autoenrich_no_results(tmp_path, monkeypatch):
    vault = _setup_vault(tmp_path)
    _write_book(
        vault, "Obscure Book.md", {"title": "Obscure Book", "authors": ["Nobody"]}
    )

    monkeypatch.setattr(
        "libris.cli.GoogleBooksClient.search",
        lambda self, q: [],
    )

    result = runner.invoke(app, ["autoenrich"])
    assert result.exit_code == 0
    assert "no results" in result.output
    assert "Obscure Book.md" in result.output
    assert "had no results" in result.output


# ── Already enriched → skipped ──────────────────────────────────────


def test_autoenrich_skips_complete_books(tmp_path, monkeypatch):
    vault = _setup_vault(tmp_path)
    _write_book(
        vault,
        "Complete Book.md",
        {
            "title": "Complete Book",
            "authors": ["Author"],
            "isbn": "123",
            "page_count": 300,
            "date_published": "2020",
            "google_books_id": "gid1",
            "genres": ["Fiction"],
            "cover_thumbnail": "http://img.example.com/thumb.jpg",
        },
    )

    search_called = []
    monkeypatch.setattr(
        "libris.cli.GoogleBooksClient.search",
        lambda self, q: search_called.append(q) or [],
    )

    result = runner.invoke(app, ["autoenrich"])
    assert result.exit_code == 0
    assert "already complete" in result.output
    assert len(search_called) == 0, "Should not call API for already-enriched books"


def test_autoenrich_skips_book_with_google_id_but_missing_fields(tmp_path, monkeypatch):
    """A book with google_books_id but missing thumbnail/genres should still be skipped."""
    vault = _setup_vault(tmp_path)
    _write_book(
        vault,
        "Partial Book.md",
        {
            "title": "Partial Book",
            "authors": ["Author"],
            "google_books_id": "gid2",
            "isbn": None,
            "cover_thumbnail": None,
        },
    )

    search_called = []
    monkeypatch.setattr(
        "libris.cli.GoogleBooksClient.search",
        lambda self, q: search_called.append(q) or [],
    )

    result = runner.invoke(app, ["autoenrich"])
    assert result.exit_code == 0
    assert len(search_called) == 0, (
        "Should not re-enrich a book that already has a google_books_id"
    )


def test_autoenrich_skips_book_with_empty_google_id_but_thumbnail(
    tmp_path, monkeypatch
):
    """A book with empty google_books_id but populated thumbnail should be skipped."""
    vault = _setup_vault(tmp_path)
    _write_book(
        vault,
        "1776 - David McCullough.md",
        {
            "title": "1776",
            "authors": ["David McCullough"],
            "google_books_id": "",
            "isbn": None,
            "cover_thumbnail": "http://books.google.com/books/content?id=yIIbAQAAMAAJ",
            "date_published": "2007-10-02",
            "page_count": 294,
        },
    )

    search_called = []
    monkeypatch.setattr(
        "libris.cli.GoogleBooksClient.search",
        lambda self, q: search_called.append(q) or [],
    )

    result = runner.invoke(app, ["autoenrich"])
    assert result.exit_code == 0
    assert len(search_called) == 0, (
        "Should not re-enrich a book that has thumbnail/published_date"
    )


# ── ISBN fallback when title/author search fails ────────────────────


def test_autoenrich_isbn_fallback(tmp_path, monkeypatch):
    vault = _setup_vault(tmp_path)
    _write_book(
        vault,
        "ISBN Book.md",
        {
            "title": "ISBN Book",
            "authors": ["Author"],
            "isbn": "9780451524935",
        },
    )

    queries = []

    def fake_search(self, q):
        queries.append(q)
        if q.startswith("isbn:"):
            return [
                _make_book(
                    title="ISBN Book", isbn="9780451524935", google_books_id="isbn1"
                )
            ]
        return []

    monkeypatch.setattr("libris.cli.GoogleBooksClient.search", fake_search)

    result = runner.invoke(app, ["autoenrich"])
    assert result.exit_code == 0
    assert "auto-enriched" in result.output
    assert len(queries) == 2
    assert queries[1] == "isbn:9780451524935"


def test_autoenrich_no_isbn_no_fallback(tmp_path, monkeypatch):
    """When ISBN is not in frontmatter, no fallback search occurs."""
    vault = _setup_vault(tmp_path)
    _write_book(vault, "No ISBN.md", {"title": "No ISBN", "authors": ["Author"]})

    queries = []

    def fake_search(self, q):
        queries.append(q)
        return []

    monkeypatch.setattr("libris.cli.GoogleBooksClient.search", fake_search)

    result = runner.invoke(app, ["autoenrich"])
    assert result.exit_code == 0
    assert "no results" in result.output
    assert len(queries) == 1, "Should not attempt ISBN fallback without an ISBN"


# ── --dry-run does not modify files ─────────────────────────────────


def test_autoenrich_dry_run(tmp_path, monkeypatch):
    vault = _setup_vault(tmp_path)
    book_path = _write_book(
        vault, "Dry Run Book.md", {"title": "Dry Run Book", "authors": ["Author"]}
    )
    original_content = book_path.read_text(encoding="utf-8")

    result = runner.invoke(app, ["autoenrich", "--dry-run"])
    assert result.exit_code == 0
    assert "would search" in result.output
    assert "Dry run:" in result.output
    assert book_path.read_text(encoding="utf-8") == original_content


# ── --limit stops after N actions ───────────────────────────────────


def test_autoenrich_limit(tmp_path, monkeypatch):
    vault = _setup_vault(tmp_path)
    _write_book(vault, "Book A.md", {"title": "Book A", "authors": ["Author"]})
    _write_book(vault, "Book B.md", {"title": "Book B", "authors": ["Author"]})
    _write_book(vault, "Book C.md", {"title": "Book C", "authors": ["Author"]})

    monkeypatch.setattr(
        "libris.cli.GoogleBooksClient.search",
        lambda self, q: [_make_book()],
    )

    result = runner.invoke(app, ["autoenrich", "--limit", "2"])
    assert result.exit_code == 0
    assert "Limit reached (2)" in result.output
    assert "Enriched (auto): 2" in result.output


# ── Query uses frontmatter title+author when available ──────────────


def test_autoenrich_query_from_frontmatter(tmp_path, monkeypatch):
    vault = _setup_vault(tmp_path)
    _write_book(
        vault, "renamed-file.md", {"title": "The Hobbit", "authors": ["J.R.R. Tolkien"]}
    )

    captured = {}

    def fake_search(self, q):
        captured["query"] = q
        return [
            _make_book(
                title="The Hobbit",
                authors=["J.R.R. Tolkien"],
                google_books_id="hobbit1",
            )
        ]

    monkeypatch.setattr("libris.cli.GoogleBooksClient.search", fake_search)

    result = runner.invoke(app, ["autoenrich"])
    assert result.exit_code == 0
    assert "intitle:The Hobbit" in captured["query"]
    assert "inauthor:J.R.R. Tolkien" in captured["query"]
