"""The local daemon the browser extension talks to.

An adapter over the same service layer the CLI uses, not a second
implementation of it (ADR 0008). It binds loopback and checks duplicates
against the live Shelf, so it can promise a Book was not in the Library and now
is - a guarantee the remote replica cannot make (ADR 0010).

Optional: the web stack installs only with `libris[server]`, so importing this
module fails on a core install. cli.py imports it lazily and says so.
"""

import secrets
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as package_version

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from . import config

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8787

# Health is reachable without a credential so the extension popup can tell a
# misconfigured token apart from a daemon that is not running.
UNAUTHENTICATED_PATHS = frozenset({"/health"})


class Health(BaseModel):
    """What the extension popup shows to say which Library it reached."""

    status: str
    version: str
    vault_path: str


def libris_version() -> str:
    """Get the installed libris version, or "unknown" if it cannot be read."""
    try:
        return package_version("libris")
    except PackageNotFoundError:
        return "unknown"


def create_app() -> FastAPI:
    """Build the daemon's ASGI app.

    A factory rather than a module-level app so configuration is read at start
    rather than at import, and so tests can build an app per configuration.

    Returns:
        The configured FastAPI application.
    """
    app = FastAPI(title="Libris", version=libris_version())

    @app.middleware("http")
    async def require_token(request: Request, call_next):
        """Reject anything without the bearer token, before routing.

        Checked in middleware rather than as a route dependency so an
        unauthenticated caller cannot learn which paths exist: every path
        answers 401 alike.
        """
        if request.url.path in UNAUTHENTICATED_PATHS:
            return await call_next(request)

        token = config.get_server_token()
        scheme, _, presented = request.headers.get("authorization", "").partition(" ")
        if (
            not token
            or scheme.lower() != "bearer"
            or not secrets.compare_digest(presented, token)
        ):
            return JSONResponse(
                {"detail": "Missing or invalid bearer token."},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
        return await call_next(request)

    # Added last, so it wraps the token check: a browser preflight carries no
    # credential and must be answered by CORS rather than refused by auth.
    # The allowlist is never "*" - the daemon is reachable from every page the
    # browser has open, and the token is the only other thing guarding it.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=config.get_extension_origins(),
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
    )

    @app.get("/health", response_model=Health)
    async def health() -> Health:
        """Report that the daemon is up and which Shelf it is serving."""
        return Health(
            status="ok",
            version=libris_version(),
            vault_path=str(config.get_vault_path()),
        )

    return app


def run(
    *, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, reload: bool = False
) -> None:
    """Serve the daemon until interrupted.

    Args:
        host: Interface to bind. The CLI refuses non-loopback hosts unless
            explicitly allowed.
        port: Port to listen on.
        reload: Restart on source changes, for development.
    """
    import uvicorn

    uvicorn.run(
        "libris.server:create_app",
        factory=True,
        host=host,
        port=port,
        reload=reload,
    )
