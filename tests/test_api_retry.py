import httpx
import pytest
from unittest.mock import MagicMock, patch
from libris.api import GoogleBooksClient

def test_search_retry_on_429():
    client = GoogleBooksClient(max_retries=2)
    
    mock_response_429 = MagicMock()
    mock_response_429.status_code = 429
    mock_response_429.raise_for_status.side_effect = httpx.HTTPStatusError(
        "Rate Limit", request=MagicMock(), response=mock_response_429
    )

    mock_response_200 = MagicMock()
    mock_response_200.status_code = 200
    mock_response_200.json.return_value = {"items": []}
    
    with patch("httpx.Client.get") as mock_get:
        # First call returns 429, second returns 200
        mock_get.side_effect = [mock_response_429, mock_response_200]
        
        with patch("time.sleep") as mock_sleep:
            books = client.search("test")
            
            assert mock_get.call_count == 2
            assert mock_sleep.call_count == 1
            mock_sleep.assert_called_with(1) # 2^0
            assert books == []

def test_search_max_retries_exceeded():
    client = GoogleBooksClient(max_retries=1)
    
    mock_response_429 = MagicMock()
    mock_response_429.status_code = 429
    mock_response_429.raise_for_status.side_effect = httpx.HTTPStatusError(
        "Rate Limit", request=MagicMock(), response=mock_response_429
    )
    
    with patch("httpx.Client.get") as mock_get:
        mock_get.return_value = mock_response_429
        
        with patch("time.sleep") as mock_sleep:
            with pytest.raises(httpx.HTTPStatusError):
                client.search("test")
            
            assert mock_get.call_count == 2 # Initial + 1 retry
            assert mock_sleep.call_count == 1
