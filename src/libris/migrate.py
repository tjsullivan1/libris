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
from datetime import date, datetime, time, timezone
from pathlib import Path

from ulid import ULID

from .markdown import BookNote, list_books

# Frontmatter order, grouped so the Properties panel reads sensibly (ADR 0009).
IDENTITY_FIELDS = ("libris_id", "title", "authors")
BIBLIOGRAPHIC_FIELDS = (
    "isbn",
    "page_count",
    "date_published",
    "google_books_id",
    "cover_thumbnail",
    "genres",
    "series",
)
READING_FIELDS = ("status", "priority", "rating", "format", "tags", "referred_by")
DATE_FIELDS = ("date_added", "date_started", "date_finished")

MODELLED_FIELDS = IDENTITY_FIELDS + BIBLIOGRAPHIC_FIELDS + READING_FIELDS + DATE_FIELDS

# Headings the linter mistook for a title, and the values that leaked from them.
LEAKED_HEADINGS = ("Notes", "Description")

_KEY_LINE = re.compile(r"^([A-Za-z_][A-Za-z0-9_ \-]*):")
_DESCRIPTION_HEADING = re.compile(r"^#{1,6}[ \t]*Description[ \t]*$", re.MULTILINE)
_CALLOUT_HEADING = re.compile(
    r"^>[ \t]*\[!abstract\]-?[ \t]*Description[ \t]*$", re.MULTILINE
)
_NOTES_HEADING = re.compile(r"^#{1,6}[ \t]*Notes[ \t]*$", re.MULTILINE)
_H1 = re.compile(r"^#[ \t]+(.+?)[ \t]*$", re.MULTILINE)
# A section ends at the next heading or a thematic break. merge.py joins bodies
# with a "---" divider, and the reader's writing must not be swept into a blurb.
_SECTION_BREAK = re.compile(r"^(?:#{1,6}[ \t]+\S|-{3,}[ \t]*$)", re.MULTILINE)


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


def mint_libris_id(date_added: object) -> str:
    """Mint a Libris ID whose timestamp comes from when the book was added.

    ULIDs sort lexicographically by their timestamp, so deriving it from
    `date_added` makes the Library sort in the order it was acquired rather than
    the order the migration happened to visit it (ADR 0011).

    Args:
        date_added: The note's `date_added` value, as a date, a datetime, an
            ISO-8601 string, or anything unparseable.

    Returns:
        A 26-character ULID string.
    """
    moment: datetime | None = None
    if isinstance(date_added, datetime):
        moment = date_added
    elif isinstance(date_added, date):
        moment = datetime.combine(date_added, time.min)
    elif isinstance(date_added, str) and date_added.strip():
        try:
            moment = datetime.fromisoformat(date_added.strip())
        except ValueError:
            moment = None

    if moment is None:
        return str(ULID())
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return str(ULID.from_datetime(moment))


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


def render_description_callout(description: str) -> str:
    """Render a description as a collapsed Obsidian callout (ADR 0009).

    Args:
        description: The description text, without any callout markup.

    Returns:
        The callout block, with every line prefixed.
    """
    lines = ["> [!abstract]- Description"]
    for line in description.strip().splitlines():
        lines.append(f"> {line}" if line.strip() else ">")
    return "\n".join(lines)


def split_body(body: str, titles: tuple[str, ...] = ()) -> tuple[str, str | None]:
    """Separate the reader's own prose from the description supplied by an API.

    The description is the content under its own heading, ending at the next
    heading rather than at the end of the note — some notes carry the description
    above `## Notes` rather than below it, and running to the end would sweep the
    reader's own writing into the blurb.

    Understands the shapes this vault holds: with and without an H1, description
    under a heading or already inside a callout, in either order. Headings the
    reader added stay with their content in the prose.

    Args:
        body: Everything after the closing frontmatter fence.
        titles: Headings that count as the note's own title heading and may be
            consumed. Any other leading H1 belongs to the reader and is kept.

    Returns:
        The reader's prose, and the description if the note carries one.
    """
    description: str | None = None
    remainder = body

    callout = _CALLOUT_HEADING.search(body)
    heading = _DESCRIPTION_HEADING.search(body)

    if callout is not None:
        after = body[callout.end() :]
        quoted, rest = _take_callout(after)
        description = quoted
        remainder = body[: callout.start()] + rest
    elif heading is not None:
        after = body[heading.end() :]
        section_break = _SECTION_BREAK.search(after)
        end = section_break.start() if section_break else len(after)
        description = after[:end].strip()
        remainder = body[: heading.start()] + after[end:]

    prose = _strip_title_heading(remainder, titles)
    prose = _NOTES_HEADING.sub("", prose)
    return prose.strip(), (description or None)


def _strip_title_heading(text: str, titles: tuple[str, ...]) -> str:
    """Remove a leading H1 only when it is the note's title heading.

    A note may open with a heading the reader wrote - a contents list, or
    "Notes from GetAbstract" - and removing it would destroy their writing. Only
    a first-line H1 naming the title, or one of the headings the linter leaked
    into the title, is the migration's to consume.

    Args:
        text: The note content with any description already removed.
        titles: Headings that count as the note's own title heading.

    Returns:
        The text, with the title heading removed if it was there.
    """
    leading = text.lstrip("\n")
    match = _H1.match(leading)
    if match is None:
        return text

    heading = match.group(1).strip()
    if heading in LEAKED_HEADINGS or heading in titles:
        return leading[match.end() :]
    return text


def _take_callout(text: str) -> tuple[str, str]:
    """Take the quoted lines of a callout, returning its content and what follows.

    Args:
        text: The note content immediately after a callout's heading line.

    Returns:
        The callout's text with its quote markers stripped, and the remainder of
        the note.
    """
    quoted: list[str] = []
    lines = text.splitlines(keepends=True)
    consumed = 0
    for line in lines:
        stripped = line.rstrip("\n")
        if stripped.startswith(">"):
            quoted.append(stripped[2:] if stripped.startswith("> ") else stripped[1:])
            consumed += 1
        elif not stripped.strip() and not quoted:
            consumed += 1
        else:
            break
    return "\n".join(quoted).strip(), "".join(lines[consumed:])


def render_body(title: str, prose: str, description: str | None) -> str:
    """Render a Book Note body: title, then the reader's notes, then the blurb.

    Args:
        title: The book's title, which becomes the H1 the linter reads for an
            alias (ADR 0012).
        prose: Whatever the reader wrote.
        description: The description from an external source, if any.

    Returns:
        The body, ending in a single newline.
    """
    sections = [f"# {title}", "## Notes"]
    if prose:
        sections.append(prose)
    if description:
        sections.append(render_description_callout(description))
    return "\n\n".join(sections) + "\n"


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
        if _H1.search(body) is None:
            changes.append("added H1")
        if description and _CALLOUT_HEADING.search(body) is None:
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
