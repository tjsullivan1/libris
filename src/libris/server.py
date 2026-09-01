"""The local daemon the browser extension talks to.

An adapter over the same service layer the CLI uses, not a second
implementation of it (ADR 0008). It binds loopback and checks duplicates
against the live Shelf, so it can promise a Book was not in the Library and now
is - a guarantee the remote replica cannot make (ADR 0010).

Optional: the web stack installs only with `libris[server]`, so importing this
module fails on a core install. cli.py imports it lazily and says so.
"""

import logging
import secrets
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import APIRouter, FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from . import config, installed_version, service, shelf
from .api import BookCandidate, GoogleBooksClient

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


# How long a single request may spend talking to Google Books. The client
# already retries a 429, but it will wait up to five minutes doing it, and a
# browser popup cannot sit behind that (#53).
UPSTREAM_MAX_WAIT_SECONDS = 15.0

# Which duplicate guarantee this adapter gives. The daemon checks the live
# Shelf, so it can say a Book was not held and now is. The remote checks a
# replica no fresher than the last sync and will answer differently, which is
# why every write states it rather than leaving the client to infer it from
# its own configuration (ADR 0010, ADR 0016).
GUARANTEE = "live_shelf"


class CandidateModel(BaseModel):
    """A Book Candidate crossing the HTTP boundary."""

    title: str
    authors: list[str] = []
    isbn: str | None = None
    page_count: int | None = None
    published_date: str | None = None
    google_books_id: str = ""
    thumbnail: str | None = None
    genres: list[str] = []
    description: str | None = None

    @classmethod
    def of(cls, candidate: BookCandidate) -> "CandidateModel":
        """Build the wire form of a Book Candidate."""
        return cls(
            title=candidate.title,
            authors=candidate.authors,
            isbn=candidate.isbn,
            page_count=candidate.page_count,
            published_date=candidate.published_date,
            google_books_id=candidate.google_books_id,
            thumbnail=candidate.thumbnail,
            genres=candidate.genres,
            description=candidate.description,
        )

    def to_candidate(self) -> BookCandidate:
        """Build the domain Book Candidate this describes."""
        return BookCandidate(
            title=self.title,
            authors=self.authors,
            isbn=self.isbn,
            page_count=self.page_count,
            published_date=self.published_date,
            google_books_id=self.google_books_id,
            thumbnail=self.thumbnail,
            genres=self.genres,
            description=self.description,
        )


class LookupRequest(BaseModel):
    """What a page yielded, as scraped."""

    isbn: str | None = None
    asin: str | None = None
    title: str | None = None
    authors: list[str] = []
    source_url: str | None = None


class LookupResponse(BaseModel):
    """Candidates for a person to choose between, best described first."""

    candidates: list[CandidateModel]


class ExistingBook(BaseModel):
    """A Book Note already on the Shelf."""

    libris_id: str | None
    path: str
    title: str | None
    authors: list[str]


class CreateRequest(BaseModel):
    """A chosen Book Candidate, plus the reading state to record with it."""

    candidate: CandidateModel
    overrides: dict[str, Any] = {}


class WriteResponse(BaseModel):
    """The answer to a write: identity, what happened, and what backs it."""

    libris_id: str | None
    path: str
    outcome: str
    guarantee: str


def libris_version() -> str:
    """Get the installed libris version, or "unknown" if it cannot be read."""
    return installed_version()


@asynccontextmanager
async def _warm_index(_app: FastAPI):
    """Read the Shelf before accepting requests.

    The index makes every query after the first cost nothing, but the first
    still pays for the whole Shelf - about seven seconds for 3,061 notes. Left
    to happen on demand, that cost lands on somebody waiting in a browser
    popup, which is the one moment it is least welcome.

    So the daemon binds late instead. Started from a scheduled task at logon
    (#55) nobody is waiting; started by hand it is one wait rather than a
    surprise later. A Surface that connects during it sees no daemon at all,
    which is a state the popup already reports plainly.
    """
    vault_path = config.get_vault_path()
    count = len(shelf.index_for(vault_path).notes())
    logging.getLogger(__name__).info("Indexed %d Book Notes", count)
    yield


def create_app() -> FastAPI:
    """Build the daemon's ASGI app.

    A factory rather than a module-level app so configuration is read at start
    rather than at import, and so tests can build an app per configuration.

    Returns:
        The configured FastAPI application.
    """
    app = FastAPI(title="Libris", version=libris_version(), lifespan=_warm_index)

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

    router = APIRouter(prefix="/api/v1")

    @router.post("/lookup", response_model=LookupResponse)
    async def lookup(request: LookupRequest) -> LookupResponse:
        """Find Book Candidates for a page, ranked best described first."""
        try:
            candidates = service.lookup_candidates(
                isbn=request.isbn,
                asin=request.asin,
                title=request.title,
                authors=request.authors,
                client=GoogleBooksClient(
                    timeout=UPSTREAM_MAX_WAIT_SECONDS / 3,
                    max_retries=1,
                    max_retry_wait=UPSTREAM_MAX_WAIT_SECONDS / 3,
                ),
            )
        except (httpx.HTTPStatusError, httpx.RequestError) as exc:
            raise HTTPException(
                status_code=502, detail=f"Google Books could not be reached: {exc}"
            ) from None

        return LookupResponse(candidates=[CandidateModel.of(c) for c in candidates])

    @router.get("/books", response_model=ExistingBook)
    async def get_book(
        isbn: str | None = None,
        google_books_id: str | None = None,
        title: str | None = None,
        authors: list[str] = Query(default=[]),
    ) -> ExistingBook:
        """Report whether the Library already holds a Book."""
        vault_path = config.get_vault_path()
        note = service.find_existing(
            vault_path,
            isbn=isbn,
            google_books_id=google_books_id,
            title=title,
            authors=authors,
        )
        if note is None:
            # A miss is a miss (ADR 0003), so this is still a 404. Notes that
            # might be the same Book ride along, because a scraped title often
            # lacks the subtitle the note carries - and deciding that for the
            # caller is exactly what would say "already held" about a Book that
            # is not. The person in the popup settles it.
            similar = service.find_similar(vault_path, title=title, authors=authors)
            raise HTTPException(
                status_code=404,
                detail={
                    "message": "Not in your Library.",
                    "similar": [
                        {
                            "libris_id": n.libris_id,
                            "path": str(n.path),
                            "title": n.title,
                            "authors": n.authors,
                        }
                        for n in similar
                    ],
                },
            )

        return ExistingBook(
            libris_id=note.libris_id,
            path=str(note.path),
            title=note.title,
            authors=note.authors,
        )

    @router.post("/books", response_model=WriteResponse, status_code=201)
    async def create_book(request: CreateRequest, response: Response) -> WriteResponse:
        """Add a Book to the Library, unless it is already held."""
        try:
            result = service.add_book(
                config.get_vault_path(),
                request.candidate.to_candidate(),
                overrides=request.overrides or None,
            )
        except ValueError as exc:
            # InvalidFieldValue subclasses ValueError, so this covers both an
            # unknown field and a value the Library does not define. Neither is
            # a crash: the caller sent something the Library will not accept.
            raise HTTPException(status_code=422, detail=str(exc)) from None

        if result.outcome is service.Outcome.ALREADY_PRESENT:
            response.status_code = 200

        return WriteResponse(
            libris_id=result.libris_id,
            path=str(result.path),
            outcome=result.outcome.value,
            guarantee=GUARANTEE,
        )

    app.include_router(router)

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
