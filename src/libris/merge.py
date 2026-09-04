"""
Book merge functionality for handling duplicate entries.

Implements intelligent merging of two books with conflict detection and user-guided resolution.
"""

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from .markdown import (
    _normalize_author,
    read_frontmatter,
    split_frontmatter,
    tidy_author,
)
from .note_format import (
    MULTI_VALUED_FIELDS,
    READER_FIELDS,
    SUPERSEDED_IDS_FIELD,
    read_superseded_ids,
)


@dataclass
class MergeConflict:
    """Represents a conflicting field during merge."""

    field: str
    primary_value: Any
    secondary_value: Any


def _count_nonnull_fields(frontmatter: Dict[str, Any]) -> int:
    """Count non-null fields in frontmatter (for determining completeness)."""
    return sum(1 for v in frontmatter.values() if v is not None and v != "")


def _extract_body_content(file_path: Path) -> str:
    """Extract the body content (everything after frontmatter) from a markdown file."""
    content = file_path.read_text(encoding="utf-8")
    split = split_frontmatter(content)
    if split is None:
        return content
    return split[1]


def _merge_body_content(primary_body: str, secondary_body: str) -> str:
    """
    Merge body content from two books.

    - Preserves all user notes from both files
    - Skips generic "### Description" sections (these come from API, not user content)
    - Ensures proper formatting
    """
    # Remove generic Description sections (these are auto-populated from API)
    primary_cleaned = re.sub(
        r"### Description\n.*?(?=\n###|\Z)", "", primary_body, flags=re.DOTALL
    )
    secondary_cleaned = re.sub(
        r"### Description\n.*?(?=\n###|\Z)", "", secondary_body, flags=re.DOTALL
    )

    # Strip and combine
    primary_cleaned = primary_cleaned.strip()
    secondary_cleaned = secondary_cleaned.strip()

    # Combine non-empty bodies
    if primary_cleaned and secondary_cleaned:
        merged = f"{primary_cleaned}\n\n---\n\n**Merged from duplicate entry:**\n\n{secondary_cleaned}"
    elif secondary_cleaned:
        merged = secondary_cleaned
    else:
        merged = primary_cleaned

    return merged.strip()


def _resolve_field_value(
    field: str, primary_value: Any, secondary_value: Any
) -> Tuple[Any, bool]:
    """
    Resolve a single field during merge.

    Returns: (resolved_value, is_conflict)

    Priority rules:
    - If either is None/empty, use the other
    - Special handling for 'status': "Read" beats "To Read"
    - For lists (author, genres, tags): merge unique items
    - For other fields with conflict: return None and mark conflict
    """
    # Normalize None and empty string
    primary_empty = primary_value is None or primary_value == ""
    secondary_empty = secondary_value is None or secondary_value == ""

    if primary_empty and secondary_empty:
        return None, False
    if primary_empty:
        return secondary_value, False
    if secondary_empty:
        return primary_value, False

    # Same values: no conflict
    if primary_value == secondary_value:
        return primary_value, False

    # Special handling for status field
    if field == "status":
        if primary_value == "Read" or secondary_value == "Read":
            return "Read", False  # "Read" beats everything
        return primary_value, False  # Use primary as default

    # Fields that hold several values are combined rather than contested. The
    # set is named once in note_format: this list used to say "author" while
    # every note carries "authors", so author lists were reported as conflicts
    # and the secondary's were dropped (#72). `format` was never here at all.
    if field in MULTI_VALUED_FIELDS:
        return _union_values(field, primary_value, secondary_value), False

    # Everything else takes the keeper's value without asking, unless the value
    # is the reader's own. Obsidian's timestamps never agree between two files;
    # an ISBN, a page count or a cover differs because two editions of one work
    # differ; and two notes always hold different Libris IDs, which ADR 0014
    # records as superseded rather than contested. None of those is a question
    # for a person. A rating is (ADR 0018).
    if field not in READER_FIELDS:
        return primary_value, False

    return primary_value, True


def _union_values(field: str, primary_value: Any, secondary_value: Any) -> List[Any]:
    """Combine two multi-valued fields, keeping every distinct value once.

    Authors are compared on their fully normalized name, so one person spelled
    `Dan   Harris` in one note and `[[Dan Harris]]` in the other survives once
    rather than twice. What is written is only whitespace-collapsed, because
    unwrapping the wikilink would delete a graph edge (ADR 0018).

    Args:
        field: The field being combined.
        primary_value: The keeper's value, which may be a bare string.
        secondary_value: The value from the note being merged away.

    Returns:
        The combined values, keeper's first.
    """
    primary_list = primary_value if isinstance(primary_value, list) else [primary_value]
    secondary_list = (
        secondary_value if isinstance(secondary_value, list) else [secondary_value]
    )

    merged: List[Any] = []
    seen = set()
    for item in list(primary_list) + list(secondary_list):
        if not item:
            continue
        if field == "authors" and isinstance(item, str):
            written = tidy_author(item)
            identity = _normalize_author(item)
        else:
            written = item
            identity = str(item)
        if identity in seen:
            continue
        merged.append(written)
        seen.add(identity)

    return merged if merged else primary_value


def _collect_superseded_ids(
    primary_fm: Dict[str, Any], secondary_fm: Dict[str, Any]
) -> List[str]:
    """Gather every identity the surviving note should answer for (ADR 0014).

    A merge destroys the secondary note, and its Libris ID with it unless the
    survivor records it. The secondary may itself have absorbed others, so the
    chain carries forward: without that, a second merge silently breaks what the
    first one preserved.

    Args:
        primary_fm: Frontmatter of the surviving note.
        secondary_fm: Frontmatter of the note being merged away.

    Returns:
        The superseded identities, in the order they were absorbed, without
        duplicates and never including the survivor's own live identity.
    """
    survivor_id = primary_fm.get("libris_id")

    # Read through the shared rule rather than iterating the raw value: a bare
    # string would otherwise be walked character by character, and every letter
    # would look like an identity.
    candidates: List[Any] = []
    candidates.extend(read_superseded_ids(primary_fm.get(SUPERSEDED_IDS_FIELD)))
    candidates.append(secondary_fm.get("libris_id"))
    candidates.extend(read_superseded_ids(secondary_fm.get(SUPERSEDED_IDS_FIELD)))

    collected: List[str] = []
    for value in candidates:
        if not isinstance(value, str):
            continue
        identity = value.strip()
        if not identity or identity == survivor_id or identity in collected:
            continue
        collected.append(identity)
    return collected


def merge_two_books(
    primary_path: Path, secondary_path: Path, allow_conflicts: bool = False
) -> Tuple[Dict[str, Any], str, List[MergeConflict]]:
    """
    Merge two book files intelligently.

    Args:
        primary_path: Path to the primary (keeper) book file
        secondary_path: Path to the secondary (to-be-deleted) book file
        allow_conflicts: If False, conflicts prevent auto-merge (return them for user review)

    Returns:
        (merged_frontmatter, merged_body, conflicts_list)

        If conflicts list is non-empty and allow_conflicts=False, the merge was prevented.
        Otherwise, frontmatter and body are ready to write.
    """
    primary_fm = read_frontmatter(primary_path)
    secondary_fm = read_frontmatter(secondary_path)

    if not primary_fm or not secondary_fm:
        raise ValueError("Could not read frontmatter from one or both book files")

    # Extract body content
    primary_body = _extract_body_content(primary_path)
    secondary_body = _extract_body_content(secondary_path)

    # Merge frontmatter fields
    merged_fm = {}
    conflicts = []

    # Get all possible field names
    all_fields = set(primary_fm.keys()) | set(secondary_fm.keys())

    for field in all_fields:
        primary_val = primary_fm.get(field)
        secondary_val = secondary_fm.get(field)

        resolved_val, has_conflict = _resolve_field_value(
            field, primary_val, secondary_val
        )
        merged_fm[field] = resolved_val

        if has_conflict:
            conflicts.append(MergeConflict(field, primary_val, secondary_val))

    # The secondary is about to be deleted, so the survivor takes on the
    # identities it answered for (ADR 0014). Done outside the field loop because
    # this is not a value being reconciled: it is the record of what was destroyed.
    superseded = _collect_superseded_ids(primary_fm, secondary_fm)
    if superseded:
        merged_fm[SUPERSEDED_IDS_FIELD] = superseded
    else:
        merged_fm.pop(SUPERSEDED_IDS_FIELD, None)

    # If there are conflicts and we don't allow them, return early
    if conflicts and not allow_conflicts:
        return merged_fm, "", conflicts

    # Merge body content
    merged_body = _merge_body_content(primary_body, secondary_body)

    return merged_fm, merged_body, conflicts


def check_auto_merge(
    primary_path: Path, secondary_path: Path
) -> Tuple[
    bool, Optional[str], Optional[Tuple[Dict[str, Any], str, List[MergeConflict]]]
]:
    """
    Determine if two books can be auto-merged based on ISBN and Google Books ID match.

    Also checks for metadata conflicts that would prevent automatic merging.

    When ISBN and Google ID both match, we allow title differences (they may be normalized
    differently), but flag other metadata conflicts.

    Returns:
        (can_auto_merge, reason_if_no, merge_result_or_none)

        When can_auto_merge is True, merge_result contains (merged_fm, merged_body, conflicts)
        so the caller can write immediately without re-reading the files.
    """
    primary_fm = read_frontmatter(primary_path)
    secondary_fm = read_frontmatter(secondary_path)

    if not primary_fm or not secondary_fm:
        return False, "Could not read frontmatter", None

    # Check ISBN and Google Books ID match
    primary_isbn = primary_fm.get("isbn")
    secondary_isbn = secondary_fm.get("isbn")
    primary_gid = primary_fm.get("google_books_id")
    secondary_gid = secondary_fm.get("google_books_id")

    # Both ISBN and Google ID must match to auto-merge
    isbn_match = (
        primary_isbn and secondary_isbn and str(primary_isbn) == str(secondary_isbn)
    )
    gid_match = primary_gid and secondary_gid and str(primary_gid) == str(secondary_gid)

    if not (isbn_match and gid_match):
        return False, "ISBN or Google Books ID mismatch", None

    # Check for metadata conflicts (excluding title, which may differ due to normalization)
    merged_fm, merged_body, conflicts = merge_two_books(
        primary_path, secondary_path, allow_conflicts=True
    )

    if conflicts:
        # Filter out title conflicts (acceptable when ISBN and Google ID match)
        non_title_conflicts = [c for c in conflicts if c.field != "title"]
        if non_title_conflicts:
            conflict_fields = [c.field for c in non_title_conflicts]
            return False, f"Metadata conflicts in: {', '.join(conflict_fields)}", None

    return True, None, (merged_fm, merged_body, conflicts)


def write_merged_book(
    primary_path: Path, merged_frontmatter: Dict[str, Any], merged_body: str
) -> None:
    """Write merged frontmatter and body to the primary file."""
    frontmatter_yaml = yaml.dump(
        merged_frontmatter, sort_keys=False, allow_unicode=True
    ).strip()
    content = f"---\n{frontmatter_yaml}\n---\n{merged_body.lstrip()}"
    primary_path.write_text(content, encoding="utf-8")


def delete_secondary_file(secondary_path: Path) -> None:
    """Delete the secondary (merged-away) file."""
    secondary_path.unlink()


def get_primary_book(path1: Path, path2: Path) -> Path:
    """
    Determine which book should be primary (keeper) based on completeness.

    Strategy:
    1. Book with more non-null frontmatter fields is primary
    2. Tiebreaker: earlier date_added
    3. Fallback: path1
    """
    fm1 = read_frontmatter(path1)
    fm2 = read_frontmatter(path2)

    if not fm1 or not fm2:
        return path1

    count1 = _count_nonnull_fields(fm1)
    count2 = _count_nonnull_fields(fm2)

    if count1 != count2:
        return path1 if count1 > count2 else path2

    # Tiebreaker: earlier date_added
    date1 = fm1.get("date_added")
    date2 = fm2.get("date_added")

    if date1 and date2:
        try:
            if date1 < date2:
                return path1
            else:
                return path2
        except TypeError:
            pass

    return path1
