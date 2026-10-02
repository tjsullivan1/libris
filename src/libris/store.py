"""The exact questions the service asks of wherever the Library is held.

ADR 0032 and ADR 0033 draw one seam: a store answers only exact questions - an
identifier, a normalized key, a word, a Status - and the service makes every
judgement over the answers. Stop words, weighting, containment, ordering and
limits are written once, in the service, so they cannot differ by location
(ADR 0020).

Two stores answer here. `ShelfStore` asks the live Shelf. `ReplicaStore` holds
what sync would push to the remote replica - each note's stored keys and words,
and word counts kept per Status - and answers from those alone. It stands in
for the remote store until there is one, so the guarantee that a query ranks
the same way in both locations can be tested rather than trusted (#157).
"""

from collections.abc import Iterable
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Protocol

from .markdown import BookNote
from .matching import normalize_for_match
from .shelf import index_for


def search_tokens(text: str) -> set[str]:
    """Split text into the normalized words a search compares.

    The one definition of how words are split. The replica stores each note's
    words at sync, computed here, so the two locations cannot split them
    differently (ADR 0033).
    """
    normalized = normalize_for_match(text)
    return set(normalized.split()) if normalized else set()


def note_words(note: BookNote) -> frozenset[str]:
    """The words a search compares a Book Note on: its title and its authors."""
    return _words(note.title or "", tuple(note.authors))


# A search on the Shelf splits every titled note's words twice - once to count
# them and once to find the notes carrying the query's - and then again for each
# note it ranks. Uncached, that was two thirds of the time a search took on the
# real Shelf. Keyed by the text rather than the note, so an edited note misses
# and is split afresh. Sized above the 3,084 notes the real Shelf holds.
@lru_cache(maxsize=8192)
def _words(title: str, authors: tuple[str, ...]) -> frozenset[str]:
    return frozenset(search_tokens(title) | search_tokens(" ".join(authors)))


def by_libris_id(note: BookNote) -> tuple[bool, str, str]:
    """Sort key putting the lowest Libris ID first and a note with none last.

    Every ordering ends on it (ADR 0033), in the service and in a store's
    listing alike, so two locations holding the same notes order them the same.
    Notes sharing an ID, or both lacking one, are then ordered by filename.
    Without it they kept the order they were read in, which is `scandir` order
    through the index and filename order through `list_books`, so a lookup and
    `build_id_index` could name different notes for one contested identity.
    """
    return (note.libris_id is None, note.libris_id or "", note.path.name)


def status_of(note: BookNote) -> str | None:
    """The Status bucket a note is counted in, or None for a note with none.

    A value that is not text (`status: [Read]`) equals no Status a filter can
    name, so it is counted with the notes that have none. It still counts
    towards the whole Library, as it does in the local search.
    """
    value = note.frontmatter.get("status")
    return value if isinstance(value, str) else None


@dataclass(frozen=True)
class WordCounts:
    """How many titled notes in a Status carry each word, and how many there are.

    `total` counts the notes, not the words: it is the `n` in ADR 0027's
    weighting, and the total a listing reports.
    """

    total: int
    counts: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class Listing:
    """One page of the titled notes in a Status, in title order, and how many there are."""

    total: int
    notes: list[BookNote] = field(default_factory=list)


class LibraryStore(Protocol):
    """The questions a store answers. None of them ranks, filters or decides.

    `status` narrows to the notes holding exactly that Status; None means every
    note, including those with no Status. The search questions cover only notes
    with a title. Normalized keys are computed by `normalize_for_match`.
    """

    def with_libris_id(self, libris_id: str) -> list[BookNote]:
        """The notes holding this Libris ID as their own."""
        ...

    def superseding(self, libris_id: str) -> list[BookNote]:
        """The notes that absorbed this Libris ID in a merge (ADR 0014)."""
        ...

    def with_isbn(self, isbn: str) -> list[BookNote]:
        """The notes carrying this ISBN, as `read_isbn` reads it."""
        ...

    def with_google_books_id(self, google_books_id: str) -> list[BookNote]:
        """The notes carrying this Google Books volume id."""
        ...

    def with_title_and_author(self, title_key: str, author_key: str) -> list[BookNote]:
        """The notes whose normalized title and first author are these."""
        ...

    def by_first_author(self, author_key: str) -> list[BookNote]:
        """The notes whose normalized first author is this."""
        ...

    def carrying_words(self, words: set[str], status: str | None) -> list[BookNote]:
        """The titled notes in a Status that carry any of these words."""
        ...

    def word_counts(self, status: str | None) -> WordCounts:
        """The word counts and note total over the titled notes in a Status."""
        ...

    def listing(self, status: str | None, limit: int) -> Listing:
        """The first `limit` titled notes in a Status, and their total.

        Ordered by normalized title, then by `by_libris_id`, so notes sharing a
        title come back in the same order from every store.
        """
        ...


def _in_status(note: BookNote, status: str | None) -> bool:
    return status is None or status_of(note) == status


def _title_order(note: BookNote) -> tuple[str, tuple[bool, str, str]]:
    return normalize_for_match(note.title or ""), by_libris_id(note)


def _count(notes: Iterable[BookNote]) -> WordCounts:
    total = 0
    counts: dict[str, int] = {}
    for note in notes:
        total += 1
        for word in note_words(note):
            counts[word] = counts.get(word, 0) + 1
    return WordCounts(total=total, counts=counts)


@dataclass
class ShelfStore:
    """The live Shelf, asked through its revalidated index.

    Every question reads the Shelf as it stands at that moment, so the answer is
    true when it is given (ADR 0010). Nothing is stored ahead: keys and words
    are computed from the notes on each question.

    Every note any question could not read is remembered for the life of the
    store, which is one operation: an answer is only as complete as the least
    complete scan behind it, and a later scan that happened to read a note does
    not make an earlier answer, given without it, complete (#168 review).
    """

    vault_path: Path
    _unread: set[Path] = field(default_factory=set, repr=False, compare=False)

    def _notes(self) -> list[BookNote]:
        index = index_for(self.vault_path)
        notes = index.notes()
        self._unread.update(index.unreadable)
        return notes

    def unreadable(self) -> list[Path]:
        """The notes this store's questions could not read as they stood.

        Scans once more as well, so a store asked nothing yet - a lookup with
        nothing to look up by - still has an answer of its own rather than
        another request's, or none (#168 review). A scan costs a stat per note;
        only notes that changed are read.

        Returns:
            Their paths, in name order. Nothing these questions answered has
            ruled them out, and a match on one of them may be a stale parse.
        """
        self._notes()
        return sorted(self._unread, key=lambda path: path.name)

    def _titled(self, status: str | None) -> list[BookNote]:
        return [n for n in self._notes() if n.title and _in_status(n, status)]

    def with_libris_id(self, libris_id: str) -> list[BookNote]:
        return [n for n in self._notes() if n.libris_id == libris_id]

    def superseding(self, libris_id: str) -> list[BookNote]:
        return [n for n in self._notes() if libris_id in n.superseded_ids]

    def with_isbn(self, isbn: str) -> list[BookNote]:
        return [n for n in self._notes() if n.isbn == isbn]

    def with_google_books_id(self, google_books_id: str) -> list[BookNote]:
        return [
            n
            for n in self._notes()
            if n.frontmatter.get("google_books_id") == google_books_id
        ]

    def with_title_and_author(self, title_key: str, author_key: str) -> list[BookNote]:
        return [
            n
            for n in self._notes()
            if n.title
            and n.first_author
            and normalize_for_match(n.title) == title_key
            and normalize_for_match(n.first_author) == author_key
        ]

    def by_first_author(self, author_key: str) -> list[BookNote]:
        return [
            n
            for n in self._notes()
            if n.first_author and normalize_for_match(n.first_author) == author_key
        ]

    def carrying_words(self, words: set[str], status: str | None) -> list[BookNote]:
        return [n for n in self._titled(status) if note_words(n) & words]

    def word_counts(self, status: str | None) -> WordCounts:
        return _count(self._titled(status))

    def listing(self, status: str | None, limit: int) -> Listing:
        # Not tokenized: listing "To Read" walks 1,452 notes on the real Shelf,
        # and splitting every title only to sort them alphabetically is work
        # with no reader.
        titled = self._titled(status)
        titled.sort(key=_title_order)
        return Listing(total=len(titled), notes=titled[:limit])


@dataclass(frozen=True)
class _Document:
    """One note as the remote replica stores it: the note and the keys sync computed."""

    note: BookNote
    libris_id: str | None
    superseded_ids: tuple[str, ...]
    isbn: str | None
    google_books_id: object
    title_key: str | None
    author_key: str | None
    words: frozenset[str]
    status: str | None


@dataclass
class ReplicaStore:
    """What the remote replica would hold after a sync, answered from stored keys.

    Built once from a set of Book Notes, as sync builds the remote from the
    Shelf. Each question compares stored keys only, and word counts come from
    per-Status buckets built at the same time (ADR 0033): the whole Library's
    counts are the sum of every bucket, including the one for notes with no
    Status. It never looks at a note's fields after it is built, so a store
    that answered from the notes instead of from what the remote can hold
    would not pass for it.
    """

    documents: list[_Document]
    buckets: dict[str | None, WordCounts]

    @classmethod
    def from_notes(cls, notes: Iterable[BookNote]) -> "ReplicaStore":
        """Build the replica sync would push for these notes."""
        documents = []
        for note in notes:
            documents.append(
                _Document(
                    note=note,
                    libris_id=note.libris_id,
                    superseded_ids=tuple(note.superseded_ids),
                    isbn=note.isbn,
                    google_books_id=note.frontmatter.get("google_books_id"),
                    title_key=normalize_for_match(note.title) if note.title else None,
                    author_key=(
                        normalize_for_match(note.first_author)
                        if note.first_author
                        else None
                    ),
                    words=note_words(note) if note.title else frozenset(),
                    status=status_of(note),
                )
            )

        buckets: dict[str | None, WordCounts] = {}
        for status in {d.status for d in documents if d.title_key is not None}:
            buckets[status] = _count(
                d.note
                for d in documents
                if d.title_key is not None and d.status == status
            )
        return cls(documents=documents, buckets=buckets)

    @classmethod
    def from_shelf(cls, vault_path: Path) -> "ReplicaStore":
        """Build the replica sync would push from the Shelf as it stands now."""
        return cls.from_notes(index_for(vault_path).notes())

    def _titled(self, status: str | None) -> list[_Document]:
        return [
            d
            for d in self.documents
            if d.title_key is not None and (status is None or d.status == status)
        ]

    def with_libris_id(self, libris_id: str) -> list[BookNote]:
        return [d.note for d in self.documents if d.libris_id == libris_id]

    def superseding(self, libris_id: str) -> list[BookNote]:
        return [d.note for d in self.documents if libris_id in d.superseded_ids]

    def with_isbn(self, isbn: str) -> list[BookNote]:
        return [d.note for d in self.documents if d.isbn == isbn]

    def with_google_books_id(self, google_books_id: str) -> list[BookNote]:
        return [d.note for d in self.documents if d.google_books_id == google_books_id]

    def with_title_and_author(self, title_key: str, author_key: str) -> list[BookNote]:
        return [
            d.note
            for d in self.documents
            if d.title_key == title_key and d.author_key == author_key
        ]

    def by_first_author(self, author_key: str) -> list[BookNote]:
        return [d.note for d in self.documents if d.author_key == author_key]

    def carrying_words(self, words: set[str], status: str | None) -> list[BookNote]:
        return [d.note for d in self._titled(status) if d.words & words]

    def word_counts(self, status: str | None) -> WordCounts:
        if status is not None:
            return self.buckets.get(status, WordCounts(total=0))
        total = 0
        counts: dict[str, int] = {}
        for bucket in self.buckets.values():
            total += bucket.total
            for word, count in bucket.counts.items():
                counts[word] = counts.get(word, 0) + count
        return WordCounts(total=total, counts=counts)

    def listing(self, status: str | None, limit: int) -> Listing:
        titled = sorted(
            self._titled(status),
            key=lambda d: (d.title_key or "", by_libris_id(d.note)),
        )
        return Listing(
            total=self.word_counts(status).total,
            notes=[d.note for d in titled[:limit]],
        )
