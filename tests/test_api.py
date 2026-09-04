from unittest.mock import MagicMock, patch

import httpx
import pytest

from libris.api import GoogleBooksClient


def test_search_gatsby():
    client = GoogleBooksClient()

    # Mock response
    mock_data = {
        "items": [
            {
                "id": "123",
                "volumeInfo": {
                    "title": "The Great Gatsby",
                    "authors": ["F. Scott Fitzgerald"],
                    "industryIdentifiers": [
                        {"type": "ISBN_13", "identifier": "1234567890123"}
                    ],
                    "pageCount": 180,
                    "publishedDate": "1925",
                    "imageLinks": {"thumbnail": "http://example.com/thumb.jpg"},
                    "categories": ["Classic"],
                    "description": "A novel about Jay Gatsby",
                },
            }
        ]
    }

    from unittest.mock import patch

    with patch("httpx.Client.get") as mock_get:
        mock_response = MagicMock()
        mock_response.json.return_value = mock_data
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()
        mock_get.return_value = mock_response

        books = client.search("The Great Gatsby")
        assert len(books) == 1
        assert books[0].title == "The Great Gatsby"
        assert books[0].isbn == "1234567890123"


# --- fetching one volume by id (ADR 0025) ---


def _volume_payload(**overrides):
    volume_info = {
        "title": "Dune",
        "authors": ["Frank Herbert"],
        "industryIdentifiers": [{"type": "ISBN_13", "identifier": "9780441013593"}],
        "pageCount": 412,
        "publishedDate": "1965",
        "imageLinks": {"thumbnail": "http://example.com/dune.jpg"},
        "categories": ["Science Fiction"],
        "description": "A desert planet.",
    }
    volume_info.update(overrides)
    return {"id": "dune-1", "volumeInfo": volume_info}


def test_a_volume_is_fetched_by_its_own_id():
    # Given a Google Books volume
    client = GoogleBooksClient()

    with patch("httpx.Client.get") as mock_get:
        mock_response = MagicMock()
        mock_response.json.return_value = _volume_payload()
        mock_response.status_code = 200
        mock_get.return_value = mock_response

        # When it is asked for by id
        candidate = client.get_volume("dune-1")

        # Then the volume endpoint was addressed, not a search
        url = mock_get.call_args.args[0]
        assert url.endswith("/volumes/dune-1")

    # And the candidate carries what the source said, rather than what a caller
    # re-typed. This is the whole point of naming a candidate by id (ADR 0025).
    assert candidate.title == "Dune"
    assert candidate.authors == ["Frank Herbert"]
    assert candidate.isbn == "9780441013593"
    assert candidate.page_count == 412
    assert candidate.google_books_id == "dune-1"
    assert candidate.description == "A desert planet."


def test_a_volume_that_does_not_exist_is_a_miss_not_a_crash():
    # Given an id Google Books does not know
    client = GoogleBooksClient(max_retries=0)

    with patch("httpx.Client.get") as mock_get:
        mock_response = MagicMock()
        mock_response.status_code = 404
        mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "404", request=MagicMock(), response=mock_response
        )
        mock_get.return_value = mock_response

        # When it is fetched
        # Then it answers None. A caller can then say "no such volume" rather
        # than surfacing an HTTP error to a person (ADR 0021).
        assert client.get_volume("no-such-volume") is None


def test_a_volume_id_is_escaped_into_the_path():
    # Given an id carrying a character that would otherwise change the path
    client = GoogleBooksClient()

    with patch("httpx.Client.get") as mock_get:
        mock_response = MagicMock()
        mock_response.json.return_value = _volume_payload()
        mock_response.status_code = 200
        mock_get.return_value = mock_response

        client.get_volume("a/../b")

        # Then it cannot climb out of the volumes path
        url = mock_get.call_args.args[0]
        assert url.endswith("/volumes/a%2F..%2Fb")


def test_an_upstream_failure_is_raised_rather_than_swallowed():
    # Given Google Books returning a server error
    client = GoogleBooksClient(max_retries=0)

    with patch("httpx.Client.get") as mock_get:
        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "500", request=MagicMock(), response=mock_response
        )
        mock_get.return_value = mock_response

        # When a volume is fetched
        # Then it raises. Only a 404 means "no such book"; everything else means
        # the answer is unknown, and reporting that as a miss would let a write
        # proceed on nothing.
        with pytest.raises(httpx.HTTPStatusError):
            client.get_volume("dune-1")
