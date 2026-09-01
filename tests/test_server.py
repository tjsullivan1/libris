"""Tests for the local daemon shell: health, token auth, and CORS.

The endpoints themselves land in #53. What is asserted here is the shell the
extension has to trust before any of that matters - that health is reachable
without a credential, that nothing else is, and that an arbitrary web page
cannot reach the daemon just because it runs on the same machine.
"""

import pytest

pytest.importorskip("fastapi")

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from typer.testing import CliRunner  # noqa: E402

from libris import config, shelf  # noqa: E402
from libris.api import BookCandidate  # noqa: E402
from libris.cli import app as cli_app  # noqa: E402
from libris.markdown import BookNote, create_book_note  # noqa: E402
from libris.note_format import (  # noqa: E402
    FORMAT_VALUES,
    PRIORITY_VALUES,
    STATUS_VALUES,
)
from libris.server import create_app  # noqa: E402

runner = CliRunner()


@pytest.fixture
def token() -> str:
    """Ensure a server token exists and return it."""
    return config.ensure_server_token()


@pytest.fixture
def client(token) -> TestClient:
    """A client for an app built after the token exists."""
    return TestClient(create_app())


@pytest.fixture
def configured_shelf(tmp_path):
    """A configured Shelf. `libris serve` refuses to start without one."""
    config.set_book_vault_path(tmp_path)
    return tmp_path


# --- health ---


def test_health_needs_no_token(client):
    # Given a running daemon and no credential at all
    # When the popup checks whether it can connect
    response = client.get("/health")

    # Then it answers, so a misconfigured token is distinguishable from a
    # daemon that is not running
    assert response.status_code == 200


def test_health_reports_version_and_shelf(client, tmp_path):
    # Given a configured Shelf
    config.set_book_vault_path(tmp_path / "Book List")

    # When health is requested
    body = TestClient(create_app()).get("/health").json()

    # Then the popup can show which Library it is talking to
    assert body["status"] == "ok"
    assert body["version"]
    assert "Book List" in body["vault_path"]


def test_health_does_not_leak_the_token(client, token):
    # Given a daemon with a token configured
    # When health is requested without credentials
    body = TestClient(create_app()).get("/health").text

    # Then the unauthenticated response does not carry the credential
    assert token not in body


# --- token auth ---


def test_request_without_a_token_is_rejected(client):
    # Given no Authorization header
    # When any non-health path is requested
    response = client.get("/anything")

    # Then it is refused before routing, so the daemon does not reveal which
    # paths exist to an unauthenticated caller
    assert response.status_code == 401


def test_request_with_a_wrong_token_is_rejected(client):
    # Given a credential that is not the configured token
    # When a non-health path is requested
    response = client.get("/anything", headers={"Authorization": "Bearer wrong"})

    # Then it is refused
    assert response.status_code == 401


def test_request_with_a_malformed_header_is_rejected(client, token):
    # Given the right token sent without the Bearer scheme
    # When a non-health path is requested
    response = client.get("/anything", headers={"Authorization": token})

    # Then it is refused; the scheme is required
    assert response.status_code == 401


def test_request_with_the_right_token_passes_auth(client, token):
    # Given the configured token
    # When a path that does not exist is requested
    response = client.get("/anything", headers={"Authorization": f"Bearer {token}"})

    # Then the answer is 404 rather than 401 - auth passed and routing ran
    assert response.status_code == 404


# --- the token itself ---


def test_token_is_generated_and_persisted_on_first_run():
    # Given a config with no server token
    assert config.get_server_token() is None

    # When the daemon ensures one exists
    created = config.ensure_server_token()

    # Then it is persisted, so a restart does not invalidate the extension
    assert created
    assert config.get_server_token() == created


def test_token_is_reused_on_subsequent_runs():
    # Given a token created on a first run
    first = config.ensure_server_token()

    # When the daemon starts again
    second = config.ensure_server_token()

    # Then the same credential is used
    assert first == second


def test_token_is_not_guessable():
    # Given a freshly generated token
    created = config.ensure_server_token()

    # Then it carries enough entropy to survive being reachable from a browser
    assert len(created) >= 32


# --- CORS ---


def test_preflight_from_an_allowlisted_origin_is_answered(token):
    # Given an extension origin on the allowlist
    origin = "chrome-extension://abcdefghijklmnop"
    config.set_config(config.EXTENSION_ORIGINS_KEY, [origin])

    # When the browser sends a preflight, which carries no credential
    response = TestClient(create_app()).options(
        "/anything",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "POST",
        },
    )

    # Then CORS answers it rather than auth refusing it
    assert response.headers.get("access-control-allow-origin") == origin


def test_preflight_from_an_unlisted_origin_is_refused(token):
    # Given an allowlist that does not include the caller
    config.set_config(
        config.EXTENSION_ORIGINS_KEY, ["chrome-extension://abcdefghijklmnop"]
    )

    # When some other page preflights the daemon
    response = TestClient(create_app()).options(
        "/anything",
        headers={
            "Origin": "https://evil.example",
            "Access-Control-Request-Method": "POST",
        },
    )

    # Then it is not granted access
    assert response.headers.get("access-control-allow-origin") is None


def test_no_origin_is_allowed_when_none_are_configured(token):
    # Given no configured extension origins
    # When any origin preflights
    response = TestClient(create_app()).options(
        "/anything",
        headers={
            "Origin": "https://evil.example",
            "Access-Control-Request-Method": "POST",
        },
    )

    # Then nothing is allowed; the default is closed, never "*"
    assert response.headers.get("access-control-allow-origin") is None


def test_health_reports_that_no_shelf_is_configured(client):
    # Given a daemon started before anyone set a vault path
    # When the popup checks the connection
    body = client.get("/health").json()

    # Then it can tell running from usable, rather than trusting a path that
    # get_vault_path invented from the working directory (#82)
    assert body["vault_configured"] is False


def test_health_reports_a_configured_shelf(token, configured_shelf):
    # Given a configured Shelf
    # When the popup checks the connection
    body = TestClient(create_app()).get("/health").json()

    # Then it is told the daemon is usable, not merely up
    assert body["vault_configured"] is True
    assert body["vault_path"] == str(configured_shelf)


# --- binding ---
def test_serve_refuses_a_non_loopback_host(monkeypatch):
    # Given a daemon asked to bind a public interface
    started = []
    monkeypatch.setattr("libris.server.run", lambda **kw: started.append(kw))

    # When it is started without explicitly allowing that
    # noqa justified: binding every interface is the thing being refused here.
    result = runner.invoke(cli_app, ["serve", "--host", "0.0.0.0"])  # noqa: S104

    # Then it refuses, because the token is the only thing guarding it
    assert result.exit_code != 0
    assert "--allow-remote" in result.output
    assert started == []


def test_serve_binds_loopback_by_default(monkeypatch, configured_shelf):
    # Given a daemon started with no host argument
    started = []
    monkeypatch.setattr("libris.server.run", lambda **kw: started.append(kw))

    # When it starts
    result = runner.invoke(cli_app, ["serve"])

    # Then it listens only on loopback, on the documented port
    assert result.exit_code == 0
    assert started == [{"host": "127.0.0.1", "port": 8787, "reload": False}]


def test_serve_accepts_any_loopback_address(monkeypatch, configured_shelf):
    # Given a loopback address that is not 127.0.0.1
    started = []
    monkeypatch.setattr("libris.server.run", lambda **kw: started.append(kw))

    # When the daemon is asked to bind it
    result = runner.invoke(cli_app, ["serve", "--host", "127.0.0.2"])

    # Then it starts, rather than pushing the user towards --allow-remote to do
    # something that never leaves the machine
    assert result.exit_code == 0
    assert started == [{"host": "127.0.0.2", "port": 8787, "reload": False}]


def test_serve_accepts_ipv6_loopback(monkeypatch, configured_shelf):
    # Given the IPv6 loopback address
    started = []
    monkeypatch.setattr("libris.server.run", lambda **kw: started.append(kw))

    # When the daemon is asked to bind it
    result = runner.invoke(cli_app, ["serve", "--host", "::1"])

    # Then it starts
    assert result.exit_code == 0
    assert started == [{"host": "::1", "port": 8787, "reload": False}]


def test_serve_refuses_a_routable_address(monkeypatch):
    # Given an address that is reachable from the network
    started = []
    monkeypatch.setattr("libris.server.run", lambda **kw: started.append(kw))

    # When the daemon is asked to bind it
    result = runner.invoke(cli_app, ["serve", "--host", "192.168.1.10"])

    # Then it refuses
    assert result.exit_code != 0
    assert "--allow-remote" in result.output
    assert started == []


def test_serve_refuses_an_unresolvable_hostname(monkeypatch):
    # Given a host that is neither a loopback name nor an address
    started = []
    monkeypatch.setattr("libris.server.run", lambda **kw: started.append(kw))

    # When the daemon is asked to bind it
    result = runner.invoke(cli_app, ["serve", "--host", "books.example.com"])

    # Then it refuses rather than resolving it to find out
    assert result.exit_code != 0
    assert started == []


def test_serve_allows_a_routable_address_when_explicitly_permitted(
    monkeypatch, configured_shelf
):
    # Given an operator who means it
    started = []
    monkeypatch.setattr("libris.server.run", lambda **kw: started.append(kw))

    # When the daemon is started with --allow-remote
    result = runner.invoke(
        cli_app, ["serve", "--host", "192.168.1.10", "--allow-remote"]
    )

    # Then it binds what was asked for
    assert result.exit_code == 0
    assert started == [{"host": "192.168.1.10", "port": 8787, "reload": False}]


def test_serve_refuses_to_start_without_a_shelf(monkeypatch):
    # Given a machine where no vault path has been set
    started = []
    monkeypatch.setattr("libris.server.run", lambda **kw: started.append(kw))

    # When the daemon is started
    result = runner.invoke(cli_app, ["serve"])

    # Then it refuses and says how to fix it, rather than serving whatever
    # directory it happened to be started in - which for #55's scheduled task
    # is a directory nobody chose (#82)
    assert result.exit_code != 0
    assert "No Shelf is configured" in result.output
    assert "libris config --vault" in result.output
    assert started == []


# --- /api/v1/lookup ---


def _mock_search(monkeypatch, results):
    """Point the service's Google Books search at fixed results."""
    captured = {}

    def _search(self, query):
        captured["query"] = query
        return results

    monkeypatch.setattr("libris.service.GoogleBooksClient.search", _search)
    return captured


def test_lookup_needs_a_token(client):
    # Given no credential
    # When lookup is called
    response = client.post("/api/v1/lookup", json={"isbn": "9780441013593"})

    # Then it is refused like every other path
    assert response.status_code == 401


def test_lookup_by_isbn_searches_by_isbn(client, token, monkeypatch):
    # Given a page that yielded an ISBN
    captured = _mock_search(
        monkeypatch, [BookCandidate(title="Dune", authors=["Frank Herbert"])]
    )

    # When the extension looks it up
    response = client.post(
        "/api/v1/lookup",
        json={"isbn": "9780441013593", "title": "Dune"},
        headers={"Authorization": f"Bearer {token}"},
    )

    # Then the identifier is used rather than the title
    assert response.status_code == 200
    assert captured["query"] == "isbn:9780441013593"
    assert response.json()["candidates"][0]["title"] == "Dune"


def test_lookup_ranks_the_best_described_candidate_first(client, token, monkeypatch):
    # Given two editions of the same book, one better described
    sparse = BookCandidate(title="Dune", authors=["Frank Herbert"])
    rich = BookCandidate(
        title="Dune",
        authors=["Frank Herbert"],
        isbn="9780441013593",
        page_count=412,
        description="A desert planet.",
    )
    _mock_search(monkeypatch, [sparse, rich])

    # When the extension looks the book up
    response = client.post(
        "/api/v1/lookup",
        json={"title": "Dune"},
        headers={"Authorization": f"Bearer {token}"},
    )

    # Then the popup's first choice is the one with the most metadata
    candidates = response.json()["candidates"]
    assert candidates[0]["isbn"] == "9780441013593"
    assert len(candidates) == 2


def test_lookup_with_a_kindle_asin_falls_back_to_names(client, token, monkeypatch):
    # Given a Kindle page, whose ASIN is not an ISBN
    captured = _mock_search(monkeypatch, [])

    # When the extension looks it up
    client.post(
        "/api/v1/lookup",
        json={"asin": "B000FC0SIM", "title": "Dune", "authors": ["Frank Herbert"]},
        headers={"Authorization": f"Bearer {token}"},
    )

    # Then the search is by name, not by a fake ISBN
    assert captured["query"] == "intitle:Dune inauthor:Frank Herbert"


def test_lookup_with_no_results_is_not_an_error(client, token, monkeypatch):
    # Given a search that matches nothing
    _mock_search(monkeypatch, [])

    # When the extension looks it up
    response = client.post(
        "/api/v1/lookup",
        json={"title": "A Book That Does Not Exist"},
        headers={"Authorization": f"Bearer {token}"},
    )

    # Then the answer is an empty list, so the popup can say "no matches"
    # rather than "something went wrong"
    assert response.status_code == 200
    assert response.json()["candidates"] == []


def test_lookup_reports_an_upstream_failure_as_502(client, token, monkeypatch):
    # Given Google Books being unreachable
    def _boom(self, query):
        raise httpx.RequestError("connection refused")

    monkeypatch.setattr("libris.service.GoogleBooksClient.search", _boom)

    # When the extension looks a book up
    response = client.post(
        "/api/v1/lookup",
        json={"isbn": "9780441013593"},
        headers={"Authorization": f"Bearer {token}"},
    )

    # Then the daemon says the upstream failed, not that it did
    assert response.status_code == 502
    assert "Traceback" not in response.text


# --- GET /api/v1/books ---


def test_existing_note_is_found_by_isbn(token, tmp_path):
    # Given a Book Note on the Shelf
    config.set_book_vault_path(tmp_path)
    create_book_note(
        BookCandidate(title="Dune", authors=["Frank Herbert"], isbn="9780441013593"),
        tmp_path,
    )

    # When the extension asks whether the book is already held
    response = TestClient(create_app()).get(
        "/api/v1/books",
        params={"isbn": "9780441013593"},
        headers={"Authorization": f"Bearer {token}"},
    )

    # Then it is told so, with the identity rather than only a filename
    assert response.status_code == 200
    body = response.json()
    assert body["found"] is True
    assert body["book"]["libris_id"]
    assert body["book"]["title"] == "Dune"
    assert "Dune" in body["book"]["path"]


def test_a_book_not_held_says_so_in_the_body(token, tmp_path):
    # Given an empty Shelf
    config.set_book_vault_path(tmp_path)

    # When the extension asks about a book
    response = TestClient(create_app()).get(
        "/api/v1/books",
        params={"isbn": "9780441013593"},
        headers={"Authorization": f"Bearer {token}"},
    )

    # Then a miss is a miss (ADR 0003), reported in the body rather than the
    # status: a search that matched nothing succeeded (ADR 0021), and a real
    # 404 means the base URL is wrong
    assert response.status_code == 200
    body = response.json()
    assert body["found"] is False
    assert body["book"] is None
    assert body["near_matches"] == []


# --- POST /api/v1/books ---


def _post_book(token, **extra):
    payload = {
        "candidate": {
            "title": "Dune",
            "authors": ["Frank Herbert"],
            "isbn": "9780441013593",
        }
    }
    payload.update(extra)
    return TestClient(create_app()).post(
        "/api/v1/books",
        json=payload,
        headers={"Authorization": f"Bearer {token}"},
    )


def test_creating_a_book_note_answers_with_its_identity(token, tmp_path):
    # Given a Shelf without the book
    config.set_book_vault_path(tmp_path)

    # When the extension adds it
    response = _post_book(token)

    # Then the answer carries the durable identity, what happened, and which
    # guarantee stands behind it (ADR 0016)
    assert response.status_code == 201
    body = response.json()
    assert body["book"]["libris_id"]
    assert body["outcome"] == "created"
    assert body["guarantee"] == "live_shelf"
    assert (tmp_path / "Dune - Frank Herbert.md").exists()


def test_adding_a_book_already_held_leaves_it_untouched(token, tmp_path):
    # Given the book already on the Shelf
    config.set_book_vault_path(tmp_path)
    first = _post_book(token).json()
    note_path = tmp_path / "Dune - Frank Herbert.md"
    original = note_path.read_text(encoding="utf-8")

    # When it is captured a second time
    response = _post_book(token)

    # Then the Library already satisfied it, and the reader's own writing in
    # that note is not overwritten
    assert response.status_code == 200
    body = response.json()
    assert body["outcome"] == "already_present"
    assert body["book"]["libris_id"] == first["book"]["libris_id"]
    assert note_path.read_text(encoding="utf-8") == original


def test_creating_applies_overrides(token, tmp_path):
    # Given a book captured as already read
    config.set_book_vault_path(tmp_path)

    # When it is added with overrides
    response = _post_book(token, overrides={"status": "Read", "rating": 5})

    # Then the note carries them
    assert response.status_code == 201
    text = (tmp_path / "Dune - Frank Herbert.md").read_text(encoding="utf-8")
    assert "status: Read" in text


def test_an_unknown_override_field_is_refused(token, tmp_path):
    # Given an override naming a field the schema has no place for
    config.set_book_vault_path(tmp_path)

    # When the extension sends it
    response = _post_book(token, overrides={"nonsense": "x"})

    # Then it is refused as a bad request, not raised as a crash
    assert response.status_code == 422
    assert "Traceback" not in response.text


def test_an_illegal_status_value_is_refused(token, tmp_path):
    # Given a status the Library does not define, arriving from a browser
    config.set_book_vault_path(tmp_path)

    # When the extension sends it
    response = _post_book(token, overrides={"status": "finished"})

    # Then it is refused before anything is written (#65)
    assert response.status_code == 422
    assert not any(tmp_path.glob("*.md"))


def test_a_near_match_is_offered_rather_than_decided(token, tmp_path):
    # Given a note whose title carries a subtitle the scraped page does not
    config.set_book_vault_path(tmp_path)
    create_book_note(
        BookCandidate(title="The Brass Verdict: A Novel", authors=["Michael Connelly"]),
        tmp_path,
    )

    # When the extension asks about the short form
    response = TestClient(create_app()).get(
        "/api/v1/books",
        params={"title": "The Brass Verdict", "authors": ["Michael Connelly"]},
        headers={"Authorization": f"Bearer {token}"},
    )

    # Then it is still a miss - nothing claims this is the same Book - but the
    # popup is given the near match so a person can settle it
    assert response.status_code == 200
    body = response.json()
    assert body["found"] is False
    near_matches = body["near_matches"]
    assert len(near_matches) == 1
    assert near_matches[0]["title"] == "The Brass Verdict: A Novel"
    assert near_matches[0]["libris_id"]


def test_a_different_book_by_the_same_author_is_not_offered(token, tmp_path):
    # Given two unrelated books by one author, the case measured on the real
    # Shelf: "Mercy" and "Long Road to Mercy" are different books
    config.set_book_vault_path(tmp_path)
    create_book_note(
        BookCandidate(title="Memory Man", authors=["David Baldacci"]), tmp_path
    )

    # When the extension asks about the other
    response = TestClient(create_app()).get(
        "/api/v1/books",
        params={"title": "The Innocent", "authors": ["David Baldacci"]},
        headers={"Authorization": f"Bearer {token}"},
    )

    # Then nothing is offered, because nothing resembles it
    assert response.status_code == 200
    assert response.json()["near_matches"] == []


def test_a_near_title_by_another_author_is_not_offered(token, tmp_path):
    # Given a similarly titled book by someone else
    config.set_book_vault_path(tmp_path)
    create_book_note(
        BookCandidate(title="Dune: Deluxe Edition", authors=["Someone Else"]), tmp_path
    )

    # When the extension asks about Frank Herbert's
    response = TestClient(create_app()).get(
        "/api/v1/books",
        params={"title": "Dune", "authors": ["Frank Herbert"]},
        headers={"Authorization": f"Bearer {token}"},
    )

    # Then the author keeps them apart
    assert response.status_code == 200
    assert response.json()["near_matches"] == []


def test_an_exact_match_still_answers_directly(token, tmp_path):
    # Given a note matching exactly
    config.set_book_vault_path(tmp_path)
    create_book_note(BookCandidate(title="Dune", authors=["Frank Herbert"]), tmp_path)

    # When the extension asks about it
    response = TestClient(create_app()).get(
        "/api/v1/books",
        params={"title": "Dune", "authors": ["Frank Herbert"]},
        headers={"Authorization": f"Bearer {token}"},
    )

    # Then it is answered, not offered as a maybe
    body = response.json()
    assert body["found"] is True
    assert body["book"]["libris_id"]
    assert body["near_matches"] == []


# --- GET /api/v1/fields ---


def test_fields_needs_a_token(client):
    # Given a caller with no credential
    # When it asks what the Library defines
    response = client.get("/api/v1/fields")

    # Then it is refused: a field vocabulary is Library data (ADR 0022)
    assert response.status_code == 401


def test_fields_reports_the_vocabularies_the_library_defines(client, token):
    # Given a running daemon
    # When a Surface asks what values it may offer
    response = client.get(
        "/api/v1/fields", headers={"Authorization": f"Bearer {token}"}
    )

    # Then it is told, so no client restates them and drifts the way
    # `libris update` did with "Finished"
    assert response.status_code == 200
    fields = response.json()["fields"]
    assert fields["status"]["values"] == list(STATUS_VALUES)
    assert fields["priority"]["values"] == list(PRIORITY_VALUES)
    assert fields["format"]["values"] == list(FORMAT_VALUES)


def test_fields_says_which_of_them_hold_several_values(client, token):
    # Given a Surface that has to choose a control for each field
    response = client.get(
        "/api/v1/fields", headers={"Authorization": f"Bearer {token}"}
    )

    # Then Format is marked as holding several at once and Status as holding
    # one, so the popup does not have to know that from somewhere else
    fields = response.json()["fields"]
    assert fields["format"]["multi"] is True
    assert fields["status"]["multi"] is False
    assert fields["priority"]["multi"] is False


# --- source_url ---


def test_an_amazon_url_yields_an_asin_when_the_scraper_found_none(
    client, token, monkeypatch
):
    # Given a page whose ASIN is a valid ISBN-10, present only in the URL
    captured = _mock_search(monkeypatch, [])

    # When the extension looks the page up without an asin field
    client.post(
        "/api/v1/lookup",
        json={
            "title": "Dune",
            "source_url": "https://www.amazon.com/dp/0441013597/ref=sr_1_1",
        },
        headers={"Authorization": f"Bearer {token}"},
    )

    # Then the identifier in the URL is used, because an identifier names one
    # edition where a title names a book that may have twenty
    assert captured["query"] == "isbn:0441013597"


def test_a_scraped_asin_wins_over_the_url(client, token, monkeypatch):
    # Given both an ASIN from the page and one in the URL
    captured = _mock_search(monkeypatch, [])

    # When the extension looks it up
    client.post(
        "/api/v1/lookup",
        json={
            "asin": "0441013597",
            "source_url": "https://www.amazon.com/dp/1234567890",
        },
        headers={"Authorization": f"Bearer {token}"},
    )

    # Then what the scraper read from the page is preferred
    assert captured["query"] == "isbn:0441013597"


def test_a_url_that_names_no_book_is_not_mistaken_for_one(client, token, monkeypatch):
    # Given a page that is not a product page
    captured = _mock_search(monkeypatch, [])

    # When the extension looks it up
    client.post(
        "/api/v1/lookup",
        json={
            "title": "Dune",
            "source_url": "https://www.amazon.com/gp/cart/view.html",
        },
        headers={"Authorization": f"Bearer {token}"},
    )

    # Then nothing is invented from the URL and the names are used
    assert captured["query"] == "intitle:Dune"


# --- what a write answers with ---


def test_a_write_names_the_note_it_points_at(token, tmp_path):
    # Given a Shelf without the book
    config.set_book_vault_path(tmp_path)

    # When the extension adds it
    body = _post_book(token).json()

    # Then the answer carries what a person is shown, not only a path - the
    # device running the extension may hold no Shelf at all (ADR 0019)
    assert body["book"]["title"] == "Dune"
    assert body["book"]["authors"] == ["Frank Herbert"]


def test_an_already_held_book_answers_with_the_title_it_is_held_under(token, tmp_path):
    # Given a note whose title differs from the one the page shows, matched on
    # ISBN rather than on the titles agreeing
    config.set_book_vault_path(tmp_path)
    create_book_note(
        BookCandidate(
            title="Dune (Dune Chronicles #1)",
            authors=["Frank Herbert"],
            isbn="9780441013593",
        ),
        tmp_path,
    )

    # When the same book is captured from a page calling it something shorter
    body = _post_book(token).json()

    # Then the popup can name the Book as the Library holds it, rather than
    # echoing back what the person tried to add
    assert body["outcome"] == "already_present"
    assert body["book"]["title"] == "Dune (Dune Chronicles #1)"


# --- startup ---


def test_the_shelf_is_read_before_the_daemon_accepts_requests(
    token, tmp_path, monkeypatch
):
    # Given a Shelf holding a book, and no index yet
    config.set_book_vault_path(tmp_path)
    create_book_note(BookCandidate(title="Dune", authors=["Frank Herbert"]), tmp_path)
    shelf.forget_indexes()

    parsed = []
    original = BookNote.read

    def _counted(path):
        parsed.append(path)
        return original(path)

    monkeypatch.setattr(BookNote, "read", staticmethod(_counted))

    # When the daemon starts, and is then asked about a book it holds
    with TestClient(create_app()) as client:
        assert parsed, "the Shelf should be read during startup, not on demand"
        parsed.clear()

        response = client.get(
            "/api/v1/books",
            params={"title": "Dune", "authors": ["Frank Herbert"]},
            headers={"Authorization": f"Bearer {token}"},
        )

    # Then the question itself parsed nothing. Reading this Shelf costs about
    # seven seconds at its real size, and the daemon binds late so that lands on
    # startup rather than on somebody waiting in a browser popup (#85).
    assert response.status_code == 200
    assert parsed == []
