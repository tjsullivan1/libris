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


def _describe(entry: os.DirEntry) -> Fingerprint | None:
    """Describe a file precisely enough to notice it changing.

    Args:
        entry: A directory entry from the Shelf.

    Returns:
        The fingerprint, or None if this is not a readable file. `stat` and
        `is_file` both reach the filesystem and can fail on an entry that is
        being written or removed while the Shelf is listed, which is ordinary
        on a vault that Obsidian and a sync client also write to.
    """
    try:
        if not entry.is_file():
            return None
        info = entry.stat()
    except OSError:
        return None
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
            The notes, in the order the directory listed them. Reparsed only
            where a file appeared, changed or went away since the last call.
        """
        seen: set[str] = set()
        found: list[BookNote] = []

        try:
            # Closed deterministically rather than left to exhaustion: if the
            # listing raises partway, the directory handle would otherwise be
            # held until the collector got to it.
            with os.scandir(self.vault_path) as scan:
                listing = [(entry.name, entry.path, _describe(entry)) for entry in scan]
        except FileNotFoundError:
            # A Shelf that has gone away holds no books, which is the truthful
            # answer and lets the caller report a miss rather than crash.
            self._notes.clear()
            self._fingerprints.clear()
            self._unparseable.clear()
            return []

        for name, path, fingerprint in listing:
            if fingerprint is None or not name.endswith(".md"):
                continue
            seen.add(name)

            cached = self._notes.get(name)
            if cached is not None and self._fingerprints.get(name) == fingerprint:
                found.append(cached)
                continue
            if self._unparseable.get(name) == fingerprint:
                continue

            try:
                note = BookNote.read(Path(path))
            except OSError:
                # The file moved, vanished or was locked between the listing and
                # the read - Obsidian saving, a sync client, or Libris itself.
                # The last parse stands rather than the Book being reported
                # absent: a duplicate check that misses writes a second note and
                # nothing ever surfaces it, where a momentarily stale title
                # harms nobody. The fingerprint is left alone, so the next call
                # tries again rather than trusting what it could not read.
                if cached is not None:
                    found.append(cached)
                continue

            if note is None:
                self._unparseable[name] = fingerprint
                self._notes.pop(name, None)
                self._fingerprints.pop(name, None)
                continue

            self._notes[name] = note
            self._fingerprints[name] = fingerprint
            self._unparseable.pop(name, None)
            found.append(note)

        for gone in self._notes.keys() - seen:
            del self._notes[gone]
            del self._fingerprints[gone]
        for gone in self._unparseable.keys() - seen:
            del self._unparseable[gone]

        return found


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
