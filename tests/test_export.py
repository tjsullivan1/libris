"""Exporting the Library (#12).

JSON is the lossless shape: every frontmatter field a note carries, plus its
body unless asked otherwise. CSV is a view for a spreadsheet - the fields the
Library models, and never the reader's prose.
"""

import csv
import io
import json
from pathlib import Path

from typer.testing import CliRunner

from libris.cli import app
from libris.config import set_config

runner = CliRunner()


def _write_note(vault: Path, name: str, body: str = "", **frontmatter) -> Path:
    """A Book Note written as the Shelf holds them, frontmatter then body."""
    lines = ["---"]
    for key, value in frontmatter.items():
        if isinstance(value, list):
            lines.append(f"{key}:")
            for item in value:
                lines.append(f"- {item}")
        elif value is None:
            lines.append(f"{key}: null")
        else:
            lines.append(f"{key}: {value}")
    lines.append("---")
    path = vault / name
    path.write_text("\n".join(lines) + "\n" + body, encoding="utf-8")
    return path


def _shelf(tmp_path: Path) -> Path:
    vault = tmp_path / "shelf"
    vault.mkdir()
    _write_note(
        vault,
        "Dune.md",
        body="\n# Dune\n\n## Notes\n\nThe reader's own writing.\n",
        libris_id="lb-1",
        title="Dune",
        authors=["Frank Herbert"],
        status="Read",
        rating=5,
        genres=["Science Fiction", "Classics"],
        date_added="2020-05-05",
        date_finished='"2021-01-02"',
    )
    set_config("book_vault", str(vault))
    set_config("vault_path", str(vault))
    return vault


def test_export_writes_json_of_every_note(tmp_path):
    # Given a Shelf holding a book
    _shelf(tmp_path)

    # When it is exported
    result = runner.invoke(app, ["export"])

    # Then the output is JSON carrying that note's fields
    assert result.exit_code == 0, result.output
    rows = json.loads(result.stdout)
    assert len(rows) == 1
    assert rows[0]["frontmatter"]["title"] == "Dune"
    assert rows[0]["frontmatter"]["authors"] == ["Frank Herbert"]


def test_export_writes_dates_as_iso_strings(tmp_path):
    # Given a note whose date_added is unquoted, which YAML reads as a date
    # object rather than a string - 3,055 notes on the real Shelf are like this
    _shelf(tmp_path)

    # When it is exported
    result = runner.invoke(app, ["export"])

    # Then the date is a string JSON can hold, not an object json.dumps refuses.
    # The same field holds both types across the Shelf depending on who wrote
    # it, so the export settles on one (#12).
    assert result.exit_code == 0, result.output
    rows = json.loads(result.stdout)
    assert rows[0]["frontmatter"]["date_added"] == "2020-05-05"
    assert rows[0]["frontmatter"]["date_finished"] == "2021-01-02"


def test_export_carries_the_body(tmp_path):
    # Given a note whose body holds the reader's own writing
    _shelf(tmp_path)

    # When it is exported
    result = runner.invoke(app, ["export"])

    # Then the body travels with it. Every note on the real Shelf has one, and
    # ADR 0009 calls that writing the irreplaceable part - an export without it
    # is not a backup.
    assert result.exit_code == 0, result.output
    rows = json.loads(result.stdout)
    assert "The reader's own writing." in rows[0]["body"]


def test_export_leaves_the_body_out_when_asked(tmp_path):
    # Given the same Shelf
    _shelf(tmp_path)

    # When the catalogue alone is wanted
    result = runner.invoke(app, ["export", "--no-bodies"])

    # Then no body travels, and the fields still do
    assert result.exit_code == 0, result.output
    rows = json.loads(result.stdout)
    assert "body" not in rows[0]
    assert rows[0]["frontmatter"]["title"] == "Dune"


def test_export_writes_to_a_file_when_given_one(tmp_path):
    # Given a Shelf and somewhere to put the export
    _shelf(tmp_path)
    out = tmp_path / "library.json"

    # When it is exported to that path
    result = runner.invoke(app, ["export", "--out", str(out)])

    # Then the file holds it, and the terminal says where it went rather than
    # printing the whole Library
    assert result.exit_code == 0, result.output
    assert out.exists()
    rows = json.loads(out.read_text(encoding="utf-8"))
    assert rows[0]["frontmatter"]["title"] == "Dune"
    assert "Dune" not in result.stdout or str(out) in result.stdout


def test_export_csv_has_a_column_per_modelled_field(tmp_path):
    # Given a Shelf
    _shelf(tmp_path)

    # When it is exported as CSV
    result = runner.invoke(app, ["export", "--format", "csv"])

    # Then the columns are the fields the Library models, taken from its own
    # vocabulary rather than invented here
    from libris.note_format import MODELLED_FIELDS

    assert result.exit_code == 0, result.output
    rows = list(csv.reader(io.StringIO(result.stdout)))
    assert rows[0] == list(MODELLED_FIELDS)


def test_export_csv_joins_the_fields_that_hold_several_values(tmp_path):
    # Given a note with two genres
    _shelf(tmp_path)

    # When it is exported as CSV
    result = runner.invoke(app, ["export", "--format", "csv"])

    # Then they arrive in one cell, joined, rather than as a Python list repr
    assert result.exit_code == 0, result.output
    rows = list(csv.DictReader(io.StringIO(result.stdout)))
    assert rows[0]["genres"] == "Science Fiction; Classics"
    assert rows[0]["authors"] == "Frank Herbert"


def test_export_csv_does_not_read_the_bodies_it_will_not_print(tmp_path, monkeypatch):
    # Given a Shelf whose notes have bodies
    vault = _shelf(tmp_path)

    # Counted rather than searched for. Asserting the prose is absent from the
    # CSV proves nothing: `DictWriter` is built with extrasaction="ignore" over
    # the modelled fields, so a body is dropped from the output whether or not
    # it was ever read. The flag is a read-cost decision, and reads are what
    # this has to measure (#12).
    reads: list[Path] = []
    real_read_text = Path.read_text

    def _counting(self, *args, **kwargs):
        if self.suffix == ".md" and self.parent == vault:
            reads.append(self)
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", _counting)

    # When it is exported as CSV
    result = runner.invoke(app, ["export", "--format", "csv"])

    # Then each note was parsed once, for its fields - and not read a second
    # time for a body no column will hold
    assert result.exit_code == 0, result.output
    assert "The reader's own writing." not in result.stdout
    assert len(reads) == len(list(vault.glob("*.md")))


def test_export_csv_has_one_line_per_book(tmp_path):
    # Given a Shelf of three books
    vault = _shelf(tmp_path)
    _write_note(vault, "Emma.md", libris_id="lb-2", title="Emma", authors=["Austen"])
    _write_note(vault, "Kim.md", libris_id="lb-3", title="Kim", authors=["Kipling"])
    out = tmp_path / "library.csv"

    # When it is exported to a file
    result = runner.invoke(app, ["export", "--format", "csv", "--out", str(out)])

    # Then the file holds a header and one line per book, with nothing between
    # them. `csv` writes its own \r\n, and writing that through a text handle
    # translates it again: the file came out double-spaced, 6,148 lines for
    # 3,073 books. Asserted on the physical lines rather than through
    # `csv.DictReader`, which skips the blanks and reports it as fine.
    assert result.exit_code == 0, result.output
    lines = out.read_text(encoding="utf-8").splitlines()
    assert lines[0].startswith("libris_id,")
    assert len(lines) == 4
    assert all(line.strip() for line in lines)


def test_export_refuses_a_format_it_does_not_know(tmp_path):
    # Given a Shelf
    _shelf(tmp_path)

    # When an unknown format is asked for
    result = runner.invoke(app, ["export", "--format", "xml"])

    # Then it says so and writes nothing, rather than defaulting quietly
    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit)
    assert "xml" in result.output
