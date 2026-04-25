import webbrowser

import typer
import questionary
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from .api import GoogleBooksClient, Book
from .markdown import create_book_note, update_book_status, list_books, ensure_frontmatter_fields, read_frontmatter, update_frontmatter_from_book, find_duplicates
from .config import get_vault_path, set_config
from .audible_client import get_auth_file, is_authenticated, get_locale

app = typer.Typer()
audible_app = typer.Typer(help="Audible integration commands.")
app.add_typer(audible_app, name="audible")

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
    api_key: str = typer.Option(None, "--api-key", help="Set the Google Books API key")
):
    """Configure libris settings."""
    if vault_path:
        p = Path(vault_path).expanduser().resolve()
        if not p.exists():
            typer.echo(f"Warning: Path {p} does not exist. Creating it...")
            p.mkdir(parents=True, exist_ok=True)
        set_config("vault_path", str(p))
        typer.echo(f"Vault path set to: {p}")
    
    if api_key:
        set_config("google_books_api_key", api_key)
        typer.echo("API key set successfully.")

    if not vault_path and not api_key:
        from .config import get_api_key
        typer.echo(f"Current vault path: {get_vault_path()}")
        key = get_api_key()
        if key:
            typer.echo(f"API key: {'*' * (len(key) - 4)}{key[-4:]}")
        else:
            typer.echo("API key: Not set")

@app.command()
def clean():
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
    if ensure_frontmatter_fields(selected_file):
        typer.echo(f"Cleaned: {selected_file_name}")
    else:
        typer.echo(f"{selected_file_name} is already up to date or invalid.")

@app.command()
def cleanup():
    """Ensure all books in the vault have the correct frontmatter fields."""
    vault_path = get_vault_path()
    books = list_books(vault_path)
    
    if not books:
        typer.echo("No books found in vault.")
        return
        
    updated_count = 0
    for book_file in books:
        if ensure_frontmatter_fields(book_file):
            updated_count += 1
            typer.echo(f"Updated: {book_file.name}")
            
    if updated_count == 0:
        typer.echo("All books are already up to date.")
    else:
        typer.echo(f"Finished. Updated {updated_count} books.")

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

    # Default search query: use the filename (minus .md extension)
    default_query = selected_file.stem
    query = questionary.text(
        "Search query for Google Books:",
        default=default_query,
    ).ask()

    if not query:
        return

    client = GoogleBooksClient()
    results = client.search(query)

    if not results:
        typer.echo("No results found on Google Books.")
        return

    result_choices = [f"{b.title} by {', '.join(b.authors)}" for b in results]
    selected_result = questionary.select(
        "Select the correct match:",
        choices=result_choices,
    ).ask()

    if not selected_result:
        return

    book = results[result_choices.index(selected_result)]

    if update_frontmatter_from_book(selected_file, book):
        typer.echo(f"Enriched: {filename}")
    else:
        typer.echo(f"{filename} already has all available data.")


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


if __name__ == "__main__":
    app()


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
