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
import secrets
import time
from base64 import urlsafe_b64encode
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlencode, urlsplit

import anyio
import httpx
import jwt
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

CONSENT_PATH = "/oauth/consent"
GOOGLE_CALLBACK_PATH = "/oauth/google/callback"
CONSENT_COOKIE = "libris_consent"

GOOGLE_AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"  # noqa: S105 - a URL, not a secret
GOOGLE_ISSUERS = ("https://accounts.google.com", "accounts.google.com")

# Headers on every page a person sees during sign-in. A page that can be framed
# can be clicked through invisibly, which would undo the consent page's point.
_PAGE_HEADERS = {
    "Cache-Control": "no-store",
    "X-Frame-Options": "DENY",
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self' https://accounts.google.com; frame-ancestors 'none'",
    "Referrer-Policy": "no-referrer",
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
        async with httpx.AsyncClient(timeout=10) as client:
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
        if response.status_code != 200:
            raise SignInFailed(f"Google refused the code ({response.status_code})")
        id_token = response.json().get("id_token")
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


@dataclass(frozen=True)
class StoredRefreshToken:
    """A refresh token as kept: everything except the token itself."""

    client_id: str
    subject: str
    scopes: list[str]
    expires_at: int


class AuthStore(Protocol):
    """What has to survive a restart: registered clients and refresh tokens.

    Refresh tokens are keyed by a hash, so a copy of the store holds nothing a
    client could present.
    """

    def get_client(self, client_id: str) -> OAuthClientInformationFull | None: ...

    def save_client(self, client: OAuthClientInformationFull) -> None: ...

    def save_refresh_token(
        self, token_hash: str, record: StoredRefreshToken
    ) -> None: ...

    def get_refresh_token(self, token_hash: str) -> StoredRefreshToken | None: ...

    def take_refresh_token(self, token_hash: str) -> StoredRefreshToken | None:
        """Remove and return a refresh token, so only one caller can ever use it."""
        ...

    def delete_refresh_token(self, token_hash: str) -> None: ...


class MemoryAuthStore:
    """An `AuthStore` that forgets everything on restart: for tests and local runs."""

    def __init__(self) -> None:
        self.clients: dict[str, OAuthClientInformationFull] = {}
        self.refresh_tokens: dict[str, StoredRefreshToken] = {}

    def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        return self.clients.get(client_id)

    def save_client(self, client: OAuthClientInformationFull) -> None:
        self.clients[client.client_id or ""] = client

    def save_refresh_token(self, token_hash: str, record: StoredRefreshToken) -> None:
        self.refresh_tokens[token_hash] = record

    def get_refresh_token(self, token_hash: str) -> StoredRefreshToken | None:
        return self.refresh_tokens.get(token_hash)

    def take_refresh_token(self, token_hash: str) -> StoredRefreshToken | None:
        return self.refresh_tokens.pop(token_hash, None)

    def delete_refresh_token(self, token_hash: str) -> None:
        self.refresh_tokens.pop(token_hash, None)


@dataclass
class _SignIn:
    """An `/authorize` request waiting for consent and then for Google."""

    client_id: str
    client_name: str
    params: AuthorizationParams
    consent_token: str
    google_verifier: str
    expires_at: float
    sent_to_google: bool = False


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


class LibrisAuthProvider:
    """The decisions behind the SDK's OAuth endpoints (ADR 0035).

    Codes and sign-ins in progress live in memory, which is correct only while
    one replica serves every request. The Terraform pins the Container App to a
    single replica for that reason.
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
    ) -> None:
        if len(signing_key) < 32:
            raise ValueError("The token signing key must be at least 32 characters")
        self.public_url = public_url.rstrip("/")
        self.resource_url = f"{self.public_url}/mcp"
        self.google_redirect_uri = f"{self.public_url}{GOOGLE_CALLBACK_PATH}"
        self._signing_key = signing_key
        self._allowed_sub = allowed_sub or None
        self._store = store
        self._identity = identity
        self._clock = clock
        self._sign_ins: dict[str, _SignIn] = {}
        self._codes: dict[str, AuthorizationCode] = {}

    # Clients ------------------------------------------------------------

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        return await anyio.to_thread.run_sync(self._store.get_client, client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        await anyio.to_thread.run_sync(self._store.save_client, client_info)

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
        self._forget_expired()
        request_id = secrets.token_urlsafe(32)
        self._sign_ins[request_id] = _SignIn(
            client_id=client.client_id or "",
            client_name=client.client_name or "An unnamed application",
            params=params,
            consent_token=secrets.token_urlsafe(32),
            google_verifier=secrets.token_urlsafe(48),
            expires_at=self._clock() + SIGN_IN_SECONDS,
        )
        return f"{self.public_url}{CONSENT_PATH}?{urlencode({'request': request_id})}"

    async def consent_page(self, request: Request) -> Response:
        """Show who is asking and where they will be sent, before any sign-in."""
        request_id = request.query_params.get("request", "")
        sign_in = self._live_sign_in(request_id)
        if sign_in is None or sign_in.sent_to_google:
            return _page(
                "Sign-in expired",
                "<p>Start again from the app you were connecting.</p>",
                400,
            )

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
            path=CONSENT_PATH,
            secure=True,
            httponly=True,
            samesite="strict",
        )
        return response

    async def consent_decision(self, request: Request) -> Response:
        """Act on the consent form, but only from the browser that was shown it."""
        form = await request.form()
        request_id = str(form.get("request", ""))
        sign_in = self._live_sign_in(request_id)
        cookie = request.cookies.get(CONSENT_COOKIE, "")
        if sign_in is None or sign_in.sent_to_google:
            return _page(
                "Sign-in expired",
                "<p>Start again from the app you were connecting.</p>",
                400,
            )
        if not secrets.compare_digest(cookie, sign_in.consent_token):
            return _page(
                "Not allowed",
                "<p>This approval did not come from the consent page.</p>",
                403,
            )

        if form.get("decision") != "allow":
            del self._sign_ins[request_id]
            return RedirectResponse(
                self._back_to_client(sign_in, error="access_denied"), status_code=303
            )

        sign_in.sent_to_google = True
        return RedirectResponse(
            self._identity.authorization_url(
                state=request_id,
                code_challenge=_challenge(sign_in.google_verifier),
                redirect_uri=self.google_redirect_uri,
            ),
            status_code=303,
        )

    async def google_callback(self, request: Request) -> Response:
        """Finish a sign-in: issue a code only for the one allowed account."""
        request_id = request.query_params.get("state", "")
        sign_in = self._live_sign_in(request_id)
        if sign_in is None or not sign_in.sent_to_google:
            return _page(
                "Sign-in expired",
                "<p>Start again from the app you were connecting.</p>",
                400,
            )
        # One use only, whatever happens next.
        del self._sign_ins[request_id]

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
        self._codes[authorization_code] = AuthorizationCode(
            code=authorization_code,
            scopes=sign_in.params.scopes or [SCOPE],
            expires_at=self._clock() + CODE_SECONDS,
            client_id=sign_in.client_id,
            code_challenge=sign_in.params.code_challenge,
            redirect_uri=sign_in.params.redirect_uri,
            redirect_uri_provided_explicitly=sign_in.params.redirect_uri_provided_explicitly,
            resource=sign_in.params.resource,
            subject=account.sub,
        )
        return RedirectResponse(
            self._back_to_client(sign_in, code=authorization_code), status_code=303
        )

    # Tokens -------------------------------------------------------------

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        code = self._codes.get(authorization_code)
        if (
            code is None
            or code.client_id != client.client_id
            or code.expires_at <= self._clock()
        ):
            return None
        return code

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        if self._codes.pop(authorization_code.code, None) is None:
            raise TokenError("invalid_grant", "The authorization code was already used")
        return await self._issue(
            client.client_id or "",
            authorization_code.subject or "",
            authorization_code.scopes,
        )

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        record = await anyio.to_thread.run_sync(
            self._store.get_refresh_token, _hash(refresh_token)
        )
        if (
            record is None
            or record.client_id != client.client_id
            or record.expires_at <= self._clock()
        ):
            return None
        return RefreshToken(
            token=refresh_token,
            client_id=record.client_id,
            scopes=record.scopes,
            expires_at=record.expires_at,
            subject=record.subject,
        )

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        # Taking the token is the rotation: whoever takes it first gets the new
        # pair, and anyone presenting it afterwards gets nothing.
        record = await anyio.to_thread.run_sync(
            self._store.take_refresh_token, _hash(refresh_token.token)
        )
        if record is None:
            raise TokenError("invalid_grant", "The refresh token was already used")
        if not self._is_allowed(record.subject):
            raise TokenError(
                "invalid_grant", "This account can no longer use this Library"
            )
        return await self._issue(
            client.client_id or "", record.subject, scopes or record.scopes
        )

    async def load_access_token(self, token: str) -> AccessToken | None:
        try:
            claims = jwt.decode(
                token,
                self._signing_key,
                algorithms=["HS256"],
                audience=self.resource_url,
                issuer=self.public_url,
                options={"require": ["exp", "sub", "client_id"]},
            )
        except jwt.InvalidTokenError:
            return None
        # Checked on every call, not only at sign-in, so changing the allowed
        # account cuts off the old one within the hour rather than never.
        if not self._is_allowed(claims["sub"]):
            return None
        return AccessToken(
            token=token,
            client_id=claims["client_id"],
            scopes=str(claims.get("scope", "")).split(),
            expires_at=int(claims["exp"]),
            resource=self.resource_url,
            subject=claims["sub"],
        )

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        # Access tokens are stateless and expire within the hour; a refresh
        # token is what keeps a client signed in, so that is what goes.
        if isinstance(token, RefreshToken):
            await anyio.to_thread.run_sync(
                self._store.delete_refresh_token, _hash(token.token)
            )

    # Internals ----------------------------------------------------------

    async def _issue(
        self, client_id: str, subject: str, scopes: list[str]
    ) -> OAuthToken:
        now = int(self._clock())
        access_token = jwt.encode(
            {
                "iss": self.public_url,
                "aud": self.resource_url,
                "sub": subject,
                "client_id": client_id,
                "scope": " ".join(scopes),
                "iat": now,
                "exp": now + ACCESS_TOKEN_SECONDS,
                "jti": secrets.token_urlsafe(16),
            },
            self._signing_key,
            algorithm="HS256",
        )
        refresh_token = secrets.token_urlsafe(32)
        record = StoredRefreshToken(
            client_id=client_id,
            subject=subject,
            scopes=scopes,
            expires_at=now + REFRESH_TOKEN_SECONDS,
        )
        await anyio.to_thread.run_sync(
            self._store.save_refresh_token, _hash(refresh_token), record
        )
        return OAuthToken(
            access_token=access_token,
            token_type="Bearer",  # noqa: S106 - the OAuth token type, not a secret
            expires_in=ACCESS_TOKEN_SECONDS,
            scope=" ".join(scopes),
            refresh_token=refresh_token,
        )

    def _is_allowed(self, sub: str) -> bool:
        return self._allowed_sub is not None and secrets.compare_digest(
            sub, self._allowed_sub
        )

    def _live_sign_in(self, request_id: str) -> _SignIn | None:
        sign_in = self._sign_ins.get(request_id)
        if sign_in is None or sign_in.expires_at <= self._clock():
            return None
        return sign_in

    def _back_to_client(self, sign_in: _SignIn, **params: str) -> str:
        return construct_redirect_uri(
            str(sign_in.params.redirect_uri), state=sign_in.params.state, **params
        )

    def _forget_expired(self) -> None:
        now = self._clock()
        for key in [k for k, v in self._sign_ins.items() if v.expires_at <= now]:
            del self._sign_ins[key]
        for key in [k for k, v in self._codes.items() if v.expires_at <= now]:
            del self._codes[key]
