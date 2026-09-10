"""The README's Schema section is checked against the code that defines it.

The previous version of that section was retyped by hand and drifted: it named
`author`, `published_date` and `thumbnail`, none of which exist, and omitted
seven fields that do. Those same three names in the code are what made every
auto-enrich query title-only and reported 152 notes as unenriched when 5 were
(#61, #84).

The section could be generated from `MODELLED_FIELDS` instead. A test is
preferred: generation needs a build step and makes the README a derived file
nobody edits, while this leaves it prose a person writes and only fails when it
stops being true.
"""

import re
from pathlib import Path

import pytest

from libris.note_format import (
    FIELD_VOCABULARIES,
    MODELLED_FIELDS,
    MULTI_VALUED_FIELDS,
    SUPERSEDED_IDS_FIELD,
)

README = Path(__file__).resolve().parent.parent / "README.md"

# Fields the Library writes but does not model, so the section may name them
# without failing the "nothing invented" check.
_UNMODELLED_BUT_WRITTEN = {SUPERSEDED_IDS_FIELD, "aliases"}


def _schema_section() -> str:
    """The README's Schema section, from its heading to the next top-level one."""
    text = README.read_text(encoding="utf-8")
    start = text.index("## Schema")
    rest = text[start + len("## Schema") :]
    end = rest.find("\n## ")
    return rest if end == -1 else rest[:end]


def _named_fields(section: str) -> set[str]:
    """Every field the section *presents* as one - the first cell of a table row.

    Deliberately not every backticked word. The section closes by naming the
    three fields that used to be listed here and do not exist, so that the drift
    is recorded rather than quietly corrected; scanning prose would read those
    as claims that they are real.
    """
    return set(re.findall(r"^\|\s*`([a-z_][a-z0-9_]*)`\s*\|", section, re.MULTILINE))


def _field_row(field: str) -> str | None:
    """The Schema table row documenting one field, or None when it has none."""
    return next(
        (
            line
            for line in _schema_section().splitlines()
            if line.startswith(f"| `{field}`")
        ),
        None,
    )


@pytest.mark.parametrize("field", MODELLED_FIELDS)
def test_every_modelled_field_appears_in_the_readme(field):
    # Given the canonical shape of a Book Note
    # Then the README gives it a row of its own. A substring search over the
    # section passed on a mention in someone else's prose - `date_added` is
    # named in the `libris_id` row - so a field could lose its row and still
    # look documented. The acceptance criterion is "appears under its real
    # name", which means a row.
    assert field in _named_fields(_schema_section()), (
        f"{field} is in MODELLED_FIELDS but has no row in the README's Schema section."
    )


def test_the_readme_invents_no_fields():
    # Given the fields the README names
    named = _named_fields(_schema_section())
    known = set(MODELLED_FIELDS) | _UNMODELLED_BUT_WRITTEN

    # Then each is one the Library actually writes. `author`, `published_date`
    # and `thumbnail` sat here for months describing a schema that had been
    # renamed out from under them (ADR 0005).
    invented = named - known
    assert invented == set(), (
        f"The README's Schema section names {sorted(invented)}, which "
        "MODELLED_FIELDS does not contain."
    )


@pytest.mark.parametrize("field,values", sorted(FIELD_VOCABULARIES.items()))
def test_a_field_with_a_closed_vocabulary_states_it(field, values):
    # Given a field whose values the Library defines
    row = _field_row(field)
    assert row is not None, f"{field} has no row in the README's Schema section."

    # Then its own row lists them, each as a bolded token. Two ways of checking
    # this were wrong before it worked. A plain substring search cannot tell a
    # value from one containing it - "To Read" is inside "Not To Read". Bolding
    # fixed that but scanning the whole section did not: dropping **Read** from
    # the status vocabulary still passed, because the `date_finished` row says
    # "becomes **Read**". A vocabulary is stated where the field is documented
    # or it is not stated.
    for value in values:
        assert f"**{value}**" in row, (
            f"{field} may be {value!r}, and its README row does not state it."
        )


@pytest.mark.parametrize("field", sorted(MULTI_VALUED_FIELDS & set(MODELLED_FIELDS)))
def test_a_field_holding_several_values_is_marked_as_such(field):
    # Given a modelled field that holds a list
    row = _field_row(field)
    assert row is not None, f"{field} has no row in the README's Schema section."

    # Then its row says so. `status: [Read]` is refused and `format: Audiobook`
    # is repaired, and the difference is not guessable from a flat list.
    assert "list" in row.lower(), (
        f"{field} holds several values at once and its README row does not say so."
    )
