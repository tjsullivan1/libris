# Workstreams

Ordered by dependency. Workstream 1 needs no Azure and blocks everything after it.

## 1. Vault migration and typing

Done as a single pass: the note migration and the typing ship together, so the 3,137 notes
are rewritten once rather than reasoned about in a half-migrated state. This blocks
workstreams 2-5 for its duration, accepted knowingly.

Order within the pass:

1. Put the repository under version control (see note below)
2. Introduce `BookNote` and `BookCandidate`; collapse `ImportBook` into the latter
3. Point `markdown.py`, `cli.py`, `merge.py` and `importer.py` at the types, fixing the
   field names as part of the move rather than as a separate edit
4. Update the tests that assert on dict shape; the 78 text assertions should pass untouched
5. Write the migration with `--dry-run` and run it against a copy
6. Take a fresh vault backup, apply, spot-check
7. Fix `Reading List.base`

Scope:

- Mint `libris_id` into all 3,137 Book Notes
- Add `priority` and `series` as modelled fields, null where unset
- Fix `DEFAULT_FRONTMATTER` and `_BOOK_TO_FRONTMATTER` to match the vault (ADR 0005)
- Fix `Reading List.base`, which sorts on `author`, `genre`, `Genre` and `Status` — none of
  which exist in any note
- Introduce a `BookNote` type: `libris_id`, path, typed frontmatter, body
- Rename `Book` to `BookCandidate`; collapse `ImportBook` into it with a `source` field
- Move `cli.py` off raw `fm.get(...)` dict access
- Restructure the note body: `## Notes` first on every note, description into a collapsed
  callout, frontmatter grouped (ADR 0009, closes #57)

The last three are the reason the first four were needed. There is no type for a book in the
Library: `list_books` returns `list[Path]`, `read_frontmatter` returns `Dict[str, Any]`,
`find_duplicates` returns `list[list[Path]]`, and `_build_vault_index` expresses the missing
concept anonymously as `Tuple[Path, Dict]`. Every field read is `fm.get("name")` against an
untyped dict, so `DEFAULT_FRONTMATTER` disagreeing with all 3,137 notes produced silent
nulls rather than an error.

Two live consequences, both caused by that:

- `fm.get("author")` is read at five sites — `cli.py:635`, `importer.py:134` and
  `markdown.py:259`, `:423`, `:512` — and is `None` at every one of them, because the notes
  hold `authors`. In `_build_query_from_frontmatter` that means every autoenrich query is
  title-only, with `inauthor:` never applied. Nineteen notes are titled "Poems".
- `_API_SOURCED_FIELDS` names `thumbnail` and `published_date`, neither of which exists.
  `_needs_enrichment` therefore tests two fields instead of four, and reports 152 notes as
  unenriched when only 5 are — a 97% false-positive rate on every run.

## 2. MCP tool surface

`search_books`, `add_book`, `update_book` over stdio, driven from Claude Code locally. No
Azure. This is where the design risk lives (ADR 0003).

## 3. Sync

`libris sync`, the Intent protocol, the Scheduled Task (ADR 0002).

Open: what happens to an Intent that cannot apply, and how sync detects changed notes.

## 4. Infrastructure

Terraform: Container Apps, ACR, Cosmos serverless, managed identity, `azuread_application`
(ADR 0006, ADR 0007).

## 5. Authentication

Entra, Streamable HTTP, OAuth with manually registered clients (ADR 0004).
