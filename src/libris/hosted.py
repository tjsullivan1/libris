"""The hosted MCP server: Streamable HTTP behind Libris's own OAuth (ADR 0035).

It serves the same tools as `libris mcp` (ADR 0007, ADR 0020), answered from
what `libris sync` pushed to Cosmos. There is no Shelf here, so the tools that
write say so and write nothing, until remote writes arrive as Intents.

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

from mcp.server.auth.settings import (
    AuthSettings,
    ClientRegistrationOptions,
    RevocationOptions,
)
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from .mcp_server import create_server
from .oauth import (
    CONSENT_PATH,
    GOOGLE_CALLBACK_PATH,
    SCOPE,
    SIGN_INS_PER_CLIENT_PER_HOUR,
    AuthStore,
    GoogleIdentity,
    IdentityProvider,
    LibrisAuthProvider,
    MemoryAuthStore,
)
from .store import LibraryStore

# Registration is open by design (ADR 0035), and each one writes a document.
# Claude registers once per connection; this is far above any real use. Only
# registrations that succeed are counted, so a flood of malformed requests
# cannot use up the allowance and lock out a real connector. A determined
# caller can still use it up with valid ones, but that only delays new
# connectors: clients already registered keep working. Counting per source
# was ruled out, because uvicorn trusts every forwarded-for header here, so a
# source address is whatever the caller says it is.
REGISTRATIONS_PER_HOUR = 30


class RegistrationLimit:
    """Refuse registrations once an hour's worth have succeeded.

    In memory, so a restart forgets the count. That is fine for a limit whose
    job is to stop a flood, not to account for every request. Answers 429
    rather than letting the SDK report a refusal as invalid client metadata.
    """

    def __init__(
        self, app: ASGIApp, *, per_hour: int, clock: Callable[[], float]
    ) -> None:
        self._app = app
        self._per_hour = per_hour
        self._clock = clock
        self._recent: deque[float] = deque()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if not (scope["type"] == "http" and scope.get("path") == "/register"):
            await self._app(scope, receive, send)
            return
        now = self._clock()
        while self._recent and self._recent[0] <= now - 3600:
            self._recent.popleft()
        if len(self._recent) >= self._per_hour:
            retry_after = int(self._recent[0] + 3600 - now) + 1
            response = JSONResponse(
                {
                    "error": "temporarily_unavailable",
                    "error_description": "Too many registrations. Try again later.",
                },
                status_code=429,
                headers={"Retry-After": str(retry_after)},
            )
            await response(scope, receive, send)
            return

        # Reserve the slot now, with no await between the check above and this,
        # so concurrent registrations each see the others. Counting only once
        # the 201 went out let a burst all pass the check before any counted.
        # A registration that fails gives its slot back.
        self._recent.append(now)
        succeeded = False

        async def watching_send(message: dict) -> None:
            nonlocal succeeded
            if message["type"] == "http.response.start":
                succeeded = message["status"] == 201
            await send(message)

        try:
            await self._app(scope, receive, watching_send)
        finally:
            if not succeeded:
                self._recent.remove(now)


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
    sign_ins_per_client_per_hour: int = SIGN_INS_PER_CLIENT_PER_HOUR,
    clock: Callable[[], float] = time.monotonic,
    library: LibraryStore | None = None,
) -> Starlette:
    """Build the hosted ASGI app.

    Args:
        settings: Where the server lives and the credentials it holds.
        store: Where the authorization server keeps its records. Defaults to
            Cosmos when an endpoint is configured, and to memory otherwise.
        identity: Who proves the person's identity. Defaults to Google.
        registrations_per_hour: How many clients may register in any hour.
        sign_ins_per_client_per_hour: How many sign-ins one client may start in
            any hour.
        clock: The time, for the write limits.
        library: What the tools answer from. Defaults to the books `libris
            sync` pushed to Cosmos, when an endpoint is configured.
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
        sign_ins_per_client_per_hour=sign_ins_per_client_per_hour,
        limit_clock=clock,
    )
    if library is None and settings.cosmos_endpoint:
        from .cosmos_store import CosmosStore, connect_containers

        library = CosmosStore(
            *connect_containers(settings.cosmos_endpoint, settings.cosmos_database)
        )

    def library_store() -> LibraryStore:
        if library is None:
            # Said by the tool rather than refused at startup, so the sign-in
            # can still be tried out locally without a Cosmos account.
            raise ToolError(
                "This server has no Library to answer from: "
                "LIBRIS_COSMOS_ENDPOINT is not set."
            )
        return library

    mcp = create_server(
        store_provider=library_store,
        shelf_provider=None,
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
    app.add_middleware(RegistrationLimit, per_hour=registrations_per_hour, clock=clock)
    return app


def create_app_from_env() -> Starlette:
    """The app as the container runs it, configured from its environment."""
    return create_app(HostedSettings.from_env())
