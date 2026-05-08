import yaml
import os
import re
from dataclasses import dataclass
from pathlib import Path
from datetime import date
from typing import Dict, Any, Literal, Optional
from titlecase import titlecase
from .api import Book

DEFAULT_FRONTMATTER = {
    "title": None,
    "author": None,
    "isbn": None,
    "page_count": None,
    "published_date": None,
    "google_books_id": None,
    "thumbnail": None,
    "genres": None,
    "tags": "Book",
    "format": None,
    "status": "To Read",
    "rating": None,
    "referred_by": None,
    "date_added": None,
    "date_started": None,
    "date_finished": None,
}

# Maps legacy/extraneous field names to their canonical counterparts.
FIELD_MIGRATIONS = {
    "Type Read": "format",
    "Rating out of 5": "rating",
    "Referred From": "referred_by",
    "Date Read": "date_finished",
    "Date Added": "date_added",
    "Status": "status",
    "Author": "author",
}

def sanitize_filename(name: str) -> str:
    """Removes invalid characters for a filename and collapses whitespace."""
    name = re.sub(r'[\\/*?:"<>|]', "", name)
    name = re.sub(r'\s+', ' ', name)
    return name.strip()


# Patterns for publisher/format annotations (not genuine title content)
_BRACKET_ANNOTATION_PAT = re.compile(r'\s*[\[\{][^\[\]\{\}]*[\]\}]')
_WHITESPACE_PAT = re.compile(r'\s+')


def standardize_title(raw: Optional[str]) -> Optional[str]:
    """Standardize a book title: strip annotations, normalize whitespace, apply title case."""
    if not raw or not isinstance(raw, str):
        return raw
    title = raw.strip()
    # Remove bracket annotations like [Illustrated], {Kindle Edition}
    title = _BRACKET_ANNOTATION_PAT.sub('', title)
    # Collapse multiple whitespace to single space
    title = _WHITESPACE_PAT.sub(' ', title).strip()
    # Apply title case (NYT Manual of Style rules)
    title = titlecase(title)
    return title

def create_book_note(book: Book, vault_path: Path, status: str = "To Read") -> Path:
    """Creates a Markdown note for a book in the specified vault path."""
    filename = sanitize_filename(f"{book.title} - {', '.join(book.authors[:1])}.md")
    file_path = vault_path / filename
    
    frontmatter = {
        **DEFAULT_FRONTMATTER,
        "title": book.title,
        "author": book.authors,
        "isbn": book.isbn,
        "page_count": book.page_count,
        "published_date": book.published_date,
        "google_books_id": book.google_books_id,
        "thumbnail": book.thumbnail,
        "genres": book.genres,
        "status": status,
        "date_added": date.today().isoformat(),
    }
    
    yaml_content = yaml.dump(frontmatter, sort_keys=False, allow_unicode=True)
    
    content = f"---\n{yaml_content}---\n\n## Notes\n\n"
    if book.description:
        content += f"### Description\n{book.description}\n"
    
    file_path.write_text(content, encoding="utf-8")
    return file_path

def update_book_status(file_path: Path, new_status: str):
    """Updates the status in the book's frontmatter."""
    content = file_path.read_text(encoding="utf-8")
    
    # Simple regex to find and replace status
    # This is safer than full YAML re-dumping if there are complex structures or custom user notes
    pattern = r"(status:\s*)(.*)"
    new_content = re.sub(pattern, f"\\1{new_status}", content)
    
    file_path.write_text(new_content, encoding="utf-8")

def list_books(vault_path: Path):
    """Lists all markdown files in the vault, assuming each is a book note."""
    return [
        Path(entry.path)
        for entry in os.scandir(vault_path)
        if entry.is_file() and entry.name.endswith(".md")
    ]

def ensure_frontmatter_fields(file_path: Path) -> tuple[bool, Optional[Dict[str, Any]]]:
    """Ensures that all current fields exist in the note's frontmatter.

    Returns a tuple of (updated, frontmatter_dict). The dict is the cleaned
    frontmatter data (whether or not it was written back), or None if the
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

    # If date_finished is set, status should be "Read"
    if data.get("date_finished") is not None and data.get("status") != "Read":
        data["status"] = "Read"
        updated = True

    # Ensure author is always a list
    if isinstance(data.get("author"), str):
        data["author"] = [data["author"]]
        updated = True

    # Standardize title casing and strip annotations
    title_val = data.get("title")
    if title_val and isinstance(title_val, str):
        standardized = standardize_title(title_val)
        if standardized != title_val:
            data["title"] = standardized
            updated = True
            
    if updated:
        # Use dump but ensure we don't add unnecessary trailing newlines or spaces
        new_frontmatter = yaml.dump(data, sort_keys=False, allow_unicode=True).strip()
        # Ensure there is exactly one newline before and after the rest of the content
        new_content = f"---\n{new_frontmatter}\n---\n{rest_of_content.lstrip()}"
        file_path.write_text(new_content, encoding="utf-8")
    
    return updated, data

# Maps Book dataclass fields to frontmatter field names.
_BOOK_TO_FRONTMATTER = {
    "title": "title",
    "authors": "author",
    "isbn": "isbn",
    "page_count": "page_count",
    "published_date": "published_date",
    "google_books_id": "google_books_id",
    "thumbnail": "thumbnail",
    "genres": "genres",
    "description": None,  # handled separately (body, not frontmatter)
}


def find_duplicates(vault_path: Path) -> list[list[Path]]:
    """Find groups of duplicate book notes by title, ISBN, or Google Books ID.

    Returns a list of groups where each group contains two or more paths
    that share at least one matching identifier.
    """
    books = list_books(vault_path)
    file_data: list[tuple[Path, Dict[str, Any]]] = []
    for p in books:
        fm = read_frontmatter(p)
        if fm is not None:
            file_data.append((p, fm))

    def _author_key(fm: Dict[str, Any]) -> tuple[str, ...]:
        author = fm.get("author")
        if isinstance(author, list):
            return tuple(sorted(a.strip().lower() for a in author if isinstance(a, str)))
        elif isinstance(author, str):
            return (author.strip().lower(),)
        return ()

    # Build groups keyed by each identifier type.
    # key -> set of indices into file_data
    groups_by_key: Dict[str, set[int]] = {}
    # Title groups need pairwise author comparison (missing author = wildcard)
    title_groups: Dict[str, list[int]] = {}

    for idx, (path, fm) in enumerate(file_data):
        title = fm.get("title")
        if title and isinstance(title, str):
            title_groups.setdefault(title.strip().lower(), []).append(idx)

        isbn = fm.get("isbn")
        if isbn:
            key = f"isbn:{str(isbn).strip()}"
            groups_by_key.setdefault(key, set()).add(idx)

        gid = fm.get("google_books_id")
        if gid:
            key = f"gid:{str(gid).strip()}"
            groups_by_key.setdefault(key, set()).add(idx)

    # Union-find to merge overlapping groups
    parent = list(range(len(file_data)))

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
        for i, idx_a in enumerate(members):
            ak = _author_key(file_data[idx_a][1])
            for idx_b in members[i + 1 :]:
                bk = _author_key(file_data[idx_b][1])
                if not ak or not bk or ak == bk:
                    union(idx_a, idx_b)

    for members in groups_by_key.values():
        if len(members) < 2:
            continue
        it = iter(members)
        first = next(it)
        for other in it:
            union(first, other)

    # Collect final groups with 2+ members
    clusters: Dict[int, list[Path]] = {}
    for idx, (path, _) in enumerate(file_data):
        root = find(idx)
        clusters.setdefault(root, []).append(path)

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


def update_frontmatter_from_book(file_path: Path, book: Book) -> bool:
    """Fill null frontmatter fields with data from a Book. Returns True if changed."""
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

    # Add description to body if missing
    if book.description and "### Description" not in rest_of_content:
        rest_of_content = rest_of_content.rstrip() + f"\n\n### Description\n{book.description}\n"
        updated = True

    if updated:
        new_frontmatter = yaml.dump(data, sort_keys=False, allow_unicode=True).strip()
        new_content = f"---\n{new_frontmatter}\n---\n{rest_of_content.lstrip()}"
        file_path.write_text(new_content, encoding="utf-8")
        return True

    return False


def compute_canonical_filename(file_path: Path) -> Optional[str]:
    """Compute the canonical 'Title - Author.md' filename from frontmatter."""
    fm = read_frontmatter(file_path)
    if not fm:
        return None
    title = fm.get("title")
    author = fm.get("author")
    if not title or not isinstance(title, str):
        return None

    if isinstance(author, list):
        author_candidates = author
    elif isinstance(author, str):
        author_candidates = [author]
    else:
        return None

    first_author = next(
        (candidate.strip() for candidate in author_candidates if isinstance(candidate, str) and candidate.strip()),
        None,
    )
    if first_author is None:
        return None

    return sanitize_filename(f"{title} - {first_author}.md")


def update_wikilinks_in_vault(vault_root: Path, old_stem: str, new_stem: str, exclude: Optional[Path] = None) -> int:
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


RenameStatus = Literal["renamed", "already_canonical", "missing_title", "missing_author", "invalid_frontmatter", "collision"]


@dataclass(frozen=True)
class RenameResult:
    """Result of a rename attempt with diagnostic information."""
    status: RenameStatus
    new_path: Optional[Path] = None
    detail: Optional[str] = None


def rename_book_file(file_path: Path, vault_root: Optional[Path] = None, frontmatter: Optional[Dict[str, Any]] = None) -> RenameResult:
    """Rename a book file to canonical format and update wikilinks.

    If frontmatter is provided, it is used directly instead of re-reading
    the file. This avoids redundant I/O when called after ensure_frontmatter_fields.
    """
    fm = frontmatter if frontmatter is not None else read_frontmatter(file_path)
    if not fm:
        return RenameResult(status="invalid_frontmatter")

    title = fm.get("title")
    if not isinstance(title, str) or not title.strip():
        return RenameResult(status="missing_title")

    author = fm.get("author")
    if isinstance(author, list):
        author_candidates = author
    elif isinstance(author, str):
        author_candidates = [author]
    else:
        return RenameResult(status="missing_author")

    first_author = next(
        (c.strip() for c in author_candidates if isinstance(c, str) and c.strip()),
        None,
    )
    if first_author is None:
        return RenameResult(status="missing_author")

    canonical_name = sanitize_filename(f"{title.strip()} - {first_author}.md")
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
