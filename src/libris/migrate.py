"""One-time migration of the Shelf to the canonical Book Note shape.

Mints a Libris ID into every note, adds the fields promoted to first class,
regroups frontmatter, restructures the body, and repairs four flavours of damage
left by Obsidian Linter reading headings it should not have.

Frontmatter is manipulated as text blocks rather than round-tripped through YAML,
so a value that did not change renders exactly as it did before. The diff a
reader reviews then shows only what the migration actually decided to do.

See ADR 0001, ADR 0005, ADR 0009, ADR 0011 and ADR 0012.
"""

import difflib
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .markdown import BookNote, list_books
from .note_format import (
    FORMAT_VALUES,
    LEAKED_HEADINGS,
    MODELLED_FIELDS,
    has_description_callout,
    has_title_heading,
    mint_libris_id,
    read_formats,
    render_body,
    split_body,
)

_KEY_LINE = re.compile(r"^([A-Za-z_][A-Za-z0-9_ \-]*):")


@dataclass
class NoteMigration:
    """What the migration would do to one Book Note."""

    path: Path
    original: str
    migrated: str
    changes: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        """Whether anything about the note would actually be rewritten."""
        return self.original != self.migrated

    def diff(self) -> str:
        """A unified diff of the rewrite, for review before anything is written."""
        return "".join(
            difflib.unified_diff(
                self.original.splitlines(keepends=True),
                self.migrated.splitlines(keepends=True),
                fromfile=f"a/{self.path.name}",
                tofile=f"b/{self.path.name}",
            )
        )


def split_frontmatter_blocks(frontmatter_text: str) -> list[tuple[str, str]]:
    """Split raw frontmatter into (key, raw lines) pairs.

    Continuation lines — the indented items of a sequence, or a wrapped scalar —
    stay attached to the key above them, so a block can be moved without
    disturbing how its value is written.

    Args:
        frontmatter_text: The text between the two `---` fences.

    Returns:
        One pair per key, in the order they appear.
    """
    blocks: list[tuple[str, str]] = []
    key: str | None = None
    lines: list[str] = []

    for line in frontmatter_text.splitlines():
        match = _KEY_LINE.match(line)
        if match:
            if key is not None:
                blocks.append((key, "\n".join(lines)))
            key = match.group(1)
            lines = [line]
        elif key is not None:
            lines.append(line)

    if key is not None:
        blocks.append((key, "\n".join(lines)))
    return blocks


def reorder_frontmatter(blocks: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Group frontmatter blocks into identity, bibliographic, reading state, dates.

    Modelled fields the note lacks are added empty. Anything Libris does not
    model — fields a plugin added — keeps its relative order and follows the rest,
    unrewritten (ADR 0005).

    Args:
        blocks: Pairs from `split_frontmatter_blocks`.

    Returns:
        The same blocks, reordered, plus any modelled field that was missing.
    """
    by_key = dict(blocks)
    ordered = [(key, by_key.get(key, f"{key}:")) for key in MODELLED_FIELDS]
    ordered.extend((key, raw) for key, raw in blocks if key not in MODELLED_FIELDS)
    return ordered


def _render_scalar(key: str, value: str) -> str:
    """Render a scalar frontmatter line in the style the vault already uses.

    Args:
        key: The frontmatter key.
        value: The value to write.

    Returns:
        A single `key: value` line, quoted only when YAML requires it.
    """
    needs_quote = (
        not value
        or value != value.strip()
        or value[:1] in "#&*!|>%@`\"'-?{}[],"
        or ": " in value
        or value.endswith(":")
        or value.lower() in {"true", "false", "null", "yes", "no", "on", "off", "~"}
    )
    if needs_quote:
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'{key}: "{escaped}"'
    return f"{key}: {value}"


def recover_title(note: BookNote) -> tuple[str | None, str | None]:
    """Recover a title Obsidian Linter overwrote or polluted with the author.

    Two flavours of the same damage. Where there was no H1 the linter took the
    `## Notes` heading as the title; where `yaml-title` ran with `mode: filename`
    it wrote `Title - Author`, putting the author inside the title (ADR 0012).

    Args:
        note: The Book Note to inspect.

    Returns:
        The repaired title and a description of what was repaired, or
        (None, None) when the title needs no repair.
    """
    title = note.title
    author = note.first_author
    stem = note.path.stem

    if title in LEAKED_HEADINGS:
        recovered = stem
        if author and stem.endswith(f" - {author}"):
            recovered = stem[: -len(f" - {author}")]
        return recovered, f'title was "{title}"; recovered from filename'

    if title and author and title.endswith(f" - {author}"):
        return title[: -len(f" - {author}")].rstrip(), "removed author from title"

    return None, None


def leaked_alias_keys(blocks: list[tuple[str, str]]) -> list[str]:
    """Find alias blocks whose every value came from a misread heading.

    A block is only reported when all of its values leaked, so an alias the
    reader added by hand is never discarded.

    Args:
        blocks: Pairs from `split_frontmatter_blocks`.

    Returns:
        The keys of blocks safe to drop.
    """
    leaked: list[str] = []
    for key, raw in blocks:
        if key not in ("aliases", "linter-yaml-title-alias"):
            continue
        values = [
            part.strip().strip("-").strip().strip("\"'")
            for part in raw.split(":", 1)[1].splitlines()
        ]
        values = [v for v in values if v]
        if values and all(v in LEAKED_HEADINGS for v in values):
            leaked.append(key)
    return leaked


def plan_note_migration(path: Path) -> NoteMigration:
    """Work out what the migration would do to one Book Note, without writing.

    Args:
        path: The Book Note to plan for.

    Returns:
        The planned rewrite, the changes it represents, and anything about the
        note that wants a human eye before it is written.
    """
    original = path.read_text(encoding="utf-8")
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", original, re.DOTALL)
    if match is None:
        return NoteMigration(
            path=path,
            original=original,
            migrated=original,
            warnings=["no parseable frontmatter; skipped"],
        )

    frontmatter_text, body = match.group(1), match.group(2)
    note = BookNote.read(path)
    if note is None:
        return NoteMigration(
            path=path,
            original=original,
            migrated=original,
            warnings=["frontmatter is not a mapping; skipped"],
        )

    changes: list[str] = []
    warnings: list[str] = []
    blocks = split_frontmatter_blocks(frontmatter_text)

    repaired_title, repair_note = recover_title(note)
    title = repaired_title or note.title
    if repaired_title is not None:
        blocks = [
            (key, _render_scalar("title", repaired_title) if key == "title" else raw)
            for key, raw in blocks
        ]
        changes.append(repair_note)

    for key in leaked_alias_keys(blocks):
        blocks = [(k, raw) for k, raw in blocks if k != key]
        changes.append(f"dropped leaked {key}")

    if note.libris_id is None:
        blocks.append(
            (
                "libris_id",
                _render_scalar(
                    "libris_id", mint_libris_id(note.frontmatter.get("date_added"))
                ),
            )
        )
        changes.append("minted libris_id")

    existing_keys = {key for key, _ in blocks}
    for key in MODELLED_FIELDS:
        if key not in existing_keys:
            changes.append(f"added {key}")

    ordered = reorder_frontmatter(blocks)
    if [k for k, _ in ordered] != [k for k, _ in blocks]:
        changes.append("regrouped frontmatter")

    candidate_titles = tuple(value for value in (note.title, repaired_title) if value)
    prose, description = split_body(body, candidate_titles)
    if title is None:
        warnings.append("no title; body left alone")
        new_body = body
    else:
        new_body = render_body(title, prose, description)
        if not has_title_heading(body):
            changes.append("added H1")
        if description and not has_description_callout(body):
            changes.append("description moved into a callout")

    if description and re.search(r"^#{1,6}\s", description, re.MULTILINE):
        warnings.append("heading found inside the description; check the split")

    migrated = "---\n" + "\n".join(raw for _, raw in ordered) + "\n---\n\n" + new_body
    return NoteMigration(
        path=path,
        original=original,
        migrated=migrated,
        changes=changes,
        warnings=warnings,
    )


def plan_migration(vault_path: Path) -> list[NoteMigration]:
    """Plan the migration for every Book Note on the Shelf.

    Args:
        vault_path: The Shelf to migrate.

    Returns:
        One plan per note, including notes that need no change.
    """
    return [plan_note_migration(path) for path in list_books(vault_path)]


def _render_formats(formats: list[str]) -> str:
    """Render a format value the way the vault already writes lists."""
    if not formats:
        return "format:"
    # Two-space indent, which is how every list in this vault is already
    # written - anything else rewrites 873 notes to say the same thing.
    lines = ["format:"]
    lines.extend(f"  - {name}" for name in formats)
    return "\n".join(lines)


def plan_note_format_migration(path: Path) -> NoteMigration:
    """Plan the format rewrite for one Book Note (ADR 0017).

    Touches `format` and nothing else. Running the whole `cleanup` pass would
    also restandardise titles - measured at 41 notes on this Shelf, seven of
    them lowercasing a word that should not be - and a format migration is no
    place to decide that.

    Args:
        path: The Book Note to plan.

    Returns:
        The plan, unchanged when the note's format is already canonical.
    """
    # Read through universal newlines and write plain ones, exactly as
    # plan_note_migration does. The Shelf is stored CRLF, and the platform adds
    # that back on write - emitting it here too produced \r\r\n on 1,345 notes.
    original = path.read_text(encoding="utf-8")

    match = re.match(r"^---\n(.*?)\n---\n(.*)$", original, re.DOTALL)
    if match is None:
        return NoteMigration(
            path=path,
            original=original,
            migrated=original,
            warnings=["no parseable frontmatter"],
        )

    frontmatter_text, body = match.group(1), match.group(2)
    blocks = split_frontmatter_blocks(frontmatter_text)
    if not any(key == "format" for key, _ in blocks):
        return NoteMigration(path=path, original=original, migrated=original)

    current = yaml.safe_load(frontmatter_text) or {}
    formats = read_formats(current.get("format"))

    unknown = [name for name in formats if name not in FORMAT_VALUES]
    if unknown:
        # Guessing what an unknown format meant is exactly the silent wrongness
        # ADR 0003 refuses, so it is reported and left alone.
        return NoteMigration(
            path=path,
            original=original,
            migrated=original,
            warnings=[f"format not in the vocabulary: {', '.join(unknown)}"],
        )

    rendered = _render_formats(formats)
    rebuilt = [(key, rendered if key == "format" else raw) for key, raw in blocks]
    new_frontmatter = "\n".join(raw for _, raw in rebuilt)
    migrated = f"---\n{new_frontmatter}\n---\n{body}"

    changes = ["format"] if migrated != original else []
    return NoteMigration(
        path=path, original=original, migrated=migrated, changes=changes
    )


def plan_format_migration(vault_path: Path) -> list[NoteMigration]:
    """Plan the format rewrite for every Book Note on the Shelf.

    Args:
        vault_path: The Shelf to migrate.

    Returns:
        One plan per note, including notes that need no change.
    """
    return [plan_note_format_migration(path) for path in list_books(vault_path)]


def apply_migration(plans: list[NoteMigration]) -> int:
    """Write the planned rewrites to disk.

    Args:
        plans: Plans from `plan_migration`.

    Returns:
        How many notes were rewritten.
    """
    written = 0
    for plan in plans:
        if plan.changed:
            plan.path.write_text(plan.migrated, encoding="utf-8")
            written += 1
    return written
