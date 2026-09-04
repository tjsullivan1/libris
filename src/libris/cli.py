import ipaddress
import json
import re
import sys
import time
from pathlib import Path

import questionary
import typer

from . import installed_version
from .api import GoogleBooksClient
from .config import (
    VaultNotConfigured,
    ensure_server_token,
    get_obsidian_vault_root,
    get_vault_path,
    is_vault_configured,
    set_book_vault_path,
    set_config,
)
from .importer import SUPPORTED_FORMATS, run_import
from .markdown import (
    BookNote,
    FrontmatterUnreadable,
    RenameResult,
    create_book_note,
    ensure_frontmatter_fields,
    find_duplicate_candidates,
    find_duplicates,
    list_books,
    read_frontmatter,
    rename_book_file,
    split_frontmatter,
    update_book_status,
    update_frontmatter_from_book,
)
from .matching import (
    best_match,
    build_search_query,
    metadata_score,
    normalize_for_match,
    titles_match,
)
from .merge import (
    check_auto_merge,
    delete_secondary_file,
    get_primary_book,
    merge_two_books,
    write_merged_book,
)
from .migrate import apply_migration, plan_format_migration, plan_migration
from .note_format import (
    STATUS_VALUES,
    InvalidFieldValue,
    normalize_field_value,
    validate_field_value,
)
from .service import apply_decisions

# Windows consoles and redirected output default to cp1252, which cannot encode
# 39 of this Shelf's filenames - they carry U+FFFD where an accent was lost. A
# command that dies while printing a book's name is never the right answer, so
# output is UTF-8 and unencodable characters are replaced rather than fatal.
for _stream in (sys.stdout, sys.stderr):
    _reconfigure = getattr(_stream, "reconfigure", None)
    if _reconfigure is not None:
        try:
            _reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):  # a stream that cannot be reconfigured
            pass


app = typer.Typer()


def _show_version(value: bool) -> None:
    """Print the installed version and stop, before any command runs."""
    if value:
        typer.echo(installed_version())
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(
        False,
        "--version",
        callback=_show_version,
        is_eager=True,
        help="Print the installed version and exit.",
    ),
) -> None:
    """Track the books you have read, are reading, or mean to read."""


def _require_vault_path() -> Path:
    """Get the Shelf, or stop the command with an explanation.

    Library code raises and the CLI reports, per `AGENTS.md`. Every command
    that touches the Shelf needs the same three lines, so they live here rather
    than at each of the eleven call sites.

    Returns:
        The resolved path to the configured Shelf.

    Raises:
        typer.Exit: With code 1 when no Shelf is configured.
    """
    try:
        return get_vault_path()
    except VaultNotConfigured:
        typer.echo("No Shelf is configured, so there is nothing to read or write.")
        typer.echo("Set one with: libris config --vault <path>")
        raise typer.Exit(code=1) from None


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
    vault_path = _require_vault_path()
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

    # Offered from the Library's own vocabulary rather than a list kept here.
    # This prompt used to offer "Finished", which no note has ever held, and
    # omit "Not To Read"; since validation landed, choosing it raised.
    new_status = questionary.select("New status:", choices=list(STATUS_VALUES)).ask()

    if not new_status:
        return

    try:
        update_book_status(selected_file, new_status)
    except FrontmatterUnreadable as exc:
        typer.echo(f"{exc} Nothing was written to it.")
        raise typer.Exit(code=1) from None

    typer.echo(f"Updated: {selected_file_name} -> {new_status}")


@app.command(name="list")
def list_cmd(
    timing: bool = typer.Option(
        False, "--timing", help="Print scan timing for list operation"
    ),
):
    """List all books in your vault."""
    vault_path = _require_vault_path()
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
    medium: list[str] = typer.Option(
        None,
        "--format",
        "-f",
        help="Format you have this book in; repeat for more than one "
        "(Physical, Ebook, Audiobook)",
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
    # Checked before the search, so an invalid status is not reported after a
    # network round trip and a book picker.
    try:
        validate_field_value("status", status)
        if medium:
            validate_field_value(
                "format", normalize_field_value("format", list(medium))
            )
    except InvalidFieldValue as exc:
        typer.echo(str(exc))
        raise typer.Exit(code=1) from None

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

    vault_path = _require_vault_path()
    if not vault_path.exists():
        typer.echo(f"Vault path does not exist: {vault_path}")
        return

    # Build overrides from CLI options
    overrides = {"status": status}
    if medium:
        overrides["format"] = list(medium)
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

        # `libris config` with no arguments is how a person finds out what is
        # set, so an unset Shelf is what it is here to report, not a failure.
        if is_vault_configured():
            typer.echo(f"Current vault path: {get_vault_path()}")
        else:
            typer.echo("Current vault path: Not set")
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
    vault_path = _require_vault_path()
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
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Report what would change without writing anything"
    ),
):
    """Ensure all books in the vault have the correct frontmatter fields."""
    vault_path = _require_vault_path()
    books = list_books(vault_path)

    if not books:
        typer.echo("No books found in vault.")
        return

    if dry_run and (rename or auto_enrich):
        # Renaming rewrites wikilinks and enrichment writes frontmatter, and
        # neither has a preview mode. A flag that says it writes nothing has to
        # mean it, so the combination is refused rather than half-honoured.
        typer.echo(
            "--dry-run covers frontmatter only. It cannot preview --rename or "
            "--auto-enrich, which write as they go."
        )
        raise typer.Exit(code=1)

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

        updated, fm = ensure_frontmatter_fields(book_file, dry_run=dry_run)
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


# Thin re-exports of the shared matching helpers (#63). The private names are
# what this module's call sites and tests already use; the implementations live
# in matching.py because every adapter needs them, not just the CLI.
_normalize_for_match = normalize_for_match
_build_search_query = build_search_query
_titles_match = titles_match
_metadata_score = metadata_score
_best_match = best_match


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
    split = split_frontmatter(content)

    if split is not None:
        data = yaml.safe_load(split[0])
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
            # The body goes back as it was read, leading newlines and all (#99).
            content = f"---\n{new_fm}\n---\n{split[1]}"

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
    vault_path = _require_vault_path()
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
    vault_path = _require_vault_path()

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
    vault_path = _require_vault_path()

    groups = find_duplicates(vault_path)

    if not groups:
        typer.echo("No duplicates found.")
    else:
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

    candidates = find_duplicate_candidates(vault_path)
    if candidates:
        typer.echo(f"\n{len(candidates)} duplicate candidate(s) need a person:")
        typer.echo(
            "  One title contains the other, which is a judgement rather than a "
            "fact.\n  Nothing here is merged without an answer."
        )
        for first, second in candidates:
            typer.echo(f"\n  {first.title}")
            typer.echo(f"  {second.title}")
            typer.echo(f"    {first.path.name}")
            typer.echo(f"    {second.path.name}")


@app.command()
def merge(
    auto: bool = typer.Option(
        False,
        "--auto",
        help="Auto-merge duplicates when ISBN and Google ID match (no conflicts)",
    ),
    decisions: str | None = typer.Option(
        None,
        "--decisions",
        help="Apply an exported duplicate review (JSON) instead of prompting",
    ),
    allow_conflicts: bool = typer.Option(
        False,
        "--allow-conflicts",
        help="With --decisions, merge even when the reader's own values disagree",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="With --decisions, report what would happen and write nothing",
    ),
):
    """Merge duplicate books in the vault.

    Without --auto: Interactive mode where you choose which books to merge.
    With --auto: Automatically merge books when ISBN + Google ID match and no metadata conflicts exist.
    """
    vault_path = _require_vault_path()

    if decisions is not None:
        _merge_from_decisions(vault_path, Path(decisions), allow_conflicts, dry_run)
        return
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


_DECISION_LABELS = {
    "merged": "Merged",
    "would_merge": "Would merge",
    "skipped": "Left alone",
    "conflicted": "Needs you",
    "drifted": "Moved on",
}


def _merge_from_decisions(
    vault_path: Path, path: Path, allow_conflicts: bool, dry_run: bool = False
) -> None:
    """Apply a duplicate review exported from the review page."""
    if not path.exists():
        typer.echo(f"No such decisions file: {path}")
        raise typer.Exit(code=1)

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        typer.echo(f"Could not read {path.name}: {exc}")
        raise typer.Exit(code=1) from None

    records = payload.get("decisions") if isinstance(payload, dict) else payload
    if not isinstance(records, list):
        typer.echo(f"{path.name} holds no decisions.")
        raise typer.Exit(code=1)

    verb = "Planning" if dry_run else "Applying"
    typer.echo(f"{verb} {len(records)} decision(s) for {vault_path}...")
    outcomes = apply_decisions(
        vault_path, records, allow_conflicts=allow_conflicts, dry_run=dry_run
    )

    counts: dict[str, int] = {}
    for outcome in outcomes:
        status = outcome.status.value
        counts[status] = counts.get(status, 0) + 1
        if status != "skipped":
            typer.echo(f"  {_DECISION_LABELS[status]}: {outcome.detail}")

    typer.echo("")
    for status in ("merged", "would_merge", "conflicted", "drifted", "skipped"):
        if counts.get(status):
            typer.echo(f"  {_DECISION_LABELS[status]}: {counts[status]}")

    if dry_run and counts.get("would_merge"):
        typer.echo("\nDry run. Nothing written. Re-run without --dry-run to merge.")

    if counts.get("conflicted"):
        typer.echo(
            "\nA pair that needs you disagrees about something only you can settle -"
            "\na rating, a status, when you read it. Merge those with `libris merge`."
        )


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

    vault_path = _require_vault_path()
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
    formats: bool = typer.Option(
        False,
        "--formats",
        help="Migrate only the format field, leaving every other field alone",
    ),
):
    """Migrate the Shelf to the canonical Book Note shape.

    A dry run by default: summarises what would change, prints a sample of
    diffs, and writes nothing. Review the diff before using --apply.
    """
    vault_path = _require_vault_path()
    if not vault_path.exists():
        typer.echo(f"Shelf does not exist: {vault_path}")
        raise typer.Exit(code=1)

    typer.echo(f"Planning migration for {vault_path}...")
    plans = plan_format_migration(vault_path) if formats else plan_migration(vault_path)
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


# Names that mean this machine but are not addresses. Everything else is
# decided by the address itself, so the whole 127.0.0.0/8 range and ::1 count.
_LOOPBACK_HOSTNAMES = frozenset({"localhost"})


def _is_loopback(host: str) -> bool:
    """Check whether a host binds this machine only.

    Binding anything else exposes the Shelf to the network with only a bearer
    token in front of it, so the CLI refuses it without an explicit flag. The
    check is done on the parsed address rather than an allowlist of literals,
    so 127.0.0.2 is correctly treated as loopback and does not push someone
    towards --allow-remote to do something safe.

    Args:
        host: The interface the daemon was asked to bind.

    Returns:
        True if the host reaches only this machine.
    """
    if host.lower() in _LOOPBACK_HOSTNAMES:
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", "--host", help="Interface to bind."),
    port: int = typer.Option(8787, "--port", help="Port to listen on."),
    show_token: bool = typer.Option(
        False,
        "--show-token",
        help="Print the bearer token the extension needs, and exit.",
    ),
    reload: bool = typer.Option(
        False, "--reload", help="Restart on source changes, for development."
    ),
    allow_remote: bool = typer.Option(
        False, "--allow-remote", help="Allow binding an interface other than loopback."
    ),
):
    """Run the local daemon that the browser extension talks to."""
    if show_token:
        typer.echo(ensure_server_token())
        return

    if not _is_loopback(host) and not allow_remote:
        typer.echo(
            f"Refusing to bind {host}: a bearer token is the only thing guarding the Shelf."
        )
        typer.echo(
            "Pass --allow-remote if you intend to expose it beyond this machine."
        )
        raise typer.Exit(1)

    try:
        from . import server
    except ImportError:
        typer.echo("The server extra is not installed, so `libris serve` cannot run.")
        typer.echo("Install it with: uv sync --extra server")
        typer.echo("or, outside this repo: pip install 'libris[server]'")
        raise typer.Exit(1) from None

    if not is_vault_configured():
        # Checked here rather than left to _require_vault_path so the message
        # names the daemon's problem: it has nothing to serve. #55 starts it
        # from a scheduled task, where nobody is watching for a traceback.
        typer.echo("No Shelf is configured, so the daemon has nothing to serve.")
        typer.echo("Set one with: libris config --vault <path>")
        raise typer.Exit(1)

    ensure_server_token()
    typer.echo(f"Libris daemon listening on http://{host}:{port}")
    typer.echo("Run `libris serve --show-token` for the token the extension needs.")
    server.run(host=host, port=port, reload=reload)


@app.command()
def mcp():
    """Run the MCP server an agent drives, over stdio.

    Speaks JSON-RPC on stdin and stdout, so it is started by an MCP client
    rather than by hand. Register it with:

        uv run --directory <repo> libris mcp

    which always runs the working tree - `libris` on PATH is a `uv tool`
    snapshot that goes stale silently, and a server registered once and spawned
    daily is where that hides longest.
    """
    try:
        from . import mcp_server
    except ImportError:
        # Written to stderr: stdout is the protocol channel, and a stray line on
        # it corrupts the first message rather than being seen by anyone.
        typer.echo(
            "The mcp extra is not installed, so `libris mcp` cannot run.", err=True
        )
        typer.echo("Install it with: uv sync --extra mcp", err=True)
        typer.echo("or, outside this repo: pip install 'libris[mcp]'", err=True)
        raise typer.Exit(1) from None

    if not is_vault_configured():
        # Unlike `libris serve`, this starts anyway. An MCP client shows a server
        # that exited as a connection failure and puts the reason on a stderr
        # nobody reads, so the tools report the problem instead - to the person
        # who can fix it, in the conversation where it came up.
        typer.echo(
            "No Shelf is configured; the tools will say so until one is set.",
            err=True,
        )

    mcp_server.run()
