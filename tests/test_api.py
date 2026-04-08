from unittest.mock import MagicMock
from libris.api import GoogleBooksClient, Book

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
                    "industryIdentifiers": [{"type": "ISBN_13", "identifier": "1234567890123"}],
                    "pageCount": 180,
                    "publishedDate": "1925",
                    "imageLinks": {"thumbnail": "http://example.com/thumb.jpg"},
                    "categories": ["Classic"],
                    "description": "A novel about Jay Gatsby"
                }
            }
        ]
    }
    
    import httpx
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
