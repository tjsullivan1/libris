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


@pytest.mark.parametrize("field", MODELLED_FIELDS)
def test_every_modelled_field_appears_in_the_readme(field):
    # Given the canonical shape of a Book Note
    # Then the README names it. Seven were missing, including `libris_id`, which
    # is the identity everything else hangs off.
    assert f"`{field}`" in _schema_section(), (
        f"{field} is in MODELLED_FIELDS but not in the README's Schema section."
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
    section = _schema_section()

    # Then the README lists them. A reader consulting this to write a note by
    # hand needs to know that "Finished" is not a status, which is the mistake
    # the CLI's own prompt used to make.
    for value in values:
        assert value in section, (
            f"{field} may be {value!r}, and the README does not say so."
        )


@pytest.mark.parametrize("field", sorted(MULTI_VALUED_FIELDS & set(MODELLED_FIELDS)))
def test_a_field_holding_several_values_is_marked_as_such(field):
    # Given a modelled field that holds a list
    section = _schema_section()
    row = next(
        (line for line in section.splitlines() if line.startswith(f"| `{field}`")),
        None,
    )
    assert row is not None, f"{field} has no row in the README's Schema section."

    # Then its row says so. `status: [Read]` is refused and `format: Audiobook`
    # is repaired, and the difference is not guessable from a flat list.
    assert "list" in row.lower(), (
        f"{field} holds several values at once and its README row does not say so."
    )
