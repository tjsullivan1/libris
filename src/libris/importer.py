"""Import books from external sources (currently Audible JSON) into the vault."""

import json
import re
import yaml
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .api import Book
from .markdown import (
    create_book_note,
    list_books,
    read_frontmatter,
    update_book_status,
)


def normalize_for_match(text: str) -> str:
    """Normalize text for fuzzy comparison: lowercase, strip punctuation/extra whitespace."""
    text = re.sub(r"[^\w\s]", " ", text.lower())
    return re.sub(r"\s+", " ", text).strip()


@dataclass
class ImportBook:
    """Common representation for a book from any import source."""

    title: str
    authors: List[str]
    status: str  # "Read", "To Read", "Reading"
    format: Optional[str] = None  # "Audiobook", "Hardcover", "Paperback", "eBook"
    isbn: Optional[str] = None
    page_count: Optional[int] = None
    published_date: Optional[str] = None
    rating: Optional[float] = None
    date_added: Optional[str] = None
    date_finished: Optional[str] = None
    genres: List[str] = field(default_factory=list)
    description: Optional[str] = None
    source_id: Optional[str] = None
    source_format: str = ""


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
                title=title,
                authors=authors,
                status=status,
                format="Audiobook",
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


def _build_vault_index(vault_path: Path) -> Dict[Tuple[str, str], Tuple[Path, Dict]]:
    """Build an index of existing vault books keyed by normalized (title, first_author)."""
    index: Dict[Tuple[str, str], Tuple[Path, Dict]] = {}
    for book_path in list_books(vault_path):
        fm = read_frontmatter(book_path)
        if fm is None:
            continue

        title = fm.get("title")
        if not isinstance(title, str) or not title.strip():
            continue

        author = fm.get("author")
        if isinstance(author, list):
            first_author = next(
                (a.strip() for a in author if isinstance(a, str) and a.strip()),
                None,
            )
        elif isinstance(author, str):
            first_author = author.strip() or None
        else:
            first_author = None

        if first_author is None:
            continue

        key = (normalize_for_match(title), normalize_for_match(first_author))
        index[key] = (book_path, fm)

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
    vault_index: Dict[Tuple[str, str], Tuple[Path, Dict]],
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
    match = vault_index.get(key)
    if match is None:
        return None

    path, fm = match
    updates: List[str] = []

    existing_status = fm.get("status", "")
    if existing_status == "To Read" and book.status == "Read":
        updates.append("status")

    existing_format = fm.get("format")
    if not existing_format and book.format:
        updates.append("format")

    return (path, fm, updates)


def _apply_updates(path: Path, book: ImportBook, updates: List[str]):
    """Apply field updates to an existing vault note."""
    if "status" in updates:
        update_book_status(path, book.status)

    if "format" in updates:
        content = path.read_text(encoding="utf-8")
        # Parse frontmatter using YAML to properly handle missing fields
        match = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", content, re.DOTALL)

        if match:
            frontmatter_yaml = match.group(1)
            rest_of_content = match.group(2)

            try:
                data = yaml.safe_load(frontmatter_yaml)
                if isinstance(data, dict):
                    # Update the format field
                    data["format"] = book.format
                    # Write back the updated frontmatter
                    new_frontmatter = yaml.dump(
                        data, sort_keys=False, allow_unicode=True
                    ).strip()
                    # Preserve content structure: remove only leading newlines
                    content_part = rest_of_content.lstrip("\n")
                    new_content = f"---\n{new_frontmatter}\n---\n{content_part}"
                    path.write_text(new_content, encoding="utf-8")
            except yaml.YAMLError:
                # Fallback to regex replacement if YAML parsing fails
                pattern = r"(format:\s*)(.*)"
                new_content = re.sub(pattern, f"\\1{book.format}", content)
                if new_content == content:
                    # format field might be null — replace the null value
                    new_content = content.replace(
                        "format: null", f"format: {book.format}"
                    )
                path.write_text(new_content, encoding="utf-8")


def _to_api_book(import_book: ImportBook) -> Book:
    """Convert an ImportBook to an api.Book for note creation."""
    return Book(
        title=import_book.title,
        authors=import_book.authors,
        isbn=import_book.isbn,
        page_count=import_book.page_count,
        published_date=import_book.published_date,
        google_books_id="",
        thumbnail=None,
        genres=import_book.genres,
        description=import_book.description,
    )


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
                api_book = _to_api_book(book)
                created_path = create_book_note(
                    api_book, vault_path, status=book.status
                )
                # Update format in the newly created note if specified
                if book.format:
                    content = created_path.read_text(encoding="utf-8")
                    new_content = content.replace(
                        "format: null", f"format: {book.format}"
                    )
                    created_path.write_text(new_content, encoding="utf-8")
        else:
            dup_path, _, updates = dup
            if updates:
                result.updated_books.append((book, dup_path, updates))
                if apply:
                    _apply_updates(dup_path, book, updates)
            else:
                result.skipped_books.append(book)

    return result
