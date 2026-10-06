from pathlib import Path

import pytest
from cosmos_fake import FakeContainer

from libris.cosmos_store import CosmosStore, push_shelf
from libris.store import ReplicaStore, ShelfStore

_WRITE_MODES = set("wax+")


@pytest.fixture(autouse=True)
def mock_config_dir(tmp_path, monkeypatch):
    """Automatically mock the configuration directory for all tests in the project."""
    config_dir = tmp_path / "libris_config"
    config_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("LIBRIS_CONFIG_DIR", str(config_dir))
    return config_dir


def pushed_to_cosmos(vault: Path) -> CosmosStore:
    """Push a Shelf to fake Cosmos containers, and open the store over them."""
    books, counts = FakeContainer(), FakeContainer()
    push_shelf(vault, books, counts)
    return CosmosStore(books, counts)


@pytest.fixture(params=["shelf", "replica", "cosmos"])
def open_store(request):
    """Open a store over a Shelf directory: as the Shelf, its replica, or Cosmos.

    A test taking this runs three times, so every question it asks is answered
    by the live Shelf, by a replica built from the same notes, and by Cosmos
    after a push of them (ADR 0020, ADR 0033). The other two are built when the
    test opens them, so open it after the notes are written.
    """

    def _open(vault):
        if request.param == "shelf":
            return ShelfStore(vault)
        if request.param == "replica":
            return ReplicaStore.from_shelf(vault)
        return pushed_to_cosmos(vault)

    return _open


@pytest.fixture
def lock_note(monkeypatch):
    """Refuse some kinds of access to one note, as a lock or a read-only file does.

    A locked, read-only or permission-denied note raises `PermissionError`, which
    is an `OSError` but not a `FileNotFoundError` (#165, #167). Refusing
    `Path.open` and `Path.unlink` for one path is what a real lock refuses, and
    works the same on every platform, unlike a test that changes permissions and
    has to skip on Windows.

    Returns:
        A function taking the note to refuse and keyword flags for what to
        refuse: `reads` (opening it to read), `writes` (opening it to write) and
        `deletes` (removing it).
    """
    real_open = Path.open
    real_unlink = Path.unlink
    refused: dict[Path, tuple[bool, bool, bool]] = {}

    def _open(self, mode="r", *args, **kwargs):
        reads, writes, _ = refused.get(self, (False, False, False))
        writing = bool(_WRITE_MODES & set(mode))
        if (writes and writing) or (reads and not writing):
            raise PermissionError(13, "Permission denied", str(self))
        return real_open(self, mode, *args, **kwargs)

    def _unlink(self, *args, **kwargs):
        if refused.get(self, (False, False, False))[2]:
            raise PermissionError(13, "Permission denied", str(self))
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", _open)
    monkeypatch.setattr(Path, "unlink", _unlink)

    def _lock(
        locked: Path,
        *,
        reads: bool = False,
        writes: bool = False,
        deletes: bool = False,
    ) -> None:
        refused[locked] = (reads, writes, deletes)

    return _lock
