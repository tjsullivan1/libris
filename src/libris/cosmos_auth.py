"""Registered clients and refresh tokens, kept in Cosmos (ADR 0035).

They have to outlive the container: the Container App scales to zero, and a
store that forgot them would sign every client out on each cold start. Reached
with the Container App's managed identity, so no key exists to leak (ADR 0006).

One container, `auth`, partitioned by `/id`. Each document's id says what it
is, so a client and a token can never collide.
"""

import time

from azure.core import MatchConditions
from azure.core.exceptions import ResourceNotFoundError
from azure.cosmos import CosmosClient
from azure.cosmos.exceptions import CosmosAccessConditionFailedError
from azure.identity import DefaultAzureCredential
from mcp.shared.auth import OAuthClientInformationFull

from .oauth import StoredRefreshToken

CONTAINER = "auth"


class CosmosAuthStore:
    """An `AuthStore` over the `auth` container."""

    def __init__(self, endpoint: str, database: str) -> None:
        client = CosmosClient(endpoint, credential=DefaultAzureCredential())
        self._container = client.get_database_client(database).get_container_client(
            CONTAINER
        )

    def _read(self, item_id: str) -> dict | None:
        try:
            return self._container.read_item(item_id, partition_key=item_id)
        except ResourceNotFoundError:
            return None

    def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        item = self._read(f"client:{client_id}")
        return (
            None
            if item is None
            else OAuthClientInformationFull.model_validate(item["client"])
        )

    def save_client(self, client: OAuthClientInformationFull) -> None:
        item_id = f"client:{client.client_id}"
        self._container.upsert_item(
            {"id": item_id, "client": client.model_dump(mode="json")}
        )

    def save_refresh_token(self, token_hash: str, record: StoredRefreshToken) -> None:
        item_id = f"refresh:{token_hash}"
        self._container.upsert_item(
            {
                "id": item_id,
                "client_id": record.client_id,
                "subject": record.subject,
                "scopes": record.scopes,
                "expires_at": record.expires_at,
                # Cosmos removes the document itself once it can no longer be used.
                "ttl": max(record.expires_at - _now(), 1),
            }
        )

    def get_refresh_token(self, token_hash: str) -> StoredRefreshToken | None:
        item = self._read(f"refresh:{token_hash}")
        return None if item is None else _record(item)

    def take_refresh_token(self, token_hash: str) -> StoredRefreshToken | None:
        item_id = f"refresh:{token_hash}"
        item = self._read(item_id)
        if item is None:
            return None
        # Delete only the version just read. If another request deleted it in
        # between, this one loses, so a token can be redeemed exactly once.
        try:
            self._container.delete_item(
                item_id,
                partition_key=item_id,
                etag=item["_etag"],
                match_condition=MatchConditions.IfNotModified,
            )
        except (ResourceNotFoundError, CosmosAccessConditionFailedError):
            return None
        return _record(item)

    def delete_refresh_token(self, token_hash: str) -> None:
        item_id = f"refresh:{token_hash}"
        try:
            self._container.delete_item(item_id, partition_key=item_id)
        except ResourceNotFoundError:
            pass


def _record(item: dict) -> StoredRefreshToken:
    return StoredRefreshToken(
        client_id=item["client_id"],
        subject=item["subject"],
        scopes=list(item["scopes"]),
        expires_at=int(item["expires_at"]),
    )


def _now() -> int:
    return int(time.time())
