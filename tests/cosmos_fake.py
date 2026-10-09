"""A Cosmos container that answers the queries `CosmosStore` sends, and no others.

The tests run without an Azure account (#173), so `CosmosStore` is tested
against this. It evaluates the handful of clause shapes the store writes, and
fails on any other: a fake that guessed at a query it did not understand would
let a wrong one pass.

Three things it does as Cosmos does, because the store would be wrong without
them. Every document goes through JSON on the way in, so a note that only
survived as a Python object would not pass. Queries return documents newest
first, not in the order they were pushed, so an answer that relied on push
order to break a tie would not agree with the Shelf. And strings are ordered by
UTF-16 code unit, not by code point as Python orders them - assumed rather than
confirmed, because it is the assumption that would catch a store relying on
Python's order.
"""

import json
import re
from typing import Any

_EQUALS = re.compile(r"^c\.(\w+) = (@\w+|true)$")
_ARRAY_HOLDS = re.compile(r"^ARRAY_CONTAINS\(c\.(\w+), (@\w+)\)$")
_ANY_WORD = re.compile(
    r"^EXISTS\(SELECT VALUE w FROM w IN c\.(\w+) WHERE ARRAY_CONTAINS\((@\w+), w\)\)$"
)
_QUERY = re.compile(
    r"^SELECT \* FROM c WHERE (?P<where>.+?)"
    r"(?: ORDER BY (?P<order>.+?) OFFSET 0 LIMIT (?P<limit>@\w+))?$"
)
_ORDER_KEY = re.compile(r"^c\.(\w+) ASC$")


class NotFound(Exception):
    """What the SDK raises for a missing item: an error carrying a 404."""

    status_code = 404


class FakeContainer:
    """The container calls `push_shelf` and `CosmosStore` make."""

    def __init__(self) -> None:
        self.items: dict[str, dict[str, Any]] = {}
        self.queries: list[str] = []
        # Every write, in order, so a test can say what a sync sent - and that
        # it sent nothing.
        self.writes: list[tuple[str, str]] = []
        # Lets a test make a write fail, as an outage or a throttle would.
        self.fail_writes: Exception | None = None

    def upsert_item(self, body: dict[str, Any]) -> dict[str, Any]:
        if self.fail_writes is not None:
            raise self.fail_writes
        stored = json.loads(json.dumps(body, allow_nan=False))
        self.writes.append(("upsert", stored["id"]))
        # Re-inserted, so the newest write comes back first.
        self.items.pop(stored["id"], None)
        self.items[stored["id"]] = stored
        return stored

    def delete_item(self, item: str, partition_key: Any) -> None:
        if self.fail_writes is not None:
            raise self.fail_writes
        # Every container here is partitioned on /id (infra/main.tf).
        assert partition_key == item, "partition key must be the id"
        self.writes.append(("delete", item))
        if item not in self.items:
            raise NotFound(item)
        del self.items[item]

    def query_items(
        self,
        query: str,
        parameters: list[dict[str, Any]] | None = None,
        enable_cross_partition_query: bool | None = None,
    ) -> list[dict[str, Any]]:
        self.queries.append(query)
        if query == "SELECT VALUE c.id FROM c":
            return list(self.items)
        values = {p["name"]: p["value"] for p in parameters or []}
        shape = _QUERY.match(query)
        assert shape, f"the fake does not understand {query!r}"
        clauses = [_clause(text, values) for text in shape["where"].split(" AND ")]

        found = [
            item
            for item in reversed(list(self.items.values()))
            if all(clause(item) for clause in clauses)
        ]
        if shape["order"]:
            keys = []
            for text in shape["order"].split(", "):
                key = _ORDER_KEY.match(text)
                assert key, f"the fake does not understand the ordering {text!r}"
                keys.append(key[1])
            found.sort(key=lambda item: tuple(_cosmos_order(item[k]) for k in keys))
            found = found[: values[shape["limit"]]]
        return found


def _clause(text: str, values: dict[str, Any]):
    if match := _EQUALS.match(text):
        name, value = match[1], True if match[2] == "true" else values[match[2]]
        # Cosmos compares types as well as values: 1 is not "1".
        return lambda item: name in item and _same(item[name], value)
    if match := _ARRAY_HOLDS.match(text):
        name, value = match[1], values[match[2]]
        return lambda item: any(_same(held, value) for held in item.get(name, []))
    if match := _ANY_WORD.match(text):
        name, wanted = match[1], values[match[2]]
        return lambda item: any(word in wanted for word in item.get(name, []))
    raise AssertionError(f"the fake does not understand the clause {text!r}")


def _cosmos_order(value: Any) -> Any:
    # Strings by UTF-16 code unit rather than by code point as Python does. The
    # two disagree past U+FFFF. Whether Cosmos orders this way is not confirmed
    # (#185 review says it does), so the fake assumes the order that differs
    # from Python's: a fake sorting the Python way would hide the difference.
    return value.encode("utf-16-be") if isinstance(value, str) else value


def _same(held: Any, value: Any) -> bool:
    return type(held) is type(value) and held == value
