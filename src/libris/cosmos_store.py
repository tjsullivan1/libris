"""The remote replica in Cosmos: what `libris sync` pushes, and the store over it.

ADR 0006 puts the remote Library in Cosmos DB serverless, one document per Book
Note in `books`, partitioned by Libris ID. ADR 0032 and ADR 0033 say what each
document carries besides the note: the exact keys a store is queried on, and the
words its title and authors split into. ADR 0033 adds one more document, the word
counts per Status, rebuilt from scratch at the end of every push.

Every key and every count is computed by `store.stored_keys` and
`store.count_by_status`, the same functions `ReplicaStore` is built from, so
Cosmos and the replica the parity tests run against cannot compute one
differently.

The push and the store take container-shaped objects rather than reaching Azure
themselves, so the tests run them against a fake. Only `connect_containers`
imports the SDK, which is the `sync` extra; nothing else here needs it.

This push is a full one. Change detection and deletions are #174.
"""

import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from .markdown import BookNote
from .service import IdCollision, find_id_collisions
from .shelf import index_for
from .store import (
    Listing,
    StoredKeys,
    WordCounts,
    count_by_status,
    stored_keys,
)

BOOKS = "books"
WORD_COUNTS = "word_counts"

# The one document in `word_counts`. Its own container rather than a document in
# `books`, so no book query has to filter it out.
_COUNTS_ID = "word-counts"

# Cosmos's limits on one item. A Libris ID is a document's id, so it must keep
# to the id's rules; and a note past either limit would fail its write and stop
# the whole push, naming nothing, rather than be reported (#185 review).
_FORBIDDEN_IN_ID = set("/\\?#")
_MAX_ID_BYTES = 1023
_MAX_ITEM_BYTES = 2 * 1024 * 1024


class Container(Protocol):
    """The container calls the push and the store make."""

    def upsert_item(self, body: dict[str, Any]) -> Any: ...

    def query_items(
        self,
        query: str,
        parameters: list[dict[str, Any]] | None = None,
        enable_cross_partition_query: bool | None = None,
    ) -> Any: ...


class SyncRefused(Exception):
    """Sync would push two notes under one identity and keep only the last.

    ADR 0033: the remote must hold exactly the notes the local search reads, and
    a shared Libris ID makes that impossible. Nothing is pushed.
    """

    def __init__(self, collisions: list[IdCollision]) -> None:
        self.collisions = collisions
        super().__init__(f"{len(collisions)} Libris ID(s) are claimed by two notes")


class CountsNotRebuilt(Exception):
    """The notes went up, but the word counts did not.

    The remote then ranks against counts for a Shelf that no longer exists, so
    the sync has failed. The next one rebuilds them whether or not it pushes a
    note (ADR 0033).
    """


class CountsMissing(Exception):
    """The remote has no word counts, so it cannot weigh a search or total a listing.

    Not the same as an empty Library (ADR 0029). A sync that pushes nothing
    still writes counts, with no buckets, so their absence means no sync has
    finished - though one may have pushed books before its rebuild failed
    (#185 review). Answering zero would rank those books with no weights and
    report a total of none.
    """


class NotStorable(ValueError):
    """A note Cosmos cannot hold as it stands on the Shelf."""


@dataclass
class PushReport:
    """What a push sent, and every note it could not.

    A note left out is one the local search can find and the remote cannot, so
    each is named rather than skipped in silence (ADR 0033).
    """

    pushed: int = 0
    without_id: list[Path] = field(default_factory=list)
    unreadable: list[Path] = field(default_factory=list)
    not_storable: list[tuple[Path, str]] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        """Whether the remote now holds every note the Shelf's search reads."""
        return not (self.without_id or self.unreadable or self.not_storable)


def book_document(note: BookNote) -> dict[str, Any]:
    """The document sync stores for one Book Note.

    The whole note goes up - every frontmatter key, modelled or not, and the
    body (ADR 0005, ADR 0006) - next to the keys a store is queried on.

    Args:
        note: The note, read with its body.

    Returns:
        The document, keyed by the note's Libris ID.

    Raises:
        NotStorable: If the note has no Libris ID, one Cosmos refuses as an id,
            frontmatter that would not come back from JSON as it went in, or
            more than Cosmos holds in one item.
    """
    keys = stored_keys(note)
    if keys.libris_id is None:
        raise NotStorable("it has no Libris ID")
    if _FORBIDDEN_IN_ID & set(keys.libris_id):
        raise NotStorable(f"its Libris ID {keys.libris_id!r} holds / \\ ? or #")
    if len(keys.libris_id.encode("utf-8")) > _MAX_ID_BYTES:
        raise NotStorable(f"its Libris ID is longer than {_MAX_ID_BYTES} bytes")

    document = {
        "id": keys.libris_id,
        # Again as a plain field: the listing orders on it, and a composite
        # index is declared over ordinary paths, not Cosmos's own `id`.
        "libris_id": keys.libris_id,
        "filename": note.path.name,
        "frontmatter": note.frontmatter,
        "body": note.body,
        "superseded_ids": list(keys.superseded_ids),
        "isbn": keys.isbn,
        "google_books_id": keys.google_books_id,
        "title_key": keys.title_key,
        "author_key": keys.author_key,
        "titled": keys.title_key is not None,
        "words": sorted(keys.words),
        "status": keys.status,
    }
    # Passthrough keys are carried verbatim (ADR 0005). YAML can hold what JSON
    # cannot - a number as a key, which JSON turns into text - and a note that
    # would come back different is reported rather than quietly changed.
    try:
        text = json.dumps(document, allow_nan=False)
    except (TypeError, ValueError) as error:
        raise NotStorable(f"its frontmatter is not JSON: {error}") from None
    if json.loads(text) != document:
        raise NotStorable("its frontmatter would not come back from JSON unchanged")
    # Measured as it goes over the wire: the SDK writes compact JSON, without the
    # spaces `json.dumps` adds by default, and Cosmos counts UTF-8 bytes. Counting
    # the spaces rejected notes Cosmos would hold (#185 review).
    wire = json.dumps(document, separators=(",", ":"), ensure_ascii=False)
    if len(wire.encode("utf-8")) > _MAX_ITEM_BYTES:
        raise NotStorable("it is larger than the 2 MB Cosmos holds in one item")
    return document


def counts_document(buckets: dict[str | None, WordCounts]) -> dict[str, Any]:
    """The word-counts document, one entry per Status bucket (ADR 0033)."""
    return {
        "id": _COUNTS_ID,
        "buckets": [
            {"status": status, "total": counts.total, "counts": counts.counts}
            for status, counts in buckets.items()
        ],
    }


def push_shelf(vault_path: Path, books: Container, counts: Container) -> PushReport:
    """Push every Book Note on the Shelf, then rebuild the word counts.

    Pushes exactly the notes the local search reads - every note the Shelf's
    index holds - so the remote answers as the Shelf does (ADR 0033).

    Args:
        vault_path: The Shelf.
        books: The `books` container.
        counts: The `word_counts` container.

    Returns:
        How many notes went up, and which could not.

    Raises:
        SyncRefused: If two notes share a Libris ID. Nothing is pushed.
        CountsNotRebuilt: If the word counts could not be written.
    """
    collisions = find_id_collisions(vault_path)
    if collisions:
        raise SyncRefused(collisions)

    # Every note is read before any is written. The check above read the Shelf
    # once and this reads it again, so a note edited in between could arrive
    # holding another's ID and overwrite it. Checking the documents actually
    # about to go up closes that, and only an all-clear writes anything
    # (#185 review).
    index = index_for(vault_path)
    report = PushReport()
    documents: list[tuple[BookNote, dict[str, Any]]] = []
    for listed in index.notes():
        # The index read every note, but one can be locked, denied or removed
        # since. That is the same race `ShelfIndex` reports rather than raises,
        # and a crash here would push nothing and name nothing (#185 review).
        try:
            note = BookNote.read_whole(listed.path)
        except OSError:
            note = None
        if note is None:
            report.unreadable.append(listed.path)
            continue
        if note.libris_id is None:
            report.without_id.append(note.path)
            continue
        try:
            documents.append((note, book_document(note)))
        except NotStorable as error:
            report.not_storable.append((note.path, str(error)))
    # The index can still answer for a note it could not re-read, from an
    # earlier parse, so one path can be named by both.
    unreadable = set(report.unreadable) | set(index.unreadable)
    report.unreadable = sorted(unreadable, key=lambda path: path.name)

    collisions = _shared_ids(note for note, _ in documents)
    if collisions:
        raise SyncRefused(collisions)

    pushed: list[StoredKeys] = []
    for note, document in documents:
        books.upsert_item(document)
        pushed.append(stored_keys(note))
        report.pushed += 1

    # Over the notes pushed, not the Shelf: the counts describe what the remote
    # holds, and a note that could not go up is not there to be weighed.
    try:
        counts.upsert_item(counts_document(count_by_status(pushed)))
    except Exception as error:
        raise CountsNotRebuilt(str(error)) from error
    return report


def _shared_ids(notes: Iterable[BookNote]) -> list[IdCollision]:
    """The Libris IDs more than one of these notes holds, named as `find_id_collisions` names them."""
    by_id: dict[str, list[BookNote]] = {}
    for note in notes:
        if note.libris_id is not None:
            by_id.setdefault(note.libris_id, []).append(note)
    return [
        IdCollision(libris_id=libris_id, notes=sorted(held, key=lambda n: n.path.name))
        for libris_id, held in sorted(by_id.items())
        if len(held) > 1
    ]


def _note(document: dict[str, Any]) -> BookNote:
    # The path carries the filename only. `by_libris_id` breaks ties on it, and
    # no remote caller can open a file on the Shelf in any case.
    return BookNote(
        path=Path(document["filename"]),
        frontmatter=document["frontmatter"],
        body=document["body"],
    )


class CosmosStore:
    """A `LibraryStore` over what sync pushed, answered from the stored keys.

    Every question is one query on keys `stored_keys` computed, and word counts
    come from the one document the push rebuilt, so it answers as `ReplicaStore`
    does. It is read afresh on each question rather than held, because a sync
    can replace it under a long-running server.
    """

    def __init__(self, books: Container, counts: Container) -> None:
        self._books = books
        self._counts = counts

    def _query(
        self,
        where: list[str],
        parameters: dict[str, Any],
        order: str = "",
    ) -> list[dict[str, Any]]:
        # Every clause is a literal written in this class; every value a caller
        # supplies travels as a parameter, never in the text.
        query = "SELECT * FROM c WHERE " + " AND ".join(where) + order  # noqa: S608
        return list(
            self._books.query_items(
                query=query,
                parameters=[{"name": k, "value": v} for k, v in parameters.items()],
                enable_cross_partition_query=True,
            )
        )

    def _notes(self, where: list[str], parameters: dict[str, Any]) -> list[BookNote]:
        return [_note(d) for d in self._query(where, parameters)]

    @staticmethod
    def _in_status(
        where: list[str], parameters: dict[str, Any], status: str | None
    ) -> None:
        where.append("c.titled = true")
        if status is not None:
            where.append("c.status = @status")
            parameters["@status"] = status

    def with_libris_id(self, libris_id: str) -> list[BookNote]:
        return self._notes(["c.id = @id"], {"@id": libris_id})

    def superseding(self, libris_id: str) -> list[BookNote]:
        return self._notes(
            ["ARRAY_CONTAINS(c.superseded_ids, @id)"], {"@id": libris_id}
        )

    def with_isbn(self, isbn: str) -> list[BookNote]:
        return self._notes(["c.isbn = @isbn"], {"@isbn": isbn})

    def with_google_books_id(self, google_books_id: str) -> list[BookNote]:
        return self._notes(
            ["c.google_books_id = @google_books_id"],
            {"@google_books_id": google_books_id},
        )

    def with_title_and_author(self, title_key: str, author_key: str) -> list[BookNote]:
        return self._notes(
            ["c.title_key = @title_key", "c.author_key = @author_key"],
            {"@title_key": title_key, "@author_key": author_key},
        )

    def by_first_author(self, author_key: str) -> list[BookNote]:
        return self._notes(["c.author_key = @author_key"], {"@author_key": author_key})

    def carrying_words(self, words: set[str], status: str | None) -> list[BookNote]:
        if not words:
            return []
        where: list[str] = []
        parameters: dict[str, Any] = {"@words": sorted(words)}
        self._in_status(where, parameters, status)
        where.append(
            "EXISTS(SELECT VALUE w FROM w IN c.words WHERE ARRAY_CONTAINS(@words, w))"
        )
        return self._notes(where, parameters)

    def word_counts(self, status: str | None) -> WordCounts:
        found = list(
            self._counts.query_items(
                query="SELECT * FROM c WHERE c.id = @id",
                parameters=[{"name": "@id", "value": _COUNTS_ID}],
                enable_cross_partition_query=True,
            )
        )
        if not found:
            raise CountsMissing(
                "the remote Library has no word counts: no sync has finished. "
                "Run `libris sync`."
            )
        total = 0
        counts: dict[str, int] = {}
        for bucket in found[0]["buckets"]:
            if status is not None and bucket["status"] != status:
                continue
            total += bucket["total"]
            for word, count in bucket["counts"].items():
                counts[word] = counts.get(word, 0) + count
        return WordCounts(total=total, counts=counts)

    def listing(self, status: str | None, limit: int) -> Listing:
        where: list[str] = []
        parameters: dict[str, Any] = {"@limit": limit}
        self._in_status(where, parameters, status)
        # Every note the remote holds has a Libris ID and none shares one (sync
        # refuses otherwise), so the ID alone breaks a tie on title, as
        # `by_libris_id` does locally.
        documents = self._query(
            where,
            parameters,
            order=" ORDER BY c.title_key ASC, c.libris_id ASC OFFSET 0 LIMIT @limit",
        )
        return Listing(
            total=self.word_counts(status).total,
            notes=[_note(d) for d in documents],
        )


def connect_containers(endpoint: str, database: str) -> tuple[Container, Container]:
    """Reach `books` and `word_counts` as whoever `DefaultAzureCredential` finds.

    On the PC that is the person signed in with `az login`; in the Container App
    it is the app's managed identity. No connection string exists (ADR 0006).

    Raises:
        ImportError: If the `sync` extra is not installed.
    """
    from azure.cosmos import CosmosClient
    from azure.identity import DefaultAzureCredential

    client = CosmosClient(endpoint, credential=DefaultAzureCredential())
    db = client.get_database_client(database)
    return db.get_container_client(BOOKS), db.get_container_client(WORD_COUNTS)
