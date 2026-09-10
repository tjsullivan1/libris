"""Tests enforcing ADR 0028: a Book Note is read through one reader.

These assert on the source rather than on behaviour, which is unusual and
deliberate. The defect ADR 0028 records is not a wrong answer that a normal
test would catch - it is a *second* reader, written later, that quietly knows
less than the first. Its symptom is a shorter list, not an error.

Five changes have fixed that defect (#92, #99, #101, #105, #75). Every one was
found after it shipped, and the last was written in ten minutes with the obvious
tool by somebody who had just spent two days consolidating readers. A document
does not catch that. This does.
"""

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src" / "libris"

# `markdown.py` defines the reader, so it is the one module allowed to know how
# a frontmatter fence is written.
#
# Only *reading* a fence counts. Composing one, to write a note back out, is not
# the defect this guards and several modules legitimately do it. What must not
# spread is code that decides whether some text *is* a fence, because that is
# the knowledge that drifted nine ways in #101.
#
# The two are told apart by metacharacters: a pattern that reads a fence
# anchors, escapes or groups, and a string that composes one does none of it.
# Written with chr(92) rather than a backslash so this file does not trip its
# own guard.
_PATTERN_TOKENS = ("^", chr(92) + "s", "(.*", "(?", "[^", chr(92) + "n(")

# Walking the whole Shelf to look for damage is what `_read_shelf` is for.
# `index_for` is a cached index for Library queries, and `BookNote.read` returns
# None for a note whose frontmatter will not parse - so the index omits exactly
# the notes a damage check exists to find (#75).
_DAMAGE_CHECKS = ("find_encoding_damage", "find_id_collisions", "inspect_shelf")


def _module_sources() -> dict[str, str]:
    """Every libris module, by filename."""
    return {
        path.name: path.read_text(encoding="utf-8") for path in sorted(SRC.glob("*.py"))
    }


def _fence_parsers(source: str) -> list[str]:
    """Find code that decides whether text is a frontmatter fence.

    Args:
        source: A module's source.

    Returns:
        A description of each offending expression, empty when there are none.
    """
    found: list[str] = []
    tree = ast.parse(source)

    for node in ast.walk(tree):
        # `something == "---"`, or `line.strip() != "---"`.
        if isinstance(node, ast.Compare):
            operands = [node.left, *node.comparators]
            if any(
                isinstance(operand, ast.Constant)
                and isinstance(operand.value, str)
                and operand.value.strip() == "---"
                for operand in operands
            ):
                found.append(f"line {node.lineno}: compares against a --- fence")

        # Every fence-matching pattern in the module, wherever it sits. Checking
        # only the first argument of `re.match(...)` would miss
        # `PATTERN = r"^---"` used a hundred lines later - and a guard against a
        # future author that only catches the naive spelling is not much of one.
        #
        # A pattern is told from a composed fence by its metacharacters:
        # `f"---\n{frontmatter}\n---\n"` writes a note back out and carries
        # none, while `r"^---\s*\n(.*?)\n---"` reads one and carries several.
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and "---" in node.value
            and any(token in node.value for token in _PATTERN_TOKENS)
        ):
            found.append(f"line {node.lineno}: holds a --- fence pattern")

    return found


def test_only_markdown_knows_how_a_frontmatter_fence_is_written():
    # Given every module in the package but the one that defines the reader
    offenders = []
    for name, source in _module_sources().items():
        if name == "markdown.py":
            continue
        offenders.extend(f"{name}:{where}" for where in _fence_parsers(source))

    # Then none of them decides for itself what a fence is. Nine hand-rolled
    # splits in three disagreeing dialects is what #101 removed; this is what
    # stops the tenth being written. Read through `markdown.split_frontmatter`,
    # or `markdown.unterminated_frontmatter` for a block that is never closed.
    assert offenders == [], "; ".join(offenders)


def test_a_damage_check_does_not_read_the_shelf_through_the_index():
    # Given the service module, where the Shelf-wide checks live
    source = (SRC / "service.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    functions = {
        node.name: node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
    }

    offenders = []
    for name in _DAMAGE_CHECKS:
        assert name in functions, f"{name} has been renamed; update ADR 0028's test"
        # Both spellings: `index_for(...)` and `shelf.index_for(...)`. A guard
        # that only sees the bare name is undone by an import style change.
        called = set()
        for call in ast.walk(functions[name]):
            if not isinstance(call, ast.Call):
                continue
            if isinstance(call.func, ast.Name):
                called.add(call.func.id)
            elif isinstance(call.func, ast.Attribute):
                called.add(call.func.attr)
        if "index_for" in called:
            offenders.append(name)

    # Then no damage check reaches for the index. `BookNote.read` returns None
    # for a note whose frontmatter will not parse, so the index silently omits
    # precisely the notes these checks exist to find (#75). Use `_read_shelf`.
    assert offenders == [], (
        f"{', '.join(offenders)} read the Shelf through index_for, which drops "
        "unparseable notes. See ADR 0028."
    )


@pytest.mark.parametrize("check", _DAMAGE_CHECKS)
def test_every_damage_check_is_named_in_the_test_that_guards_them(check):
    # Given the list this test file guards
    source = (SRC / "service.py").read_text(encoding="utf-8")

    # Then each name in it still exists. The guard is only worth as much as its
    # list, and a check renamed or added without updating this is how the list
    # silently stops covering the module.
    assert f"def {check}(" in source
