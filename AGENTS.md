# Agent Instructions

**Libris** — A Python CLI tool to track your reading list in Obsidian.
Uses Google Books API for search/enrichment and writes Markdown notes with YAML frontmatter.

This project uses **GitHub Issues** for issue tracking.

## Stack

- Python 3.12+ | Package manager: `uv` | Build backend: hatchling
- CLI: Typer | HTTP: httpx | Config: PyYAML | Interactive prompts: questionary
- Linter/formatter: ruff | Testing: pytest
- CI: GitHub Actions (test matrix 3.12+3.13, lint, security audit)

## Architecture

```
src/libris/           # src layout — hatchling maps src/ → "" in wheel
├── cli.py            # Typer app, all CLI commands (entry point: libris.cli:app)
├── api.py            # GoogleBooksClient — httpx with retry + Book dataclass
├── config.py         # YAML config at ~/.config/libris/config.yaml
│                     #   (override via LIBRIS_CONFIG_DIR env var)
├── markdown.py       # Obsidian note CRUD, frontmatter parsing, file renaming
├── importer.py       # Import from external sources (Audible JSON, etc.)
└── merge.py          # Duplicate detection and merge logic
tests/                # Mirrors src structure
├── conftest.py       # autouse fixture: monkeypatches LIBRIS_CONFIG_DIR → tmp_path
├── test_api.py       # GoogleBooksClient tests (mocked HTTP)
├── test_cli.py       # CLI integration tests
└── ...
```

## Build & Test Commands

```bash
uv sync --frozen              # Install dependencies (use --frozen in CI)
uv run pytest                 # Run tests (NEVER invoke pytest directly)
uv run pytest tests/test_api.py::test_search  # Run a single test
uv run pytest --cov=src/libris               # Run with coverage
uv run ruff check --fix .     # Lint and auto-fix (run before committing)
uv run ruff check .           # Lint check only (CI mode)
uv run ruff format .          # Format
uv run ruff format --check .  # Format check (CI mode)
```

`libris` on your PATH is a `uv tool` snapshot, not the working tree. It goes stale silently:
after landing a change, `uv tool install . --force` from the repo root refreshes it, and
`uv cache clean libris` first if a newly added flag is still missing. Check the installed file
rather than the install output, which says "Installed" either way:

```bash
uv run libris --help            # the working tree, always current
uv tool install . --force       # refresh the global command
```

## Workflow

- **Branching:** Always create a feature branch from `main`. Never commit directly to `main`.
- **Pre-commit:** Run `uv run ruff check --fix .` (auto-fix lint issues, including import sorting), `uv run ruff format .`, and `uv run pytest` before every commit. Fix any remaining failures. All three are needed: CI lints and checks formatting as separate steps, so a commit that ran only `ruff check` still fails `ruff format --check .`.
- **Versioning:** Bump `version` in `pyproject.toml` in any PR that changes behaviour a user can see, matching the change's conventional-commit type: `feat:` bumps the minor, `fix:` the patch. This is not bookkeeping. `uv` keys its build cache on the version, so shipping two different builds as the same version means `uv tool install . --force` reports success and installs the older one - which happened, and cost a debugging session before anyone thought to compare the installed file's timestamp. `extension/manifest.json` carries the same version and moves with it: the extension and the daemon ship together and share the `/api/v1` contract, so two numbers drifting apart makes "which extension works with which daemon" unanswerable.
- **Commit messages:** Use [Conventional Commits](https://www.conventionalcommits.org/):
  - `feat: add book export command`
  - `fix: handle missing ISBN in frontmatter`
  - `docs: update README with import examples`
  - `test: add coverage for retry logic`
  - `refactor: extract frontmatter parsing`
  - `chore: bump httpx dependency`
- **PRs:** Reference the GitHub Issue (e.g., `Closes #9`). Ask the user before creating a PR.

## Python Conventions

- **Type hints** on all new/modified function signatures. Prefer Python 3.12+ built-in generic syntax (`list[str]`, `str | None`) when touching code; legacy `typing.List`/`Optional` may exist and can be migrated opportunistically.
- **Google-style docstrings** on new/modified public functions and classes.
- **Data models:** Use `@dataclass` from stdlib (not Pydantic) for data structures.
- **HTTP:** Use `httpx.Client` inside context managers (`with` blocks).
- **Config:** All config via `config.py` — reads `~/.config/libris/config.yaml` or `$LIBRIS_CONFIG_DIR`.
- **Error handling:**
  - Define custom exceptions for domain errors (don't raise generic `Exception`).
  - Never use bare `except:` — always catch specific exception types.
  - Use `typer.echo()` for user-facing error messages in CLI code.
  - Let library code (`api.py`, `markdown.py`) raise exceptions; CLI layer catches and displays.
- **Ruff rules active:** `E` (pycodestyle), `F` (pyflakes), `I` (isort), `S` (bandit security). `S101` suppressed in `tests/**`.

## Testing

- **Framework:** pytest, run via `uv run pytest`
- **Isolation:** `conftest.py` has an `autouse` fixture that monkeypatches `LIBRIS_CONFIG_DIR` to `tmp_path` — tests never touch real `~/.config/libris`.
- **Structure:** Given-When-Then (BDD style):
  ```python
  def test_search_returns_books_for_valid_query(mock_http):
      # Given a mock API response with 3 books
      mock_http.return_value = fake_response(count=3)

      # When searching for "Dune"
      client = GoogleBooksClient()
      results = client.search("Dune")

      # Then 3 books are returned
      assert len(results) == 3
  ```
- **Naming:** Be descriptive — no strict pattern required, but names should clearly convey what's being tested.
- **Mocking:** Mock external HTTP calls (monkeypatch or `unittest.mock`). No live API calls in tests.
- **Coverage:** `uv run pytest --cov=src/libris` — aim for high coverage on library modules.

## Non-Interactive Shell Commands

**ALWAYS use non-interactive flags** with file operations to avoid hanging on confirmation prompts.

Shell commands like `cp`, `mv`, and `rm` may be aliased to include `-i` (interactive) mode on some systems, causing the agent to hang indefinitely waiting for y/n input.

```bash
cp -f source dest           # NOT: cp source dest
mv -f source dest           # NOT: mv source dest
rm -f file                  # NOT: rm file
rm -rf directory            # NOT: rm -r directory
```

Other commands that may prompt:
- `apt-get` — use `-y` flag
- `scp`/`ssh` — use `-o BatchMode=yes`
- `brew` — use `HOMEBREW_NO_AUTO_UPDATE=1`

## Skills

Reusable task-specific skills are in `.agents/skills/`. These are invoked on demand, not loaded every session.
