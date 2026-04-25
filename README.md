# Libris

A simple CLI tool to track your reading list in Obsidian. The name comes from the Latin expression "Ex Libris" (from the books of). 

## Features
- Search books using the Google Books API.
- Add books to your Obsidian vault with a pre-defined schema (Frontmatter).
- Track reading status (To Read, Reading, Finished).
- Interactive search and selection.

## Installation
Ensure you have `uv` installed.
```bash
uv build
uv run libris --help
```

## Usage

### 1. Configure your Vault Path
Set the directory where your book notes will be stored.
```bash
libris config --vault ~/Documents/ObsidianVault/Books
```

### 2. Search and Add a Book
```bash
libris add "The Great Gatsby"
```
Follow the interactive prompt to select the correct book.

### 3. Update Reading Status
```bash
libris status
```
Select a book from your vault and update its status.

### 4. List Books
```bash
libris list
```

### 5. Find Duplicate Books
Scan your vault for duplicate book notes matched by title, ISBN, or Google Books ID.
```bash
libris duplicates
```

### 6. Audible Integration
Connect your Audible account to sync your audiobook library.

```bash
# Authenticate with Audible (opens your browser)
libris audible login

# Check authentication status
libris audible status

# Log out and deregister the device
libris audible logout
```

You can specify a marketplace locale during login:
```bash
libris audible login --locale uk
```

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
