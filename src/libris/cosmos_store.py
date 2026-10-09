"""The remote replica in Cosmos: what `libris sync` pushes, and the store over it.

ADR 0006 puts the remote Library in Cosmos DB serverless, one document per Book
Note in `books`, partitioned by Libris ID. ADR 0032 and ADR 0033 say what each
document carries besides the note: the exact keys a store is queried on, and the
words its title and authors split into. ADR 0033 adds one more document, the word
counts per Status, rebuilt from scratch at the end of every push. ADR 0015 says
which notes a push sends: those whose document changed since this PC last sent
it, as a state file in the config directory records, with the remote documents
of notes that left the Shelf deleted.

Every key and every count is computed by `store.stored_keys` and
`store.count_by_status`, the same functions `ReplicaStore` is built from, so
Cosmos and the replica the parity tests run against cannot compute one
differently.

The push and the store take container-shaped objects rather than reaching Azure
themselves, so the tests run them against a fake. Only `connect_containers`
imports the SDK, which is the `sync` extra; nothing else here needs it.
"""

import contextlib
import hashlib
import json
import os
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from .config import get_config_dir
from .markdown import BookNote
from .service import IdCollision, find_id_collisions
from .shelf import index_for
from .store import (
    SPLIT_VERSION,
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

    def delete_item(self, item: str, partition_key: Any) -> Any: ...

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


class DeletionsRefused(Exception):
    """Sync would delete more of the remote than a Shelf plausibly loses at once.

    An unmounted drive or a half-finished Obsidian Sync looks like every note
    leaving, and would empty the remote Library (ADR 0015). Nothing is written.
    """

    def __init__(self, deleting: int, held: int, scanned_empty: bool) -> None:
        self.deleting = deleting
        self.held = held
        self.scanned_empty = scanned_empty
        super().__init__(
            f"sync would delete {deleting} of the {held} notes the remote holds"
        )


class NotStorable(ValueError):
    """A note Cosmos cannot hold as it stands on the Shelf."""


@dataclass
class PushReport:
    """What a push sent, and every note it could not.

    A note left out is one the local search can find and the remote cannot, so
    each is named rather than skipped in silence (ADR 0033).
    """

    pushed: int = 0
    unchanged: int = 0
    deleted: int = 0
    # Notes that left the Shelf but were not deleted, because a note that could
    # not be read might have been any of them.
    deletions_held: int = 0
    # Every note went up because the remote's words were split another way.
    repushed_all: bool = False
    without_id: list[Path] = field(default_factory=list)
    unreadable: list[Path] = field(default_factory=list)
    # `.md` files whose frontmatter would not parse. Not searchable on the
    # Shelf either, but one may be a note an edit broke, so they hold back
    # deletions just as an unreadable note does.
    unparseable: list[Path] = field(default_factory=list)
    not_storable: list[tuple[Path, str]] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        """Whether the remote now holds exactly the notes the Shelf's search reads."""
        return not (
            self.without_id
            or self.unreadable
            or self.unparseable
            or self.not_storable
            or self.deletions_held
        )


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
        "title_order": _code_point_order(keys.title_key),
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
    # Measured exactly as the SDK sends it: compact, without the spaces
    # `json.dumps` adds by default, and with non-ASCII escaped, as every write
    # is unless the client opts into `enable_compact_utf8_item_writes`, which
    # `connect_containers` does not. Counting the spaces refused notes Cosmos
    # would hold; counting "é" as two UTF-8 bytes rather than the six of "é"
    # let through notes it would refuse (#185 review).
    wire = json.dumps(document, separators=(",", ":"))
    if len(wire) > _MAX_ITEM_BYTES:
        raise NotStorable("it is larger than the 2 MB Cosmos holds in one item")
    return document


def _code_point_order(title_key: str | None) -> str | None:
    """A title key spelled so any string ordering sorts it by code point.

    The local stores sort titles in Python, by code point. Cosmos orders strings
    by its own rules - by UTF-16 code unit, per #185 review, which would put a
    character past U+FFFF before U+F900 - so ordering on the title itself could
    cut a limited listing at a different book. Each code point as six hex digits is
    ASCII of fixed width, which every ordering sorts the same, and sorts in the
    order Python gives the title.
    """
    if title_key is None:
        return None
    return "".join(f"{ord(character):06x}" for character in title_key)


def counts_document(buckets: dict[str | None, WordCounts]) -> dict[str, Any]:
    """The word-counts document, one entry per Status bucket (ADR 0033).

    Records the `SPLIT_VERSION` the words were split by. The buckets are in a
    fixed order, so a Shelf that has not changed rebuilds a document equal to
    the one stored, and a sync can tell it need not write it.
    """
    return {
        "id": _COUNTS_ID,
        "split_version": SPLIT_VERSION,
        "buckets": [
            {"status": status, "total": counts.total, "counts": counts.counts}
            for status, counts in sorted(
                buckets.items(), key=lambda item: (item[0] is not None, item[0] or "")
            )
        ],
    }


def push_shelf(
    vault_path: Path,
    books: Container,
    counts: Container,
    *,
    target: str = "",
    allow_mass_deletion: bool = False,
) -> PushReport:
    """Push the Book Notes that changed, delete those that left, rebuild the counts.

    The remote ends up holding exactly the notes the local search reads - every
    note the Shelf's index holds - so it answers as the Shelf does (ADR 0033).
    What it already holds is known from the state file (ADR 0015), unless the
    remote's word counts record a different way of splitting words, or none,
    and then every note goes up.

    Args:
        vault_path: The Shelf.
        books: The `books` container.
        counts: The `word_counts` container.
        target: Names the account and database being pushed to. State recorded
            for one is not trusted for another.
        allow_mass_deletion: Delete however many notes have left the Shelf,
            rather than refusing a number that looks like a missing drive.

    Returns:
        What went up, what was deleted, and every note that could not go up.

    Raises:
        SyncRefused: If two notes share a Libris ID. Nothing is written.
        DeletionsRefused: If the Shelf scans empty or implausibly many notes
            have left it. Nothing is written.
        CountsNotRebuilt: If the word counts could not be written.
        SyncInProgress: If another sync on this PC is running. Nothing is
            written.
    """
    # One sync at a time, from reading the state to saving it. Two interleaved
    # could leave Cosmos holding one run's document while the state records the
    # other's digest, and every later sync would skip the stale copy (#195
    # review). The scheduled task's own runs never overlap, but a sync typed
    # while it runs would.
    with _sync_lock():
        return _push_shelf(
            vault_path,
            books,
            counts,
            target=target,
            allow_mass_deletion=allow_mass_deletion,
        )


class SyncInProgress(Exception):
    """Another `libris sync` on this PC holds the lock. Nothing was written."""


@contextlib.contextmanager
def _sync_lock() -> Iterator[None]:
    # An operating-system lock rather than a file that exists or not: the lock
    # goes when the process does, so a crash cannot leave every later sync
    # refused.
    path = get_config_dir() / "sync.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+b") as handle:
        try:
            _lock(handle)
        except OSError:
            raise SyncInProgress(
                "another `libris sync` is running on this PC"
            ) from None
        try:
            yield
        finally:
            _unlock(handle)


if os.name == "nt":
    import msvcrt

    def _lock(handle: Any) -> None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)

    def _unlock(handle: Any) -> None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _lock(handle: Any) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(handle: Any) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _push_shelf(
    vault_path: Path,
    books: Container,
    counts: Container,
    *,
    target: str,
    allow_mass_deletion: bool,
) -> PushReport:
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
    # Every Libris ID still on the Shelf, including notes that cannot go up:
    # one Cosmos cannot hold now may be one it held before, and it has not left.
    present: set[str] = set()
    # Once: each call rescans the Shelf, and resets what it could not read.
    on_shelf = index.notes()
    for listed in on_shelf:
        # The index read every note, but one can be locked, denied or removed
        # since. That is the same race `ShelfIndex` reports rather than raises,
        # and a crash here would push nothing and name nothing (#185 review).
        try:
            note = BookNote.read_whole(listed.path)
        except OSError:
            note = None
        if note is None:
            report.unreadable.append(listed.path)
            # The index's earlier reading is the best word on what this note
            # is. Only then: a note read afresh may have changed its ID since,
            # and the ID it gave up has left the Shelf (#195 review).
            if listed.libris_id is not None:
                present.add(listed.libris_id)
            continue
        if note.libris_id is None:
            report.without_id.append(note.path)
            continue
        present.add(note.libris_id)
        try:
            documents.append((note, book_document(note)))
        except NotStorable as error:
            report.not_storable.append((note.path, str(error)))
    # The index can still answer for a note it could not re-read, from an
    # earlier parse, so one path can be named by both.
    unreadable = set(report.unreadable) | set(index.unreadable)
    report.unreadable = sorted(unreadable, key=lambda path: path.name)
    report.unparseable = sorted(index.unparseable, key=lambda path: path.name)

    collisions = _shared_ids(note for note, _ in documents)
    if collisions:
        raise SyncRefused(collisions)

    state = SyncState.load(target)
    remote = _counts_on_remote(counts)
    # No counts means no sync has finished there (`CountsMissing`): a new or
    # emptied account, which the state cannot speak for.
    report.repushed_all = (
        remote is not None and remote.get("split_version") != SPLIT_VERSION
    )
    if remote is None or report.repushed_all or not state.trusted:
        # Every document goes up, so no recorded hash may match. And the state
        # cannot be the inventory: a lost file, or one written for another
        # account, forgets documents the remote still holds, and remembers ones
        # a recreated container no longer does. So the remote is asked, and
        # what it answers replaces the state's IDs outright (#195 review).
        # Asked only when everything goes up anyway, so an ordinary sync pays
        # nothing.
        state.hashes = dict.fromkeys(_ids_on_remote(books), "")

    leaving = sorted(set(state.hashes) - present)
    if not allow_mass_deletion:
        # Whatever the count, and whether or not the IDs held are known: an
        # empty Shelf would otherwise rebuild the counts as empty, and hide
        # every book the remote holds (#195 review).
        # A scan that found files it could not read or parse is not empty: it
        # is incomplete, and the hold below names those files (#195 review).
        incomplete = bool(unreadable or report.unparseable)
        scanned_empty = not on_shelf and not incomplete and bool(state.hashes)
        if scanned_empty or len(leaving) > _plausible_deletions(len(state.hashes)):
            raise DeletionsRefused(
                deleting=len(leaving),
                held=len(state.hashes),
                scanned_empty=scanned_empty,
            )
    # A note that cannot be read may be any of the ones leaving, so none is
    # deleted until every note on the Shelf can be. A file that will not parse
    # is the same: a note whose frontmatter an edit broke looks exactly like a
    # note that left, and deleting it for a typo is not a sync's call.
    if unreadable or report.unparseable:
        report.deletions_held = len(leaving)
        leaving = []

    try:
        for note, document in documents:
            digest = _digest(document)
            if state.hashes.get(document["id"]) == digest:
                report.unchanged += 1
                continue
            books.upsert_item(document)
            state.hashes[document["id"]] = digest
            report.pushed += 1
        for libris_id in leaving:
            _delete(books, libris_id)
            del state.hashes[libris_id]
            report.deleted += 1
    finally:
        # Whatever went up is recorded, so a sync that fails partway resends
        # only what it did not get to.
        state.save()

    # Over every note the remote now holds, not only those read this run: one
    # unchanged since the last sync is there to be weighed, and so is one kept
    # because its note could not be read or stored, or because its deletion
    # waited (#195 review). Kept documents are fetched, which is none on a
    # Shelf that reads and stores cleanly.
    sent = {document["id"] for _, document in documents}
    kept = [
        _keys_as_stored(document)
        for document in _held_documents(books, set(state.hashes) - sent)
    ]
    rebuilt = counts_document(
        count_by_status([stored_keys(n) for n, _ in documents] + kept)
    )
    # A hash left empty is a document this code has not sent: a full push kept
    # it rather than replacing it. Its words may be split the old way, so the
    # version stays as it was, and the next sync tries the full push again.
    if any(not digest for digest in state.hashes.values()):
        rebuilt["split_version"] = (remote or {}).get("split_version")
    if remote is None or any(
        remote.get(key) != value for key, value in rebuilt.items()
    ):
        try:
            counts.upsert_item(rebuilt)
        except Exception as error:
            raise CountsNotRebuilt(str(error)) from error
    return report


# Removing a few notes, or merging a run of duplicates, deletes a handful. A
# Shelf that has lost a tenth of itself since the last sync is more likely an
# unmounted drive or a half-finished Obsidian Sync (ADR 0015).
_DELETIONS_ALWAYS_PLAUSIBLE = 20


def _plausible_deletions(held: int) -> int:
    return max(_DELETIONS_ALWAYS_PLAUSIBLE, held // 10)


def _digest(document: dict[str, Any]) -> str:
    # The document, not the file: it carries the filename, so a rename is a
    # change, and the words, so a change to how they are split is one too.
    text = json.dumps(document, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _counts_on_remote(counts: Container) -> dict[str, Any] | None:
    found = list(
        counts.query_items(
            query="SELECT * FROM c WHERE c.id = @id",
            parameters=[{"name": "@id", "value": _COUNTS_ID}],
            enable_cross_partition_query=True,
        )
    )
    return found[0] if found else None


def _ids_on_remote(books: Container) -> list[str]:
    return list(
        books.query_items(
            query="SELECT VALUE c.id FROM c", enable_cross_partition_query=True
        )
    )


def _keys_as_stored(document: dict[str, Any]) -> StoredKeys:
    """A kept document's keys as Cosmos holds them, not as this code would split them.

    During a split-version change a kept document still holds words split the
    old way, and those are what it is queried on (#195 review).
    """
    return StoredKeys(
        note=_note(document),
        libris_id=document["id"],
        superseded_ids=tuple(document.get("superseded_ids") or ()),
        isbn=document.get("isbn"),
        google_books_id=document.get("google_books_id"),
        title_key=document.get("title_key"),
        author_key=document.get("author_key"),
        words=frozenset(document.get("words") or ()),
        status=document.get("status"),
    )


# Kept documents are fetched this many to a query, not one each: a Shelf with
# hundreds of broken notes would otherwise make hundreds of round trips every
# sync (#195 review).
_HELD_PER_QUERY = 100


def _held_documents(books: Container, ids: set[str]) -> list[dict[str, Any]]:
    wanted = sorted(ids)
    found: list[dict[str, Any]] = []
    for start in range(0, len(wanted), _HELD_PER_QUERY):
        found.extend(
            books.query_items(
                query="SELECT * FROM c WHERE ARRAY_CONTAINS(@ids, c.id)",
                parameters=[
                    {"name": "@ids", "value": wanted[start : start + _HELD_PER_QUERY]}
                ],
                enable_cross_partition_query=True,
            )
        )
    return found


def _delete(books: Container, libris_id: str) -> None:
    try:
        books.delete_item(libris_id, partition_key=libris_id)
    except Exception as error:
        # Already gone is what a delete wants. The SDK's not-found error is
        # known by its status alone here, so this module need not import it.
        if getattr(error, "status_code", None) != 404:
            raise


@dataclass
class SyncState:
    """What the remote holds, as this PC last pushed it (ADR 0015).

    Keyed by Libris ID, because paths move. Kept in Libris's config directory,
    not the Vault: it describes one PC's syncs and has no business on a phone.
    Lost, unreadable or written for another target, it is empty and not
    `trusted`, and the next sync pushes everything and asks the remote what it
    holds.
    """

    target: str
    hashes: dict[str, str] = field(default_factory=dict)
    trusted: bool = False

    @staticmethod
    def path() -> Path:
        return get_config_dir() / "sync-state.json"

    @classmethod
    def load(cls, target: str) -> "SyncState":
        try:
            recorded = json.loads(cls.path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls(target)
        if not isinstance(recorded, dict) or recorded.get("target") != target:
            return cls(target)
        hashes = recorded.get("hashes")
        if not isinstance(hashes, dict):
            return cls(target)
        return cls(target, {str(k): str(v) for k, v in hashes.items()}, trusted=True)

    def save(self) -> None:
        path = self.path()
        path.parent.mkdir(parents=True, exist_ok=True)
        # Written beside and moved over, so a crash mid-write leaves the old
        # state rather than half a file the next sync would read as none.
        temporary = path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps({"target": self.target, "hashes": self.hashes}, indent=1),
            encoding="utf-8",
        )
        os.replace(temporary, path)


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
            order=" ORDER BY c.title_order ASC, c.libris_id ASC OFFSET 0 LIMIT @limit",
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
