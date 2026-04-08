import json
from unittest.mock import MagicMock, patch

from libris.audible_client import AudibleBook, AudibleClient, get_auth_file, is_authenticated, get_locale


def test_audible_book_dataclass():
    book = AudibleBook(
        title="Project Hail Mary",
        authors=["Andy Weir"],
        asin="B08G9PRS1K",
        runtime_minutes=976,
        percent_complete=100.0,
        is_finished=True,
    )
    assert book.title == "Project Hail Mary"
    assert book.authors == ["Andy Weir"]
    assert book.asin == "B08G9PRS1K"
    assert book.runtime_minutes == 976
    assert book.percent_complete == 100.0
    assert book.is_finished is True
    assert book.genres == []
    assert book.thumbnail is None


def test_audible_book_defaults():
    book = AudibleBook(title="Test", authors=["Author"], asin="B000000000")
    assert book.runtime_minutes is None
    assert book.percent_complete is None
    assert book.is_finished is False
    assert book.subtitle is None
    assert book.description is None


def test_get_auth_file(mock_config_dir):
    auth_file = get_auth_file()
    assert auth_file == mock_config_dir / "audible_auth.json"


def test_is_authenticated_false(mock_config_dir):
    assert is_authenticated() is False


def test_is_authenticated_true(mock_config_dir):
    auth_file = mock_config_dir / "audible_auth.json"
    auth_file.write_text("{}")
    assert is_authenticated() is True


def test_get_locale_default(mock_config_dir):
    assert get_locale() == "us"


def test_get_locale_configured(mock_config_dir):
    import yaml
    config_file = mock_config_dir / "config.yaml"
    config_file.write_text(yaml.dump({"audible_locale": "uk"}))
    assert get_locale() == "uk"


def test_client_raises_without_auth(mock_config_dir):
    import pytest
    with pytest.raises(FileNotFoundError, match="Audible auth file not found"):
        AudibleClient()


def test_parse_book():
    item = {
        "title": "The Martian",
        "authors": [{"name": "Andy Weir"}],
        "asin": "B00B5HZGSE",
        "runtime_length_min": 645,
        "percent_complete": 75.5,
        "is_finished": False,
        "subtitle": "A Novel",
        "publisher_summary": "A lone astronaut on Mars.",
        "product_images": {"500": "http://example.com/image.jpg"},
        "category_ladders": [
            {"ladder": [{"name": "Science Fiction"}, {"name": "Hard Sci-Fi"}]}
        ],
    }

    client = AudibleClient.__new__(AudibleClient)
    book = client._parse_book(item)

    assert book.title == "The Martian"
    assert book.authors == ["Andy Weir"]
    assert book.asin == "B00B5HZGSE"
    assert book.runtime_minutes == 645
    assert book.percent_complete == 75.5
    assert book.is_finished is False
    assert book.subtitle == "A Novel"
    assert book.description == "A lone astronaut on Mars."
    assert book.thumbnail == "http://example.com/image.jpg"
    assert "Science Fiction" in book.genres
    assert "Hard Sci-Fi" in book.genres


def test_parse_book_missing_fields():
    item = {"asin": "B000000000"}

    client = AudibleClient.__new__(AudibleClient)
    book = client._parse_book(item)

    assert book.title == "Unknown Title"
    assert book.authors == ["Unknown Author"]
    assert book.asin == "B000000000"
    assert book.runtime_minutes is None
    assert book.percent_complete is None
    assert book.is_finished is False
    assert book.thumbnail is None
    assert book.genres == []


def test_get_library(mock_config_dir):
    # Create a fake auth file so the client can be instantiated
    auth_file = mock_config_dir / "audible_auth.json"
    auth_file.write_text("{}")

    mock_library_response = {
        "items": [
            {
                "title": "Book One",
                "authors": [{"name": "Author A"}],
                "asin": "B001",
                "runtime_length_min": 300,
                "percent_complete": 50.0,
                "is_finished": False,
            },
            {
                "title": "Book Two",
                "authors": [{"name": "Author B"}],
                "asin": "B002",
                "runtime_length_min": 500,
                "percent_complete": 100.0,
                "is_finished": True,
            },
        ]
    }

    with patch("libris.audible_client.audible.Authenticator.from_file") as mock_from_file, \
         patch("libris.audible_client.audible.Client") as mock_client_cls:

        mock_auth = MagicMock()
        mock_from_file.return_value = mock_auth

        mock_client = MagicMock()
        mock_client.get.return_value = mock_library_response
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client_cls.return_value = mock_client

        client = AudibleClient()
        books = client.get_library()

        assert len(books) == 2
        assert books[0].title == "Book One"
        assert books[0].percent_complete == 50.0
        assert books[1].title == "Book Two"
        assert books[1].is_finished is True

        mock_client.get.assert_called_once_with(
            "1.0/library",
            num_results=1000,
            response_groups="product_desc, product_attrs, contributors, "
                           "is_finished, percent_complete, listening_status",
            sort_by="-PurchaseDate",
        )
