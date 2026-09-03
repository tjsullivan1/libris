"""What the adapters do, with no adapter in it.

ADR 0008 puts resolution, creation and querying here so the REST surface and
the MCP tools cannot drift apart by reimplementing matching. Nothing in this
module knows about HTTP, and it raises rather than returning status codes: the
adapter decides what a failure looks like on the wire.
"""

import math
import re
from dataclasses import dataclass, field
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
from .note_format import validate_field_value
from .shelf import index_for


class Outcome(Enum):
    """What a write did to the Library.

    Deliberately not ADR 0013's Intent vocabulary: an Intent is applied to the
    Shelf later by the CLI, whereas this writes now, so nothing is absorbed.
    """

    CREATED = "created"
    ALREADY_PRESENT = "already_present"


@dataclass
class AddResult:
    """The answer to a write: identity first, path only for display (ADR 0016).

    Carries the title and authors of the note it points at, because they are
    what a Surface shows a person. A path is not something to show on a device
    that holds no Shelf (ADR 0019), and on an already-held Book the note's title
    can differ from the candidate's - matching on ISBN or Google Books ID says
    nothing about the two titles agreeing.
    """

    libris_id: str | None
    path: Path
    outcome: Outcome
    title: str | None = None
    authors: list[str] = field(default_factory=list)


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


# Amazon states the ASIN in the product URL itself, in one of two shapes. A
# scraper that cannot read the page - or reads a layout it does not know - still
# has this.
_AMAZON_ASIN = re.compile(r"/(?:dp|gp/product)/([A-Z0-9]{10})(?:[/?#]|$)")


def asin_from_source_url(source_url: str | None) -> str | None:
    """Read the ASIN out of an Amazon product URL.

    The source URL is used for this and nothing else: it is never written to a
    Book Note, because no modelled field records where a capture came from and
    adding one would write a null into every note on the Shelf to say nothing.

    Args:
        source_url: The page the scrape came from, if any.

    Returns:
        The ASIN the URL names, or None if it names none.
    """
    if not source_url:
        return None
    found = _AMAZON_ASIN.search(source_url)
    return found.group(1) if found else None


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
    source_url: str | None = None,
    client: GoogleBooksClient | None = None,
) -> list[BookCandidate]:
    """Find Book Candidates for what a page yielded, best described first.

    Args:
        isbn: An ISBN scraped from the page.
        asin: An Amazon identifier.
        title: The title as scraped.
        authors: The authors as scraped.
        source_url: The page the scrape came from. Read for an ASIN when the
            scraper found none, and never persisted.
        client: The Google Books client to search with.

    Returns:
        Candidates ordered so the best described comes first, or an empty list.
        A picker needs an order; it does not need a decision made for it, which
        is why this ranks rather than choosing (ADR 0003).
    """
    query = build_lookup_query(
        isbn=isbn,
        asin=asin or asin_from_source_url(source_url),
        title=title,
        authors=authors,
    )
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

    Checked against the live Shelf, so the answer is true at the moment it is
    given - the stronger of the two duplicate guarantees (ADR 0010). The notes
    come from an index, which is revalidated against the filesystem on every
    call rather than held for a lifetime, so that remains true.

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

    for note in index_for(vault_path).notes():
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
    for note in index_for(vault_path).notes():
        if not note.title:
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


# What a search returns when the caller does not say, and the most it will
# return however loudly they ask. A Surface asking for everything is asking to
# read 1,452 To Read notes into a context window, which is what the ceiling
# exists to refuse; the total travels instead so the answer stays honest.
DEFAULT_SEARCH_LIMIT = 20
MAX_SEARCH_LIMIT = 50

# Words that say how a person is talking rather than which book they mean. A
# query still scores them - "The Way of Kings" should outrank "Kings of the
# Wyld" partly on "of" - but a note matching nothing else is not a match, or
# searching for a title with "the" in it would report most of the Shelf.
#
# Curated rather than derived, because frequency cannot do this job: measured on
# the real Shelf, "poems" appears in 51 notes and "one" in 50. They are
# statistically identical and only one of them names a book. The list covers
# pronouns, determiners, common prepositions and the filler of a spoken request
# ("that mistborn one I just finished"), and holds no word that could title a
# book on its own.
_STOP_WORDS = frozenset(
    {
        "a",
        "about",
        "am",
        "an",
        "and",
        "any",
        "are",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "i",
        "in",
        "is",
        "it",
        "its",
        "just",
        "me",
        "mine",
        "my",
        "of",
        "on",
        "one",
        "ones",
        "or",
        "some",
        "that",
        "the",
        "these",
        "this",
        "those",
        "to",
        "was",
        "were",
        "with",
    }
)


@dataclass
class SearchResult:
    """What the Library holds for a query, and how much of it was returned.

    `total` counts every match, not the returned slice. A Surface that shows
    five of 1,452 can then say so, rather than implying it saw them all.
    """

    total: int
    limit: int
    books: list[BookNote] = field(default_factory=list)


def _search_tokens(text: str) -> set[str]:
    """Split text into the normalized words a search compares."""
    normalized = normalize_for_match(text)
    return set(normalized.split()) if normalized else set()


def _weights(note_tokens: list[set[str]]) -> dict[str, float]:
    """Weigh each word on the Shelf by how few notes carry it.

    A word naming three notes says far more about which book was meant than one
    naming fifty, and scoring every matched word alike is what put "The Hot One"
    above "The Final Empire: Mistborn Book 1" for "that mistborn one".

    Args:
        note_tokens: The word set of every Book Note being searched.

    Returns:
        A weight per word. `1 + n/df` rather than `n/df`, so a word every note
        carries still weighs something and a Shelf of near-identical titles does
        not score nothing at all.
    """
    total = len(note_tokens)
    frequency: dict[str, int] = {}
    for tokens in note_tokens:
        for token in tokens:
            frequency[token] = frequency.get(token, 0) + 1
    return {token: math.log(1 + total / count) for token, count in frequency.items()}


def _rank(
    query_tokens: set[str],
    note_tokens: set[str],
    weights: dict[str, float],
    distinctive: set[str],
) -> tuple[float, float] | None:
    """Score one Book Note against a query, or None when it does not match.

    Ranks rather than decides, for the reason find_similar does (ADR 0003):
    "Mercy" and "Long Road to Mercy" are different books that share a word, and
    only the person asking can say which they meant. Both are returned; the note
    the query describes best is put first.

    Args:
        query_tokens: The words the person used.
        note_tokens: The words in this note's title and authors.
        weights: What each word on this Shelf is worth.
        distinctive: The query's words that could name a book, so a note that
            matched only filler is never offered as an answer.

    Returns:
        The weight of what matched, and how much of the note that accounts for -
        so a short title matching one word outranks a long one matching the same
        word incidentally. None when nothing matched, or only filler did.
    """
    matched = query_tokens & note_tokens
    if not matched or not (matched & distinctive):
        return None

    scored = sum(weights.get(token, 0.0) for token in matched)
    available = sum(weights.get(token, 0.0) for token in note_tokens) or 1.0
    return scored, scored / available


def search_library(
    vault_path: Path,
    query: str | None = None,
    status: str | None = None,
    limit: int = DEFAULT_SEARCH_LIMIT,
) -> SearchResult:
    """Find the Book Notes a person is describing, without deciding which.

    The free-text half is deliberately fuzzy and the filter half is not. A
    status belongs to a closed vocabulary the Library defines (ADR 0022), so it
    narrows exactly; the words someone actually said are matched loosely and
    ranked, and every plausible answer goes back for them to settle (ADR 0003).

    Args:
        vault_path: The Shelf to search.
        query: What the person said, matched against titles and authors. When
            absent the filters answer alone - "what am I reading?" carries no
            query at all.
        status: A status to narrow to, from the Library's own vocabulary.
        limit: The most notes to return, clamped to MAX_SEARCH_LIMIT.

    Returns:
        The matching Book Notes, best first, and the total number of matches
        the limit may have cut short.

    Raises:
        InvalidFieldValue: If the status is not one the Library defines. Refused
            rather than quietly matching nothing, which would report an empty
            Library for a typo.
    """
    if status is not None:
        validate_field_value("status", status)

    limit = max(0, min(limit, MAX_SEARCH_LIMIT))
    query_tokens = _search_tokens(query) if query else set()

    # A query of nothing but filler is taken at face value - the alternative is
    # answering "the" with silence, when the Shelf may well hold "The Road".
    distinctive = query_tokens - _STOP_WORDS or query_tokens

    candidates: list[tuple[BookNote, set[str]]] = []
    for note in index_for(vault_path).notes():
        if not note.title:
            # Obsidian writes into this directory too, so a file that is not a
            # Book Note is ordinary rather than exceptional.
            continue
        if status is not None and note.frontmatter.get("status") != status:
            continue
        candidates.append(
            (note, _search_tokens(note.title) | _search_tokens(" ".join(note.authors)))
        )

    # Weighed across what the filter left, not the whole Shelf, so narrowing to
    # one status weighs words by how well they separate the books still in play.
    weights = _weights([tokens for _, tokens in candidates]) if query_tokens else {}

    scored: list[tuple[tuple, BookNote]] = []
    for note, note_tokens in candidates:
        sort_title = normalize_for_match(note.title or "")
        if not query_tokens:
            # Nothing was asked, so nothing is ranked. Alphabetical is the order
            # a person expects and, unlike relevance, is identical between two
            # identical calls.
            scored.append(((0.0, 0.0, 0, sort_title), note))
            continue
        rank = _rank(query_tokens, note_tokens, weights, distinctive)
        if rank is None:
            continue
        matched, density = rank
        scored.append(((-matched, -density, len(note.title or ""), sort_title), note))

    scored.sort(key=lambda pair: pair[0])
    return SearchResult(
        total=len(scored),
        limit=limit,
        books=[note for _, note in scored[:limit]],
    )


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
            title=existing.title,
            authors=existing.authors,
        )

    path = create_book_note(candidate, vault_path, overrides=overrides or None)
    note = BookNote.read(path)
    return AddResult(
        libris_id=note.libris_id if note else None,
        path=path,
        outcome=Outcome.CREATED,
        # The candidate is what was just written, so it answers for a note that
        # cannot be read back rather than leaving the Surface with only a path.
        title=note.title if note else candidate.title,
        authors=note.authors if note else candidate.authors,
    )


class DecisionStatus(Enum):
    """What became of one decision from an exported review."""

    MERGED = "merged"
    WOULD_MERGE = "would_merge"
    SKIPPED = "skipped"
    CONFLICTED = "conflicted"
    DRIFTED = "drifted"


@dataclass
class DecisionOutcome:
    """The result of applying one decision, and a line explaining it."""

    status: DecisionStatus
    detail: str


def build_id_index(vault_path: Path) -> dict[str, BookNote]:
    """Map every Libris ID the Shelf answers for to the note that answers.

    Includes superseded IDs, so an identity merged away still resolves
    (ADR 0014). A live identity always wins over a superseded one.

    Reading the whole Shelf once and looking up in a dict is the difference
    between a bulk run finishing and not: resolving 83 decisions one
    `find_by_libris_id` at a time did not complete inside ten minutes, because
    each miss reads every note.

    Args:
        vault_path: The Shelf to index.

    Returns:
        A mapping from Libris ID to Book Note.
    """
    index: dict[str, BookNote] = {}
    live: dict[str, BookNote] = {}

    for book_path in list_books(vault_path):
        note = BookNote.read(book_path)
        if note is None:
            continue
        for superseded in note.superseded_ids:
            index.setdefault(superseded, note)
        if note.libris_id:
            live[note.libris_id] = note

    index.update(live)
    return index


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
    index = build_id_index(vault_path)

    for decision in decisions:
        verdict = decision.get("decision")
        shorter = (decision.get("shorter") or {}).get("libris_id")
        longer = (decision.get("longer") or {}).get("libris_id")
        label = (decision.get("shorter") or {}).get("title") or "unknown"

        if verdict != "same":
            outcomes.append(
                DecisionOutcome(
                    DecisionStatus.SKIPPED, f"{label}: recorded as two books"
                )
            )
            continue

        if not shorter or not longer:
            outcomes.append(
                DecisionOutcome(
                    DecisionStatus.DRIFTED, f"{label}: decision names no Libris ID"
                )
            )
            continue

        first = index.get(shorter.strip())
        second = index.get(longer.strip())
        if first is None or second is None or first.path == second.path:
            outcomes.append(
                DecisionOutcome(
                    DecisionStatus.DRIFTED,
                    f"{label}: no longer two notes on the Shelf",
                )
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
                DecisionOutcome(
                    DecisionStatus.CONFLICTED, f"{label}: {fields} disagree"
                )
            )
            continue

        if dry_run:
            outcomes.append(
                DecisionOutcome(
                    DecisionStatus.WOULD_MERGE, f"{label} -> {primary.name}"
                )
            )
            continue

        write_merged_book(primary, merged_fm, merged_body)
        delete_secondary_file(secondary)

        # The Shelf just changed, so the index has to change with it: the
        # survivor now answers for the identities the deleted note held.
        survivor = BookNote.read(primary)
        if survivor is not None:
            for key, note in list(index.items()):
                if note.path in (primary, secondary):
                    index[key] = survivor

        outcomes.append(
            DecisionOutcome(DecisionStatus.MERGED, f"{label} -> {primary.name}")
        )

    return outcomes
