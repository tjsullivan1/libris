"""The canonical shape of a Book Note.

One definition of what a Book Note looks like: which fields it carries and in
what order, and how its body is written. Both note creation and the one-time
migration render through here, so a note written today and a note migrated from
2019 come out the same. The drift this project spent a refactor undoing began
with two places disagreeing about a field name.

See ADR 0005, ADR 0009, ADR 0011 and ADR 0012.
"""

import re
from datetime import date, datetime, time, timezone

from ulid import ULID

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

# The values these fields may hold, taken from the vault rather than from what
# the code used to assume (ADR 0005). Measured across all 3,136 notes: status
# and priority already held nothing else. `format` held eleven shapes across
# two types and joined them once ADR 0017 settled what it is.
STATUS_VALUES = ("To Read", "Reading", "Read", "Not To Read")
PRIORITY_VALUES = ("Low", "Medium", "High")
FORMAT_VALUES = ("Physical", "Ebook", "Audiobook")
FIELD_VOCABULARIES = {
    "status": STATUS_VALUES,
    "priority": PRIORITY_VALUES,
    "format": FORMAT_VALUES,
}

# Fields that hold several values at once. Everything else is a scalar, so a
# list arriving for one of them is the wrong shape rather than a set of values
# to check one by one - `status: [Read]` must be refused, not accepted - and a
# merge unions these rather than reporting a conflict and keeping one side.
# `aliases` is Obsidian's, not modelled here, but it genuinely holds several
# values and a merge should combine them rather than keep one side's.
MULTI_VALUED_FIELDS = frozenset({"authors", "genres", "tags", "format", "aliases"})
DATE_FIELDS = ("date_added", "date_started", "date_finished")

# The identities of Book Notes merged into this one (ADR 0014). Deliberately
# NOT part of MODELLED_FIELDS: it is absent on a note that has never absorbed
# another, which is all but a handful, and adding it to the canonical shape
# would write `superseded_ids: null` into every note in the Shelf to say
# nothing. Both `ensure_frontmatter_fields` and the migration preserve keys
# they do not model, so a note that carries it keeps it.
SUPERSEDED_IDS_FIELD = "superseded_ids"

MODELLED_FIELDS = IDENTITY_FIELDS + BIBLIOGRAPHIC_FIELDS + READING_FIELDS + DATE_FIELDS

# Headings the linter mistook for a title, and the values that leaked from them.
LEAKED_HEADINGS = ("Notes", "Description")

_DESCRIPTION_HEADING = re.compile(r"^#{1,6}[ \t]*Description[ \t]*$", re.MULTILINE)
_CALLOUT_HEADING = re.compile(
    r"^>[ \t]*\[!abstract\]-?[ \t]*Description[ \t]*$", re.MULTILINE
)
_NOTES_HEADING = re.compile(r"^#{1,6}[ \t]*Notes[ \t]*$", re.MULTILINE)
_H1 = re.compile(r"^#[ \t]+(.+?)[ \t]*$", re.MULTILINE)
# A section ends at the next heading or a thematic break. merge.py joins bodies
# with a "---" divider, and the reader's writing must not be swept into a blurb.
_SECTION_BREAK = re.compile(r"^(?:#{1,6}[ \t]+\S|-{3,}[ \t]*$)", re.MULTILINE)


class InvalidFieldValue(ValueError):
    """A Book Note field was given a value the Library does not define.

    Subclasses ValueError so callers that already guard against bad input keep
    working; the distinct type is what lets the CLI report it as a user error
    rather than a crash.
    """


def validate_field_value(field: str, value: object) -> None:
    """Check a field value against the vocabulary the Library defines for it.

    Fields with no vocabulary, and values that are simply unset, always pass:
    absent is not the same as invalid, and 2,251 notes have no priority.

    Args:
        field: The frontmatter field being written.
        value: The value proposed for it.

    Raises:
        InvalidFieldValue: If the field has a vocabulary and the value is not
            in it.
    """
    allowed = FIELD_VOCABULARIES.get(field)
    if allowed is None or value is None:
        return

    if isinstance(value, list):
        if field not in MULTI_VALUED_FIELDS:
            raise InvalidFieldValue(
                f"Invalid {field}: {value!r} is a list, but {field} holds a "
                f"single value. Valid values: {', '.join(allowed)}."
            )
        # A multi-valued field is checked entry by entry, so the message names
        # the value that is wrong rather than the whole list.
        proposed = list(value)
    else:
        proposed = [value]

    for item in proposed:
        if item not in allowed:
            raise InvalidFieldValue(
                f"Invalid {field}: {item!r}. Valid values: {', '.join(allowed)}."
            )


def read_superseded_ids(value: object) -> list[str]:
    """Read a `superseded_ids` value into the list of identities it means.

    Frontmatter arrives from YAML and may hold any shape. This vault writes
    list-shaped fields as bare strings often enough to matter - 1,341 notes do
    it with `format` - and a bare string here is especially dangerous, because
    iterating one yields characters that each look like an identity.

    Args:
        value: Whatever the frontmatter held.

    Returns:
        The identities, with blanks dropped. Anything that is neither a string
        nor a list names no identity at all.
    """
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def read_formats(value: object) -> list[str]:
    """Read a `format` value into the list of media it means (ADR 0017).

    The Shelf holds this field in two shapes, written by two different tools:
    Libris wrote a bare string, Obsidian's property editor writes a list. Both
    read the same way here, and case is repaired, because "audiobook" names a
    real format and refusing it would only punish the reader for the help text
    Libris used to print.

    Args:
        value: Whatever the frontmatter or a caller held.

    Returns:
        The formats named, title-cased and without blanks. Anything that is
        neither a string nor a list names no format at all.
    """
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []

    formats: list[str] = []
    for item in value:
        if not isinstance(item, str):
            continue
        name = item.strip().title()
        if name and name not in formats:
            formats.append(name)
    return formats


def normalize_field_value(field: str, value: object) -> object:
    """Put a value into the shape the Library stores it in.

    Shape and case are repaired; meaning is not guessed at. A value that names
    nothing the Library defines is left alone for validation to refuse.

    Args:
        field: The frontmatter field being written.
        value: The value proposed for it.

    Returns:
        The normalized value, or the original for fields with no shape rule.
    """
    if field != "format":
        return value
    formats = read_formats(value)
    return formats or None


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


def has_title_heading(body: str) -> bool:
    """Whether a note body already opens with a heading.

    Args:
        body: Everything after the closing frontmatter fence.

    Returns:
        True when an H1 is present anywhere in the body.
    """
    return _H1.search(body) is not None


def has_description_callout(body: str) -> bool:
    """Whether a note's description already sits in a collapsed callout.

    Args:
        body: Everything after the closing frontmatter fence.

    Returns:
        True when the callout is present.
    """
    return _CALLOUT_HEADING.search(body) is not None
