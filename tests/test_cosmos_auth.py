"""The Cosmos store's own logic, against a container that behaves like Cosmos.

The authorization tests run on `MemoryAuthStore`, so nothing there exercises
how `CosmosAuthStore` reads Cosmos's answers. The one that matters most is the
lost race in `take`: a refresh token, code or sign-in is single use only
because the etag-conditioned delete refuses a second taker, and Cosmos reports
that refusal as its own exception, not the azure-core one a reader would guess.
The first version caught the wrong one, so these tests raise the real types.
"""

import time

import pytest

pytest.importorskip("azure.cosmos")

from azure.cosmos.exceptions import (  # noqa: E402
    CosmosAccessConditionFailedError,
    CosmosResourceNotFoundError,
)

from libris.cosmos_auth import CosmosAuthStore  # noqa: E402


class FakeContainer:
    """The four container calls the store makes, with etags as Cosmos keeps them."""

    def __init__(self) -> None:
        self.items: dict[str, dict] = {}
        self._version = 0
        # Lets a test slip another taker's delete in between a read and a delete.
        self.before_delete = lambda item_id: None

    def upsert_item(self, body: dict) -> None:
        self._version += 1
        self.items[body["id"]] = {**body, "_etag": f'"{self._version}"'}

    def read_item(self, item: str, partition_key: str) -> dict:
        assert partition_key == item
        if item not in self.items:
            raise CosmosResourceNotFoundError(status_code=404, message="gone")
        return dict(self.items[item])

    def delete_item(
        self,
        item: str,
        partition_key: str,
        etag: str | None = None,
        match_condition: object | None = None,
    ) -> None:
        assert partition_key == item
        self.before_delete(item)
        if item not in self.items:
            raise CosmosResourceNotFoundError(status_code=404, message="gone")
        if etag is not None and self.items[item]["_etag"] != etag:
            raise CosmosAccessConditionFailedError(status_code=412, message="changed")
        del self.items[item]


@pytest.fixture
def container() -> FakeContainer:
    return FakeContainer()


@pytest.fixture
def store(container: FakeContainer) -> CosmosAuthStore:
    return CosmosAuthStore(container)


def test_a_record_reads_back_as_written(store: CosmosAuthStore) -> None:
    store.put("code", "h1", {"client_id": "c", "expires_at": 9}, int(time.time()) + 300)
    assert store.get("code", "h1") == {"client_id": "c", "expires_at": 9}


def test_kinds_never_collide(store: CosmosAuthStore) -> None:
    # Given two records sharing a key but not a kind
    store.put("client", "same", {"which": "client"}, int(time.time()) + 300)
    store.put("refresh", "same", {"which": "refresh"}, int(time.time()) + 300)

    # Then each reads back as itself
    assert store.get("client", "same") == {"which": "client"}
    assert store.get("refresh", "same") == {"which": "refresh"}


def test_a_record_carries_a_ttl_so_cosmos_removes_it(
    store: CosmosAuthStore, container: FakeContainer
) -> None:
    store.put("sign_in", "s", {}, int(time.time()) + 600)
    assert 590 <= container.items["sign_in:s"]["ttl"] <= 600


def test_an_already_expired_record_still_gets_a_valid_ttl(
    store: CosmosAuthStore, container: FakeContainer
) -> None:
    # Cosmos rejects a ttl of zero or less, so a record written late must not
    # fail the write that carries it
    store.put("code", "late", {}, int(time.time()) - 10)
    assert container.items["code:late"]["ttl"] == 1


def test_a_missing_record_reads_as_none(store: CosmosAuthStore) -> None:
    assert store.get("refresh", "never") is None
    assert store.take("refresh", "never") is None


def test_taking_a_record_returns_it_once(store: CosmosAuthStore) -> None:
    # Given a refresh token
    store.put("refresh", "h", {"subject": "s"}, int(time.time()) + 300)

    # When it is taken twice
    first = store.take("refresh", "h")
    second = store.take("refresh", "h")

    # Then the first taker has it and the second gets nothing
    assert first == {"subject": "s"}
    assert second is None


def test_a_taker_that_loses_the_race_gets_nothing(
    store: CosmosAuthStore, container: FakeContainer
) -> None:
    # Given a refresh token that is rewritten by another request between this
    # taker's read and its delete, as a concurrent rotation would
    store.put("refresh", "h", {"subject": "s"}, int(time.time()) + 300)
    container.before_delete = lambda item_id: store.put(
        "refresh", "h", {"subject": "other"}, int(time.time()) + 300
    )

    # When this taker tries to delete the version it read
    taken = store.take("refresh", "h")

    # Then Cosmos refuses on the etag, and the taker is told it got nothing
    # rather than crashing or getting a token someone else now holds
    assert taken is None


def test_a_taker_whose_record_vanished_gets_nothing(
    store: CosmosAuthStore, container: FakeContainer
) -> None:
    # Given a record deleted by another taker between this one's read and delete
    store.put("code", "h", {"subject": "s"}, int(time.time()) + 300)
    container.before_delete = lambda item_id: container.items.pop(item_id)

    # Then this taker gets nothing
    assert store.take("code", "h") is None


def test_deleting_a_missing_record_is_not_an_error(store: CosmosAuthStore) -> None:
    store.delete("refresh", "never")


def test_a_document_from_the_first_deployment_reads_as_absent(
    store: CosmosAuthStore, container: FakeContainer
) -> None:
    # Given a refresh token and a client as the first deployment wrote them,
    # before every record shared one shape
    container.upsert_item(
        {
            "id": "refresh:h",
            "client_id": "c",
            "subject": "s",
            "scopes": [],
            "expires_at": 9,
            "ttl": 60,
        }
    )
    container.upsert_item({"id": "client:c", "client": {"client_id": "c"}})

    # Then they read as absent, so the client is asked to sign in again
    # rather than getting a server error
    assert store.get("refresh", "h") is None
    assert store.take("refresh", "h") is None
    assert store.get("client", "c") is None
