from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from libris.api import GoogleBooksClient, parse_retry_after


def _rate_limited_response(headers: dict[str, str] | None = None) -> MagicMock:
    response = MagicMock()
    response.status_code = 429
    response.headers = headers or {}
    return response


def _ok_response() -> MagicMock:
    response = MagicMock()
    response.status_code = 200
    response.headers = {}
    response.json.return_value = {"items": []}
    return response


def test_parse_retry_after_accepts_delta_seconds():
    # Given a Retry-After header expressed in seconds
    # When parsing it
    # Then the numeric value is returned
    assert parse_retry_after("42") == 42.0


def test_parse_retry_after_accepts_http_date():
    # Given a Retry-After header expressed as an HTTP-date 60s in the future
    now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    header = (now + timedelta(seconds=60)).strftime("%a, %d %b %Y %H:%M:%S GMT")

    # When parsing it relative to now
    seconds = parse_retry_after(header, now=now)

    # Then roughly 60 seconds are returned
    assert seconds == 60.0


def test_parse_retry_after_returns_none_for_garbage():
    # Given malformed or missing header values
    # When parsing them
    # Then None is returned so the caller can fall back to backoff
    assert parse_retry_after(None) is None
    assert parse_retry_after("soon") is None
    assert parse_retry_after("") is None
    assert parse_retry_after(MagicMock()) is None


def test_parse_retry_after_never_returns_negative():
    # Given a Retry-After date already in the past
    now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    header = (now - timedelta(seconds=90)).strftime("%a, %d %b %Y %H:%M:%S GMT")

    # When parsing it
    # Then the wait is clamped to zero
    assert parse_retry_after(header, now=now) == 0.0


def test_search_waits_for_retry_after_header():
    # Given a 429 response asking the client to wait 7 seconds
    client = GoogleBooksClient(max_retries=2)
    responses = [_rate_limited_response({"Retry-After": "7"}), _ok_response()]

    with patch("httpx.Client.get", side_effect=responses):
        with patch("time.sleep") as mock_sleep:
            # When searching
            client.search("test")

    # Then the client sleeps for exactly that duration
    mock_sleep.assert_called_once_with(7.0)


def test_search_reports_wait_time_to_user():
    # Given a rate-limited response and a notification callback
    notices: list[tuple[float, int, int]] = []
    client = GoogleBooksClient(
        max_retries=2,
        on_rate_limit=lambda wait, attempt, total: notices.append(
            (wait, attempt, total)
        ),
    )
    responses = [_rate_limited_response({"Retry-After": "12"}), _ok_response()]

    with patch("httpx.Client.get", side_effect=responses):
        with patch("time.sleep"):
            # When searching
            client.search("test")

    # Then the user is told how long the client is waiting
    assert notices == [(12.0, 1, 2)]


def test_default_rate_limit_notice_prints_wait_time(capsys):
    # Given a client using the default notifier
    client = GoogleBooksClient(max_retries=1)
    responses = [_rate_limited_response({"Retry-After": "5"}), _ok_response()]

    with patch("httpx.Client.get", side_effect=responses):
        with patch("time.sleep"):
            # When searching
            client.search("test")

    # Then the wait time is printed for the user
    assert "Waiting 5s" in capsys.readouterr().err


def test_search_caps_absurd_retry_after_values():
    # Given a server asking us to wait a full day
    client = GoogleBooksClient(max_retries=1, max_retry_wait=60.0)
    responses = [_rate_limited_response({"Retry-After": "86400"}), _ok_response()]

    with patch("httpx.Client.get", side_effect=responses):
        with patch("time.sleep") as mock_sleep:
            # When searching
            client.search("test")

    # Then the wait is capped at max_retry_wait
    mock_sleep.assert_called_once_with(60.0)


def test_search_falls_back_to_backoff_without_retry_after():
    # Given 429 responses with no Retry-After header
    client = GoogleBooksClient(max_retries=2)
    responses = [
        _rate_limited_response(),
        _rate_limited_response(),
        _ok_response(),
    ]

    with patch("httpx.Client.get", side_effect=responses):
        with patch("time.sleep") as mock_sleep:
            # When searching
            client.search("test")

    # Then exponential backoff is used
    assert [call.args[0] for call in mock_sleep.call_args_list] == [1.0, 2.0]
