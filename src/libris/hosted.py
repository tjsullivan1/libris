"""The hosted MCP server: Streamable HTTP behind Libris's own OAuth (ADR 0035).

It serves the same tools as `libris mcp` (ADR 0007, ADR 0020), answered from
what `libris sync` pushed to Cosmos. There is no Shelf here, so an add or an
update is recorded as an Intent for the next sync to apply (ADR 0002, #176,
#177).

Configured from environment variables rather than `config.yaml`, because it
runs in a container whose settings come from the Container App (and its secrets
from Key Vault), not from a person's config directory.

Run it the way the container does:

    uvicorn --factory libris.hosted:create_app_from_env --host 0.0.0.0 --port 8000
"""

import logging
import os
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

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
from .service import IntentRecorder
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
    intents: IntentRecorder | None = None,
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
        intents: Where an add or update is recorded for sync to apply.
            Defaults to the `intents` container, when an endpoint is
            configured; without one, writes are refused.
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
    if intents is None and settings.cosmos_endpoint:
        from .cosmos_store import connect_container
        from .intents import INTENTS, IntentQueue

        (container,) = connect_container(
            settings.cosmos_endpoint, settings.cosmos_database, INTENTS
        )
        intents = IntentQueue(container)

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
        intent_recorder=(lambda: intents) if intents is not None else None,
        auth_server_provider=provider,
        auth=AuthSettings(
            issuer_url=public_url,
            resource_server_url=provider.resource_url,
            # load_access_token already refuses a token whose audience is not
            # this resource; the SDK checking too (GHSA-w4fh-qvv9-3v23) means
            # a change to that function cannot quietly open the door.
            validate_token_resource=True,
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


# Set by the Container App when Application Insights exists (#179).
TELEMETRY_CONNECTION_STRING = "APPLICATIONINSIGHTS_CONNECTION_STRING"

# The liveness probe asks every few seconds while a replica runs. Tracing it
# would bill for thousands of identical requests a day and bury the real ones.
# A regex, searched for in the request's full URL (`https://host/healthz`), so
# it is anchored on the scheme rather than with a bare `^/healthz` - which
# would match nothing - and ends at the path, so `/healthz2` is still traced
# (#222 review).
UNTRACED_URLS = r"^https?://[^/]+/healthz$"

# Uvicorn's own logger: it reaches the container's stdout, and so Log
# Analytics, whether or not tracing works.
_STARTUP_LOG = logging.getLogger("uvicorn.error")


# Where a request span keeps its URL, in the old semantic conventions and the
# stable ones.
_URL_ATTRIBUTES = ("http.url", "http.target", "url.full")


def drop_query_strings(span: Any, scope: Scope) -> None:
    """Keep a request's query string out of its span.

    The sign-in carries credentials there: Google's callback brings an
    authorization code as `?code=`, `/authorize` a PKCE challenge and the
    client's state, and the consent page the pending sign-in's ID (#222
    review). The path says which endpoint was called, and that is all a trace
    needs.

    Args:
        span: The request span, already holding the URL attributes.
        scope: The ASGI scope. Unused; the hook's signature requires it.
    """
    attributes = getattr(span, "attributes", None) or {}
    for name in _URL_ATTRIBUTES:
        value = attributes.get(name)
        if isinstance(value, str) and "?" in value:
            span.set_attribute(name, value.split("?", 1)[0])
    if "url.query" in attributes:
        span.set_attribute("url.query", "")


def export_to_azure_monitor(connection_string: str) -> None:
    """Send traces and the `libris` logs to Application Insights.

    Signs in as the app's managed identity: the Application Insights resource
    refuses its ingestion key (ADR 0006), so the connection string only says
    where to send, not who may.
    """
    from azure.identity import DefaultAzureCredential
    from azure.monitor.opentelemetry import configure_azure_monitor

    configure_azure_monitor(
        connection_string=connection_string,
        credential=DefaultAzureCredential(),
        # The root logger would also take the Azure SDK's own HTTP logging,
        # billed by the gigabyte. Container stdout reaches Log Analytics anyway.
        logger_name="libris",
        enable_live_metrics=False,
    )


def instrument(
    app: Starlette,
    environ: Mapping[str, str] | None = None,
    *,
    export: Callable[[str], None] = export_to_azure_monitor,
) -> bool:
    """Trace each request, when the environment says where to send traces.

    The MCP SDK opens a span for every tool call by itself (`tools/call
    find_book`); this gives those spans an exporter and wraps each in the HTTP
    request it arrived on, so a call can be found by tool name and its status
    and duration read beside it. Added outermost, so a request refused before
    it reaches the SDK, such as a 429 from the registration limit, is traced
    too.

    Args:
        app: The hosted app.
        environ: Where to look for the connection string. Defaults to the
            process environment.
        export: Points OpenTelemetry at its destination. Replaced in tests.

    Tracing is the one part of the server it can do without. If it cannot be
    switched on - the packages are missing, or the connection string is
    malformed - the server says so in its log and serves the Library untraced,
    rather than refusing to start over its telemetry (#222 review).

    Returns:
        Whether tracing was switched on.
    """
    env = os.environ if environ is None else environ
    connection_string = env.get(TELEMETRY_CONNECTION_STRING)
    if not connection_string:
        return False
    try:
        from opentelemetry.instrumentation.asgi import OpenTelemetryMiddleware

        export(connection_string)
    except (ImportError, ValueError) as exc:
        _STARTUP_LOG.warning(
            "%s is set, but tracing could not start, so requests are not traced: %s",
            TELEMETRY_CONNECTION_STRING,
            exc,
        )
        return False
    app.add_middleware(
        OpenTelemetryMiddleware,
        excluded_urls=UNTRACED_URLS,
        # One span per ASGI message would triple the count for nothing.
        exclude_spans=["receive", "send"],
        server_request_hook=drop_query_strings,
    )
    return True


def quiet_azure_sdk_logs() -> None:
    """Keep the Azure SDKs' INFO logging out of the container's output.

    The MCP SDK sets the root logger to INFO, so every Azure SDK logs at that
    level too: each managed-identity token fetch, each Cosmos request and each
    telemetry upload, headers and all, a few dozen lines a minute from a replica
    doing nothing (#179). Their values are redacted, so this is noise, not a
    leak, but it buried the lines worth reading. Warnings and errors still show.
    """
    logging.getLogger("azure").setLevel(logging.WARNING)


def create_app_from_env() -> Starlette:
    """The app as the container runs it, configured from its environment."""
    quiet_azure_sdk_logs()
    app = create_app(HostedSettings.from_env())
    instrument(app)
    return app
