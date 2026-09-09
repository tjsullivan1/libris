from unittest.mock import MagicMock, patch

import httpx
import pytest

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
            mock_sleep.assert_called_with(1)  # 2^0
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

            assert mock_get.call_count == 2  # Initial + 1 retry
            assert mock_sleep.call_count == 1


# --- the code after the retry loop (#96) ------------------------------------


def test_a_negative_retry_count_says_so_rather_than_raising_NameError():
    # Given a client configured to attempt nothing at all, which makes
    # `range(max_retries + 1)` empty so the loop body never runs
    client = GoogleBooksClient(max_retries=-1)

    # When a search is attempted
    # Then it says what is wrong. What used to sit after the loop read
    # `response`, bound only inside it, so this raised NameError - and the
    # comment above it described a success path that cannot happen (#96).
    with patch("httpx.Client") as mock_client:
        mock_client.return_value.__enter__.return_value = MagicMock()
        with pytest.raises(AssertionError, match="max_retries=-1"):
            client.search("test")


def test_the_retry_loop_still_returns_on_a_first_try_success():
    # Given a client and a request that succeeds immediately
    client = GoogleBooksClient(max_retries=2)

    ok = MagicMock()
    ok.status_code = 200
    ok.raise_for_status.return_value = None
    ok.json.return_value = {"items": []}

    # When it is searched
    with patch("httpx.Client") as mock_client:
        mock_client.return_value.__enter__.return_value.get.return_value = ok
        results = client.search("test")

    # Then the loop returns rather than falling through to the guard above
    assert results == []
