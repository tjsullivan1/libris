"""Tests for the shared matching helpers.

These cover the behaviour every adapter in workstream 2 depends on, lifted out
of cli.py by #63. The move is meant to preserve behaviour exactly, so these
assert what the helpers did in cli.py rather than what they arguably should do.
"""

import pytest

from libris.api import BookCandidate
from libris.matching import (
    best_match,
    build_search_query,
    metadata_score,
    normalize_for_match,
    titles_match,
)


def _candidate(**overrides) -> BookCandidate:
    """Build a BookCandidate with only the fields a test cares about."""
    fields = {"title": "Dune", "authors": ["Frank Herbert"]}
    fields.update(overrides)
    return BookCandidate(**fields)


# --- normalize_for_match ---


def test_normalize_lowercases_and_collapses_whitespace():
    # Given text with mixed case and irregular spacing
    text = "  The   Way OF Kings  "

    # When it is normalized
    result = normalize_for_match(text)

    # Then case is flattened and runs of whitespace become single spaces
    assert result == "the way of kings"


def test_normalize_replaces_punctuation_with_spaces():
    # Given a title carrying punctuation
    text = "It's a Test: Book #1"

    # When it is normalized
    result = normalize_for_match(text)

    # Then punctuation becomes whitespace rather than being deleted
    assert result == "it s a test book 1"


def test_normalize_of_punctuation_only_text_is_empty():
    # Given text with nothing but punctuation
    text = "--- !!! ---"

    # When it is normalized
    result = normalize_for_match(text)

    # Then the result is empty, which callers treat as unmatchable
    assert result == ""


# --- build_search_query ---


def test_build_search_query_splits_title_from_author():
    # Given a filename stem in "Title - Author" form
    stem = "Dune - Frank Herbert"

    # When a query is built
    query = build_search_query(stem)

    # Then title and author become separate Google Books operators
    assert query == "intitle:Dune inauthor:Frank Herbert"


def test_build_search_query_splits_only_on_the_first_separator():
    # Given a stem whose title itself contains " - "
    stem = "Leviathan Wakes - Book 1 - James S. A. Corey"

    # When a query is built
    query = build_search_query(stem)

    # Then everything after the first separator is treated as the author
    assert query == "intitle:Leviathan Wakes inauthor:Book 1 - James S. A. Corey"


def test_build_search_query_without_a_separator_returns_the_stem():
    # Given a stem with no " - " separator
    stem = "Poems"

    # When a query is built
    query = build_search_query(stem)

    # Then the stem is used as-is, with no operators
    assert query == "Poems"


# --- titles_match ---


def test_titles_match_when_the_stem_is_contained_in_the_title():
    # Given a filename stem that is a prefix of the candidate's fuller title
    # When the two are compared
    # Then they match
    assert titles_match("Dune", "Dune: Deluxe Edition") is True


def test_titles_match_when_the_title_is_contained_in_the_stem():
    # Given a stem carrying more than the candidate's title does
    # When the two are compared
    # Then containment in either direction counts as a match
    assert titles_match("Dune Messiah Special", "Dune Messiah") is True


def test_titles_match_ignores_case_and_punctuation():
    # Given the same title differing only in case and punctuation
    # When the two are compared
    # Then normalization makes them match
    assert titles_match("the hobbit", "The Hobbit!") is True


def test_titles_do_not_match_when_unrelated():
    # Given two unrelated titles
    # When they are compared
    # Then they do not match
    assert titles_match("Dune", "Neuromancer") is False


def test_titles_do_not_match_when_either_normalizes_to_empty():
    # Given a stem that is nothing but punctuation
    # When it is compared against a real title
    # Then it does not match, rather than matching everything by empty containment
    assert titles_match("...", "Dune") is False
    assert titles_match("Dune", "...") is False


# --- metadata_score ---


def test_metadata_score_counts_populated_fields():
    # Given a candidate with three of the counted fields populated
    candidate = _candidate(
        isbn="9780441013593", page_count=412, description="A desert."
    )

    # When it is scored
    score = metadata_score(candidate)

    # Then each populated field contributes one point
    assert score == 3


def test_metadata_score_ignores_empty_values():
    # Given a candidate whose counted fields are empty rather than absent
    candidate = _candidate(isbn="", page_count=0, genres=[], description=None)

    # When it is scored
    score = metadata_score(candidate)

    # Then nothing empty is counted
    assert score == 0


def test_metadata_score_ignores_uncounted_fields():
    # Given a candidate populated only in fields outside the completeness set
    candidate = _candidate(title="Dune", authors=["Frank Herbert"], source="audible")

    # When it is scored
    score = metadata_score(candidate)

    # Then title, authors and source do not contribute
    assert score == 0


# --- best_match ---


def test_best_match_picks_the_most_complete_candidate():
    # Given three candidates of differing completeness
    sparse = _candidate(isbn="1")
    rich = _candidate(isbn="2", page_count=412, genres=["SF"], description="A desert.")
    middling = _candidate(isbn="3", page_count=412)

    # When the best is chosen
    chosen = best_match([sparse, rich, middling])

    # Then the one with the most populated metadata fields wins
    assert chosen is rich


def test_best_match_returns_the_first_of_an_equal_scoring_tie():
    # Given two candidates scoring identically
    first = _candidate(isbn="1")
    second = _candidate(isbn="2")

    # When the best is chosen
    chosen = best_match([first, second])

    # Then the earlier candidate wins, preserving the API's own ranking
    assert chosen is first


def test_best_match_on_an_empty_list_raises():
    # Given no candidates at all
    # When the best is chosen
    # Then it raises rather than inventing a result; callers guard for emptiness
    with pytest.raises(ValueError):
        best_match([])


# --- re-exports ---


def test_cli_re_exports_the_same_objects():
    # Given cli.py keeps thin re-exports so existing call sites are untouched
    from libris import cli

    # When the re-exported names are resolved
    # Then they are the very functions defined in matching, not copies
    assert cli._normalize_for_match is normalize_for_match
    assert cli._build_search_query is build_search_query
    assert cli._titles_match is titles_match
    assert cli._metadata_score is metadata_score
    assert cli._best_match is best_match


def test_importer_re_exports_the_same_normalizer():
    # Given importer.normalize_for_match is imported by existing tests
    from libris import importer

    # When the name is resolved
    # Then it is the shared implementation rather than a second one
    assert importer.normalize_for_match is normalize_for_match
