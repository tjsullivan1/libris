import re
import time
from pathlib import Path

import questionary
import typer

from .api import GoogleBooksClient
from .config import (
    get_obsidian_vault_root,
    get_vault_path,
    set_book_vault_path,
    set_config,
)
from .importer import SUPPORTED_FORMATS, normalize_for_match, run_import
from .markdown import (
    BookNote,
    RenameResult,
    create_book_note,
    ensure_frontmatter_fields,
    find_duplicates,
    list_books,
    read_frontmatter,
    rename_book_file,
    update_book_status,
    update_frontmatter_from_book,
)
from .merge import (
    check_auto_merge,
    delete_secondary_file,
    get_primary_book,
    merge_two_books,
    write_merged_book,
)
from .migrate import apply_migration, plan_migration

app = typer.Typer()

_RENAME_SKIP_MESSAGES = {
    "missing_title": "missing title in frontmatter",
    "missing_author": "missing author in frontmatter",
    "invalid_frontmatter": "invalid or missing frontmatter",
    "already_canonical": None,  # not an error
}


def _format_rename_skip(filename: str, result: RenameResult) -> str | None:
    """Return a user-facing message for a skipped rename, or None if already canonical."""
    if result.status == "collision":
        return f"Skipped rename for {filename}: target already exists: {result.detail}"
    msg = _RENAME_SKIP_MESSAGES.get(result.status)
    if msg is None:
        return None
    return f"Skipped rename for {filename}: {msg}"


@app.command()
def status():
    """Update the status of a book in your vault."""
    vault_path = get_vault_path()
    books = list_books(vault_path)

    if not books:
        typer.echo("No books found in vault.")
        return

    choices = [p.name for p in books]
    selected_file_name = questionary.select(
        "Select a book to update:", choices=choices
    ).ask()

    if not selected_file_name:
        return

    selected_file = vault_path / selected_file_name

    new_status = questionary.select(
        "New status:", choices=["To Read", "Reading", "Finished"]
    ).ask()

    if not new_status:
        return

    update_book_status(selected_file, new_status)
    typer.echo(f"Updated: {selected_file_name} -> {new_status}")


@app.command(name="list")
def list_cmd(
    timing: bool = typer.Option(
        False, "--timing", help="Print scan timing for list operation"
    ),
):
    """List all books in your vault."""
    vault_path = get_vault_path()
    start = time.perf_counter() if timing else None
    books = list_books(vault_path)
    elapsed = (time.perf_counter() - start) if timing and start is not None else None

    if not books:
        typer.echo("No books found in vault.")
        return

    for p in books:
        # Only read the first 1KB - enough for status extraction from frontmatter
        try:
            with open(p, "r", encoding="utf-8") as f:
                head = f.read(1024)
            status_match = re.search(r"status:\s*(.*)", head)
            status = status_match.group(1).strip() if status_match else "Unknown"
        except Exception:
            status = "Error"
        typer.echo(f"- {p.name} [{status}]")

    if elapsed is not None:
        typer.echo(f"Scan time: {elapsed * 1000:.2f} ms")


@app.command()
def search(
    query: str = typer.Argument(..., help="Search term"),
    author: bool = typer.Option(False, "--author", "-a", help="Search by author"),
    title: bool = typer.Option(False, "--title", "-t", help="Search by title"),
    isbn: bool = typer.Option(False, "--isbn", "-i", help="Search by ISBN"),
):
    """Search for books by author, title, or ISBN and display results."""
    if author:
        search_query = f"inauthor:{query}"
    elif title:
        search_query = f"intitle:{query}"
    elif isbn:
        search_query = f"isbn:{query}"
    else:
        search_query = query

    client = GoogleBooksClient()
    books = client.search(search_query)

    if not books:
        typer.echo("No books found.")
        return

    typer.echo(f"Found {len(books)} result(s):\n")
    for book in books:
        authors_str = ", ".join(book.authors) if book.authors else "Unknown"
        typer.echo(f"  Title:     {book.title}")
        typer.echo(f"  Author(s): {authors_str}")
        if book.isbn:
            typer.echo(f"  ISBN:      {book.isbn}")
        if book.published_date:
            typer.echo(f"  Published: {book.published_date}")
        if book.page_count:
            typer.echo(f"  Pages:     {book.page_count}")
        typer.echo()


@app.command()
def add(
    query: str = typer.Argument(..., help="Title, author, or ISBN to search for"),
    status: str = typer.Option("To Read", "--status", "-s", help="Reading status"),
    medium: str | None = typer.Option(
        None,
        "--format",
        "-f",
        help="Reading format (e.g., paperback, kindle, audiobook)",
    ),
    rating: int | None = typer.Option(
        None, "--rating", "-r", help="Rating (1-5)", min=1, max=5
    ),
    referred_by: str | None = typer.Option(
        None, "--referred-by", help="Who recommended this book"
    ),
    tags: str | None = typer.Option(None, "--tags", help="Tags (default: Book)"),
    date_started: str | None = typer.Option(
        None, "--date-started", help="Date started reading (YYYY-MM-DD)"
    ),
    date_finished: str | None = typer.Option(
        None, "--date-finished", help="Date finished reading (YYYY-MM-DD)"
    ),
):
    """Search for a book and add it to your Obsidian vault."""
    client = GoogleBooksClient()
    books = client.search(query)

    if not books:
        typer.echo("No books found.")
        return

    choices = [f"{book.title} by {', '.join(book.authors)}" for book in books]
    selected_choice = questionary.select("Select a book to add:", choices=choices).ask()

    if not selected_choice:
        return

    book_index = choices.index(selected_choice)
    selected_book = books[book_index]

    vault_path = get_vault_path()
    if not vault_path.exists():
        typer.echo(f"Vault path does not exist: {vault_path}")
        return

    # Build overrides from CLI options
    overrides = {"status": status}
    if medium is not None:
        overrides["format"] = medium
    if rating is not None:
        overrides["rating"] = rating
    if referred_by is not None:
        overrides["referred_by"] = referred_by
    if tags is not None:
        overrides["tags"] = tags
    if date_started is not None:
        overrides["date_started"] = date_started
    if date_finished is not None:
        overrides["date_finished"] = date_finished

    # Create the book note
    file_path = create_book_note(
        selected_book, vault_path, status=status, overrides=overrides or None
    )
    typer.echo(f"Added: {file_path}")


@app.command()
def config(
    vault_path: str = typer.Option(None, "--vault", help="Set the vault path"),
    obsidian_vault: str = typer.Option(
        None,
        "--obsidian-vault",
        help="Set the Obsidian vault root path (for wikilink updates)",
    ),
    api_key: str = typer.Option(None, "--api-key", help="Set the Google Books API key"),
):
    """Configure libris settings."""
    if vault_path:
        p = Path(vault_path).expanduser().resolve()
        if not p.exists():
            typer.echo(f"Warning: Path {p} does not exist. Creating it...")
            p.mkdir(parents=True, exist_ok=True)
        set_book_vault_path(p)
        typer.echo(f"Vault path set to: {p}")

    if obsidian_vault:
        p = Path(obsidian_vault).expanduser().resolve()
        if not p.exists():
            typer.echo(f"Warning: Path {p} does not exist.")
            raise typer.Exit(code=1)
        if not p.is_dir():
            typer.echo(f"Error: Obsidian vault root must be a directory: {p}")
            raise typer.Exit(code=1)
        set_config("obsidian_vault_root", str(p))
        typer.echo(f"Obsidian vault root set to: {p}")

    if api_key:
        set_config("google_books_api_key", api_key)
        typer.echo("API key set successfully.")

    if not vault_path and not api_key and not obsidian_vault:
        from .config import get_api_key

        typer.echo(f"Current vault path: {get_vault_path()}")
        obsidian_root = get_obsidian_vault_root()
        if obsidian_root:
            typer.echo(f"Obsidian vault root: {obsidian_root}")
        key = get_api_key()
        if key:
            typer.echo(f"API key: {'*' * (len(key) - 4)}{key[-4:]}")
        else:
            typer.echo("API key: Not set")


@app.command()
def clean(
    rename: bool = typer.Option(
        False, "--rename", help="Rename file to canonical Title - Author.md format"
    ),
):
    """Select a specific book to clean its frontmatter."""
    vault_path = get_vault_path()
    books = list_books(vault_path)

    if not books:
        typer.echo("No books found in vault.")
        return

    choices = [p.name for p in books]
    selected_file_name = questionary.autocomplete(
        "Select a book to clean:",
        choices=choices,
        match_middle=True,
    ).ask()

    if not selected_file_name:
        return

    selected_file = vault_path / selected_file_name
    updated, fm = ensure_frontmatter_fields(selected_file)
    if updated:
        typer.echo(f"Cleaned: {selected_file_name}")
    else:
        typer.echo(f"{selected_file_name} is already up to date or invalid.")

    if rename:
        vault_root = get_obsidian_vault_root() or vault_path
        result = rename_book_file(selected_file, vault_root, frontmatter=fm)
        if result.status == "renamed":
            typer.echo(f"Renamed: {selected_file_name} → {result.new_path.name}")
        else:
            msg = _format_rename_skip(selected_file_name, result)
            if msg:
                typer.echo(msg)


@app.command()
def cleanup(
    rename: bool = typer.Option(
        False, "--rename", help="Rename files to canonical Title - Author.md format"
    ),
    auto_enrich: bool = typer.Option(
        False,
        "--auto-enrich",
        help="Automatically enrich from Google Books when title/author is missing (uses first matching result)",
    ),
    limit: int = typer.Option(
        0, "--limit", "-n", help="Stop after N files that needed action (0 = unlimited)"
    ),
):
    """Ensure all books in the vault have the correct frontmatter fields."""
    vault_path = get_vault_path()
    books = list_books(vault_path)

    if not books:
        typer.echo("No books found in vault.")
        return

    vault_root = get_obsidian_vault_root() or vault_path if rename else None

    updated_count = 0
    renamed_count = 0
    skipped_count = 0
    action_count = 0
    unmatched_files: list[str] = []
    for i, book_file in enumerate(books, 1):
        if limit > 0 and action_count >= limit:
            typer.echo(f"Limit reached ({limit}). Stopping.")
            break

        limit_status = f" ({action_count} of {limit} limit used)" if limit > 0 else ""
        typer.echo(f"Processing file {i}...{limit_status} {book_file.name}", nl=False)

        file_had_action = False

        updated, fm = ensure_frontmatter_fields(book_file)
        if updated:
            updated_count += 1
            file_had_action = True
            typer.echo(f"\n  Updated: {book_file.name}", nl=False)

        if rename:
            result = rename_book_file(book_file, vault_root, frontmatter=fm)

            # Offer to enrich when title or author is missing
            if result.status in ("missing_title", "missing_author"):
                enriched = False
                if auto_enrich:
                    typer.echo("")  # newline before enrich output
                    enriched = _enrich_auto(book_file, unmatched_files)
                else:
                    typer.echo(
                        f"\n  {book_file.name}: {result.status.replace('_', ' ')}"
                    )
                    if questionary.confirm(
                        f"Enrich {book_file.name} from Google Books?",
                        default=True,
                    ).ask():
                        enriched = _enrich_interactive(book_file)

                if enriched:
                    # Re-read updated frontmatter and retry rename
                    _, fm = ensure_frontmatter_fields(book_file)
                    result = rename_book_file(book_file, vault_root, frontmatter=fm)

                file_had_action = True

            if result.status == "renamed":
                renamed_count += 1
                file_had_action = True
                typer.echo(
                    f"\n  Renamed: {book_file.name} → {result.new_path.name}", nl=False
                )
            elif result.status not in ("already_canonical",):
                msg = _format_rename_skip(book_file.name, result)
                if msg:
                    skipped_count += 1
                    file_had_action = True
                    typer.echo(f"\n  {msg}", nl=False)

        typer.echo("")  # final newline for this file
        if file_had_action:
            action_count += 1

    typer.echo("")
    if updated_count == 0:
        typer.echo("All books are already up to date.")
    else:
        typer.echo(f"Finished. Updated {updated_count} books.")

    if rename:
        if renamed_count:
            typer.echo(f"Renamed {renamed_count} file(s).")
        elif skipped_count:
            typer.echo(
                f"No files renamed. {skipped_count} file(s) could not be renamed."
            )
        else:
            typer.echo("All files already have canonical names.")

    if unmatched_files:
        typer.echo(
            f"\n--- {len(unmatched_files)} file(s) could not be auto-enriched (no matching result) ---"
        )
        for name in unmatched_files:
            typer.echo(f"  • {name}")


def _normalize_for_match(text: str) -> str:
    """Normalize text for fuzzy comparison: lowercase, strip punctuation/extra whitespace.

    Delegates to the shared implementation in importer module.
    """
    return normalize_for_match(text)


def _build_search_query(filename_stem: str) -> str:
    """Build an effective Google Books search query from a filename stem.

    Handles common filename patterns like:
      - "Title - Author"
      - "Title Subtitle - Author"

    Splits on " - " to separate title from author, then uses Google Books
    query operators (intitle:/inauthor:) for more precise results.
    If no separator is found, returns the stem as-is.
    """
    parts = filename_stem.split(" - ", maxsplit=1)
    if len(parts) == 2:
        title_part = parts[0].strip()
        author_part = parts[1].strip()
        return f"intitle:{title_part} inauthor:{author_part}"
    return filename_stem


def _titles_match(filename_stem: str, book_title: str) -> bool:
    """Check if a Google Books title fuzzy-matches the filename stem.

    Returns True if the normalized filename stem is contained within the
    normalized book title, or vice versa.
    """
    norm_file = _normalize_for_match(filename_stem)
    norm_title = _normalize_for_match(book_title)
    if not norm_file or not norm_title:
        return False
    return norm_file in norm_title or norm_title in norm_file


def _enrich_auto(file_path: Path, unmatched_log: list[str]) -> bool:
    """Auto-enrich a file using the first matching Google Books result.

    Searches using the filename stem, checks if the first result's title
    is a fuzzy match. If it matches, applies the enrichment. If not, appends
    the filename to unmatched_log for later reporting.
    """
    query = _build_search_query(file_path.stem)
    if not _normalize_for_match(query):
        unmatched_log.append(file_path.name)
        return False

    client = GoogleBooksClient()
    results = client.search(query)

    if not results:
        unmatched_log.append(file_path.name)
        return False

    first = results[0]
    if _titles_match(file_path.stem, first.title):
        if update_frontmatter_from_book(file_path, first):
            # Append a note to the body indicating this was an automatic match
            _append_auto_enrich_note(file_path, first.title, query)
            typer.echo(
                f'Auto-enriched: {file_path.name} → "{first.title}" by {", ".join(first.authors)}'
            )
            return True
        return False
    else:
        unmatched_log.append(file_path.name)
        return False


def _append_auto_enrich_note(
    file_path: Path, matched_title: str, original_query: str
) -> None:
    """Append a note to the document body and add 'review' tag indicating auto-enrichment."""
    from datetime import date

    import yaml

    content = file_path.read_text(encoding="utf-8")

    # Add 'review' to the tags in frontmatter
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", content, re.DOTALL)
    if not match:
        match = re.match(r"^---\s*\n(.*?)\n---(.*)$", content, re.DOTALL)

    if match:
        data = yaml.safe_load(match.group(1))
        if isinstance(data, dict):
            tags = data.get("tags")
            if isinstance(tags, str):
                # Convert string tag to list (lowercase) and add review
                tag_list = [t.strip().lower() for t in tags.split(",") if t.strip()]
                if "review" not in tag_list:
                    tag_list.append("review")
                data["tags"] = tag_list
            elif isinstance(tags, list):
                existing = [t.lower() for t in tags if isinstance(t, str)]
                if "review" not in existing:
                    tags.append("review")
            else:
                data["tags"] = ["review"]
            new_fm = yaml.dump(data, sort_keys=False, allow_unicode=True).strip()
            rest = match.group(2)
            content = f"---\n{new_fm}\n---\n{rest.lstrip()}"

    # Append the callout note to the body
    note = (
        f"\n\n> [!warning] Auto-enriched ({date.today().isoformat()})\n"
        f"> Title matched automatically from Google Books.\n"
        f'> Query: "{original_query}" → Matched: "{matched_title}"\n'
        f"> Please verify this is the correct book.\n"
    )
    file_path.write_text(content.rstrip() + note + "\n", encoding="utf-8")


def _enrich_interactive(file_path: Path, results: list | None = None) -> bool:
    """Run the interactive enrich flow for a single file. Returns True if enriched.

    If *results* is provided the search step is skipped and those results are
    presented directly for selection.
    """
    if results is None:
        default_query = _build_search_query(file_path.stem)
        query = questionary.text(
            f"Search query for Google Books ({file_path.name}):",
            default=default_query,
        ).ask()

        if not query:
            return False

        client = GoogleBooksClient()
        results = client.search(query)

    if not results:
        typer.echo("No results found on Google Books.")
        return False

    _SKIP_CHOICE = "[ Skip this book ]"
    result_choices = [f"{b.title} by {', '.join(b.authors)}" for b in results]
    selected_result = questionary.select(
        "Select the correct match:",
        choices=result_choices + [_SKIP_CHOICE],
    ).ask()

    if not selected_result or selected_result == _SKIP_CHOICE:
        return False

    book = results[result_choices.index(selected_result)]

    if update_frontmatter_from_book(file_path, book):
        typer.echo(f"Enriched: {file_path.name}")
        return True
    else:
        typer.echo(f"{file_path.name} already has all available data.")
        return False


# Fields that are only populated via Google Books enrichment.
_API_SOURCED_FIELDS = (
    "google_books_id",
    "cover_thumbnail",
    "date_published",
    "page_count",
)


def _needs_enrichment(fm: dict) -> bool:
    """Return True if the book hasn't been enriched from Google Books yet.

    A book is considered already enriched if *any* API-sourced field has a
    non-empty value — this covers cases where google_books_id was not captured
    but other fields (thumbnail, published_date, etc.) were filled by an
    earlier enrichment.
    """
    return not any(fm.get(f) for f in _API_SOURCED_FIELDS)


# Fields counted when ranking which edition has the most complete metadata.
_COMPLETENESS_FIELDS = (
    "isbn",
    "page_count",
    "published_date",
    "google_books_id",
    "thumbnail",
    "genres",
    "description",
)


def _metadata_score(book) -> int:
    """Count how many metadata fields are non-empty on a BookCandidate."""
    score = 0
    for field in _COMPLETENESS_FIELDS:
        val = getattr(book, field, None)
        if val:
            # Lists/strings: only count if non-empty
            if isinstance(val, (list, str)) and len(val) == 0:
                continue
            score += 1
    return score


def _best_match(candidates: list) -> object:
    """Pick the candidate with the most complete metadata."""
    return max(candidates, key=_metadata_score)


def _build_query_from_frontmatter(fm: dict, file_path: Path) -> str:
    """Build a Google Books search query preferring frontmatter over filename."""
    note = BookNote(path=file_path, frontmatter=fm)
    if note.title is None:
        return _build_search_query(file_path.stem)

    parts = [f"intitle:{note.title}"]
    if note.first_author is not None:
        parts.append(f"inauthor:{note.first_author}")
    return " ".join(parts)


@app.command()
def autoenrich(
    interactive: bool = typer.Option(
        False,
        "--interactive",
        "-i",
        help="Prompt user to select when multiple matches are found",
    ),
    limit: int = typer.Option(
        0, "--limit", "-n", help="Stop after N files that needed action (0 = unlimited)"
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Show what would be enriched without making changes"
    ),
):
    """Enrich all books in the vault by filling missing frontmatter from Google Books.

    Iterates every book, searches Google Books, and auto-applies when there is
    a single confident match.  When multiple plausible results are found, use
    --interactive to select the right book; otherwise they are logged for later
    review.
    """
    vault_path = get_vault_path()
    books = list_books(vault_path)

    if not books:
        typer.echo("No books found in vault.")
        return

    client = GoogleBooksClient()

    enriched_auto = 0
    enriched_interactive = 0
    skipped = 0
    needs_interactive: list[str] = []
    unmatched: list[str] = []
    action_count = 0

    for i, book_file in enumerate(books, 1):
        if limit > 0 and action_count >= limit:
            typer.echo(f"Limit reached ({limit}). Stopping.")
            break

        fm = read_frontmatter(book_file)
        if fm is None:
            skipped += 1
            continue

        if not _needs_enrichment(fm):
            skipped += 1
            continue

        query = _build_query_from_frontmatter(fm, book_file)
        typer.echo(f"[{i}/{len(books)}] {book_file.name}", nl=False)

        if dry_run:
            typer.echo(f" — would search: {query}")
            action_count += 1
            continue

        results = client.search(query)

        if not results:
            # Fallback: try ISBN search if available
            isbn = fm.get("isbn")
            if isbn:
                results = client.search(f"isbn:{isbn}")

        if not results:
            typer.echo(" — no results")
            unmatched.append(book_file.name)
            action_count += 1
            continue

        # Find confident matches via title comparison
        confident = [b for b in results if _titles_match(book_file.stem, b.title)]

        # Auto-apply when there's a single result, or all confident matches
        # share the same title (i.e. different editions of the same book).
        unique_titles = (
            {_normalize_for_match(b.title) for b in confident} if confident else set()
        )
        if len(results) == 1 or (confident and len(unique_titles) == 1):
            pick = _best_match(confident) if confident else results[0]
            if update_frontmatter_from_book(book_file, pick):
                typer.echo(f' — auto-enriched → "{pick.title}"')
                enriched_auto += 1
            else:
                typer.echo(" — already up to date")
                skipped += 1
        elif interactive:
            typer.echo("")  # newline before interactive prompt
            if _enrich_interactive(book_file, results=results):
                enriched_interactive += 1
            else:
                skipped += 1
        else:
            typer.echo(f" — {len(results)} results, needs interactive selection")
            needs_interactive.append(book_file.name)

        action_count += 1

    # Summary
    typer.echo("")
    if dry_run:
        typer.echo(
            f"Dry run: {action_count} book(s) would be enriched, {skipped} already complete."
        )
        return

    typer.echo(f"Enriched (auto): {enriched_auto}")
    if enriched_interactive:
        typer.echo(f"Enriched (interactive): {enriched_interactive}")
    typer.echo(f"Skipped (already complete): {skipped}")

    if needs_interactive:
        typer.echo(
            f"\n--- {len(needs_interactive)} book(s) need interactive selection (rerun with --interactive) ---"
        )
        for name in needs_interactive:
            typer.echo(f"  • {name}")

    if unmatched:
        typer.echo(f"\n--- {len(unmatched)} book(s) had no results ---")
        for name in unmatched:
            typer.echo(f"  • {name}")


@app.command()
def enrich(
    filename: str = typer.Argument(
        None, help="Name of the markdown file to enrich (e.g. 'My Book.md')"
    ),
):
    """Search Google Books to fill in missing data for a book."""
    vault_path = get_vault_path()

    if filename is None:
        books = list_books(vault_path)
        if not books:
            typer.echo("No books found in vault.")
            return

        choices = [p.name for p in books]
        filename = questionary.autocomplete(
            "Select a book to enrich:",
            choices=choices,
            match_middle=True,
        ).ask()

        if not filename:
            return

    selected_file = vault_path / filename

    if not selected_file.exists():
        typer.echo(f"File not found: {selected_file}")
        raise typer.Exit(code=1)

    _enrich_interactive(selected_file)


@app.command()
def duplicates():
    """Find and report duplicate books in the vault."""
    vault_path = get_vault_path()
    groups = find_duplicates(vault_path)

    if not groups:
        typer.echo("No duplicates found.")
        return

    typer.echo(f"Found {len(groups)} group(s) of duplicates:\n")
    for i, group in enumerate(groups, 1):
        typer.echo(f"Group {i}:")
        for path in group:
            fm = read_frontmatter(path)
            isbn = fm.get("isbn") if fm else None
            gid = fm.get("google_books_id") if fm else None
            details = []
            if isbn:
                details.append(f"ISBN: {isbn}")
            if gid:
                details.append(f"Google ID: {gid}")
            detail_str = f" ({', '.join(details)})" if details else ""
            typer.echo(f"  - {path.name}{detail_str}")
        typer.echo()


@app.command()
def merge(
    auto: bool = typer.Option(
        False,
        "--auto",
        help="Auto-merge duplicates when ISBN and Google ID match (no conflicts)",
    ),
):
    """Merge duplicate books in the vault.

    Without --auto: Interactive mode where you choose which books to merge.
    With --auto: Automatically merge books when ISBN + Google ID match and no metadata conflicts exist.
    """
    vault_path = get_vault_path()
    groups = find_duplicates(vault_path)

    if not groups:
        typer.echo("No duplicates found.")
        return

    total_merged = 0

    for group_idx, group in enumerate(groups, 1):
        typer.echo(f"\n{'=' * 60}")
        typer.echo(f"Duplicate Group {group_idx} ({len(group)} books)")
        typer.echo(f"{'=' * 60}")

        # Display group members
        for path in group:
            fm = read_frontmatter(path)
            title = fm.get("title", "Unknown") if fm else "Unknown"
            status = fm.get("status", "Unknown") if fm else "Unknown"
            isbn = fm.get("isbn") if fm else None
            gid = fm.get("google_books_id") if fm else None
            details = [f"Status: {status}"]
            if isbn:
                details.append(f"ISBN: {isbn}")
            if gid:
                details.append(f"Google ID: {gid}")
            typer.echo(f"  {path.name}")
            typer.echo(f"    {title} | {' | '.join(details)}")

        if len(group) < 2:
            continue

        # Pick the most complete book as primary; merge others into it
        primary = get_primary_book(group[0], group[1])
        for candidate in group[2:]:
            primary = get_primary_book(primary, candidate)
        secondaries = [p for p in group if p != primary]

        typer.echo(f"\n  Primary (keeper): {primary.name}")

        for secondary in secondaries:
            typer.echo(f"\n  Merging {secondary.name} into {primary.name}...")

            try:
                if auto:
                    can_merge, reason, merge_result = check_auto_merge(
                        primary, secondary
                    )
                    if not can_merge:
                        typer.echo(f"    Skipped: {reason}")
                        continue

                    merged_fm, merged_body, _ = merge_result
                    write_merged_book(primary, merged_fm, merged_body)
                    delete_secondary_file(secondary)
                    typer.echo("    Auto-merged successfully")
                    total_merged += 1
                else:
                    merged_fm, merged_body, conflicts = merge_two_books(
                        primary, secondary, allow_conflicts=False
                    )

                    if conflicts:
                        typer.echo("    Conflicts detected:")
                        for conflict in conflicts:
                            typer.echo(
                                f"      {conflict.field}: '{conflict.primary_value}' vs '{conflict.secondary_value}'"
                            )
                        confirm = questionary.confirm(
                            "    Proceed with merge (keeping primary's conflicting values)?",
                            default=False,
                        ).ask()
                        if not confirm:
                            typer.echo("    Skipped by user")
                            continue

                        merged_fm, merged_body, _ = merge_two_books(
                            primary, secondary, allow_conflicts=True
                        )
                    else:
                        confirm = questionary.confirm(
                            f"    No conflicts. Merge {secondary.name} into {primary.name}?",
                            default=True,
                        ).ask()
                        if not confirm:
                            typer.echo("    Skipped by user")
                            continue

                    write_merged_book(primary, merged_fm, merged_body)
                    delete_secondary_file(secondary)
                    typer.echo("    Merged successfully")
                    total_merged += 1
            except Exception as e:
                typer.echo(f"    Error: {e}")
                continue

    typer.echo(f"\nMerge complete: {total_merged} duplicate(s) merged")


@app.command(name="import")
def import_cmd(
    file: str = typer.Argument(..., help="Path to the import file (JSON or CSV)"),
    fmt: str | None = typer.Option(
        None, "--format", "-f", help=f"Import format ({', '.join(SUPPORTED_FORMATS)})"
    ),
    apply: bool = typer.Option(
        False, "--apply", help="Actually create/update notes (default is dry-run)"
    ),
    limit: int = typer.Option(
        0, "--limit", "-n", help="Process only the first N entries (0 = all)"
    ),
):
    """Import books from a JSON or CSV file into your vault.

    By default, runs in dry-run mode showing what would happen.
    Use --apply to actually create and update notes.
    """
    file_path = Path(file).expanduser().resolve()
    if not file_path.exists():
        typer.echo(f"File not found: {file_path}")
        raise typer.Exit(code=1)

    vault_path = get_vault_path()
    if not vault_path.exists():
        typer.echo(f"Vault path does not exist: {vault_path}")
        raise typer.Exit(code=1)

    try:
        result = run_import(
            file_path, vault_path, apply=apply, format_name=fmt, limit=limit
        )
    except (ValueError, FileNotFoundError) as e:
        typer.echo(f"Error: {e}")
        raise typer.Exit(code=1)

    mode_label = "Import" if apply else "Dry run"
    typer.echo(f"\n{mode_label} complete:")
    typer.echo(
        f"  {len(result.new_books)} new book(s) {'added' if apply else 'would be added'}"
    )
    typer.echo(
        f"  {len(result.updated_books)} existing book(s) {'updated' if apply else 'would be updated'}"
    )
    typer.echo(
        f"  {len(result.skipped_books)} duplicate(s) already up-to-date (skipped)"
    )

    if result.new_books:
        preview_count = min(len(result.new_books), 20)
        typer.echo(
            f"\n{'Added' if apply else 'New'} books (showing {preview_count} of {len(result.new_books)}):"
        )
        for book in result.new_books[:preview_count]:
            authors_str = ", ".join(book.authors)
            typer.echo(f"  + {book.title} by {authors_str}")
        if len(result.new_books) > preview_count:
            typer.echo(f"  ... and {len(result.new_books) - preview_count} more")

    if result.updated_books:
        typer.echo(f"\n{'Updated' if apply else 'Would update'}:")
        for book, path, updates in result.updated_books:
            update_desc = ", ".join(updates)
            typer.echo(f"  ~ {path.name} ({update_desc})")

    if not apply and (result.new_books or result.updated_books):
        typer.echo("\nRun with --apply to execute these changes.")


@app.command()
def migrate(
    apply: bool = typer.Option(
        False, "--apply", help="Write the changes (a dry run by default)"
    ),
    limit: int = typer.Option(
        3, "--limit", "-n", help="How many diffs to print (0 for all)"
    ),
    out: str | None = typer.Option(
        None, "--out", help="Write the full diff to this file for review"
    ),
):
    """Migrate the Shelf to the canonical Book Note shape.

    A dry run by default: summarises what would change, prints a sample of
    diffs, and writes nothing. Review the diff before using --apply.
    """
    vault_path = get_vault_path()
    if not vault_path.exists():
        typer.echo(f"Shelf does not exist: {vault_path}")
        raise typer.Exit(code=1)

    typer.echo(f"Planning migration for {vault_path}...")
    plans = plan_migration(vault_path)
    changing = [plan for plan in plans if plan.changed]

    change_counts: dict[str, int] = {}
    for plan in plans:
        for change in plan.changes:
            label = change.split(";")[0]
            change_counts[label] = change_counts.get(label, 0) + 1

    typer.echo(f"\n{len(plans)} notes planned, {len(changing)} would change.\n")
    for label, count in sorted(change_counts.items(), key=lambda kv: -kv[1]):
        typer.echo(f"  {count:5d}  {label}")

    flagged = [plan for plan in plans if plan.warnings]
    if flagged:
        typer.echo(f"\n{len(flagged)} notes need a look:")
        for plan in flagged[:20]:
            typer.echo(f"  {plan.path.name}: {'; '.join(plan.warnings)}")

    if out:
        out_path = Path(out).expanduser()
        out_path.write_text("".join(plan.diff() for plan in changing), encoding="utf-8")
        typer.echo(f"\nFull diff written to {out_path}")

    shown = changing if limit == 0 else changing[:limit]
    for plan in shown:
        typer.echo("")
        typer.echo(plan.diff())

    if not apply:
        typer.echo(
            f"\nDry run. Nothing written. Re-run with --apply to migrate "
            f"{len(changing)} notes."
        )
        return

    if not flagged and not changing:
        typer.echo("Nothing to do.")
        return

    confirm = questionary.confirm(
        f"Rewrite {len(changing)} notes in {vault_path}?", default=False
    ).ask()
    if not confirm:
        typer.echo("Cancelled. Nothing written.")
        return

    written = apply_migration(plans)
    typer.echo(f"Migrated {written} notes.")


if __name__ == "__main__":
    app()
