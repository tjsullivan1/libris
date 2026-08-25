"""What the adapters do, with no adapter in it.

ADR 0008 puts resolution, creation and querying here so the REST surface and
the MCP tools cannot drift apart by reimplementing matching. Nothing in this
module knows about HTTP, and it raises rather than returning status codes: the
adapter decides what a failure looks like on the wire.
"""

from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from .api import BookCandidate, GoogleBooksClient
from .markdown import BookNote, create_book_note, list_books
from .matching import best_match, normalize_for_match, titles_match


class Outcome(Enum):
    """What a write did to the Library.

    Deliberately not ADR 0013's Intent vocabulary: an Intent is applied to the
    Shelf later by the CLI, whereas this writes now, so nothing is absorbed.
    """

    CREATED = "created"
    ALREADY_PRESENT = "already_present"


@dataclass
class AddResult:
    """The answer to a write: identity first, path only for display (ADR 0016)."""

    libris_id: str | None
    path: Path
    outcome: Outcome


def is_isbn10(value: str) -> bool:
    """Check whether a ten-character identifier is a valid ISBN-10.

    Amazon reuses the ISBN-10 as the ASIN for print books but mints its own for
    Kindle editions, and the two are indistinguishable by shape. The checksum
    is what separates them, so a Kindle ASIN is never sent as `isbn:`.

    Args:
        value: The identifier to check.

    Returns:
        True if the value passes the ISBN-10 check digit.
    """
    digits = value.replace("-", "").replace(" ", "").upper()
    if len(digits) != 10:
        return False

    total = 0
    for position, char in enumerate(digits):
        if char.isdigit():
            digit = int(char)
        elif char == "X" and position == 9:
            digit = 10
        else:
            return False
        total += digit * (10 - position)
    return total % 11 == 0


def build_lookup_query(
    isbn: str | None = None,
    asin: str | None = None,
    title: str | None = None,
    authors: list[str] | None = None,
) -> str | None:
    """Build a Google Books query from whatever a page yielded.

    Identifiers win over names: an ISBN names one edition, while a title and
    author name a book that may have twenty.

    Args:
        isbn: An ISBN scraped from the page.
        asin: An Amazon identifier, which is an ISBN-10 only for print editions.
        title: The title as scraped.
        authors: The authors as scraped.

    Returns:
        A query string, or None when nothing identifying was found - the caller
        is told rather than handed a search for everything.
    """
    if isbn:
        return f"isbn:{isbn}"
    if asin and is_isbn10(asin):
        return f"isbn:{asin}"

    parts = []
    if title:
        parts.append(f"intitle:{title}")
    if authors:
        parts.append(f"inauthor:{authors[0]}")
    return " ".join(parts) or None


def lookup_candidates(
    isbn: str | None = None,
    asin: str | None = None,
    title: str | None = None,
    authors: list[str] | None = None,
    client: GoogleBooksClient | None = None,
) -> list[BookCandidate]:
    """Find Book Candidates for what a page yielded, best described first.

    Args:
        isbn: An ISBN scraped from the page.
        asin: An Amazon identifier.
        title: The title as scraped.
        authors: The authors as scraped.
        client: The Google Books client to search with.

    Returns:
        Candidates ordered so the best described comes first, or an empty list.
        A picker needs an order; it does not need a decision made for it, which
        is why this ranks rather than choosing (ADR 0003).
    """
    query = build_lookup_query(isbn=isbn, asin=asin, title=title, authors=authors)
    if query is None:
        return []

    results = (client or GoogleBooksClient()).search(query)
    if not results:
        return []

    ranked = []
    remaining = list(results)
    while remaining:
        pick = best_match(remaining)
        ranked.append(pick)
        remaining.remove(pick)
    return ranked


def find_existing(
    vault_path: Path,
    isbn: str | None = None,
    google_books_id: str | None = None,
    title: str | None = None,
    authors: list[str] | None = None,
) -> BookNote | None:
    """Find a Book Note already on the Shelf describing this book.

    Checked against the live Shelf rather than an index, so the answer is true
    at the moment it is given - the stronger of the two duplicate guarantees
    (ADR 0010).

    Args:
        vault_path: The Shelf to search.
        isbn: An ISBN to match exactly.
        google_books_id: A Google Books volume id to match exactly.
        title: A title to match after normalization.
        authors: Authors whose first entry is matched after normalization.

    Returns:
        The Book Note, or None. A miss is a miss (ADR 0003).
    """
    wanted_title = normalize_for_match(title) if title else None
    wanted_author = normalize_for_match(authors[0]) if authors else None

    for book_path in list_books(vault_path):
        note = BookNote.read(book_path)
        if note is None:
            continue

        if isbn and note.frontmatter.get("isbn") == isbn:
            return note
        if (
            google_books_id
            and note.frontmatter.get("google_books_id") == google_books_id
        ):
            return note
        if (
            wanted_title
            and note.title
            and normalize_for_match(note.title) == wanted_title
        ):
            if wanted_author is None:
                return note
            if (
                note.first_author
                and normalize_for_match(note.first_author) == wanted_author
            ):
                return note
    return None


def find_similar(
    vault_path: Path,
    title: str | None = None,
    authors: list[str] | None = None,
    limit: int = 5,
) -> list[BookNote]:
    """Find Book Notes that might be the same Book, without deciding that they are.

    Title matching is fuzzy on purpose and cannot be trusted to decide. Measured
    against the real Shelf, containment matching conflates 83 pairs: some are
    genuine variants ("The Brass Verdict" and "The Brass Verdict: A Novel"), and
    some are different books entirely ("Mercy" and "Long Road to Mercy"). Telling
    a person a Book is already held when it is not means it never gets added and
    nothing surfaces the error, which ADR 0003 refuses.

    So this decides nothing. It hands candidates back to the Surface, where a
    person is still present to say which one it is.

    Args:
        vault_path: The Shelf to search.
        title: The title as scraped.
        authors: The authors as scraped. When given, only notes by the same
            first author are considered.
        limit: The most notes to return.

    Returns:
        Book Notes whose title plausibly describes the same Book, nearest first
        by title length, or an empty list.
    """
    if not title:
        return []

    wanted_author = normalize_for_match(authors[0]) if authors else None

    found: list[BookNote] = []
    for book_path in list_books(vault_path):
        note = BookNote.read(book_path)
        if note is None or not note.title:
            continue
        if wanted_author is not None:
            if not note.first_author:
                continue
            if normalize_for_match(note.first_author) != wanted_author:
                continue
        if titles_match(title, note.title):
            found.append(note)

    found.sort(key=lambda n: len(n.title or ""))
    return found[:limit]


def add_book(
    vault_path: Path,
    candidate: BookCandidate,
    overrides: dict | None = None,
) -> AddResult:
    """Add a Book to the Library, unless it is already held.

    The duplicate check runs before the write and never overwrites: a Book Note
    holds a reader's own writing, and a second capture of the same book must not
    cost them that.

    Args:
        vault_path: The Shelf to write into.
        candidate: The Book Candidate accepted by whoever chose it.
        overrides: Frontmatter fields to set, validated by create_book_note.

    Returns:
        The identity of the Book Note and what happened to it.

    Raises:
        InvalidFieldValue: If an override carries a value the Library does not
            define.
        ValueError: If an override names a field the canonical schema has no
            place for.
    """
    existing = find_existing(
        vault_path,
        isbn=candidate.isbn,
        google_books_id=candidate.google_books_id or None,
        title=candidate.title,
        authors=candidate.authors,
    )
    if existing is not None:
        return AddResult(
            libris_id=existing.libris_id,
            path=existing.path,
            outcome=Outcome.ALREADY_PRESENT,
        )

    path = create_book_note(candidate, vault_path, overrides=overrides or None)
    note = BookNote.read(path)
    return AddResult(
        libris_id=note.libris_id if note else None,
        path=path,
        outcome=Outcome.CREATED,
    )
