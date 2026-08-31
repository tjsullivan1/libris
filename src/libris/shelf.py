"""The Shelf's Book Notes, parsed once and revalidated against disk.

Every query used to read and YAML-parse all 3,061 notes. Measured against the
real Shelf that is about 8 seconds for one exact-match query and 13 for one
Near Match query, and `GET /api/v1/books` runs both - so a browser popup that
checks the Shelf twice in a flow spent the better part of a minute waiting.
Parsing is where it goes: reading all 4.6 MB takes 1.5 seconds and parsing the
frontmatter takes ten.

So the parse happens once per note per change, rather than once per note per
question.

This does not weaken the guarantee the daemon exists to give (ADR 0010). The
promise is that an answer is true at the moment it is given, and every call
re-reads the directory and re-parses whatever changed - it is a revalidated
index, never a cache with a lifetime. A note edited in Obsidian between two
questions is seen by the second one.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path

from .markdown import BookNote

# What distinguishes one version of a file from the next. `os.scandir` reports
# both without a second system call, which is what makes revalidating the whole
# Shelf cost about 30 milliseconds.
Fingerprint = tuple[int, int]


def _fingerprint(entry: os.DirEntry) -> Fingerprint:
    """Describe a file precisely enough to notice it changing."""
    info = entry.stat()
    return (info.st_mtime_ns, info.st_size)


@dataclass
class ShelfIndex:
    """Every Book Note on one Shelf, kept in step with the files.

    Held for the life of a process. A CLI command builds it, asks one question
    and throws it away, paying exactly what the old scan cost; the daemon keeps
    it and pays only for what changed.
    """

    vault_path: Path
    _notes: dict[str, BookNote] = field(default_factory=dict)
    _fingerprints: dict[str, Fingerprint] = field(default_factory=dict)
    # Files whose frontmatter would not parse. Remembered so a broken note is
    # not re-read on every question, and re-read the moment it is edited.
    _unparseable: dict[str, Fingerprint] = field(default_factory=dict)

    def notes(self) -> list[BookNote]:
        """Every Book Note on the Shelf, as it stands right now.

        Returns:
            The notes, in the order the filesystem reports them. Reparsed only
            where a file appeared, changed or went away since the last call.
        """
        seen: set[str] = set()

        try:
            entries = list(os.scandir(self.vault_path))
        except FileNotFoundError:
            # A Shelf that has gone away holds no books, which is the truthful
            # answer and lets the caller report a miss rather than crash.
            self._notes.clear()
            self._fingerprints.clear()
            self._unparseable.clear()
            return []

        for entry in entries:
            if not entry.name.endswith(".md") or not entry.is_file():
                continue
            seen.add(entry.name)

            fingerprint = _fingerprint(entry)
            if self._fingerprints.get(entry.name) == fingerprint:
                continue
            if self._unparseable.get(entry.name) == fingerprint:
                continue

            note = BookNote.read(Path(entry.path))
            if note is None:
                self._unparseable[entry.name] = fingerprint
                self._notes.pop(entry.name, None)
                self._fingerprints.pop(entry.name, None)
                continue

            self._notes[entry.name] = note
            self._fingerprints[entry.name] = fingerprint
            self._unparseable.pop(entry.name, None)

        for gone in self._notes.keys() - seen:
            del self._notes[gone]
            del self._fingerprints[gone]
        for gone in self._unparseable.keys() - seen:
            del self._unparseable[gone]

        return list(self._notes.values())


_INDEXES: dict[Path, ShelfIndex] = {}


def index_for(vault_path: Path) -> ShelfIndex:
    """Get the index for a Shelf, building it if this process has none.

    Keyed by path rather than held as one global, so a test pointing at a
    temporary Shelf never sees another one's notes.

    Args:
        vault_path: The Shelf to index.

    Returns:
        The index for that Shelf.
    """
    resolved = vault_path.resolve()
    index = _INDEXES.get(resolved)
    if index is None:
        index = ShelfIndex(vault_path=resolved)
        _INDEXES[resolved] = index
    return index


def forget_indexes() -> None:
    """Drop every index this process holds.

    For tests, which reuse one process across many temporary Shelves and can
    otherwise recreate a path a previous test already indexed.
    """
    _INDEXES.clear()
