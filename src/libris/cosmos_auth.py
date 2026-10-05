"""The authorization server's records, kept in Cosmos (ADR 0035).

They have to outlive the container: the Container App scales to zero, and a
store that forgot them would sign every client out on each cold start, or lose
a sign-in while the person was on Google's page. Reached with the Container
App's managed identity, so no key exists to leak (ADR 0006).

One container, `auth`, partitioned by `/id`. A document's id is its kind and
key, so records of different kinds can never collide. Each carries a `ttl`, so
Cosmos removes it once it can no longer be used.

A document without `data` was written by the first deployment, before every
record shared this shape. It reads as absent, so a client holding one is asked
to sign in again rather than getting a server error.
"""

import logging
import time

from azure.core import MatchConditions
from azure.core.exceptions import ResourceNotFoundError
from azure.cosmos import ContainerProxy, CosmosClient
from azure.cosmos.exceptions import CosmosAccessConditionFailedError
from azure.identity import DefaultAzureCredential

CONTAINER = "auth"

# The SDK logs every request and response header at INFO, about sixty lines per
# Cosmos call, which buried the container's request log the first time anyone
# needed to read it. Its warnings and errors still come through.
logging.getLogger("azure.cosmos._cosmos_http_logging_policy").setLevel(logging.WARNING)


class CosmosAuthStore:
    """An `AuthStore` over the `auth` container."""

    def __init__(self, container: ContainerProxy) -> None:
        self._container = container

    @classmethod
    def connect(cls, endpoint: str, database: str) -> "CosmosAuthStore":
        """Reach the `auth` container as the app's managed identity."""
        client = CosmosClient(endpoint, credential=DefaultAzureCredential())
        return cls(client.get_database_client(database).get_container_client(CONTAINER))

    def put(self, kind: str, key: str, data: dict, expires_at: int) -> None:
        self._container.upsert_item(
            {
                "id": f"{kind}:{key}",
                "data": data,
                "ttl": max(expires_at - int(time.time()), 1),
            }
        )

    def get(self, kind: str, key: str) -> dict | None:
        item = self._read(f"{kind}:{key}")
        return None if item is None else item.get("data")

    def take(self, kind: str, key: str) -> dict | None:
        item_id = f"{kind}:{key}"
        item = self._read(item_id)
        if item is None or "data" not in item:
            return None
        # Delete only the version just read. If another request deleted it in
        # between, this one loses, so a record is taken exactly once. Cosmos
        # reports the lost race as its own exception, not azure-core's
        # ResourceModifiedError, which is the one a reader would guess.
        try:
            self._container.delete_item(
                item_id,
                partition_key=item_id,
                etag=item["_etag"],
                match_condition=MatchConditions.IfNotModified,
            )
        except (ResourceNotFoundError, CosmosAccessConditionFailedError):
            return None
        return item["data"]

    def delete(self, kind: str, key: str) -> None:
        item_id = f"{kind}:{key}"
        try:
            self._container.delete_item(item_id, partition_key=item_id)
        except ResourceNotFoundError:
            pass

    def _read(self, item_id: str) -> dict | None:
        try:
            return self._container.read_item(item_id, partition_key=item_id)
        except ResourceNotFoundError:
            return None
