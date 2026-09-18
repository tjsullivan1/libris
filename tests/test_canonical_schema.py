"""Tests asserting the canonical frontmatter vocabulary from ADR 0005.

Every note in the vault uses `authors`, `date_published` and `cover_thumbnail`. The code
reads `author`, `published_date` and `thumbnail`, so every read returns None silently and
six behaviours are broken in production while the rest of the suite stays green — because
its fixtures use the code's schema rather than the vault's.

These tests build notes shaped like real ones. See #61.
"""

from datetime import date

import pytest

from libris.api import BookCandidate
from libris.cli import _build_query_from_frontmatter, _needs_enrichment
from libris.importer import _build_vault_index
from libris.markdown import (
    compute_canonical_filename,
    create_book_note,
    ensure_frontmatter_fields,
    find_duplicates,
    read_frontmatter,
    rename_book_file,
    update_book_status,
)
from libris.note_format import InvalidFieldValue, read_formats

# The exact key set present on all 3,137 notes in the vault.
CANONICAL_FRONTMATTER = {
    "title": None,
    "authors": None,
    "isbn": None,
    "page_count": None,
    "date_published": None,
    "google_books_id": "",
    "cover_thumbnail": None,
    "genres": [],
    "tags": "Book",
    "format": None,
    "status": "To Read",
    "rating": None,
    "referred_by": None,
    "date_added": None,
    "date_started": None,
    "date_finished": None,
}


def write_note(vault, filename, **overrides):
    """Write a Book Note shaped exactly like the ones in the vault.

    Args:
        vault: Directory to write into.
        filename: Note filename, including the .md suffix.
        **overrides: Frontmatter values to set, using canonical field names.

    Returns:
        Path to the written note.
    """
    fm = {**CANONICAL_FRONTMATTER, **overrides}
    lines = []
    for key, value in fm.items():
        if value is None:
            lines.append(f"{key}:")
        elif isinstance(value, list):
            if not value:
                lines.append(f"{key}: []")
            else:
                lines.append(f"{key}:")
                # Quote as the vault does, so a wikilink is a string and not a
                # nested YAML sequence.
                lines.extend(f'  - "{item}"' for item in value)
        elif isinstance(value, str):
            lines.append(f'{key}: "{value}"')
        else:
            lines.append(f"{key}: {value}")

    path = vault / filename
    path.write_text(
        "---\n" + "\n".join(lines) + "\n---\n\n## Notes\n", encoding="utf-8"
    )
    return path


@pytest.fixture
def vault(tmp_path):
    """A Shelf directory to write Book Notes into."""
    shelf = tmp_path / "Book List"
    shelf.mkdir()
    return shelf


def test_vault_index_finds_notes_written_with_canonical_fields(vault):
    # Given two Book Notes shaped like the ones in the vault
    write_note(
        vault, "Dune - Frank Herbert.md", title="Dune", authors=["Frank Herbert"]
    )
    write_note(
        vault,
        "Oathbringer - Brandon Sanderson.md",
        title="Oathbringer",
        authors=["Brandon Sanderson"],
    )

    # When the importer builds its duplicate-detection index
    index = _build_vault_index(vault)

    # Then both notes are indexed
    # Currently 0: every note is skipped at importer.py:134 because fm.get("author")
    # is None, so import duplicate detection has never matched anything.
    assert len(index) == 2


def test_duplicates_does_not_group_books_by_different_authors(vault):
    # Given two unrelated books that happen to share a title
    write_note(vault, "Poems - Catullus.md", title="Poems", authors=["Catullus"])
    write_note(
        vault,
        "Poems - Gerard Manley Hopkins.md",
        title="Poems",
        authors=["Gerard Manley Hopkins"],
    )

    # When duplicates are detected
    groups = find_duplicates(vault)

    # Then they are not duplicates of each other
    # Currently one group of 2: _author_key returns () for every note, so a missing
    # author is treated as a wildcard and grouping falls back to title alone.
    assert groups == []


def test_canonical_filename_resolves_from_the_authors_field(vault):
    # Given a Book Note with a misnamed file
    note = write_note(vault, "wrong-name.md", title="Dune", authors=["Frank Herbert"])

    # When the canonical filename is computed
    result = compute_canonical_filename(note)

    # Then it comes from the title and first author
    # Currently None for every note in the vault, which makes clean --rename a no-op.
    assert result == "Dune - Frank Herbert.md"


def test_rename_reads_the_authors_field(vault):
    # Given a Book Note with a misnamed file
    note = write_note(vault, "wrong-name.md", title="Dune", authors=["Frank Herbert"])

    # When it is renamed
    result = rename_book_file(note)

    # Then the author was found
    # Currently "missing_author" for every note in the vault.
    assert result.status != "missing_author"


def test_enrichment_query_searches_by_author(vault):
    # Given frontmatter from a Book Note in the vault
    fm = {**CANONICAL_FRONTMATTER, "title": "Poems", "authors": ["Catullus"]}
    note = vault / "Poems - Catullus.md"

    # When an enrichment query is built from it
    query = _build_query_from_frontmatter(fm, note)

    # Then it constrains the search by author
    # Currently title-only, because fm.get("author") is None. Nineteen notes in the
    # vault are titled "Poems", so a title-only query cannot resolve them.
    assert "inauthor:Catullus" in query


def test_already_enriched_note_is_not_re_enriched():
    # Given a note carrying metadata that only Google Books could have supplied
    fm = {
        **CANONICAL_FRONTMATTER,
        "title": "Dune",
        "authors": ["Frank Herbert"],
        "cover_thumbnail": "http://books.google.com/books/content?id=abc",
        "date_published": "1965-08-01",
    }

    # When it is checked for enrichment
    # Then it is recognised as already enriched
    # Currently True: _API_SOURCED_FIELDS names "thumbnail" and "published_date", neither
    # of which exists, so it tests two fields instead of four. Measured against the
    # vault it reports 152 unenriched notes when only 5 are.
    assert _needs_enrichment(fm) is False


def test_created_notes_use_the_canonical_field_names(vault):
    # Given a book resolved from Google Books
    book = BookCandidate(
        title="Dune",
        authors=["Frank Herbert"],
        isbn="9780441013593",
        page_count=412,
        published_date="1965-08-01",
        google_books_id="dune1",
        thumbnail="http://books.google.com/books/content?id=dune1",
        genres=["Science Fiction"],
        description="A novel about a desert planet.",
    )

    # When a note is created for it
    note = create_book_note(book, vault)
    content = note.read_text(encoding="utf-8")

    # Then the frontmatter matches every other note in the vault
    # Currently writes author/published_date/thumbnail, so newly created notes disagree
    # with all 3,137 existing ones. This is the root cause of the five failures above.
    assert "authors:" in content
    assert "date_published:" in content
    assert "cover_thumbnail:" in content


def test_an_author_written_as_a_wikilink_reads_as_a_plain_name(vault):
    # Given a note that links its author to their own note
    note = write_note(
        vault,
        "Atlas Shrugged - Ayn Rand.md",
        title="Atlas Shrugged",
        authors=["[[Ayn Rand]]"],
    )

    # Then the name is used, not the link markup
    # Seven notes in the vault store authors this way; the brackets would
    # otherwise end up in a canonical filename.
    from libris.markdown import BookNote

    assert BookNote.read(note).authors == ["Ayn Rand"]
    assert BookNote.read(note).canonical_filename == "Atlas Shrugged - Ayn Rand.md"


def test_stray_whitespace_in_an_author_name_is_collapsed(vault):
    # Given an author name carrying a doubled space
    note = write_note(
        vault,
        "10% Happier - Dan Harris.md",
        title="10% Happier",
        authors=["Dan   Harris"],
    )

    # Then it matches the same name written normally
    # This is why the migration first missed one polluted title: the check
    # compared "Dan   Harris" against "Dan Harris" literally.
    from libris.markdown import BookNote

    assert BookNote.read(note).first_author == "Dan Harris"


# --- field value vocabularies (#65) ---
#
# Measured against the vault before writing these: all 3,136 notes already hold
# a legal status (1,667 Read, 1,467 To Read, 1 Reading, 1 Not To Read) and a
# legal priority (2,251 unset, then Low/Medium/High). Nothing needs migrating,
# so this is pure enforcement. `format` is deliberately not validated here: it
# holds eleven shapes across two types and needs a migration of its own.


def _candidate() -> BookCandidate:
    return BookCandidate(title="Dune", authors=["Frank Herbert"])


@pytest.mark.parametrize("status", ["To Read", "Reading", "Read", "Not To Read"])
def test_every_status_the_vault_uses_is_accepted(tmp_path, status):
    # Given a status that exists in the vault today
    # When a note is created with it
    path = create_book_note(_candidate(), tmp_path, status=status)

    # Then it is written
    assert path.exists()


def test_an_unknown_status_is_refused(tmp_path):
    # Given a status outside the four the Library defines
    # When a note is created with it
    # Then it is refused rather than silently written into the Shelf
    with pytest.raises(InvalidFieldValue):
        create_book_note(_candidate(), tmp_path, status="finished")


def test_a_status_differing_only_in_case_is_refused(tmp_path):
    # Given the right word in the wrong case
    # When a note is created with it
    # Then it is refused; the vault's Bases views group on the exact string
    with pytest.raises(InvalidFieldValue):
        create_book_note(_candidate(), tmp_path, status="read")


def test_the_refusal_names_the_field_and_the_legal_values(tmp_path):
    # Given an illegal status
    # When a note is created with it
    with pytest.raises(InvalidFieldValue) as excinfo:
        create_book_note(_candidate(), tmp_path, status="finished")

    # Then the message is usable without reading the source
    message = str(excinfo.value)
    assert "status" in message
    assert "finished" in message
    assert "To Read" in message


@pytest.mark.parametrize("priority", ["Low", "Medium", "High"])
def test_every_priority_the_vault_uses_is_accepted(tmp_path, priority):
    # Given a priority that exists in the vault today
    # When a note is created with it
    path = create_book_note(_candidate(), tmp_path, overrides={"priority": priority})

    # Then it is written
    assert path.exists()


def test_priority_may_be_unset(tmp_path):
    # Given a book never triaged - 2,251 notes in the vault
    # When a note is created with no priority
    path = create_book_note(_candidate(), tmp_path, overrides={"priority": None})

    # Then that is legal; absent is not the same as invalid
    assert path.exists()


def test_an_unknown_priority_is_refused(tmp_path):
    # Given a priority outside Low, Medium, High
    # When a note is created with it
    # Then it is refused
    with pytest.raises(InvalidFieldValue):
        create_book_note(_candidate(), tmp_path, overrides={"priority": "Urgent"})


def test_status_override_is_validated_too(tmp_path):
    # Given an illegal status arriving as an override rather than the argument
    # When a note is created
    # Then the same rule applies; there is one gate, not two
    with pytest.raises(InvalidFieldValue):
        create_book_note(_candidate(), tmp_path, overrides={"status": "finished"})


def test_updating_to_an_unknown_status_is_refused(tmp_path):
    # Given an existing note
    path = create_book_note(_candidate(), tmp_path, status="To Read")

    # When its status is updated to something the Library does not define
    # Then it is refused and the note is left alone
    with pytest.raises(InvalidFieldValue):
        update_book_status(path, "finished")
    assert "status: To Read" in path.read_text(encoding="utf-8")


def test_updating_to_a_known_status_is_allowed(tmp_path):
    # Given an existing note
    path = create_book_note(_candidate(), tmp_path, status="To Read")

    # When its status is updated to a legal value
    update_book_status(path, "Read")

    # Then the note carries it
    assert "status: Read" in path.read_text(encoding="utf-8")


def test_format_is_normalised_rather_than_taken_as_given(tmp_path):
    # Given a bare string, as `libris add -f` used to write
    path = create_book_note(_candidate(), tmp_path, overrides={"format": "Audiobook"})

    # Then it is stored as the list the Library defines (#69, ADR 0017). This
    # test used to assert the opposite, pinning the gap until it was closed.
    assert read_frontmatter(path)["format"] == ["Audiobook"]


# --- format is a list from a closed vocabulary (#69, ADR 0017) ---
#
# Measured before writing these. The vault holds eleven shapes across two types:
# 1,341 bare strings (only Audiobook variants, 1,280 from one Audible import),
# 873 lists (only Physical/Ebook/Audiobook, written by Obsidian), 35 lowercase,
# 4 empty lists, and 3 genuinely multi-valued notes.


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Audiobook", ["Audiobook"]),
        ("audiobook", ["Audiobook"]),
        ("ebook", ["Ebook"]),
        (["Physical"], ["Physical"]),
        (["Physical", "Audiobook"], ["Physical", "Audiobook"]),
        ([], []),
        (None, []),
        (42, []),
        (["Physical", "", None], ["Physical"]),
    ],
)
def test_every_shape_the_vault_holds_reads_as_a_list(raw, expected):
    # Given each shape measured in the vault
    # When it is read
    # Then it becomes the list of formats it meant
    assert read_formats(raw) == expected


def test_a_multi_format_note_keeps_both(tmp_path):
    # Given a book owned on paper and listened to - three notes say this
    path = create_book_note(
        BookCandidate(title="Changes", authors=["Jim Butcher"]),
        tmp_path,
        overrides={"format": ["Physical", "Audiobook"]},
    )

    # Then nothing is lost
    assert read_frontmatter(path)["format"] == ["Physical", "Audiobook"]


def test_a_bare_string_format_is_stored_as_a_list(tmp_path):
    # Given a writer that still sends a scalar, as the importer did
    path = create_book_note(
        BookCandidate(title="Dune", authors=["Frank Herbert"]),
        tmp_path,
        overrides={"format": "Audiobook"},
    )

    # Then the note holds the shape the Library defines
    assert read_frontmatter(path)["format"] == ["Audiobook"]


def test_a_lowercase_format_is_corrected(tmp_path):
    # Given the lowercase the old help text invited - 35 notes carry it
    path = create_book_note(
        BookCandidate(title="Dune", authors=["Frank Herbert"]),
        tmp_path,
        overrides={"format": "audiobook"},
    )

    # Then case is repaired rather than refused; it names a real format
    assert read_frontmatter(path)["format"] == ["Audiobook"]


def test_a_format_outside_the_vocabulary_is_refused(tmp_path):
    # Given a value no note in the vault holds, and which guessing cannot repair
    # When it is written
    # Then it is refused (ADR 0017)
    with pytest.raises(InvalidFieldValue):
        create_book_note(
            BookCandidate(title="Dune", authors=["Frank Herbert"]),
            tmp_path,
            overrides={"format": "Kindle"},
        )


def test_one_bad_format_among_good_ones_is_refused(tmp_path):
    # Given a list where only one entry is wrong
    with pytest.raises(InvalidFieldValue) as excinfo:
        create_book_note(
            BookCandidate(title="Dune", authors=["Frank Herbert"]),
            tmp_path,
            overrides={"format": ["Physical", "Kindle"]},
        )

    # Then the message names the offending value, not the whole list
    assert "Kindle" in str(excinfo.value)


def test_cleanup_repairs_a_format_obsidian_could_have_written(tmp_path):
    # Given a note edited outside Libris into the old scalar shape
    path = create_book_note(
        BookCandidate(title="Dune", authors=["Frank Herbert"]), tmp_path
    )
    text = path.read_text(encoding="utf-8")
    # The fixture edits the note as Libris spells it; a new spelling must fail here.
    assert "\nformat:\n" in text
    path.write_text(
        text.replace("\nformat:\n", "\nformat: audiobook\n"), encoding="utf-8"
    )

    # When cleanup runs
    updated, data = ensure_frontmatter_fields(path)

    # Then shape and case are repaired, because Obsidian is a writer Libris
    # cannot guard and this is where notes are actually edited (ADR 0017)
    assert updated is True
    assert data["format"] == ["Audiobook"]


def test_cleanup_leaves_a_conforming_format_alone(tmp_path):
    # Given a note already in the right shape
    path = create_book_note(
        BookCandidate(title="Dune", authors=["Frank Herbert"]),
        tmp_path,
        overrides={"format": ["Physical"]},
    )

    # When cleanup runs
    _, data = ensure_frontmatter_fields(path)

    # Then it is unchanged
    assert data["format"] == ["Physical"]


def test_cleanup_turns_an_empty_format_list_into_unset(tmp_path):
    # Given the four notes in the vault holding an empty list
    path = create_book_note(
        BookCandidate(title="Dune", authors=["Frank Herbert"]), tmp_path
    )
    text = path.read_text(encoding="utf-8")
    # The fixture edits the note as Libris spells it; a new spelling must fail here.
    assert "\nformat:\n" in text
    path.write_text(text.replace("\nformat:\n", "\nformat: []\n"), encoding="utf-8")

    # When cleanup runs
    _, data = ensure_frontmatter_fields(path)

    # Then it is unset: an empty list and no value say the same thing
    assert data["format"] is None


def test_cleanup_dry_run_reports_without_writing(tmp_path):
    # Given a note Obsidian left in the old scalar shape
    path = create_book_note(
        BookCandidate(title="Dune", authors=["Frank Herbert"]), tmp_path
    )
    text = path.read_text(encoding="utf-8")
    # The fixture edits the note as Libris spells it; a new spelling must fail here.
    assert "\nformat:\n" in text
    path.write_text(
        text.replace("\nformat:\n", "\nformat: audiobook\n"), encoding="utf-8"
    )
    before = path.read_text(encoding="utf-8")

    # When cleanup is asked what it would do
    updated, data = ensure_frontmatter_fields(path, dry_run=True)

    # Then it reports the repair it would make
    assert updated is True
    assert data["format"] == ["Audiobook"]

    # And the note on disk is untouched, so 1,341 of these can be previewed
    # before any of them is rewritten
    assert path.read_text(encoding="utf-8") == before


def test_a_list_is_refused_for_a_single_valued_field(tmp_path):
    # Given status arriving as a list, which a browser client could send
    # When it is written
    # Then the shape is refused rather than the entries being checked one by
    # one and the list written into frontmatter
    with pytest.raises(InvalidFieldValue) as excinfo:
        create_book_note(_candidate(), tmp_path, overrides={"status": ["Read"]})

    assert "single value" in str(excinfo.value)


def test_a_list_is_still_accepted_for_format(tmp_path):
    # Given the one field that does hold several values
    path = create_book_note(
        _candidate(), tmp_path, overrides={"format": ["Physical", "Audiobook"]}
    )

    # Then it is written
    assert read_frontmatter(path)["format"] == ["Physical", "Audiobook"]


# --- the frontmatter parser (#94) ------------------------------------------


# Frontmatter shaped like the real Shelf's, covering every value type it holds.
# Date coercion is one of the two places the C and Python YAML loaders are known
# to diverge, so real dates appear here rather than being taken on trust. The
# other place is duplicate keys, which no real note carries; that one has its own
# test below rather than being smuggled into a fixture meant to look like a note.
_REPRESENTATIVE_FRONTMATTER = """\
libris_id: lb-2026-0001
title: A Book
authors:
  - "[[An Author]]"
  - Another Author
isbn: 9780000000001
page_count: 321
date_published: 2019-04-01
date_added: 2026-09-04
date_started: 2026-09-01
date_finished:
status: Reading
rating: 4
format:
  - Audiobook
tags: Book
genres: null
referred_by: "A friend: with a colon"
"""


def test_the_frontmatter_parser_agrees_with_the_python_loader(monkeypatch):
    # Given frontmatter shaped like the Shelf's, dates and all
    import yaml

    from libris import note_format

    if not hasattr(yaml, "CSafeLoader"):
        pytest.skip("PyYAML was built without libyaml, so there is one loader")

    # When it is parsed through the C loader and through the pure-Python one
    ours = note_format.parse_frontmatter_yaml(_REPRESENTATIVE_FRONTMATTER)
    monkeypatch.setattr(note_format, "_HAVE_LIBYAML", False)
    theirs = note_format.parse_frontmatter_yaml(_REPRESENTATIVE_FRONTMATTER)

    # Then they agree on the values and, separately, on their types. Equality
    # would catch a date returned as the string that spells it, since those do
    # not compare equal; what it misses is two types that do - `True == 1` and
    # `1 == 1.0` - and this Shelf holds 2,898 integer page counts and 888
    # integer ratings for such a divergence to hide in.
    assert ours == theirs
    assert {k: type(v) for k, v in ours.items()} == {
        k: type(v) for k, v in theirs.items()
    }
    assert isinstance(ours["rating"], int) and not isinstance(ours["rating"], bool)


# --- a date field is text (#143, ADR 0030) -----------------------------------


@pytest.mark.parametrize("libyaml", [True, False])
def test_a_date_field_reads_as_the_text_it_spells(monkeypatch, libyaml):
    # Given a note whose dates are spelled both ways the Shelf spells them -
    # unquoted, as 3,055 `date_added` values are, and quoted, as Libris wrote
    # its own stamps
    import yaml

    from libris import note_format

    if libyaml and not hasattr(yaml, "CSafeLoader"):
        pytest.skip("PyYAML was built without libyaml")
    monkeypatch.setattr(note_format, "_HAVE_LIBYAML", libyaml)

    text = (
        "date_added: 2020-05-05\n"
        "date_finished: '2020-05-06'\n"
        "date_published: '1994'\n"
        "date_modified: 2026-08-01T10:00:00\n"
    )

    # When it is read
    frontmatter = note_format.parse_frontmatter_yaml(text)

    # Then every one is the text it spells, whichever way it was written
    assert frontmatter == {
        "date_added": "2020-05-05",
        "date_finished": "2020-05-06",
        "date_published": "1994",
        "date_modified": "2026-08-01T10:00:00",
    }
    assert all(type(value) is str for value in frontmatter.values())


def test_rewriting_frontmatter_leaves_an_unquoted_date_as_it_was():
    # Given frontmatter spelled the way almost every note on the Shelf is
    from libris.note_format import dump_frontmatter_yaml, parse_frontmatter_yaml

    text = 'title: Dune\ndate_added: 2020-05-05\ndate_published: "1965"\n'

    # When it is read and written back unchanged
    written = dump_frontmatter_yaml(parse_frontmatter_yaml(text))

    # Then not a byte moves. Reading dates as text must not make every note
    # Libris touches sprout quotes around its dates.
    assert written == text


def test_a_stamped_date_is_written_as_the_shelf_writes_one(tmp_path):
    # Given a new Book Note, which Libris stamps with today's `date_added`
    book = BookCandidate(
        title="Dune",
        authors=["Frank Herbert"],
        isbn="9780441013593",
        published_date="1965",
    )

    # When it is written
    path = create_book_note(book, tmp_path)

    # Then its date is spelled unquoted, like the 3,055 already on the Shelf,
    # and reads back as the same text. A year alone stays quoted: unquoted,
    # YAML would read it as a number.
    content = path.read_text(encoding="utf-8")
    assert f"date_added: {date.today().isoformat()}\n" in content
    assert 'date_published: "1965"\n' in content
    assert read_frontmatter(path)["date_added"] == date.today().isoformat()


# --- a write leaves the rest of the block as Obsidian wrote it (#146) ---------

# A note as it sits on the real Shelf, spelled by Obsidian: lists indented under
# their key, empty values blank, double quotes only where a value needs them, a
# long title on one line, and an ISBN-10 whose leading zero the quotes protect.
OBSIDIAN_NOTE = (
    "---\n"
    "libris_id: 01KQZVQW00D7JWAYAPCBKYYSV4\n"
    "title: \"Brotopia: Breaking Up the Boys' Club of Silicon Valley, a Title Long"
    ' Enough That PyYAML Would Fold It"\n'
    "authors:\n"
    "  - Emily Chang\n"
    '  - "#1 Co-Author"\n'
    'isbn: "0786937521"\n'
    "page_count: 497\n"
    "date_published: 2011-10-04\n"
    'google_books_id: ""\n'
    "genres: []\n"
    "series:\n"
    "status: Read\n"
    "priority:\n"
    "rating:\n"
    "format:\n"
    "  - Audiobook\n"
    "tags: Book\n"
    "date_added: 2026-05-07\n"
    'date_published_year: "2018"\n'
    "---\n"
    "\n"
    "# Brotopia\n"
)


def test_setting_one_field_changes_only_its_line(tmp_path):
    # Given a note written in Obsidian's style
    from libris.markdown import set_frontmatter_fields

    path = tmp_path / "Brotopia - Emily Chang.md"
    path.write_text(OBSIDIAN_NOTE, encoding="utf-8")

    # When Libris sets its rating
    set_frontmatter_fields(path, {"rating": 4})

    # Then the rating's line is the only line that moved. Before #146 the whole
    # block was restyled, and 2,969 of 3,083 notes changed on any write.
    before = OBSIDIAN_NOTE.splitlines()
    after = path.read_text(encoding="utf-8").splitlines()
    changed = [(old, new) for old, new in zip(before, after, strict=True) if old != new]
    assert changed == [("rating:", "rating: 4")]


@pytest.mark.parametrize("value", ["0786937521", "08", "0o17", "1e3"])
def test_a_string_yaml_1_2_would_read_as_a_number_is_written_quoted(value):
    # Given text that YAML 1.1 reads as a string but YAML 1.2 - which Obsidian
    # reads by - reads as a number. `isbn: 0786937521` unquoted is the integer
    # 786937521 there, and 35 ISBNs on the Shelf have that shape.
    from libris.note_format import dump_frontmatter_yaml, parse_frontmatter_yaml

    # When it is written
    written = dump_frontmatter_yaml({"isbn": value})

    # Then it is quoted, so every reader reads it as the text it is
    assert written == f'isbn: "{value}"\n'
    assert parse_frontmatter_yaml(written) == {"isbn": value}


def test_the_frontmatter_parser_agrees_on_a_repeated_key():
    # Given frontmatter that names the same key twice - the second of the two
    # places the loaders are known to diverge, and one no real note takes, so it
    # is asserted here rather than inferred from the Shelf
    import yaml

    from libris.note_format import parse_frontmatter_yaml

    repeated = "title: First\nstatus: To Read\ntitle: Second\n"

    # When it is parsed both ways
    ours = parse_frontmatter_yaml(repeated)
    theirs = yaml.load(repeated, Loader=yaml.SafeLoader)

    # Then both take the last value, and neither refuses the note
    assert ours == theirs
    assert ours["title"] == "Second"


def test_the_frontmatter_parser_reports_damage_rather_than_guessing():
    # Given a frontmatter block that is not YAML
    import yaml

    from libris.note_format import parse_frontmatter_yaml

    # When it is parsed
    # Then it raises the error every caller already catches, whichever loader
    # is compiled in
    with pytest.raises(yaml.YAMLError):
        parse_frontmatter_yaml("title: [unclosed\nstatus: To Read\n")


# --- reading an isbn (#105) -------------------------------------------------


@pytest.mark.parametrize(
    "stored,expected",
    [
        (9780000000001, "9780000000001"),  # unquoted, so YAML gives an int
        ("9780000000001", "9780000000001"),
        ("978-0-00-000000-1", "9780000000001"),  # hyphens are separators
        ("  9780000000001  ", "9780000000001"),
        ("161145736x", "161145736X"),  # the ISBN-10 check digit, either case
        ("161145736X", "161145736X"),
        (786937521, "786937521"),  # a nine-digit value is still read, not judged
        ("B000ELJ9NO", "B000ELJ9NO"),  # an ASIN: not an ISBN, but still itself
        ("‎ 979-8212632836", "9798212632836"),  # a pasted LTR mark
        ("978 1440629570", "9781440629570"),  # a no-break space
        (None, None),
        ("", None),
        ("   ", None),
        (True, None),  # bool is an int in Python; `isbn: true` names nothing
        ([], None),
    ],
    ids=[
        "int",
        "plain-string",
        "hyphenated",
        "padded",
        "lowercase-x",
        "uppercase-x",
        "nine-digits",
        "asin",
        "ltr-mark",
        "no-break-space",
        "none",
        "empty",
        "whitespace",
        "bool",
        "wrong-type",
    ],
)
def test_read_isbn_reads_the_identifier_however_it_is_written(stored, expected):
    # Given an isbn value in one of the shapes the real Shelf holds
    from libris.note_format import read_isbn

    # When it is read
    # Then it comes back as the identifier it names, or None when it names none
    assert read_isbn(stored) == expected


def test_two_notes_spelling_one_isbn_differently_read_the_same(tmp_path):
    # Given the same ISBN written unquoted in one note and hyphenated in another
    from libris.markdown import BookNote
    from libris.note_format import read_isbn

    for name, line in [
        ("bare.md", "isbn: 9780000000001"),
        ("hyph.md", 'isbn: "978-0-00-000000-1"'),
    ]:
        (tmp_path / name).write_text(
            f"---\ntitle: A Book\n{line}\nstatus: To Read\n---\n\nBody.\n",
            encoding="utf-8",
        )

    # When both are read through the Book Note accessor
    bare = BookNote.read(tmp_path / "bare.md")
    hyphenated = BookNote.read(tmp_path / "hyph.md")

    # Then they name one identifier, which is what makes them one book to
    # find_existing and to the duplicate check
    assert bare.isbn == hyphenated.isbn == read_isbn("9780000000001")
