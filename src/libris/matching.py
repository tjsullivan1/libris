"""Deciding which Book Candidate a reference means.

Matching is the fuzzy judgement behind duplicate detection, import
de-duplication and enrichment: whether two descriptions of a book describe the
same book. It is deliberately separate from identity, which is exact and lives
in the Libris ID (ADR 0001).

These helpers were private to cli.py until #63. Every adapter in workstream 2
needs them - the `libris serve` endpoints first, the MCP tools after - and none
of them should reach into the CLI module to get them.
"""

import re

from .api import BookCandidate

# Fields counted when ranking which edition has the most complete metadata.
COMPLETENESS_FIELDS = (
    "isbn",
    "page_count",
    "published_date",
    "google_books_id",
    "thumbnail",
    "genres",
    "description",
)


def normalize_for_match(text: str) -> str:
    """Normalize text for fuzzy comparison.

    Lowercases, replaces punctuation with whitespace, and collapses runs of
    whitespace. Punctuation becomes a space rather than being deleted, so
    "Book:One" and "Book One" normalize alike.

    Args:
        text: The text to normalize.

    Returns:
        The normalized text, which may be empty if the input held no word
        characters.
    """
    text = re.sub(r"[^\w\s]", " ", text.lower())
    return re.sub(r"\s+", " ", text).strip()


def build_search_query(filename_stem: str) -> str:
    """Build a Google Books search query from a filename stem.

    Handles the "Title - Author" and "Title Subtitle - Author" shapes the Shelf
    uses, turning them into `intitle:`/`inauthor:` operators so the search is
    constrained by author rather than title alone. Splits on the first " - "
    only, so a title containing a separator keeps everything after it as the
    author.

    Args:
        filename_stem: A Book Note's filename without its extension.

    Returns:
        A query string, or the stem unchanged when it holds no separator.
    """
    parts = filename_stem.split(" - ", maxsplit=1)
    if len(parts) == 2:
        title_part = parts[0].strip()
        author_part = parts[1].strip()
        return f"intitle:{title_part} inauthor:{author_part}"
    return filename_stem


def titles_match(filename_stem: str, book_title: str) -> bool:
    """Check whether a Book Candidate's title fuzzily matches a filename stem.

    True when either normalized string contains the other, which tolerates the
    subtitle a candidate carries and the filename does not.

    Args:
        filename_stem: A Book Note's filename without its extension.
        book_title: The title a Book Candidate proposes.

    Returns:
        True if the two plausibly describe the same book. False if either
        normalizes to empty, since empty is contained in everything and would
        otherwise match indiscriminately.
    """
    norm_file = normalize_for_match(filename_stem)
    norm_title = normalize_for_match(book_title)
    if not norm_file or not norm_title:
        return False
    return norm_file in norm_title or norm_title in norm_file


def metadata_score(book: BookCandidate) -> int:
    """Count how many of the completeness fields a Book Candidate populates.

    A proxy for which edition is best described, used to choose between
    candidates that all match the same book.

    Args:
        book: The candidate to score.

    Returns:
        The number of populated fields, between zero and the length of
        COMPLETENESS_FIELDS.
    """
    score = 0
    for field in COMPLETENESS_FIELDS:
        if getattr(book, field, None):
            score += 1
    return score


def best_match(candidates: list[BookCandidate]) -> BookCandidate:
    """Pick the Book Candidate with the most complete metadata.

    Ties keep the earliest candidate, preserving the order the source ranked
    them in.

    Args:
        candidates: The candidates to choose between. Must not be empty.

    Returns:
        The candidate with the highest metadata score.

    Raises:
        ValueError: If `candidates` is empty. Callers check for no results
            before choosing between them.
    """
    return max(candidates, key=metadata_score)
