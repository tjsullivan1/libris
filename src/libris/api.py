import httpx
import time
import logging
import socket
from typing import List, Dict, Any, Optional
from dataclasses import dataclass
from .config import get_api_key

logger = logging.getLogger(__name__)

@dataclass
class Book:
    title: str
    authors: List[str]
    isbn: Optional[str]
    page_count: Optional[int]
    published_date: Optional[str]
    google_books_id: str
    thumbnail: Optional[str]
    genres: List[str]
    description: Optional[str]

class GoogleBooksClient:
    BASE_URL = "https://www.googleapis.com/books/v1/volumes"

    def __init__(self, timeout: float = 10.0, max_retries: int = 3):
        self.timeout = timeout
        self.max_retries = max_retries

    def search(self, query: str) -> List[Book]:
        params = {"q": query, "maxResults": 10}
        
        api_key = get_api_key()
        if api_key:
            params["key"] = api_key
        else:
            # Use a unique identifier to help avoid global rate limits for unauthenticated users
            try:
                params["quotaUser"] = socket.gethostname()
            except Exception:
                pass

        with httpx.Client(timeout=self.timeout) as client:
            for attempt in range(self.max_retries + 1):
                try:
                    response = client.get(self.BASE_URL, params=params)
                    
                    if response.status_code == 429:
                        if attempt < self.max_retries:
                            wait_time = 2 ** attempt
                            logger.warning(f"Rate limited (429). Retrying in {wait_time}s... (Attempt {attempt + 1}/{self.max_retries})")
                            time.sleep(wait_time)
                            continue
                    
                    response.raise_for_status()
                    data = response.json()
                    break
                except (httpx.HTTPStatusError, httpx.RequestError) as e:
                    if attempt < self.max_retries and (
                        isinstance(e, httpx.RequestError) or 
                        (isinstance(e, httpx.HTTPStatusError) and e.response.status_code in [429, 500, 502, 503, 504])
                    ):
                        wait_time = 2 ** attempt
                        logger.warning(f"Request failed: {e}. Retrying in {wait_time}s... (Attempt {attempt + 1}/{self.max_retries})")
                        time.sleep(wait_time)
                        continue
                    raise
            else:
                # This part is technically reached if all retries for 429 were exhausted but no exception was raised
                response.raise_for_status()
                data = response.json()

        items = data.get("items", [])
        books = []
        for item in items:
            volume_info = item.get("volumeInfo", {})
            
            # Extract ISBN
            isbn = None
            for ident in volume_info.get("industryIdentifiers", []):
                if ident.get("type") == "ISBN_13":
                    isbn = ident.get("identifier")
                    break
                if ident.get("type") == "ISBN_10" and not isbn:
                    isbn = ident.get("identifier")

            book = Book(
                title=volume_info.get("title", "Unknown Title"),
                authors=volume_info.get("authors", ["Unknown Author"]),
                isbn=isbn,
                page_count=volume_info.get("pageCount"),
                published_date=volume_info.get("publishedDate"),
                google_books_id=item.get("id"),
                thumbnail=volume_info.get("imageLinks", {}).get("thumbnail"),
                genres=volume_info.get("categories", []),
                description=volume_info.get("description")
            )
            books.append(book)
        return books
