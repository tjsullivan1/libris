import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

import audible

from .config import get_config, get_config_dir

logger = logging.getLogger(__name__)

LIBRARY_RESPONSE_GROUPS = (
    "product_desc, product_attrs, contributors, "
    "is_finished, percent_complete, listening_status"
)


@dataclass
class AudibleBook:
    title: str
    authors: List[str]
    asin: str
    runtime_minutes: Optional[int] = None
    percent_complete: Optional[float] = None
    is_finished: bool = False
    thumbnail: Optional[str] = None
    subtitle: Optional[str] = None
    genres: List[str] = field(default_factory=list)
    description: Optional[str] = None


def get_auth_file() -> Path:
    return get_config_dir() / "audible_auth.json"


def is_authenticated() -> bool:
    return get_auth_file().exists()


def get_locale() -> str:
    config = get_config()
    return config.get("audible_locale", "us")


class AudibleClient:
    def __init__(self):
        auth_file = get_auth_file()
        if not auth_file.exists():
            raise FileNotFoundError(
                "Audible auth file not found. Run 'libris audible login' first."
            )
        self.auth = audible.Authenticator.from_file(str(auth_file))

    def get_library(self, num_results: int = 1000) -> List[AudibleBook]:
        with audible.Client(auth=self.auth) as client:
            library = client.get(
                "1.0/library",
                num_results=num_results,
                response_groups=LIBRARY_RESPONSE_GROUPS,
                sort_by="-PurchaseDate",
            )
        return [self._parse_book(item) for item in library.get("items", [])]

    def _parse_book(self, item: dict) -> AudibleBook:
        authors = [
            a.get("name", "Unknown Author")
            for a in item.get("authors", [])
        ]
        if not authors:
            authors = ["Unknown Author"]

        runtime_minutes = item.get("runtime_length_min")

        percent_complete = item.get("percent_complete")
        if percent_complete is not None:
            percent_complete = float(percent_complete)

        is_finished = bool(item.get("is_finished", False))

        thumbnail = item.get("product_images", {}).get("500") if isinstance(
            item.get("product_images"), dict
        ) else None

        genres = [
            cat.get("name")
            for ladder in item.get("category_ladders", [])
            for cat in ladder.get("ladder", [])
            if cat.get("name")
        ]

        return AudibleBook(
            title=item.get("title", "Unknown Title"),
            authors=authors,
            asin=item.get("asin", ""),
            runtime_minutes=runtime_minutes,
            percent_complete=percent_complete,
            is_finished=is_finished,
            thumbnail=thumbnail,
            subtitle=item.get("subtitle"),
            genres=genres,
            description=item.get("publisher_summary"),
        )
