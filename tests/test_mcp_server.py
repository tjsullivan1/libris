"""The MCP adapter, driven as a client actually drives it.

These are deliberately thin. The behaviour lives in service.py and is tested
there; what cannot be tested there is whether a tool registers, whether its
schema is one a model can fill, and whether a failure comes back as something
readable rather than as a dead connection. Those are the ones that look fine in
a unit test and break on first use, so they go through a real tools/list and
tools/call over the SDK's in-memory transport.
"""

import json
from pathlib import Path

import pytest

pytest.importorskip("mcp")

import anyio  # noqa: E402
from mcp import Client  # noqa: E402

from libris import config, shelf  # noqa: E402
from libris.api import BookCandidate  # noqa: E402
from libris.markdown import BookNote, create_book_note  # noqa: E402
from libris.mcp_server import create_server  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_indexes():
    """No index survives into another test's temporary Shelf."""
    shelf.forget_indexes()
    yield
    shelf.forget_indexes()


@pytest.fixture
def shelved(tmp_path, monkeypatch):
    """A configured Shelf holding two books."""
    monkeypatch.setattr(config, "get_vault_path", lambda: tmp_path)
    monkeypatch.setattr(config, "is_vault_configured", lambda: True)
    create_book_note(
        BookCandidate(title="Dune", authors=["Frank Herbert"]),
        tmp_path,
        overrides={"status": "Reading"},
    )
    create_book_note(
        BookCandidate(title="Oathbringer", authors=["Brandon Sanderson"]),
        tmp_path,
        overrides={"status": "Read"},
    )
    return tmp_path


def call(tool: str, arguments: dict | None = None):
    """Run one tool through a real client, and hand back the result."""

    async def _run():
        async with Client(create_server()) as client:
            return await client.call_tool(tool, arguments or {})

    return anyio.run(_run)


def list_tools():
    """Ask the server what it offers, the way a client does."""

    async def _run():
        async with Client(create_server()) as client:
            return (await client.list_tools()).tools

    return anyio.run(_run)


def payload(result):
    """Read a tool result's structured content, or its text when it errored."""
    if result.structured_content is not None:
        return result.structured_content
    return json.loads(result.content[0].text)


def text(result):
    """Read a tool result's text, which is where an error message lands."""
    return result.content[0].text


# --- what the model is shown ---


def test_every_tool_is_offered(shelved):
    # Given the server
    # When a client lists its tools
    names = {t.name for t in list_tools()}

    # Then all four are there, and nothing that reads a note body is (ADR 0023)
    assert names == {"search_library", "find_book", "add_book", "update_book"}
    assert not {n for n in names if "note" in n or "body" in n}


def test_the_vocabularies_ride_in_the_schemas(shelved):
    # Given the tools that write
    tools = {t.name: t for t in list_tools()}

    # When their schemas are read, as a model reads them before composing a call
    status = tools["update_book"].input_schema["properties"]["status"]
    enums = status.get("enum") or [
        e for branch in status.get("anyOf", []) for e in branch.get("enum", [])
    ]

    # Then the Library's own values are in them, generated rather than restated
    # (ADR 0022). `libris update` still offers "Finished", which is exactly the
    # drift a served vocabulary prevents.
    assert enums == ["To Read", "Reading", "Read", "Not To Read"]
    assert "Finished" not in enums


# --- one round trip per tool ---


def test_searching_the_library_answers_with_reading_state(shelved):
    # When the Library is searched
    answer = payload(call("search_library", {"query": "sanderson"}))

    # Then the book comes back with enough to tell it from another (Q4)
    assert answer["total"] == 1
    book = answer["books"][0]
    assert book["title"] == "Oathbringer"
    assert book["status"] == "Read"
    assert book["libris_id"]


def test_searching_by_status_alone_answers_what_am_i_reading(shelved):
    # When there is no query at all
    answer = payload(call("search_library", {"status": "Reading"}))

    # Then the filter answers it
    assert [b["title"] for b in answer["books"]] == ["Dune"]


def test_a_search_that_matches_nothing_is_not_an_error(shelved):
    # When nothing matches
    result = call("search_library", {"query": "neuromancer"})

    # Then it succeeded and says so in the body. A miss is a miss (ADR 0021).
    assert not result.is_error
    assert payload(result) == {"total": 0, "books": []}


def test_updating_a_book_reports_what_it_derived(shelved):
    # Given the book being read
    note = next(
        n
        for n in (BookNote.read(p) for p in Path(shelved).glob("*.md"))
        if n.title == "Dune"
    )

    # When it is marked Read without a date
    answer = payload(
        call("update_book", {"libris_id": note.libris_id, "status": "Read"})
    )

    # Then the stamped date travels back, so the agent can relay it and a person
    # who finished it last week can correct it (ADR 0024)
    assert answer["outcome"] == "updated"
    assert answer["book"]["status"] == "Read"
    assert list(answer["derived"]) == ["date_finished"]


# --- failures a person can act on ---


def test_an_unknown_book_comes_back_readable_not_as_a_crash(shelved):
    # When an identity nothing answers for is updated
    result = call(
        "update_book", {"libris_id": "01J0000000000000000000000A", "status": "Read"}
    )

    # Then the tool reports it in words rather than killing the connection
    assert result.is_error
    assert "01J0000000000000000000000A" in text(result)


def test_a_value_the_library_rejects_names_what_is_allowed(shelved):
    # Given a status the schema would have caught, sent anyway
    result = call("update_book", {"libris_id": "x", "status": "Finished"})

    # Then the answer says which values are legal, rather than only refusing
    assert result.is_error
    assert "Finished" in text(result)


def test_an_update_naming_no_fields_says_so(shelved):
    # When nothing is asked to change
    result = call("update_book", {"libris_id": "anything"})

    # Then it says so rather than reporting a successful no-op
    assert result.is_error
    assert "nothing to change" in text(result)


def test_an_unconfigured_shelf_never_reaches_the_working_directory(
    tmp_path, monkeypatch
):
    # Given a server started with no Shelf configured, which is allowed (Q11)
    monkeypatch.setattr(config, "is_vault_configured", lambda: False)

    # When a tool is called
    result = call("search_library", {"query": "dune"})

    # Then the fix reaches the person who can apply it, in the conversation.
    # Exiting at startup would put this on a stderr nobody reads.
    assert result.is_error
    assert "libris config --vault" in text(result)


def test_connecting_does_not_read_the_shelf(shelved, monkeypatch):
    # Given a Shelf, and a count of how many notes get parsed
    reads = []
    original = BookNote.read
    monkeypatch.setattr(
        BookNote,
        "read",
        staticmethod(lambda path: (reads.append(path), original(path))[1]),
    )

    # When a client connects and lists the tools, but calls none
    async def _connect():
        async with Client(create_server()) as client:
            return (await client.list_tools()).tools

    assert len(anyio.run(_connect)) == 4

    # Then nothing was parsed. A blocking warm-up here measured 49.4 seconds to
    # connect against a real Shelf, and a client that times out during
    # initialize records the server as failed rather than slow (#94). The cost
    # belongs on the first tool call, where a person can see it happen.
    assert reads == []
