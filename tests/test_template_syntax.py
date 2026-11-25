"""Template syntax from a catalogue never reaches a note (#231).

Templater, an Obsidian plugin, can run every `<% %>` command in a note the
moment the note appears in the vault, whoever wrote it, and `<%* %>` is
JavaScript with Node's `require`. A Book Note is written from Google Books
metadata, which anyone can publish to. So every path that carries that metadata
into a note either escapes the opener, where the text is body, or refuses it,
where the text is frontmatter.

Each test names a way in. Asserting "no `<%` anywhere in the file" rather than
in one field is the point: the opener runs wherever it lands.
"""

import json
from dataclasses import replace
from pathlib import Path

import pytest
from cosmos_fake import FakeContainer

from libris import service, shelf
from libris.api import BookCandidate
from libris.cli import _append_auto_enrich_note
from libris.config import set_config
from libris.importer import run_import
from libris.intents import IntentQueue, Settled, apply_pending
from libris.markdown import (
    TemplateSyntaxInMetadata,
    create_book_note,
    update_frontmatter_from_book,
)
from libris.note_format import render_description_callout
from libris.store import ReplicaStore
from libris.sync_note import render

PAYLOAD = "<%* require('child_process').exec('calc') %>"

DUNE = BookCandidate(
    title="Dune",
    authors=["Frank Herbert"],
    isbn="9780441013593",
    google_books_id="dune-1",
)


@pytest.fixture(autouse=True)
def fresh_indexes():
    """No index survives into another test's temporary Shelf."""
    shelf.forget_indexes()
    yield
    shelf.forget_indexes()


def _shelf_text(vault: Path) -> str:
    return "".join(p.read_text(encoding="utf-8") for p in vault.rglob("*.md"))


# --- the body: escaped ---


def test_a_description_carrying_template_syntax_is_written_escaped(tmp_path):
    # Given a candidate whose description carries a Templater command
    book = replace(DUNE, description=f"A desert planet. {PAYLOAD}")

    # When its note is written
    path = create_book_note(book, tmp_path)

    # Then the note holds no opener anywhere
    text = path.read_text(encoding="utf-8")
    assert "<%" not in text
    # And the description is still there, spelled so Obsidian shows `<%`
    assert "&lt;%* require('child_process')" in text


def test_every_line_of_a_description_callout_is_escaped():
    # Given a description whose opener sits on a later line
    description = f"First line.\n\nSecond {PAYLOAD}\n<% tp.file.title %>"

    # When it is rendered as the callout every writer uses
    callout = render_description_callout(description)

    # Then no line carries the opener
    assert "<%" not in callout


# --- the frontmatter: refused ---


@pytest.mark.parametrize(
    "field_name, value",
    [
        ("title", f"Dune {PAYLOAD}"),
        ("authors", ["Frank Herbert", PAYLOAD]),
        ("genres", ["Fiction", PAYLOAD]),
        ("isbn", PAYLOAD),
        ("published_date", PAYLOAD),
        ("google_books_id", PAYLOAD),
        ("thumbnail", f"https://example.com/{PAYLOAD}"),
    ],
)
def test_a_candidate_with_template_syntax_in_its_frontmatter_is_refused(
    tmp_path, field_name, value
):
    # Given a candidate with the opener in a field bound for the frontmatter,
    # where it cannot be escaped without changing the value
    book = replace(DUNE, **{field_name: value})

    # When its note is written, it is refused, naming the field
    with pytest.raises(TemplateSyntaxInMetadata, match=field_name.replace("_", " ")):
        create_book_note(book, tmp_path)

    # And nothing is written
    assert list(tmp_path.glob("*.md")) == []


def test_the_refusal_is_a_value_error_so_every_surface_reports_it():
    # The CLI, the daemon, both MCP servers and sync all turn a ValueError into
    # a refusal; this is what lets them do so without knowing the new type.
    assert issubclass(TemplateSyntaxInMetadata, ValueError)


# --- the surfaces that write through the service ---


def test_a_local_add_through_the_service_is_refused(tmp_path):
    # Given an empty Shelf, and a candidate whose title carries the opener
    book = replace(DUNE, title=f"Dune {PAYLOAD}")

    # When it is added, as the CLI, the daemon and the local MCP server add
    with pytest.raises(ValueError, match="template syntax"):
        service.add_book(tmp_path, book)

    # Then nothing is written
    assert list(tmp_path.glob("*.md")) == []


def test_a_remote_add_is_refused_when_asked_not_queued(tmp_path):
    # Given an empty Shelf, and a candidate whose title carries the opener
    intents = FakeContainer()
    book = replace(DUNE, title=f"Dune {PAYLOAD}")

    # When the hosted server is asked to add it, it is refused at once, while
    # someone is there to choose another edition
    with pytest.raises(ValueError, match="template syntax"):
        service.queue_add(
            ReplicaStore.from_shelf(tmp_path), IntentQueue(intents), book, None
        )

    # And nothing is queued for sync to write
    assert intents.items == {}


def test_a_queued_add_carrying_template_syntax_is_rejected_by_sync(tmp_path):
    # Given an add-Intent recorded before this check existed, carrying the
    # opener in its title
    intents = FakeContainer()
    result = service.queue_add(
        ReplicaStore.from_shelf(tmp_path), IntentQueue(intents), DUNE, None
    )
    intents.items[result.intent_id]["candidate"]["title"] = f"Dune {PAYLOAD}"

    # When sync applies it
    report = apply_pending(tmp_path, intents)

    # Then it is rejected rather than retried, and no note is written
    assert intents.items[result.intent_id]["outcome"] == "rejected"
    assert "template syntax" in intents.items[result.intent_id]["reason"]
    assert list(tmp_path.glob("*.md")) == []
    assert len(report.rejected) == 1


def test_the_sync_note_listing_a_rejected_add_escapes_its_title():
    # Given the Intent above, rejected, as sync lists it in its own note - a
    # note created afresh whenever it was deleted
    rejected = [
        {
            "id": "intent-1",
            "kind": "add",
            "surface": "hosted_mcp",
            "recorded_at": "2026-11-25T12:00:00+00:00",
            "candidate": {"title": f"Dune {PAYLOAD}"},
            "fields": {},
            "reason": f"it carries {PAYLOAD}",
        }
    ]
    waiting = [Settled("intent-2", f"Arrakis {PAYLOAD}", reason=PAYLOAD)]

    # When the note is rendered
    text = render(rejected, waiting)

    # Then neither list puts the opener in it
    assert "<%" not in text
    assert "Dune &lt;%*" in text


# --- enriching an existing note ---


def test_enrichment_skips_a_field_carrying_template_syntax(tmp_path):
    # Given a note with no ISBN and no cover, and a match whose cover and
    # description carry the opener. Only an empty field is filled, so the
    # poisoned value must be one the note lacks, or skipping it proves nothing.
    path = create_book_note(replace(DUNE, isbn=None), tmp_path)
    match = replace(
        DUNE,
        thumbnail=f"https://example.com/{PAYLOAD}",
        description=f"Blurb {PAYLOAD}",
        isbn="9780441013593",
    )

    # When the note is enriched from it
    changed = update_frontmatter_from_book(path, match)

    # Then the safe field is filled
    text = path.read_text(encoding="utf-8")
    assert changed
    assert "9780441013593" in text
    # And nothing anywhere in the note carries the opener: the cover was
    # skipped, the description escaped
    assert "<%" not in text
    assert "cover_thumbnail: https://example.com" not in text
    assert "Blurb &lt;%*" in text


def test_the_auto_enrich_review_callout_escapes_the_matched_title(tmp_path):
    # Given a note, matched automatically to a title carrying the opener
    path = create_book_note(DUNE, tmp_path)

    # When the review callout is appended
    _append_auto_enrich_note(path, f"Dune {PAYLOAD}", "dune")

    # Then the callout quotes the title without the opener
    assert "<%" not in path.read_text(encoding="utf-8")


# --- importing ---


def test_import_goes_on_past_a_book_carrying_template_syntax(tmp_path):
    # Given an import of three books, the second titled with the opener
    vault = tmp_path / "vault"
    vault.mkdir()
    set_config("book_vault", str(vault))
    library = tmp_path / "library.json"
    library.write_text(
        json.dumps(
            [
                {"title": "Book One", "author": "Author A", "finished": "No"},
                {
                    "title": f"Book Two {PAYLOAD}",
                    "author": "Author B",
                    "finished": "No",
                },
                {"title": "Book Three", "author": "Author C", "finished": "No"},
            ]
        ),
        encoding="utf-8",
    )

    # When the import is applied
    result = run_import(library, vault, apply=True)

    # Then the books either side are created, and the second is reported, not
    # added
    assert [b.title for b in result.new_books] == ["Book One", "Book Three"]
    assert len(result.uncreated_books) == 1
    assert "template syntax" in result.uncreated_books[0][1]
    # And nothing on the Shelf carries the opener
    assert "<%" not in _shelf_text(vault)
