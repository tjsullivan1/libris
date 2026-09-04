"""The MCP tools, with no logic in them.

ADR 0008 puts resolution, creation and querying in `service.py`, so this module
translates and nothing else: it turns tool arguments into service calls and
service answers into structured results. The REST adapter next door does the
same job for a different caller.

The tool surface is deliberately small and reaches frontmatter only. A Book
Note's body holds a reader's own writing and stays in Obsidian (ADR 0023), so
there is no tool that reads one and none that writes one.
"""

from datetime import date
from pathlib import Path
from typing import Annotated, Literal

import httpx
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, Field

from . import config, installed_version, service
from .api import GoogleBooksClient
from .markdown import BookNote
from .note_format import (
    FORMAT_VALUES,
    PRIORITY_VALUES,
    STATUS_VALUES,
    InvalidFieldValue,
    read_formats,
)

# The vocabularies as the model sees them. Generated from note_format rather
# than restated, so the Library still defines its own values (ADR 0022): a tool
# schema is read before a call is composed, where a `fields` tool is one a model
# can simply not call.
#
# Subscripting Literal with a tuple is how Python passes it several arguments,
# so these are each exactly the four/three/three strings and not "the tuple" -
# `Literal[STATUS_VALUES] == Literal[*STATUS_VALUES]` is True. Written this way
# because the values are a runtime import; a reviewer has already read it as a
# bug once, hence this note.
Status = Literal[STATUS_VALUES]
Priority = Literal[PRIORITY_VALUES]
Format = Literal[FORMAT_VALUES]

INSTRUCTIONS = """\
Libris tracks which books someone has read, is reading, or means to read.

Search the Library before adding to it, and pass the words that identify a book
rather than the whole sentence someone said: "mistborn", not "that mistborn one
I just finished".

These tools reach a book's catalogue fields only. They cannot read or write the
notes someone has written about a book - those live in Obsidian.\
"""


# Deliberately no startup warm-up, unlike `libris serve`, which warms the index
# in `_warm_index`. The daemon can: it starts once, in the background, with
# nobody waiting. This server is spawned by an MCP client that is waiting for
# `initialize`, and parsing 3,063 notes takes 12-48 seconds (#94) - measured at
# 49.4s to connect with a blocking warm-up against 5s without one, of which
# about 2s is `uv run` and 4s is importing the SDK. A client that times out
# during initialize records the server as failed rather than slow, which is the
# worse failure: invisible, and indistinguishable from a broken adapter.
#
# So the cost sits on the first tool call, where a person can see it happening.
# Warming in a background thread would get both, but `ShelfIndex.notes()`
# mutates its caches unguarded, so a warm-up racing a tool call is a data race
# for a 40-second window - more machinery than this deserves while #94 is open.
# Fixing #94 (the loader swap alone takes the parse from 6.9s to 0.9s) makes a
# blocking warm-up affordable, and this comment obsolete.


def _shelf() -> Path:
    """Get the configured Shelf, or say how to configure one.

    The server starts without a Shelf on purpose. A stdio server that exits
    during startup reaches its client as a connection failure, with the reason
    on a stderr nobody reads; a tool that answers with the fix puts it in front
    of the person who can apply it.
    """
    try:
        if not config.is_vault_configured():
            raise config.VaultNotConfigured(
                "No Shelf is configured, so there is no Library to work with."
            )
        return config.get_vault_path()
    except config.VaultNotConfigured as exc:
        # Asked and caught, rather than either alone. get_vault_path refuses to
        # guess since #82, so the catch is what makes this correct; the question
        # in front of it keeps the message the tool's own, and covers a Shelf
        # unconfigured between the two calls. Getting this wrong is not a small
        # thing here: an MCP client spawns the server from whatever directory it
        # happens to be in, so a fallback to the working directory would write
        # Book Notes into a stranger's project folder.
        raise ToolError(f"{exc} Set one with: libris config --vault <path>") from None


def _text(value: object) -> str | None:
    """Render a frontmatter value as text, or None when it holds nothing.

    Frontmatter arrives from YAML, so a date field is a `date` and a rating is
    an `int`. Both are shown to a person rather than computed with.
    """
    if value is None or value == "":
        return None
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


class Book(BaseModel):
    """A Book Note as a tool reports it.

    Carries the reading state as well as the identity, so a model choosing
    between six Sandersons can tell them apart without six more calls.
    """

    libris_id: str | None
    title: str | None
    authors: list[str]
    status: str | None = None
    priority: str | None = None
    rating: str | None = None
    series: str | None = None
    format: list[str] = []
    date_started: str | None = None
    date_finished: str | None = None
    path: str | None = Field(
        default=None,
        description="Where the note lives. To show a person, not to pass back.",
    )

    @classmethod
    def of(cls, note: BookNote) -> "Book":
        """Build the tool form of a Book Note."""
        frontmatter = note.frontmatter
        # Read through the vocabulary's own reader rather than trusting the
        # shape on disk. `format` is a list in most notes and a bare string in
        # some - two on the Shelf today - and Obsidian writes the field where
        # Libris cannot guard it (ADR 0017), so the shape is not ours to assume.
        # Testing `isinstance(..., list)` reported no format at all for those
        # notes, and dropped the case repair besides.
        formats = read_formats(frontmatter.get("format"))
        return cls(
            libris_id=note.libris_id,
            title=note.title,
            authors=note.authors,
            status=_text(frontmatter.get("status")),
            priority=_text(frontmatter.get("priority")),
            rating=_text(frontmatter.get("rating")),
            series=_text(frontmatter.get("series")),
            format=formats,
            date_started=_text(frontmatter.get("date_started")),
            date_finished=_text(frontmatter.get("date_finished")),
            path=str(note.path),
        )


class SearchAnswer(BaseModel):
    """What the Library holds for a query, and how much of it was returned."""

    total: int = Field(description="Every match, not just the ones returned.")
    books: list[Book]


class Candidate(BaseModel):
    """A Book Candidate offered for adding.

    Deliberately without the description and cover: they are the two heaviest
    fields and neither helps anyone choose between editions. `add_book` fetches
    the volume again by id, so nothing here needs to survive the round trip
    (ADR 0025).
    """

    google_books_id: str = Field(description="Pass this to add_book.")
    title: str
    authors: list[str]
    published_date: str | None = None
    page_count: int | None = None
    isbn: str | None = None


class WriteAnswer(BaseModel):
    """What a write did, and what stands behind it."""

    outcome: str = Field(
        description=("created, already_present, needs_confirmation, or updated.")
    )
    book: Book | None = None
    guarantee: str = Field(
        default="live_shelf",
        description="What the duplicate check was made against.",
    )
    near_matches: list[Book] = Field(
        default=[],
        description="Books that stopped the write. Ask which, then set confirm.",
    )
    derived: dict[str, str] = Field(
        default={},
        description="Fields the Library set that you did not ask for.",
    )


def create_server(name: str = "libris") -> "MCPServer":
    """Build the MCP server and its tools.

    A factory rather than a module-level server, for the reason `create_app` is
    one: configuration is read at start rather than at import, and a test can
    build a server per configuration.
    """
    mcp = MCPServer(
        name=name,
        version=installed_version(),
        instructions=INSTRUCTIONS,
    )

    @mcp.tool()
    def search_library(
        query: Annotated[
            str | None,
            Field(
                description=(
                    "Words that identify a book - a title, an author, or both. "
                    "Omit to list by filter alone."
                )
            ),
        ] = None,
        status: Annotated[
            Status | None, Field(description="Narrow to one reading status.")
        ] = None,
        limit: Annotated[int, Field(ge=0, le=service.MAX_SEARCH_LIMIT)] = (
            service.DEFAULT_SEARCH_LIMIT
        ),
    ) -> SearchAnswer:
        """Find books already in the Library.

        Returns every plausible match rather than deciding between them, so
        check the titles before acting on one. `total` counts all matches; a
        large total with no obvious answer usually means the book is not held.
        """
        try:
            found = service.search_library(
                _shelf(), query=query, status=status, limit=limit
            )
        except InvalidFieldValue as exc:
            raise ToolError(str(exc)) from None
        return SearchAnswer(total=found.total, books=[Book.of(n) for n in found.books])

    @mcp.tool()
    def find_book(
        title: str | None = None,
        authors: list[str] | None = None,
        isbn: str | None = None,
    ) -> list[Candidate]:
        """Look up a book in Google Books, to add it to the Library.

        This searches the outside world, not the Library - use search_library
        for what is already held. Offer the candidates to the person and let
        them pick the edition; pass the chosen google_books_id to add_book.
        """
        try:
            candidates = service.lookup_candidates(
                isbn=isbn, title=title, authors=authors or None
            )
        except (httpx.HTTPStatusError, httpx.RequestError) as exc:
            # Only the upstream failures, matching the REST adapter. Catching
            # every Exception would report a bug in this code as an outage, and
            # a tool that never fails loudly is one nobody debugs.
            raise ToolError(f"Google Books could not be reached: {exc}") from None

        return [
            Candidate(
                google_books_id=c.google_books_id,
                title=c.title,
                authors=c.authors,
                published_date=c.published_date,
                page_count=c.page_count,
                isbn=c.isbn,
            )
            for c in candidates
            if c.google_books_id
        ]

    @mcp.tool()
    def add_book(
        google_books_id: Annotated[
            str, Field(description="From find_book. Never invented.")
        ],
        status: Status | None = None,
        priority: Priority | None = None,
        format: list[Format] | None = None,
        confirm: Annotated[
            bool,
            Field(
                description=(
                    "Set only after a person has seen near_matches and said "
                    "this is a different book."
                )
            ),
        ] = False,
    ) -> WriteAnswer:
        """Add a book to the Library.

        Stops and returns near_matches when the Library may already hold this
        book, because a duplicate found afterwards is a cleanup task rather than
        a question. Show them to the person, then call again with confirm.
        """
        shelf = _shelf()
        try:
            candidate = GoogleBooksClient().get_volume(google_books_id)
        except (httpx.HTTPStatusError, httpx.RequestError) as exc:
            # Both causes are named because the API cannot tell them apart for
            # us. Measured against the live service: an id that is well formed
            # but names nothing answers 404, while a malformed one answers 503
            # "Service temporarily unavailable". Reporting only the outage would
            # send someone looking at Google's status page for their own typo.
            raise ToolError(
                f"Could not fetch volume {google_books_id!r}: {exc}. Either "
                f"Google Books is unreachable, or that id is malformed - it "
                f"answers 503 rather than 404 for an id of the wrong shape. "
                f"Use find_book to get an id rather than composing one."
            ) from None
        if candidate is None:
            raise ToolError(
                f"Google Books has no volume {google_books_id!r}. Use find_book "
                f"to get an id rather than composing one."
            )

        overrides: dict[str, object] = {}
        if status is not None:
            overrides["status"] = status
        if priority is not None:
            overrides["priority"] = priority
        if format is not None:
            overrides["format"] = list(format)

        try:
            result = service.add_book(
                shelf,
                candidate,
                overrides=overrides or None,
                stop_on_near_match=not confirm,
            )
        except ValueError as exc:
            raise ToolError(str(exc)) from None

        return WriteAnswer(
            outcome=result.outcome.value,
            book=_written(shelf, result),
            near_matches=[Book.of(n) for n in result.near_matches],
        )

    @mcp.tool()
    def update_book(
        libris_id: Annotated[
            str, Field(description="From search_library. Never invented.")
        ],
        status: Status | None = None,
        priority: Priority | None = None,
        rating: int | None = None,
        format: list[Format] | None = None,
        date_started: Annotated[str | None, Field(description="YYYY-MM-DD.")] = None,
        date_finished: Annotated[str | None, Field(description="YYYY-MM-DD.")] = None,
    ) -> WriteAnswer:
        """Change a book's reading state.

        Only the fields given are changed; anything omitted is left alone.
        Marking a book Read dates it today unless you say otherwise, and says so
        in `derived` - relay that, because a person who finished it last week
        can only correct it now.
        """
        fields: dict[str, object] = {}
        for name, value in (
            ("status", status),
            ("priority", priority),
            ("rating", rating),
            ("format", list(format) if format is not None else None),
            ("date_started", date_started),
            ("date_finished", date_finished),
        ):
            if value is not None:
                fields[name] = value

        if not fields:
            raise ToolError("No fields were given, so there is nothing to change.")

        try:
            result = service.update_book(_shelf(), libris_id, fields)
        except service.BookNotFound as exc:
            raise ToolError(str(exc)) from None
        except ValueError as exc:
            # InvalidFieldValue subclasses ValueError, so this covers a value the
            # Library does not define and a field that is not the reader's.
            raise ToolError(str(exc)) from None

        return WriteAnswer(
            outcome="updated",
            book=Book.of(result.note),
            derived={k: str(v) for k, v in result.derived.items()},
        )

    return mcp


def _written(vault_path: Path, result: "service.AddResult") -> Book | None:
    """Describe the Book Note a write points at, when it points at one."""
    if result.libris_id is None or result.path is None:
        return None
    note = BookNote.read(result.path)
    if note is not None:
        return Book.of(note)
    return Book(
        libris_id=result.libris_id,
        title=result.title,
        authors=result.authors,
        path=str(result.path),
    )


def run() -> None:
    """Serve the tools over stdio until the client disconnects."""
    create_server().run(transport="stdio")
