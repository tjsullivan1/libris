"""Markdown file operations for book notes (frontmatter, creation, enrichment)."""

import errno
import hashlib
import os
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, BinaryIO, Callable, Dict, Literal, Optional

import yaml
from titlecase import titlecase

from .api import BookCandidate
from .matching import normalize_for_match
from .note_format import (
    MODELLED_FIELDS,
    SUPERSEDED_IDS_FIELD,
    has_description_callout,
    mint_libris_id,
    normalize_field_value,
    parse_frontmatter_yaml,
    read_formats,
    read_isbn,
    read_superseded_ids,
    render_body,
    render_description_callout,
    validate_field_value,
)

# Values a Book Note starts with when nothing else supplies them.
_FRONTMATTER_DEFAULTS = {"tags": "Book", "status": "To Read"}

# Derived from the canonical field order so a note written today and a note
# migrated from years ago cannot disagree about which fields exist.
DEFAULT_FRONTMATTER = {
    name: _FRONTMATTER_DEFAULTS.get(name) for name in MODELLED_FIELDS
}

# Maps legacy/extraneous field names to their canonical counterparts.
FIELD_MIGRATIONS = {
    "Type Read": "format",
    "Rating out of 5": "rating",
    "Referred From": "referred_by",
    "Date Read": "date_finished",
    "Date Added": "date_added",
    "Status": "status",
    "Author": "authors",
    # Names this code wrote before ADR 0005 settled the canonical vocabulary.
    "author": "authors",
    "published_date": "date_published",
    "thumbnail": "cover_thumbnail",
}


def _normalize_author(name: str) -> str:
    """Reduce an author name as written to the name itself.

    Args:
        name: The value as it appears in frontmatter, possibly a wikilink.

    Returns:
        The plain name, with runs of whitespace collapsed.
    """
    unlinked = re.sub(r"^\[\[(.+?)\]\]$", r"\1", name.strip())
    if "|" in unlinked:
        unlinked = unlinked.split("|", 1)[1]
    return re.sub(r"\s+", " ", unlinked).strip()


def tidy_author(name: str) -> str:
    """Collapse whitespace in an author value, leaving a wikilink intact.

    Narrower than `_normalize_author` on purpose. That one is for deciding
    whether two spellings mean the same person; this is for deciding what to
    write, and unwrapping a wikilink would delete the edge to an author's note
    (ADR 0018).

    Args:
        name: The value as it appears in frontmatter.

    Returns:
        The value with runs of whitespace collapsed.
    """
    return re.sub(r"\s+", " ", name).strip()


@dataclass
class BookNote:
    """A book on the Shelf: the file it lives in, its frontmatter, and its body.

    Frontmatter arrives from YAML and may hold any shape, so the accessors
    normalise rather than trusting what is on disk. Callers should read fields
    through them instead of reaching into the dict, which is how this code came
    to read a key that no note has ever carried.
    """

    path: Path
    frontmatter: dict[str, Any]
    body: str = ""

    @classmethod
    def read(cls, path: Path) -> "BookNote | None":
        """Read a Book Note from disk.

        Args:
            path: Path to the Markdown file.

        Returns:
            The Book Note, or None if the file has no parseable frontmatter.
        """
        frontmatter = read_frontmatter(path)
        if frontmatter is None:
            return None
        return cls(path=path, frontmatter=frontmatter)

    @property
    def libris_id(self) -> str | None:
        """The note's stable identity, or None until the vault is migrated."""
        value = self.frontmatter.get("libris_id")
        return value.strip() if isinstance(value, str) and value.strip() else None

    @property
    def superseded_ids(self) -> list[str]:
        """Identities of Book Notes merged into this one (ADR 0014).

        Returns:
            The superseded identities, or an empty list. A note that has never
            absorbed another does not carry the field at all.
        """
        return read_superseded_ids(self.frontmatter.get(SUPERSEDED_IDS_FIELD))

    @property
    def title(self) -> str | None:
        """The book's title, or None when absent or blank."""
        value = self.frontmatter.get("title")
        return value.strip() if isinstance(value, str) and value.strip() else None

    @property
    def isbn(self) -> str | None:
        """The book's ISBN as text, or None when the note names none.

        Read through `read_isbn` rather than off the dict, because how a note
        happens to be quoted decided whether it could be found (#105).
        """
        return read_isbn(self.frontmatter.get("isbn"))

    @property
    def authors(self) -> list[str]:
        """The book's authors as a list of plain, non-empty names.

        Accepts the list every note in the vault carries and the bare string that
        older notes used. Anything else counts as naming no author at all.

        Names are normalised for use rather than taken literally: some notes hold
        an author as a wikilink to their own note, and some carry stray inner
        whitespace. Both would otherwise leak into filenames and defeat matching,
        while the note itself keeps whatever it holds.
        """
        value = self.frontmatter.get("authors")
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, list):
            return []
        names = (_normalize_author(name) for name in value if isinstance(name, str))
        return [name for name in names if name]

    @property
    def first_author(self) -> str | None:
        """The first named author, or None when the note names none."""
        authors = self.authors
        return authors[0] if authors else None

    @property
    def canonical_filename(self) -> str | None:
        """The 'Title - Author.md' filename this note should have.

        Returns:
            The filename, or None when the note lacks a title or an author.
        """
        if self.title is None or self.first_author is None:
            return None
        return sanitize_filename(f"{self.title} - {self.first_author}.md")


def sanitize_filename(name: str) -> str:
    """Removes invalid characters for a filename and collapses whitespace."""
    name = re.sub(r'[\\/*?:"<>|]', "", name)
    name = re.sub(r"\s+", " ", name)
    return name.strip()


# Patterns for publisher/format annotations (not genuine title content)
_BRACKET_ANNOTATION_PAT = re.compile(r"\s*[\[\{][^\[\]\{\}]*[\]\}]")
_WHITESPACE_PAT = re.compile(r"\s+")


def standardize_title(raw: Optional[str]) -> Optional[str]:
    """Standardize a book title: strip annotations, normalize whitespace, apply title case."""
    if not raw or not isinstance(raw, str):
        return raw
    title = raw.strip()
    # Remove bracket annotations like [Illustrated], {Kindle Edition}
    title = _BRACKET_ANNOTATION_PAT.sub("", title)
    # Collapse multiple whitespace to single space
    title = _WHITESPACE_PAT.sub(" ", title).strip()
    # Apply title case (NYT Manual of Style rules)
    title = titlecase(title)
    return title


def create_book_note(
    book: BookCandidate,
    vault_path: Path,
    status: str = "To Read",
    overrides: Dict[str, Any] | None = None,
) -> Path:
    """Creates a Markdown note for a book in the specified vault path.

    Args:
        book: The candidate whose metadata seeds the note.
        vault_path: Directory where the note will be written.
        status: Default reading status (overridden if 'status' is in overrides).
        overrides: Optional dict of frontmatter fields to set/override.
            Keys must exist in DEFAULT_FRONTMATTER.
    """
    filename = sanitize_filename(f"{book.title} - {', '.join(book.authors[:1])}.md")
    file_path = vault_path / filename

    if overrides and "status" in overrides:
        status = overrides["status"]
        overrides = {k: v for k, v in overrides.items() if k != "status"}

    validate_field_value("status", status)
    added = date.today()
    frontmatter = {
        **DEFAULT_FRONTMATTER,
        "libris_id": mint_libris_id(added),
        "title": book.title,
        "authors": book.authors,
        "isbn": book.isbn,
        "page_count": book.page_count,
        "date_published": book.published_date,
        "google_books_id": book.google_books_id,
        "cover_thumbnail": book.thumbnail,
        "genres": book.genres,
        "status": status,
        "date_added": added.isoformat(),
    }

    if overrides:
        for key, value in overrides.items():
            if key not in DEFAULT_FRONTMATTER:
                raise ValueError(
                    f"Unknown frontmatter field: '{key}'. "
                    f"Valid fields: {', '.join(DEFAULT_FRONTMATTER.keys())}"
                )
            value = normalize_field_value(key, value)
            validate_field_value(key, value)
            frontmatter[key] = value

    yaml_content = yaml.dump(frontmatter, sort_keys=False, allow_unicode=True)

    body = render_body(book.title, "", book.description)
    write_note(file_path, f"---\n{yaml_content}---\n\n{body}")
    return file_path


def update_book_status(file_path: Path, new_status: str) -> None:
    r"""Set the status in a Book Note's frontmatter, leaving the body untouched.

    This used to be an unanchored, uncounted `re.sub` for `r"(status:\s*)(.*)"` over
    the whole file, on the reasoning that a regex disturbs a note less than a
    YAML round-trip does. The reasoning was right and the region was wrong: it
    rewrote every line containing `status:` anywhere in the file, including a
    reader's own sentences about the book (#92). `set_frontmatter_fields` keeps
    the intent - the body is carried across unchanged - and confines the
    edit to the frontmatter block, where the field actually lives.

    Args:
        file_path: The Book Note to write.
        new_status: The status to set, from the Library's own vocabulary.

    Raises:
        InvalidFieldValue: If the status is not one the Library defines.
        FrontmatterUnreadable: If the note has no frontmatter block to write to.
    """
    validate_field_value("status", new_status)
    set_frontmatter_fields(file_path, {"status": new_status})


class FrontmatterUnreadable(ValueError):
    """A Book Note's frontmatter could not be parsed, so it was not written to."""


class NoteChanged(Exception):
    """A Book Note is no longer the file a decision about it was made from.

    Raised before anything is written. A change decided against a report of a
    note - which line is which, which author a twin is - is only sound while
    the note is exactly as reported, and no narrower check has held: counts,
    then damaged sequences, each met an edit they could not see (#129 reviews).
    """


def split_frontmatter(content: str) -> Optional[tuple[str, str]]:
    """Split a note into its frontmatter block and everything after it.

    The one place that knows how a frontmatter fence is written (ADR 0028).
    Nine hand-rolled splits in three disagreeing dialects preceded it, and a
    test enforces that a tenth is not added.

    Deliberately not a regex. The body is returned exactly as it was found -
    every byte after the line closing the block, blank lines included - because
    an update to one field must not reflow a reader's own writing (ADR 0023).

    Args:
        content: The whole file.

    Returns:
        The YAML text and the body, or None if there is no closed frontmatter
        block at the start of the file.
    """
    lines = content.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return None
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            return "".join(lines[1:index]), "".join(lines[index + 1 :])
    return None


def unterminated_frontmatter(content: str) -> str | None:
    """The text of a frontmatter block that was opened and never closed.

    `split_frontmatter` returns None for such a note, because there is no
    closing fence to split on. That is the right answer for anything writing a
    note back, and the wrong one for a check whose subject is damaged notes: a
    note like this states its `libris_id` on the second line, and reading it as
    prose loses that (#75).

    Lives here rather than at the call site so that how a fence is written stays
    knowledge this module holds alone (ADR 0028).

    Args:
        content: The whole file.

    Returns:
        Everything after the opening fence, or None when the file does not open
        one - in which case it has no frontmatter rather than a broken block.
    """
    lines = content.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return None
    return "".join(lines[1:])


def note_newline(path: Path) -> str:
    """The line ending a Book Note already uses.

    Args:
        path: The Book Note. It need not exist.

    Returns:
        The note's dominant line ending. A note that does not exist yet, or holds
        no line at all, gets the platform's - there is nothing to preserve, and
        creating a note is not the same act as rewriting one.
    """
    try:
        raw = path.read_bytes()
    except OSError:
        return os.linesep
    return _dominant_newline(raw)


def note_fingerprint(path: Path) -> str:
    """The SHA-256 of a Book Note's bytes as they stand on disk.

    What a write carries to say which note it was worked out from. A write that
    reads the note itself wants `read_note_with_fingerprint`, which hashes the
    one read rather than reading twice.

    Args:
        path: The Book Note to fingerprint.

    Returns:
        The hex digest of its bytes.

    Raises:
        FileNotFoundError: If the note is not there.
    """
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_note_with_fingerprint(path: Path) -> tuple[str, str]:
    """Read a note's text, and the fingerprint of the bytes it came from.

    One read, so the text and the fingerprint describe the same note - reading
    again to hash is a second chance for the file to be replaced (#127 review).
    The text is newline-normalised exactly as `read_text` leaves it, so nothing
    downstream sees a carriage return it did not see before.

    Args:
        path: The Book Note to read.

    Returns:
        Its text, and the hex digest of the bytes it was decoded from.

    Raises:
        FileNotFoundError: If the note is not there.
        UnicodeDecodeError: If it is not UTF-8, as `read_text` raises.
    """
    raw = path.read_bytes()
    content = raw.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
    return content, hashlib.sha256(raw).hexdigest()


def verify_note_unchanged(path: Path, expected_sha256: str) -> None:
    """Refuse to go on if a note is no longer the one that was read.

    For an act that destroys a note rather than writing one. `rewrite_note`
    checks the note it writes, but a merge also deletes the note it merged away,
    and a deletion writes nothing for that check to guard. A secondary edited
    after the merge read it holds writing the merged note never saw, so deleting
    it destroys the only copy of that writing (#133 review).

    Args:
        path: The Book Note to check.
        expected_sha256: The SHA-256 its bytes had when it was read.

    Raises:
        FileNotFoundError: If the note is not there.
        NoteChanged: If its bytes no longer match.
    """
    if note_fingerprint(path) != expected_sha256:
        raise NoteChanged(f"{path.name} has changed since it was read.")


def _dominant_newline(raw: bytes) -> str:
    """The line ending most of a note's lines use, or the platform's if none."""
    crlf = raw.count(b"\r\n")
    bare_lf = raw.count(b"\n") - crlf
    if not crlf and not bare_lf:
        return os.linesep
    return "\r\n" if crlf >= bare_lf else "\n"


def _encode_with_newline(content: str, newline: str) -> bytes:
    """Encode a note's text with one line ending throughout.

    The content is normalised to "\\n" before the ending is applied, because
    applying "\\r\\n" to text that already holds it is what produced "\\r\\r\\n"
    on 1,345 notes once already - see the comment in
    `migrate.plan_format_note_migration`.
    """
    content = content.replace("\r\n", "\n").replace("\r", "\n")
    if newline != "\n":
        content = content.replace("\n", newline)
    return content.encode("utf-8")


def write_note(path: Path, content: str) -> None:
    """Write a Book Note, keeping the line endings it already had.

    `Path.write_text` leaves `newline=None`, and that translates every "\\n" to
    `os.linesep` on the way out. It made every write platform-dependent: this
    Shelf is stored CRLF, so reading a note on macOS and writing it back turned
    all 3,059 of them into LF and showed every line as changed (#100). Writing
    bytes takes the platform out of it.

    Creates the file when it does not exist. A write that means to change a note
    already on the Shelf wants `rewrite_note`, which refuses instead.

    Args:
        path: The Book Note to write.
        content: The note's full text.
    """
    path.write_bytes(_encode_with_newline(content, note_newline(path)))


def rewrite_note(path: Path, content: str, expected_sha256: str | None = None) -> None:
    """Replace the text of a Book Note that exists, never creating one.

    `write_note` opens for writing, which creates a missing file. A write that
    read a note and then found it removed before the write - Obsidian renaming
    it, a sync client moving it - put the old note back under its old name and
    reported success (#127 review): a Book Note resurrected by the act of
    marking it Read, beside the renamed copy of itself. Opening the existing
    file for update cannot create one.

    Refusing to create a note covers one removed before the write. A note
    *replaced* at the same path in that gap - a sync client writing a newer copy,
    a save in Obsidian - is still a file, so there is nothing for that check to
    see, and the rewrite puts back text worked out before the edit existed.
    `expected_sha256` closes that: the note the caller read is named, and any
    other note under that name is refused (#132).

    Args:
        path: The Book Note to rewrite.
        content: The note's full new text.
        expected_sha256: The SHA-256 the note's bytes must have when opened for
            writing, or None to write without checking.

    Raises:
        FileNotFoundError: If the note is not there to rewrite, or stopped being
            the file at that path before the write finished.
        NoteChanged: If `expected_sha256` is given and the note's bytes do not
            match it. Nothing is written.
    """
    with path.open("r+b") as handle:
        raw = handle.read()
        # Checked through the handle that will write, as `edit_note` does, so the
        # note compared is the note written. An edit landing after this read is
        # not caught: no lock Obsidian honours exists, and a plain file offers no
        # atomic compare-and-write (#129 sixth review).
        if expected_sha256 is not None and (
            hashlib.sha256(raw).hexdigest() != expected_sha256
        ):
            raise NoteChanged(f"{path.name} has changed since it was read.")
        _write_through(handle, path, content, raw)


def _write_through(handle: BinaryIO, path: Path, content: str, raw: bytes) -> None:
    """Replace a note's text through the handle it was read from.

    Everything that decided the new text was read through this handle, so the
    write goes back through it rather than reopening the path - a reopen can
    reach a different file than the one that was read (#127 review). On Windows
    the open handle also stops anyone else renaming or removing the note until
    this returns.

    Args:
        handle: The note, open for update and positioned anywhere.
        path: The path the note was opened by.
        content: The note's full new text.
        raw: The bytes the handle held when it was read, for the line ending.

    Raises:
        FileNotFoundError: If `path` no longer names the open file, or the file
            was unlinked while it was written.
    """
    encoded = _encode_with_newline(content, _dominant_newline(raw))

    # POSIX lets another process rename or replace a file this one holds open.
    # Checked before writing, so a note that has moved is reported with nothing
    # written, rather than as a write under a name that no longer reaches it.
    if not os.path.samestat(os.stat(path), os.fstat(handle.fileno())):
        raise FileNotFoundError(errno.ENOENT, "moved before it was written", str(path))

    handle.seek(0)
    handle.write(encoded)
    handle.truncate()
    handle.flush()

    # Unlinked during the write itself: the bytes went to a file no name reaches,
    # a write that happened to nothing. A rename in that same instant is not
    # caught, and needs none - the write reached the note, under its new name.
    if os.fstat(handle.fileno()).st_nlink == 0:
        raise FileNotFoundError(
            errno.ENOENT, "removed while it was being written", str(path)
        )


def edit_note(
    file_path: Path,
    decide: Callable[[dict[str, Any], str], tuple[dict[str, Any], str | None]],
    expected_sha256: str | None = None,
) -> tuple[dict[str, Any], str]:
    """Change a Book Note's frontmatter and body in one write.

    The note is read, decided on and written through one open handle, so what
    is written was decided from the file it lands in (#127 review), and a change
    that touches both halves cannot half-land. Repairing a lost character needs
    exactly that: the title and the `# Title` heading rendered from it are the
    same damage in two places (#78).

    Field order is preserved for keys the note already carries; new keys are
    appended, so an edit does not reshuffle a note. A body left alone is carried
    across byte for byte rather than re-rendered (ADR 0023).

    Args:
        file_path: The Book Note to write.
        decide: Handed the note's frontmatter and body as they stand, returning
            the fields to set and the body to write - or None for the body to
            leave it alone. It may raise to refuse the write, and nothing is
            written.
        expected_sha256: The SHA-256 the note's bytes must have when read for
            writing, or None to write without checking. Catches an edit made
            before that read; an edit landing between that read and the write
            is not caught (#129 sixth review).

    Returns:
        The frontmatter and body as they now stand on disk.

    Raises:
        FrontmatterUnreadable: If the file has no parseable frontmatter block,
            or is not UTF-8. Refused rather than repaired: this is the write
            path for named fields, not the place to rebuild a broken note.
        FileNotFoundError: If the note is not there, or stops being the file at
            that path before it is written. It is never recreated.
        NoteChanged: If `expected_sha256` is given and the note's bytes, when
            read for writing, do not match it. Nothing is written.
    """
    with file_path.open("r+b") as handle:
        raw = handle.read()
        # Checked through the handle that will write, so the note compared is
        # the note written. `expected_sha256` is the SHA-256 of its bytes when
        # whatever `decide` acts on was read; an edit made between then and this
        # read refuses the change. An edit landing after this read - while
        # `decide` runs, a pure computation over one note - is not caught: no
        # lock Obsidian honours exists, and a plain file offers no atomic
        # compare-and-write (#129 sixth review).
        if expected_sha256 is not None and (
            hashlib.sha256(raw).hexdigest() != expected_sha256
        ):
            raise NoteChanged(f"{file_path.name} has changed since it was read.")
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise FrontmatterUnreadable(
                f"{file_path.name} is not UTF-8 text."
            ) from None

        split = split_frontmatter(content)
        if split is None:
            raise FrontmatterUnreadable(
                f"{file_path.name} has no readable frontmatter."
            )

        frontmatter_yaml, body = split
        try:
            data = parse_frontmatter_yaml(frontmatter_yaml)
        except yaml.YAMLError as exc:
            raise FrontmatterUnreadable(f"{file_path.name}: {exc}") from None
        if not isinstance(data, dict):
            raise FrontmatterUnreadable(f"{file_path.name} has no frontmatter mapping.")

        changes, new_body = decide(dict(data), body)
        if not changes and new_body is None:
            return data, body

        data.update(changes or {})
        if new_body is not None:
            body = new_body
        rendered = yaml.dump(data, sort_keys=False, allow_unicode=True).strip()
        _write_through(handle, file_path, "---\n" + rendered + "\n---\n" + body, raw)
    return data, body


def set_frontmatter_fields(
    file_path: Path,
    updates: dict[str, Any] | Callable[[dict[str, Any]], dict[str, Any]],
) -> dict[str, Any]:
    """Set named frontmatter fields on a Book Note, leaving the body untouched.

    The body is carried across exactly rather than re-rendered. Every other
    write path here reconstructs a note - `ensure_frontmatter_fields` re-dumps
    the YAML and reflows the body on every pass - which is right for a repair
    pass and wrong for setting a status.

    Args:
        file_path: The Book Note to write.
        updates: Field names and the values to set them to - or a function
            handed the note's current frontmatter that returns them, for a
            write whose values depend on what the note already holds. It may
            raise to refuse the write, and nothing is written.

    Returns:
        The frontmatter as it now stands on disk.

    Raises:
        FrontmatterUnreadable: If the file has no parseable frontmatter block,
            or is not UTF-8.
        FileNotFoundError: If the note is not there, or stops being the file at
            that path before it is written. It is never recreated.
    """

    def _decide(data: dict[str, Any], _body: str) -> tuple[dict[str, Any], None]:
        return (updates(data) if callable(updates) else updates), None

    data, _ = edit_note(file_path, _decide)
    return data


def list_books(vault_path: Path) -> list[Path]:
    """Every Markdown file on the Shelf, in one order on every platform.

    `os.scandir` returns entries in whatever order the filesystem keeps them:
    case-insensitive on NTFS, hash order on ext4. So every Shelf-wide command -
    an export, a migration's diffs, which of two same-keyed notes an index keeps
    - answered in a different order on Linux than on Windows (#144 review).

    Sorting `Path` objects would not settle it either. `WindowsPath` compares
    ignoring case and `PosixPath` does not, and on the real 3,073-note Shelf the
    two orders disagree in 1,417 positions. The key is the filename itself,
    case-folded, with the exact name breaking a tie - the order NTFS already
    returns, so nothing moves on Windows, now produced by the code rather than
    by the filesystem.

    Args:
        vault_path: The Shelf to list.

    Returns:
        The path of every `.md` file directly on the Shelf, each assumed to be a
        Book Note.
    """
    paths = [
        Path(entry.path)
        for entry in os.scandir(vault_path)
        if entry.is_file() and entry.name.endswith(".md")
    ]
    return sorted(paths, key=lambda path: (path.name.casefold(), path.name))


# What a longer title adds when it is a companion volume rather than the book:
# a workbook, a study guide, a summary. Measured on the real Shelf, the only
# prefix-shaped false pair was "The 7 Habits of Highly Effective People" and
# the same title plus "Workbook" (#136).
_COMPANION_VOLUMES = frozenset(
    normalize_for_match(marker)
    for marker in (
        "workbook",
        "study guide",
        "summary",
        "companion",
        "journal",
        "handbook",
        "a reader's guide",
        "reader's guide",
    )
)


def _is_subtitle_variant(shorter: str, longer: str) -> bool:
    """Whether `longer` looks like `shorter` with a subtitle, not another book.

    Containment anywhere was the old rule, and on this Shelf it was wrong every
    time: all 8 pairs it offered were two different books - "Mercy" and "Long
    Road to Mercy", "American Assassin" and "Kill Shot: An American Assassin
    Thriller", "Freakonomics" and "SuperFreakonomics". A title that merely ends
    with or carries another names a sequel, a prequel or a series, which is a
    relationship rather than an identity (#136).

    What a real subtitle variant does is *begin* with the whole title and add to
    it - "The Brass Verdict" and "The Brass Verdict: A Novel". So the shorter
    title must be a prefix, ending on a word boundary, and what the longer adds
    must not name a companion volume.

    Args:
        shorter: The title that would be the book, normalized. The caller
            compares every pair both ways round, so this is not guaranteed to
            be the shorter string - it is simply the one being tested as the
            prefix, and the answer is False when it is the longer of the two.
        longer: The title that would be the variant, normalized.

    Returns:
        True when the pair is worth offering to a person to settle.
    """
    if not longer.startswith(shorter):
        return False

    # On a word boundary: "Freakonomics" is a prefix of "SuperFreakonomics"
    # only by accident of spelling, and this Shelf holds that exact pair.
    rest = longer[len(shorter) :]
    if not rest.startswith(" "):
        return False

    return not _names_a_companion(rest.strip())


def _names_a_companion(suffix: str) -> bool:
    """Whether what a longer title adds names a companion volume.

    Matched as a whole-word prefix of the suffix, not by equality: a workbook
    is still a workbook when it says "Workbook: Revised" or "Workbook Edition",
    and comparing the whole suffix let both through (#141 review).

    The markers are normalized through `normalize_for_match`, because that is
    what the titles they are compared against have been through. Written by
    hand, "readers guide" never matched anything at all: an apostrophe becomes
    a space, so `A Reader's Guide` arrives here as `a reader s guide`.

    Args:
        suffix: What the longer title adds, normalized.

    Returns:
        True when the suffix names a companion volume rather than a subtitle.
    """
    return any(
        suffix == marker or suffix.startswith(f"{marker} ")
        for marker in _COMPANION_VOLUMES
    )


def read_shelf_notes(vault_path: Path) -> list[BookNote]:
    """Parse every readable Book Note on the Shelf, once.

    Reading and parsing notes is what a Shelf-wide query costs: profiled
    against the real 3,073-note Shelf, `read_frontmatter` accounts for 85% of
    `libris duplicates`, most of it inside `yaml.load`. Anything that needs the
    whole Shelf more than once should read it here and pass the result on,
    rather than reaching for the disk again (#107).

    Args:
        vault_path: The Shelf to read.

    Returns:
        A Book Note per file that could be parsed, in the order the Shelf lists
        them. A file that cannot be read is left out rather than raising, the
        same answer `find_duplicates` has always given for one.
    """
    return [
        note
        for note in (BookNote.read(path) for path in list_books(vault_path))
        if note is not None
    ]


def find_duplicate_candidates(
    vault_path: Path, notes: list[BookNote] | None = None
) -> list[list[BookNote]]:
    """Find pairs of Book Notes that may describe one Book.

    Matched by title shape rather than by a shared identifier, which is a
    judgement rather than a fact, so these are offered to a person and never
    merged automatically (ADR 0018).

    The rule was once containment anywhere, and by the time the real duplicates
    had been merged away it was wrong every time: all 8 pairs it still offered
    were two different books. It now asks for the shape a subtitle variant
    actually has - see `_is_subtitle_variant` - which leaves none of those 8
    and keeps the variants the suite encodes (#136).

    Pairs that `find_duplicates` already reports are left out; they are settled,
    not candidates.

    Args:
        vault_path: The Shelf to search.
        notes: The Shelf already parsed, when the caller has it. This function
            parsed the Shelf itself and then called `find_duplicates`, which
            parsed it again - 6,146 parses of a 3,073-note Shelf for one
            answer (#107).

    Returns:
        Pairs of Book Notes, shorter title first, ordered by author then title.
    """
    if notes is None:
        notes = read_shelf_notes(vault_path)

    # Every readable note goes to `find_duplicates`, not just the named ones
    # below: a note missing an author can still share an ISBN, and settling
    # pairs from a smaller Shelf than it used to see would change what this
    # reports rather than only what it costs.
    settled = set()
    for group in find_duplicates(vault_path, notes):
        for a in group:
            for b in group:
                if a != b:
                    settled.add(frozenset((str(a), str(b))))

    named = [note for note in notes if note.title and note.first_author]

    by_author: dict[str, list[BookNote]] = {}
    for note in named:
        by_author.setdefault(normalize_for_match(note.first_author), []).append(note)

    seen = set()
    pairs: list[list[BookNote]] = []
    for group in by_author.values():
        for a in group:
            for b in group:
                if a.path == b.path:
                    continue
                title_a = normalize_for_match(a.title)
                title_b = normalize_for_match(b.title)
                if title_a == title_b or not _is_subtitle_variant(title_a, title_b):
                    continue
                key = frozenset((str(a.path), str(b.path)))
                if key in seen or key in settled:
                    continue
                seen.add(key)
                pairs.append([a, b])

    pairs.sort(
        key=lambda pair: ((pair[0].first_author or "").lower(), pair[0].title or "")
    )
    return pairs


def ensure_frontmatter_fields(
    file_path: Path, dry_run: bool = False
) -> tuple[bool, Optional[Dict[str, Any]]]:
    """Ensures that all current fields exist in the note's frontmatter.

    Args:
        file_path: The Book Note to repair.
        dry_run: Report what would change without writing it. This pass is what
            migrates `format` across the Shelf (ADR 0017), so it can be
            previewed before it rewrites anything.

    Returns:
        A tuple of (updated, frontmatter_dict). The dict is the cleaned
        frontmatter data, whether or not it was written back, or None if the
        frontmatter could not be parsed.

    Raises:
        FileNotFoundError: If the note is gone when read, or removed before the
            repair is written. It is never recreated (#128).
        NoteChanged: If the note was edited between being read here and being
            written back. Nothing is written (#132).
    """
    content, fingerprint = read_note_with_fingerprint(file_path)

    # Split by line rather than by regex. The regex this replaced ended
    # `---\s*\n?(.*)`, and `\s*` is greedy over all whitespace: on a body opening
    # with an indented code block it ate the blank line and the next line's
    # indentation together, so a repair pass turned the first line of the block
    # into a paragraph (#99).
    split = split_frontmatter(content)
    if split is None:
        return False, None

    frontmatter_yaml, rest_of_content = split

    try:
        data = parse_frontmatter_yaml(frontmatter_yaml)
        if not isinstance(data, dict):
            return False, None
    except Exception:
        return False, None

    updated = False

    # Migrate legacy field names to canonical ones.
    for old_name, new_name in FIELD_MIGRATIONS.items():
        if old_name in data:
            if data.get(new_name) is None:
                data[new_name] = data[old_name]
            del data[old_name]
            updated = True

    for field, default in DEFAULT_FRONTMATTER.items():
        if field not in data:
            data[field] = default
            updated = True

    # A note that reached us without an identity gets one here, so a book typed
    # straight into Obsidian is not left unaddressable (ADR 0001, ADR 0011).
    if not data.get("libris_id"):
        data["libris_id"] = mint_libris_id(data.get("date_added"))
        updated = True

    # If date_finished is set, status should be "Read"
    if data.get("date_finished") is not None and data.get("status") != "Read":
        data["status"] = "Read"
        updated = True

    # Ensure authors is always a list
    if isinstance(data.get("authors"), str):
        data["authors"] = [data["authors"]]
        updated = True

    # Repair format's shape and case, the same way authors is repaired above.
    # Obsidian writes this field too and Libris cannot guard it there, so the
    # rule is applied on every pass rather than once in a migration (ADR 0017).
    if "format" in data:
        formats = read_formats(data["format"]) or None
        if formats != data["format"]:
            data["format"] = formats
            updated = True

    # Standardize title casing and strip annotations
    title_val = data.get("title")
    if title_val and isinstance(title_val, str):
        standardized = standardize_title(title_val)
        if standardized != title_val:
            data["title"] = standardized
            updated = True

    title = data.get("title")
    stripped_content = rest_of_content.lstrip()
    if (
        isinstance(title, str)
        and title
        and stripped_content.startswith("## Notes")
        and not re.search(r"(?m)^#\s+", stripped_content)
    ):
        # Deliberately composing new content here, so the leading blank lines go
        # rather than surviving between the heading and the body it introduces.
        rest_of_content = f"\n# {title}\n\n{rest_of_content.lstrip()}"
        updated = True

    if updated and not dry_run:
        new_frontmatter = yaml.dump(data, sort_keys=False, allow_unicode=True).strip()
        # The body goes back exactly as it was read - it carries its own leading
        # newlines, and stripping them was what cost an indented block its indent.
        new_content = f"---\n{new_frontmatter}\n---\n{rest_of_content}"
        # Rewritten, never created: a note removed since it was read above - moved
        # in Obsidian while `cleanup` works through the Shelf - came back under
        # its old name through `write_note` (#128). Carrying the fingerprint of
        # what was read refuses a note edited in that same gap, rather than
        # putting the pre-edit text back over it (#132).
        rewrite_note(file_path, new_content, fingerprint)

    return updated, data


# Maps BookCandidate fields to frontmatter field names.
_BOOK_TO_FRONTMATTER = {
    "title": "title",
    "authors": "authors",
    "isbn": "isbn",
    "page_count": "page_count",
    "published_date": "date_published",
    "google_books_id": "google_books_id",
    "thumbnail": "cover_thumbnail",
    "genres": "genres",
    "description": None,  # handled separately (body, not frontmatter)
}

EXCLUDED_GOOGLE_BOOKS_IDS = {
    "_not_found_in_google_books_api",
    "_not_a_book",
}


def find_duplicates(
    vault_path: Path, notes: list[BookNote] | None = None
) -> list[list[Path]]:
    """Find groups of duplicate book notes by title, ISBN, or Google Books ID.

    Returns a list of groups where each group contains two or more paths
    that share at least one matching identifier.

    Args:
        vault_path: The Shelf to search.
        notes: The Shelf already parsed, when the caller has it. Parsing is
            what this costs, so a caller holding the notes already should not
            pay for them twice (#107).
    """
    if notes is None:
        notes = read_shelf_notes(vault_path)

    def _author_key(note: BookNote) -> tuple[str, ...]:
        return tuple(sorted(name.lower() for name in note.authors))

    # Build groups keyed by each identifier type.
    # key -> set of indices into file_data
    groups_by_key: Dict[str, set[int]] = {}
    # Title groups need pairwise author comparison (missing author = wildcard)
    title_groups: Dict[str, list[int]] = {}

    for idx, note in enumerate(notes):
        if note.title is not None:
            # Normalized rather than lowercased: punctuation is not meaning
            # here, and "Crucial Conversations- Tools" and "Crucial
            # Conversations: Tools" are one Book (#72).
            title_groups.setdefault(normalize_for_match(note.title), []).append(idx)

        isbn = note.isbn
        if isbn:
            key = f"isbn:{isbn}"
            groups_by_key.setdefault(key, set()).add(idx)

        gid = note.frontmatter.get("google_books_id")
        if gid and gid not in EXCLUDED_GOOGLE_BOOKS_IDS:
            key = f"gid:{str(gid).strip()}"
            groups_by_key.setdefault(key, set()).add(idx)

    # Union-find to merge overlapping groups
    parent = list(range(len(notes)))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    # Title duplicates: same title + (same author OR either has no author)
    for members in title_groups.values():
        if len(members) < 2:
            continue

        author_buckets: Dict[str, list[int]] = {}
        has_missing_author = False

        for idx in members:
            author_key = _author_key(notes[idx])
            if author_key:
                author_buckets.setdefault(author_key, []).append(idx)
            else:
                has_missing_author = True

        if has_missing_author:
            first = members[0]
            for other in members[1:]:
                union(first, other)
        else:
            for bucket_members in author_buckets.values():
                if len(bucket_members) < 2:
                    continue
                first = bucket_members[0]
                for other in bucket_members[1:]:
                    union(first, other)

    for members in groups_by_key.values():
        if len(members) < 2:
            continue
        it = iter(members)
        first = next(it)
        for other in it:
            union(first, other)

    # Collect final groups with 2+ members
    clusters: Dict[int, list[Path]] = {}
    for idx, note in enumerate(notes):
        root = find(idx)
        clusters.setdefault(root, []).append(note.path)

    return [sorted(group) for group in clusters.values() if len(group) >= 2]


def read_frontmatter(file_path: Path) -> Optional[Dict[str, Any]]:
    """Read and return the frontmatter dict from a markdown file, or None."""
    try:
        content = file_path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        # Not UTF-8, so not a note this can parse - the same answer as broken
        # YAML. Raised, it escaped every Shelf query: one such file on the Shelf
        # made `search_library` fail for every book (#127 review).
        return None
    split = split_frontmatter(content)
    if split is None:
        return None
    try:
        data = parse_frontmatter_yaml(split[0])
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def update_frontmatter_from_book(file_path: Path, book: BookCandidate) -> bool:
    """Fill null frontmatter fields from a candidate.

    Args:
        file_path: The Book Note to enrich.
        book: The candidate whose values fill the note's empty fields.

    Returns:
        True when the note was changed.

    Raises:
        FileNotFoundError: If the note is gone when read, or removed before the
            enrichment is written. It is never recreated (#128).
        NoteChanged: If the note was edited between being read here and being
            written back. Nothing is written (#132).
    """
    content, fingerprint = read_note_with_fingerprint(file_path)
    split = split_frontmatter(content)
    if split is None:
        return False

    frontmatter_yaml, rest_of_content = split

    try:
        data = parse_frontmatter_yaml(frontmatter_yaml)
        if not isinstance(data, dict):
            return False
    except Exception:
        return False

    updated = False
    for book_field, fm_field in _BOOK_TO_FRONTMATTER.items():
        if fm_field is None:
            continue
        value = getattr(book, book_field, None)
        if value is not None and data.get(fm_field) is None:
            data[fm_field] = value
            updated = True

    title = data.get("title")
    stripped_content = rest_of_content.lstrip()
    if (
        isinstance(title, str)
        and title
        and stripped_content.startswith("## Notes")
        and not re.search(r"(?m)^#\s+", stripped_content)
    ):
        # Deliberately composing new content here, so the leading blank lines go
        # rather than surviving between the heading and the body it introduces.
        rest_of_content = f"\n# {title}\n\n{rest_of_content.lstrip()}"
        updated = True

    # Add description to body if missing
    if book.description and not has_description_callout(rest_of_content):
        rest_of_content = (
            rest_of_content.rstrip()
            + "\n\n"
            + render_description_callout(book.description)
            + "\n"
        )
        updated = True

    if updated:
        new_frontmatter = yaml.dump(data, sort_keys=False, allow_unicode=True).strip()
        # As in ensure_frontmatter_fields: the body carries its own leading
        # newlines, and stripping them cost an indented block its indent (#99).
        new_content = f"---\n{new_frontmatter}\n---\n{rest_of_content}"
        # Rewritten, never created, and refused if the note was edited since it
        # was read, as in ensure_frontmatter_fields (#128, #132).
        rewrite_note(file_path, new_content, fingerprint)
        return True

    return False


def compute_canonical_filename(file_path: Path) -> Optional[str]:
    """Compute the canonical 'Title - Author.md' filename from frontmatter."""
    note = BookNote.read(file_path)
    return note.canonical_filename if note is not None else None


def update_wikilinks_in_vault(
    vault_root: Path, old_stem: str, new_stem: str, exclude: Optional[Path] = None
) -> int:
    """Update all wikilinks from old_stem to new_stem across the vault.

    A linking note removed while the sweep runs is skipped. It holds no link
    left to fix, and one note moving must not stop the rest of the vault being
    updated - nor be written back into existence (#128).

    Args:
        vault_root: The vault to sweep.
        old_stem: The renamed note's old filename, without its extension.
        new_stem: Its new filename, without its extension.
        exclude: A note to leave unswept, usually the renamed note itself.

    Returns:
        How many notes had a link updated.
    """
    updated_count = 0
    exclude_resolved = exclude.resolve() if exclude else None
    for root, dirnames, filenames in os.walk(vault_root):
        # Skip hidden directories (.obsidian, .git, etc.) before descending into them.
        dirnames[:] = [dirname for dirname in dirnames if not dirname.startswith(".")]
        root_path = Path(root)
        for filename in filenames:
            if not filename.endswith(".md") or filename.startswith("."):
                continue
            md_file = root_path / filename
            if exclude_resolved and md_file.resolve() == exclude_resolved:
                continue
            try:
                if _sweep_one_note(md_file, old_stem, new_stem):
                    updated_count += 1
            except FileNotFoundError:
                continue
    return updated_count


def _sweep_one_note(md_file: Path, old_stem: str, new_stem: str) -> bool:
    """Point one note's links at a renamed note, redoing it if the note changes.

    A note edited between being read and being written is read again and the
    replacement redone, rather than the older text being written back over the
    edit (#132). That is sound here and nowhere else in the sweep's company: the
    new text is not a decision made earlier, it is whatever the note says with
    one link spelled differently, so it is the same answer against the newer
    note. A note being written continuously gives up after a few passes and is
    left alone, with its links unchanged rather than its text lost.

    Args:
        md_file: The note to sweep.
        old_stem: The renamed note's old filename, without its extension.
        new_stem: Its new filename, without its extension.

    Returns:
        True when a link was updated.

    Raises:
        FileNotFoundError: If the note is gone when read, or removed before the
            sweep writes it. It is never recreated (#128).
    """
    for _ in range(3):
        content, fingerprint = read_note_with_fingerprint(md_file)
        new_content = content.replace(f"[[{old_stem}]]", f"[[{new_stem}]]")
        new_content = new_content.replace(f"[[{old_stem}|", f"[[{new_stem}|")
        new_content = new_content.replace(f"[[{old_stem}#", f"[[{new_stem}#")
        new_content = new_content.replace(f"[[{old_stem}^", f"[[{new_stem}^")
        if new_content == content:
            return False
        try:
            rewrite_note(md_file, new_content, fingerprint)
        except NoteChanged:
            continue
        return True
    return False


RenameStatus = Literal[
    "renamed",
    "already_canonical",
    "missing_title",
    "missing_author",
    "invalid_frontmatter",
    "collision",
]


@dataclass(frozen=True)
class RenameResult:
    """Result of a rename attempt with diagnostic information."""

    status: RenameStatus
    new_path: Optional[Path] = None
    detail: Optional[str] = None


def rename_book_file(
    file_path: Path,
    vault_root: Optional[Path] = None,
    frontmatter: Optional[Dict[str, Any]] = None,
) -> RenameResult:
    """Rename a book file to canonical format and update wikilinks.

    If frontmatter is provided, it is used directly instead of re-reading
    the file. This avoids redundant I/O when called after ensure_frontmatter_fields.
    """
    note = (
        BookNote(path=file_path, frontmatter=frontmatter)
        if frontmatter is not None
        else BookNote.read(file_path)
    )
    if note is None or not note.frontmatter:
        return RenameResult(status="invalid_frontmatter")

    if note.title is None:
        return RenameResult(status="missing_title")

    if note.first_author is None:
        return RenameResult(status="missing_author")

    canonical_name = note.canonical_filename
    if canonical_name == file_path.name:
        return RenameResult(status="already_canonical")

    new_path = file_path.parent / canonical_name
    if new_path.exists():
        return RenameResult(status="collision", detail=canonical_name)

    search_root = vault_root or file_path.parent
    old_stem = file_path.stem
    new_stem = new_path.stem

    # Perform the rename first so wikilinks are only updated if it succeeds.
    file_path.rename(new_path)

    # Update wikilinks across the vault
    update_wikilinks_in_vault(search_root, old_stem, new_stem, exclude=new_path)
    return RenameResult(status="renamed", new_path=new_path)
