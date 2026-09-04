"""Import books from external sources (currently Audible JSON) into the vault."""

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import yaml

from .api import BookCandidate
from .markdown import (
    BookNote,
    FrontmatterUnreadable,
    create_book_note,
    list_books,
    split_frontmatter,
    update_book_status,
    write_note,
)
from .matching import normalize_for_match


@dataclass
class ImportBook:
    """A book from an import source, paired with how that source says it was read.

    The candidate carries what the source knows about the book; status and format
    are reading state and travel separately, because they are not metadata about
    the book itself.
    """

    candidate: BookCandidate
    status: str  # "Read", "To Read", "Reading"
    format: list[str] = field(default_factory=list)  # see note_format.FORMAT_VALUES
    source_format: str = ""

    @property
    def title(self) -> str:
        """The candidate's title."""
        return self.candidate.title

    @property
    def authors(self) -> list[str]:
        """The candidate's authors."""
        return self.candidate.authors


def parse_audible_json(path: Path) -> List[ImportBook]:
    """Parse an Audible library JSON export into ImportBook instances."""
    with open(path, encoding="utf-8-sig") as f:
        data = json.load(f)

    if not isinstance(data, list):
        raise ValueError("Expected a JSON array of book objects")

    books: List[ImportBook] = []
    for entry in data:
        title = entry.get("title", "").strip()
        if not title:
            continue

        author_str = entry.get("author", "").strip()
        authors = (
            [a.strip() for a in author_str.split(",") if a.strip()]
            if author_str
            else ["Unknown Author"]
        )

        finished = entry.get("finished", "").strip().lower() == "yes"
        status = "Read" if finished else "To Read"

        books.append(
            ImportBook(
                candidate=BookCandidate(title=title, authors=authors, source="audible"),
                status=status,
                format=["Audiobook"],
                source_format="audible-json",
            )
        )

    return books


SUPPORTED_FORMATS = {
    "audible-json": parse_audible_json,
}


def detect_format(path: Path) -> Optional[str]:
    """Auto-detect the import file format based on extension and content."""
    suffix = path.suffix.lower()
    if suffix == ".json":
        return "audible-json"
    # Future: inspect CSV header for Goodreads vs StoryGraph
    return None


def parse_import_file(
    path: Path, format_name: Optional[str] = None
) -> List[ImportBook]:
    """Parse an import file, auto-detecting format if not specified."""
    if not path.exists():
        raise FileNotFoundError(f"Import file not found: {path}")

    if format_name is None:
        format_name = detect_format(path)

    if format_name is None:
        raise ValueError(
            f"Cannot detect format for {path.name}. "
            f"Use --format with one of: {', '.join(SUPPORTED_FORMATS)}"
        )

    parser = SUPPORTED_FORMATS.get(format_name)
    if parser is None:
        raise ValueError(
            f"Unknown format: {format_name}. Supported: {', '.join(SUPPORTED_FORMATS)}"
        )

    return parser(path)


def _build_vault_index(vault_path: Path) -> dict[tuple[str, str], BookNote]:
    """Build an index of Book Notes keyed by normalized (title, first_author).

    Args:
        vault_path: The Shelf to index.

    Returns:
        A mapping from normalized title and first author to the Book Note. Notes
        without a title or an author cannot be matched and are left out.
    """
    index: dict[tuple[str, str], BookNote] = {}
    for book_path in list_books(vault_path):
        note = BookNote.read(book_path)
        if note is None or note.title is None or note.first_author is None:
            continue

        key = (normalize_for_match(note.title), normalize_for_match(note.first_author))
        index[key] = note

    return index


@dataclass
class ImportResult:
    """Tracks the outcome of an import operation."""

    new_books: List[ImportBook] = field(default_factory=list)
    updated_books: List[Tuple[ImportBook, Path, List[str]]] = field(
        default_factory=list
    )
    skipped_books: List[ImportBook] = field(default_factory=list)


def _check_duplicate(
    book: ImportBook,
    vault_index: dict[tuple[str, str], BookNote],
) -> Optional[Tuple[Path, Dict, List[str]]]:
    """Check if an import book matches an existing vault entry.

    Returns (path, frontmatter, list_of_updates_needed) if a duplicate is
    found that should be updated, or (path, frontmatter, []) if the
    duplicate is already up-to-date. Returns None if no duplicate.
    """
    first_author = book.authors[0] if book.authors else None
    if first_author is None:
        return None

    key = (normalize_for_match(book.title), normalize_for_match(first_author))
    note = vault_index.get(key)
    if note is None:
        return None

    fm = note.frontmatter
    updates: List[str] = []

    existing_status = fm.get("status", "")
    if existing_status == "To Read" and book.status == "Read":
        updates.append("status")

    existing_format = fm.get("format")
    if not existing_format and book.format:
        updates.append("format")

    return (note.path, fm, updates)


def _apply_updates(path: Path, book: ImportBook, updates: List[str]) -> bool:
    """Apply field updates to an existing vault note.

    Args:
        path: The Book Note to update.
        book: The imported book carrying the new values.
        updates: Which fields to apply.

    Returns:
        True when the note was updated, False when its frontmatter could not be
        read - in which case nothing was written to it.
    """
    if "status" in updates:
        try:
            update_book_status(path, book.status)
        except FrontmatterUnreadable:
            # Same answer this function already gives for a format update it
            # cannot parse: report the note as untouched rather than guessing
            # at its shape. An import run writes many notes and must not stop
            # on one it cannot read.
            return False

    if "format" not in updates:
        return True

    content = path.read_text(encoding="utf-8")
    split = split_frontmatter(content)
    if split is None:
        return False

    frontmatter_yaml, rest_of_content = split
    try:
        data = yaml.safe_load(frontmatter_yaml)
    except yaml.YAMLError:
        # Deliberately no regex fallback. format is a list (ADR 0017), so a
        # line-level substitution would write a Python repr and strand any
        # existing block items below it, turning a note we could not parse into
        # one nobody can. It is left alone instead.
        return False

    if not isinstance(data, dict):
        return False

    data["format"] = book.format
    new_frontmatter = yaml.dump(data, sort_keys=False, allow_unicode=True).strip()
    # The body goes back as it was read. It carries its own leading newlines now
    # that the split preserves them, so stripping would delete a blank line the
    # reader put there (#99).
    write_note(path, f"---\n{new_frontmatter}\n---\n{rest_of_content}")
    return True


def run_import(
    path: Path,
    vault_path: Path,
    apply: bool = False,
    format_name: Optional[str] = None,
    limit: int = 0,
) -> ImportResult:
    """Run the import process: parse, detect duplicates, optionally write.

    Returns an ImportResult with categorized books regardless of apply mode.
    """
    books = parse_import_file(path, format_name)
    if limit > 0:
        books = books[:limit]

    vault_index = _build_vault_index(vault_path)
    result = ImportResult()

    for book in books:
        dup = _check_duplicate(book, vault_index)

        if dup is None:
            result.new_books.append(book)
            if apply:
                overrides = {"format": book.format} if book.format else None
                create_book_note(
                    book.candidate,
                    vault_path,
                    status=book.status,
                    overrides=overrides,
                )
        else:
            dup_path, _, updates = dup
            if updates:
                result.updated_books.append((book, dup_path, updates))
                if apply:
                    if not _apply_updates(dup_path, book, updates):
                        result.skipped_books.append(book)
            else:
                result.skipped_books.append(book)

    return result
