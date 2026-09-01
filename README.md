# Libris

A simple CLI tool to track your reading list in Obsidian. The name comes from the Latin expression "Ex Libris" (from the books of). 

## Features
- Search books using the Google Books API.
- Add books to your Obsidian vault with a pre-defined schema (Frontmatter).
- Track reading status (To Read, Reading, Finished).
- Enrich existing book notes with data from Google Books.
- Interactive search and selection.

## Installation
Ensure you have `uv` installed.
```bash
uv build
uv run libris --help
```

## Usage

### 1. Configure Libris
Set the directory where your book notes will be stored, your Obsidian vault root, and optionally a Google Books API key.

```bash
# Set the folder where book notes are stored
libris config --vault ~/Documents/ObsidianVault/Books

# Set the Obsidian vault root (used for wikilink updates when renaming files)
libris config --obsidian-vault ~/Documents/ObsidianVault

# Set a Google Books API key (optional; increases rate limits)
libris config --api-key YOUR_KEY

# Show current configuration
libris config
```

### 2. Search for Books
Search the Google Books catalog without adding anything to your vault. You can search by a general query, or narrow results by author, title, or ISBN:
```bash
# General search
libris search "The Great Gatsby"

# Search by author
libris search --author "Frank Herbert"

# Search by title
libris search --title "Dune"

# Search by ISBN
libris search --isbn 9780441013593
```

### 3. Add a Book
```bash
libris add "The Great Gatsby"
```
Follow the interactive prompt to select the correct book.

### 4. Update Reading Status
```bash
libris status
```
Select a book from your vault and update its status.

### 5. List Books
```bash
libris list
```

### 6. Enrich a Book Note
Fill in missing frontmatter fields for an existing book note using Google Books data.
```bash
# Select a book interactively
libris enrich

# Enrich a specific file
libris enrich "The Great Gatsby.md"
```

### 7. Clean Up Book Notes
Standardize frontmatter and optionally rename files to the canonical `Title - Author.md` format:
```bash
# Clean all book frontmatter (title casing, missing fields, etc.)
libris cleanup

# Also rename files to match Title - Author.md pattern (updates wikilinks)
libris cleanup --rename

# Automatically enrich books with missing title/author from Google Books
libris cleanup --rename --auto-enrich

# Stop after N files that needed action
libris cleanup --limit 10

# Clean a single book interactively
libris clean
libris clean --rename
```

### 8. Find Duplicate Books
Scan your vault for duplicate book notes matched by title, ISBN, or Google Books ID.
```bash
libris duplicates
```

### 9. Merge Duplicates
Merge duplicate book notes, combining metadata from both files into one.
```bash
# Interactive merge — review each duplicate group
libris merge

# Auto-merge when ISBN + Google ID match and no conflicts exist
libris merge --auto
```

### 10. Auto-Enrich All Books
Batch-enrich every book in your vault by filling missing frontmatter fields from Google Books. Automatically applies when there is a single result, or when all confident matches share the same title (e.g., different editions).
```bash
# Enrich all books (auto-match only)
libris autoenrich

# Prompt for selection when multiple ambiguous matches are found
libris autoenrich --interactive

# Preview what would be enriched without making changes
libris autoenrich --dry-run

# Stop after N files that needed action
libris autoenrich --limit 10
```

### 11. Import Books
Import books from external sources (e.g., Audible JSON export) into your vault. By default runs in dry-run mode showing what would happen.
```bash
# Dry-run: preview what would be imported
libris import library.json

# Actually create/update notes
libris import library.json --apply

# Specify format explicitly
libris import library.json --format audible-json

# Limit to first N entries
libris import library.json --apply --limit 50
```

Currently supported import formats:
- `audible-json` — Audible library JSON export (auto-detected for `.json` files)

## Browser extension

Clip the book on an Amazon or Goodreads page straight into your Library. The extension talks to a
small daemon on your own machine, so the duplicate check runs against the live vault and a book
you add is a file that exists by the time the popup says so.

### 1. Install the daemon

The web stack is optional, so it ships as an extra:

```bash
uv sync --extra server            # in this repo
uv tool install 'libris[server]'  # anywhere else
```

Then start it and take note of the token:

```bash
libris serve
libris serve --show-token
```

It binds `127.0.0.1` only, and refuses any other interface unless you pass `--allow-remote`. A
bearer token is the only thing guarding your vault, so that refusal is deliberate.

### 2. Load the extension

1. Open `edge://extensions` (or `chrome://extensions`)
2. Turn on **Developer mode**
3. **Load unpacked**, and choose the `extension/` directory

There is no build step. The extension is plain JavaScript with no dependencies, so what you load
is what is in the repository.

### 3. Connect it

Open the extension's options page, paste the token from `libris serve --show-token`, and press
**Save and test**. The default server URL is `http://127.0.0.1:8787`.

If you change the URL — a different port, or a remote Libris later — the browser will ask for
permission for that address the first time. That prompt is expected: the extension ships with
permission for the default daemon only, and asks for anything else when you point it there.

### Using it

Open a book page and click the Libris icon. The extension reads the page, asks Google Books, and
shows you the matches. Pick one, set the status, format and rating, and add it.

- **Amazon** and **Goodreads** have scrapers that read identifiers directly.
- **Anywhere else** falls back to Open Graph and JSON-LD metadata, so most bookshop sites work.
- If a book is already in your vault, you are told before you fill anything in, and nothing is
  overwritten.
- If your vault holds something with a similar title, it is shown to you rather than guessed at.
  Confirming that it is the same book skips the write; nothing is merged.
- If Google Books finds nothing, you can correct the title and search again, or add the book
  unenriched and let `libris autoenrich` fill it in later.

The extension only reads a page when you click its icon. It holds no permission for Amazon,
Goodreads, or anywhere else, which is also why it cannot tell you a page is a book page before
you click.

### Keeping the daemon running

The extension needs the daemon up. On Windows, register it to start at logon:

```powershell
.\contrib\Register-LibrisDaemon.ps1
```

That creates a scheduled task, starts it, checks that the daemon actually answers, and prints
the token. `-Remove` undoes it, and `-Port` matches a non-default port.

On macOS a `launchd` user agent in `~/Library/LaunchAgents` does the same job, and on Linux a
user-level `systemd` unit with `WantedBy=default.target`. Both need the absolute path to
`libris` and the argument `serve`. Files for them are deliberately not shipped: nobody here can
run them, and an untested plist that is subtly wrong is worse than a paragraph telling you what
it needs to contain.

### Troubleshooting

The popup names the problem rather than reporting a generic failure:

| What you see | What it means |
| --- | --- |
| Grant access to … on the options page | The server URL is not one the browser has let the extension reach. Press **Save and test** in options and accept the prompt. |
| Libris isn't answering | The daemon is not running, or the URL points somewhere else. Start it with `libris serve`. |
| No Shelf is configured | The daemon is up but has no vault. Run `libris config --vault <path>`. |
| That token doesn't match | The daemon is up and reachable, but the token is stale. Run `libris serve --show-token` and paste it again. |
| Google Books couldn't be reached | The lookup failed upstream. Nothing was written; try again. |
| This doesn't look like a book page | The page yielded no title and no identifier. This is not a lookup failure — you are probably not on a book's page. |

## Schema
Books are saved as Markdown files with the following frontmatter:
- `title`
- `author`
- `isbn`
- `page_count`
- `published_date`
- `google_books_id`
- `thumbnail`
- `genres`
- `status`
- `rating`
- `date_started`
- `date_finished`
