import json
from pathlib import Path

from typer.testing import CliRunner

from libris.cli import app
from libris.config import set_config
from libris.markdown import BookNote, find_duplicate_candidates, find_duplicates

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
    _write_book(
        tmp_path,
        "A.md",
        title="Same Title",
        isbn="111",
        google_books_id="a1",
        authors=["Author A"],
    )
    _write_book(
        tmp_path,
        "B.md",
        title="same title",
        isbn="222",
        google_books_id="b2",
        authors=["Author A"],
    )
    groups = find_duplicates(tmp_path)
    assert len(groups) == 1
    names = {p.name for p in groups[0]}
    assert names == {"A.md", "B.md"}


def test_same_title_different_author_not_duplicate(tmp_path):
    _write_book(tmp_path, "A.md", title="Zero Day", isbn="111", authors=["Author A"])
    _write_book(tmp_path, "B.md", title="Zero Day", isbn="222", authors=["Author B"])
    assert find_duplicates(tmp_path) == []


def test_same_title_missing_author_is_duplicate(tmp_path):
    """A book with no author should match same-titled books as a potential duplicate."""
    _write_book(tmp_path, "A.md", title="Zero Day", isbn="111", authors=["Author A"])
    _write_book(tmp_path, "B.md", title="Zero Day", isbn="222")
    groups = find_duplicates(tmp_path)
    assert len(groups) == 1
    names = {p.name for p in groups[0]}
    assert names == {"A.md", "B.md"}


def test_duplicate_by_isbn(tmp_path):
    _write_book(tmp_path, "A.md", title="First Title", isbn="111", google_books_id="a1")
    _write_book(
        tmp_path, "B.md", title="Second Title", isbn="111", google_books_id="b2"
    )
    groups = find_duplicates(tmp_path)
    assert len(groups) == 1


def test_duplicate_by_google_books_id(tmp_path):
    _write_book(tmp_path, "A.md", title="First", isbn="111", google_books_id="same")
    _write_book(tmp_path, "B.md", title="Second", isbn="222", google_books_id="same")
    groups = find_duplicates(tmp_path)
    assert len(groups) == 1


def test_transitive_duplicates_merged(tmp_path):
    """A shares title with B, B shares ISBN with C => all three in one group."""
    _write_book(
        tmp_path,
        "A.md",
        title="Shared Title",
        isbn="111",
        google_books_id="a1",
        authors=["Same Author"],
    )
    _write_book(
        tmp_path,
        "B.md",
        title="Shared Title",
        isbn="222",
        google_books_id="b2",
        authors=["Same Author"],
    )
    _write_book(tmp_path, "C.md", title="Other Title", isbn="222", google_books_id="c3")
    groups = find_duplicates(tmp_path)
    assert len(groups) == 1
    assert len(groups[0]) == 3


def test_multiple_independent_groups(tmp_path):
    _write_book(tmp_path, "A.md", title="Group One", isbn="111", authors=["Author X"])
    _write_book(tmp_path, "B.md", title="Group One", isbn="112", authors=["Author X"])
    _write_book(tmp_path, "C.md", title="Group Two", isbn="333", authors=["Author Y"])
    _write_book(tmp_path, "D.md", title="Group Two", isbn="444", authors=["Author Y"])
    _write_book(tmp_path, "E.md", title="Unique", isbn="555")
    groups = find_duplicates(tmp_path)
    assert len(groups) == 2


def test_files_without_frontmatter_skipped(tmp_path):
    _write_book(tmp_path, "A.md", title="Same", isbn="111", authors=["Author"])
    _write_book(tmp_path, "B.md", title="Same", isbn="222", authors=["Author"])
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
    _write_book(
        vault,
        "A.md",
        title="Dup Book",
        isbn="111",
        google_books_id="g1",
        authors=["Author"],
    )
    _write_book(
        vault,
        "B.md",
        title="Dup Book",
        isbn="222",
        google_books_id="g2",
        authors=["Author"],
    )
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


# --- detection (#72) ---


def test_titles_differing_only_in_punctuation_are_one_group(tmp_path):
    # Given the same Book written with different punctuation, which the vault
    # holds as "Crucial Conversations- Tools" and "Crucial Conversations: Tools"
    _write_book(
        tmp_path, "A.md", title='"Crucial Conversations- Tools"', authors=["Al"]
    )
    _write_book(
        tmp_path, "B.md", title='"Crucial Conversations: Tools"', authors=["Al"]
    )

    # When duplicates are found
    groups = find_duplicates(tmp_path)

    # Then they group: comparing titles with .lower() missed this entirely
    assert len(groups) == 1
    assert len(groups[0]) == 2


def test_a_subtitle_is_not_treated_as_the_same_title(tmp_path):
    # Given a title that contains another
    _write_book(tmp_path, "A.md", title="The Brass Verdict", authors=["Michael"])
    _write_book(
        tmp_path, "B.md", title='"The Brass Verdict: A Novel"', authors=["Michael"]
    )

    # When duplicates are found
    groups = find_duplicates(tmp_path)

    # Then they are not merged automatically: containment is a judgement, and
    # "Mercy" contains nothing that makes it "Long Road to Mercy"
    assert groups == []


def test_a_subtitle_variant_is_offered_as_a_candidate(tmp_path):
    # Given the same two notes
    _write_book(tmp_path, "A.md", title="The Brass Verdict", authors=["Michael"])
    _write_book(
        tmp_path, "B.md", title='"The Brass Verdict: A Novel"', authors=["Michael"]
    )

    # When candidates are found
    candidates = find_duplicate_candidates(tmp_path)

    # Then a person is offered the pair to settle
    assert len(candidates) == 1
    titles = {note.title for note in candidates[0]}
    assert titles == {"The Brass Verdict", "The Brass Verdict: A Novel"}


def test_a_different_book_by_the_same_author_is_not_a_candidate(tmp_path):
    # Given two unrelated books, the case measured on the real Shelf
    _write_book(tmp_path, "A.md", title="Freakonomics", authors=["Steven"])
    _write_book(tmp_path, "B.md", title="Think Like a Freak", authors=["Steven"])

    # When candidates are found
    # Then nothing is offered: neither title contains the other
    assert find_duplicate_candidates(tmp_path) == []


def test_a_similar_title_by_another_author_is_not_a_candidate(tmp_path):
    # Given a containing title credited to someone else
    _write_book(tmp_path, "A.md", title="Dune", authors=["Frank Herbert"])
    _write_book(tmp_path, "B.md", title='"Dune: Deluxe"', authors=["Someone Else"])

    # When candidates are found
    # Then the author keeps them apart
    assert find_duplicate_candidates(tmp_path) == []


def test_a_confirmed_duplicate_is_not_also_a_candidate(tmp_path):
    # Given two notes that already group by an identifier
    _write_book(tmp_path, "A.md", title="Dune", authors=["Frank"], isbn="978-0-441-0")
    _write_book(
        tmp_path, "B.md", title='"Dune: Deluxe"', authors=["Frank"], isbn="978-0-441-0"
    )

    # When both are computed
    groups = find_duplicates(tmp_path)
    candidates = find_duplicate_candidates(tmp_path)

    # Then the pair is reported once, as the settled thing it is
    assert len(groups) == 1
    assert candidates == []


def test_duplicates_lists_candidates_separately(tmp_path):
    # Given a subtitle variant, which is a judgement rather than a fact
    set_config("book_vault", str(tmp_path))
    _write_book(tmp_path, "A.md", title="The Brass Verdict", authors=["Michael"])
    _write_book(
        tmp_path, "B.md", title='"The Brass Verdict: A Novel"', authors=["Michael"]
    )

    # When duplicates are reported
    result = runner.invoke(app, ["duplicates"])

    # Then the pair is offered rather than counted as a duplicate
    assert result.exit_code == 0
    assert "candidate" in result.output.lower()
    assert "The Brass Verdict" in result.output


def test_merge_decisions_applies_a_reviewed_pair(tmp_path):
    # Given two notes and a review saying they are one Book
    set_config("book_vault", str(tmp_path))
    a = _write_book(
        tmp_path,
        "A.md",
        title="The Brass Verdict",
        authors=["Michael"],
        libris_id="01AAA",
    )
    b = _write_book(
        tmp_path,
        "B.md",
        title='"The Brass Verdict: A Novel"',
        authors=["Michael"],
        libris_id="01BBB",
    )
    first, second = BookNote.read(a), BookNote.read(b)
    decisions = tmp_path / "decisions.json"
    decisions.write_text(
        json.dumps(
            {
                "decisions": [
                    {
                        "decision": "same",
                        "shorter": {
                            "title": first.title,
                            "libris_id": first.libris_id,
                        },
                        "longer": {
                            "title": second.title,
                            "libris_id": second.libris_id,
                        },
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    # When the review is applied
    result = runner.invoke(app, ["merge", "--decisions", str(decisions)])

    # Then one note remains
    assert result.exit_code == 0
    assert "Merged" in result.output
    assert len(list(tmp_path.glob("*.md"))) == 1


def test_merge_decisions_reports_a_missing_file(tmp_path):
    # Given a path that is not there
    set_config("book_vault", str(tmp_path))

    # When it is applied
    result = runner.invoke(app, ["merge", "--decisions", str(tmp_path / "nope.json")])

    # Then it says so rather than raising
    assert result.exit_code != 0
    assert "No such decisions file" in result.output
    assert "Traceback" not in result.output


def test_merge_decisions_reports_unreadable_json(tmp_path):
    # Given a file that is not JSON
    set_config("book_vault", str(tmp_path))
    bad = tmp_path / "bad.json"
    bad.write_text("not json at all", encoding="utf-8")

    # When it is applied
    result = runner.invoke(app, ["merge", "--decisions", str(bad)])

    # Then the failure is explained, not raised
    assert result.exit_code != 0
    assert "Could not read" in result.output
    assert "Traceback" not in result.output


def test_merge_decisions_dry_run_writes_nothing(tmp_path):
    # Given a review that would merge a pair
    set_config("book_vault", str(tmp_path))
    _write_book(
        tmp_path,
        "A.md",
        title="The Brass Verdict",
        authors=["Michael"],
        libris_id="01AAA",
    )
    _write_book(
        tmp_path,
        "B.md",
        title='"The Brass Verdict: A Novel"',
        authors=["Michael"],
        libris_id="01BBB",
    )
    decisions = tmp_path / "decisions.json"
    decisions.write_text(
        json.dumps(
            {
                "decisions": [
                    {
                        "decision": "same",
                        "shorter": {"title": "The Brass Verdict", "libris_id": "01AAA"},
                        "longer": {"title": "A Novel", "libris_id": "01BBB"},
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    # When it is previewed
    result = runner.invoke(app, ["merge", "--decisions", str(decisions), "--dry-run"])

    # Then it says what it would do and both notes are still there. This
    # command deletes Book Notes; 74 of them in one run deserves a look first.
    assert result.exit_code == 0
    assert "Would merge" in result.output
    assert "Nothing written" in result.output
    assert len(list(tmp_path.glob("*.md"))) == 2
