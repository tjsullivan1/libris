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
from .merge import (
    delete_secondary_file,
    get_primary_book,
    merge_two_books,
    write_merged_book,
)


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


def find_by_libris_id(vault_path: Path, libris_id: str) -> BookNote | None:
    """Resolve a Libris ID to the Book Note that answers for it.

    A note merged away leaves its identity on the survivor (ADR 0014), so an
    Intent naming an ID that no longer lives anywhere still applies rather than
    being rejected for a note Libris itself destroyed.

    Args:
        vault_path: The Shelf to search.
        libris_id: The identity to resolve.

    Returns:
        The Book Note holding that identity, the note that superseded it, or
        None. A live identity always wins over a superseded one.

    Note:
        Guaranteeing that a live identity wins means the scan cannot stop at the
        first superseded match, so resolving an identity that is superseded or
        unknown reads every Book Note: about 14 seconds against the 3,136-note
        Shelf. Resolving one Intent that way is fine; resolving a queue of them
        in a loop is not. A caller with many to resolve should build an index in
        one pass instead.
    """
    wanted = libris_id.strip() if libris_id else ""
    if not wanted:
        # Checked after stripping: a whitespace-only id would otherwise read
        # every note on the Shelf to find nothing.
        return None

    superseding: BookNote | None = None

    for book_path in list_books(vault_path):
        note = BookNote.read(book_path)
        if note is None:
            continue
        if note.libris_id == wanted:
            return note
        if superseding is None and wanted in note.superseded_ids:
            superseding = note

    return superseding


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
            and wanted_author
            and note.title
            and normalize_for_match(note.title) == wanted_title
            and note.first_author
            and normalize_for_match(note.first_author) == wanted_author
        ):
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


@dataclass
class DecisionOutcome:
    """What became of one decision from an exported review."""

    status: str  # merged, skipped, conflicted, drifted, undecided
    detail: str


def _resolve_pair(vault_path: Path, first_id: str, second_id: str):
    """Find the two Book Notes a decision names, following superseded IDs."""
    first = find_by_libris_id(vault_path, first_id)
    second = find_by_libris_id(vault_path, second_id)
    return first, second


def apply_decisions(
    vault_path: Path,
    decisions: list[dict],
    allow_conflicts: bool = False,
    dry_run: bool = False,
) -> list[DecisionOutcome]:
    """Merge the pairs an exported review marked as one Book.

    The file records a judgement made against the Shelf as it was. Rather than
    trusting it, every pair is resolved against the Shelf as it is now, by
    Libris ID - which survives a rename and, since ADR 0014, a merge. A pair
    that no longer resolves has drifted and is reported rather than acted on.

    Args:
        vault_path: The Shelf to act on.
        decisions: Decision records from the review export.
        allow_conflicts: Merge even when one of the reader's own values
            disagrees. Off by default: the file answered "is this one Book",
            not "which rating is yours".
        dry_run: Report what would happen without writing or deleting anything.
            This deletes Book Notes, so it can be previewed first.

    Returns:
        One outcome per decision, in the order given.
    """
    outcomes: list[DecisionOutcome] = []

    for decision in decisions:
        verdict = decision.get("decision")
        shorter = (decision.get("shorter") or {}).get("libris_id")
        longer = (decision.get("longer") or {}).get("libris_id")
        label = (decision.get("shorter") or {}).get("title") or "unknown"

        if verdict != "same":
            outcomes.append(
                DecisionOutcome("skipped", f"{label}: recorded as two books")
            )
            continue

        if not shorter or not longer:
            outcomes.append(
                DecisionOutcome("drifted", f"{label}: decision names no Libris ID")
            )
            continue

        first, second = _resolve_pair(vault_path, shorter, longer)
        if first is None or second is None or first.path == second.path:
            outcomes.append(
                DecisionOutcome("drifted", f"{label}: no longer two notes on the Shelf")
            )
            continue

        primary = get_primary_book(first.path, second.path)
        secondary = second.path if primary == first.path else first.path

        merged_fm, merged_body, conflicts = merge_two_books(
            primary, secondary, allow_conflicts=allow_conflicts
        )
        if conflicts and not allow_conflicts:
            fields = ", ".join(sorted({c.field for c in conflicts}))
            outcomes.append(
                DecisionOutcome("conflicted", f"{label}: {fields} disagree")
            )
            continue

        if dry_run:
            outcomes.append(
                DecisionOutcome("would_merge", f"{label} -> {primary.name}")
            )
            continue

        write_merged_book(primary, merged_fm, merged_body)
        delete_secondary_file(secondary)
        outcomes.append(DecisionOutcome("merged", f"{label} -> {primary.name}"))

    return outcomes
