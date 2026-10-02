"""The hosted server's sign-in, driven end to end the way Claude and Gemini drive it.

ADR 0035 makes Libris its own authorization server. The SDK parses every OAuth
request, so what is tested here is what Libris decides: who gets a code, what a
token is good for, and what a page on another site cannot make a browser do.
Google is replaced by a fake that names whichever account a test chooses.
"""

import hashlib
import json
import secrets
import time
from base64 import urlsafe_b64encode
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlsplit

import pytest

pytest.importorskip("mcp")

import jwt  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

from libris.hosted import HostedConfigError, HostedSettings, create_app  # noqa: E402
from libris.oauth import (  # noqa: E402
    CONSENT_COOKIE,
    GoogleAccount,
    MemoryAuthStore,
    SignInFailed,
    account_from_id_token,
)

PUBLIC_URL = "https://libris.test"
RESOURCE = f"{PUBLIC_URL}/mcp"
CLAUDE_CALLBACK = "https://claude.ai/api/mcp/auth_callback"
SIGNING_KEY = "k" * 48
OWNER_SUB = "112233445566778899000"


@dataclass
class FakeGoogle:
    """Google, reduced to: whoever the test says signed in, signed in."""

    signed_in: GoogleAccount = field(
        default_factory=lambda: GoogleAccount(
            sub=OWNER_SUB, email="owner@gmail.com", email_verified=True
        )
    )
    verifiers: list[str] = field(default_factory=list)

    def authorization_url(
        self, *, state: str, code_challenge: str, redirect_uri: str
    ) -> str:
        return f"https://google.test/auth?state={state}&challenge={code_challenge}"

    async def account(
        self, *, code: str, code_verifier: str, redirect_uri: str
    ) -> GoogleAccount:
        self.verifiers.append(code_verifier)
        return self.signed_in


def settings(
    allowed_sub: str | None = OWNER_SUB, signing_key: str = SIGNING_KEY
) -> HostedSettings:
    return HostedSettings(
        public_url=PUBLIC_URL,
        google_client_id="google-client",
        google_client_secret="google-secret",  # noqa: S106 - a test value
        signing_key=signing_key,
        allowed_google_sub=allowed_sub,
    )


@pytest.fixture
def google():
    return FakeGoogle()


@pytest.fixture
def store():
    return MemoryAuthStore()


@pytest.fixture
def client(google, store):
    with TestClient(
        create_app(settings(), store=store, identity=google), base_url=PUBLIC_URL
    ) as test_client:
        yield test_client


def pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = (
        urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .decode()
        .rstrip("=")
    )
    return verifier, challenge


def register(client: TestClient) -> str:
    response = client.post(
        "/register",
        json={
            "redirect_uris": [CLAUDE_CALLBACK],
            "client_name": "Claude",
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["client_id"]


def start_sign_in(
    client: TestClient, client_id: str, challenge: str, resource: str = RESOURCE
):
    """`/authorize`, as a client sends it. Returns the response, unfollowed."""
    return client.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": CLAUDE_CALLBACK,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": "client-state",
            "scope": "libris",
            "resource": resource,
        },
        follow_redirects=False,
    )


def query(url: str) -> dict[str, str]:
    return {key: values[0] for key, values in parse_qs(urlsplit(url).query).items()}


def consent(
    client: TestClient, client_id: str, challenge: str, decision: str = "allow"
):
    """Through `/authorize` and the consent page. Returns the decision's response."""
    authorize = start_sign_in(client, client_id, challenge)
    assert authorize.status_code == 302, authorize.text
    page = client.get(authorize.headers["location"])
    assert page.status_code == 200
    request_id = query(authorize.headers["location"])["request"]
    return client.post(
        "/oauth/consent",
        data={"request": request_id, "decision": decision},
        follow_redirects=False,
    )


def sign_in(client: TestClient, client_id: str, challenge: str):
    """The whole browser leg. Returns the response that sends the browser back to the client."""
    to_google = consent(client, client_id, challenge)
    assert to_google.status_code == 303
    assert to_google.headers["location"].startswith("https://google.test/auth")
    state = query(to_google.headers["location"])["state"]
    return client.get(
        "/oauth/google/callback",
        params={"state": state, "code": "google-code"},
        follow_redirects=False,
    )


def exchange(client: TestClient, client_id: str, code: str, verifier: str):
    return client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": CLAUDE_CALLBACK,
            "client_id": client_id,
            "code_verifier": verifier,
            "resource": RESOURCE,
        },
    )


def tokens(client: TestClient) -> tuple[str, dict]:
    """A registered client and the tokens it ends up holding."""
    client_id = register(client)
    verifier, challenge = pkce()
    back = sign_in(client, client_id, challenge)
    response = exchange(
        client, client_id, query(back.headers["location"])["code"], verifier
    )
    assert response.status_code == 200, response.text
    return client_id, response.json()


def call_ping(client: TestClient, access_token: str | None):
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "ping", "arguments": {}},
    }
    return client.post("/mcp", content=json.dumps(body), headers=headers)


# Discovery ------------------------------------------------------------------


def test_an_unauthenticated_call_points_the_client_at_the_metadata(client):
    # When a client calls the MCP endpoint without a token
    response = call_ping(client, None)

    # Then it is refused with a pointer to where sign-in is described,
    # which is the only way Claude finds the authorization server
    assert response.status_code == 401
    assert "resource_metadata=" in response.headers["www-authenticate"]

    # And that metadata names this server as both the resource and its issuer
    metadata = client.get("/.well-known/oauth-protected-resource/mcp").json()
    assert metadata["resource"] == RESOURCE
    assert [server.rstrip("/") for server in metadata["authorization_servers"]] == [
        PUBLIC_URL
    ]


def test_the_authorization_server_offers_what_claude_and_gemini_need(client):
    # When a client reads the authorization server's metadata
    metadata = client.get("/.well-known/oauth-authorization-server").json()

    # Then it can register itself and must use PKCE
    assert metadata["registration_endpoint"] == f"{PUBLIC_URL}/register"
    assert metadata["code_challenge_methods_supported"] == ["S256"]


# Signing in -----------------------------------------------------------------


def test_the_owner_signs_in_and_the_token_reaches_the_tools(client, google):
    # Given a client that registered and signed in as the owner
    _, issued = tokens(client)

    # When it calls a tool with the token it was given
    response = call_ping(client, issued["access_token"])

    # Then the tool answers
    assert response.status_code == 200, response.text
    assert "pong from libris" in response.text
    # And Google was asked with the verifier for the challenge Libris sent it
    assert len(google.verifiers) == 1


def test_the_client_gets_its_own_state_back(client):
    # Given a sign-in started with a state of the client's choosing
    client_id = register(client)
    _, challenge = pkce()

    # When the browser is sent back to the client
    back = sign_in(client, client_id, challenge)

    # Then it goes to the client's registered callback, carrying that state
    assert back.headers["location"].startswith(CLAUDE_CALLBACK)
    assert query(back.headers["location"])["state"] == "client-state"


def test_another_google_account_gets_no_code(client, google):
    # Given Google says someone other than the owner signed in
    google.signed_in = GoogleAccount(
        sub="999", email="someone@gmail.com", email_verified=True
    )
    client_id = register(client)
    _, challenge = pkce()

    # When the sign-in finishes
    response = sign_in(client, client_id, challenge)

    # Then no code goes back to the client, and the page names the ID it saw,
    # which is how the first deployment finds the right one to allow
    assert response.status_code == 403
    assert "location" not in response.headers
    assert "999" in response.text


def test_an_unverified_email_gets_no_code_even_for_the_owners_id(client, google):
    # Given Google says the owner's account signed in but the email is unverified
    google.signed_in = GoogleAccount(
        sub=OWNER_SUB, email="owner@gmail.com", email_verified=False
    )
    client_id = register(client)
    _, challenge = pkce()

    # When the sign-in finishes
    response = sign_in(client, client_id, challenge)

    # Then it is refused
    assert response.status_code == 403


def test_with_no_owner_configured_nobody_gets_in(google, store):
    # Given a first deployment with no allowed account set yet
    app = create_app(settings(allowed_sub=None), store=store, identity=google)
    with TestClient(app, base_url=PUBLIC_URL) as client:
        client_id = register(client)
        _, challenge = pkce()

        # When the owner signs in
        response = sign_in(client, client_id, challenge)

    # Then they are refused, and shown the ID to configure
    assert response.status_code == 403
    assert OWNER_SUB in response.text


# The consent page -----------------------------------------------------------


def test_the_consent_page_names_the_client_and_where_it_sends_you(client):
    # Given a client called Claude that registered Claude's callback
    client_id = register(client)
    _, challenge = pkce()
    authorize = start_sign_in(client, client_id, challenge)

    # When the browser lands on the consent page
    page = client.get(authorize.headers["location"])

    # Then it names both, and cannot be framed by another site
    assert "Claude" in page.text
    assert "claude.ai" in page.text
    assert page.headers["x-frame-options"] == "DENY"


def test_another_site_cannot_approve_on_your_behalf(client, google):
    # Given a sign-in an attacker started with a client they registered
    client_id = register(client)
    _, challenge = pkce()
    authorize = start_sign_in(client, client_id, challenge)
    request_id = query(authorize.headers["location"])["request"]

    # When their page submits the consent form from your browser, which never
    # loaded the consent page and so holds no consent cookie
    client.cookies.clear()
    response = client.post(
        "/oauth/consent",
        data={"request": request_id, "decision": "allow"},
        follow_redirects=False,
    )

    # Then nothing is approved and Google is never asked
    assert response.status_code == 403
    assert "location" not in response.headers
    assert google.verifiers == []


def test_a_consent_cookie_from_another_sign_in_does_not_count(client):
    # Given the owner loaded the consent page for one sign-in
    client_id = register(client)
    _, challenge = pkce()
    first = start_sign_in(client, client_id, challenge)
    client.get(first.headers["location"])
    held = client.cookies.get(CONSENT_COOKIE)

    # When a second sign-in is approved with only that first cookie
    second = start_sign_in(client, client_id, challenge)
    client.cookies.clear()
    client.cookies.set(
        CONSENT_COOKIE, held, domain="libris.test", path="/oauth/consent"
    )
    response = client.post(
        "/oauth/consent",
        data={
            "request": query(second.headers["location"])["request"],
            "decision": "allow",
        },
        follow_redirects=False,
    )

    # Then it is refused
    assert response.status_code == 403


def test_denying_sends_the_client_an_access_denied(client):
    # Given a sign-in at the consent page
    client_id = register(client)
    _, challenge = pkce()

    # When the person denies it
    response = consent(client, client_id, challenge, decision="deny")

    # Then the client hears access_denied, with its state
    assert response.status_code == 303
    assert query(response.headers["location"]) == {
        "error": "access_denied",
        "state": "client-state",
    }


def test_a_sign_in_cannot_be_finished_twice(client):
    # Given a sign-in Google has already returned from once
    client_id = register(client)
    _, challenge = pkce()
    to_google = consent(client, client_id, challenge)
    state = query(to_google.headers["location"])["state"]
    client.get(
        "/oauth/google/callback",
        params={"state": state, "code": "g"},
        follow_redirects=False,
    )

    # When the same callback arrives again
    again = client.get(
        "/oauth/google/callback",
        params={"state": state, "code": "g"},
        follow_redirects=False,
    )

    # Then it issues nothing
    assert again.status_code == 400


def test_a_token_for_another_resource_is_not_issued(client):
    # Given a client asking for a token meant for some other server
    client_id = register(client)
    _, challenge = pkce()

    # When it starts a sign-in
    response = start_sign_in(
        client, client_id, challenge, resource="https://elsewhere.test/mcp"
    )

    # Then it is refused before anyone is asked to sign in
    assert "consent" not in response.headers.get("location", "")
    assert query(response.headers["location"])["error"] == "invalid_target"


# Tokens ---------------------------------------------------------------------


def test_a_code_works_once(client):
    # Given a code that has already been exchanged
    client_id = register(client)
    verifier, challenge = pkce()
    code = query(sign_in(client, client_id, challenge).headers["location"])["code"]
    assert exchange(client, client_id, code, verifier).status_code == 200

    # When it is presented again
    again = exchange(client, client_id, code, verifier)

    # Then it is refused
    assert again.status_code == 400
    assert again.json()["error"] == "invalid_grant"


def test_a_code_needs_the_verifier_it_was_started_with(client):
    # Given a code from a sign-in started with one PKCE challenge
    client_id = register(client)
    _, challenge = pkce()
    code = query(sign_in(client, client_id, challenge).headers["location"])["code"]

    # When it is exchanged with some other verifier
    other_verifier, _ = pkce()
    response = exchange(client, client_id, code, other_verifier)

    # Then it is refused
    assert response.status_code == 400


def test_a_refresh_token_rotates(client):
    # Given a client holding a refresh token
    client_id, issued = tokens(client)

    def refresh(token: str):
        return client.post(
            "/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": token,
                "client_id": client_id,
            },
        )

    # When it refreshes
    renewed = refresh(issued["refresh_token"])

    # Then it gets a new pair that works
    assert renewed.status_code == 200, renewed.text
    assert renewed.json()["refresh_token"] != issued["refresh_token"]
    assert call_ping(client, renewed.json()["access_token"]).status_code == 200
    # And the old refresh token is spent
    assert refresh(issued["refresh_token"]).status_code == 400


def test_refresh_tokens_are_not_stored_as_issued(client, store):
    # Given a client holding a refresh token
    _, issued = tokens(client)

    # Then the store holds no copy of it that could be presented
    assert issued["refresh_token"] not in store.refresh_tokens
    assert len(store.refresh_tokens) == 1


def test_a_token_signed_with_another_key_is_refused(client):
    # Given a token shaped exactly like Libris's, signed with some other key
    now = int(time.time())
    forged = jwt.encode(
        {
            "iss": PUBLIC_URL,
            "aud": RESOURCE,
            "sub": OWNER_SUB,
            "client_id": "x",
            "scope": "libris",
            "iat": now,
            "exp": now + 600,
        },
        "f" * 48,
        algorithm="HS256",
    )

    # Then it reaches no tool
    assert call_ping(client, forged).status_code == 401


def test_a_token_for_another_audience_is_refused(client):
    # Given a token signed with Libris's own key but issued for another resource
    now = int(time.time())
    elsewhere = jwt.encode(
        {
            "iss": PUBLIC_URL,
            "aud": "https://elsewhere.test/mcp",
            "sub": OWNER_SUB,
            "client_id": "x",
            "scope": "libris",
            "iat": now,
            "exp": now + 600,
        },
        SIGNING_KEY,
        algorithm="HS256",
    )

    # Then it reaches no tool
    assert call_ping(client, elsewhere).status_code == 401


def test_an_expired_token_is_refused(client):
    # Given a correctly signed token that expired a minute ago
    now = int(time.time())
    expired = jwt.encode(
        {
            "iss": PUBLIC_URL,
            "aud": RESOURCE,
            "sub": OWNER_SUB,
            "client_id": "x",
            "scope": "libris",
            "iat": now - 3600,
            "exp": now - 60,
        },
        SIGNING_KEY,
        algorithm="HS256",
    )

    # Then it reaches no tool
    assert call_ping(client, expired).status_code == 401


def test_changing_the_owner_cuts_off_tokens_already_issued(google, store):
    # Given a token issued to the owner
    with TestClient(
        create_app(settings(), store=store, identity=google), base_url=PUBLIC_URL
    ) as client:
        client_id, issued = tokens(client)

    # When the server restarts allowing a different account
    app = create_app(settings(allowed_sub="someone-else"), store=store, identity=google)
    with TestClient(app, base_url=PUBLIC_URL) as client:
        # Then the old token reaches no tool, and cannot be refreshed either
        assert call_ping(client, issued["access_token"]).status_code == 401
        refreshed = client.post(
            "/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": issued["refresh_token"],
                "client_id": client_id,
            },
        )
        assert refreshed.status_code == 400


def test_clients_and_refresh_tokens_survive_a_restart(google, store):
    # Given a client that signed in before the server restarted
    with TestClient(
        create_app(settings(), store=store, identity=google), base_url=PUBLIC_URL
    ) as client:
        client_id, issued = tokens(client)

    # When it refreshes against the restarted server
    with TestClient(
        create_app(settings(), store=store, identity=google), base_url=PUBLIC_URL
    ) as client:
        renewed = client.post(
            "/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": issued["refresh_token"],
                "client_id": client_id,
            },
        )

    # Then it stays signed in, which is what scale-to-zero needs
    assert renewed.status_code == 200, renewed.text


# Reading Google's answer ----------------------------------------------------


def id_token(**claims) -> str:
    base = {
        "iss": "https://accounts.google.com",
        "aud": "google-client",
        "sub": OWNER_SUB,
        "email": "owner@gmail.com",
        "email_verified": True,
        "exp": time.time() + 300,
    }
    return jwt.encode(
        base | claims, "unused-by-the-reader-" + "x" * 32, algorithm="HS256"
    )


def test_an_id_token_from_google_names_the_account():
    account = account_from_id_token(id_token(), client_id="google-client")
    assert account == GoogleAccount(
        sub=OWNER_SUB, email="owner@gmail.com", email_verified=True
    )


@pytest.mark.parametrize(
    "claims",
    [
        {"aud": "another-client"},
        {"iss": "https://evil.test"},
        {"exp": time.time() - 1},
        {"sub": ""},
    ],
    ids=["another audience", "another issuer", "expired", "no account"],
)
def test_an_id_token_that_is_not_for_libris_is_refused(claims):
    with pytest.raises(SignInFailed):
        account_from_id_token(id_token(**claims), client_id="google-client")


# Configuration --------------------------------------------------------------


def test_missing_settings_are_named_together():
    with pytest.raises(HostedConfigError) as error:
        HostedSettings.from_env({"LIBRIS_PUBLIC_URL": PUBLIC_URL})
    message = str(error.value)
    for name in (
        "LIBRIS_GOOGLE_CLIENT_ID",
        "LIBRIS_GOOGLE_CLIENT_SECRET",
        "LIBRIS_TOKEN_SIGNING_KEY",
    ):
        assert name in message


def test_a_short_signing_key_is_refused(google, store):
    with pytest.raises(ValueError):
        create_app(settings(signing_key="short"), store=store, identity=google)
