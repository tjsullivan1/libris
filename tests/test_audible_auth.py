import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from libris.audible_client import get_auth_file
from libris.cli import app

runner = CliRunner()

# The audible commands use lazy `import audible` inside the function body,
# so we patch the audible module directly.
PATCH_AUTH = "audible.Authenticator"


def _write_fake_auth(config_dir, **overrides):
    """Write a minimal fake auth JSON file for testing."""
    data = {
        "adp_token": "fake-adp",
        "device_private_key": "fake-key",
        "access_token": "fake-access",
        "refresh_token": "fake-refresh",
        "expires": (datetime.now(timezone.utc) + timedelta(hours=1)).timestamp(),
        "device_info": {"device_name": "Test Device"},
        "customer_info": {"name": "Test User"},
        "locale_code": "us",
    }
    data.update(overrides)
    auth_file = config_dir / "audible_auth.json"
    auth_file.write_text(json.dumps(data))
    return auth_file


# --- login tests ---


def test_login_already_authenticated(mock_config_dir):
    _write_fake_auth(mock_config_dir)
    result = runner.invoke(app, ["audible", "login"])
    assert result.exit_code == 0
    assert "Already authenticated" in result.output


def test_login_success(mock_config_dir):
    mock_auth = MagicMock()
    mock_auth.device_info = {"device_name": "Libris Test Device"}

    with patch(f"{PATCH_AUTH}.from_login_external", return_value=mock_auth) as mock_login:
        result = runner.invoke(app, ["audible", "login"], input="https://example.com?openid.oa2.authorization_code=abc\n")
        assert result.exit_code == 0
        assert "Successfully authenticated" in result.output
        assert "Libris Test Device" in result.output
        mock_login.assert_called_once()
        mock_auth.to_file.assert_called_once()


def test_login_with_locale_saves_config(mock_config_dir):
    mock_auth = MagicMock()
    mock_auth.device_info = {"device_name": "Test Device"}

    with patch(f"{PATCH_AUTH}.from_login_external", return_value=mock_auth):
        result = runner.invoke(app, ["audible", "login", "--locale", "uk"], input="https://example.com?code=abc\n")
        assert result.exit_code == 0

    from libris.config import get_config
    config = get_config()
    assert config.get("audible_locale") == "uk"


def test_login_failure(mock_config_dir):
    with patch(f"{PATCH_AUTH}.from_login_external", side_effect=Exception("Network error")):
        result = runner.invoke(app, ["audible", "login"], input="https://example.com?code=abc\n")
        assert result.exit_code == 1
        assert "Authentication failed" in result.output


# --- logout tests ---


def test_logout_not_authenticated(mock_config_dir):
    result = runner.invoke(app, ["audible", "logout"])
    assert result.exit_code == 0
    assert "Not currently authenticated" in result.output


def test_logout_success(mock_config_dir):
    auth_file = _write_fake_auth(mock_config_dir)
    assert auth_file.exists()

    mock_auth = MagicMock()
    mock_auth.device_info = {"device_name": "Test Device"}

    with patch(f"{PATCH_AUTH}.from_file", return_value=mock_auth):
        result = runner.invoke(app, ["audible", "logout"])
        assert result.exit_code == 0
        assert "Deregistered" in result.output
        assert "Logged out" in result.output
        mock_auth.refresh_access_token.assert_called_once()
        mock_auth.deregister_device.assert_called_once()

    assert not auth_file.exists()


def test_logout_deregister_fails_still_removes_file(mock_config_dir):
    auth_file = _write_fake_auth(mock_config_dir)

    with patch(f"{PATCH_AUTH}.from_file", side_effect=Exception("corrupt file")):
        result = runner.invoke(app, ["audible", "logout"])
        assert result.exit_code == 0
        assert "Warning" in result.output
        assert "Logged out" in result.output

    assert not auth_file.exists()


# --- status tests ---


def test_status_not_authenticated(mock_config_dir):
    result = runner.invoke(app, ["audible", "status"])
    assert result.exit_code == 0
    assert "Not authenticated" in result.output


def test_status_authenticated(mock_config_dir):
    _write_fake_auth(mock_config_dir)

    mock_auth = MagicMock()
    mock_auth.device_info = {"device_name": "Test Device"}
    mock_auth.expires = (datetime.now(timezone.utc) + timedelta(minutes=30)).timestamp()

    mock_locale = MagicMock()
    mock_locale.country_code = "us"
    mock_auth.locale = mock_locale

    with patch(f"{PATCH_AUTH}.from_file", return_value=mock_auth):
        result = runner.invoke(app, ["audible", "status"])
        assert result.exit_code == 0
        assert "Authenticated" in result.output
        assert "us" in result.output
        assert "Test Device" in result.output
        assert "min" in result.output


def test_status_token_expired(mock_config_dir):
    _write_fake_auth(
        mock_config_dir,
        expires=(datetime.now(timezone.utc) - timedelta(hours=1)).timestamp(),
    )

    mock_auth = MagicMock()
    mock_auth.device_info = {"device_name": "Test Device"}
    mock_auth.expires = (datetime.now(timezone.utc) - timedelta(hours=1)).timestamp()

    mock_locale = MagicMock()
    mock_locale.country_code = "us"
    mock_auth.locale = mock_locale

    with patch(f"{PATCH_AUTH}.from_file", return_value=mock_auth):
        result = runner.invoke(app, ["audible", "status"])
        assert result.exit_code == 0
        assert "Expired" in result.output


# --- token persistence test ---


def test_get_library_persists_tokens(mock_config_dir):
    auth_file = _write_fake_auth(mock_config_dir)

    mock_library_response = {
        "items": [
            {
                "title": "Test Book",
                "authors": [{"name": "Author"}],
                "asin": "B001",
            }
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

        from libris.audible_client import AudibleClient
        client = AudibleClient()
        client.get_library()

        # Verify to_file was called to persist tokens
        mock_auth.to_file.assert_called_once_with(filename=str(auth_file))
