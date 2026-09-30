import pytest

from libris.store import ReplicaStore, ShelfStore


@pytest.fixture(autouse=True)
def mock_config_dir(tmp_path, monkeypatch):
    """Automatically mock the configuration directory for all tests in the project."""
    config_dir = tmp_path / "libris_config"
    config_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("LIBRIS_CONFIG_DIR", str(config_dir))
    return config_dir


@pytest.fixture(params=["shelf", "replica"])
def open_store(request):
    """Open a store over a Shelf directory, as the Shelf or as its replica.

    A test taking this runs twice, so every question it asks is answered once by
    the live Shelf and once by a replica built from the same notes, as sync
    would build the remote one (ADR 0020, ADR 0033). The replica is built when
    the test opens it, so open it after the notes are written.
    """

    def _open(vault):
        if request.param == "shelf":
            return ShelfStore(vault)
        return ReplicaStore.from_shelf(vault)

    return _open
