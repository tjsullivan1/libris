"""The hosted server's sign-in, driven end to end the way Claude and Gemini drive it.

ADR 0035 makes Libris its own authorization server. The SDK parses every OAuth
request, so what is tested here is what Libris decides: who gets a code, what a
token is good for, and what a page on another site cannot make a browser do.
Google is replaced by a fake that names whichever account a test chooses.
"""

import hashlib
import json
import secrets
import threading
import time
from base64 import urlsafe_b64encode
from collections.abc import Iterator
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlsplit

import pytest

pytest.importorskip("mcp")

import anyio  # noqa: E402
import httpx  # noqa: E402
import jwt  # noqa: E402
from cosmos_fake import FakeContainer  # noqa: E402
from mcp.server.auth.provider import TokenError  # noqa: E402
from mcp.shared.auth import OAuthClientInformationFull  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

from libris.api import BookCandidate  # noqa: E402
from libris.cosmos_store import CosmosStore, push_shelf  # noqa: E402
from libris.hosted import HostedConfigError, HostedSettings, create_app  # noqa: E402
from libris.markdown import create_book_note  # noqa: E402
from libris.oauth import (  # noqa: E402
    CONSENT_COOKIE,
    GoogleAccount,
    GoogleIdentity,
    LibrisAuthProvider,
    MemoryAuthStore,
    SignInFailed,
    account_from_id_token,
)

PUBLIC_URL = "https://libris.test"
RESOURCE = f"{PUBLIC_URL}/mcp"
CLAUDE_CALLBACK = "https://claude.ai/api/mcp/auth_callback"
# What a browser sends with the consent form when it came from Libris's page.
SAME_ORIGIN = {"Origin": PUBLIC_URL}
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
def google() -> FakeGoogle:
    return FakeGoogle()


@pytest.fixture
def store() -> MemoryAuthStore:
    return MemoryAuthStore()


@pytest.fixture
def client(google: FakeGoogle, store: MemoryAuthStore) -> Iterator[TestClient]:
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
) -> httpx.Response:
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
) -> httpx.Response:
    """Through `/authorize` and the consent page. Returns the decision's response."""
    authorize = start_sign_in(client, client_id, challenge)
    assert authorize.status_code == 302, authorize.text
    page = client.get(authorize.headers["location"])
    assert page.status_code == 200
    request_id = query(authorize.headers["location"])["request"]
    return client.post(
        "/oauth/consent",
        headers={"Origin": origin_a_browser_sends(page)},
        data={"request": request_id, "decision": decision},
        follow_redirects=False,
    )


def origin_a_browser_sends(page: httpx.Response) -> str:
    """The Origin a browser puts on a form POST from this page back to itself.

    Fetch's "append a request Origin header" decides it from the page's
    Referrer-Policy: `no-referrer` sends `null` even to the page's own origin.
    The consent tests once sent the right Origin by hand, so a page header that
    made every real browser send `null` passed them all and locked the owner out.
    """
    policy = page.headers.get("referrer-policy", "strict-origin-when-cross-origin")
    return "null" if policy.strip().lower() == "no-referrer" else PUBLIC_URL


def sign_in(client: TestClient, client_id: str, challenge: str) -> httpx.Response:
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


def exchange(
    client: TestClient,
    client_id: str,
    code: str,
    verifier: str,
    secret: str | None = None,
) -> httpx.Response:
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": CLAUDE_CALLBACK,
        "client_id": client_id,
        "code_verifier": verifier,
        "resource": RESOURCE,
    }
    if secret is not None:
        data["client_secret"] = secret
    return client.post("/token", data=data)


def refresh(
    client: TestClient, client_id: str, token: str, secret: str | None = None
) -> httpx.Response:
    data = {
        "grant_type": "refresh_token",
        "refresh_token": token,
        "client_id": client_id,
    }
    if secret is not None:
        data["client_secret"] = secret
    return client.post("/token", data=data)


def register_confidential(client: TestClient) -> tuple[str, str]:
    """Register the way Claude actually did: a web client holding a secret."""
    response = client.post(
        "/register",
        json={
            "redirect_uris": [CLAUDE_CALLBACK],
            "client_name": "Claude",
            "token_endpoint_auth_method": "client_secret_post",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "application_type": "web",
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["client_secret"]
    return body["client_id"], body["client_secret"]


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


def rpc(
    client: TestClient, access_token: str | None, method: str, params: dict
) -> httpx.Response:
    """One MCP request, as a client sends it over Streamable HTTP."""
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    return client.post("/mcp", content=json.dumps(body), headers=headers)


def list_tools(client: TestClient, access_token: str | None) -> httpx.Response:
    """Ask for the tools: the cheapest call that still needs a valid token."""
    return rpc(client, access_token, "tools/list", {})


def call_tool(
    client: TestClient, access_token: str, name: str, arguments: dict
) -> dict:
    """Call a tool and hand back its JSON-RPC result, failing on a transport error."""
    response = rpc(
        client, access_token, "tools/call", {"name": name, "arguments": arguments}
    )
    assert response.status_code == 200, response.text
    return response.json()["result"]


# Discovery ------------------------------------------------------------------


def test_an_unauthenticated_call_points_the_client_at_the_metadata(
    client: TestClient,
) -> None:
    # When a client calls the MCP endpoint without a token
    response = list_tools(client, None)

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


def test_the_authorization_server_offers_what_claude_and_gemini_need(
    client: TestClient,
) -> None:
    # When a client reads the authorization server's metadata
    metadata = client.get("/.well-known/oauth-authorization-server").json()

    # Then it can register itself and must use PKCE
    assert metadata["registration_endpoint"] == f"{PUBLIC_URL}/register"
    assert metadata["code_challenge_methods_supported"] == ["S256"]


# Signing in -----------------------------------------------------------------


def test_the_owner_signs_in_and_the_token_reaches_the_tools(
    client: TestClient, google: FakeGoogle
) -> None:
    # Given a client that registered and signed in as the owner
    _, issued = tokens(client)

    # When it asks for the tools with the token it was given
    response = list_tools(client, issued["access_token"])

    # Then it is offered the Library's tools
    assert response.status_code == 200, response.text
    offered = {tool["name"] for tool in response.json()["result"]["tools"]}
    assert offered == {"search_library", "find_book", "add_book", "update_book"}
    # And Google was asked with the verifier for the challenge Libris sent it
    assert len(google.verifiers) == 1


def test_the_client_gets_its_own_state_back(client: TestClient) -> None:
    # Given a sign-in started with a state of the client's choosing
    client_id = register(client)
    _, challenge = pkce()

    # When the browser is sent back to the client
    back = sign_in(client, client_id, challenge)

    # Then it goes to the client's registered callback, carrying that state
    assert back.headers["location"].startswith(CLAUDE_CALLBACK)
    assert query(back.headers["location"])["state"] == "client-state"


def test_another_google_account_gets_no_code(
    client: TestClient, google: FakeGoogle
) -> None:
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


def test_an_unverified_email_gets_no_code_even_for_the_owners_id(
    client: TestClient, google: FakeGoogle
) -> None:
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


def test_with_no_owner_configured_nobody_gets_in(
    google: FakeGoogle, store: MemoryAuthStore
) -> None:
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


def test_the_consent_page_names_the_client_and_where_it_sends_you(
    client: TestClient,
) -> None:
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


def test_another_site_cannot_approve_on_your_behalf(
    client: TestClient, google: FakeGoogle
) -> None:
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
        headers=SAME_ORIGIN,
        data={"request": request_id, "decision": "allow"},
        follow_redirects=False,
    )

    # Then nothing is approved and Google is never asked
    assert response.status_code == 403
    assert "location" not in response.headers
    assert google.verifiers == []


def test_a_consent_cookie_from_another_sign_in_does_not_count(
    client: TestClient,
) -> None:
    # Given the owner loaded the consent page for one sign-in
    client_id = register(client)
    _, challenge = pkce()
    first = start_sign_in(client, client_id, challenge)
    client.get(first.headers["location"])
    held = client.cookies.get(CONSENT_COOKIE)

    # When a second sign-in is approved with only that first cookie
    second = start_sign_in(client, client_id, challenge)
    client.cookies.clear()
    client.cookies.set(CONSENT_COOKIE, held, domain="libris.test", path="/")
    response = client.post(
        "/oauth/consent",
        headers=SAME_ORIGIN,
        data={
            "request": query(second.headers["location"])["request"],
            "decision": "allow",
        },
        follow_redirects=False,
    )

    # Then it is refused
    assert response.status_code == 403


def test_denying_sends_the_client_an_access_denied(client: TestClient) -> None:
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


def test_a_sign_in_cannot_be_finished_twice(client: TestClient) -> None:
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


def test_a_token_for_another_resource_is_not_issued(client: TestClient) -> None:
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


def test_a_code_works_once(client: TestClient) -> None:
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


def test_a_code_needs_the_verifier_it_was_started_with(client: TestClient) -> None:
    # Given a code from a sign-in started with one PKCE challenge
    client_id = register(client)
    _, challenge = pkce()
    code = query(sign_in(client, client_id, challenge).headers["location"])["code"]

    # When it is exchanged with some other verifier
    other_verifier, _ = pkce()
    response = exchange(client, client_id, code, other_verifier)

    # Then it is refused
    assert response.status_code == 400


def test_a_refresh_token_rotates(client: TestClient) -> None:
    # Given a client holding a refresh token
    client_id, issued = tokens(client)

    def refresh(token: str) -> httpx.Response:
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
    assert list_tools(client, renewed.json()["access_token"]).status_code == 200
    # And the old refresh token is spent
    assert refresh(issued["refresh_token"]).status_code == 400


def test_nothing_a_client_can_present_is_stored_as_issued(
    client: TestClient, store: MemoryAuthStore
) -> None:
    # Given a sign-in caught between the code being issued and exchanged, and
    # a client that went on to hold a refresh token
    client_id = register(client)
    verifier, challenge = pkce()
    code = query(sign_in(client, client_id, challenge).headers["location"])["code"]
    held_code = str(store.records)
    issued = exchange(client, client_id, code, verifier).json()

    # Then no record, of any kind, holds the code or the refresh token as
    # issued, so a copy of the store holds nothing that works
    assert code not in held_code
    everything = str(store.records)
    assert issued["refresh_token"] not in everything
    assert any(kind == "refresh" for kind, _ in store.records)


def reissued(client: TestClient, key: str = SIGNING_KEY, **changes: object) -> str:
    """A real sign-in's access token, re-signed with one thing changed.

    Starting from a real token keeps every other claim valid, including the
    grant, so a refusal can only be for the change under test. Tokens built
    from scratch went blind the day a new required claim arrived: they were
    refused for lacking it, whatever they were meant to test.
    """
    _, issued = tokens(client)
    claims = jwt.decode(issued["access_token"], options={"verify_signature": False})
    assert list_tools(client, issued["access_token"]).status_code == 200
    return jwt.encode({**claims, **changes}, key, algorithm="HS256")


def test_a_token_signed_with_another_key_is_refused(client: TestClient) -> None:
    # Given a real token re-signed with some other key
    forged = reissued(client, key="f" * 48)

    # Then it reaches no tool
    assert list_tools(client, forged).status_code == 401


def test_a_token_for_another_audience_is_refused(client: TestClient) -> None:
    # Given a real token, signed with Libris's own key, but for another resource
    elsewhere = reissued(client, aud="https://elsewhere.test/mcp")

    # Then it reaches no tool
    assert list_tools(client, elsewhere).status_code == 401


def test_a_token_from_another_issuer_is_refused(client: TestClient) -> None:
    # Given a real token, signed with Libris's own key, claiming another issuer
    impostor = reissued(client, iss="https://elsewhere.test")

    # Then it reaches no tool
    assert list_tools(client, impostor).status_code == 401


def test_an_expired_token_is_refused(client: TestClient) -> None:
    # Given a real token, correctly signed, that expired a minute ago
    now = int(time.time())
    expired = reissued(client, iat=now - 3600, exp=now - 60)

    # Then it reaches no tool
    assert list_tools(client, expired).status_code == 401


def test_changing_the_owner_cuts_off_tokens_already_issued(
    google: FakeGoogle, store: MemoryAuthStore
) -> None:
    # Given a token issued to the owner
    with TestClient(
        create_app(settings(), store=store, identity=google), base_url=PUBLIC_URL
    ) as client:
        client_id, issued = tokens(client)

    # When the server restarts allowing a different account
    app = create_app(settings(allowed_sub="someone-else"), store=store, identity=google)
    with TestClient(app, base_url=PUBLIC_URL) as client:
        # Then the old token reaches no tool, and cannot be refreshed either
        assert list_tools(client, issued["access_token"]).status_code == 401
        refreshed = client.post(
            "/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": issued["refresh_token"],
                "client_id": client_id,
            },
        )
        assert refreshed.status_code == 400


def test_clients_and_refresh_tokens_survive_a_restart(
    google: FakeGoogle, store: MemoryAuthStore
) -> None:
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


def id_token(**claims: object) -> str:
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


def test_an_id_token_from_google_names_the_account() -> None:
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
def test_an_id_token_that_is_not_for_libris_is_refused(claims: dict) -> None:
    with pytest.raises(SignInFailed):
        account_from_id_token(id_token(**claims), client_id="google-client")


# Configuration --------------------------------------------------------------


def test_missing_settings_are_named_together() -> None:
    with pytest.raises(HostedConfigError) as error:
        HostedSettings.from_env({"LIBRIS_PUBLIC_URL": PUBLIC_URL})
    message = str(error.value)
    for name in (
        "LIBRIS_GOOGLE_CLIENT_ID",
        "LIBRIS_GOOGLE_CLIENT_SECRET",
        "LIBRIS_TOKEN_SIGNING_KEY",
    ):
        assert name in message


def test_a_short_signing_key_is_refused(
    google: FakeGoogle, store: MemoryAuthStore
) -> None:
    with pytest.raises(ValueError):
        create_app(settings(signing_key="short"), store=store, identity=google)


# Scaling to zero -------------------------------------------------------------


def test_a_sign_in_survives_a_restart_while_the_person_is_at_google(
    google: FakeGoogle, store: MemoryAuthStore
) -> None:
    # Given a sign-in that reached Google, and then the app scaled to zero
    # while the person was on Google's page, so nothing reached Libris
    app = create_app(settings(), store=store, identity=google)
    with TestClient(app, base_url=PUBLIC_URL) as before:
        client_id = register(before)
        verifier, challenge = pkce()
        to_google = consent(before, client_id, challenge)
        browser_cookies = before.cookies
    state = query(to_google.headers["location"])["state"]

    # When Google sends them back to a freshly started instance
    app = create_app(settings(), store=store, identity=google)
    with TestClient(app, base_url=PUBLIC_URL) as after:
        # The same browser, so the same cookies: only the server restarted
        after.cookies = browser_cookies
        back = after.get(
            "/oauth/google/callback",
            params={"state": state, "code": "google-code"},
            follow_redirects=False,
        )
        code = query(back.headers["location"])["code"]

        # Then the sign-in finishes, and its code works on yet another start
    app = create_app(settings(), store=store, identity=google)
    with TestClient(app, base_url=PUBLIC_URL) as later:
        assert exchange(later, client_id, code, verifier).status_code == 200


# Open registration -----------------------------------------------------------


def test_registrations_beyond_the_hourly_limit_are_refused(
    google: FakeGoogle, store: MemoryAuthStore
) -> None:
    # Given a limit of two registrations an hour
    now = [1000.0]
    app = create_app(
        settings(),
        store=store,
        identity=google,
        registrations_per_hour=2,
        clock=lambda: now[0],
    )
    with TestClient(app, base_url=PUBLIC_URL) as client:
        register(client)
        register(client)
        stored = len(store.records)

        # When a third arrives within the hour
        refused = client.post("/register", json={"redirect_uris": [CLAUDE_CALLBACK]})

        # Then it is refused as too many, says when to retry, and stores nothing
        assert refused.status_code == 429
        assert int(refused.headers["retry-after"]) > 0
        assert len(store.records) == stored

        # And once the hour has passed, registration works again
        now[0] += 3601
        register(client)


def test_a_client_nobody_uses_is_forgotten(
    google: FakeGoogle, store: MemoryAuthStore
) -> None:
    # Given a client that registered and never signed in
    with TestClient(
        create_app(settings(), store=store, identity=google), base_url=PUBLIC_URL
    ) as client:
        client_id = register(client)
        _, challenge = pkce()

        # When the store reads it back after its idle time has passed
        record = store.get("client", client_id)
        record["expires_at"] = int(time.time()) - 1
        store.put("client", client_id, record, record["expires_at"])

        # Then it is treated as never having registered
        response = start_sign_in(client, client_id, challenge)
        assert response.status_code == 400


def test_issuing_a_token_keeps_a_client_from_expiring(
    client: TestClient, store: MemoryAuthStore
) -> None:
    # Given a client close to its idle expiry
    client_id = register(client)
    record = store.get("client", client_id)
    record["expires_at"] = int(time.time()) + 60
    store.put("client", client_id, record, record["expires_at"])

    # When it signs in and is issued tokens
    verifier, challenge = pkce()
    code = query(sign_in(client, client_id, challenge).headers["location"])["code"]
    assert exchange(client, client_id, code, verifier).status_code == 200

    # Then its expiry has moved out to a full idle period again
    assert store.get("client", client_id)["expires_at"] > time.time() + 30 * 24 * 3600


# The consent cookie ----------------------------------------------------------


def test_the_consent_cookie_cannot_travel_cross_site(client: TestClient) -> None:
    # Given the consent page as a browser receives it
    client_id = register(client)
    _, challenge = pkce()
    page = client.get(start_sign_in(client, client_id, challenge).headers["location"])

    # Then the cookie is one a browser never sends with another site's form,
    # never sends over http, never shows to script, and sends only to the form
    cookie = page.headers["set-cookie"].lower()
    assert "samesite=strict" in cookie
    assert "secure" in cookie
    assert "httponly" in cookie
    assert cookie.startswith("__host-")
    assert "path=/;" in cookie or cookie.endswith("path=/")
    assert "domain=" not in cookie


# A client with a secret, as Claude registered -------------------------------


def test_a_confidential_client_signs_in_and_refreshes_across_restarts(
    google: FakeGoogle, store: MemoryAuthStore
) -> None:
    # Given a client registered as Claude really did, with a secret, which
    # signed in before the app restarted
    with TestClient(
        create_app(settings(), store=store, identity=google), base_url=PUBLIC_URL
    ) as before:
        client_id, secret = register_confidential(before)
        verifier, challenge = pkce()
        code = query(sign_in(before, client_id, challenge).headers["location"])["code"]
        issued = exchange(before, client_id, code, verifier, secret)
        assert issued.status_code == 200, issued.text

    # When it refreshes against the restarted app, presenting its secret
    with TestClient(
        create_app(settings(), store=store, identity=google), base_url=PUBLIC_URL
    ) as after:
        renewed = refresh(after, client_id, issued.json()["refresh_token"], secret)

        # Then the secret survived the store and the restart, and the new
        # token reaches the tools
        assert renewed.status_code == 200, renewed.text
        assert list_tools(after, renewed.json()["access_token"]).status_code == 200


def test_a_confidential_client_without_its_secret_gets_nothing(
    client: TestClient,
) -> None:
    # Given a confidential client holding a code
    client_id, secret = register_confidential(client)
    verifier, challenge = pkce()
    code = query(sign_in(client, client_id, challenge).headers["location"])["code"]

    # When the code is exchanged with the wrong secret, then with none
    wrong = exchange(client, client_id, code, verifier, "not-the-secret")
    missing = exchange(client, client_id, code, verifier)

    # Then both are refused, and the code still works with the right one
    assert wrong.status_code == 401
    assert missing.status_code == 401
    assert exchange(client, client_id, code, verifier, secret).status_code == 200


# Revocation ------------------------------------------------------------------


def revoke(client: TestClient, client_id: str, token: str) -> httpx.Response:
    # The SDK's revocation request declares client_secret as `str | None` with
    # no default, which pydantic reads as required, so a public client has to
    # send it empty. Claude registers with a secret and always sends one.
    return client.post(
        "/revoke", data={"token": token, "client_id": client_id, "client_secret": ""}
    )


def test_revoking_the_access_token_ends_the_whole_sign_in(client: TestClient) -> None:
    # Given a client holding both tokens
    client_id, issued = tokens(client)

    # When it revokes with the access token, as a client disconnecting may
    assert revoke(client, client_id, issued["access_token"]).status_code == 200

    # Then the access token stops working now, not in an hour, and the
    # refresh token from the same sign-in cannot replace it
    assert list_tools(client, issued["access_token"]).status_code == 401
    assert refresh(client, client_id, issued["refresh_token"]).status_code == 400


def test_revoking_the_refresh_token_ends_the_whole_sign_in(client: TestClient) -> None:
    # Given a client holding both tokens
    client_id, issued = tokens(client)

    # When it revokes with the refresh token
    assert revoke(client, client_id, issued["refresh_token"]).status_code == 200

    # Then the access token issued alongside it stops working too
    assert list_tools(client, issued["access_token"]).status_code == 401
    assert refresh(client, client_id, issued["refresh_token"]).status_code == 400


def test_revoking_after_a_refresh_still_ends_the_sign_in(client: TestClient) -> None:
    # Given a sign-in whose tokens have been rotated once
    client_id, issued = tokens(client)
    renewed = refresh(client, client_id, issued["refresh_token"]).json()

    # When the client revokes the token it now holds
    assert revoke(client, client_id, renewed["access_token"]).status_code == 200

    # Then every token from that sign-in is dead, old and new
    assert list_tools(client, renewed["access_token"]).status_code == 401
    assert refresh(client, client_id, renewed["refresh_token"]).status_code == 400


def test_revoking_one_sign_in_leaves_another_alone(client: TestClient) -> None:
    # Given two separate sign-ins, as two devices would have
    first_id, first = tokens(client)
    _, second = tokens(client)

    # When the first is revoked
    revoke(client, first_id, first["access_token"])

    # Then the second still works
    assert list_tools(client, second["access_token"]).status_code == 200


def test_a_replayed_refresh_token_ends_the_thiefs_sign_in_too(
    client: TestClient,
) -> None:
    # Given a refresh token that a thief redeemed before its owner did
    client_id, issued = tokens(client)
    stolen = refresh(client, client_id, issued["refresh_token"]).json()
    assert list_tools(client, stolen["access_token"]).status_code == 200

    # When the owner presents the same token, now spent
    replayed = refresh(client, client_id, issued["refresh_token"])

    # Then the owner is refused, and the thief's tokens stop working too:
    # refusing the replay alone would leave the thief holding a live sign-in
    assert replayed.status_code == 400
    assert list_tools(client, stolen["access_token"]).status_code == 401
    assert refresh(client, client_id, stolen["refresh_token"]).status_code == 400


def test_a_replay_ends_only_its_own_sign_in(client: TestClient) -> None:
    # Given two sign-ins, and a replay within the first
    first_id, first = tokens(client)
    _, second = tokens(client)
    refresh(client, first_id, first["refresh_token"])
    refresh(client, first_id, first["refresh_token"])

    # Then the other sign-in is untouched
    assert list_tools(client, second["access_token"]).status_code == 200


def test_sign_ins_beyond_a_clients_hourly_limit_are_refused(
    google: FakeGoogle, store: MemoryAuthStore
) -> None:
    # Given a limit of two sign-ins per client an hour, and two clients
    now = [1000.0]
    app = create_app(
        settings(),
        store=store,
        identity=google,
        sign_ins_per_client_per_hour=2,
        clock=lambda: now[0],
    )
    with TestClient(app, base_url=PUBLIC_URL) as client:
        flooder = register(client)
        connecting = register(client)
        _, challenge = pkce()

        # When one client floods /authorize with malformed requests, which the
        # SDK rejects before Libris sees them, and then with valid ones
        for _ in range(20):
            client.get(
                "/authorize", params={"client_id": flooder}, follow_redirects=False
            )
        start_sign_in(client, flooder, challenge)
        start_sign_in(client, flooder, challenge)
        stored = len(store.records)
        refused = start_sign_in(client, flooder, challenge)

        # Then only its valid sign-ins counted, its third is refused as
        # temporarily unavailable on its own redirect URI, and nothing is written
        assert refused.status_code == 302
        assert query(refused.headers["location"])["error"] == "temporarily_unavailable"
        assert len(store.records) == stored

        # And the client a person is actually connecting is untouched
        assert (
            "consent"
            in start_sign_in(client, connecting, challenge).headers["location"]
        )

        # And once the hour has passed, the flooder can sign in again
        now[0] += 3601
        assert (
            "consent" in start_sign_in(client, flooder, challenge).headers["location"]
        )


def test_malformed_registrations_do_not_use_up_the_limit(
    google: FakeGoogle, store: MemoryAuthStore
) -> None:
    # Given a limit of two registrations an hour
    app = create_app(settings(), store=store, identity=google, registrations_per_hour=2)
    with TestClient(app, base_url=PUBLIC_URL) as client:
        # When a flood of malformed registrations arrives first
        for _ in range(20):
            assert (
                client.post("/register", json={"redirect_uris": []}).status_code == 400
            )

        # Then a real connector can still register
        register(client)
        register(client)


def provider_over(store: MemoryAuthStore, google: FakeGoogle) -> LibrisAuthProvider:
    return LibrisAuthProvider(
        public_url=PUBLIC_URL,
        signing_key=SIGNING_KEY,
        allowed_sub=OWNER_SUB,
        store=store,
        identity=google,
    )


def signed_in(
    store: MemoryAuthStore, google: FakeGoogle
) -> tuple[OAuthClientInformationFull, dict]:
    """A client and its tokens, from a real sign-in over HTTP."""
    with TestClient(
        create_app(settings(), store=store, identity=google), base_url=PUBLIC_URL
    ) as client:
        client_id, issued = tokens(client)
    registered = OAuthClientInformationFull.model_validate(
        store.get("client", client_id)["client"]
    )
    return registered, issued


def test_two_requests_spending_one_refresh_token_at_once_end_the_sign_in(
    google: FakeGoogle, store: MemoryAuthStore
) -> None:
    # Given a refresh token, and two requests that both read it unspent before
    # either marks it spent
    registered, issued = signed_in(store, google)
    provider = provider_over(store, google)
    both_have_read = threading.Barrier(2, timeout=5)

    def hold_refresh(kind: str, key: str) -> None:
        if kind == "refresh":
            both_have_read.wait()

    store.between_read_and_write = hold_refresh
    outcomes: list[object] = []

    async def spend() -> None:
        token = await provider.load_refresh_token(registered, issued["refresh_token"])
        try:
            outcomes.append(
                await provider.exchange_refresh_token(registered, token, [])
            )
        except TokenError as refused:
            outcomes.append(refused)

    async def race() -> None:
        async with anyio.create_task_group() as group:
            group.start_soon(spend)
            group.start_soon(spend)

    # When they race
    anyio.run(race)
    store.between_read_and_write = lambda kind, key: None

    # Then at least one is refused as a replay, and nothing from the sign-in
    # still works: two parties held one token. Which order the rest happens
    # in is the scheduler's choice, and both are safe. Either the refusal
    # revokes the grant before the winner can extend it, so the winner is
    # refused too, or the winner gets tokens the refusal then kills.
    won = [o for o in outcomes if not isinstance(o, TokenError)]
    lost = [o for o in outcomes if isinstance(o, TokenError)]
    assert len(outcomes) == 2 and lost
    assert {refused.error for refused in lost} == {"invalid_grant"}
    for tokens_won in won:
        assert anyio.run(provider.load_access_token, tokens_won.access_token) is None
        assert (
            anyio.run(provider.load_refresh_token, registered, tokens_won.refresh_token)
            is None
        )
    assert not [key for kind, key in store.records if kind == "grant"]
    assert anyio.run(provider.load_access_token, issued["access_token"]) is None


def test_a_refresh_cannot_bring_back_a_grant_revoked_while_it_ran(
    google: FakeGoogle, store: MemoryAuthStore
) -> None:
    # Given a refresh that has spent its token, and is about to extend the
    # grant, when the sign-in is revoked
    registered, issued = signed_in(store, google)
    provider = provider_over(store, google)

    def revoke_first(kind: str, key: str) -> None:
        if kind == "grant":
            store.delete("grant", key)

    store.between_read_and_write = revoke_first

    async def refresh_it() -> object:
        token = await provider.load_refresh_token(registered, issued["refresh_token"])
        try:
            return await provider.exchange_refresh_token(registered, token, [])
        except TokenError as refused:
            return refused

    # When the refresh carries on
    outcome = anyio.run(refresh_it)
    store.between_read_and_write = lambda kind, key: None

    # Then it is refused, and the revocation stands: no grant came back, so
    # the access token issued before it is dead as well
    assert isinstance(outcome, TokenError)
    assert not [key for kind, key in store.records if kind == "grant"]
    assert anyio.run(provider.load_access_token, issued["access_token"]) is None


def test_a_google_link_approved_in_another_browser_gives_the_owner_no_code(
    google: FakeGoogle, store: MemoryAuthStore
) -> None:
    # Given an attacker who registered a client and approved its consent page
    # in their own browser, and so holds a Google sign-in link
    app = create_app(settings(), store=store, identity=google)
    # A second browser on the same app. Not entered as a context manager: that
    # would start the app's lifespan a second time, and nothing here uses /mcp.
    owner = TestClient(app, base_url=PUBLIC_URL)
    with TestClient(app, base_url=PUBLIC_URL) as attacker:
        client_id = register(attacker)
        _, challenge = pkce()
        to_google = consent(attacker, client_id, challenge)
        state = query(to_google.headers["location"])["state"]

        # When the owner, sent that link, signs in with Google, which returns
        # them to Libris in their own browser
        back = owner.get(
            "/oauth/google/callback",
            params={"state": state, "code": "google-code"},
            follow_redirects=False,
        )

        # Then no code goes anywhere, Google is never asked about the owner's
        # account, and the attacker cannot finish it from their browser either
        assert back.status_code == 403
        assert "location" not in back.headers
        assert google.verifiers == []
        retry = attacker.get(
            "/oauth/google/callback",
            params={"state": state, "code": "google-code"},
            follow_redirects=False,
        )
        assert retry.status_code == 400


def test_the_approval_cookie_survives_googles_redirect_back(client: TestClient) -> None:
    # Given consent approved in a browser
    client_id = register(client)
    _, challenge = pkce()
    to_google = consent(client, client_id, challenge)

    # Then the cookie it sets is one a browser sends on Google's cross-site
    # redirect back (Lax, not Strict), and only to the callback, over https,
    # never to script
    cookie = to_google.headers["set-cookie"].lower()
    assert "samesite=lax" in cookie
    assert cookie.startswith("__host-libris_approved=")
    assert "path=/;" in cookie or cookie.endswith("path=/")
    assert "domain=" not in cookie
    assert "secure" in cookie
    assert "httponly" in cookie


# The real Google exchange ----------------------------------------------------
#
# Everything above signs in through FakeGoogle. These drive GoogleIdentity
# itself against a stand-in for Google's token endpoint, so a wrong URL,
# verifier, redirect or credential fails here instead of on the first real
# sign-in.

GOOGLE_REDIRECT = f"{PUBLIC_URL}/oauth/google/callback"


def google_answering(
    answer: httpx.Response, seen: list[httpx.Request]
) -> GoogleIdentity:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return answer

    return GoogleIdentity(
        "google-client",
        "google-secret",  # noqa: S106 - a test value
        transport=httpx.MockTransport(handler),
    )


def test_the_exchange_sends_google_what_it_needs() -> None:
    # Given Google answering with an ID token for the owner
    seen: list[httpx.Request] = []
    identity = google_answering(
        httpx.Response(200, json={"id_token": id_token()}), seen
    )

    # When a code is exchanged
    account = anyio.run(
        lambda: identity.account(
            code="the-code", code_verifier="the-verifier", redirect_uri=GOOGLE_REDIRECT
        )
    )

    # Then it went to Google's token endpoint as a form, carrying the code,
    # the PKCE verifier, the same redirect URI and Libris's own credentials
    assert account.sub == OWNER_SUB
    (request,) = seen
    assert request.method == "POST"
    assert str(request.url) == "https://oauth2.googleapis.com/token"
    assert request.headers["content-type"] == "application/x-www-form-urlencoded"
    assert dict(parse_qs(request.content.decode())) == {
        "code": ["the-code"],
        "code_verifier": ["the-verifier"],
        "redirect_uri": [GOOGLE_REDIRECT],
        "client_id": ["google-client"],
        "client_secret": ["google-secret"],
        "grant_type": ["authorization_code"],
    }


@pytest.mark.parametrize(
    "answer",
    [
        httpx.Response(400, json={"error": "invalid_grant"}),
        httpx.Response(500, text="oops"),
        httpx.Response(200, json={"access_token": "no id token"}),
    ],
    ids=["code refused", "google failing", "no id token"],
)
def test_a_failed_exchange_signs_nobody_in(answer: httpx.Response) -> None:
    identity = google_answering(answer, [])
    with pytest.raises(SignInFailed):
        anyio.run(
            lambda: identity.account(
                code="c", code_verifier="v", redirect_uri=GOOGLE_REDIRECT
            )
        )


def test_the_sign_in_link_asks_google_only_for_identity_with_pkce() -> None:
    link = GoogleIdentity("google-client", "google-secret").authorization_url(  # noqa: S106
        state="the-state", code_challenge="the-challenge", redirect_uri=GOOGLE_REDIRECT
    )
    assert link.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    assert query(link) == {
        "client_id": "google-client",
        "redirect_uri": GOOGLE_REDIRECT,
        "response_type": "code",
        "scope": "openid email",
        "state": "the-state",
        "code_challenge": "the-challenge",
        "code_challenge_method": "S256",
        "prompt": "select_account",
    }


def test_a_burst_of_registrations_cannot_slip_past_the_limit(
    google: FakeGoogle, store: MemoryAuthStore
) -> None:
    # Given a limit of two registrations an hour
    app = create_app(settings(), store=store, identity=google, registrations_per_hour=2)

    async def burst() -> list[int]:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url=PUBLIC_URL) as http:

            async def one() -> int:
                response = await http.post(
                    "/register",
                    json={
                        "redirect_uris": [CLAUDE_CALLBACK],
                        "token_endpoint_auth_method": "none",
                    },
                )
                return response.status_code

            statuses: list[int] = []

            async def record() -> None:
                statuses.append(await one())

            async with anyio.create_task_group() as group:
                for _ in range(10):
                    group.start_soon(record)
            return statuses

    # When ten arrive at once, each waiting on the store while the others run
    statuses = anyio.run(burst)

    # Then exactly two get through, and only two clients were written
    assert sorted(statuses) == [201, 201] + [429] * 8
    assert len([key for kind, key in store.records if kind == "client"]) == 2


@pytest.mark.parametrize(
    "origin",
    [
        "https://attacker.salmonmushroom-205b8504.eastus2.azurecontainerapps.io",
        "https://libris.test.attacker.example",
        "http://libris.test",
        None,
    ],
    ids=["sibling container app", "lookalike host", "plain http", "no origin"],
)
def test_consent_is_refused_unless_it_comes_from_libris_itself(
    client: TestClient, google: FakeGoogle, origin: str | None
) -> None:
    # Given a browser holding the real consent cookie, as it would after the
    # owner was made to load the consent page
    client_id = register(client)
    _, challenge = pkce()
    authorize = start_sign_in(client, client_id, challenge)
    client.get(authorize.headers["location"])
    request_id = query(authorize.headers["location"])["request"]

    # When the approval is submitted from anywhere but Libris's own origin.
    # A sibling Container App is the same site, so SameSite=Strict lets the
    # cookie ride along, and only the Origin check is left to refuse it.
    headers = {} if origin is None else {"Origin": origin}
    response = client.post(
        "/oauth/consent",
        headers=headers,
        data={"request": request_id, "decision": "allow"},
        follow_redirects=False,
    )

    # Then nothing is approved and Google is never reached
    assert response.status_code == 403
    assert "location" not in response.headers
    assert google.verifiers == []


# Client secrets at rest -------------------------------------------------------


def test_a_client_secret_is_never_stored_readable(
    client: TestClient, store: MemoryAuthStore
) -> None:
    # Given a client registered with a secret, as Claude registers
    client_id, secret = register_confidential(client)

    # Then the store holds no copy of it, only a sealed one
    assert secret not in str(store.records)
    assert "sealed_secret" in store.get("client", client_id)

    # And the secret still authenticates the client
    verifier, challenge = pkce()
    code = query(sign_in(client, client_id, challenge).headers["location"])["code"]
    assert exchange(client, client_id, code, verifier, secret).status_code == 200


def test_a_sealed_secret_opens_only_under_the_key_that_sealed_it(
    google: FakeGoogle, store: MemoryAuthStore
) -> None:
    # Given a confidential client registered under one signing key
    with TestClient(
        create_app(settings(), store=store, identity=google), base_url=PUBLIC_URL
    ) as before:
        client_id, secret = register_confidential(before)

    # When the server restarts with a different key
    other = settings(signing_key="z" * 48)
    with TestClient(
        create_app(other, store=store, identity=google), base_url=PUBLIC_URL
    ) as after:
        _, challenge = pkce()
        # Then the client is unknown, and has to register again
        assert start_sign_in(after, client_id, challenge).status_code == 400


def test_a_sealed_secret_moved_to_another_client_does_not_open(
    client: TestClient, store: MemoryAuthStore
) -> None:
    # Given two confidential clients, and one's sealed secret copied onto the
    # other's record, as someone with write access to the store might try
    first_id, _ = register_confidential(client)
    second_id, _ = register_confidential(client)
    first = store.get("client", first_id)
    second = store.get("client", second_id)
    second["sealed_secret"] = first["sealed_secret"]
    store.put("client", second_id, second, second["expires_at"])

    # Then the moved secret does not open, and that client is unknown
    _, challenge = pkce()
    assert start_sign_in(client, second_id, challenge).status_code == 400


# Google failing mid-sign-in ---------------------------------------------------


def unreachable_google() -> GoogleIdentity:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to Google", request=request)

    return GoogleIdentity(
        "google-client",
        "google-secret",  # noqa: S106 - a test value
        transport=httpx.MockTransport(handler),
    )


def test_an_unreachable_google_fails_the_exchange_cleanly() -> None:
    with pytest.raises(SignInFailed):
        anyio.run(
            lambda: unreachable_google().account(
                code="c", code_verifier="v", redirect_uri=GOOGLE_REDIRECT
            )
        )


def test_an_unreadable_answer_from_google_fails_the_exchange_cleanly() -> None:
    identity = google_answering(httpx.Response(200, text="<html>proxy</html>"), [])
    with pytest.raises(SignInFailed):
        anyio.run(
            lambda: identity.account(
                code="c", code_verifier="v", redirect_uri=GOOGLE_REDIRECT
            )
        )


def test_an_unreachable_google_ends_the_sign_in_with_a_page(
    store: MemoryAuthStore,
) -> None:
    # Given the real Google identity, with Google unreachable
    app = create_app(settings(), store=store, identity=unreachable_google())
    with TestClient(app, base_url=PUBLIC_URL, raise_server_exceptions=False) as client:
        client_id = register(client)
        _, challenge = pkce()
        to_google = consent(client, client_id, challenge)
        state = query(to_google.headers["location"])["state"]

        # When Google sends the person back and Libris cannot reach it
        back = client.get(
            "/oauth/google/callback",
            params={"state": state, "code": "google-code"},
            follow_redirects=False,
        )

    # Then they see a sign-in failure, not a server error
    assert back.status_code == 502
    assert "Sign-in failed" in back.text


# The Library, answered from what sync pushed (#175) -------------------------


@pytest.fixture
def library(tmp_path) -> CosmosStore:
    """Cosmos as `libris sync` leaves it: Mistborn read, Dune being read."""
    shelf_dir = tmp_path / "shelf"
    shelf_dir.mkdir()
    create_book_note(
        BookCandidate(title="Mistborn", authors=["Brandon Sanderson"]),
        shelf_dir,
        overrides={"status": "Read"},
    )
    create_book_note(
        BookCandidate(title="Dune", authors=["Frank Herbert"]),
        shelf_dir,
        overrides={"status": "Reading"},
    )
    books, counts = FakeContainer(), FakeContainer()
    push_shelf(shelf_dir, books, counts)
    return CosmosStore(books, counts)


@pytest.fixture
def library_client(
    google: FakeGoogle, store: MemoryAuthStore, library: CosmosStore
) -> Iterator[TestClient]:
    app = create_app(settings(), store=store, identity=google, library=library)
    with TestClient(app, base_url=PUBLIC_URL) as test_client:
        yield test_client


def test_have_i_read_mistborn_is_answered_from_the_synced_library(
    library_client: TestClient,
) -> None:
    # Given a signed-in client
    _, issued = tokens(library_client)

    # When it asks the Library about Mistborn
    result = call_tool(
        library_client, issued["access_token"], "search_library", {"query": "mistborn"}
    )

    # Then the answer comes from what sync pushed, status and all
    assert not result["isError"], result
    found = result["structuredContent"]
    assert found["total"] == 1
    assert found["books"][0]["title"] == "Mistborn"
    assert found["books"][0]["status"] == "Read"


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("add_book", {"google_books_id": "v1"}),
        ("update_book", {"libris_id": "01D", "status": "Read"}),
    ],
    ids=["add", "update"],
)
def test_a_hosted_write_is_declined_and_says_so(
    library_client: TestClient, tool: str, arguments: dict
) -> None:
    # Given a signed-in client
    _, issued = tokens(library_client)

    # When it tries to change the Library from here
    result = call_tool(library_client, issued["access_token"], tool, arguments)

    # Then it is told remote writes are not available, and nothing was written
    assert result["isError"]
    message = result["content"][0]["text"]
    assert "Changes can't be made from here yet" in message
    assert "Nothing was written" in message


def test_find_book_reaches_google_books_from_the_hosted_server(
    library_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given Google Books knows one edition of Mistborn
    asked: list[dict] = []

    def lookup(**kwargs) -> list[BookCandidate]:
        asked.append(kwargs)
        return [
            BookCandidate(
                title="Mistborn",
                authors=["Brandon Sanderson"],
                google_books_id="mist-1",
            )
        ]

    monkeypatch.setattr("libris.service.lookup_candidates", lookup)
    _, issued = tokens(library_client)

    # When a signed-in client looks it up
    result = call_tool(
        library_client, issued["access_token"], "find_book", {"title": "Mistborn"}
    )

    # Then the candidate comes back for the person to choose
    assert not result["isError"], result
    assert asked and asked[0]["title"] == "Mistborn"
    (candidate,) = result["structuredContent"]["result"]
    assert candidate["google_books_id"] == "mist-1"


def test_a_library_no_sync_has_finished_says_to_run_one(
    google: FakeGoogle, store: MemoryAuthStore
) -> None:
    # Given Cosmos with nothing pushed to it
    empty = CosmosStore(FakeContainer(), FakeContainer())
    app = create_app(settings(), store=store, identity=google, library=empty)
    with TestClient(app, base_url=PUBLIC_URL) as client:
        _, issued = tokens(client)

        # When a client searches it
        result = call_tool(
            client, issued["access_token"], "search_library", {"query": "mistborn"}
        )

    # Then it is told why there is no answer and what fixes it
    assert result["isError"]
    assert "libris sync" in result["content"][0]["text"]


def test_with_no_cosmos_configured_a_search_says_what_is_missing(
    client: TestClient,
) -> None:
    # Given a hosted server started without LIBRIS_COSMOS_ENDPOINT
    _, issued = tokens(client)

    # When a client searches the Library
    result = call_tool(client, issued["access_token"], "search_library", {})

    # Then the tool names the setting, rather than failing as a server error
    assert result["isError"]
    assert "LIBRIS_COSMOS_ENDPOINT" in result["content"][0]["text"]
