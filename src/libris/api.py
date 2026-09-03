"""Google Books API client for searching and retrieving book metadata."""

import logging
import socket
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote

import httpx

from .config import get_api_key

logger = logging.getLogger(__name__)


def _default_rate_limit_notice(
    wait_seconds: float, attempt: int, max_retries: int
) -> None:
    """Inform the user that we are pausing because of an API rate limit.

    Args:
        wait_seconds: Number of seconds we are about to sleep.
        attempt: 1-based retry attempt number.
        max_retries: Total number of retries that will be attempted.
    """
    wait_display = f"{wait_seconds:.1f}".rstrip("0").rstrip(".")
    print(
        f"Rate limited by Google Books API. Waiting {wait_display}s before retrying "
        f"(attempt {attempt}/{max_retries})...",
        file=sys.stderr,
        flush=True,
    )


def parse_retry_after(value: object, now: datetime | None = None) -> float | None:
    """Parse an HTTP ``Retry-After`` header value into seconds.

    Supports both the delta-seconds form (e.g. ``"120"``) and the HTTP-date
    form (e.g. ``"Wed, 21 Oct 2015 07:28:00 GMT"``).

        value: Raw header value. Strings are parsed as ``Retry-After`` (delta-seconds
            or HTTP-date); numeric values are treated as delta-seconds. Missing or
            malformed values yield ``None``.
        now: Reference time used for HTTP-date values. Defaults to the
            current UTC time.

    Returns:
        The wait time in seconds (never negative), or ``None`` if the value
        could not be parsed.
    """
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return max(0.0, float(value))
    if not isinstance(value, str):
        return None

    raw = value.strip()
    if not raw:
        return None

    try:
        return max(0.0, float(raw))
    except ValueError:
        pass

    try:
        retry_at = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if retry_at is None:
        return None
    if retry_at.tzinfo is None:
        retry_at = retry_at.replace(tzinfo=timezone.utc)

    reference = now or datetime.now(timezone.utc)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    return max(0.0, (retry_at - reference).total_seconds())


@dataclass
class BookCandidate:
    """Metadata about a book proposed by an external source.

    A candidate has not been accepted into the Library. It may describe a book
    already held, a different edition, or the wrong book entirely. Reading state
    is deliberately absent: a rating is not metadata about the book.
    """

    title: str
    authors: list[str]
    isbn: str | None = None
    page_count: int | None = None
    published_date: str | None = None
    google_books_id: str = ""
    thumbnail: str | None = None
    genres: list[str] = field(default_factory=list)
    description: str | None = None
    source: str = "google_books"


class GoogleBooksClient:
    BASE_URL = "https://www.googleapis.com/books/v1/volumes"

    def __init__(
        self,
        timeout: float = 10.0,
        max_retries: int = 3,
        max_retry_wait: float = 300.0,
        on_rate_limit: Callable[[float, int, int], None] | None = None,
    ):
        """Create a Google Books API client.

        Args:
            timeout: Per-request timeout in seconds.
            max_retries: Number of retries attempted after the initial request.
            max_retry_wait: Upper bound, in seconds, for a wait requested by a
                ``Retry-After`` header. Protects against absurd server values.
            on_rate_limit: Callback invoked with ``(wait_seconds, attempt,
                max_retries)`` before sleeping due to a 429 response. Defaults
                to printing the wait time to stderr.
        """
        self.timeout = timeout
        self.max_retries = max_retries
        if max_retry_wait < 0:
            raise ValueError("max_retry_wait must be non-negative.")
        self.max_retry_wait = max_retry_wait
        self.on_rate_limit = on_rate_limit or _default_rate_limit_notice

    def _rate_limit_wait(self, response: object, attempt: int) -> float:
        """Determine how long to wait after a rate-limited response.

        Uses the ``Retry-After`` header when present and parseable, otherwise
        falls back to exponential backoff.

        Args:
            response: The rate-limited HTTP response.
            attempt: Zero-based attempt index used for backoff.

        Returns:
            The number of seconds to sleep.
        """
        retry_after = None
        headers = getattr(response, "headers", None)
        if headers is not None:
            try:
                retry_after = parse_retry_after(headers.get("Retry-After"))
            except (AttributeError, TypeError):
                retry_after = None

        if retry_after is None:
            return float(2**attempt)
        return min(retry_after, self.max_retry_wait)

    def _handle_rate_limit(self, response: object, attempt: int) -> None:
        """Notify the user and sleep before retrying a rate-limited request.

        Args:
            response: The rate-limited HTTP response.
            attempt: Zero-based attempt index used for backoff.
        """
        wait_time = self._rate_limit_wait(response, attempt)
        logger.warning(
            f"Rate limited (429). Retrying in {wait_time}s... "
            f"(Attempt {attempt + 1}/{self.max_retries})"
        )
        self.on_rate_limit(wait_time, attempt + 1, self.max_retries)
        time.sleep(wait_time)

    def _identifying_params(self) -> dict:
        """Build the params that say who is asking.

        Returns:
            The API key when one is configured, and otherwise a `quotaUser` so
            an unauthenticated caller is rate limited on its own account rather
            than against every anonymous user of the API.
        """
        params: dict = {}
        api_key = get_api_key()
        if api_key:
            params["key"] = api_key
            return params
        try:
            params["quotaUser"] = socket.gethostname()
        except OSError:
            logger.debug("Could not determine hostname for quotaUser.")
        return params

    def _get(self, url: str, params: dict) -> dict:
        """Fetch one URL, retrying what is worth retrying.

        Extracted from `search` so `get_volume` shares the retry and rate-limit
        handling rather than growing a second, subtly different copy of it.

        Args:
            url: The endpoint to fetch.
            params: Query parameters, before the identifying ones are added.

        Returns:
            The decoded JSON body.

        Raises:
            httpx.HTTPStatusError: If the response is an error the retries could
                not clear.
            httpx.RequestError: If the request could not be made.
        """
        params = {**params, **self._identifying_params()}

        with httpx.Client(timeout=self.timeout) as client:
            for attempt in range(self.max_retries + 1):
                try:
                    response = client.get(url, params=params)

                    if response.status_code == 429:
                        if attempt < self.max_retries:
                            self._handle_rate_limit(response, attempt)
                            continue

                    response.raise_for_status()
                    return response.json()
                except (httpx.HTTPStatusError, httpx.RequestError) as e:
                    if attempt < self.max_retries and (
                        isinstance(e, httpx.RequestError)
                        or (
                            isinstance(e, httpx.HTTPStatusError)
                            and e.response.status_code in [429, 500, 502, 503, 504]
                        )
                    ):
                        if (
                            isinstance(e, httpx.HTTPStatusError)
                            and e.response.status_code == 429
                        ):
                            self._handle_rate_limit(e.response, attempt)
                            continue
                        wait_time = 2**attempt
                        logger.warning(
                            f"Request failed: {e}. Retrying in {wait_time}s... (Attempt {attempt + 1}/{self.max_retries})"
                        )
                        time.sleep(wait_time)
                        continue
                    raise
            # Reached when every 429 retry was used without an exception being
            # raised: ask once more and let the error out this time.
            response.raise_for_status()
            return response.json()

    @staticmethod
    def _to_candidate(item: dict) -> BookCandidate:
        """Build a Book Candidate from one Google Books volume.

        Args:
            item: A volume, as the API returns it.

        Returns:
            The candidate it describes. An ISBN-13 is preferred over an ISBN-10,
            because that is what a Book Note records.
        """
        volume_info = item.get("volumeInfo", {})

        isbn = None
        for ident in volume_info.get("industryIdentifiers", []):
            if ident.get("type") == "ISBN_13":
                isbn = ident.get("identifier")
                break
            if ident.get("type") == "ISBN_10" and not isbn:
                isbn = ident.get("identifier")

        return BookCandidate(
            title=volume_info.get("title", "Unknown Title"),
            authors=volume_info.get("authors", ["Unknown Author"]),
            isbn=isbn,
            page_count=volume_info.get("pageCount"),
            published_date=volume_info.get("publishedDate"),
            google_books_id=item.get("id"),
            thumbnail=volume_info.get("imageLinks", {}).get("thumbnail"),
            genres=volume_info.get("categories", []),
            description=volume_info.get("description"),
        )

    def search(self, query: str) -> list[BookCandidate]:
        """Find Book Candidates matching a query.

        Args:
            query: A Google Books query, which may carry `intitle:` and
                `inauthor:` operators.

        Returns:
            The candidates the API offered, in the order it offered them.
        """
        data = self._get(self.BASE_URL, {"q": query, "maxResults": 10})
        return [self._to_candidate(item) for item in data.get("items", [])]

    def get_volume(self, google_books_id: str) -> BookCandidate | None:
        """Fetch one volume by the id that names it.

        This is what lets a Surface name a Book Candidate instead of handing the
        whole record back (ADR 0025): the metadata written into a Book Note is
        always fetched by this code rather than retyped by a caller, so a note
        can never assert something the source did not say.

        Args:
            google_books_id: The volume id, as carried on a Book Candidate.

        Returns:
            The candidate, or None if Google Books has no such volume.

        Raises:
            httpx.HTTPStatusError: If the API failed for any reason other than
                the volume being absent. Only a 404 means "no such book";
                anything else means the answer is unknown, and reporting that
                as a miss would let a write proceed on nothing.
            httpx.RequestError: If the request could not be made.

        Note:
            Google Books distinguishes an absent volume from a malformed id, and
            not in the way you would expect. Measured against the live API: a
            well-formed id naming nothing ("zzzzzzzzzzzz") answers 404 with "The
            volume ID could not be found", while a malformed one ("x", or
            anything not of the right shape) answers 503 "Service temporarily
            unavailable". So a caller that invents an id - which for MCP means a
            model that hallucinated or truncated one - is retried with backoff
            and then told the service is down. A Surface reporting that failure
            should name both causes rather than only the outage.
        """
        url = f"{self.BASE_URL}/{quote(google_books_id, safe='')}"
        try:
            data = self._get(url, {})
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return None
            raise
        return self._to_candidate(data)
