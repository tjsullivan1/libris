"""Libris as its own OAuth authorization server, with Google saying who you are.

ADR 0035. The MCP SDK serves every OAuth endpoint a client talks to - discovery,
`/register`, `/authorize`, `/token` - and calls the provider here for the parts
only Libris can decide: which clients exist, who signed in, and what a token
means. Nothing in this module parses an OAuth request.

The flow, from a client's side:

1. `/authorize` lands in `LibrisAuthProvider.authorize`, which records the
   request and sends the browser to Libris's own consent page.
2. The consent page names the client and where it will be sent back to. Its
   form only works from a browser holding the cookie the page set, so another
   site cannot press the button for you.
3. Approval sends the browser to Google. Google's answer comes back to the
   callback, and a code is issued only for the one allowed Google account.
4. The client trades the code at `/token` for a signed access token and a
   refresh token that rotates on every use.
"""

from __future__ import annotations

import hashlib
import html
import json
import secrets
import threading
import time
from base64 import b64decode, b64encode, urlsafe_b64encode
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import urlencode, urlsplit

import anyio
import httpx
import jwt
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

SCOPE = "libris"
ACCESS_TOKEN_SECONDS = 60 * 60
REFRESH_TOKEN_SECONDS = 30 * 24 * 60 * 60
CODE_SECONDS = 5 * 60
SIGN_IN_SECONDS = 10 * 60
# A client unused for this long is forgotten. Registration is open (ADR 0035),
# so clients nobody uses must go away by themselves.
CLIENT_IDLE_SECONDS = 60 * 24 * 60 * 60
# Each sign-in writes a document, and any registered client can start one.
# Counted per client, after the SDK has validated the request, so neither
# malformed requests nor another client's flood can use up the allowance of
# the client a person is actually connecting.
SIGN_INS_PER_CLIENT_PER_HOUR = 10

CONSENT_PATH = "/oauth/consent"
GOOGLE_CALLBACK_PATH = "/oauth/google/callback"
# Both cookies carry the __Host- prefix, which makes a browser refuse any such
# cookie that this exact host did not set itself: no Domain attribute, Path=/,
# Secure. Without it they were not safe here. The app lives under
# azurecontainerapps.io, which is not on the Public Suffix List, so every other
# Container App in the region is the same site. A sibling could set a cookie for
# the shared parent domain and the browser would send it to Libris ("cookie
# tossing"). That let an attacker plant their own approval cookie in the
# owner's browser.
CONSENT_COOKIE = "__Host-libris_consent"
# Set when consent is given, and required back at Google's callback, so the
# browser that finishes a sign-in is the one that approved it.
APPROVAL_COOKIE = "__Host-libris_approved"

GOOGLE_AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"  # noqa: S105 - a URL, not a secret
GOOGLE_ISSUERS = ("https://accounts.google.com", "accounts.google.com")

# Headers on every page a person sees during sign-in. A page that can be framed
# can be clicked through invisibly, which would undo the consent page's point.
_PAGE_HEADERS = {
    "Cache-Control": "no-store",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self' https://accounts.google.com; frame-ancestors 'none'",
    # same-origin, not no-referrer. Under no-referrer a browser sends
    # `Origin: null` on every form POST, its own origin included (Fetch,
    # "append a request Origin header"), so the consent form's exact Origin
    # check refused the owner. That shipped once. same-origin keeps the
    # Origin on the consent POST and still sends no Referer to Google.
    "Referrer-Policy": "same-origin",
}


class SignInFailed(Exception):
    """Google did not vouch for anyone, so there is no account to check."""


@dataclass(frozen=True)
class GoogleAccount:
    """Who Google says signed in."""

    sub: str
    email: str | None
    email_verified: bool


class IdentityProvider(Protocol):
    """The upstream that proves who is at the keyboard."""

    def authorization_url(
        self, *, state: str, code_challenge: str, redirect_uri: str
    ) -> str:
        """Where to send the browser to sign in."""
        ...

    async def account(
        self, *, code: str, code_verifier: str, redirect_uri: str
    ) -> GoogleAccount:
        """Trade the code the browser brought back for the account it proves."""
        ...


@dataclass(frozen=True)
class GoogleIdentity:
    """Google as the identity provider, through one OAuth client Libris owns."""

    client_id: str
    client_secret: str
    # Only tests set this, to stand in for Google's token endpoint.
    transport: httpx.AsyncBaseTransport | None = field(default=None, compare=False)

    def authorization_url(
        self, *, state: str, code_challenge: str, redirect_uri: str
    ) -> str:
        """Google's sign-in page, asking only for the account's identity."""
        query = urlencode(
            {
                "client_id": self.client_id,
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "scope": "openid email",
                "state": state,
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
                # Always show the account chooser, so a sign-in is a choice a
                # person made rather than whichever Google session was open.
                "prompt": "select_account",
            }
        )
        return f"{GOOGLE_AUTHORIZE_URL}?{query}"

    async def account(
        self, *, code: str, code_verifier: str, redirect_uri: str
    ) -> GoogleAccount:
        """Trade the code at Google's token endpoint and read the ID token.

        The ID token arrives directly from Google over TLS in exchange for
        Libris's client secret, so its signature is not checked again: OIDC
        Core 3.1.3.7 allows exactly that. Issuer, audience and expiry are.
        """
        # Every way the exchange can fail becomes SignInFailed, which the
        # callback turns into a page. Anything else escaped as a 500 after the
        # sign-in had already been used up: a DNS failure, a timeout, or a
        # proxy answering 200 with HTML.
        try:
            async with httpx.AsyncClient(
                timeout=10, transport=self.transport
            ) as client:
                response = await client.post(
                    GOOGLE_TOKEN_URL,
                    data={
                        "code": code,
                        "client_id": self.client_id,
                        "client_secret": self.client_secret,
                        "redirect_uri": redirect_uri,
                        "grant_type": "authorization_code",
                        "code_verifier": code_verifier,
                    },
                )
        except httpx.HTTPError as exc:
            raise SignInFailed("Google could not be reached. Try again.") from exc
        if response.status_code != 200:
            raise SignInFailed(f"Google refused the code ({response.status_code})")
        try:
            id_token = response.json().get("id_token")
        except (ValueError, AttributeError) as exc:
            raise SignInFailed("Google's answer could not be read") from exc
        if not id_token:
            raise SignInFailed("Google answered without an ID token")
        return account_from_id_token(id_token, client_id=self.client_id)


def account_from_id_token(
    id_token: str, *, client_id: str, now: float | None = None
) -> GoogleAccount:
    """Read the account out of an ID token received directly from Google.

    Raises:
        SignInFailed: The token is not one Google issued to this client, or it
            has expired.
    """
    try:
        claims = jwt.decode(id_token, options={"verify_signature": False})
    except jwt.InvalidTokenError as exc:
        raise SignInFailed("Google's ID token could not be read") from exc
    if claims.get("iss") not in GOOGLE_ISSUERS:
        raise SignInFailed("The ID token was not issued by Google")
    if claims.get("aud") != client_id:
        raise SignInFailed("The ID token was issued to another client")
    if float(claims.get("exp", 0)) <= (time.time() if now is None else now):
        raise SignInFailed("The ID token has expired")
    if not claims.get("sub"):
        raise SignInFailed("The ID token names no account")
    return GoogleAccount(
        sub=str(claims["sub"]),
        email=claims.get("email"),
        email_verified=claims.get("email_verified") is True,
    )


class AuthStore(Protocol):
    """Everything the authorization server must not forget, as expiring records.

    Clients, sign-ins in progress, authorization codes, refresh tokens and
    grants are all kept here rather than in memory. The Container App scales to
    zero, and a person can be on Google's page while it does: nothing reaches
    Libris then, so a sign-in held in memory would be gone when Google sends
    them back.

    Each record has a kind, a key and an expiry. Records are removed once they
    expire, but readers check `expires_at` themselves because removal can lag.
    Anything a client could present (a code, a refresh token) is keyed by its
    hash, so a copy of the store holds nothing that works.

    `take` and `transition` are the two operations that must be atomic, because
    concurrent requests decide things by them: who redeemed a code, whether a
    refresh token was used twice, whether a grant survived a revocation.
    """

    def put(self, kind: str, key: str, data: dict, expires_at: int) -> None:
        """Create or replace a record."""
        ...

    def get(self, kind: str, key: str) -> dict | None:
        """Read a record, or None if there is none."""
        ...

    def take(self, kind: str, key: str) -> dict | None:
        """Remove and return a record, so that only one caller ever gets it."""
        ...

    def transition(
        self,
        kind: str,
        key: str,
        change: Callable[[dict], dict],
        expires_at: int,
    ) -> dict | None:
        """Replace a record with `change(record)`, if nothing changed it meanwhile.

        Returns the record as it was before, or None if there was none or
        another writer got to it between the read and the write. A record that
        is gone stays gone: this never creates one.
        """
        ...

    def delete(self, kind: str, key: str) -> None:
        """Remove a record if it exists."""
        ...


class MemoryAuthStore:
    """An `AuthStore` that forgets everything on restart: for tests and local runs.

    Records go through JSON on the way in and out, as they do in Cosmos, so a
    value that only survives in memory fails here first. Each carries a version,
    as a Cosmos etag does, so `transition` loses a race the way Cosmos would.
    `between_read_and_write` lets a test hold a transition at that point while
    another request runs.
    """

    def __init__(self) -> None:
        self.records: dict[tuple[str, str], tuple[str, int]] = {}
        self._versions = 0
        self._lock = threading.Lock()
        self.between_read_and_write: Callable[[str, str], None] = lambda kind, key: None

    def _write(self, kind: str, key: str, data: dict) -> None:
        self._versions += 1
        self.records[(kind, key)] = (json.dumps(data), self._versions)

    def put(self, kind: str, key: str, data: dict, expires_at: int) -> None:
        with self._lock:
            self._write(kind, key, data)

    def get(self, kind: str, key: str) -> dict | None:
        held = self.records.get((kind, key))
        return None if held is None else json.loads(held[0])

    def take(self, kind: str, key: str) -> dict | None:
        with self._lock:
            held = self.records.pop((kind, key), None)
        return None if held is None else json.loads(held[0])

    def transition(
        self,
        kind: str,
        key: str,
        change: Callable[[dict], dict],
        expires_at: int,
    ) -> dict | None:
        held = self.records.get((kind, key))
        if held is None:
            return None
        before = json.loads(held[0])
        after = change(before)
        self.between_read_and_write(kind, key)
        with self._lock:
            if self.records.get((kind, key)) != held:
                return None
            self._write(kind, key, after)
        return before

    def delete(self, kind: str, key: str) -> None:
        with self._lock:
            self.records.pop((kind, key), None)


@dataclass
class _SignIn:
    """An `/authorize` request waiting for consent and then for Google."""

    client_id: str
    client_name: str
    params: AuthorizationParams
    consent_token: str
    google_verifier: str
    expires_at: int
    sent_to_google: bool = False

    def record(self) -> dict:
        return {
            "client_id": self.client_id,
            "client_name": self.client_name,
            "params": self.params.model_dump(mode="json"),
            "consent_token": self.consent_token,
            "google_verifier": self.google_verifier,
            "expires_at": self.expires_at,
            "sent_to_google": self.sent_to_google,
        }

    @classmethod
    def of(cls, record: dict) -> "_SignIn":
        return cls(
            **{**record, "params": AuthorizationParams.model_validate(record["params"])}
        )


class _Refresh(RefreshToken):
    """A refresh token as loaded, carrying the grant it belongs to.

    The SDK hands back whatever `load_refresh_token` returned, so the grant id
    travels with the token into the exchange and the revocation.
    """

    grant: str


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return urlsafe_b64encode(digest).decode().rstrip("=")


def _page(title: str, body: str, status_code: int = 200) -> HTMLResponse:
    document = (
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        '<meta name=viewport content="width=device-width, initial-scale=1">'
        f"<title>{html.escape(title)}</title>"
        "<style>body{font:16px/1.5 system-ui,sans-serif;max-width:32rem;margin:3rem auto;padding:0 1rem}"
        "button{font:inherit;padding:.5rem 1rem;margin-right:.5rem}code{word-break:break-all}</style>"
        f"</head><body><h1>{html.escape(title)}</h1>{body}</body></html>"
    )
    return HTMLResponse(document, status_code=status_code, headers=_PAGE_HEADERS)


_EXPIRED = (
    "Sign-in expired",
    "<p>Start again from the app you were connecting.</p>",
    400,
)


class LibrisAuthProvider:
    """The decisions behind the SDK's OAuth endpoints (ADR 0035).

    Holds no state of its own: everything lives in the `AuthStore`, so any
    instance can finish a sign-in another one started.
    """

    def __init__(
        self,
        *,
        public_url: str,
        signing_key: str,
        allowed_sub: str | None,
        store: AuthStore,
        identity: IdentityProvider,
        clock: Callable[[], float] = time.time,
        sign_ins_per_client_per_hour: int = SIGN_INS_PER_CLIENT_PER_HOUR,
        limit_clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if len(signing_key) < 32:
            raise ValueError("The token signing key must be at least 32 characters")
        self.public_url = public_url.rstrip("/")
        self.resource_url = f"{self.public_url}/mcp"
        self.google_redirect_uri = f"{self.public_url}{GOOGLE_CALLBACK_PATH}"
        parts = urlsplit(self.public_url)
        self._origin = f"{parts.scheme}://{parts.netloc}"
        self._signing_key = signing_key
        self._secret_box = AESGCM(
            hashlib.sha256(b"libris client secrets:" + signing_key.encode()).digest()
        )
        self._allowed_sub = allowed_sub or None
        self._store = store
        self._identity = identity
        self._clock = clock
        self._sign_ins_per_client = sign_ins_per_client_per_hour
        self._limit_clock = limit_clock
        self._recent_sign_ins: dict[str, deque[float]] = {}

    # Clients ------------------------------------------------------------

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        record = await self._get("client", client_id)
        if record is None:
            return None
        client = OAuthClientInformationFull.model_validate(record["client"])
        sealed = record.get("sealed_secret")
        if sealed:
            try:
                secret = self._secret_box.decrypt(
                    b64decode(sealed["nonce"]),
                    b64decode(sealed["ciphertext"]),
                    client_id.encode(),
                )
            except InvalidTag:
                # Sealed under another signing key, or tampered with. Either
                # way it cannot authenticate, so the client has to register
                # again, as it would after any other loss of its record.
                return None
            client = client.model_copy(update={"client_secret": secret.decode()})
        return client

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        await self._keep_client(client_info)

    async def _keep_client(self, client: OAuthClientInformationFull) -> None:
        # Registration is open, so a client nobody uses has to go away by
        # itself. Each token issued to a client renews this, so one in use
        # never expires.
        expires_at = self._now() + CLIENT_IDLE_SECONDS
        record: dict = {
            "client": client.model_dump(mode="json", exclude={"client_secret"}),
            "expires_at": expires_at,
        }
        if client.client_secret:
            # A client secret is a live credential, and the SDK authenticates a
            # client by comparing it in plain text, so it cannot be kept as a
            # hash the way codes and refresh tokens are. It is sealed instead,
            # under a key derived from the signing key, which lives in Key
            # Vault and not here. A copy of the store alone then holds no
            # secret. The client id is the associated data, so a sealed secret
            # moved onto another client's record will not open.
            nonce = secrets.token_bytes(12)
            record["sealed_secret"] = {
                "nonce": b64encode(nonce).decode(),
                "ciphertext": b64encode(
                    self._secret_box.encrypt(
                        nonce,
                        client.client_secret.encode(),
                        (client.client_id or "").encode(),
                    )
                ).decode(),
            }
        await self._put("client", client.client_id or "", record, expires_at)

    # Authorization ------------------------------------------------------

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        if (
            params.resource is not None
            and params.resource.rstrip("/") != self.resource_url
        ):
            raise AuthorizeError(
                "invalid_target", "Libris issues tokens for its own MCP endpoint only"
            )
        self._count_sign_in(client.client_id or "")
        request_id = secrets.token_urlsafe(32)
        sign_in = _SignIn(
            client_id=client.client_id or "",
            client_name=client.client_name or "An unnamed application",
            params=params,
            consent_token=secrets.token_urlsafe(32),
            google_verifier=secrets.token_urlsafe(48),
            expires_at=self._now() + SIGN_IN_SECONDS,
        )
        await self._put("sign_in", request_id, sign_in.record(), sign_in.expires_at)
        return f"{self.public_url}{CONSENT_PATH}?{urlencode({'request': request_id})}"

    async def consent_page(self, request: Request) -> Response:
        """Show who is asking and where they will be sent, before any sign-in."""
        request_id = request.query_params.get("request", "")
        sign_in = await self._sign_in(request_id)
        if sign_in is None or sign_in.sent_to_google:
            return _page(*_EXPIRED)

        host = urlsplit(str(sign_in.params.redirect_uri)).hostname or "an unknown host"
        body = (
            f"<p><strong>{html.escape(sign_in.client_name)}</strong> wants to use your Library.</p>"
            f"<p>After you sign in, you will be sent back to <strong>{html.escape(host)}</strong>. "
            "If you did not just ask to connect an app, deny this.</p>"
            f'<form method=post action="{CONSENT_PATH}">'
            f'<input type=hidden name=request value="{html.escape(request_id)}">'
            "<button name=decision value=allow>Continue with Google</button>"
            "<button name=decision value=deny>Deny</button></form>"
        )
        response = _page("Connect to Libris", body)
        response.set_cookie(
            CONSENT_COOKIE,
            sign_in.consent_token,
            max_age=SIGN_IN_SECONDS,
            path="/",
            secure=True,
            httponly=True,
            samesite="strict",
        )
        return response

    async def consent_decision(self, request: Request) -> Response:
        """Act on the consent form, but only from the browser that was shown it."""
        # The form must come from Libris's own page. SameSite=Strict on the
        # consent cookie stops another site's form, but a sibling Container App
        # is the same site, so its form would carry the cookie. Browsers always
        # send Origin on a form POST, so a missing one is refused too.
        if request.headers.get("origin") != self._origin:
            return _page(
                "Not allowed",
                "<p>This approval did not come from the consent page.</p>",
                403,
            )
        form = await request.form()
        request_id = str(form.get("request", ""))
        sign_in = await self._sign_in(request_id)
        cookie = request.cookies.get(CONSENT_COOKIE, "")
        if sign_in is None or sign_in.sent_to_google:
            return _page(*_EXPIRED)
        if not secrets.compare_digest(cookie, sign_in.consent_token):
            return _page(
                "Not allowed",
                "<p>This approval did not come from the consent page.</p>",
                403,
            )

        if form.get("decision") != "allow":
            await self._delete("sign_in", request_id)
            return RedirectResponse(
                self._back_to_client(sign_in, error="access_denied"), status_code=303
            )

        sign_in.sent_to_google = True
        await self._put("sign_in", request_id, sign_in.record(), sign_in.expires_at)
        response = RedirectResponse(
            self._identity.authorization_url(
                state=request_id,
                code_challenge=_challenge(sign_in.google_verifier),
                redirect_uri=self.google_redirect_uri,
            ),
            status_code=303,
        )
        # The Google link this redirects to can be copied, and `state` alone
        # would let anyone's browser finish the sign-in. Someone could approve
        # their own client's consent page and send the link to the owner, who
        # signs in and hands a code to that client. This cookie stays with the
        # browser that approved, and the callback requires it. Lax rather than
        # Strict, because Google's redirect back is a cross-site navigation and
        # a Strict cookie would never arrive.
        response.set_cookie(
            APPROVAL_COOKIE,
            sign_in.consent_token,
            max_age=SIGN_IN_SECONDS,
            path="/",
            secure=True,
            httponly=True,
            samesite="lax",
        )
        return response

    async def google_callback(self, request: Request) -> Response:
        """Finish a sign-in: issue a code only for the one allowed account."""
        # Taken, not read: one use only, whatever happens next, and a second
        # callback racing this one finds nothing.
        record = await self._take("sign_in", request.query_params.get("state", ""))
        sign_in = None if record is None else _SignIn.of(record)
        if (
            sign_in is None
            or not sign_in.sent_to_google
            or sign_in.expires_at <= self._clock()
        ):
            return _page(*_EXPIRED)
        # Checked before Google is asked anything, and after the sign-in was
        # taken, so a refused attempt cannot be retried from another browser.
        approved = request.cookies.get(APPROVAL_COOKIE, "")
        if not secrets.compare_digest(approved, sign_in.consent_token):
            return _page(
                "Not allowed",
                "<p>This sign-in was approved in a different browser. "
                "Start again from the app you were connecting.</p>",
                403,
            )

        code = request.query_params.get("code")
        if not code:
            return RedirectResponse(
                self._back_to_client(sign_in, error="access_denied"), status_code=303
            )
        try:
            account = await self._identity.account(
                code=code,
                code_verifier=sign_in.google_verifier,
                redirect_uri=self.google_redirect_uri,
            )
        except SignInFailed as exc:
            return _page("Sign-in failed", f"<p>{html.escape(str(exc))}</p>", 502)

        if not self._is_allowed(account.sub) or not account.email_verified:
            return _page(
                "This account cannot use this Library",
                f"<p>Google signed in <strong>{html.escape(account.email or 'an account')}</strong>, "
                "which is not the account this Library belongs to.</p>"
                "<p>Setting this Library up for the first time? This account's Google ID is:</p>"
                f"<p><code>{html.escape(account.sub)}</code></p>",
                403,
            )

        authorization_code = secrets.token_urlsafe(32)
        expires_at = self._now() + CODE_SECONDS
        stored = AuthorizationCode(
            code="",  # kept only as its hash, the key
            scopes=sign_in.params.scopes or [SCOPE],
            expires_at=expires_at,
            client_id=sign_in.client_id,
            code_challenge=sign_in.params.code_challenge,
            redirect_uri=sign_in.params.redirect_uri,
            redirect_uri_provided_explicitly=sign_in.params.redirect_uri_provided_explicitly,
            resource=sign_in.params.resource,
            subject=account.sub,
        )
        await self._put(
            "code",
            _hash(authorization_code),
            stored.model_dump(mode="json"),
            expires_at,
        )
        return RedirectResponse(
            self._back_to_client(sign_in, code=authorization_code), status_code=303
        )

    # Tokens -------------------------------------------------------------

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        record = await self._get("code", _hash(authorization_code))
        if record is None:
            return None
        code = AuthorizationCode.model_validate({**record, "code": authorization_code})
        if code.client_id != client.client_id or code.expires_at <= self._clock():
            return None
        return code

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        if await self._take("code", _hash(authorization_code.code)) is None:
            raise TokenError("invalid_grant", "The authorization code was already used")
        subject = authorization_code.subject or ""
        grant, expires_at = await self._new_grant(client, subject)
        return await self._issue(
            client, subject, authorization_code.scopes, grant, expires_at
        )

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> "_Refresh | None":
        record = await self._get("refresh", _hash(refresh_token))
        if (
            record is None
            or record["client_id"] != client.client_id
            or await self._get("grant", record.get("grant", "")) is None
        ):
            return None
        return _Refresh(
            token=refresh_token,
            client_id=record["client_id"],
            scopes=record["scopes"],
            expires_at=record["expires_at"],
            subject=record["subject"],
            grant=record["grant"],
        )

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: "_Refresh",
        scopes: list[str],
    ) -> OAuthToken:
        # Spending the token is one atomic step: it is marked spent only if
        # nothing changed it since it was read. The marker stays for the rest
        # of the token's life, so a later replay is recognised rather than
        # looking like a token that never existed.
        before = await self._transition(
            "refresh",
            _hash(refresh_token.token),
            lambda record: {**record, "spent": True},
            int(refresh_token.expires_at or 0),
        )
        if before is None or before.get("spent"):
            # Spent already, or spent by another request in the same instant.
            # Either way two parties presented one token, one of them is not
            # the client it was issued to, and there is no telling which, so
            # the whole sign-in ends (RFC 9700 4.14.2). Refusing only this
            # request would leave whoever won holding a live successor.
            await self._delete("grant", refresh_token.grant)
            raise TokenError("invalid_grant", "This refresh token was reused")
        if not self._is_allowed(before["subject"]):
            raise TokenError(
                "invalid_grant", "This account can no longer use this Library"
            )
        # Extend the grant only if it still exists. Writing it outright would
        # bring back a grant that a revocation deleted after the checks above,
        # and every token the revocation was meant to end along with it.
        expires_at = self._now() + REFRESH_TOKEN_SECONDS
        extended = await self._transition(
            "grant",
            refresh_token.grant,
            lambda grant: {**grant, "expires_at": expires_at},
            expires_at,
        )
        if extended is None:
            raise TokenError("invalid_grant", "This sign-in was revoked")
        return await self._issue(
            client,
            before["subject"],
            scopes or before["scopes"],
            refresh_token.grant,
            expires_at,
        )

    async def load_access_token(self, token: str) -> AccessToken | None:
        try:
            claims = jwt.decode(
                token,
                self._signing_key,
                algorithms=["HS256"],
                audience=self.resource_url,
                issuer=self.public_url,
                options={"require": ["exp", "sub", "client_id", "grant"]},
            )
        except jwt.InvalidTokenError:
            return None
        # Checked on every call, not only at sign-in, so changing the allowed
        # account cuts off the old one within the hour rather than never.
        if not self._is_allowed(claims["sub"]):
            return None
        # And the grant must still exist, so revoking either token ends this
        # one at once rather than when it expires. One point read per call.
        if await self._get("grant", claims["grant"]) is None:
            return None
        return AccessToken(
            token=token,
            client_id=claims["client_id"],
            scopes=str(claims.get("scope", "")).split(),
            expires_at=int(claims["exp"]),
            resource=self.resource_url,
            subject=claims["sub"],
            claims={"grant": claims["grant"]},
        )

    async def revoke_token(self, token: "AccessToken | _Refresh") -> None:
        # Either token revokes the whole sign-in: deleting the grant ends the
        # access token on its next call, and every refresh token issued under
        # it, however many rotations later.
        if isinstance(token, _Refresh):
            await self._delete("refresh", _hash(token.token))
            grant = token.grant
        else:
            grant = (token.claims or {}).get("grant")
        if grant:
            await self._delete("grant", grant)

    # Internals ----------------------------------------------------------

    async def _new_grant(
        self, client: OAuthClientInformationFull, subject: str
    ) -> tuple[str, int]:
        """Start a grant: one sign-in, which every token issued from it shares.

        Through any number of refreshes, the tokens carry its id, so deleting
        it revokes all of them. Only a new sign-in creates one; a refresh can
        only extend a grant that still exists.
        """
        grant = secrets.token_urlsafe(16)
        expires_at = self._now() + REFRESH_TOKEN_SECONDS
        await self._put(
            "grant",
            grant,
            {
                "client_id": client.client_id or "",
                "subject": subject,
                "expires_at": expires_at,
            },
            expires_at,
        )
        return grant, expires_at

    async def _issue(
        self,
        client: OAuthClientInformationFull,
        subject: str,
        scopes: list[str],
        grant: str,
        expires_at: int,
    ) -> OAuthToken:
        """Issue an access and refresh token under a grant that already exists.

        Never writes the grant. A revocation that lands while this runs leaves
        these tokens pointing at a grant that is gone, so they are refused.
        """
        now = self._now()
        client_id = client.client_id or ""
        access_token = jwt.encode(
            {
                "iss": self.public_url,
                "aud": self.resource_url,
                "sub": subject,
                "client_id": client_id,
                "scope": " ".join(scopes),
                "grant": grant,
                "iat": now,
                "exp": now + ACCESS_TOKEN_SECONDS,
                "jti": secrets.token_urlsafe(16),
            },
            self._signing_key,
            algorithm="HS256",
        )
        refresh_token = secrets.token_urlsafe(32)
        await self._put(
            "refresh",
            _hash(refresh_token),
            {
                "client_id": client_id,
                "subject": subject,
                "scopes": scopes,
                "grant": grant,
                "expires_at": expires_at,
            },
            expires_at,
        )
        await self._keep_client(client)
        return OAuthToken(
            access_token=access_token,
            token_type="Bearer",  # noqa: S106 - the OAuth token type, not a secret
            expires_in=ACCESS_TOKEN_SECONDS,
            scope=" ".join(scopes),
            refresh_token=refresh_token,
        )

    async def _sign_in(self, request_id: str) -> _SignIn | None:
        record = await self._get("sign_in", request_id)
        if record is None:
            return None
        sign_in = _SignIn.of(record)
        return None if sign_in.expires_at <= self._clock() else sign_in

    def _count_sign_in(self, client_id: str) -> None:
        """Refuse a sign-in beyond this client's hourly allowance.

        In memory, so a restart forgets the count. That is fine for a limit
        whose job is to stop a flood. A refusal reaches the client as OAuth's
        own temporarily_unavailable, on its redirect URI.
        """
        recent = self._recent_sign_ins.setdefault(client_id, deque())
        now = self._limit_clock()
        while recent and recent[0] <= now - 3600:
            recent.popleft()
        if len(recent) >= self._sign_ins_per_client:
            raise AuthorizeError(
                "temporarily_unavailable",
                "Too many sign-ins for this application. Try again later.",
            )
        recent.append(now)

    def _is_allowed(self, sub: str) -> bool:
        return self._allowed_sub is not None and secrets.compare_digest(
            sub, self._allowed_sub
        )

    def _back_to_client(self, sign_in: _SignIn, **params: str) -> str:
        return construct_redirect_uri(
            str(sign_in.params.redirect_uri), state=sign_in.params.state, **params
        )

    def _now(self) -> int:
        return int(self._clock())

    # The store is synchronous (the Cosmos client is), so its calls run in a
    # worker thread rather than blocking the event loop.

    async def _put(self, kind: str, key: str, data: dict, expires_at: int) -> None:
        await anyio.to_thread.run_sync(self._store.put, kind, key, data, expires_at)

    async def _get(self, kind: str, key: str) -> dict | None:
        record = await anyio.to_thread.run_sync(self._store.get, kind, key)
        if record is None or record.get("expires_at", 0) <= self._clock():
            return None
        return record

    async def _take(self, kind: str, key: str) -> dict | None:
        return await anyio.to_thread.run_sync(self._store.take, kind, key)

    async def _transition(
        self, kind: str, key: str, change: Callable[[dict], dict], expires_at: int
    ) -> dict | None:
        return await anyio.to_thread.run_sync(
            self._store.transition, kind, key, change, expires_at
        )

    async def _delete(self, kind: str, key: str) -> None:
        await anyio.to_thread.run_sync(self._store.delete, kind, key)
