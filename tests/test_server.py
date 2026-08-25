"""Tests for the local daemon shell: health, token auth, and CORS.

The endpoints themselves land in #53. What is asserted here is the shell the
extension has to trust before any of that matters - that health is reachable
without a credential, that nothing else is, and that an arbitrary web page
cannot reach the daemon just because it runs on the same machine.
"""

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402
from typer.testing import CliRunner  # noqa: E402

from libris import config  # noqa: E402
from libris.cli import app as cli_app  # noqa: E402
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


def test_serve_binds_loopback_by_default(monkeypatch):
    # Given a daemon started with no host argument
    started = []
    monkeypatch.setattr("libris.server.run", lambda **kw: started.append(kw))

    # When it starts
    result = runner.invoke(cli_app, ["serve"])

    # Then it listens only on loopback, on the documented port
    assert result.exit_code == 0
    assert started == [{"host": "127.0.0.1", "port": 8787, "reload": False}]
