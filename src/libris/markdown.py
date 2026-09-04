"""Markdown file operations for book notes (frontmatter, creation, enrichment)."""

import os
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Dict, Literal, Optional

import yaml
from titlecase import titlecase

from .api import BookCandidate
from .matching import normalize_for_match
from .note_format import (
    MODELLED_FIELDS,
    SUPERSEDED_IDS_FIELD,
    has_description_callout,
    mint_libris_id,
    normalize_field_value,
    read_formats,
    read_superseded_ids,
    render_body,
    render_description_callout,
    validate_field_value,
)

# Values a Book Note starts with when nothing else supplies them.
_FRONTMATTER_DEFAULTS = {"tags": "Book", "status": "To Read"}

# Derived from the canonical field order so a note written today and a note
# migrated from years ago cannot disagree about which fields exist.
DEFAULT_FRONTMATTER = {
    name: _FRONTMATTER_DEFAULTS.get(name) for name in MODELLED_FIELDS
}

# Maps legacy/extraneous field names to their canonical counterparts.
FIELD_MIGRATIONS = {
    "Type Read": "format",
    "Rating out of 5": "rating",
    "Referred From": "referred_by",
    "Date Read": "date_finished",
    "Date Added": "date_added",
    "Status": "status",
    "Author": "authors",
    # Names this code wrote before ADR 0005 settled the canonical vocabulary.
    "author": "authors",
    "published_date": "date_published",
    "thumbnail": "cover_thumbnail",
}


def _normalize_author(name: str) -> str:
    """Reduce an author name as written to the name itself.

    Args:
        name: The value as it appears in frontmatter, possibly a wikilink.

    Returns:
        The plain name, with runs of whitespace collapsed.
    """
    unlinked = re.sub(r"^\[\[(.+?)\]\]$", r"\1", name.strip())
    if "|" in unlinked:
        unlinked = unlinked.split("|", 1)[1]
    return re.sub(r"\s+", " ", unlinked).strip()


def tidy_author(name: str) -> str:
    """Collapse whitespace in an author value, leaving a wikilink intact.

    Narrower than `_normalize_author` on purpose. That one is for deciding
    whether two spellings mean the same person; this is for deciding what to
    write, and unwrapping a wikilink would delete the edge to an author's note
    (ADR 0018).

    Args:
        name: The value as it appears in frontmatter.

    Returns:
        The value with runs of whitespace collapsed.
    """
    return re.sub(r"\s+", " ", name).strip()


@dataclass
class BookNote:
    """A book on the Shelf: the file it lives in, its frontmatter, and its body.

    Frontmatter arrives from YAML and may hold any shape, so the accessors
    normalise rather than trusting what is on disk. Callers should read fields
    through them instead of reaching into the dict, which is how this code came
    to read a key that no note has ever carried.
    """

    path: Path
    frontmatter: dict[str, Any]
    body: str = ""

    @classmethod
    def read(cls, path: Path) -> "BookNote | None":
        """Read a Book Note from disk.

        Args:
            path: Path to the Markdown file.

        Returns:
            The Book Note, or None if the file has no parseable frontmatter.
        """
        frontmatter = read_frontmatter(path)
        if frontmatter is None:
            return None
        return cls(path=path, frontmatter=frontmatter)

    @property
    def libris_id(self) -> str | None:
        """The note's stable identity, or None until the vault is migrated."""
        value = self.frontmatter.get("libris_id")
        return value.strip() if isinstance(value, str) and value.strip() else None

    @property
    def superseded_ids(self) -> list[str]:
        """Identities of Book Notes merged into this one (ADR 0014).

        Returns:
            The superseded identities, or an empty list. A note that has never
            absorbed another does not carry the field at all.
        """
        return read_superseded_ids(self.frontmatter.get(SUPERSEDED_IDS_FIELD))

    @property
    def title(self) -> str | None:
        """The book's title, or None when absent or blank."""
        value = self.frontmatter.get("title")
        return value.strip() if isinstance(value, str) and value.strip() else None

    @property
    def authors(self) -> list[str]:
        """The book's authors as a list of plain, non-empty names.

        Accepts the list every note in the vault carries and the bare string that
        older notes used. Anything else counts as naming no author at all.

        Names are normalised for use rather than taken literally: some notes hold
        an author as a wikilink to their own note, and some carry stray inner
        whitespace. Both would otherwise leak into filenames and defeat matching,
        while the note itself keeps whatever it holds.
        """
        value = self.frontmatter.get("authors")
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, list):
            return []
        names = (_normalize_author(name) for name in value if isinstance(name, str))
        return [name for name in names if name]

    @property
    def first_author(self) -> str | None:
        """The first named author, or None when the note names none."""
        authors = self.authors
        return authors[0] if authors else None

    @property
    def canonical_filename(self) -> str | None:
        """The 'Title - Author.md' filename this note should have.

        Returns:
            The filename, or None when the note lacks a title or an author.
        """
        if self.title is None or self.first_author is None:
            return None
        return sanitize_filename(f"{self.title} - {self.first_author}.md")


def sanitize_filename(name: str) -> str:
    """Removes invalid characters for a filename and collapses whitespace."""
    name = re.sub(r'[\\/*?:"<>|]', "", name)
    name = re.sub(r"\s+", " ", name)
    return name.strip()


# Patterns for publisher/format annotations (not genuine title content)
_BRACKET_ANNOTATION_PAT = re.compile(r"\s*[\[\{][^\[\]\{\}]*[\]\}]")
_WHITESPACE_PAT = re.compile(r"\s+")


def standardize_title(raw: Optional[str]) -> Optional[str]:
    """Standardize a book title: strip annotations, normalize whitespace, apply title case."""
    if not raw or not isinstance(raw, str):
        return raw
    title = raw.strip()
    # Remove bracket annotations like [Illustrated], {Kindle Edition}
    title = _BRACKET_ANNOTATION_PAT.sub("", title)
    # Collapse multiple whitespace to single space
    title = _WHITESPACE_PAT.sub(" ", title).strip()
    # Apply title case (NYT Manual of Style rules)
    title = titlecase(title)
    return title


def create_book_note(
    book: BookCandidate,
    vault_path: Path,
    status: str = "To Read",
    overrides: Dict[str, Any] | None = None,
) -> Path:
    """Creates a Markdown note for a book in the specified vault path.

    Args:
        book: The candidate whose metadata seeds the note.
        vault_path: Directory where the note will be written.
        status: Default reading status (overridden if 'status' is in overrides).
        overrides: Optional dict of frontmatter fields to set/override.
            Keys must exist in DEFAULT_FRONTMATTER.
    """
    filename = sanitize_filename(f"{book.title} - {', '.join(book.authors[:1])}.md")
    file_path = vault_path / filename

    if overrides and "status" in overrides:
        status = overrides["status"]
        overrides = {k: v for k, v in overrides.items() if k != "status"}

    validate_field_value("status", status)
    added = date.today()
    frontmatter = {
        **DEFAULT_FRONTMATTER,
        "libris_id": mint_libris_id(added),
        "title": book.title,
        "authors": book.authors,
        "isbn": book.isbn,
        "page_count": book.page_count,
        "date_published": book.published_date,
        "google_books_id": book.google_books_id,
        "cover_thumbnail": book.thumbnail,
        "genres": book.genres,
        "status": status,
        "date_added": added.isoformat(),
    }

    if overrides:
        for key, value in overrides.items():
            if key not in DEFAULT_FRONTMATTER:
                raise ValueError(
                    f"Unknown frontmatter field: '{key}'. "
                    f"Valid fields: {', '.join(DEFAULT_FRONTMATTER.keys())}"
                )
            value = normalize_field_value(key, value)
            validate_field_value(key, value)
            frontmatter[key] = value

    yaml_content = yaml.dump(frontmatter, sort_keys=False, allow_unicode=True)

    body = render_body(book.title, "", book.description)
    file_path.write_text(f"---\n{yaml_content}---\n\n{body}", encoding="utf-8")
    return file_path


def update_book_status(file_path: Path, new_status: str) -> None:
    """Set the status in a Book Note's frontmatter, leaving the body untouched.

    This used to be an unanchored, uncounted `re.sub` for `status:\\s*(.*)` over
    the whole file, on the reasoning that a regex disturbs a note less than a
    YAML round-trip does. The reasoning was right and the region was wrong: it
    rewrote every line containing `status:` anywhere in the file, including a
    reader's own sentences about the book (#92). `set_frontmatter_fields` keeps
    the intent - the body is carried across unchanged - and confines the
    edit to the frontmatter block, where the field actually lives.

    Args:
        file_path: The Book Note to write.
        new_status: The status to set, from the Library's own vocabulary.

    Raises:
        InvalidFieldValue: If the status is not one the Library defines.
        FrontmatterUnreadable: If the note has no frontmatter block to write to.
    """
    validate_field_value("status", new_status)
    set_frontmatter_fields(file_path, {"status": new_status})


class FrontmatterUnreadable(ValueError):
    """A Book Note's frontmatter could not be parsed, so it was not written to."""


def _split_frontmatter(content: str) -> Optional[tuple[str, str]]:
    """Split a note into its frontmatter block and everything after it.

    Deliberately not a regex. The body is returned exactly as it was found -
    every byte after the line closing the block, blank lines included - because
    an update to one field must not reflow a reader's own writing (ADR 0023).

    Args:
        content: The whole file.

    Returns:
        The YAML text and the body, or None if there is no closed frontmatter
        block at the start of the file.
    """
    lines = content.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return None
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            return "".join(lines[1:index]), "".join(lines[index + 1 :])
    return None


def set_frontmatter_fields(file_path: Path, updates: Dict[str, Any]) -> None:
    """Set named frontmatter fields on a Book Note, leaving the body untouched.

    The body is carried across exactly rather than re-rendered. Every other
    write path here reconstructs a note - `ensure_frontmatter_fields` re-dumps
    the YAML and reflows the body on every pass - which is right for a repair
    pass and wrong for setting a status.

    Field order is preserved for keys the note already carries; new keys are
    appended, so an update does not reshuffle a note.

    Args:
        file_path: The Book Note to write.
        updates: Field names and the values to set them to.

    Raises:
        FrontmatterUnreadable: If the file has no parseable frontmatter block.
            Refused rather than repaired: this is the write path for one field,
            not the place to rebuild a broken note.
    """
    content = file_path.read_text(encoding="utf-8")
    split = _split_frontmatter(content)
    if split is None:
        raise FrontmatterUnreadable(f"{file_path.name} has no readable frontmatter.")

    frontmatter_yaml, body = split
    try:
        data = yaml.safe_load(frontmatter_yaml)
    except yaml.YAMLError as exc:
        raise FrontmatterUnreadable(f"{file_path.name}: {exc}") from None
    if not isinstance(data, dict):
        raise FrontmatterUnreadable(f"{file_path.name} has no frontmatter mapping.")

    data.update(updates)
    rendered = yaml.dump(data, sort_keys=False, allow_unicode=True).strip()
    file_path.write_text("---\n" + rendered + "\n---\n" + body, encoding="utf-8")


def list_books(vault_path: Path):
    """Lists all markdown files in the vault, assuming each is a book note."""
    return [
        Path(entry.path)
        for entry in os.scandir(vault_path)
        if entry.is_file() and entry.name.endswith(".md")
    ]


def find_duplicate_candidates(vault_path: Path) -> list[list[BookNote]]:
    """Find pairs of Book Notes that may describe one Book.

    Matched by title containment rather than by a shared identifier, which is a
    judgement rather than a fact: measured against the real Shelf, containment
    conflates 83 pairs, of which roughly nine are different books - "Mercy" and
    "Long Road to Mercy", "Freakonomics" and "SuperFreakonomics". So these are
    offered to a person and never merged automatically (ADR 0018).

    Pairs that `find_duplicates` already reports are left out; they are settled,
    not candidates.

    Args:
        vault_path: The Shelf to search.

    Returns:
        Pairs of Book Notes, shorter title first, ordered by author then title.
    """
    notes: list[BookNote] = []
    for book_path in list_books(vault_path):
        note = BookNote.read(book_path)
        if note is not None and note.title and note.first_author:
            notes.append(note)

    settled = set()
    for group in find_duplicates(vault_path):
        for a in group:
            for b in group:
                if a != b:
                    settled.add(frozenset((str(a), str(b))))

    by_author: dict[str, list[BookNote]] = {}
    for note in notes:
        by_author.setdefault(normalize_for_match(note.first_author), []).append(note)

    seen = set()
    pairs: list[list[BookNote]] = []
    for group in by_author.values():
        for a in group:
            for b in group:
                if a.path == b.path:
                    continue
                title_a = normalize_for_match(a.title)
                title_b = normalize_for_match(b.title)
                if title_a == title_b or title_a not in title_b:
                    continue
                key = frozenset((str(a.path), str(b.path)))
                if key in seen or key in settled:
                    continue
                seen.add(key)
                pairs.append([a, b])

    pairs.sort(
        key=lambda pair: ((pair[0].first_author or "").lower(), pair[0].title or "")
    )
    return pairs


def ensure_frontmatter_fields(
    file_path: Path, dry_run: bool = False
) -> tuple[bool, Optional[Dict[str, Any]]]:
    """Ensures that all current fields exist in the note's frontmatter.

    Args:
        file_path: The Book Note to repair.
        dry_run: Report what would change without writing it. This pass is what
            migrates `format` across the Shelf (ADR 0017), so it can be
            previewed before it rewrites anything.

    Returns:
        A tuple of (updated, frontmatter_dict). The dict is the cleaned
        frontmatter data, whether or not it was written back, or None if the
        frontmatter could not be parsed.
    """
    content = file_path.read_text(encoding="utf-8")

    # Use a more robust regex to find the frontmatter block
    # Matches --- at start of file, then content, then --- on its own line
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", content, re.DOTALL)
    if not match:
        # Fallback for files that might not have a newline after the closing ---
        match = re.match(r"^---\s*\n(.*?)\n---(.*)$", content, re.DOTALL)
        if not match:
            return False, None

    frontmatter_yaml = match.group(1)
    rest_of_content = match.group(2)

    try:
        data = yaml.safe_load(frontmatter_yaml)
        if not isinstance(data, dict):
            return False, None
    except Exception:
        return False, None

    updated = False

    # Migrate legacy field names to canonical ones.
    for old_name, new_name in FIELD_MIGRATIONS.items():
        if old_name in data:
            if data.get(new_name) is None:
                data[new_name] = data[old_name]
            del data[old_name]
            updated = True

    for field, default in DEFAULT_FRONTMATTER.items():
        if field not in data:
            data[field] = default
            updated = True

    # A note that reached us without an identity gets one here, so a book typed
    # straight into Obsidian is not left unaddressable (ADR 0001, ADR 0011).
    if not data.get("libris_id"):
        data["libris_id"] = mint_libris_id(data.get("date_added"))
        updated = True

    # If date_finished is set, status should be "Read"
    if data.get("date_finished") is not None and data.get("status") != "Read":
        data["status"] = "Read"
        updated = True

    # Ensure authors is always a list
    if isinstance(data.get("authors"), str):
        data["authors"] = [data["authors"]]
        updated = True

    # Repair format's shape and case, the same way authors is repaired above.
    # Obsidian writes this field too and Libris cannot guard it there, so the
    # rule is applied on every pass rather than once in a migration (ADR 0017).
    if "format" in data:
        formats = read_formats(data["format"]) or None
        if formats != data["format"]:
            data["format"] = formats
            updated = True

    # Standardize title casing and strip annotations
    title_val = data.get("title")
    if title_val and isinstance(title_val, str):
        standardized = standardize_title(title_val)
        if standardized != title_val:
            data["title"] = standardized
            updated = True

    title = data.get("title")
    stripped_content = rest_of_content.lstrip()
    if (
        isinstance(title, str)
        and title
        and stripped_content.startswith("## Notes")
        and not re.search(r"(?m)^#\s+", stripped_content)
    ):
        rest_of_content = f"# {title}\n\n{rest_of_content.lstrip()}"
        updated = True

    if updated and not dry_run:
        # Use dump but ensure we don't add unnecessary trailing newlines or spaces
        new_frontmatter = yaml.dump(data, sort_keys=False, allow_unicode=True).strip()
        # Ensure there is exactly one newline before and after the rest of the content
        new_content = f"---\n{new_frontmatter}\n---\n{rest_of_content.lstrip()}"
        file_path.write_text(new_content, encoding="utf-8")

    return updated, data


# Maps BookCandidate fields to frontmatter field names.
_BOOK_TO_FRONTMATTER = {
    "title": "title",
    "authors": "authors",
    "isbn": "isbn",
    "page_count": "page_count",
    "published_date": "date_published",
    "google_books_id": "google_books_id",
    "thumbnail": "cover_thumbnail",
    "genres": "genres",
    "description": None,  # handled separately (body, not frontmatter)
}

EXCLUDED_GOOGLE_BOOKS_IDS = {
    "_not_found_in_google_books_api",
    "_not_a_book",
}


def find_duplicates(vault_path: Path) -> list[list[Path]]:
    """Find groups of duplicate book notes by title, ISBN, or Google Books ID.

    Returns a list of groups where each group contains two or more paths
    that share at least one matching identifier.
    """
    notes: list[BookNote] = []
    for book_path in list_books(vault_path):
        note = BookNote.read(book_path)
        if note is not None:
            notes.append(note)

    def _author_key(note: BookNote) -> tuple[str, ...]:
        return tuple(sorted(name.lower() for name in note.authors))

    # Build groups keyed by each identifier type.
    # key -> set of indices into file_data
    groups_by_key: Dict[str, set[int]] = {}
    # Title groups need pairwise author comparison (missing author = wildcard)
    title_groups: Dict[str, list[int]] = {}

    for idx, note in enumerate(notes):
        if note.title is not None:
            # Normalized rather than lowercased: punctuation is not meaning
            # here, and "Crucial Conversations- Tools" and "Crucial
            # Conversations: Tools" are one Book (#72).
            title_groups.setdefault(normalize_for_match(note.title), []).append(idx)

        isbn = note.frontmatter.get("isbn")
        if isbn:
            key = f"isbn:{str(isbn).strip()}"
            groups_by_key.setdefault(key, set()).add(idx)

        gid = note.frontmatter.get("google_books_id")
        if gid and gid not in EXCLUDED_GOOGLE_BOOKS_IDS:
            key = f"gid:{str(gid).strip()}"
            groups_by_key.setdefault(key, set()).add(idx)

    # Union-find to merge overlapping groups
    parent = list(range(len(notes)))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    # Title duplicates: same title + (same author OR either has no author)
    for members in title_groups.values():
        if len(members) < 2:
            continue

        author_buckets: Dict[str, list[int]] = {}
        has_missing_author = False

        for idx in members:
            author_key = _author_key(notes[idx])
            if author_key:
                author_buckets.setdefault(author_key, []).append(idx)
            else:
                has_missing_author = True

        if has_missing_author:
            first = members[0]
            for other in members[1:]:
                union(first, other)
        else:
            for bucket_members in author_buckets.values():
                if len(bucket_members) < 2:
                    continue
                first = bucket_members[0]
                for other in bucket_members[1:]:
                    union(first, other)

    for members in groups_by_key.values():
        if len(members) < 2:
            continue
        it = iter(members)
        first = next(it)
        for other in it:
            union(first, other)

    # Collect final groups with 2+ members
    clusters: Dict[int, list[Path]] = {}
    for idx, note in enumerate(notes):
        root = find(idx)
        clusters.setdefault(root, []).append(note.path)

    return [sorted(group) for group in clusters.values() if len(group) >= 2]


def read_frontmatter(file_path: Path) -> Optional[Dict[str, Any]]:
    """Read and return the frontmatter dict from a markdown file, or None."""
    content = file_path.read_text(encoding="utf-8")
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", content, re.DOTALL)
    if not match:
        match = re.match(r"^---\s*\n(.*?)\n---(.*)$", content, re.DOTALL)
        if not match:
            return None
    try:
        data = yaml.safe_load(match.group(1))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def update_frontmatter_from_book(file_path: Path, book: BookCandidate) -> bool:
    """Fill null frontmatter fields from a candidate. Returns True if changed."""
    content = file_path.read_text(encoding="utf-8")
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", content, re.DOTALL)
    if not match:
        match = re.match(r"^---\s*\n(.*?)\n---(.*)$", content, re.DOTALL)
        if not match:
            return False

    frontmatter_yaml = match.group(1)
    rest_of_content = match.group(2)

    try:
        data = yaml.safe_load(frontmatter_yaml)
        if not isinstance(data, dict):
            return False
    except Exception:
        return False

    updated = False
    for book_field, fm_field in _BOOK_TO_FRONTMATTER.items():
        if fm_field is None:
            continue
        value = getattr(book, book_field, None)
        if value is not None and data.get(fm_field) is None:
            data[fm_field] = value
            updated = True

    title = data.get("title")
    stripped_content = rest_of_content.lstrip()
    if (
        isinstance(title, str)
        and title
        and stripped_content.startswith("## Notes")
        and not re.search(r"(?m)^#\s+", stripped_content)
    ):
        rest_of_content = f"# {title}\n\n{rest_of_content.lstrip()}"
        updated = True

    # Add description to body if missing
    if book.description and not has_description_callout(rest_of_content):
        rest_of_content = (
            rest_of_content.rstrip()
            + "\n\n"
            + render_description_callout(book.description)
            + "\n"
        )
        updated = True

    if updated:
        new_frontmatter = yaml.dump(data, sort_keys=False, allow_unicode=True).strip()
        new_content = f"---\n{new_frontmatter}\n---\n{rest_of_content.lstrip()}"
        file_path.write_text(new_content, encoding="utf-8")
        return True

    return False


def compute_canonical_filename(file_path: Path) -> Optional[str]:
    """Compute the canonical 'Title - Author.md' filename from frontmatter."""
    note = BookNote.read(file_path)
    return note.canonical_filename if note is not None else None


def update_wikilinks_in_vault(
    vault_root: Path, old_stem: str, new_stem: str, exclude: Optional[Path] = None
) -> int:
    """Update all wikilinks from old_stem to new_stem across the vault. Returns count of files updated."""
    updated_count = 0
    exclude_resolved = exclude.resolve() if exclude else None
    for root, dirnames, filenames in os.walk(vault_root):
        # Skip hidden directories (.obsidian, .git, etc.) before descending into them.
        dirnames[:] = [dirname for dirname in dirnames if not dirname.startswith(".")]
        root_path = Path(root)
        for filename in filenames:
            if not filename.endswith(".md") or filename.startswith("."):
                continue
            md_file = root_path / filename
            if exclude_resolved and md_file.resolve() == exclude_resolved:
                continue
            content = md_file.read_text(encoding="utf-8")
            new_content = content.replace(f"[[{old_stem}]]", f"[[{new_stem}]]")
            new_content = new_content.replace(f"[[{old_stem}|", f"[[{new_stem}|")
            new_content = new_content.replace(f"[[{old_stem}#", f"[[{new_stem}#")
            new_content = new_content.replace(f"[[{old_stem}^", f"[[{new_stem}^")
            if new_content != content:
                md_file.write_text(new_content, encoding="utf-8")
                updated_count += 1
    return updated_count


RenameStatus = Literal[
    "renamed",
    "already_canonical",
    "missing_title",
    "missing_author",
    "invalid_frontmatter",
    "collision",
]


@dataclass(frozen=True)
class RenameResult:
    """Result of a rename attempt with diagnostic information."""

    status: RenameStatus
    new_path: Optional[Path] = None
    detail: Optional[str] = None


def rename_book_file(
    file_path: Path,
    vault_root: Optional[Path] = None,
    frontmatter: Optional[Dict[str, Any]] = None,
) -> RenameResult:
    """Rename a book file to canonical format and update wikilinks.

    If frontmatter is provided, it is used directly instead of re-reading
    the file. This avoids redundant I/O when called after ensure_frontmatter_fields.
    """
    note = (
        BookNote(path=file_path, frontmatter=frontmatter)
        if frontmatter is not None
        else BookNote.read(file_path)
    )
    if note is None or not note.frontmatter:
        return RenameResult(status="invalid_frontmatter")

    if note.title is None:
        return RenameResult(status="missing_title")

    if note.first_author is None:
        return RenameResult(status="missing_author")

    canonical_name = note.canonical_filename
    if canonical_name == file_path.name:
        return RenameResult(status="already_canonical")

    new_path = file_path.parent / canonical_name
    if new_path.exists():
        return RenameResult(status="collision", detail=canonical_name)

    search_root = vault_root or file_path.parent
    old_stem = file_path.stem
    new_stem = new_path.stem

    # Perform the rename first so wikilinks are only updated if it succeeds.
    file_path.rename(new_path)

    # Update wikilinks across the vault
    update_wikilinks_in_vault(search_root, old_stem, new_stem, exclude=new_path)
    return RenameResult(status="renamed", new_path=new_path)
