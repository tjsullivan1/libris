"""Tests for the one-time migration of the Shelf to the canonical Book Note shape."""

from datetime import date

from libris.markdown import BookNote
from libris.migrate import (
    leaked_alias_keys,
    mint_libris_id,
    plan_note_migration,
    recover_title,
    render_description_callout,
    reorder_frontmatter,
    split_body,
    split_frontmatter_blocks,
)


def write_note(path, frontmatter: str, body: str):
    """Write a note from raw frontmatter and body text."""
    path.write_text(f"---\n{frontmatter}\n---\n\n{body}", encoding="utf-8")
    return path


# --- Libris IDs ---


def test_libris_ids_sort_in_the_order_books_were_added():
    # Given two books added years apart
    early = mint_libris_id(date(2019, 3, 12))
    late = mint_libris_id(date(2026, 5, 7))

    # When their IDs are compared as strings
    # Then the earlier book sorts first
    assert early < late


def test_libris_id_accepts_an_iso_string():
    # Given a date_added that YAML left as a string
    minted = mint_libris_id("2019-03-12")

    # Then it is a well-formed ULID
    assert len(minted) == 26


def test_libris_id_falls_back_when_the_date_is_unusable():
    # Given notes with no usable date_added
    # Then an ID is still minted for each, and they differ
    assert len(mint_libris_id(None)) == 26
    assert len(mint_libris_id("not a date")) == 26
    assert mint_libris_id(None) != mint_libris_id(None)


# --- Frontmatter ---


def test_sequence_items_stay_with_their_key():
    # Given frontmatter holding a multi-line sequence
    blocks = split_frontmatter_blocks(
        'title: "Dune"\nauthors:\n  - Frank Herbert\n  - Someone Else\nisbn: "123"'
    )

    # Then each key keeps its own lines, rendered exactly as written
    assert dict(blocks)["authors"] == "authors:\n  - Frank Herbert\n  - Someone Else"
    assert dict(blocks)["isbn"] == 'isbn: "123"'


def test_reordering_groups_fields_and_adds_the_missing_ones():
    # Given a note carrying two modelled fields and one the linter added
    blocks = [
        ("status", "status: Read"),
        ("title", "title: Dune"),
        ("date_created", "date_created: Tuesday"),
    ]

    # When the frontmatter is regrouped
    keys = [key for key, _ in reorder_frontmatter(blocks)]

    # Then identity comes first, dates last, and the unmodelled field survives
    assert keys[0] == "libris_id"
    assert keys.index("title") < keys.index("status")
    assert keys[-1] == "date_created"
    assert "priority" in keys and "series" in keys


def test_reordering_does_not_rewrite_values_it_did_not_change():
    # Given a value with quoting the vault chose
    blocks = [("isbn", 'isbn: "9780441013593"')]

    # When regrouped
    ordered = dict(reorder_frontmatter(blocks))

    # Then the value renders exactly as it did
    assert ordered["isbn"] == 'isbn: "9780441013593"'


# --- Bodies ---


def test_description_below_the_notes_heading_is_separated():
    # Given the shape most notes have
    prose, description = split_body(
        "# Dune\n\n## Notes\n\nmy prose\n\n### Description\nblurb\n"
    )

    # Then the reader's prose and the blurb are told apart
    assert prose == "my prose"
    assert description == "blurb"


def test_description_above_the_notes_heading_does_not_swallow_prose():
    # Given a note where the description comes first
    prose, description = split_body(
        "# Dune\n\n### Description\nblurb\n\n# Notes\n\nmy prose\n"
    )

    # Then the description stops at the next heading
    assert prose == "my prose"
    assert description == "blurb"


def test_merged_content_is_prose_not_blurb():
    # Given a note that merge.py joined with a thematic break
    prose, description = split_body(
        "# Dune\n\n### Description\nblurb\n\n---\n\n"
        "**Merged from duplicate entry:**\n\nnotes from the other copy\n"
    )

    # Then the reader's writing is kept out of the description
    assert description == "blurb"
    assert "notes from the other copy" in prose


def test_an_already_migrated_body_reads_back_unchanged():
    # Given a body in the shape the migration produces
    prose, description = split_body(
        "# Dune\n\n## Notes\n\nmy prose\n\n> [!abstract]- Description\n> blurb\n> more\n"
    )

    # Then it round-trips, so the migration is safe to run twice
    assert prose == "my prose"
    assert description == "blurb\nmore"


def test_callout_quotes_every_line_including_blank_ones():
    # Given a description with a paragraph break
    rendered = render_description_callout("first\n\nsecond")

    # Then every line is quoted and the callout is collapsed
    assert rendered == "> [!abstract]- Description\n> first\n>\n> second"


# --- Repairs ---


def test_a_title_overwritten_by_a_heading_is_recovered_from_the_filename(tmp_path):
    # Given a note whose title the linter replaced with the Notes heading
    path = tmp_path / "Dune - Frank Herbert.md"
    note = BookNote(
        path=path, frontmatter={"title": "Notes", "authors": ["Frank Herbert"]}
    )

    # When the title is repaired
    repaired, reason = recover_title(note)

    # Then it comes back from the filename, without the author
    assert repaired == "Dune"
    assert "recovered from filename" in reason


def test_an_author_inside_the_title_is_removed(tmp_path):
    # Given a title the linter built from the filename
    path = tmp_path / "10% Happier - Dan Harris.md"
    note = BookNote(
        path=path,
        frontmatter={"title": "10% Happier - Dan Harris", "authors": ["Dan Harris"]},
    )

    # When the title is repaired
    repaired, reason = recover_title(note)

    # Then only the title remains
    assert repaired == "10% Happier"
    assert reason == "removed author from title"


def test_a_good_title_is_left_alone(tmp_path):
    # Given a title with nothing wrong with it
    path = tmp_path / "Dune - Frank Herbert.md"
    note = BookNote(
        path=path, frontmatter={"title": "Dune", "authors": ["Frank Herbert"]}
    )

    # Then nothing is repaired
    assert recover_title(note) == (None, None)


def test_only_wholly_leaked_aliases_are_dropped():
    # Given one alias that leaked from a heading and one the reader added
    leaked = leaked_alias_keys([("aliases", "aliases:\n  - Notes")])
    mixed = leaked_alias_keys([("aliases", "aliases:\n  - Notes\n  - Dune")])

    # Then only the wholly leaked block is discarded
    assert leaked == ["aliases"]
    assert mixed == []


# --- End to end ---


def test_migrating_a_note_mints_an_id_regroups_and_restructures(tmp_path):
    # Given a Book Note in the shape the vault holds
    path = write_note(
        tmp_path / "Dune - Frank Herbert.md",
        'title: "Dune"\nauthors:\n  - Frank Herbert\nstatus: Read\n'
        "date_added: 2019-03-12\ndate_created: Tuesday",
        "## Notes\n\nmy prose\n\n### Description\nblurb\n",
    )

    # When the migration is planned
    plan = plan_note_migration(path)

    # Then the note gains an identity, a title heading and a collapsed blurb
    assert "libris_id: 01" in plan.migrated
    assert "# Dune\n\n## Notes\n\nmy prose" in plan.migrated
    assert "> [!abstract]- Description\n> blurb" in plan.migrated

    # And the field a plugin added survives untouched
    assert "date_created: Tuesday" in plan.migrated

    # And the value the vault already had is not rewritten
    assert 'title: "Dune"' in plan.migrated


def test_migrating_a_note_twice_changes_nothing_the_second_time(tmp_path):
    # Given a note that has already been migrated
    path = write_note(
        tmp_path / "Dune - Frank Herbert.md",
        'title: "Dune"\nauthors:\n  - Frank Herbert\ndate_added: 2019-03-12',
        "## Notes\n\nmy prose\n\n### Description\nblurb\n",
    )
    path.write_text(plan_note_migration(path).migrated, encoding="utf-8")

    # When it is planned again
    second = plan_note_migration(path)

    # Then there is nothing left to do
    assert not second.changed
    assert second.changes == []


def test_a_note_without_frontmatter_is_skipped_not_mangled(tmp_path):
    # Given a file that is not a Book Note
    path = tmp_path / "stray.md"
    path.write_text("# Just a heading\n", encoding="utf-8")

    # When the migration is planned
    plan = plan_note_migration(path)

    # Then it is left exactly as it was, and says why
    assert not plan.changed
    assert plan.warnings == ["no parseable frontmatter; skipped"]
