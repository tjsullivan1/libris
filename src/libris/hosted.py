"""The hosted MCP server: Streamable HTTP behind Libris's own OAuth (ADR 0035).

This is the auth spike for #171. Its one tool, `ping`, proves a client got all
the way through sign-in. The Library's real tools arrive with #175, once the
remote store exists to answer them.

Configured from environment variables rather than `config.yaml`, because it
runs in a container whose settings come from the Container App (and its secrets
from Key Vault), not from a person's config directory.

Run it the way the container does:

    uvicorn --factory libris.hosted:create_app_from_env --host 0.0.0.0 --port 8000
"""

import os
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

from mcp.server import MCPServer
from mcp.server.auth.settings import (
    AuthSettings,
    ClientRegistrationOptions,
    RevocationOptions,
)
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from . import installed_version
from .oauth import (
    CONSENT_PATH,
    GOOGLE_CALLBACK_PATH,
    SCOPE,
    AuthStore,
    GoogleIdentity,
    IdentityProvider,
    LibrisAuthProvider,
    MemoryAuthStore,
)

# The two public endpoints that create a document for any caller (ADR 0035):
# /register writes a client, and /authorize writes a sign-in for any registered
# one. Everything else either changes a record that already exists or needs a
# code only the allowed account can get. Claude registers and authorizes once
# per connection, so these are far above real use and far below any cost.
REGISTRATIONS_PER_HOUR = 30
SIGN_INS_PER_HOUR = 60


class WriteLimit:
    """Refuse calls to the public writing endpoints beyond a number per hour.

    Each limited path counts separately, before the request reaches the SDK or
    the store. In memory, so a restart forgets the counts; that is fine for a
    limit whose job is to stop a flood, not to account for every request.
    Answers 429 rather than letting the SDK report a refusal as a bad request.
    """

    def __init__(
        self, app: ASGIApp, *, per_hour: dict[str, int], clock: Callable[[], float]
    ) -> None:
        self._app = app
        self._per_hour = per_hour
        self._clock = clock
        self._recent: dict[str, deque[float]] = {path: deque() for path in per_hour}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        if scope["type"] == "http" and path in self._per_hour:
            recent = self._recent[path]
            now = self._clock()
            while recent and recent[0] <= now - 3600:
                recent.popleft()
            if len(recent) >= self._per_hour[path]:
                retry_after = int(recent[0] + 3600 - now) + 1
                response = JSONResponse(
                    {
                        "error": "temporarily_unavailable",
                        "error_description": "Too many requests. Try again later.",
                    },
                    status_code=429,
                    headers={"Retry-After": str(retry_after)},
                )
                await response(scope, receive, send)
                return
            recent.append(now)
        await self._app(scope, receive, send)


class HostedConfigError(Exception):
    """A setting the hosted server cannot start without is missing."""


@dataclass(frozen=True)
class HostedSettings:
    """Everything the hosted server is told by its environment."""

    public_url: str
    google_client_id: str
    google_client_secret: str
    signing_key: str
    allowed_google_sub: str | None
    cosmos_endpoint: str | None = None
    cosmos_database: str = "libris"

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> "HostedSettings":
        """Read the settings, naming every required one that is missing.

        `LIBRIS_ALLOWED_GOOGLE_SUB` may be unset on a first deployment: every
        sign-in is then refused, and the refusal shows the Google ID to set.

        Raises:
            HostedConfigError: A required variable is unset.
        """
        env = os.environ if environ is None else environ
        required = {
            "LIBRIS_PUBLIC_URL": "public_url",
            "LIBRIS_GOOGLE_CLIENT_ID": "google_client_id",
            "LIBRIS_GOOGLE_CLIENT_SECRET": "google_client_secret",
            "LIBRIS_TOKEN_SIGNING_KEY": "signing_key",
        }
        missing = [name for name in required if not env.get(name)]
        if missing:
            raise HostedConfigError(
                f"Missing environment variables: {', '.join(missing)}"
            )
        return cls(
            **{field: env[name] for name, field in required.items()},
            allowed_google_sub=env.get("LIBRIS_ALLOWED_GOOGLE_SUB") or None,
            cosmos_endpoint=env.get("LIBRIS_COSMOS_ENDPOINT") or None,
            cosmos_database=env.get("LIBRIS_COSMOS_DATABASE") or "libris",
        )


def create_app(
    settings: HostedSettings,
    *,
    store: AuthStore | None = None,
    identity: IdentityProvider | None = None,
    registrations_per_hour: int = REGISTRATIONS_PER_HOUR,
    sign_ins_per_hour: int = SIGN_INS_PER_HOUR,
    clock: Callable[[], float] = time.monotonic,
) -> Starlette:
    """Build the hosted ASGI app.

    Args:
        settings: Where the server lives and the credentials it holds.
        store: Where the authorization server keeps its records. Defaults to
            Cosmos when an endpoint is configured, and to memory otherwise.
        identity: Who proves the person's identity. Defaults to Google.
        registrations_per_hour: How many clients may register in any hour.
        sign_ins_per_hour: How many sign-ins may start in any hour.
        clock: The time, for the write limits.
    """
    if store is None:
        if settings.cosmos_endpoint:
            from .cosmos_auth import CosmosAuthStore

            store = CosmosAuthStore.connect(
                settings.cosmos_endpoint, settings.cosmos_database
            )
        else:
            store = MemoryAuthStore()
    if identity is None:
        identity = GoogleIdentity(
            settings.google_client_id, settings.google_client_secret
        )

    public_url = settings.public_url.rstrip("/")
    provider = LibrisAuthProvider(
        public_url=public_url,
        signing_key=settings.signing_key,
        allowed_sub=settings.allowed_google_sub,
        store=store,
        identity=identity,
    )
    mcp = MCPServer(
        name="libris",
        instructions="Libris tracks which books someone has read, is reading, or means to read.",
        version=installed_version(),
        auth_server_provider=provider,
        auth=AuthSettings(
            issuer_url=public_url,
            resource_server_url=provider.resource_url,
            required_scopes=[SCOPE],
            client_registration_options=ClientRegistrationOptions(
                enabled=True, valid_scopes=[SCOPE], default_scopes=[SCOPE]
            ),
            revocation_options=RevocationOptions(enabled=True),
        ),
    )

    @mcp.tool()
    def ping() -> str:
        """Check that this connection reaches the Library and is signed in."""
        return f"pong from libris {installed_version()}"

    @mcp.custom_route(CONSENT_PATH, methods=["GET"])
    async def consent_page(request: Request) -> Response:
        return await provider.consent_page(request)

    @mcp.custom_route(CONSENT_PATH, methods=["POST"])
    async def consent_decision(request: Request) -> Response:
        return await provider.consent_decision(request)

    @mcp.custom_route(GOOGLE_CALLBACK_PATH, methods=["GET"])
    async def google_callback(request: Request) -> Response:
        return await provider.google_callback(request)

    @mcp.custom_route("/healthz", methods=["GET"])
    async def healthz(request: Request) -> Response:
        return JSONResponse({"status": "ok"})

    app = mcp.streamable_http_app(
        # Stateless, so a restart or a scale-from-zero never strands a
        # session a client thinks it still has.
        stateless_http=True,
        json_response=True,
        # The SDK's DNS rebinding guard exists for servers on localhost. This
        # one is public by design and authenticates every call instead.
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=False
        ),
    )
    app.add_middleware(
        WriteLimit,
        per_hour={"/register": registrations_per_hour, "/authorize": sign_ins_per_hour},
        clock=clock,
    )
    return app


def create_app_from_env() -> Starlette:
    """The app as the container runs it, configured from its environment."""
    return create_app(HostedSettings.from_env())
