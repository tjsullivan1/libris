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

# Prompt for selection when multiple matches are found
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
