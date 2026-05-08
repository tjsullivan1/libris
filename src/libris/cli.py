import webbrowser

import typer
import questionary
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from .api import GoogleBooksClient, Book
from .markdown import create_book_note, update_book_status, list_books, ensure_frontmatter_fields, read_frontmatter, update_frontmatter_from_book, find_duplicates, rename_book_file, RenameResult
from .config import get_vault_path, set_config, get_obsidian_vault_root, set_book_vault_path
from .audible_client import get_auth_file, is_authenticated, get_locale
from .merge import (
    merge_two_books,
    check_auto_merge,
    get_primary_book,
    write_merged_book,
    delete_secondary_file,
)

app = typer.Typer()
audible_app = typer.Typer(help="Audible integration commands.")
app.add_typer(audible_app, name="audible")

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
        "Select a book to update:",
        choices=choices
    ).ask()
    
    if not selected_file_name:
        return
        
    selected_file = vault_path / selected_file_name
    
    new_status = questionary.select(
        "New status:",
        choices=["To Read", "Reading", "Finished"]
    ).ask()
    
    if not new_status:
        return
        
    update_book_status(selected_file, new_status)
    typer.echo(f"Updated: {selected_file_name} -> {new_status}")

@app.command(name="list")
def list_cmd(
    timing: bool = typer.Option(False, "--timing", help="Print scan timing for list operation")
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
def add(query: str = typer.Argument(..., help="Title, author, or ISBN to search for")):
    """Search for a book and add it to your Obsidian vault."""
    client = GoogleBooksClient()
    books = client.search(query)
    
    if not books:
        typer.echo("No books found.")
        return

    choices = [f"{book.title} by {', '.join(book.authors)}" for book in books]
    selected_choice = questionary.select(
        "Select a book to add:",
        choices=choices
    ).ask()
    
    if not selected_choice:
        return
        
    book_index = choices.index(selected_choice)
    selected_book = books[book_index]
    
    vault_path = get_vault_path()
    if not vault_path.exists():
        typer.echo(f"Vault path does not exist: {vault_path}")
        return
    
    # Create the book note
    file_path = create_book_note(selected_book, vault_path)
    typer.echo(f"Added: {file_path}")

@app.command()
def config(
    vault_path: str = typer.Option(None, "--vault", help="Set the vault path"),
    obsidian_vault: str = typer.Option(None, "--obsidian-vault", help="Set the Obsidian vault root path (for wikilink updates)"),
    api_key: str = typer.Option(None, "--api-key", help="Set the Google Books API key")
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
    rename: bool = typer.Option(False, "--rename", help="Rename file to canonical Title - Author.md format"),
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
    rename: bool = typer.Option(False, "--rename", help="Rename files to canonical Title - Author.md format"),
    auto_enrich: bool = typer.Option(False, "--auto-enrich", help="Automatically enrich from Google Books when title/author is missing (uses first matching result)"),
    limit: int = typer.Option(0, "--limit", "-n", help="Stop after N files that needed action (0 = unlimited)"),
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
                    typer.echo(f"\n  {book_file.name}: {result.status.replace('_', ' ')}")
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
                typer.echo(f"\n  Renamed: {book_file.name} → {result.new_path.name}", nl=False)
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
            typer.echo(f"No files renamed. {skipped_count} file(s) could not be renamed.")
        else:
            typer.echo("All files already have canonical names.")

    if unmatched_files:
        typer.echo(f"\n--- {len(unmatched_files)} file(s) could not be auto-enriched (no matching result) ---")
        for name in unmatched_files:
            typer.echo(f"  • {name}")

def _normalize_for_match(text: str) -> str:
    """Normalize text for fuzzy comparison: lowercase, strip punctuation/extra whitespace."""
    text = re.sub(r'[^\w\s]', ' ', text.lower())
    return re.sub(r'\s+', ' ', text).strip()


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
    query = _normalize_for_match(file_path.stem)
    if not query:
        unmatched_log.append(file_path.name)
        return False

    client = GoogleBooksClient()
    results = client.search(file_path.stem)

    if not results:
        unmatched_log.append(file_path.name)
        return False

    first = results[0]
    if _titles_match(file_path.stem, first.title):
        if update_frontmatter_from_book(file_path, first):
            # Append a note to the body indicating this was an automatic match
            _append_auto_enrich_note(file_path, first.title, file_path.stem)
            typer.echo(f"Auto-enriched: {file_path.name} → \"{first.title}\" by {', '.join(first.authors)}")
            return True
        return False
    else:
        unmatched_log.append(file_path.name)
        return False


def _append_auto_enrich_note(file_path: Path, matched_title: str, original_query: str) -> None:
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
        f"> Query: \"{original_query}\" → Matched: \"{matched_title}\"\n"
        f"> Please verify this is the correct book.\n"
    )
    file_path.write_text(content.rstrip() + note + "\n", encoding="utf-8")


def _enrich_interactive(file_path: Path) -> bool:
    """Run the interactive enrich flow for a single file. Returns True if enriched."""
    default_query = file_path.stem
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

    result_choices = [f"{b.title} by {', '.join(b.authors)}" for b in results]
    selected_result = questionary.select(
        "Select the correct match:",
        choices=result_choices,
    ).ask()

    if not selected_result:
        return False

    book = results[result_choices.index(selected_result)]

    if update_frontmatter_from_book(file_path, book):
        typer.echo(f"Enriched: {file_path.name}")
        return True
    else:
        typer.echo(f"{file_path.name} already has all available data.")
        return False


@app.command()
def enrich(filename: str = typer.Argument(None, help="Name of the markdown file to enrich (e.g. 'My Book.md')")):
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


@audible_app.command()
def login(
    locale: str = typer.Option(None, "--locale", help="Audible marketplace country code (e.g. us, uk, de)"),
):
    """Authenticate with Audible via your web browser."""
    import audible

    auth_file = get_auth_file()
    if auth_file.exists():
        typer.echo("Already authenticated. Run 'libris audible logout' first to re-authenticate.")
        return

    effective_locale = locale or get_locale()

    def login_url_callback(url: str) -> str:
        typer.echo("\nOpening your browser for Audible login...")
        typer.echo(f"If the browser doesn't open, visit this URL:\n{url}\n")
        webbrowser.open(url)
        typer.echo(
            "After logging in, your browser will show an error page (this is expected).\n"
            "Copy the full URL from your browser's address bar and paste it below."
        )
        return input("Paste the URL here: ").strip()

    try:
        auth = audible.Authenticator.from_login_external(
            locale=effective_locale,
            login_url_callback=login_url_callback,
        )
    except Exception as e:
        typer.echo(f"Authentication failed: {e}")
        raise typer.Exit(code=1)

    auth_file.parent.mkdir(parents=True, exist_ok=True)
    auth.to_file(filename=str(auth_file))

    if locale:
        set_config("audible_locale", locale)

    device_name = auth.device_info.get("device_name", "Unknown device") if auth.device_info else "Unknown device"
    typer.echo(f"Successfully authenticated with Audible! Device: {device_name}")


@audible_app.command()
def logout():
    """Deregister Audible device and remove authentication."""
    import audible

    auth_file = get_auth_file()
    if not auth_file.exists():
        typer.echo("Not currently authenticated.")
        return

    try:
        auth = audible.Authenticator.from_file(str(auth_file))
        auth.refresh_access_token()
        auth.deregister_device()
        device_name = auth.device_info.get("device_name", "device") if auth.device_info else "device"
        typer.echo(f"Deregistered {device_name}.")
    except Exception as e:
        typer.echo(f"Warning: Could not deregister device: {e}")
        typer.echo("Removing local auth file anyway.")

    auth_file.unlink(missing_ok=True)
    typer.echo("Logged out of Audible.")


@audible_app.command(name="status")
def audible_status():
    """Show current Audible authentication status."""
    import audible

    auth_file = get_auth_file()
    if not auth_file.exists():
        typer.echo("Not authenticated. Run 'libris audible login' to connect your account.")
        return

    try:
        auth = audible.Authenticator.from_file(str(auth_file))
    except Exception as e:
        typer.echo(f"Error reading auth file: {e}")
        return

    typer.echo("Audible: Authenticated")
    typer.echo(f"  Locale: {auth.locale.country_code if auth.locale else get_locale()}")

    if auth.device_info:
        typer.echo(f"  Device: {auth.device_info.get('device_name', 'Unknown')}")

    if auth.expires:
        expires_dt = datetime.fromtimestamp(auth.expires, tz=timezone.utc)
        now = datetime.now(tz=timezone.utc)
        if expires_dt > now:
            remaining = expires_dt - now
            minutes = int(remaining.total_seconds() // 60)
            typer.echo(f"  Token expires in: {minutes} min")
        else:
            typer.echo("  Token: Expired (will auto-refresh on next use)")


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
            title = fm.get("title", "Unknown") if fm else "Unknown"
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
    auto: bool = typer.Option(False, "--auto", help="Auto-merge duplicates when ISBN and Google ID match (no conflicts)")
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
        typer.echo(f"\n{'='*60}")
        typer.echo(f"Duplicate Group {group_idx} ({len(group)} books)")
        typer.echo(f"{'='*60}")
        
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
                    can_merge, reason, merge_result = check_auto_merge(primary, secondary)
                    if not can_merge:
                        typer.echo(f"    Skipped: {reason}")
                        continue

                    merged_fm, merged_body, _ = merge_result
                    write_merged_book(primary, merged_fm, merged_body)
                    delete_secondary_file(secondary)
                    typer.echo(f"    Auto-merged successfully")
                    total_merged += 1
                else:
                    merged_fm, merged_body, conflicts = merge_two_books(primary, secondary, allow_conflicts=False)

                    if conflicts:
                        typer.echo(f"    Conflicts detected:")
                        for conflict in conflicts:
                            typer.echo(f"      {conflict.field}: '{conflict.primary_value}' vs '{conflict.secondary_value}'")
                        confirm = questionary.confirm(
                            "    Proceed with merge (keeping primary's conflicting values)?",
                            default=False
                        ).ask()
                        if not confirm:
                            typer.echo(f"    Skipped by user")
                            continue

                        merged_fm, merged_body, _ = merge_two_books(primary, secondary, allow_conflicts=True)
                    else:
                        confirm = questionary.confirm(
                            f"    No conflicts. Merge {secondary.name} into {primary.name}?",
                            default=True
                        ).ask()
                        if not confirm:
                            typer.echo(f"    Skipped by user")
                            continue

                    write_merged_book(primary, merged_fm, merged_body)
                    delete_secondary_file(secondary)
                    typer.echo(f"    Merged successfully")
                    total_merged += 1
            except Exception as e:
                typer.echo(f"    Error: {e}")
                continue

    typer.echo(f"\nMerge complete: {total_merged} duplicate(s) merged")


if __name__ == "__main__":
    app()
